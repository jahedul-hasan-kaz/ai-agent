"""Opt-out handling: the n8n Opt-out Detector port (app/services/opt_out.py) and the gate that runs before any agent
(ChatService._opt_out_gate). Cases come from the detector's own version notes (real PROD messages)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.llm.base import LLMError
from app.models.chat import ChatCompletionRequest
from app.services.chat_service import ChatService
from app.services.opt_out import OPT_OUT_CONFIRMATION, WRONG_NUMBER_CONFIRMATION, confirmation_text, detect

TENANT = "22222222-2222-2222-2222-222222222222"
LEAD = {"id": "11111111-1111-1111-1111-111111111111", "tenant_id": TENANT, "phone": "+15551234567"}


# ---- the detector -----------------------------------------------------------------------------------------------

OPT_OUTS = [
    "STOP", "stop ✋", "Stop,", "S t, o p.", "STOPPPPP", "St0p", "5top", "Sto", "stopstop", "STÓP", "unsubscribe",
    "Stop texting me, I'm not interested", "Please leave me alone", "leave me the fuck alone", "Take me off your list",
    "remove me", "delete my number", "lose my #", "Wrong number", "wrong #", "not interested", "I'm not interested",
    "Stop calling", "STOP. I no longer own this car.", "No. Stop", "I sold the ProMaster City STOP", "I don't care, stop",
    "🛑", "⛔️", "Out", "count me out", "Don't text me anymore", "never contact me again", "quit messaging me",
    "Stop bothering me", "I already opted out", "We've previously opted out", "do not call list",
    "Don't send me spam texts", "Take me off your list. Don't ever text me payoff numbers.",
    "I don't want to unsubscribe, but stop texting me", "Don't unsubscribe me, I want to opt out",
    "I don't want to opt out, unsubscribe me", "leave me alone but what is it worth", "go away but what is it worth",
    "I already told you I'm not interested", "STOP please", "opt me out", "fuck off",
]
NOT_OPT_OUTS = [
    "Stop by anytime", "Can I stop by Friday?", "End of August would be great", "one stop", "I do not want to opt out.",
    "I don’t want to opt out", "I don't want to opt out or unsubscribe from anything",
    "I opted out of the extended warranty",
    "Sure. I am NOT interested in trading or selling it, but I am curious about its present value.",
    "I'm not interested in selling, what is it worth?", "don't ever text me the payoff numbers",
    "he never text me back thanks anyway", "He stol my car", "Out?", "Do you have any used Tacomas?",
    "Thanks, see you then!", "Sounds good! Reply STOP to opt out", "take me off-road this weekend",
    "do not call me before noon", "It has about 62,000 miles", "Can I talk to a salesperson?", "Yes", "👋",
]


@pytest.mark.parametrize("message", OPT_OUTS)
def test_opt_outs(message):
    assert detect(message).is_opt_out, message


@pytest.mark.parametrize("message", NOT_OPT_OUTS)
def test_not_opt_outs(message):
    assert not detect(message).is_opt_out, message


def test_reaction_echo_of_our_footer_is_never_an_opt_out():
    for echo in ('Liked “Sounds good. Reply STOP to opt out.”', '❤️ to "See you Tuesday. Reply STOP to opt out"',
                 'Le gustó “Gracias. Reply STOP to opt out.”'):
        d = detect(echo)
        assert d.is_reaction_echo and not d.is_opt_out and not d.is_opt_in, echo


def test_opt_ins():
    assert detect("START").is_opt_in and detect("unstop").is_opt_in and detect("Opt me back in").is_opt_in
    # conversational and bare-affirmative opt-ins only for a lead that is already opted out
    assert not detect("Text me").is_opt_in and detect("Text me", lead_opted_out=True).is_opt_in
    assert not detect("yes").is_opt_in and detect("yes", lead_opted_out=True).is_opt_in
    assert not detect("START stop").is_opt_in  # opt-out wins ties


def test_confirmation_text():
    assert confirmation_text("STOP") == OPT_OUT_CONFIRMATION
    assert confirmation_text("wrong number") == WRONG_NUMBER_CONFIRMATION == confirmation_text("Wrong # sorry")


# ---- the gate ---------------------------------------------------------------------------------------------------


class FakePool:
    def __init__(self, status="new", lifecycle="active", dnc=False, blocked=False, dnc_row=None, opt_out_result=None):
        self.row = {"status": status, "lifecycle_stage": lifecycle, "dnc": dnc, "blocked": blocked}
        self.dnc_row, self.opt_out_result, self.calls = dnc_row, opt_out_result, []

    async def fetchrow(self, sql, *args):
        return self.row

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if "process_opt_out" in sql:
            if isinstance(self.opt_out_result, Exception):
                raise self.opt_out_result
            return json.dumps(self.opt_out_result or {"success": True, "lead_updated": 1, "cadences_stopped": 1})
        return self.dnc_row


def _gate(text, live=False, pool=None, **body):
    svc = ChatService(SimpleNamespace(resolve=lambda m, t: SimpleNamespace(id="m", type=None)))
    pool = pool or FakePool()

    async def lead():
        return LEAD

    ctx = SimpleNamespace(pool=pool, lead=lead, inbound_text=text, live=live)
    body.setdefault("sms_thread_key", LEAD["phone"])
    req = ChatCompletionRequest(model="m", agent="scheduling", tenant_id=TENANT,
                                messages=[{"role": "user", "content": text}], **body)
    return asyncio.run(svc._opt_out_gate(req, ctx)), pool


def test_opt_out_is_answered_with_the_fixed_confirmation_and_no_agent():
    answer, pool = _gate("Stop texting me, I'm not interested")
    assert answer.result.content == OPT_OUT_CONFIRMATION and answer.routing.outcome == "opted_out"
    [run] = answer.runs
    assert run.name == "Opt_Out" and run.ok and run.dry_run  # dry run: process_opt_out not called
    assert not any("process_opt_out" in sql for sql, _ in pool.calls)


def test_live_opt_out_calls_process_opt_out_like_n8n():
    answer, pool = _gate("STOP", live=True)
    [(sql, args)] = [c for c in pool.calls if "process_opt_out" in c[0]]
    assert args == (TENANT, LEAD["id"], LEAD["phone"], "SMS opt-out")
    assert answer.runs[0].ok and not answer.runs[0].dry_run


def test_wrong_number_gets_its_own_confirmation():
    answer, _ = _gate("Wrong number")
    assert answer.result.content == WRONG_NUMBER_CONFIRMATION


def test_a_failed_opt_out_is_an_error_not_a_false_confirmation():
    with pytest.raises(LLMError) as err:
        _gate("STOP", live=True, pool=FakePool(opt_out_result=RuntimeError("db down")))
    assert err.value.code == "opt_out_failed"
    with pytest.raises(LLMError):
        _gate("STOP", live=True, pool=FakePool(opt_out_result={"success": False}))


@pytest.mark.parametrize("pool", [FakePool(status="opted_out"), FakePool(lifecycle="opted_out"), FakePool(dnc=True),
                                  FakePool(blocked=True), FakePool(dnc_row=1)])
def test_an_opted_out_customer_gets_no_reply(pool):
    answer, _ = _gate("Hey, what's my car worth?", pool=pool)
    assert answer.result.content == "<NOOP>" and answer.routing.outcome == "silent_opted_out" and answer.runs == []


def test_normal_messages_go_to_the_agents():
    for text in ("Do you have any used Tacomas?", "Can I stop by Friday?", "Thanks, see you then!"):
        answer, pool = _gate(text)
        assert answer is None and not any("process_opt_out" in sql for sql, _ in pool.calls)


def test_gate_is_skipped_when_off_or_not_sms():
    assert _gate("STOP", opt_out_gate=False)[0] is None
    assert _gate("STOP", sms_thread_key=None, email_thread_key="a@b.com")[0] is None

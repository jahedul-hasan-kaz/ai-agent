"""<NOOP> handling, ported from the n8n Empty Reply Detector and NOOP Pivot Re-Router (app/services/noop_router.py):
silence for a closing, another agent for a real message, the safe fallback when that agent declines too."""
import json

import pytest

from app.llm.base import LLMResult, StreamDone
from app.services.agent_prompts import get_agent
from app.services.noop_router import fallback_text, is_closing, is_noop, is_substantive, is_tapback, reroute_target

from tests.test_tools import _post, client, providers  # noqa: F401 - fixtures

NOOP = LLMResult("<NOOP>", "stop", 10, 1)


def _reply(text: str) -> LLMResult:
    return LLMResult(text, "stop", 10, 5)


def _agent_of(request) -> str:
    system = request.messages[0]["content"]
    return next(a for a in ("value_data_collection", "scheduling", "inventory", "service_trade_reply", "general")
                if system.startswith(get_agent(a).text))


# ---- the rules ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["<NOOP>", "noop", "<<NOOP>>", "[noop]", "(NOOP)", '"<NOOP>."', "<NOOP/>", " <noop> "])
def test_noop_spellings(reply):
    assert is_noop(reply)


@pytest.mark.parametrize("reply", ["No op", "Sure", "Nope", "I can't help with that."])
def test_not_noop(reply):
    assert not is_noop(reply)


@pytest.mark.parametrize("message", ["Ok, thanks!", "Thanks, see you then!", "see you tomorrow at 2pm", "Appreciate it",
                                     "Thanks, have a good day", "Got it, thank you", "You too 😊", "Ok thanks, bye!",
                                     "thank you so much", "perfect thanks"])
def test_closings(message):
    assert is_closing(message) and not is_substantive(message)


@pytest.mark.parametrize("message", ["What's my 2018 Camry worth?", "Can I talk to a salesperson?", "Thanks 30000",
                                     "thank you interested", "I already told you I'm not interested",
                                     "Maybe next year, around spring"])
def test_substantive(message):
    assert is_substantive(message)


def test_yes_answering_our_question_is_substantive():
    assert not is_substantive("Yes") and is_substantive("Yes", "Would you like a quick call with our buyer?")
    assert is_substantive("Ok sounds good", "Want me to pull your number?")


def test_reactions_and_emoji_only_are_not_substantive():
    assert is_tapback('Liked “Sounds good, see you Tuesday!”') and not is_substantive('Liked “Sounds good, see you Tuesday!”')
    assert is_tapback('❤️ to "See you then"') and not is_substantive("👍")


@pytest.mark.parametrize("message,origin,target", [
    ("What's my 2018 Camry worth?", "inventory", "value_data_collection"),
    ("Can I come in Friday at 3?", "inventory", "scheduling"),
    ("Do you have any used Tacomas?", "scheduling", "inventory"),
    ("Can I talk to a salesperson?", "inventory", "general"),
    ("Do you have any RAV4s in stock?", "inventory", "general"),  # never back to the agent that declined
    ("hello there", "general", "general"),  # general gets a second opinion
])
def test_reroute_target(message, origin, target):
    assert reroute_target(message, origin) == target


def test_fallback_copy():
    assert fallback_text("inventory", "Sam").startswith("Hey Sam — give me a few minutes")
    assert fallback_text("general") == "Let me take a quick look into that, and someone from our team will follow up shortly."
    assert fallback_text("service_trade_reply").startswith("Let me take a quick look")
    assert fallback_text("value_data_collection", "", complaint=True).startswith("I'm sorry about that. I've flagged it")


# ---- through the API ------------------------------------------------------------------------------------------


def _ask(client, agent, text, **body):  # noqa: F811
    return _post(client, agent=agent, use_tools=False, messages=[{"role": "user", "content": text}], **body).json()


def test_a_pivot_is_answered_by_the_agent_it_points_to(client, providers):  # noqa: F811
    providers["claude"].script = [NOOP, _reply("Happy to help! What's the mileage on your Camry?")]
    data = _ask(client, "inventory", "What's my 2018 Camry worth?")
    assert data["choices"][0]["message"]["content"] == "Happy to help! What's the mileage on your Camry?"
    assert data["routing"] == {"outcome": "rerouted", "from_agent": "inventory", "to_agent": "value_data_collection"}
    first, second = providers["claude"].requests
    assert (_agent_of(first), _agent_of(second)) == ("inventory", "value_data_collection")
    assert data["usage"]["total_tokens"] == 26  # both model calls


def test_a_closing_stays_silent(client, providers):  # noqa: F811
    providers["claude"].script = [NOOP]
    data = _ask(client, "inventory", "Ok, thanks!")
    assert data["choices"][0]["message"]["content"] == "<NOOP>"
    assert data["routing"]["outcome"] == "silent_closing" and len(providers["claude"].requests) == 1


def test_noop_spellings_are_normalized(client, providers):  # noqa: F811
    providers["claude"].script = [LLMResult("<<noop>>", "stop", 1, 1)]
    assert _ask(client, "scheduling", "thanks, bye")["choices"][0]["message"]["content"] == "<NOOP>"


def test_two_declines_send_the_safe_fallback(client, providers):  # noqa: F811
    providers["claude"].script = [NOOP, NOOP]
    data = _ask(client, "inventory", "What's my 2018 Camry worth?")
    assert data["choices"][0]["message"]["content"].startswith("Give me a few minutes to pull this up properly")
    assert data["routing"] == {"outcome": "fallback", "from_agent": "inventory", "to_agent": "value_data_collection"}


def test_complaint_classifier_gets_the_complaint_fallback(client, providers):  # noqa: F811
    providers["claude"].script = [NOOP, NOOP]
    data = _ask(client, "inventory", "This is ridiculous, nobody called me back",
                node_data={"Intent Classifier": {"intent": "complaint", "complaint_signals": True}})
    assert data["choices"][0]["message"]["content"].startswith("I'm sorry about that. I've flagged it for our team")


def test_general_declining_twice_stays_silent(client, providers):  # noqa: F811
    providers["claude"].script = [NOOP, NOOP]
    data = _ask(client, "general", "hmm interesting")
    assert data["choices"][0]["message"]["content"] == "<NOOP>"
    assert data["routing"] == {"outcome": "silent_confirmed", "from_agent": "general", "to_agent": "general"}


def test_an_answer_to_our_question_is_rerouted(client, providers):  # noqa: F811
    providers["claude"].script = [NOOP, _reply("Great, I'll get that set up.")]
    data = _post(client, agent="inventory", use_tools=False, messages=[
        {"role": "assistant", "content": "Would you like a quick call with one of our buyers?"},
        {"role": "user", "content": "Yes"},
    ]).json()
    assert data["routing"]["outcome"] == "rerouted" and data["routing"]["to_agent"] == "general"


def test_reroute_can_be_turned_off(client, providers):  # noqa: F811
    providers["claude"].script = [NOOP]
    data = _ask(client, "inventory", "What's my 2018 Camry worth?", reroute_noop=False)
    assert data["choices"][0]["message"]["content"] == "<NOOP>" and data["routing"] is None
    assert len(providers["claude"].requests) == 1


def test_a_normal_reply_is_untouched(client, providers):  # noqa: F811
    providers["claude"].script = [_reply("We have 2 RAV4s in stock.")]
    data = _ask(client, "inventory", "Do you have any RAV4s?")
    assert data["choices"][0]["message"]["content"] == "We have 2 RAV4s in stock." and data["routing"] is None


def test_with_tools_the_pivot_is_rerouted_too(client, providers):  # noqa: F811
    providers["claude"].script = [NOOP, _reply("Let me check our inventory for you.")]
    data = _post(client, agent="scheduling", messages=[{"role": "user", "content": "Do you have any used Tacomas?"}]).json()
    assert data["routing"] == {"outcome": "rerouted", "from_agent": "scheduling", "to_agent": "inventory"}


# ---- streaming ------------------------------------------------------------------------------------------------


def _stream_lines(client, **body):  # noqa: F811
    body.update(model="claude", stream=True)
    with client.stream("POST", "/v1/chat/completions", json=body) as res:
        return [json.loads(line[6:]) for line in res.iter_lines() if line.startswith("data: {")]


def _scripted_stream(provider, *chunks):
    async def stream(request):
        provider.requests.append(request)
        for c in chunks:
            yield c
        yield StreamDone("stop", 10, 1)
    provider.stream = stream


def _content(events) -> str:
    return "".join(e["choices"][0]["delta"].get("content", "") for e in events if "choices" in e)


def test_streamed_noop_is_held_back_and_rerouted(client, providers):  # noqa: F811
    _scripted_stream(providers["claude"], "<NO", "OP>")
    providers["claude"].script = [_reply("What's the current mileage on it?")]
    events = _stream_lines(client, agent="inventory", use_tools=False,
                           messages=[{"role": "user", "content": "What's my 2018 Camry worth?"}])
    assert _content(events) == "What's the current mileage on it?"  # the <NOOP> never reached the client
    assert events[-1]["routing"] == {"outcome": "rerouted", "from_agent": "inventory", "to_agent": "value_data_collection"}


def test_streamed_closing_noop_stays_silent(client, providers):  # noqa: F811
    _scripted_stream(providers["claude"], "<NOOP>")
    events = _stream_lines(client, agent="inventory", use_tools=False, messages=[{"role": "user", "content": "ok thanks"}])
    assert _content(events) == "<NOOP>" and events[-1]["routing"]["outcome"] == "silent_closing"


def test_a_normal_stream_still_streams(client, providers):  # noqa: F811
    _scripted_stream(providers["claude"], "No", "pe, we ", "have 2 in stock.")
    events = _stream_lines(client, agent="inventory", use_tools=False, messages=[{"role": "user", "content": "any RAV4s?"}])
    assert _content(events) == "Nope, we have 2 in stock." and "routing" not in events[-1]
    assert len([e for e in events if e.get("choices") and e["choices"][0]["delta"].get("content")]) >= 2


# ---- repeat decline safety net (General) ----------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from app.models.chat import ChatCompletionRequest  # noqa: E402
from app.services.chat_service import ChatService, _Answer  # noqa: E402
from app.services.noop_router import Routing  # noqa: E402
from app.tools.registry import ALL_TOOLS, ToolRun  # noqa: E402

FIRST_TIME = "No problem at all - thanks for letting me know. If anything changes down the road, I'm just a text away. Take care!"


class StubTools:
    settings = SimpleNamespace(enabled=True)

    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def offered(self, agent, ctx):
        return [ALL_TOOLS["Suppress_Lead"]], {}

    async def run(self, tools, call, ctx):
        self.calls.append(call)
        return "{}", ToolRun(call.id, call.name, "code", call.arguments, self.ok, 0)


def _net(text, agent="general", ok=True, runs=None, routing=None, node_data=None):
    svc = ChatService(None)
    svc.tools = StubTools(ok)
    req = ChatCompletionRequest(model="m", agent=agent, messages=[{"role": "user", "content": text}], node_data=node_data)
    answer = _Answer(model=None, result=LLMResult(FIRST_TIME, "stop", 1, 1), fallback_used=False, runs=list(runs or []),
                     ctx=SimpleNamespace(pool=None, tenant_id=None), routing=routing)
    import asyncio
    out = asyncio.run(svc._ensure_repeat_decline_suppressed(req, answer))
    return out, svc.tools.calls


def test_repeat_decline_is_suppressed_when_general_forgot():
    out, calls = _net("I already told you I'm not interested")
    assert [c.arguments["suppression_category"] for c in calls] == ["repeat_decline"]
    assert out.result.content.startswith("Understood — I'll make sure you don't get further messages")
    assert out.tool_calls[-1].name == "Suppress_Lead"


def test_failed_suppression_never_promises_messages_stop():
    out, _ = _net("I already told you I'm not interested", ok=False)
    assert out.result.content == "Sorry for the bother. Reply STOP and you won't receive further messages."


def test_rerouted_to_general_counts():
    out, calls = _net("I already told you I'm not interested", agent="inventory",
                      routing=Routing("rerouted", "inventory", "general"))
    assert calls and out.result.content.startswith("Understood")


@pytest.mark.parametrize("kwargs", [
    {"text": "No thanks"},  # a first decline: the prompt says not to suppress
    {"text": "Stop calling me, text me instead"},  # calling only: not an opt-out of texts
    {"text": "Can you stop by Friday to look at it?"},  # "stop by" is not a stop request
    {"text": "Do you have any used Tacomas?"},
    {"text": "I already told you I'm not interested", "runs": [ToolRun("x", "Suppress_Lead", "code", {}, False, 0)]},
    {"text": "I already told you I'm not interested", "runs": [ToolRun("x", "Escalate_To_Human", "code", {}, True, 0)]},
    # The classifier says complaint: complaint work, not a protected close.
    {"text": "I already told you I'm not interested, get me a manager",
     "node_data": {"Intent Classifier": {"intent": "complaint", "complaint_signals": True}}},
])
def test_safety_net_leaves_other_turns_alone(kwargs):
    out, calls = _net(**kwargs)
    assert calls == [] and out.result.content == FIRST_TIME


def test_classifier_repeat_decline_triggers_it_without_the_words():
    out, calls = _net("please just stop", node_data={"Intent Classifier": {"intent": "repeat_decline"}})
    assert calls and out.result.content.startswith("Understood")



@pytest.mark.parametrize("agent", ["scheduling", "inventory", "value_data_collection", "service_trade_reply", "general"])
@pytest.mark.parametrize("text", ["Stop texting me, I'm not interested", "Please leave me alone",
                                  "Take me off your list", "Don't text me anymore", "quit messaging me",
                                  "I already told you I'm not interested"])
def test_any_agent_reply_to_a_stop_request_is_suppressed(agent, text):
    out, calls = _net(text, agent=agent)
    assert [c.arguments["suppression_category"] for c in calls] == ["repeat_decline"]
    assert "permanent_dnc" not in calls[0].arguments  # halt + hold only, never a DNC
    assert out.result.content.startswith("Understood")


def test_stop_request_with_a_non_complaint_classifier_is_suppressed():
    out, calls = _net("Stop texting me", node_data={"Intent Classifier": {"intent": "not_interested_soft"}})
    assert calls and out.result.content.startswith("Understood")


def test_noop_replies_are_left_to_the_router():
    svc_out, calls = _net_reply("<NOOP>", "Stop texting me")
    assert calls == [] and svc_out.result.content == "<NOOP>"


def _net_reply(reply, text):
    svc = ChatService(None)
    svc.tools = StubTools()
    req = ChatCompletionRequest(model="m", agent="scheduling", messages=[{"role": "user", "content": text}])
    answer = _Answer(model=None, result=LLMResult(reply, "stop", 1, 1), fallback_used=False,
                     ctx=SimpleNamespace(pool=None, tenant_id=None))
    import asyncio
    return asyncio.run(svc._ensure_repeat_decline_suppressed(req, answer)), svc.tools.calls


# ---- B. empty reply fallback ------------------------------------------------------------------------------------

from app.llm.base import LLMError as _LLMError, ToolCall  # noqa: E402


class EscalateTools:
    settings = SimpleNamespace(enabled=True)

    def __init__(self, ok=True, offered=True):
        self.ok, self._offered, self.calls = ok, offered, []

    def offered(self, agent, ctx):
        return ([ALL_TOOLS["Escalate_To_Human"]] if self._offered else []), {}

    async def run(self, tools, call, ctx):
        self.calls.append(call)
        return "{}", ToolRun(call.id, call.name, "code", call.arguments, self.ok, 0, None, True)


def _empty(text="maybe I could swing by wednesday around 5?", code="empty_response", tools=None, **body):
    svc = ChatService(SimpleNamespace(resolve=lambda m, t: SimpleNamespace(id="m", type=None)))
    svc.tools = tools or EscalateTools()

    async def lead():
        return {"first_name": "Artrice"}

    ctx = SimpleNamespace(pool=None, tenant_id="t", lead=lead)
    body.setdefault("tenant_id", "22222222-2222-2222-2222-222222222222")
    body.setdefault("sms_thread_key", "+15551234567")
    req = ChatCompletionRequest(model="m", agent="scheduling", messages=[{"role": "user", "content": text}], **body)
    import asyncio
    error = _LLMError("gpt-oss:20b returned an empty reply (4 tries)", code=code)
    return asyncio.run(svc._empty_fallback(req, error, ctx)), svc.tools


def test_empty_reply_gets_the_safe_fallback_and_a_page():
    answer, tools = _empty()
    assert answer.result.content == ("Hey Artrice — give me a few minutes to pull this up properly, and someone from "
                                      "our team will follow up shortly with the right number.")
    assert answer.routing.outcome == "empty_fallback" and [c.name for c in tools.calls] == ["Escalate_To_Human"]


@pytest.mark.parametrize("kwargs", [
    {"code": "provider_error"},  # only empty replies
    {"sms_thread_key": None, "tenant_id": None},  # no customer: nobody to follow up with
    {"tools": EscalateTools(ok=False)},  # page failed: no false promise
    {"tools": EscalateTools(offered=False)},  # no escalation possible
    {"reroute_noop": False},
])
def test_empty_reply_keeps_the_error_when_the_fallback_cannot_be_honest(kwargs):
    answer, _ = _empty(**kwargs)
    assert answer is None


# ---- C. a value question answered by the wrong agent ----------------------------------------------------------

from app.services.noop_router import asks_for_value  # noqa: E402


@pytest.mark.parametrize("message", ["what would you give me for my car? want to know before I come in",
                                     "What's my 2018 Camry worth?", "just tell me what it's worth",
                                     "how much would you pay for my truck", "can I get an offer on my Jeep"])
def test_value_questions(message):
    assert asks_for_value(message)


@pytest.mark.parametrize("message", ["how much is the RAV4?", "Do you have any used Tacomas?", "is the K5 worth it?",
                                     "can I come see it tomorrow at 4pm", "is it worth coming in on Saturday?"])
def test_not_value_questions(message):
    assert not asks_for_value(message)


def test_scheduling_answering_a_value_question_is_rerouted_to_value(client, providers):  # noqa: F811
    providers["claude"].script = [_reply("I don't have an offer ready. Give us a call."),
                                  _reply("Happy to help - what's the current mileage on it?")]
    data = _ask(client, "scheduling", "what would you give me for my car? want to know before I come in")
    assert data["choices"][0]["message"]["content"] == "Happy to help - what's the current mileage on it?"
    assert data["routing"] == {"outcome": "rerouted", "from_agent": "scheduling", "to_agent": "value_data_collection"}
    assert [_agent_of(r) for r in providers["claude"].requests] == ["scheduling", "value_data_collection"]


def test_value_reroute_leaves_other_turns_alone(client, providers):  # noqa: F811
    providers["claude"].script = [_reply("We have 2 RAV4s from $27,800.")]
    data = _ask(client, "inventory", "how much is the RAV4?")
    assert data["routing"] is None and len(providers["claude"].requests) == 1


def test_value_reroute_keeps_the_first_reply_if_value_declines(client, providers):  # noqa: F811
    providers["claude"].script = [_reply("Call us for an offer."), NOOP]
    data = _ask(client, "inventory", "what's my car worth?")
    assert data["choices"][0]["message"]["content"] == "Call us for an offer." and data["routing"] is None


def test_value_reroute_skips_turns_where_a_tool_ran(client, providers):  # noqa: F811
    providers["claude"].script = [
        LLMResult("", "tool_calls", 1, 1, [ToolCall("t1", "Validate_Appointment", {"proposed_time": "Friday at 3pm"})]),
        _reply("Friday at 3 works - want me to lock it in? We can talk numbers when you're here."),
    ]
    data = _post(client, agent="scheduling", messages=[{"role": "user", "content": "what's my car worth? can I come Friday at 3?"}]).json()
    assert data["routing"] is None and len(providers["claude"].requests) == 2

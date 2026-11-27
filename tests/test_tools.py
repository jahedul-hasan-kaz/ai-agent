import json
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import config
from app.llm.anthropic_provider import _alternating_turns
from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResult, StreamDone, StreamItem, ToolCall, flatten_tool_turns
from app.llm.model_manager import ModelManager
from app.llm.openai_compatible import _wire_messages
from app.llm.settings import AgentConfig, ModelConfig, ToolsConfig
from app.tools.appointments import DEFAULT_HOURS, check_appointment, parse_proposed_time
from app.tools.base import ToolContext
from app.tools.escalation import escalate_to_human, is_closing_filler_only, is_deferral_only, sanitize_named_person, suppress_lead
from app.tools.inventory import customer_inventory_url, hyphenated_model, slugify
from app.tools.records import stated_mileage, update_mileage
from app.tools.registry import AGENT_TOOLS, ALL_TOOLS, ToolRunner
from app.tools.appointments import save_appointment_notes

CONFIG = {
    "models": [
        {"id": "claude", "type": "frontier", "provider": "anthropic", "protocol": "anthropic", "model": "claude-sonnet-5-5",
         "api_key": "k", "base_url": "https://api.anthropic.com"},
        {"id": "plain", "type": "opensource", "provider": "ollama", "protocol": "openai", "model": "tiny",
         "base_url": "http://localhost:11434/v1", "supports_tools": False},
    ],
}
LEAD = {"id": "11111111-1111-1111-1111-111111111111", "tenant_id": "22222222-2222-2222-2222-222222222222",
        "phone": "+15551234567", "current_cadence_id": None}
# Monday 2026-11-16, 10:00 in New York.
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
WALL_NOW = datetime(2026, 11, 16, 10, 0)


class ScriptedProvider(LLMProvider):
    """Answers each call with the next scripted result (tool calls or text)."""

    def __init__(self, settings: ModelConfig) -> None:
        super().__init__(settings)
        self.script: list[LLMResult] = []
        self.requests: list[LLMRequest] = []
        self.error: LLMError | None = None

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self.error:
            error, self.error = self.error, None
            raise error
        return self.script.pop(0) if self.script else LLMResult("Done.", "stop", 10, 5)

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamItem]:
        self.requests.append(request)
        yield "Streamed."
        yield StreamDone("stop", 10, 5)

    async def list_models(self) -> list[str]:
        return [self.settings.model]


@pytest.fixture
def providers():
    return {}


@pytest.fixture
def client(providers, monkeypatch):
    def factory(settings):
        providers[settings.id] = ScriptedProvider(settings)
        return providers[settings.id]

    async def no_store(_):
        return None

    async def tools_without_db(cfg):
        return ToolRunner(cfg.tools, "")

    monkeypatch.setattr(config, "load_agent_config", lambda: AgentConfig.model_validate(CONFIG))
    monkeypatch.setattr(main, "ModelManager", lambda cfg: ModelManager(cfg, factory))
    monkeypatch.setattr(main, "_start_customers", no_store)
    monkeypatch.setattr(main, "_start_tools", tools_without_db)
    with TestClient(main.app) as c:
        yield c


def _ctx(text="", write_mode="dry_run", turns=None, **cache) -> ToolContext:
    ctx = ToolContext(
        settings=ToolsConfig(write_mode=write_mode), tenant_id=LEAD["tenant_id"], sms_thread_key="+15551234567",
        email_thread_key=None, turns=turns or [{"role": "user", "content": text}], now_utc=NOW,
    )
    ctx._cache.update({"lead": LEAD, "cadence": {}, **cache})
    return ctx


# ---- wiring ---------------------------------------------------------------------------------------------------


def test_agent_tools_match_the_n8n_wiring():
    assert AGENT_TOOLS == {
        "general": ("Escalate_To_Human", "Get_SMS_conversation_log", "Knowledge_Base_Search", "Suppress_Lead"),
        "inventory": ("Escalate_To_Human", "Search_Inventory"),
        "scheduling": ("Cancel_Appointment", "Escalate_To_Human", "Save_Appointment_Notes", "Validate_Appointment"),
        "value_data_collection": ("Escalate_To_Human", "Knowledge_Base_Search", "Save_Appointment_Notes", "Update_mileage"),
        "service_trade_reply": ("Escalate_To_Human", "Get_SMS_conversation_log", "Knowledge_Base_Search", "Suppress_Lead"),
    }
    assert {t.kind for t in ALL_TOOLS.values()} == {"db", "code", "http"}
    assert all(name in ALL_TOOLS for names in AGENT_TOOLS.values() for name in names)


def test_agents_endpoint_lists_tools(client):
    data = {a["id"]: a for a in client.get("/v1/agents").json()["data"]}
    assert data["inventory"]["tools"] == ["Escalate_To_Human", "Search_Inventory"]


def test_only_tools_the_request_can_run_are_offered():
    runner = ToolRunner(ToolsConfig())
    no_customer = ToolContext(settings=runner.settings, tenant_id=None, sms_thread_key=None, email_thread_key=None, turns=[])
    offered, skipped = runner.offered("scheduling", no_customer)
    assert [t.name for t in offered] == ["Validate_Appointment"]
    assert "lead" in skipped["Escalate_To_Human"] and "lead" in skipped["Cancel_Appointment"]
    offered, skipped = runner.offered("inventory", no_customer)
    assert offered == [] and "marketcheck" in skipped["Search_Inventory"] or "tenant" in skipped["Search_Inventory"]


# ---- the loop through the API ---------------------------------------------------------------------------------


def _post(client, **body):
    body.setdefault("model", "claude")
    body.setdefault("messages", [{"role": "user", "content": "Can I come in Tuesday at 2pm?"}])
    return client.post("/v1/chat/completions", json=body)


def test_tool_round_trip_reports_tools_used(client, providers):
    providers["claude"].script = [
        LLMResult("", "tool_calls", 100, 10, [ToolCall("t1", "Validate_Appointment", {"proposed_time": "Tuesday at 2pm"})]),
        LLMResult("Tuesday at 2PM works, see you then!", "stop", 150, 20),
    ]
    res = _post(client, agent="scheduling")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["choices"][0]["message"]["content"] == "Tuesday at 2PM works, see you then!"
    assert data["tools_used"] == ["Validate_Appointment"]
    [call] = data["tool_calls"]
    assert call["name"] == "Validate_Appointment" and call["kind"] == "code" and call["ok"] is True
    assert call["arguments"] == {"proposed_time": "Tuesday at 2pm"}
    assert data["usage"] == {"prompt_tokens": 250, "completion_tokens": 30, "total_tokens": 280}

    first, second = providers["claude"].requests
    assert [t.name for t in first.tools] == ["Validate_Appointment"]
    tool_turn = second.messages[-1]
    assert tool_turn["role"] == "tool" and tool_turn["tool_call_id"] == "t1"
    assert "is_valid" in json.loads(tool_turn["content"])


def test_unknown_tool_and_bad_arguments_go_back_to_the_model(client, providers):
    providers["claude"].script = [
        LLMResult("", "tool_calls", 1, 1, [ToolCall("a", "Search_Inventory", {"make": "Ford"}),
                                           ToolCall("b", "Validate_Appointment", {}, "arguments are not valid JSON")]),
        LLMResult("Let me check with the team.", "stop", 1, 1),
    ]
    data = _post(client, agent="scheduling").json()
    assert data["tools_used"] == ["Search_Inventory", "Validate_Appointment"]
    assert [c["ok"] for c in data["tool_calls"]] == [False, False]
    assert "unknown tool" in data["tool_calls"][0]["error"]


def test_max_iterations_forces_an_answer(client, providers, monkeypatch):
    call = LLMResult("", "tool_calls", 1, 1, [ToolCall("x", "Validate_Appointment", {"proposed_time": "monday 3pm"})])
    providers["claude"].script = [call] * 6 + [LLMResult("Answer.", "stop", 1, 1)]
    data = _post(client, agent="scheduling").json()
    assert data["choices"][0]["message"]["content"] == "Answer."
    assert len(data["tool_calls"]) == 6
    assert providers["claude"].requests[-1].tool_choice == "none"


def test_use_tools_false_and_agents_without_tools(client, providers):
    _post(client, agent="scheduling", use_tools=False)
    assert providers["claude"].requests[-1].tools == []
    # The inventory agent's tools need a customer or the database; neither is on this request.
    _post(client, agent="inventory")
    assert providers["claude"].requests[-1].tools == []
    # Knowledge_Base_Search reads the static knowledge base, so it needs nothing.
    _post(client, agent="general")
    assert [t.name for t in providers["claude"].requests[-1].tools] == ["Knowledge_Base_Search"]


def test_model_without_tool_support_gets_none(client, providers):
    res = _post(client, model="plain", agent="scheduling")
    assert res.status_code == 200 and res.json()["tools_used"] == []
    assert providers["plain"].requests[-1].tools == []


def test_model_rejecting_tools_answers_without_them(client, providers):
    providers["claude"].error = LLMError("anthropic rejected the request: tools are not supported", 400, False, "invalid_request")
    res = _post(client, agent="scheduling")
    assert res.status_code == 200, res.text
    assert providers["claude"].requests[-1].tools == []


def test_stream_with_tools_sends_tools_used_on_the_last_chunk(client, providers):
    providers["claude"].script = [
        LLMResult("", "tool_calls", 1, 1, [ToolCall("t", "Validate_Appointment", {"proposed_time": "Tuesday 2pm"})]),
        LLMResult("Booked in!", "stop", 1, 1),
    ]
    res = _post(client, agent="scheduling", stream=True)
    events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: {")]
    assert "".join(e["choices"][0]["delta"].get("content", "") for e in events) == "Booked in!"
    assert events[-1]["tools_used"] == ["Validate_Appointment"]
    # Without tools the reply still streams token by token.
    res = _post(client, agent="inventory", stream=True)
    events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: {")]
    assert events[-1]["tools_used"] == [] and "Streamed." in res.text


# ---- provider wire formats ------------------------------------------------------------------------------------


def _tool_conversation():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Tuesday 2pm?"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [ToolCall("t1", "Validate_Appointment", {"proposed_time": "Tuesday 2pm"})]},
        {"role": "tool", "tool_call_id": "t1", "name": "Validate_Appointment", "content": '{"is_valid": true}'},
    ]


def test_anthropic_turns_carry_tool_blocks():
    turns = _alternating_turns(_tool_conversation())
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[1]["content"] == [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "t1", "name": "Validate_Appointment", "input": {"proposed_time": "Tuesday 2pm"}},
    ]
    assert turns[2]["content"] == [{"type": "tool_result", "tool_use_id": "t1", "content": '{"is_valid": true}'}]
    # Claude's own blocks (thinking included) are sent back unchanged.
    raw = [{"type": "thinking", "thinking": "hm", "signature": "s"}, {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]
    conversation = _tool_conversation()
    conversation[2]["anthropic_blocks"] = raw
    assert _alternating_turns(conversation)[1]["content"] == raw


def test_openai_messages_carry_tool_calls():
    wire = _wire_messages(_tool_conversation())
    assert wire[2]["tool_calls"][0] == {
        "id": "t1", "type": "function", "function": {"name": "Validate_Appointment", "arguments": '{"proposed_time": "Tuesday 2pm"}'},
    }
    assert wire[3] == {"role": "tool", "tool_call_id": "t1", "content": '{"is_valid": true}'}


def test_flatten_tool_turns_for_models_without_tools():
    flat = flatten_tool_turns(_tool_conversation())
    assert [m["role"] for m in flat] == ["system", "user", "assistant", "user"]
    assert "Called Validate_Appointment" in flat[2]["content"] and "Result of Validate_Appointment" in flat[3]["content"]


# ---- Validate_Appointment -------------------------------------------------------------------------------------


def _check(text=None, **args):
    if text is not None:
        args["proposed_time"] = text
    return check_appointment(args, DEFAULT_HOURS, WALL_NOW, "America/New_York")


def test_validate_appointment_valid_slot_is_utc():
    r = _check("Tuesday at 2:30pm")
    assert r["is_valid"] is True and r["day_of_week"] == "Tuesday" and r["proposed_date"] == "2026-11-17"
    assert r["confirmed_datetime"] == "2026-11-17T19:30:00.000Z"  # 2:30pm EST


@pytest.mark.parametrize("text, reason", [
    ("Sunday at 9am", "outside_hours"),
    ("today at 9am", "past"),
    ("today at 10:30am", "too_soon"),
    ("I'm not sure yet", "no_time_anchor"),
    ("tuesday next year", "far_future_request"),
    ("Nov 20", "date_only_no_time"),
])
def test_validate_appointment_rejections(text, reason):
    assert _check(text)["reason"] == reason


def test_validate_appointment_availability_and_closed_days():
    r = _check(availability_date="tomorrow")
    assert r["reason"] == "availability" and r["next_available"] == "tomorrow at 9AM"
    assert _check("Sunday at 9am")["next_available"] == "Sunday, Nov 22 at 12PM"


def test_lenient_times():
    assert parse_proposed_time("2-4pm monday", WALL_NOW) == datetime(2026, 11, 23, 14, 0)
    assert parse_proposed_time("friday evening", WALL_NOW) == datetime(2026, 11, 20, 18, 0)
    assert parse_proposed_time("tomorrow at 3", WALL_NOW) == datetime(2026, 11, 17, 15, 0)


# ---- Save_Appointment_Notes -----------------------------------------------------------------------------------


async def test_save_appointment_needs_a_time_validated_this_turn():
    ctx = _ctx("Tuesday 2:30pm works")
    refused = await save_appointment_notes(ctx, {"note": "Appraisal", "appointment_datetime": "2026-11-17T19:30:00.000Z"})
    assert refused["statusCode"] == 422
    ctx.validated_appointments["2026-11-17T19:30:00.000Z"] = {"is_valid": True}
    saved = await save_appointment_notes(ctx, {"note": "Appraisal", "appointment_datetime": "2026-11-17T19:30:00.000Z"})
    assert saved["statusCode"] == 201 and saved["dry_run"] is True and saved["body"][0]["status"] == "confirmed"
    note_only = await save_appointment_notes(ctx, {"note": "Has a loan", "appointment_datetime": None})
    assert note_only["statusCode"] == 200 and "appointment_datetime" not in note_only["body"][0]


# ---- Escalate_To_Human / Suppress_Lead ------------------------------------------------------------------------


def test_named_person_and_turn_gates():
    assert sanitize_named_person("to Mike") == "Mike"
    assert sanitize_named_person("my wife") == ""
    assert sanitize_named_person("the manager") == "manager"
    assert is_deferral_only("I'll have to talk to my wife and get back to you, thanks")
    assert not is_deferral_only("Can someone call me?")
    assert is_closing_filler_only("ok sounds good thanks!")
    assert not is_closing_filler_only("can a manager call me")


async def test_escalation_gates_and_dry_run():
    blocked = await escalate_to_human(_ctx("sounds good!"), {"reason": "ack", "category": "other", "urgency": "normal"})
    assert blocked["ok"] is False and blocked["blocked"] == "low_signal_no_escalation"
    deferral = await escalate_to_human(_ctx("let me talk to my wife"), {"reason": "x", "category": "other", "urgency": "normal"})
    assert deferral["blocked"] == "deferral_no_escalation"
    passive = await escalate_to_human(_ctx("ok thanks!"), {"reason": "x", "category": "other", "urgency": "normal"})
    assert passive["error"] == "passive_callback_continuation_no_new_page"
    decline = await escalate_to_human(_ctx("I already told you I'm not interested"), {"reason": "x", "category": "other", "urgency": "normal"})
    assert decline["error"] == "protected_close_uses_suppression"
    ok = await escalate_to_human(_ctx("Can Mike call me?"), {"reason": "wants a call", "category": "bogus", "named_person": "Mike", "urgency": "high"})
    assert ok["ok"] is True and ok["dry_run"] is True and ok["category"] == "relay_request"
    assert ok["would_insert"]["lane"] == "sales"


async def test_suppression_authorization():
    args = {"reason": "not interested", "suppression_category": "polite_decline", "permanent_dnc": True}
    assert (await suppress_lead(_ctx("no thanks"), args))["error"] == "permanent_dnc_requires_optout_authorization"
    halt = await suppress_lead(_ctx("wrong number"), {"reason": "wrong number", "suppression_category": "wrong_owner", "permanent_dnc": True})
    assert halt["ok"] is True and halt["dnc_downgraded_to_halt"] is True and halt["dnc_applied"] is False
    stop = await suppress_lead(_ctx("STOP"), {"reason": "stop", "suppression_category": "repeat_decline", "permanent_dnc": True})
    assert stop["dnc_applied"] is True
    polite = await suppress_lead(_ctx("not for me"), {"reason": "no", "suppression_category": "polite_decline"})
    assert polite["ok"] is False and polite["error"] == "suppression_noop_nothing_written"
    vehicle = {"id": "v", "year": 2019, "make": "Ford", "model": "Fusion"}
    sold = await suppress_lead(_ctx("I sold the Fusion last month", vehicle=vehicle),
                               {"reason": "sold", "suppression_category": "vehicle_sold", "vehicle_disposed": True})
    assert sold["ok"] is True and sold["vehicle_marked_disposed"] is True
    maybe = await suppress_lead(_ctx("if I sold it what would I get?", vehicle=vehicle),
                                {"reason": "sold", "suppression_category": "vehicle_sold", "vehicle_disposed": True})
    assert maybe["error"] == "vehicle_sold_requires_unambiguous_deterministic_disposal"


# ---- Update_mileage -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, outbound, expected", [
    ("about 45k miles", "", 45000),
    ("It has 62,300 miles on it", "", 62300),
    ("52000", "What's the mileage on it?", 52000),
    ("I bought it with 10k miles and now it has 48k", "", 48000),
    ("it's a 2019", "What's the mileage?", None),
    ("either 40k or 50k miles", "", None),
])
def test_stated_mileage(text, outbound, expected):
    assert stated_mileage(text, outbound)[0] == expected


async def test_update_mileage_only_writes_the_customers_figure():
    vehicle = {"id": "33333333-3333-3333-3333-333333333333", "year": 2019, "make": "Ford", "model": "Fusion"}
    ctx = _ctx("it has 48k miles", vehicle=vehicle)
    assert (await update_mileage(ctx, {"mileage": 50000}))["error"] == "mileage_differs_from_customer_message"
    done = await update_mileage(ctx, {"mileage": 48000})
    assert done == {"ok": True, "dry_run": True, "vehicle_id": vehicle["id"], "vehicle": "2019 Ford Fusion",
                    "mileage": 48000, "mileage_update_date": "2026-11-16"}
    assert (await update_mileage(_ctx("sounds good", vehicle=vehicle), {}))["error"] == "mileage_not_stated_by_customer"


# ---- Search_Inventory URLs ------------------------------------------------------------------------------------


def test_inventory_urls():
    assert slugify("Mustang Mach-E", "_") == "mustang_mach-e"
    assert hyphenated_model("F150") == "F-150" and hyphenated_model("CR-V") is None
    row = {"inventory_url": "https://dealer.com/inventory", "inventory_url_used": "https://dealer.com/inventory/used",
           "inventory_search_url_template_new": "{{inventory_url_new}}new/{{make_slug}}/{{model_slug}}?year={{year}}"}
    f = {"make": "Ford", "model": "Bronco Sport", "year": "2026"}
    assert customer_inventory_url(row, f, "new") == "https://dealer.com/inventory/new/ford/bronco-sport?year=2026"
    assert customer_inventory_url(row, f, "used") == "https://dealer.com/inventory/used/ford/bronco-sport?years=2026"


# ---- a clock time beats a stray availability_date (gateway addition) -------------------------------------------


def test_a_named_clock_time_is_validated_even_with_availability_date():
    # The model sent both: the customer asked for Friday at 3pm, not for Friday's first slot.
    r = _check("Friday at 3pm", availability_date="Friday", availability_request=False)
    assert r["is_valid"] is True and r["confirmed_datetime"].startswith("2026-11-20T20:00")  # 3pm New York
    assert _check("friday at 3", availability_date="Friday")["is_valid"] is True


def test_availability_is_kept_without_an_exact_clock_time():
    assert _check(availability_date="Friday")["reason"] == "availability"  # no proposed_time
    assert _check("Friday", availability_date="Friday")["reason"] == "availability"  # no clock time
    assert _check("Friday after 3pm", availability_date="Friday")["reason"] == "availability"  # a range
    assert _check("Friday at 3pm", availability_date="Friday", availability_request=True)["reason"] == "availability"
    assert _check("What times do you have available Friday?")["reason"] == "availability"  # n8n wording rule


async def test_suppression_dnc_not_recorded_when_switched_off():
    # request.record_opt_out=false: a STOP-authorized DNC is not written; the halt + hold still apply.
    ctx = _ctx("STOP")
    ctx.record_opt_out = False
    stop = await suppress_lead(ctx, {"reason": "stop", "suppression_category": "repeat_decline", "permanent_dnc": True})
    assert stop["ok"] is True and stop["dnc_applied"] is False and stop["dnc_not_recorded"] is True
    assert stop["hold_applied"] is True
    on = await suppress_lead(_ctx("STOP"), {"reason": "stop", "suppression_category": "repeat_decline", "permanent_dnc": True})
    assert on["dnc_applied"] is True and on["dnc_not_recorded"] is False

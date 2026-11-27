"""Reply checks for models that slip out of the tool protocol (app/services/reply_guard.py): a 'none' reasoning effort
raised for tool requests, tool calls written as text run for real, and unbacked hand-off claims asked again, then
removed."""
from types import SimpleNamespace

from app.llm.base import LLMRequest, LLMResult, ToolCall, ToolSpec
from app.llm.model_manager import ModelManager
from app.llm.settings import AgentConfig
from app.services.reply_guard import text_tool_calls, unevidenced_claim, without_claims

from tests.test_tools import ScriptedProvider, _post, client, providers  # noqa: F401 - fixtures

ESCALATE_OK = SimpleNamespace(name="Escalate_To_Human", ok=True, error=None)
ESCALATE_REFUSED = SimpleNamespace(name="Escalate_To_Human", ok=False, error="protected_close_uses_suppression")


# ---- 1. reasoning floor for tool requests ---------------------------------------------------------------------


def _manager(**model) -> tuple[ModelManager, dict]:
    made = {}

    def factory(settings):
        made[settings.id] = ScriptedProvider(settings)
        return made[settings.id]

    cfg = AgentConfig.model_validate({"models": [{
        "id": "oss", "type": "opensource", "provider": "ollama", "protocol": "openai", "model": "gpt-oss:20b",
        "base_url": "http://localhost:11434/v1", **model,
    }]})
    return ModelManager(cfg, factory), made


TOOL = ToolSpec("Validate_Appointment", "check a time", {"type": "object", "properties": {}})


async def _sent_effort(manager, made, **request) -> str:
    await manager.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}], **request), "oss")
    return made["oss"].requests[-1].reasoning_effort


async def test_none_reasoning_is_raised_when_tools_are_offered():
    manager, made = _manager(tool_reasoning_effort="low")
    assert await _sent_effort(manager, made, tools=[TOOL], reasoning_effort="none") == "low"
    assert await _sent_effort(manager, made, tools=[], reasoning_effort="none") == "none"  # no tools: unchanged
    assert await _sent_effort(manager, made, tools=[TOOL], reasoning_effort="high") == "high"  # only 'none' is raised
    assert await _sent_effort(manager, made, tools=[TOOL]) is None  # the model's own default


async def test_none_in_extra_body_is_raised_too_and_unset_floor_changes_nothing():
    manager, made = _manager(tool_reasoning_effort="low", extra_body={"reasoning_effort": "none"})
    assert await _sent_effort(manager, made, tools=[TOOL]) == "low"
    manager, made = _manager()
    assert await _sent_effort(manager, made, tools=[TOOL], reasoning_effort="none") == "none"


# ---- 2. tool calls written as text ----------------------------------------------------------------------------


def test_text_tool_call_shapes():
    offered = ["Escalate_To_Human", "Validate_Appointment"]
    text = '{\n  "name": "Escalate_To_Human",\n  "arguments": {"category": "relay_request"}\n}\nGot it.'
    [call], rest = text_tool_calls(text, offered)
    assert (call.name, call.arguments, rest) == ("Escalate_To_Human", {"category": "relay_request"}, "Got it.")
    [call], rest = text_tool_calls('```json\n{"Escalate_To_Human": {"category": "other"}}\n```', offered)
    assert call.arguments == {"category": "other"} and rest == ""
    [call], _ = text_tool_calls('{"name": "functions.validate_appointment", "arguments": "{\\"proposed_time\\": \\"Tue 2pm\\"}"}', offered)
    assert (call.name, call.arguments) == ("Validate_Appointment", {"proposed_time": "Tue 2pm"})
    [call], _ = text_tool_calls('{"type": "function", "function": {"name": "Escalate_To_Human", "parameters": {}}}', offered)
    assert call.name == "Escalate_To_Human"


def test_json_that_is_not_an_offered_tool_is_left_alone():
    for text in ['{"inventory": {"make": "Toyota"}}', 'Price is {"usd": 5}', "Plain reply.", '{"name": "Search_Inventory", "arguments": {}}']:
        assert text_tool_calls(text, ["Escalate_To_Human"]) == ([], text)


def test_a_text_tool_call_is_run_and_kept_out_of_the_reply(client, providers):  # noqa: F811
    providers["claude"].script = [
        LLMResult('{"name": "Validate_Appointment", "arguments": {"proposed_time": "Tuesday at 2pm"}}\nTuesday works!', "stop", 10, 5),
        LLMResult("Tuesday at 2PM works, see you then!", "stop", 10, 5),
    ]
    data = _post(client, agent="scheduling").json()
    assert data["choices"][0]["message"]["content"] == "Tuesday at 2PM works, see you then!"
    [call] = data["tool_calls"]
    assert call["name"] == "Validate_Appointment" and call["from_text"] is True and call["ok"] is True
    second = providers["claude"].requests[1].messages
    assert second[-2]["role"] == "assistant" and second[-2]["content"] == "" and second[-1]["role"] == "tool"


def test_a_text_tool_call_after_the_last_round_is_only_removed(client, providers):  # noqa: F811
    call = LLMResult("", "tool_calls", 1, 1, [ToolCall("x", "Validate_Appointment", {"proposed_time": "monday 3pm"})])
    providers["claude"].script = [call] * 6 + [
        LLMResult('{"Validate_Appointment": {"proposed_time": "monday 4pm"}}\nMonday works.', "stop", 1, 1)]
    data = _post(client, agent="scheduling").json()
    assert data["choices"][0]["message"]["content"] == "Monday works."
    assert len(data["tool_calls"]) == 6 and not any(c["from_text"] for c in data["tool_calls"])


def test_real_tool_calls_are_reported_as_not_from_text(client, providers):  # noqa: F811
    providers["claude"].script = [
        LLMResult("", "tool_calls", 1, 1, [ToolCall("t1", "Validate_Appointment", {"proposed_time": "Tuesday at 2pm"})]),
        LLMResult("See you Tuesday!", "stop", 1, 1),
    ]
    assert _post(client, agent="scheduling").json()["tool_calls"][0]["from_text"] is False


# ---- 3. unbacked hand-off claims ------------------------------------------------------------------------------


def test_claims_and_non_claims():
    claims = ["Got it, I've flagged it for our team to handle directly.", "Mike will call you shortly.",
              "Someone will reach out shortly.", "I'll have the manager call you.", "I'm flagging it now.",
              "Our sales manager will be in touch today.", "I've let the manager know.", "I've passed this along."]
    fine = ["Our team at (916) 880-6167 can check for you.", "Want me to have someone call you?",
            "Would you like the team to call you?", "Tuesday at 2PM works, see you then!", "Monday will work great.",
            "I'll make sure we stop texting this number.", "Thanks for letting me know.", "We have 2 used RAV4s."]
    assert all(unevidenced_claim(c, []) for c in claims)
    assert not any(unevidenced_claim(f, []) for f in fine)
    assert unevidenced_claim(claims[0], [ESCALATE_OK]) is None  # backed by a successful escalation
    assert unevidenced_claim(claims[0], [ESCALATE_REFUSED])  # a refused escalation backs nothing
    assert without_claims("I've flagged it for the team. You can also call (916) 880-6167.") == "You can also call (916) 880-6167."


def test_an_unbacked_claim_is_asked_again_once(client, providers):  # noqa: F811
    providers["claude"].script = [
        LLMResult("Got it, I've flagged it for our team to handle directly.", "stop", 10, 5),
        LLMResult("Sorry about that. You can reach the dealership directly anytime.", "stop", 10, 5),
    ]
    data = _post(client, agent="scheduling").json()
    assert data["choices"][0]["message"]["content"] == "Sorry about that. You can reach the dealership directly anytime."
    first, second = providers["claude"].requests
    assert "REPLY CHECK" not in first.messages[0]["content"] and "REPLY CHECK" in second.messages[0]["content"]
    assert second.messages[0]["content"].startswith(first.messages[0]["content"])  # cached prefix kept
    assert data["usage"]["total_tokens"] == 30


def test_a_claim_made_again_is_removed(client, providers):  # noqa: F811
    providers["claude"].script = [
        LLMResult("I've flagged it for the team.", "stop", 1, 1),
        LLMResult("Mike will call you shortly. Anything else I can help with?", "stop", 1, 1),
    ]
    data = _post(client, agent="scheduling").json()
    assert data["choices"][0]["message"]["content"] == "Anything else I can help with?"
    assert len(providers["claude"].requests) == 2


def test_a_reply_that_was_only_a_claim_becomes_a_neutral_line(client, providers):  # noqa: F811
    providers["claude"].script = [LLMResult("I've flagged it for the team.", "stop", 1, 1)] * 2
    data = _post(client, agent="scheduling").json()
    assert data["choices"][0]["message"]["content"] == "Thanks for letting me know."


def test_no_check_without_tools(client, providers):  # noqa: F811
    providers["claude"].script = [LLMResult("I've flagged it for the team.", "stop", 1, 1)]
    data = _post(client, agent="scheduling", use_tools=False).json()
    assert data["choices"][0]["message"]["content"] == "I've flagged it for the team."
    assert len(providers["claude"].requests) == 1


# ---- tool names written loosely -------------------------------------------------------------------------------


def test_a_tool_name_in_another_case_runs_the_tool(client, providers):  # noqa: F811
    # The value prompt writes "update_mileage" for Update_mileage; any such spelling runs the real tool.
    providers["claude"].script = [
        LLMResult("", "tool_calls", 1, 1, [ToolCall("t1", "validate_appointment", {"proposed_time": "Tuesday at 2pm"}),
                                           ToolCall("t2", "functions.Validate-Appointment", {"proposed_time": "Tuesday at 3pm"})]),
        LLMResult("See you Tuesday!", "stop", 1, 1),
    ]
    data = _post(client, agent="scheduling").json()
    assert data["tools_used"] == ["Validate_Appointment"]
    assert [c["ok"] for c in data["tool_calls"]] == [True, True]


def test_a_name_that_is_no_offered_tool_is_still_unknown(client, providers):  # noqa: F811
    providers["claude"].script = [
        LLMResult("", "tool_calls", 1, 1, [ToolCall("t1", "validate", {})]),
        LLMResult("Ok.", "stop", 1, 1),
    ]
    [call] = _post(client, agent="scheduling").json()["tool_calls"]
    assert call["ok"] is False and "unknown tool" in call["error"]


# ---- agent addenda --------------------------------------------------------------------------------------------


def test_the_value_agent_gets_its_gateway_addendum(client, providers):  # noqa: F811
    from app.services.agent_prompts import get_agent
    _post(client, agent="value_data_collection", use_tools=False)
    _post(client, agent="scheduling", use_tools=False)
    value, scheduling = (r.messages[0]["content"] for r in providers["claude"].requests)
    agent_text = get_agent("value_data_collection").text
    assert value.startswith(agent_text) and "value-data-collection-notes-v1" in value
    assert value.index("value-data-collection-notes-v1") < value.index("=== RUNTIME CONTEXT ===")
    assert "value-data-collection-notes-v1" not in scheduling
    assert "escalation-tone-v1" in value and "escalation-tone-v1" in scheduling
    assert "closing-wording-v1" in value and "closing-wording-v1" in scheduling


def test_lane_agents_get_their_boundary_addenda(client, providers):  # noqa: F811
    for agent in ("inventory", "scheduling", "value_data_collection", "general"):
        _post(client, agent=agent, use_tools=False)
    inventory, scheduling, value, general = (r.messages[0]["content"] for r in providers["claude"].requests)
    assert "inventory-lane-boundary-v1" in inventory and "scheduling-lane-boundary-v1" in scheduling
    assert "LANE BOUNDARY" in value
    assert not any(m in general for m in ("lane-boundary-v1", "LANE BOUNDARY"))


# ---- A. unbacked booking claims --------------------------------------------------------------------------------

from app.services.reply_guard import unbacked_booking, without_booking_claims  # noqa: E402


def test_booking_claims_and_non_claims():
    claims = ["Your appraisal is scheduled for Friday at 4 pm.", "You're all set for Friday at 3PM!",
              "You're booked for tomorrow at 2.", "I've booked you in for Tuesday at 10.", "I've got you down for Friday at 4.",
              "Your visit is confirmed for Monday.", "Locked in for 4pm Friday!"]
    fine = ["See you Friday at 3!", "Sounds good, see you then!", "Would Friday at 4 work?", "Want me to set that up?",
            "Thanks, have a great day!", "I can get that set up for you - what time works?", "We have 2 RAV4s in stock."]
    assert all(unbacked_booking(c, []) for c in claims)
    assert not any(unbacked_booking(f, []) for f in fine)
    saved = SimpleNamespace(name="Save_Appointment_Notes", ok=True, arguments={"appointment_datetime": "2026-11-27T23:00:00.000Z"})
    note_only = SimpleNamespace(name="Save_Appointment_Notes", ok=True, arguments={"appointment_datetime": None})
    failed = SimpleNamespace(name="Save_Appointment_Notes", ok=False, arguments={"appointment_datetime": "2026-11-27T23:00:00.000Z"})
    assert unbacked_booking(claims[1], [saved]) is None  # backed by a saved appointment
    assert unbacked_booking(claims[1], [note_only]) and unbacked_booking(claims[1], [failed])
    assert without_booking_claims("Got it. Your appraisal is scheduled for Friday at 4 pm. See you then!") == "Got it. See you then!"


def test_an_unbacked_booking_is_asked_again_then_removed(client, providers):  # noqa: F811
    claim = LLMResult("Got it, Artrice. Your appraisal is scheduled for Friday at 4 pm. See you then!", "stop", 1, 1)
    providers["claude"].script = [claim, claim]
    data = _post(client, agent="scheduling", messages=[{"role": "user", "content": "can I come see it tomorrow at 4pm"}]).json()
    assert data["choices"][0]["message"]["content"] == "Got it, Artrice. See you then!"
    first, second = providers["claude"].requests
    assert "no appointment was saved" not in first.messages[0]["content"]
    assert "no appointment was saved" in second.messages[0]["content"]


def test_a_claim_only_reply_becomes_an_honest_question(client, providers):  # noqa: F811
    providers["claude"].script = [LLMResult("You're all set for Friday at 4!", "stop", 1, 1)] * 2
    data = _post(client, agent="scheduling", messages=[{"role": "user", "content": "Friday at 4 works"}]).json()
    assert data["choices"][0]["message"]["content"].startswith("Thanks! I want to make sure that time is open")


def test_a_fixed_reply_after_the_check_is_kept(client, providers):  # noqa: F811
    providers["claude"].script = [LLMResult("You're all set for Friday at 4!", "stop", 1, 1),
                                  LLMResult("Friday at 4 - does 4 PM work, or is earlier better?", "stop", 1, 1)]
    data = _post(client, agent="scheduling", messages=[{"role": "user", "content": "Friday at 4 works"}]).json()
    assert data["choices"][0]["message"]["content"] == "Friday at 4 - does 4 PM work, or is earlier better?"

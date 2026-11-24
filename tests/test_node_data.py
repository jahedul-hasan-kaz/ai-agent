"""Workflow node values: the database-backed nodes (tenant, lead, vehicle, SMS history, ...) are read by the gateway
itself; the computed ones (Intent Classifier, clock, combined message, ...) can be supplied as `node_data`. Both
reach the agent's prompt and tools the way the n8n workflow hands them over."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.llm.settings import ToolsConfig
from app.models.chat import ChatCompletionRequest
from app.services.agent_prompts import get_agent, runtime_context
from app.services.chat_service import ChatService
from app.services.node_data import INTERNAL_NODES
from app.tools.base import ToolContext
from app.tools.escalation import escalate_to_human, protected_close_category
from app.tools.registry import agent_inputs

from tests.test_tools import client, providers  # noqa: F401 - fixtures

TENANT = "22222222-2222-2222-2222-222222222222"
LEAD = {"id": "11111111-1111-1111-1111-111111111111", "tenant_id": TENANT, "first_name": "Sam", "current_cadence_id": None}
ARGS = {"reason": "wants a person", "category": "other", "urgency": "normal"}


def _ctx(node_data: dict, text: str = "Can someone call me about the RAV4?", **cache) -> ToolContext:
    """A context whose database lookups are already answered (as if read from the database)."""
    ctx = ToolContext(settings=ToolsConfig(), tenant_id=TENANT, sms_thread_key="+15551234567", email_thread_key=None,
                      turns=[{"role": "user", "content": text}], node_data=node_data)
    ctx._cache.update({"lead": LEAD, "cadence": {}, "saved_inbound": None, **cache})
    return ctx


# ---- which nodes are inputs -----------------------------------------------------------------------------------


def test_only_computed_nodes_are_inputs():
    inputs = {i["node"]: i for i in agent_inputs("inventory", get_agent("inventory").inputs())}
    assert set(inputs) == {"Intent Classifier", "Normalize Phone", "Combine messages"}
    assert inputs["Intent Classifier"]["used_by"] == ["Escalate_To_Human"]
    for agent in ("general", "scheduling", "value_data_collection", "service_trade_reply"):
        assert not {i["node"] for i in agent_inputs(agent, get_agent(agent).inputs())} & INTERNAL_NODES


def test_agents_endpoint_lists_inputs(client):  # noqa: F811
    data = {a["id"]: a for a in client.get("/v1/agents").json()["data"]}
    nodes = {i["node"] for i in data["inventory"]["inputs"]}
    assert "Intent Classifier" in nodes and "Lookup Lead" not in nodes


def test_request_rejects_database_nodes_and_the_clock():
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tenant_id": TENANT}
    assert ChatCompletionRequest(**base, node_data={}).node_data is None
    assert ChatCompletionRequest(**base, node_data={"Intent Classifier": {"intent": "inventory"}}).node_data
    with pytest.raises(ValidationError, match="reads or computes it itself"):
        ChatCompletionRequest(**base, node_data={"Lookup Lead": {"id": LEAD["id"]}})
    with pytest.raises(ValidationError, match="Get Current Date/Time"):
        ChatCompletionRequest(**base, node_data={"Get Current Date/Time": {"date": "2026-01-02"}})
    with pytest.raises(ValidationError, match="JSON object"):
        ChatCompletionRequest(**base, node_data={"Intent Classifier": "inventory"})


# ---- prompt ---------------------------------------------------------------------------------------------------


async def test_database_nodes_fill_the_prompt_from_the_database():
    tenant = {"slug": "koons", "main_phone": "+15550001111", "hours_of_operation": {"timezone": "America/Chicago"}}
    vehicle = {"model": "Highlander"}
    ctx = _ctx({}, tenant=tenant, vehicle=vehicle)
    nodes = await ChatService(None)._database_nodes(get_agent("inventory"), ctx)
    assert nodes["Get tenant"]["slug"] == "koons" and nodes["Lookup Lead"]["first_name"] == "Sam"
    text = get_agent("inventory").render(nodes)
    assert "+15550001111" in text and "our main number" not in text and "koons" in text and "Highlander" in text


async def test_no_lead_or_database_keeps_the_fallbacks():
    agent = get_agent("inventory")
    no_thread = ToolContext(settings=ToolsConfig(), tenant_id=TENANT, sms_thread_key=None, email_thread_key=None, turns=[])
    assert await ChatService(None)._database_nodes(agent, no_thread) == {}  # no database, no thread key
    assert agent.render({}) == agent.text and "our main number" in agent.text


def test_the_clock_is_the_gateways_own_in_new_york():
    # n8n "Get Current Date/Time": the server's clock in America/New_York; the prompt's date/time expressions point here.
    clock = runtime_context(datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc))
    assert "Current date: 2026-11-19" in clock and "10:00 AM (America/New_York)" in clock and "Thursday" in clock
    assert "RUNTIME CONTEXT" in get_agent("inventory").render({})


def test_node_data_reaches_the_models_prompt(client, providers):  # noqa: F811
    # Select Market Value is a computed node the value agent's prompt reads.
    body = {"model": "claude", "agent": "value_data_collection", "use_tools": False,
            "messages": [{"role": "user", "content": "What's my car worth?"}],
            "node_data": {"Select Market Value": {"market_value": 18450, "source": "acv"}}}
    assert client.post("/v1/chat/completions", json=body).status_code == 200
    system = providers["claude"].requests[-1].messages[0]["content"]
    assert "18450" in system


# ---- tool context ---------------------------------------------------------------------------------------------


def test_tool_context_reads_supplied_computed_nodes():
    ctx = _ctx({"Normalize Phone": {"text": "from n8n"}, "Combine messages": {"combinedMessage": "a\nb"},
                "Intent Classifier": {"intent": "inventory"}})
    assert ctx.inbound_text == "from n8n" and ctx.combined_message == "a\nb"
    assert ctx.classifier() == {"intent": "inventory"}
    assert _ctx({}).classifier() is None and _ctx({}).inbound_text.startswith("Can someone")


# ---- Escalate_To_Human with the Intent Classifier -------------------------------------------------------------


def test_protected_close_category_follows_n8n():
    decline = "I already told you I'm not interested"
    assert protected_close_category({}, decline) == "repeat_decline"
    assert protected_close_category({"complaint_signals": True}, decline) is None
    assert protected_close_category({"intent": "wrong_owner"}, "x") == "wrong_owner"
    assert protected_close_category({"internal_employee": True}, "x") == "internal_employee"
    assert protected_close_category({"explicit_decline_or_deferral": True}, "x") == "polite_decline"
    assert protected_close_category({"intent": "inventory", "explicit_decline_or_deferral": False}, decline) is None
    assert protected_close_category({"intent": "relay_request"}, decline) is None


async def test_classifier_closes_and_complaint_override():
    wrong_owner = await escalate_to_human(_ctx({"Intent Classifier": {"wrong_owner": True}}), ARGS)
    assert wrong_owner["error"] == "protected_close_uses_suppression"
    decline = "I already told you I'm not interested, get me a manager"
    complaint = await escalate_to_human(_ctx({"Intent Classifier": {"complaint_signals": True}}, decline),
                                        {**ARGS, "category": "complaint"})
    assert complaint["ok"] is True and complaint["dry_run"] is True


async def test_deceased_classifier_halts_and_holds_in_dry_run():
    result = await escalate_to_human(
        _ctx({"Intent Classifier": {"intent": "deceased", "deceased": True}}, "My father passed away"),
        {**ARGS, "urgency": "urgent"},
    )
    s = result["suppression"]
    assert s["attempted"] and s["suppression_effective"] and s["category"] == "deceased" and not s["dnc_applied"]
    assert "send hold is in place" in result["message"]


async def test_inbound_receipt_from_the_database_must_bind_to_the_lead():
    row = {"id": 255, "tenant_id": TENANT, "lead_id": LEAD["id"], "direction": "inbound"}
    ok = await escalate_to_human(_ctx({}, saved_inbound=row), ARGS)
    assert ok["action"] == "sales_handoff" and ok["would_insert"]["id"].startswith("a1080908-")
    assert ok["lead_id"] == LEAD["id"] and ok["inbound_log_id"] == "255"
    wrong = await escalate_to_human(_ctx({}, saved_inbound={**row, "lead_id": "someone-else"}), ARGS)
    assert wrong["error"] == "missing_current_saved_inbound"


async def test_legacy_permanent_dnc_is_reported_as_ignored():
    result = await escalate_to_human(_ctx({}), {**ARGS, "permanent_dnc": True})
    assert result["suppression"]["legacy_permanent_dnc_requested"] is True and "IGNORED" in result["message"]


async def test_classifier_complaint_sets_category_and_urgency():
    # "Your offer is insulting, get me a manager": the model sent relay_request/normal; the classifier says complaint.
    result = await escalate_to_human(
        _ctx({"Intent Classifier": {"intent": "complaint", "complaint_signals": True}}, "Your offer is insulting, get me a manager"),
        {"reason": "wants a manager", "category": "relay_request", "named_person": "manager", "urgency": "normal"},
    )
    assert result["ok"] and (result["category"], result["urgency"]) == ("complaint", "high")
    assert result["classifier_adjusted"] == {"category": ["relay_request", "complaint"], "urgency": ["normal", "high"]}
    assert result["would_insert"]["category"] == "complaint"


async def test_classifier_complaint_keeps_urgent_and_service_complaint():
    ctx = _ctx({"Intent Classifier": {"complaint_signals": True}}, "The repair was botched, I want a manager")
    result = await escalate_to_human(ctx, {"reason": "x", "category": "service_complaint", "urgency": "urgent"})
    assert (result["category"], result["urgency"]) == ("service_complaint", "urgent") and "classifier_adjusted" not in result


async def test_no_classifier_keeps_the_models_choice():
    result = await escalate_to_human(_ctx({}, "Can someone call me?"), {"reason": "x", "category": "relay_request", "urgency": "normal"})
    assert (result["category"], result["urgency"]) == ("relay_request", "normal") and "classifier_adjusted" not in result

"""node_data: outputs of the n8n workflow's upstream nodes supplied on a request, read by the agent's prompt and its
tools the way the workflow reads those nodes; anything not supplied keeps the gateway's own behaviour."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.llm.settings import ToolsConfig
from app.models.chat import ChatCompletionRequest
from app.services.agent_prompts import get_agent, runtime_context
from app.tools.base import ToolContext
from app.tools.escalation import escalate_to_human, protected_close_category
from app.tools.registry import ToolRunner, agent_inputs

from tests.test_tools import client, providers  # noqa: F401 - fixtures

TENANT = "22222222-2222-2222-2222-222222222222"
LEAD_ID = "11111111-1111-1111-1111-111111111111"
ARGS = {"reason": "wants a person", "category": "other", "urgency": "normal"}


def _ctx(node_data: dict, text: str = "Can someone call me about the RAV4?", **kw) -> ToolContext:
    return ToolContext(settings=ToolsConfig(), tenant_id=TENANT, sms_thread_key=None, email_thread_key=None,
                       turns=[{"role": "user", "content": text}], node_data=node_data, **kw)


LEAD = {"Lookup Lead": {"id": LEAD_ID, "tenant_id": TENANT, "first_name": "Sam"}}


# ---- prompt ---------------------------------------------------------------------------------------------------


def test_supplied_nodes_fill_the_prompt_and_the_rest_keep_their_fallback():
    agent = get_agent("inventory")
    assert agent.render(None) == agent.text and agent.render({"Unrelated": {}}) == agent.text
    assert "our main number" in agent.text
    text = agent.render({"Get tenant emails & phone": {"main_phone": "+15550001111"}, "Get tenant": {"slug": "koons"}})
    assert "+15550001111" in text and "our main number" not in text and "koons" in text
    assert "there" in text  # Lookup Lead.first_name not supplied: its fallback stays
    assert "Sam" in agent.render(LEAD)


def test_runtime_context_takes_the_supplied_clock_and_timezone():
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    ctx = runtime_context(now, node_data={"Get tenant emails & phone": {"hours_of_operation": {"timezone": "America/Chicago"}}})
    assert "9:00 AM (America/Chicago)" in ctx
    given = runtime_context(now, node_data={"Get Current Date/Time": {"date": "2026-01-02", "time_12h": "8:15 AM", "day_of_week": "Friday"}})
    assert "Current date: 2026-01-02" in given and "8:15 AM" in given and "Day of week: Friday" in given


def test_inventory_agent_lists_the_nodes_it_reads():
    inputs = {i["node"]: i for i in agent_inputs("inventory", get_agent("inventory").inputs())}
    assert inputs["Get tenant emails & phone"]["used_by"] == ["prompt", "Escalate_To_Human"]
    assert "main_phone" in inputs["Get tenant emails & phone"]["fields"]
    assert inputs["Intent Classifier"]["used_by"] == ["Escalate_To_Human"]
    assert inputs["Get Recent SMS History"]["list_of_rows"] is True
    assert {"Insert Log", "Lookup Lead", "Normalize Phone", "Combine messages", "Get Current Date/Time"} <= set(inputs)


def test_agents_endpoint_lists_inputs(client):  # noqa: F811
    data = {a["id"]: a for a in client.get("/v1/agents").json()["data"]}
    assert any(i["node"] == "Intent Classifier" for i in data["inventory"]["inputs"])


def test_node_data_reaches_the_models_prompt(client, providers):  # noqa: F811
    body = {"model": "claude", "agent": "inventory", "tenant_id": TENANT, "use_tools": False,
            "messages": [{"role": "user", "content": "Are you a bot?"}],
            "node_data": {"Get tenant emails & phone": {"main_phone": "+15550001111"}}}
    assert client.post("/v1/chat/completions", json=body).status_code == 200
    system = providers["claude"].requests[-1].messages[0]["content"]
    assert "+15550001111" in system and "our main number" not in system


# ---- request --------------------------------------------------------------------------------------------------


def test_request_validates_node_data():
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tenant_id": TENANT}
    assert ChatCompletionRequest(**base, node_data={}).node_data is None
    with pytest.raises(ValidationError, match="JSON object"):
        ChatCompletionRequest(**base, node_data={"Get tenant": "koons"})
    with pytest.raises(ValidationError, match="does not match tenant_id"):
        ChatCompletionRequest(**base, node_data={"Lookup Lead": {"id": LEAD_ID, "tenant_id": "33333333-3333-3333-3333-333333333333"}})
    with pytest.raises(ValidationError, match="tenant_id is required"):
        ChatCompletionRequest(model="m", messages=base["messages"], node_data=LEAD)


# ---- tool context ---------------------------------------------------------------------------------------------


async def test_tool_context_reads_supplied_nodes_first():
    history = [{"direction": "inbound", "message": "second", "created_at": "2026-11-19T10:05:00Z"},
               {"direction": "outbound", "message": "first", "created_at": "2026-11-19T10:00:00Z"},
               {"direction": None, "message": "dropped"}]
    ctx = _ctx({**LEAD, "Normalize Phone": {"text": "from n8n"}, "Combine messages": {"combinedMessage": "a\nb"},
                "Get Recent SMS History": history, "Insert Log": {"id": 42}, "Intent Classifier": {"intent": "inventory"},
                "Get tenant emails & phone": {"timezone": "America/Denver"}})
    assert ctx.inbound_text == "from n8n" and ctx.combined_message == "a\nb"
    assert (await ctx.lead())["id"] == LEAD_ID and (await ctx.lead())["tenant_id"] == TENANT  # no database needed
    assert [r["message"] for r in await ctx.recent_sms(5)] == ["first", "second"]
    assert (await ctx.saved_inbound())["id"] == 42 and ctx.classifier() == {"intent": "inventory"}
    assert await ctx.timezone_name() == "America/Denver"


def test_a_supplied_lead_lets_escalation_be_offered_without_a_thread_key():
    runner = ToolRunner(ToolsConfig())
    _, skipped = runner.offered("inventory", _ctx({}))
    assert "lead" in skipped["Escalate_To_Human"]
    _, skipped = runner.offered("inventory", _ctx(LEAD))
    assert "database" in skipped["Escalate_To_Human"]  # past the lead check; only the database is missing here


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
    wrong_owner = await escalate_to_human(_ctx({**LEAD, "Intent Classifier": {"wrong_owner": True}}), ARGS)
    assert wrong_owner["error"] == "protected_close_uses_suppression"
    decline = "I already told you I'm not interested, get me a manager"
    complaint = await escalate_to_human(_ctx({**LEAD, "Intent Classifier": {"complaint_signals": True}}, decline),
                                        {**ARGS, "category": "complaint"})
    assert complaint["ok"] is True and complaint["dry_run"] is True


async def test_deceased_classifier_halts_and_holds_in_dry_run():
    result = await escalate_to_human(
        _ctx({**LEAD, "Intent Classifier": {"intent": "deceased", "deceased": True}}, "My father passed away"),
        {**ARGS, "category": "other", "urgency": "urgent"},
    )
    s = result["suppression"]
    assert s["attempted"] and s["suppression_effective"] and s["category"] == "deceased" and not s["dnc_applied"]
    assert "send hold is in place" in result["message"]


async def test_supplied_inbound_receipt_must_bind_to_the_lead():
    good = {"id": 255, "tenant_id": TENANT, "lead_id": LEAD_ID, "direction": "inbound"}
    ok = await escalate_to_human(_ctx({**LEAD, "Insert Log": good}), ARGS)
    assert ok["action"] == "sales_handoff" and ok["would_insert"]["id"].startswith("a1080908-")
    assert ok["lead_id"] == LEAD_ID and ok["inbound_log_id"] == "255"
    wrong = await escalate_to_human(_ctx({**LEAD, "Insert Log": {**good, "lead_id": "someone-else"}}), ARGS)
    assert wrong["error"] == "missing_current_saved_inbound"


async def test_legacy_permanent_dnc_is_reported_as_ignored():
    result = await escalate_to_human(_ctx(LEAD), {**ARGS, "permanent_dnc": True})
    assert result["suppression"]["legacy_permanent_dnc_requested"] is True and "IGNORED" in result["message"]

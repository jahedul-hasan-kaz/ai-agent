import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import config
from app.customer.context import build_customer_context
from app.customer.store import (
    Campaign,
    CustomerRecords,
    CustomerStore,
    Dealership,
    Lead,
    Message,
    Thread,
    Vehicle,
    email_variants,
    phone_variants,
)
from app.customer.writer import Stored
from app.llm.model_manager import ModelManager
from app.llm.settings import AgentConfig, CustomerConfig
from tests.test_chat_completions import CONFIG, TENANT, FakeProvider

START = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
LEAD = uuid.uuid4()


def _msg(i, direction, body, cadence=False, sent_by=None, subject=None):
    return Message(str(i), START + timedelta(minutes=i), direction, body, subject,
                   uuid.uuid4() if cadence else None, sent_by, "Trade-in 8 touch" if cadence else None)


def _vehicle(**kw):
    base = dict(lead_id=LEAD, year=2022, make="Kia", model="Telluride", trim="EX", mileage=37000,
                mileage_date=date(2026, 1, 2), vin="5XYP34HC0NG123456", market_value=Decimal("38050"),
                market_value_date=date(2026, 1, 3), is_primary=True, ownership_status="probable",
                lease_loan_cash=None, purchase_date=None, is_sold=False, date_sold=None)
    base.update(kw)
    return Vehicle(**base)


def _records(thread=None, **kw):
    lead = Lead(LEAD, "Mychal", "Reed", "m@example.com", "+15551234567", "active", False, None, False, "Blake", None)
    hours = {"monday": {"open": "09:00", "close": "20:00"}, "sunday": None, "timezone": "America/New_York"}
    return CustomerRecords(
        "ok",
        dealership=Dealership("Jeff Wyler Fairfield", "1 Main St, Fairfield, OH", "+15130000000", hours),
        leads=kw.get("leads", [lead]),
        vehicles=kw.get("vehicles", [_vehicle(), _vehicle(year=2019, model="Soul", trim=None, vin=None, market_value=None,
                                                is_primary=False, mileage=None)]),
        threads=[thread] if thread else [],
    )


def test_phone_and_email_variants():
    assert phone_variants("(555) 123-4567") == ["(555) 123-4567", "+15551234567", "15551234567", "5551234567"]
    assert phone_variants("+15551234567") == ["+15551234567", "15551234567", "5551234567"]
    assert email_variants("Mychal Reed <Mychal@Example.com>") == ["mychal@example.com"]


def test_profile_lists_every_vehicle_with_estimates():
    context = build_customer_context(_records(), 4000, 1500, [])
    assert "Customer name on file: Mychal Reed" in context
    assert "2022 Kia Telluride EX: 37,000 mi (as of 2026-01-02), estimated market value $38,050 (as of 2026-01-03)" in context
    assert "VIN 5XYP34HC0NG123456 [primary]\n- 2019 Kia Soul: mileage unknown, no value on file\n" in context
    assert "Mon 09:00-20:00" in context and "Sun closed" in context
    assert "no earlier messages" in context


def test_no_leads_and_unavailable_tell_the_model_not_to_guess():
    assert "none were found" in build_customer_context(_records(leads=[], vehicles=[]), 4000, 1500, [])
    assert "could not be loaded" in build_customer_context(CustomerRecords("error", "timeout"), 4000, 1500, [])
    assert build_customer_context(CustomerRecords("skipped"), 4000, 1500, []) == ""


def test_huge_thread_fits_the_budget_and_keeps_what_matters():
    # 10K messages: the store hands over aggregates, the recent window, matches for the question and older replies.
    recent = [_msg(10_000 + i, "inbound" if i % 2 else "outbound", f"recent message {i}") for i in range(40)]
    older = [_msg(9_000 - i, "inbound", f"older customer reply {i} " + "x" * 200) for i in range(300)]
    relevant = [_msg(5, "outbound", "Your Telluride is looking at roughly $38,050.")]
    thread = Thread("sms", total=10_000, inbound=1_500, first_at=START, last_at=START + timedelta(days=300),
                    stopped_at=START, restarted_at=START + timedelta(days=1),
                    campaigns=[Campaign("Trade-in 8 touch", 8_000, START, START + timedelta(days=200))],
                    recent=recent, relevant=relevant, older=older)
    turns = [{"role": "user", "content": "Recent message 39"}]
    context = build_customer_context(_records(thread), history_tokens=2000, max_message_chars=1500, turns=turns)

    history = context.split("<history>\n", 1)[1]
    assert len(history) <= 2000 * 4 + 200
    assert "SMS: 10,000 messages (1,500 from the customer)" in history
    assert "Trade-in 8 touch x8000" in history and "opted out" not in history  # opted back in
    assert "roughly $38,050" in history  # old but relevant to the question survives the budget
    assert "recent message 38" in history and "recent message 39" not in history  # the live turn isn't repeated
    assert "other earlier messages not shown" in history
    lines = [line for line in history.splitlines() if line.startswith("[")]
    assert lines.index(next(l for l in lines if "$38,050" in l)) < lines.index(next(l for l in lines if "recent message 38" in l))


def test_opt_out_and_speakers():
    msgs = [_msg(1, "outbound", "Hi, want a value?", cadence=True), _msg(2, "inbound", "STOP"),
            _msg(3, "outbound", "Unsubscribed.", sent_by=str(uuid.uuid4()))]
    thread = Thread("sms", total=3, inbound=1, first_at=START, last_at=START, stopped_at=START, recent=msgs)
    history = build_customer_context(_records(thread), 4000, 1500, [])
    assert "opted out of SMS on 2026-01-05" in history
    assert "Dealer (campaign: Trade-in 8 touch): Hi, want a value?" in history
    assert "Customer: STOP" in history and "Dealer staff: Unsubscribed." in history


def test_email_bodies_lose_html_and_quoted_replies():
    body = "<p>Sounds good, Monday works.</p><br>On Mon, Jun 1, 2026 Blake wrote:\n> Would Monday work?"
    thread = Thread("email", total=1, inbound=1, first_at=START, last_at=START,
                    recent=[_msg(1, "inbound", body, subject="Re: Your Telluride")])
    history = build_customer_context(_records(thread), 4000, 1500, [])
    assert "Customer: (subject: Re: Your Telluride) Sounds good, Monday works." in history
    assert "Would Monday work" not in history


class FakeStore:
    def __init__(self, records):
        self.settings = CustomerConfig()
        self.records = records
        self.calls = []
        self.ready = True

        self.stored = []
        self.store_error = None

    async def load(self, tenant_id, sms_key, email_key, query):
        self.calls.append((tenant_id, sms_key, email_key, query))
        return self.records

    async def store_exchange(self, *args):
        if self.store_error:
            raise self.store_error
        self.stored.append(args)
        return Stored("sms" if args[1] else "email", "101", "102" if args[4] else None)


@pytest.fixture
def customer_client(monkeypatch):
    providers = {}

    def factory(settings):
        providers[settings.id] = FakeProvider(settings)
        return providers[settings.id]

    async def none(_):
        return None

    store = FakeStore(_records(Thread("sms", total=1, inbound=1, first_at=START, last_at=START,
                                      recent=[_msg(1, "outbound", "Want a quick trade-in value?")])))
    monkeypatch.setattr(config, "load_agent_config", lambda: AgentConfig.model_validate(CONFIG))
    monkeypatch.setattr(main, "ModelManager", lambda cfg: ModelManager(cfg, factory))
    monkeypatch.setattr(main, "_start_customers", none)
    with TestClient(main.app) as c:
        main.app.state.chat_service.customers = store
        yield c, store, providers


def test_thread_keys_load_the_customer_into_the_prompt(customer_client):
    client, store, providers = customer_client
    res = client.post("/v1/chat/completions", json={
        "model": "claude", "tenant_id": TENANT, "sms_thread_key": " +15551234567 ",
        "messages": [{"role": "user", "content": "What's my Telluride worth?"}],
    })
    assert res.status_code == 200, res.text
    assert store.calls == [(uuid.UUID(TENANT), "+15551234567", None, "What's my Telluride worth?")]
    system = providers["claude"].requests[0].messages[0]["content"]
    runtime = system.index("RUNTIME CONTEXT")
    assert runtime < system.index("<customer>", runtime) < system.index("<history>", runtime)
    assert "2022 Kia Telluride" in system and "Want a quick trade-in value?" in system
    # The default (general) agent must not quote a value, so none is listed for it.
    assert "$38,050" not in system and "estimated market value" not in system


@pytest.mark.parametrize("agent,shows", [("value_data_collection", True), ("general", False), ("inventory", False),
                                         ("scheduling", False), ("service_trade_reply", False)])
def test_only_the_value_agent_sees_vehicle_values(customer_client, agent, shows):
    client, _, providers = customer_client
    client.post("/v1/chat/completions", json={
        "model": "claude", "agent": agent, "use_tools": False, "tenant_id": TENANT, "sms_thread_key": "+15551234567",
        "messages": [{"role": "user", "content": "What would you give me for my car?"}],
    })
    # The asked agent's own prompt (a value question asked of inventory/scheduling is then re-routed to value).
    system = providers["claude"].requests[0].messages[0]["content"]
    assert ("estimated market value $38,050" in system) is shows
    assert "2022 Kia Telluride EX: 37,000 mi" in system  # vehicle and mileage stay visible to every agent
    assert ("never state or estimate what a vehicle is worth" in system) is not shows


def test_without_thread_keys_nothing_is_loaded(customer_client):
    client, store, providers = customer_client
    res = client.post("/v1/chat/completions", json={"model": "claude", "tenant_id": TENANT,
                                                    "messages": [{"role": "user", "content": "Hi"}]})
    assert res.status_code == 200 and store.calls == []
    assert "<customer>" not in providers["claude"].requests[0].messages[0]["content"]


def test_thread_keys_need_a_tenant(customer_client):
    client, _, _ = customer_client
    res = client.post("/v1/chat/completions", json={"model": "claude", "email_thread_key": "m@example.com",
                                                    "messages": [{"role": "user", "content": "Hi"}]})
    assert res.status_code == 422 and "tenant_id is required" in res.json()["error"]["message"]


async def test_store_degrades_when_db_is_down():
    store = CustomerStore(CustomerConfig(), "postgresql://nobody@127.0.0.1:1/none")
    await store.start()  # unreachable DB: logs, doesn't raise
    assert not store.ready
    assert (await store.load(uuid.UUID(TENANT), "+15551234567", None, "hi")).status == "error"
    assert (await store.load(uuid.UUID(TENANT), None, None, "hi")).status == "skipped"
    await store.aclose()


def _store_body(**kw):
    body = {"model": "claude", "tenant_id": TENANT, "sms_thread_key": "+15551234567", "store": True,
            "messages": [{"role": "assistant", "content": "Want a value?"}, {"role": "user", "content": "Yes, 37000 miles"}]}
    body.update(kw)
    return body


def test_store_saves_the_user_message_and_the_reply(customer_client):
    client, store, _ = customer_client
    res = client.post("/v1/chat/completions", json=_store_body(cnp_provider="telgorithm", step_num=3))
    assert res.status_code == 200, res.text
    assert res.json()["stored"] == {"channel": "sms", "inbound_id": "101", "outbound_id": "102", "error": None}
    assert store.stored == [(uuid.UUID(TENANT), "+15551234567", None, "Yes, 37000 miles", "Reply from claude", "agent", "telgorithm", 3)]


def test_store_is_opt_in_and_needs_one_channel(customer_client):
    client, store, _ = customer_client
    assert client.post("/v1/chat/completions", json=_store_body(store=False)).json()["stored"] is None
    assert store.stored == []
    res = client.post("/v1/chat/completions", json=_store_body(email_thread_key="m@example.com"))
    assert res.status_code == 422 and "exactly one" in res.json()["error"]["message"]


def test_store_failure_still_returns_the_reply(customer_client):
    client, store, _ = customer_client
    store.store_error = RuntimeError("db down")
    data = client.post("/v1/chat/completions", json=_store_body(sent_by="user-1")).json()
    assert data["choices"][0]["message"]["content"] == "Reply from claude"
    assert data["stored"]["error"] == "RuntimeError: db down"


def test_streaming_stores_the_full_reply(customer_client):
    client, store, _ = customer_client
    with client.stream("POST", "/v1/chat/completions", json=_store_body(model="llama", stream=True, sent_by="u1")) as res:
        lines = [line for line in res.iter_lines() if line]
    final = json.loads(lines[-2][6:])
    assert final["stored"]["outbound_id"] == "102"
    assert store.stored[0][4] == "Reply from llama" and store.stored[0][5] == "u1"



def test_a_noop_reply_logs_only_the_customer_message(customer_client):
    # n8n never sends <NOOP>: the customer's message is logged, no outbound row.
    client, store, providers = customer_client
    providers["claude"].reply = "<NOOP>"
    body = _store_body(reroute_noop=False)
    res = client.post("/v1/chat/completions", json=body)
    assert res.json()["choices"][0]["message"]["content"] == "<NOOP>"
    assert res.json()["stored"] == {"channel": "sms", "inbound_id": "101", "outbound_id": None, "error": None}
    assert store.stored[-1][3:5] == ("Yes, 37000 miles", "")


def test_opt_out_message_not_logged_when_record_opt_out_is_false(customer_client):
    # sms_log's own opt-out trigger would record a DNC for a stored STOP, so it is not stored at all.
    client, store, _ = customer_client
    body = _store_body(record_opt_out=False)
    body["messages"] = [{"role": "user", "content": "STOP"}]
    res = client.post("/v1/chat/completions", json=body).json()
    assert res["stored"] == {"channel": None, "inbound_id": None, "outbound_id": None,
                             "error": "opt-out message not stored: record_opt_out is false"}
    assert store.stored == []
    # an ordinary message is still stored, and with the default an opt-out message is too
    client.post("/v1/chat/completions", json=_store_body(record_opt_out=False))
    assert len(store.stored) == 1
    body["record_opt_out"] = True
    client.post("/v1/chat/completions", json=body)
    assert len(store.stored) == 2

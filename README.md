# inride-ai-agent

A small model gateway for dealer automotive conversations. One OpenAI-compatible
chat completion endpoint serves both **frontier** models (Anthropic, OpenAI,
Gemini, OpenRouter, Groq) and **open source** models (Ollama, vLLM, or any
OpenAI-compatible server). A single JSON value, `ai-agent-config`, holds the
whole model setup.


## Run

```bash
python -m pip install --user poetry
poetry install
cp dev.env.example dev.env      # put your ai-agent-config JSON in AI_AGENT_CONFIG
poetry run uvicorn app.main:app --reload --port 8080
```

Docs are at http://localhost:8080/docs. They are disabled when `ENV=prod`.

## Configuration: `ai-agent-config`

| ENV  | Where the JSON comes from |
|------|---------------------------|
| dev  | The `AI_AGENT_CONFIG` env var, loaded from `dev.env`. |
| prod | GCP Secret Manager secret `ai-agent-config`, read with `PROJECT_NUMBER`. You can override the secret name with `AI_AGENT_CONFIG_NAME`. |

The service validates the config at startup. A missing key, an unknown
fallback, or a duplicate model id stops the service with a
message naming the field. The full example is in
[ai-agent-config.example.json](ai-agent-config.example.json).

| Field | Meaning |
|-------|---------|
| `service_api_key` | Callers send this as `Authorization: Bearer …` or `X-API-Key`. If it is empty, auth is off in dev, and in prod every request is refused. |
| `models[]` | `id` (the name callers use), `type`, `provider`, `protocol`, `model` (the provider's own name), `base_url`, `api_key`, `headers`, plus per-model settings `max_tokens`, `temperature`, `timeout`, `max_retries`, `max_tokens_field`, `stream_usage`, and `fallbacks` (model ids tried in order). |

No model, vendor, or URL is hardcoded; everything comes from the config.

- `protocol` picks the wire format: `anthropic` (Anthropic Messages API) or `openai` (any OpenAI-compatible server: OpenAI, OpenRouter, Gemini, Groq, Ollama, vLLM, LM Studio, …).
- `provider` is a free-text label used in logs and error messages (`"ollama"`, `"my-vllm"`, …).
- `base_url` is required (Anthropic: `https://api.anthropic.com`; OpenAI-protocol servers: their `/v1` URL).
- `api_key` can be empty for servers that need none. A wrong or missing key comes back as a clear upstream auth error.
- `extra_body` sends extra request fields to the provider unchanged, e.g. `{"reasoning_effort": "none"}` to turn off a thinking model's hidden reasoning (much faster on Ollama).
- Vendor quirks are per-model fields: set `"max_tokens_field": "max_completion_tokens"` for OpenAI's own API, and `"stream_usage": false` for servers that reject usage reporting while streaming (Gemini).

## Knowledge base (`knowledge_base_system`)

Dealership facts are a static prompt: [app/prompts/knowledge_base_system.md](app/prompts/knowledge_base_system.md)
holds the instructions and, inside `<knowledge>`, every entry (FAQs, policies and the outbound campaign's SMS
compliance footers). There is no RAG, no embedding and no database read: the file is sent whole to every agent,
for every request, with or without `tenant_id`.

- **Placement.** Right after the agent prompt and its addendum, before the runtime block. It never changes, so it
  is part of the cached prefix (Anthropic `cache_control`; OpenAI and Ollama reuse the prefix on their own).
- **Format.** One entry per `[kind] tags: a, b | metadata: key: value, …` header line, followed by its text. The
  entries were copied from the `docs` table with exact duplicates listed once (32 entries, ~3k tokens).
- **Changing it.** Edit the file and restart the service. Startup logs the entry count;
  `GET /health` reports `knowledge_base_entries`.
- **Knowledge_Base_Search** parses the same entries from the file and searches them by keyword.

The service does not use the `docs` table at all (no reads, no writes, no `/v1/docs` endpoints). A `rag` section left in `ai-agent-config` is
ignored (with a startup warning).

The database is the backend's Postgres: `PG_DB_URL` in dev (`dev.env`), and in prod the `pg-db-url` secret
(override the name with `PG_DB_URL_NAME`). This is the Supavisor transaction pooler (:6543) that the backend's API
traffic uses, so prepared-statement caching is off. `PG_DB_URL_DIRECT` (session mode) is not needed: the backend
keeps it for LISTEN/NOTIFY.

## Customer records and history

When a request carries `sms_thread_key` and/or `email_thread_key`, it must also carry `tenant_id` (otherwise `422`).
The gateway then reads that customer's records from the same Postgres ([app/customer](app/customer)) and adds them to
the system prompt, after your `system` messages. It only reads; nothing is written.

- **Who the customer is.** Every lead of the tenant whose `phone` / `thread_id` matches the SMS number (in any of
  the formats it is stored in: `+15551234567`, `15551234567`, `5551234567`) or whose `email` matches. One number
  can have several leads and a lead several vehicles, so the names, rep, status, do-not-contact flag and every
  `lead_vehicles` row are gathered (duplicates of one VIN collapsed). Vehicles carry year, make, model, trim,
  mileage, `market_value` with its date, lease/loan/cash and sold status. The model is told to give values as
  estimates and to ask which car is meant when there are several. The dealership's name, address, phone and
  hours come from `tenants`.
- **What was said before.** `sms_log` by `thread_key`; `email_log` by `email_to` / `email_from` (inbound mail has
  the customer as `Name <addr>`). A thread can hold ~10K messages, so it is never loaded whole. Per channel, in
  one parallel round trip:
  - totals, first/last contact, the latest STOP (and whether they opted back in), and per-campaign counts
    (`cadences.name`): the automated campaign blasts are templates, so they are summarised, not listed;
  - the newest `recent_messages`, verbatim, minus the newest ones the caller already sent in `messages`;
  - older messages that match the customer's latest message (`pg_trgm` `word_similarity`), so an old quote or
    appointment still reaches the model;
  - older non-campaign messages (customer replies, staff and AI answers), newest first.

  These fill `history_tokens` (split between SMS and email) in that order and are shown oldest first, with times
  in the customer's time zone. Whatever doesn't fit is stated as a count (`[N other earlier messages not shown]`).
  Email bodies lose their HTML and quoted earlier mail.

If the DB is down or slower than `customer.timeout`, the reply still goes out and the model is told not to guess
the customer's vehicles or history. `GET /health` reports `customer_records: ready | unavailable | off`.

### Storing the exchange (`store`)

With `"store": true` and exactly one thread key, the gateway writes two rows to `sms_log` or `email_log` in one
transaction ([app/customer/writer.py](app/customer/writer.py)):

- the latest `user` message: `inbound`, status `received`;
- the reply: `outbound`, status `delivered`, `sent_by` from the request (default `agent`), and `delivered_at` set.
  A `<NOOP>` reply is not sent to the customer, so it gets no outbound row (`outbound_id` is null); only the
  customer's message is logged, as in n8n.
  For email, `sent_at` is set as well.

Both rows get `created_at` / `updated_at` set to the time of the insert, and SMS rows also get `received_at`.
`created_at_et` is generated by Postgres. Every other field is copied unchanged from the thread's latest message:
lead, campaign (`cadence_id`, `step_num`), template fields, the dealer's number or address, and the provider.
`cnp_provider` in the request overrides `sms_log.cnp_provider` / `email_log.provider`. Email keeps the thread's
subject as `Re: …`. A thread with no messages yet uses the tenant's `sms_number` / `email_from_address`.

The response gets `stored: {channel, inbound_id, outbound_id}`. When streaming, it is on the last chunk. If the write
fails, the reply is still returned, with `stored.error` set.

| `customer` field | Default | Meaning |
|------------------|---------|---------|
| `enabled` | `true` | Turn customer lookups off. |
| `history_tokens` | `4000` | Budget for the conversation history across SMS and email. |
| `recent_messages` | `40` | Newest messages per channel considered verbatim. |
| `relevant_messages` | `8` | Older messages per channel brought back because they match the latest message. |
| `older_messages` | `300` | Older non-campaign messages per channel read to fill the budget. |
| `max_message_chars` | `1500` | One message is cut to this. |
| `max_vehicles` | `30` | Most vehicles listed. |
| `timeout` | `5` | Seconds for all customer queries. |

## Agent tools

Each agent can call the tools wired to it in the n8n SMS handler (workflow `328CXNQvb2QWXXWk`), ported to Python in
[app/tools](app/tools). The model decides when to call one; the gateway runs it, hands the result back, and repeats
until the model answers (at most `tools.max_iterations` rounds, then it must answer).

| Agent | Tools |
|-------|-------|
| `general` | Escalate_To_Human, Get_SMS_conversation_log, Knowledge_Base_Search, Suppress_Lead |
| `inventory` | Escalate_To_Human, Search_Inventory |
| `scheduling` | Cancel_Appointment, Escalate_To_Human, Save_Appointment_Notes, Validate_Appointment |
| `value_data_collection` | Escalate_To_Human, Knowledge_Base_Search, Save_Appointment_Notes, Update_mileage |
| `service_trade_reply` | Escalate_To_Human, Get_SMS_conversation_log, Knowledge_Base_Search, Suppress_Lead |

| Tool | n8n node | What it does here |
|------|----------|-------------------|
| Knowledge_Base_Search | HTTP (query-kb webhook) | Keyword search (shared words, tags count double) over the static knowledge base entries the prompt already lists |
| Get_SMS_conversation_log | Supabase | The thread's `sms_log` rows, oldest first |
| Update_mileage | Supabase | `lead_vehicles.mileage`; only a figure the customer stated in their latest message |
| Validate_Appointment | Code | Same parsing and guards as n8n, in the dealership's hours and time zone |
| Save_Appointment_Notes | HTTP (PostgREST upsert) | Upsert on `(lead_id, cadence_id)`; a time is booked only if Validate_Appointment confirmed it in this completion |
| Cancel_Appointment | Code | `public.cancel_appointment` |
| Search_Inventory | Code | MarketCheck by `marketcheck_dealer_id`, dealer-site links from `tenant_inventory_config`, website fallback |
| Escalate_To_Human | Code | `escalations` row (deterministic id per inbound message, so a retry never pages twice) |
| Suppress_Lead | Code | `public.apply_lead_suppression` |

Book_Service_Appointment and Get_Service_Pricing are disabled in the workflow and wired to no agent, so they are
not offered even though some prompts mention them.

- **Only what the request can run is offered.** Tools that act on the customer need `tenant_id` plus a thread key
  (to find the lead); Knowledge_Base_Search needs nothing; Search_Inventory needs `tools.marketcheck_api_key`.
  Validate_Appointment needs nothing (it falls back to the default opening hours). Skipped tools are logged.
- **Failures never fail the reply.** A tool that errors, times out (`tools.timeout`) or refuses returns
  `{"ok": false, "error": …}` to the model, which answers accordingly.
- **Arguments are requests, not authorization**, as in n8n: a permanent DNC needs a STOP-keyword turn or a reported
  death; `vehicle_sold` needs a plain disposal statement; deferrals and bare acknowledgments never page a dealer.
  These checks read the customer's own message. n8n's Intent Classifier gates (protected closes, the deceased /
  wrong-owner halt and hold in Escalate_To_Human) run only when its output is supplied in `node_data`.
- **`write_mode`.** `dry_run` (default): tools that change data run every check and read, then report what they
  would write (`dry_run: true`) and write nothing. `live`: they write to the backend's Postgres like n8n does;
  an escalation row pages the dealer.
- **Models.** Set `"supports_tools": false` on a model that cannot call functions; it then answers from the prompt
  (as a fallback, it gets earlier tool calls as plain text). A model that rejects `tools` with a 400 is retried
  without them. Send `"use_tools": false` on a request to turn tools off for it.
- **Reasoning with tools.** `"tool_reasoning_effort": "low"` on a model raises a `none` reasoning effort (from the
  request or `extra_body`) to `low` whenever tools are offered. gpt-oss with no reasoning often writes tool calls
  as text instead of calling them. Unset, nothing changes.
- **Reply checks** (`app/services/reply_guard.py`, only when tools are offered):
  - A tool call the model wrote into its reply as JSON (`{"name": "Escalate_To_Human", "arguments": {...}}`,
    `{"Escalate_To_Human": {...}}`, also in a ```` ``` ```` fence) for a tool it was offered is run as a real call, so
    its checks apply, and the model writes the reply from the result. It is reported with `"from_text": true`.
    After the last allowed round the JSON is only removed. JSON naming no offered tool is left as is.
  - A reply that claims a hand-off ("I've flagged it for the team", "a manager will call you") with no
    `Escalate_To_Human` call returning `ok: true` this turn is asked once more, with the reason added to the
    system prompt. If the claim comes back, its sentences are removed; if nothing is left, the reply becomes
    "Thanks for letting me know." plus the dealership's main number when known.
- **Booking claims.** A reply that says an appointment is booked ("scheduled for Friday at 4", "you're all set",
  "I've got you down for…") with no appointment saved by Save_Appointment_Notes this turn is asked again once (with
  the reason), then the claim is removed; a reply that was only the claim becomes "Thanks! I want to make sure that
  time is open before I lock it in - what day and time work best for you?". "See you Friday" alone is not a claim.
- **Empty replies.** When the model's reply is still empty after `empty_retries`, a customer thread gets n8n's safe
  fallback ("…someone from our team will follow up shortly…") and Escalate_To_Human pages the team (`routing.outcome`
  `empty_fallback`). Only when the page succeeded (dry runs count); otherwise the `empty_response` error stands.
- **Value questions.** When Inventory or Scheduling answers a plain value question ("what would you give me for my
  car?", "what's it worth?") itself, without a tool, the turn is run on Value Data Collection (`rerouted`). "How
  much is the RAV4?" stays with Inventory; if Value declines, the first reply is kept.
- **SMS opt-out** (`app/services/opt_out.py`, a port of n8n's Opt-out Detector; on by default, `"opt_out_gate":
  false` turns it off). Before any agent runs on an SMS thread with a known lead:
  - an opt-out message (STOP and its typos, unsubscribe, "stop texting me", "leave me alone", "take me off your
    list", "wrong number", "not interested", 🛑 …, with n8n's guards: "stop by", "I don't want to opt out", our own
    "Reply STOP to opt out" footer quoted in a reaction) is recorded with `public.process_opt_out(tenant, lead,
    phone, 'SMS opt-out')` (dnc row, cadences stopped, lead `opted_out`; nothing in `dry_run`) and answered with the
    fixed confirmation "You've been unsubscribed and won't receive further messages." (a wrong-number variant). No
    model is called. If recording fails, the request fails with `opt_out_failed` instead of confirming;
  - a customer who already opted out (lead status or lifecycle `opted_out`, `dnc`, `blocked`, or a `dnc` row) gets
    `<NOOP>`: no agent runs. Opt-in (START) is not handled yet: it also gets `<NOOP>`.

  `"record_opt_out": false` (default `true`; the Chat Tester's "Recording opt-outs" chip) keeps opt-outs and DNCs
  out of the database for testing: the opt-out is detected and answered the same way, but `process_opt_out` is not
  called, Suppress_Lead writes no DNC row (its halt and hold still apply), and with `store` the opt-out message is
  not logged (sms_log's opt-out trigger would record a DNC).

  `routing.outcome` is `opted_out` or `silent_opted_out`; the `Opt_Out` entry in `tool_calls` reports the write.
- **Repeat decline / stop-contact safety net.** When any agent answers a repeat decline ("I already told you
  I'm not interested", or the classifier's `repeat_decline`) or a request to stop contacting the customer ("stop
  texting me", "leave me alone", "take me off your list", "don't text me") without calling Suppress_Lead, the
  gateway calls it as General's tool (`repeat_decline`: cadence halt + send hold, never a DNC; usual checks and
  `write_mode`) and uses General's wording: "Understood — I'll make sure you don't get further messages from us…" on
  success, "…Reply STOP…" on failure. Skipped for `<NOOP>`, complaints (classifier), "stop calling me", and after
  the agent's own Suppress_Lead call or a successful escalation.
- **`<NOOP>` replies** (`app/services/noop_router.py`, a port of n8n's Empty Reply Detector and NOOP Pivot
  Re-Router; on by default, `"reroute_noop": false` turns it off). Spellings like `noop` or `<<NOOP>>` become
  `<NOOP>`. Then:
  - the customer only closed the conversation ("thanks", "ok bye", "see you then", an emoji reaction): `<NOOP>`
    stays, nothing to send. A bare "yes" answering our question is not a closing;
  - a tool already handled the turn (Suppress_Lead ok, or an Escalate_To_Human attempt): `<NOOP>` stays;
  - otherwise the message was real: the turn runs once more on the agent its wording points to (scheduling words,
    then value words, then stock words, else `general`; never the agent that declined, except `general`, which gets a
    second opinion);
  - if that agent declines too, n8n's safe fallback text is sent (complaint, general or value wording) and
    Escalate_To_Human pages the team (its usual checks and `write_mode` apply). Two `general` declines send nothing
    but still page.

  The response's `routing` (`outcome`, `from_agent`, `to_agent`) says which happened; the Chat Tester footer shows
  it. When streaming without tools, a reply that could still be `<NOOP>` is held back until it is clear.
- **Streaming.** With tools, the tool rounds run first and the reply arrives as one content chunk; without tools
  it streams token by token as before.

The response lists what was used:

```json
"tools_used": ["Validate_Appointment", "Save_Appointment_Notes"],
"tool_calls": [
  {"id": "toolu_…", "name": "Validate_Appointment", "kind": "code", "arguments": {"proposed_time": "Tuesday at 2pm"},
   "ok": true, "latency_ms": 3, "error": null, "dry_run": false}
]
```

When streaming, both are on the last chunk. `GET /v1/agents` lists each agent's `tools`; `GET /health` reports
`tools: dry_run | live | no_database | off`.

| `tools` field | Default | Meaning |
|---------------|---------|---------|
| `enabled` | `true` | Turn every tool off. |
| `write_mode` | `dry_run` | `dry_run` or `live` (see above). |
| `max_iterations` | `6` | Model turns that may call tools before it must answer. |
| `timeout` | `15` | Seconds for one tool call. |
| `max_result_chars` | `8000` | A tool result is cut to this before the model sees it. |
| `disabled` | `[]` | Tool names never offered. |
| `marketcheck_api_key` | `""` | Search_Inventory is offered only when set. |
| `knowledge_base_limit` | `3` | Entries Knowledge_Base_Search returns. |

## API

### `POST /v1/chat/completions`

```json
{
  "model": "claude-sonnet",
  "model_type": "frontier",
  "messages": [{"role": "user", "content": "Is the 2021 Civic still available?"}],
  "temperature": 0.4,
  "max_tokens": 300,
  "stream": false
}
```

The only parameters are the LLM ones: `model`, `model_type`, `messages`,
`temperature`, `top_p`, `reasoning_effort`, `max_tokens`, `stop`, `response_format`, and `stream`, plus `tenant_id`
(the dealership: its records, the customer's lead and the tools are looked up with it), `sms_thread_key` (the customer's SMS number, `sms_log.thread_key`)
and `email_thread_key` (the customer's email address). See [Customer records](#customer-records-and-history).

`reasoning_effort` (`none` | `low` | `medium` | `high`) sets how much a thinking model reasons before answering, per request. It overrides any `reasoning_effort` in the model's `extra_body`; leave it unset to use the model's configured default. On OpenAI-protocol servers (Ollama, vLLM, OpenAI) it is sent as `reasoning_effort`. On Claude it becomes `output_config.effort`; Claude has no `none`, so `none` is sent as `low`. Claude models also reject `temperature` and `top_p`, so the gateway doesn't send those two to Claude.

- `model` is required; there is no default model. If it is missing or unknown, you get a `400` that lists the configured model ids. If you also send `model_type`, it must match the model, and the list is narrowed to that type.
- `agent` picks the system prompt: `app/prompts/agents/<agent>_system_instr.md` : `general` (default), `inventory`, `scheduling` (file `schedule_system_instr.md`), `value_data_collection`, `service_trade_reply` (see `GET /v1/agents`). Names are case-insensitive, spaces and hyphens count as `_`, and `schedule` is accepted for `scheduling`. Without it (or with `""`) the `general` agent answers; an unknown agent is a `400 unknown_agent` listing the choices. The files are only read. Their n8n `{{ … }}` expressions are replaced by the fallback each one names (e.g. `'the dealership'`), and the date/time ones point to a `RUNTIME CONTEXT` block with today's date and time (America/New_York). The system message is: agent prompt → its addendum → knowledge base (`knowledge_base_system`) → runtime context → your own `system` messages → customer records.
- **Large prompts.** The agent prompts are 8k–14k tokens. The agent prompt and the knowledge base always come first and are byte-identical on every request, so providers cache them: Anthropic via `cache_control` (cached reads cost ~10% and are faster), OpenAI and Ollama by reusing a matching prefix on their own. Set `context_window` on each model: the prompt is then fitted inside it. The agent's instructions are never cut; the reply's `max_tokens` is reserved; then the latest message, recent turns (up to half of what is left) and customer history get shrunk budgets, and the oldest turns are dropped. If even the agent prompt doesn't fit you get `400 context_window_exceeded` instead of a silently truncated prompt. **Ollama:** its default context length is far smaller than these prompts and it silently drops the start of a longer prompt. Start it with e.g. `OLLAMA_CONTEXT_LENGTH=32768` and set the model's `context_window` to the same value. Small local models (1B–3B) follow 14k-token rulebooks poorly; prefer 8B+ for these agents.
- **Workflow node values.** The prompts' `{{ $('<node>')… }}` expressions and the tools read other n8n nodes.
  - *Database-backed nodes* (`Get tenant`, `Get tenant emails & phone`, `Lookup Lead`, `Lookup Lead Vehicle`,
    `Get Recent SMS History`, `Insert Log`, `Get Equity Context`, `Load Conversation State`) are read by the gateway
    itself: the dealership from `tenant_id`, the lead and vehicle from the thread key. They fill the prompt (e.g. the
    dealer's slug and main phone, the customer's first name) and the tools. Sending one in `node_data` is a `400`.
    `Get Equity Context` and `Load Conversation State` (other agents' prompts) are not loaded yet; their expressions
    keep their fallbacks.
  - *The clock* (`Get Current Date/Time`) is the gateway's own: the server's time in America/New_York, as n8n's node
    computes it, in the `RUNTIME CONTEXT` block. Sending it in `node_data` is a `400` too.
  - *Other computed nodes* (`Intent Classifier`, `Normalize Phone`, `Combine messages`, `Select Market Value`) can be
    supplied in `node_data`, keyed by node name and shaped like that node's `$('<node>').first().json`; anything left
    out is derived as usual. `GET /v1/agents` lists, per agent, the ones it reads (`inputs`). Example:

  ```json
  "node_data": {
    "Intent Classifier": {"intent": "inventory", "active_buying_request": true, "explicit_decline_or_deferral": false}
  }
  ```
- Values the request leaves unset come from that model's config entry.
- If a model fails upstream (5xx, 429, timeout, unreachable, bad key), its `fallbacks` are tried in order. A request the model rejects as invalid (400) is not retried on another model. When streaming, fallback happens only before the first token is sent.
- An empty reply (no text and no tool call, `empty_response`) is first asked again of the same model, `empty_retries` more times (default `3`, `0`–`10`, `0` turns it off), and only then are the `fallbacks` tried. Each empty reply is logged with the model's `finish_reason`. When streaming, an empty stream is retried the same way, since nothing has been sent yet. The Chat Tester sets it with the **Retry empty** chip.

The response follows the OpenAI shape and adds `model_type`, `latency_ms`, `fallback_used`, `tools_used` and `tool_calls` (see [Agent tools](#agent-tools)). With `stream: true`, you get `chat.completion.chunk` SSE events ending with `data: [DONE]`, and the last chunk carries `usage`.

Errors always look like `{"error": {"message", "type", "code"}}`.

### `GET /v1/models`

Lists the configured models (`id`, `type`, `provider`, `model`). API keys are never returned.

### `GET /v1/agents` (and `GET /v1/playground/agents` for the chat tester)

Lists the agents a request can name in `agent`: `id`, `label`, `default` (true for `general`), `prompt_tokens` (estimated size of its instructions), `tools`, and `inputs`: the computed workflow nodes its prompt and tools read (`node`, `fields`, `list_of_rows`, `used_by`), i.e. what `node_data` can supply. Database-backed nodes and the clock are not listed: the gateway reads or computes them. The chat tester shows one JSON box per input under **Workflow data**.

### `GET /v1/models/remote`

Asks each configured server (each distinct `base_url`) which models it serves right now, using the server's own `/models` endpoint. For a local Ollama URL, that's the models installed on that machine. A server that can't be reached is listed with an `error` and an empty `models` list; it doesn't fail the whole response.

### `GET /health`

Returns the service status and the configured model ids.

## Code map

| Path | Role |
|------|------|
| `app/llm/settings.py` | The `ai-agent-config` schema and its validation |
| `app/llm/model_manager.py` | **ModelManager**: resolves a model, applies its per-model settings, calls it, falls back |
| `app/llm/anthropic_provider.py`, `app/llm/openai_compatible.py` | The two protocol adapters |
| `app/services/agent_prompts.py` | Loads and renders the agent prompts in `app/prompts/agents/` |
| `app/prompts/knowledge_base_system.md` | The static knowledge base every agent gets: instructions and all entries |
| `app/knowledge/base.py` | Reads that file, parses its entries, and the keyword search behind Knowledge_Base_Search |
| `app/services/chat_service.py` | Builds the request (system prompt + knowledge base), runs the tool loop, shapes JSON and SSE output |
| `app/tools/registry.py` | Which tools each agent has; runs a tool call safely |
| `app/tools/*.py` | The tools, ported from the n8n SMS handler |
| `app/core/config.py` | Dev/prod loading of `ai-agent-config` and `PG_DB_URL` |

To use the manager from other code:

```python
manager = ModelManager(config.load_agent_config())
done = await manager.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}]), model_id="llama-local")
print(done.model.id, done.result.content)
```

## Tests

```bash
poetry run pytest
```

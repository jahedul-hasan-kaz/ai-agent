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

| `rag` | Optional. Grounds every reply in the backend's `docs` table (see below). |

No model, vendor, or URL is hardcoded; everything comes from the config.

- `protocol` picks the wire format: `anthropic` (Anthropic Messages API) or `openai` (any OpenAI-compatible server: OpenAI, OpenRouter, Gemini, Groq, Ollama, vLLM, LM Studio, …).
- `provider` is a free-text label used in logs and error messages (`"ollama"`, `"my-vllm"`, …).
- `base_url` is required (Anthropic: `https://api.anthropic.com`; OpenAI-protocol servers: their `/v1` URL).
- `api_key` can be empty for servers that need none. A wrong or missing key comes back as a clear upstream auth error.
- `extra_body` sends extra request fields to the provider unchanged, e.g. `{"reasoning_effort": "none"}` to turn off a thinking model's hidden reasoning (much faster on Ollama).
- Vendor quirks are per-model fields: set `"max_tokens_field": "max_completion_tokens"` for OpenAI's own API, and `"stream_usage": false` for servers that reject usage reporting while streaming (Gemini).

## Knowledge base (RAG)

Dealership facts (FAQ, policies, offers, products, locations) are not in the system prompt. For every chat
message the service searches the `docs` table owned by **ai-agent-management**, which stores each
document's `content` and its pgvector `embedding`. Chat only reads that table; the `/v1/docs` endpoints below
are the only code here that writes to it, and nothing runs migrations.

1. **Query.** The latest user message. A short follow-up (under 6 words) is joined to the previous user message.
2. **Embed.** The query goes to `rag.embedding`, any OpenAI-compatible `/embeddings` endpoint. Use **the same model and dimensions that produced `docs.embedding`**. At startup the service reads the column's dimension and refuses to search on a mismatch.
3. **Search.** Cosine distance (`<=>`) over the request's `tenant_id` only, optionally narrowed to `rag.kinds`. It fetches `top_k × 2` rows.
4. **Select.** Drops matches below `min_similarity`, matches more than `max_similarity_gap` below the best one, and duplicate content. Keeps at most `top_k`.
5. **Build the context.** Each doc is cut to `max_doc_tokens`, and all of them together fit in `max_context_tokens` (~4 chars/token). The result is added as the last system block, inside `<knowledge>`, with instructions to answer only from it.

When nothing matches, when the request has no `tenant_id`, or when retrieval fails or takes longer than
`rag.timeout`, the reply still goes out. The model is then told it has no dealership facts and must say a team
member will confirm rather than guess. Failures are logged; a DB that is down is retried at most every 30s.
`GET /health` reports `rag: ready | unavailable | off`.

| `rag` field | Default | Meaning |
|-------------|---------|---------|
| `enabled` | `true` | Turn retrieval off without deleting the section. |
| `embedding` | required | `provider`, `model`, `base_url`, `api_key`, `headers`, `dimensions` (sent only if set), `timeout`, `max_retries`. |
| `top_k` | `5` | Most docs per prompt. |
| `min_similarity` | `0.3` | Cosine similarity floor. Tune it for your embedding model. |
| `max_similarity_gap` | `0.15` | Keep only docs within this much of the best match. |
| `max_context_tokens` / `max_doc_tokens` | `1500` / `600` | Token budgets for the whole block and for one doc. |
| `kinds` | `null` | Limit to these `docs.kind` values. |
| `timeout` | `5` | Seconds for embedding plus search. |

The database is the backend's Postgres: `PG_DB_URL` in dev (`dev.env`), and in prod the `pg-db-url` secret
(override the name with `PG_DB_URL_NAME`). This is the Supavisor transaction pooler (:6543) that the backend's API
traffic uses, so prepared-statement caching is off. `PG_DB_URL_DIRECT` (session mode) is not needed: the backend
keeps it for LISTEN/NOTIFY.

## Customer records and history

When a request carries `sms_thread_key` and/or `email_thread_key`, it must also carry `tenant_id` (otherwise `422`).
The gateway then reads that customer's records from the same Postgres ([app/customer](app/customer)) and adds them to
the system prompt, between your `system` messages and the retrieved knowledge. It only reads; nothing is written.

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
| Knowledge_Base_Search | HTTP (query-kb webhook) | pgvector search of the tenant's `docs`, with the gateway's own retriever |
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
  (to find the lead); Knowledge_Base_Search needs `rag`; Search_Inventory needs `tools.marketcheck_api_key`.
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
| `knowledge_base_limit` | `3` | Docs Knowledge_Base_Search returns. |

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
(the dealership whose `docs` ground the reply), `sms_thread_key` (the customer's SMS number, `sms_log.thread_key`)
and `email_thread_key` (the customer's email address). See [Customer records](#customer-records-and-history).

`reasoning_effort` (`none` | `low` | `medium` | `high`) sets how much a thinking model reasons before answering, per request. It overrides any `reasoning_effort` in the model's `extra_body`; leave it unset to use the model's configured default. On OpenAI-protocol servers (Ollama, vLLM, OpenAI) it is sent as `reasoning_effort`. On Claude it becomes `output_config.effort`; Claude has no `none`, so `none` is sent as `low`. Claude models also reject `temperature` and `top_p`, so the gateway doesn't send those two to Claude.

- `model` is required; there is no default model. If it is missing or unknown, you get a `400` that lists the configured model ids. If you also send `model_type`, it must match the model, and the list is narrowed to that type.
- `agent` picks the system prompt: `app/prompts/agents/<agent>_system_instr.md` : `general` (default), `inventory`, `scheduling` (file `schedule_system_instr.md`), `value_data_collection`, `service_trade_reply` (see `GET /v1/agents`). Names are case-insensitive, spaces and hyphens count as `_`, and `schedule` is accepted for `scheduling`. Without it (or with `""`) the `general` agent answers; an unknown agent is a `400 unknown_agent` listing the choices. The files are only read. Their n8n `{{ … }}` expressions are replaced by the fallback each one names (e.g. `'the dealership'`), and the date/time ones point to a `RUNTIME CONTEXT` block with today's date and time (America/New_York). The system message is: agent prompt → runtime context → your own `system` messages → customer records → retrieved knowledge.
- **Large prompts.** The agent prompts are 8k–14k tokens. The agent prompt always comes first and is byte-identical on every request, so providers cache it: Anthropic via `cache_control` (cached reads cost ~10% and are faster), OpenAI and Ollama by reusing a matching prefix on their own. Set `context_window` on each model: the prompt is then fitted inside it. The agent's instructions are never cut; the reply's `max_tokens` is reserved; then the latest message, recent turns (up to half of what is left), retrieved knowledge and customer history get shrunk budgets, and the oldest turns are dropped. If even the agent prompt doesn't fit you get `400 context_window_exceeded` instead of a silently truncated prompt. **Ollama:** its default context length is far smaller than these prompts and it silently drops the start of a longer prompt. Start it with e.g. `OLLAMA_CONTEXT_LENGTH=32768` and set the model's `context_window` to the same value. Small local models (1B–3B) follow 14k-token rulebooks poorly; prefer 8B+ for these agents.
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

The response follows the OpenAI shape and adds `model_type`, `latency_ms`, `fallback_used`, `tools_used` and `tool_calls` (see [Agent tools](#agent-tools)). With `stream: true`, you get `chat.completion.chunk` SSE events ending with `data: [DONE]`, and the last chunk carries `usage`.

Errors always look like `{"error": {"message", "type", "code"}}`.

### `POST /v1/docs`, `PUT /v1/docs/{id}`, `DELETE /v1/docs/{id}?tenant_id=`

Manage knowledge-base docs and keep `docs.embedding` in step with the text. Each needs the same API key as chat and a
configured `rag` section (otherwise `503 docs_unavailable`).

```json
POST /v1/docs
{"tenant_id": "<uuid>", "kind": "faq", "content": "Appraisals take 15-20 minutes.",
 "tags": ["trade-in"], "metadata": {"source": "website"}}
```

- **Required:** `tenant_id`, `kind` and `content`. Everything else (`tags`, `metadata`, `image_path`, `cadence_id`) is optional; `null` or a blank `""` means "not set".
- `kind` is one of `product`, `offer`, `compliance`, `persona`, `faq`, `policy` (the table's check constraint).
- **What is embedded:** `kind`, `tags`, `content` and every text, number and bool value in `metadata` (as `key: value`, nested keys dotted). **Datetimes and GUIDs are left out**: `id`, `tenant_id`, `cadence_id`, `updated_at`, and any tag or metadata value that is a GUID or an ISO date/datetime. `image_path` is left out too (a storage path). Dates written inside `content` are kept. The vector is stored in `docs.embedding`; the raw values (including the dates and GUIDs in `metadata`) are stored as sent.
- **Same model as search:** it uses `rag.embedding`, so stored vectors and query vectors are comparable. A dimension mismatch with `docs.embedding` is a `500 embedding_mismatch`.
- **Embed first, then write.** If the embedding model fails or times out (`502`/`504`), nothing is saved. A doc is never stored without an embedding or with a stale one.
- **`PUT`** changes only the fields sent (`null` clears `tags`, `metadata`, `image_path`, `cadence_id`; `kind` and `content` cannot be null). It re-embeds, using the stored values for the fields you did not send, only when `kind`, `tags`, `content` or `metadata` change. If the row changes while it is being re-embedded you get `409 doc_conflict`; retry.
- **Tenant-scoped.** `PUT` and `DELETE` need the doc's `tenant_id`; a doc of another tenant is a `404`. An unknown `tenant_id` or `cadence_id` is a `400 invalid_reference`.
- The response is the stored doc (without the vector) plus `embedded: {model, dimensions, chars, truncated}`. Text over ~24,000 characters is embedded up to that point (`truncated: true`): the rest is stored but not searchable. Split a long document into several docs.

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
| `app/rag/retriever.py` | Read-only pgvector search over `docs` and selection of the relevant matches |
| `app/rag/embedding_text.py` | Builds the text that is embedded for a doc (no datetimes or GUIDs) |
| `app/rag/store.py` | Creates, updates and deletes `docs` rows with their embedding |
| `app/api/v1/endpoints/docs.py` | The `/v1/docs` routes |
| `app/rag/context.py` | Builds the token-budgeted `<knowledge>` block, or the "nothing found" notice |
| `app/services/chat_service.py` | Builds the request (system prompt + retrieved context), runs the tool loop, shapes JSON and SSE output |
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

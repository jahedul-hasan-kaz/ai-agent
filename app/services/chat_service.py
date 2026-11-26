"""Turns an API request into a ModelManager call and shapes the answer (JSON or SSE)."""
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import AsyncIterator, Optional

from loguru import logger

from app.customer.context import build_customer_context, build_query
from app.customer.store import CustomerRecords, CustomerStore
from app.llm.base import LLMError, LLMRequest, LLMResult, StreamDone
from app.llm.model_manager import ModelManager
from app.llm.settings import CustomerConfig, ModelConfig
from app.models.chat import (
    AssistantMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    RoutingInfo,
    StoredMessages,
    ToolCallInfo,
    Usage,
)
from app.knowledge.base import knowledge_base_system
from app.services import noop_router
from app.services.agent_prompts import (
    AgentPrompt, CHARS_PER_TOKEN, addendum, estimate_tokens, get_agent, normalize, runtime_context,
)
from app.services.node_data import DATABASE_NODES
from app.services.reply_guard import retry_note, text_tool_calls, unevidenced_claim, without_claims
from app.tools.base import Tool, ToolContext
from app.llm.base import ToolCall
from app.tools.escalation import is_repeat_decline, protected_close_category
from app.tools.registry import ToolRun, ToolRunner, spec

# Role markers and separators per message.
_TOKENS_PER_MESSAGE = 4
# The customer's profile, leads and vehicles, which customer.history_tokens does not cover.
_CUSTOMER_RECORDS_TOKENS = 1000


@dataclass
class _Fit:
    turns: list[dict]
    history_tokens: int
    window: Optional[int] = None
    reserve: int = 0


@dataclass
class _Answer:
    """The reply after any tool rounds: the last model turn plus everything spent getting there."""

    model: ModelConfig
    result: LLMResult
    fallback_used: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    runs: list[ToolRun] = field(default_factory=list)
    ctx: Optional[ToolContext] = None  # the request's lookups, reused when a <NOOP> turn is handled
    routing: Optional[noop_router.Routing] = None

    @property
    def routing_info(self) -> Optional[RoutingInfo]:
        return RoutingInfo(**vars(self.routing)) if self.routing else None

    @property
    def tools_used(self) -> list[str]:
        return list(dict.fromkeys(r.name for r in self.runs))

    @property
    def tool_calls(self) -> list[ToolCallInfo]:
        return [ToolCallInfo(**vars(r)) for r in self.runs]


def error_body(exc: LLMError) -> dict:
    if exc.status_code == 400:
        kind = "invalid_request_error"
    elif exc.status_code == 401:
        kind = "authentication_error"
    elif exc.status_code == 404:
        kind = "not_found_error"
    elif exc.status_code == 409:
        kind = "conflict_error"
    elif exc.status_code == 429:
        kind = "rate_limit_error"
    elif exc.status_code in (502, 503, 504):
        kind = "upstream_error"
    else:
        kind = "api_error"
    return {"error": {"message": exc.message, "type": kind, "code": exc.code}}


class ChatService:
    def __init__(
        self,
        manager: ModelManager,
        customers: Optional[CustomerStore] = None,
        tools: Optional[ToolRunner] = None,
    ) -> None:
        self.manager = manager
        self.customers = customers
        self.tools = tools

    async def _context(self, req: ChatCompletionRequest) -> Optional[ToolContext]:
        """The per-request lookups (tenant, lead, vehicle, ...) shared by the prompt and the tools, so each record is
        read once per request. None when the gateway has no database side at all."""
        if self.tools is None:
            return None
        turns = [{"role": m.role, "content": m.content} for m in req.messages if m.role != "system"]
        return await self.tools.context(req.tenant_id, req.sms_thread_key, req.email_thread_key, turns, req.node_data)

    async def _database_nodes(self, agent: AgentPrompt, ctx: Optional[ToolContext]) -> dict:
        """The database-backed workflow nodes the agent's prompt reads, from the database: what n8n's "Get tenant",
        "Get tenant emails & phone", "Lookup Lead" and "Lookup Lead Vehicle" return. A lookup that fails or finds
        nothing leaves its node out, so the prompt keeps that expression's fallback."""
        wanted = set(agent.inputs()) & DATABASE_NODES
        if ctx is None or not wanted:
            return {}
        nodes: dict = {}
        if wanted & {"Get tenant", "Get tenant emails & phone"} and ctx.tenant_id is not None:
            tenant = await ctx.tenant()  # never raises
            if tenant:
                nodes["Get tenant"] = nodes["Get tenant emails & phone"] = tenant
        if wanted & {"Lookup Lead", "Lookup Lead Vehicle"} and ctx.tenant_id is not None and (
            ctx.sms_thread_key or ctx.email_thread_key
        ):
            try:
                nodes["Lookup Lead"] = await ctx.lead()
                if "Lookup Lead Vehicle" in wanted:
                    vehicle = await ctx.vehicle()
                    if vehicle:
                        nodes["Lookup Lead Vehicle"] = vehicle
            except Exception as exc:  # noqa: BLE001 - no lead or no database: the prompt keeps its fallbacks
                logger.info("prompt: lead/vehicle not loaded ({})", f"{type(exc).__name__}: {exc}"[:200])
        return nodes

    async def build_request(self, req: ChatCompletionRequest, ctx: Optional[ToolContext] = None) -> LLMRequest:
        # Order matters. The unchanging part leads, so providers can cache it as a prefix: the agent prompt, its
        # gateway addendum and the dealership knowledge base (knowledge_base_system, the same static text for every
        # agent). Then today's date/time, the caller's own system messages and the customer's records and history.
        agent = get_agent(req.agent)
        # The prompt's n8n expressions get the database-backed nodes read here and the computed ones the caller
        # supplied (node_data); see app/services/node_data.py.
        ctx = ctx or await self._context(req)
        nodes = {**(await self._database_nodes(agent, ctx)), **(req.node_data or {})}
        if nodes:
            logger.info("prompt nodes: {}", ", ".join(sorted(nodes)))
        agent_text = agent.render(nodes)
        static = [agent_text, *filter(None, [addendum(agent.id)]), knowledge_base_system()]
        system = [*static, runtime_context()] + [m.content for m in req.messages if m.role == "system"]
        all_turns = [{"role": m.role, "content": m.content} for m in req.messages if m.role != "system"]
        wants_customer = bool(req.tenant_id and (req.sms_thread_key or req.email_thread_key))
        customer_settings = self.customers.settings if self.customers else CustomerConfig()
        fit = self._fit(req, agent.id, system, all_turns, wants_customer, customer_settings)
        turns = fit.turns

        if wants_customer:
            system.append(await self._customer_context(req, turns, fit.history_tokens, agent.id))
        messages = [{"role": "system", "content": "\n\n".join(system)}] + turns
        prompt_tokens = sum(estimate_tokens(m["content"]) + _TOKENS_PER_MESSAGE for m in messages)
        logger.info(
            "prompt agent={} ~{} tokens (agent instructions ~{}){} turns={}/{}",
            agent.id, prompt_tokens, agent.tokens,
            f", fitted to context_window={fit.window} reserving {fit.reserve} for the reply" if fit.window else "",
            len(turns), len(all_turns),
        )
        return LLMRequest(
            cache_prefix=len("\n\n".join(static)),
            messages=messages,
            temperature=req.temperature,
            top_p=req.top_p,
            top_k=req.top_k,
            reasoning_effort=req.reasoning_effort,
            max_tokens=req.max_tokens,
            stop=req.stop,
            response_format=req.response_format.model_dump(by_alias=True, exclude_none=True)
            if req.response_format
            else None,
        )

    def _fit(
        self, req: ChatCompletionRequest, agent_id: str, system: list[str], turns: list[dict],
        wants_customer: bool, customer: CustomerConfig,
    ) -> "_Fit":
        """Split the model's context window between the parts of the prompt.

        The agent's instructions are never cut: a partial rulebook is worse than a clear error. What is left
        after them (and the static knowledge base) and the reply goes, in order, to the customer's latest message,
        recent turns (up to half), the customer's records and history, then older turns. The builders get these
        budgets up front, so they drop whole messages instead of text being chopped mid-way.
        """
        history_want = customer.history_tokens if wants_customer else 0
        chain = self.manager.candidates(self.manager.resolve(req.model, req.model_type))
        windows = [m.context_window for m in chain if m.context_window]
        if not windows:
            return _Fit(turns, history_want)

        # A fallback may answer, so fit the smallest window in the chain.
        window = min(windows)
        reserve = max(req.max_tokens or m.max_tokens for m in chain)
        required = sum(estimate_tokens(s) for s in system) + _TOKENS_PER_MESSAGE
        free = window - reserve - max(256, window // 20) - required

        last_user = max(i for i, t in enumerate(turns) if t["role"] == "user")
        cost = [estimate_tokens(t["content"]) + _TOKENS_PER_MESSAGE for t in turns]
        used, start = sum(cost[last_user:]), last_user
        if free < used:
            needed = required + used + reserve
            raise LLMError(
                f"The '{agent_id}' agent's instructions and the latest message need ~{needed} tokens, including "
                f"{reserve} reserved for the reply, but model '{chain[0].id}' fits {window} (context_window). "
                "Choose a model with a larger context window, lower max_tokens, or raise the model's context "
                "length (for Ollama, OLLAMA_CONTEXT_LENGTH) and its context_window to match.",
                400, retryable=False, code="context_window_exceeded",
            )
        while start > 0 and used + cost[start - 1] <= free // 2:
            start -= 1
            used += cost[start]

        left = free - used
        history = 0
        if history_want:
            left -= min(_CUSTOMER_RECORDS_TOKENS, left)
            history = min(history_want, left)
            left -= history
        while start > 0 and cost[start - 1] <= left:
            start -= 1
            left -= cost[start]
        # The context builders count ~4 chars/token; this budget counts 3.5.
        to_builder = lambda tokens: max(int(tokens * CHARS_PER_TOKEN / 4), 0)  # noqa: E731
        return _Fit(turns[start:], to_builder(history), window, reserve)

    # Only this agent may quote a vehicle's value (it applies the mileage-freshness and high-mileage rules first);
    # the others get records without values, so they cannot quote one.
    _VALUE_AGENTS = frozenset({"value_data_collection"})

    async def _customer_context(
        self, req: ChatCompletionRequest, turns: list[dict], history_tokens: int, agent_id: str = "value_data_collection"
    ) -> str:
        if self.customers is None:
            records = CustomerRecords("error", "customer records are not configured")
        else:
            records = await self.customers.load(req.tenant_id, req.sms_thread_key, req.email_thread_key, build_query(turns))
        s = self.customers.settings if self.customers else CustomerConfig()
        context = build_customer_context(
            records, history_tokens, s.max_message_chars, turns, show_values=agent_id in self._VALUE_AGENTS,
        )
        logger.info("Customer context status={} injected_chars={} (~{} tokens)", records.status, len(context), len(context) // 4)
        return context

    async def _store(self, req: ChatCompletionRequest, reply: str) -> Optional[StoredMessages]:
        """Save the latest user message and the reply when the request asked for it. Never raises: the reply wins."""
        if not req.store:
            return None
        customer_text = next(m.content for m in reversed(req.messages) if m.role == "user")
        if not reply.strip():
            return StoredMessages(error="empty reply, nothing stored")
        if noop_router.is_noop(reply):
            reply = ""  # nothing is sent to the customer, so no outbound row: only their message is logged (as n8n)
        if self.customers is None:
            return StoredMessages(error="customer records are not configured")
        try:
            done = await self.customers.store_exchange(
                req.tenant_id, req.sms_thread_key, req.email_thread_key, customer_text, reply,
                req.sent_by or "agent", req.cnp_provider, req.step_num,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"[:300]
            logger.error("Storing the exchange failed tenant={}: {}", req.tenant_id, message)
            return StoredMessages(error=message)
        logger.info("Stored exchange {} inbound={} outbound={}", done.channel, done.inbound_id, done.outbound_id)
        return StoredMessages(channel=done.channel, inbound_id=done.inbound_id, outbound_id=done.outbound_id)

    @staticmethod
    async def _without_claims(reply: str, ctx: Optional[ToolContext]) -> str:
        """The reply minus its unbacked hand-off claims; when nothing is left, a neutral line with the dealership's
        number when it is known."""
        rest = without_claims(reply)
        if rest:
            return rest
        tenant = await ctx.tenant() if ctx is not None and ctx.tenant_id is not None else None  # never raises
        phone = (tenant or {}).get("main_phone")
        return f"Thanks for letting me know. You can reach our team directly at {phone}." if phone else (
            "Thanks for letting me know."
        )

    async def _tooling(
        self, req: ChatCompletionRequest, ctx: Optional[ToolContext] = None
    ) -> tuple[Optional[ToolContext], dict[str, Tool]]:
        """The tools the request's agent may call in this request (see app/tools/registry.py)."""
        if self.tools is None or not self.tools.settings.enabled or not req.use_tools:
            return ctx, {}
        primary = self.manager.resolve(req.model, req.model_type)
        if not primary.supports_tools:
            logger.info("tools off: model {} has supports_tools=false", primary.id)
            return ctx, {}
        agent = get_agent(req.agent)
        ctx = ctx or await self._context(req)
        offered, skipped = self.tools.offered(agent.id, ctx)
        if offered or skipped:
            logger.info(
                "tools agent={} offered=[{}]{}", agent.id, ", ".join(t.name for t in offered),
                "".join(f" skipped {name}: {why};" for name, why in skipped.items()),
            )
        return ctx, {t.name: t for t in offered}

    async def _answer(self, req: ChatCompletionRequest, tooling=None) -> _Answer:
        """Ask the model; while it calls tools, run them and ask again with their results. After
        tools.max_iterations rounds of calls the model must answer (tool_choice=none)."""
        ctx = tooling[0] if tooling else await self._context(req)
        request = await self.build_request(req, ctx)
        ctx, tools = tooling or await self._tooling(req, ctx)
        if tools:
            request = replace(request, tools=[spec(t) for t in tools.values()])
        answer: Optional[_Answer] = None
        runs: list[ToolRun] = []
        rounds = 0
        rechecked = False
        while True:
            final_round = bool(request.tools) and rounds >= self.tools.settings.max_iterations
            if final_round:
                request = replace(request, tool_choice="none")
            try:
                done = await self.manager.complete(request, req.model, req.model_type, req.empty_retries)
            except LLMError as exc:
                if request.tools and not runs and _rejects_tools(exc):
                    # Some servers (and small local models) refuse `tools` outright: answer without them.
                    logger.warning("model rejected tools ({}); answering without them", exc.message[:200])
                    request, tools = replace(request, tools=[], tool_choice=None), {}
                    continue
                raise
            result, from_text = done.result, set()
            if tools and not result.tool_calls:
                # A call written into the reply as JSON: run it for real (or, when no more calls are allowed, just
                # keep the JSON out of the reply). See app/services/reply_guard.py.
                calls, rest = text_tool_calls(result.content, tools)
                if calls:
                    logger.warning("model {} wrote tool calls as text: {}", done.model.id, ", ".join(c.name for c in calls))
                    if final_round:
                        result = replace(result, content=rest)
                    else:
                        result = replace(result, content="", tool_calls=calls)
                        from_text = {c.id for c in calls}
            answer = _Answer(
                model=done.model,
                result=result,
                fallback_used=done.fallback_used or (answer.fallback_used if answer else False),
                prompt_tokens=(answer.prompt_tokens if answer else 0) + done.result.prompt_tokens,
                completion_tokens=(answer.completion_tokens if answer else 0) + done.result.completion_tokens,
                runs=runs,
            )
            if not result.tool_calls or not tools or final_round:
                claim = unevidenced_claim(result.content, runs) if tools else None
                if claim and not rechecked:
                    # Claims a hand-off no Escalate_To_Human call backs: ask once more, told why.
                    logger.warning("reply claims a hand-off without an escalation ({!r}); asking again", claim)
                    rechecked = True
                    system = {**request.messages[0], "content": request.messages[0]["content"] + retry_note(runs, tools)}
                    request = replace(request, messages=[system, *request.messages[1:]])
                    continue
                if claim:
                    answer.result = replace(result, content=await self._without_claims(result.content, ctx))
                    logger.warning("reply still claimed a hand-off ({!r}); claim removed", claim)
                break
            rounds += 1
            request.messages.append({
                "role": "assistant", "content": result.content, "tool_calls": result.tool_calls,
                "anthropic_blocks": result.anthropic_blocks,
            })
            # In order, one at a time: a later call may depend on an earlier one (Validate, then Save).
            for call in result.tool_calls:
                content, run = await self.tools.run(tools, call, ctx)
                if call.id in from_text:
                    run.from_text = True
                runs.append(run)
                request.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
        if not answer.result.content.strip():
            raise LLMError(f"{answer.model.id} kept calling tools without answering", code="empty_response")
        answer.ctx = ctx
        return answer

    # ---- <NOOP> handling (app/services/noop_router.py) -------------------------------------------------------

    async def _routed_answer(self, req: ChatCompletionRequest, tooling=None) -> _Answer:
        answer = await self._route_noop(req, await self._answer(req, tooling))
        return await self._ensure_repeat_decline_suppressed(req, answer)

    _SUPPRESSED_REPLY = "Understood — I'll make sure you don't get further messages from us. Sorry for the bother."
    _SUPPRESS_FAILED_REPLY = "Sorry for the bother. Reply STOP and you won't receive further messages."

    async def _ensure_repeat_decline_suppressed(self, req: ChatCompletionRequest, answer: _Answer) -> _Answer:
        """Safety net for the General agent's Repeat decline rule: a customer who says they already declined
        ("I already told you I'm not interested") must be suppressed (cadence halt + send hold), not just thanked.
        When the agent answered without calling Suppress_Lead, the gateway calls it (usual checks and write_mode) and
        uses the prompt's own wording for the result. Only when General answered with text, tools are available,
        and no Suppress_Lead or successful escalation already happened this turn."""
        agent = answer.routing.to_agent if answer.routing and answer.routing.outcome == "rerouted" else normalize(req.agent)
        if (agent != noop_router.GENERAL or noop_router.is_noop(answer.result.content) or self.tools is None
                or not self.tools.settings.enabled or not req.use_tools
                or _ran(answer.runs, "Suppress_Lead") or _ran(answer.runs, "Escalate_To_Human", ok=True)):
            return answer
        message, _ = await self._turn_text(req, answer.ctx)
        classifier = ((req.node_data or {}).get("Intent Classifier"))
        repeat = (protected_close_category(classifier, message) == "repeat_decline" if isinstance(classifier, dict)
                  else is_repeat_decline(message))
        if not repeat:
            return answer
        ctx = answer.ctx or await self._context(req)
        offered = {t.name: t for t in self.tools.offered(noop_router.GENERAL, ctx)[0]} if ctx else {}
        if "Suppress_Lead" not in offered:
            return answer
        call = ToolCall("call_repeat_decline", "Suppress_Lead", {
            "reason": "Customer firmly declined repeat contact", "suppression_category": "repeat_decline",
        })
        _, run = await self.tools.run(offered, call, ctx)
        answer.runs.append(run)
        reply = self._SUPPRESSED_REPLY if run.ok else self._SUPPRESS_FAILED_REPLY
        logger.warning("repeat decline without Suppress_Lead: gateway suppressed (ok={}), reply replaced", run.ok)
        answer.result = replace(answer.result, content=reply)
        return answer

    async def _route_noop(self, req: ChatCompletionRequest, answer: _Answer) -> _Answer:
        """What n8n's Empty Reply Detector and NOOP Pivot Re-Router do with an agent's <NOOP>: silence for a closing,
        another agent for a real message, and the safe fallback plus a page when that agent declines too."""
        if not noop_router.is_noop(answer.result.content):
            return answer
        answer.result = replace(answer.result, content=noop_router.NOOP)
        origin = normalize(req.agent)
        if not req.reroute_noop or origin not in (*noop_router.LANES, noop_router.GENERAL):
            return answer
        message, previous = await self._turn_text(req, answer.ctx)
        if _ran(answer.runs, "Suppress_Lead", ok=True):
            answer.routing = noop_router.Routing("silent_suppressed", origin)
        elif _ran(answer.runs, "Escalate_To_Human"):
            answer.routing = noop_router.Routing("silent_escalated", origin)
        elif not noop_router.is_substantive(message, previous):
            answer.routing = noop_router.Routing("silent_closing", origin)
        if answer.routing:
            logger.info("<NOOP> from {}: {}", origin, answer.routing.outcome)
            return answer

        target = noop_router.reroute_target(message, origin)
        logger.info("<NOOP> from {} on a substantive message: re-running on {}", origin, target)
        second = await self._answer(req.model_copy(update={"agent": target}))
        combined = _Answer(
            model=second.model, result=second.result, fallback_used=answer.fallback_used or second.fallback_used,
            prompt_tokens=answer.prompt_tokens + second.prompt_tokens,
            completion_tokens=answer.completion_tokens + second.completion_tokens,
            runs=answer.runs + second.runs, ctx=second.ctx or answer.ctx,
        )
        if not noop_router.is_noop(second.result.content):
            combined.routing = noop_router.Routing("rerouted", origin, target)
            return combined
        combined.result = replace(second.result, content=noop_router.NOOP)
        if _ran(second.runs, "Suppress_Lead", ok=True):
            combined.routing = noop_router.Routing("silent_suppressed", origin, target)
            return combined
        if _ran(second.runs, "Escalate_To_Human"):
            combined.routing = noop_router.Routing("silent_escalated", origin, target)
            return combined
        complaint = ((req.node_data or {}).get("Intent Classifier") or {}).get("complaint_signals") is True
        page = await self._page_for_noop(req, origin, combined.ctx, complaint, message)
        if page:
            combined.runs.append(page)
        if origin == noop_router.GENERAL:
            # Two independent silences agree: send nothing, but a person sees the turn.
            combined.routing = noop_router.Routing("silent_confirmed", origin, target)
        else:
            first_name = await self._first_name(combined.ctx)
            combined.result = replace(combined.result, content=noop_router.fallback_text(target, first_name, complaint))
            combined.routing = noop_router.Routing("fallback", origin, target)
        logger.warning("<NOOP> from {} and {}: {}", origin, target, combined.routing.outcome)
        return combined

    async def _turn_text(self, req: ChatCompletionRequest, ctx: Optional[ToolContext]) -> tuple[str, str]:
        """The customer's messages since our last reply (n8n "Combine messages") and our last message before them
        (from the conversation, else the thread's SMS log)."""
        turns = [m for m in req.messages if m.role != "system"]
        tail: list[str] = []
        for m in reversed(turns):
            if m.role != "user":
                break
            tail.append(m.content)
        message = " ".join(reversed(tail))
        previous = next((m.content for m in reversed(turns[: len(turns) - len(tail)]) if m.role == "assistant"), "")
        if not previous and ctx is not None and ctx.pool is not None:
            try:
                recent = await ctx.recent_sms(5)
                previous = next((str(r["message"]) for r in reversed(recent) if r["direction"] == "outbound"), "")
            except Exception as exc:  # noqa: BLE001 - only refines the closing check
                logger.info("<NOOP>: recent outbound not loaded ({})", type(exc).__name__)
        return message, previous

    @staticmethod
    async def _first_name(ctx: Optional[ToolContext]) -> str:
        if ctx is None or ctx.tenant_id is None:
            return ""
        try:
            return str((await ctx.lead()).get("first_name") or "")
        except Exception:  # noqa: BLE001 - no lead: the fallback has a name-less form
            return ""

    async def _page_for_noop(
        self, req: ChatCompletionRequest, origin: str, ctx: Optional[ToolContext], complaint: bool, message: str
    ) -> Optional[ToolRun]:
        """n8n's escalation_required for a turn no agent answered: Escalate_To_Human, with its usual checks and
        write_mode. Skipped when tools are off or the request has no customer to escalate."""
        if self.tools is None or not self.tools.settings.enabled or not req.use_tools:
            return None
        ctx = ctx or await self._context(req)
        offered = {t.name: t for t in self.tools.offered(origin, ctx)[0]} if ctx else {}
        if "Escalate_To_Human" not in offered:
            return None
        call = ToolCall("call_noop_fallback", "Escalate_To_Human", {
            "reason": f"No agent could answer the customer's message; the safe fallback was used: {message[:300]}",
            "category": "complaint" if complaint else "other", "urgency": "high" if complaint else "normal",
        })
        _, run = await self.tools.run(offered, call, ctx)
        return run

    async def complete(self, req: ChatCompletionRequest) -> ChatCompletionResponse:
        started = time.perf_counter()
        answer = await self._routed_answer(req)
        latency_ms = _elapsed_ms(started)
        usage = Usage.of(answer.prompt_tokens, answer.completion_tokens)
        reply = answer.result.content.strip()
        stored = await self._store(req, reply)
        logger.info(
            "chat completion model={} tokens={} latency_ms={} tools_used={}",
            answer.model.id, usage.total_tokens, latency_ms, ",".join(answer.tools_used) or "none",
        )
        return ChatCompletionResponse(
            id=_completion_id(),
            created=int(time.time()),
            model=answer.model.id,
            model_type=answer.model.type,
            choices=[
                Choice(
                    message=AssistantMessage(content=reply),
                    finish_reason=answer.result.finish_reason,
                )
            ],
            usage=usage,
            latency_ms=latency_ms,
            fallback_used=answer.fallback_used,
            tools_used=answer.tools_used,
            tool_calls=answer.tool_calls,
            stored=stored,
            routing=answer.routing_info,
        )

    def stream(self, req: ChatCompletionRequest) -> AsyncIterator[str]:
        """Validate the model choice now, so a bad `model` is a plain 400 instead of a broken stream."""
        primary = self.manager.resolve(req.model, req.model_type)
        return self._sse(req, primary)

    async def _sse(self, req: ChatCompletionRequest, primary: ModelConfig) -> AsyncIterator[str]:
        """OpenAI-style `chat.completion.chunk` events, ending with `data: [DONE]`.

        When the agent has tools, the tool rounds run first and the finished reply is sent as one content chunk:
        a reply cannot start streaming before the model has decided whether it still needs a tool."""
        started, completion_id, created = time.perf_counter(), _completion_id(), int(time.time())

        def chunk(model: ModelConfig, delta: dict, finish_reason=None) -> dict:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model.id,
                "model_type": model.type.value,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        def final_chunk(model: ModelConfig, finish_reason, prompt_tokens, completion_tokens, fallback_used,
                        tools_used: list[str], tool_calls: list[ToolCallInfo]) -> dict:
            final = chunk(model, {}, finish_reason or "stop")
            final["usage"] = Usage.of(prompt_tokens, completion_tokens).model_dump()
            final["latency_ms"] = _elapsed_ms(started)
            final["fallback_used"] = fallback_used
            final["tools_used"] = tools_used
            final["tool_calls"] = [c.model_dump() for c in tool_calls]
            return final

        try:
            tooling = await self._tooling(req, await self._context(req))
            if tooling[1]:
                answer = await self._routed_answer(req, tooling)
                reply = answer.result.content.strip()
                yield _event(chunk(answer.model, {"role": "assistant", "content": ""}))
                yield _event(chunk(answer.model, {"content": reply}))
                final = final_chunk(
                    answer.model, answer.result.finish_reason, answer.prompt_tokens, answer.completion_tokens,
                    answer.fallback_used, answer.tools_used, answer.tool_calls,
                )
                if answer.routing:
                    final["routing"] = answer.routing_info.model_dump()
                stored = await self._store(req, reply)
                if stored:
                    final["stored"] = stored.model_dump()
                yield _event(final)
            else:
                first, parts = True, []
                request = await self.build_request(req, tooling[0])
                async for model, item in self.manager.stream(request, primary.id, empty_retries=req.empty_retries):
                    if isinstance(item, StreamDone):
                        reply, routing, runs = "".join(parts).strip(), None, []
                        prompt_tokens, completion_tokens = item.prompt_tokens, item.completion_tokens
                        fallback_used = model.id != primary.id
                        if first and noop_router.is_noop(reply):
                            # Nothing was sent yet: handle the <NOOP> as a complete() reply would be.
                            answer = await self._route_noop(req, _Answer(
                                model, LLMResult(reply, item.finish_reason, prompt_tokens, completion_tokens),
                                fallback_used, prompt_tokens, completion_tokens, ctx=tooling[0],
                            ))
                            model, reply, routing, runs = answer.model, answer.result.content.strip(), answer.routing_info, answer.runs
                            prompt_tokens, completion_tokens = answer.prompt_tokens, answer.completion_tokens
                            fallback_used = answer.fallback_used
                        if first:
                            yield _event(chunk(model, {"role": "assistant", "content": ""}))
                            yield _event(chunk(model, {"content": reply}))
                        final = final_chunk(
                            model, item.finish_reason, prompt_tokens, completion_tokens, fallback_used,
                            list(dict.fromkeys(r.name for r in runs)), [ToolCallInfo(**vars(r)) for r in runs],
                        )
                        if routing:
                            final["routing"] = routing.model_dump()
                        stored = await self._store(req, reply)
                        if stored:
                            final["stored"] = stored.model_dump()
                        yield _event(final)
                        continue
                    parts.append(item)
                    if first and _could_be_noop("".join(parts)):
                        continue  # hold back a reply that may still turn out to be <NOOP>
                    if first:
                        yield _event(chunk(model, {"role": "assistant", "content": ""}))
                        first = False
                        item = "".join(parts)
                    yield _event(chunk(model, {"content": item}))
        except LLMError as exc:
            yield _event(error_body(exc))
        yield "data: [DONE]\n\n"


def _ran(runs: list[ToolRun], name: str, ok: Optional[bool] = None) -> bool:
    return any(r.name == name and (ok is None or r.ok == ok) for r in runs)


_NOOP_CHARS = set("<>[](){}/noop .!?\"'*`-\u2026")


def _could_be_noop(text: str) -> bool:
    """A streamed reply so far could still become <NOOP> (or one of its spellings)."""
    return len(text.strip()) <= 12 and set(text.lower()) <= _NOOP_CHARS


def _rejects_tools(exc: LLMError) -> bool:
    """A 400 that names tools: the model or server does not do function calling."""
    return exc.code == "invalid_request" and "tool" in exc.message.lower()


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"

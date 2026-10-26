"""Port of src/agent/run.ts — one turn as a run of steps, as an async loop.

The client's controller is callback-driven (a step completes inside the
transport's onDone). On the server a step is awaited, so the controller is a
loop: `start()` runs steps until the run ends, `pause()`/`resume()` stop and
continue it between steps (backend_authority.md "Runaway runs"), and
`snapshot()`/`restore()` carry it across an API restart.
"""
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from wc_text.invocations import arguments_text_of, collect_step, plan_writes
from wc_text.jsstr import js_trim
from wc_text.plan import unfinished_plan_items
from wc_text.policy import decide_after_step, detect_step_failure, steps_left
from wc_text.run_compaction import elision_trace, plan_elisions, prompt_tokens
from wc_text.reminders import (LOOKUP_NUDGE_STEPS, PLAN_NUDGE_BUDGET, REPEAT_NUDGE_STEPS, append_reminders, html_read_nudge,
                               long_reasoning_reminder, lookup_streak_nudge, plan_reminder, plan_unfinished_nudge, repeat_nudge,
                               steer_message, unbacked_claim_nudge, wrap_reminder, carried_plan_reminder)
from wc_text.stream_handlers import NO_ACTION_RETRY_INSTRUCTION, STEP_LIMIT_NOTE
from wc_text.text import claims_own_write, is_blank_content
from wc_text.tool_call_stream import call_signature

from .registry import Tool, ToolRegistry
from .tools.book_reads import settle_deletion
from .types import ToolContext, seen_chapters, urls_in_text, writes_so_far

UNKNOWN_TOOL_KIND = "read"
logger = logging.getLogger("web_canvas.runs")


class StepUnavailable(Exception):
    """The driver could not get this step through (agentic_chat_loop.md §0.10).

    The loop puts the step back and pauses with `pause` ({reason, message, …});
    a resume sends the same step again.
    """

    def __init__(self, pause: Dict[str, Any]) -> None:
        super().__init__(pause.get("message") or pause.get("reason") or "step unavailable")
        self.pause = pause


class StepOutput:
    def __init__(self, text: str, native_calls: Optional[List[Dict[str, Any]]] = None, thinking: Optional[List[Any]] = None,
                 response_items: Optional[List[Any]] = None, usage: Optional[Dict[str, Any]] = None) -> None:
        self.text = text
        self.native_calls = native_calls or []
        self.thinking = thinking
        self.response_items = response_items
        self.usage = usage


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def offered_tools_for(registry: ToolRegistry, ctx: ToolContext, write_protocol: str, agent_tools: bool) -> List[Tool]:
    """The tools a run offers natively — known before the run exists, for a call that must offer the same (a summary)."""
    return registry.available(ctx, lambda t:
                              (write_protocol == "tools" or not t.markup_form or (t.native_on_markup and agent_tools))
                              and (agent_tools or t.markup_form))


def _with_carried_plan(messages: List[Dict[str, Any]], ctx: ToolContext, carried: Optional[List[Dict[str, str]]]) -> List[Dict[str, Any]]:
    """Port of withCarriedPlan (run.ts): the previous turn's unfinished plan becomes the run's, said on the last user message."""
    if not carried or not unfinished_plan_items(carried):
        return messages
    ctx.run.plan = [dict(i) for i in carried]
    for i in carried:
        if i.get("status") == "in_progress":
            ctx.run.plan_baseline[i["id"]] = 0
    if not messages or messages[-1].get("role") != "user":
        return messages
    last = messages[-1]
    return [*messages[:-1], {**last, "content": f"{last.get('content') or ''}\n\n{wrap_reminder(carried_plan_reminder(carried))}"}]


class AgentRun:
    def __init__(self, *, registry: ToolRegistry, ctx: ToolContext, write_protocol: str,
                 driver: Callable[[List[Dict[str, Any]], int, bool], Awaitable[StepOutput]], observer: Any,
                 budgets: Dict[str, Any], policy: Dict[str, Any], can_continue: bool, initial_messages: List[Dict[str, Any]],
                 agent_tools: bool = True, guard: Optional[Callable[["AgentRun"], Optional[Dict[str, Any]]]] = None,
                 long_reasoning_tokens: int = 0, reminders: Optional[Callable[["AgentRun"], List[str]]] = None,
                 prompt_token_limit: int = 0, carried_plan: Optional[List[Dict[str, str]]] = None) -> None:
        self.registry = registry
        self.ctx = ctx
        self.write_protocol = write_protocol
        self.driver = driver
        self.observer = observer
        self.budgets = budgets
        self.policy = policy
        self.can_continue = can_continue
        self.agent_tools = agent_tools
        self.guard = guard
        self.long_reasoning_tokens = long_reasoning_tokens
        self.host_reminders = reminders
        #: Past ELIDE_ABOVE of this, the oldest read results are elided before the next step (wc_text.run_compaction). 0 = never.
        self.prompt_token_limit = prompt_token_limit
        #: Read results in the messages that may be elided when the prompt outgrows the window.
        self.elidable: List[Dict[str, Any]] = []
        #: Where the latest step's results start in the messages: never elided.
        self.last_results_start = 0
        #: The last step's real prompt size and the messages it covered (calibrates the elision check).
        self.measured: Optional[Dict[str, int]] = None
        self.long_reasoning_due: Optional[Dict[str, int]] = None
        self.plan_nudges = 0
        #: The trace of the last HTML read that no write has followed yet (the step before an edit).
        self.html_read_pending: Optional[str] = None
        self.html_read_nudged = False
        #: A reply claimed a write the run never made: told once per run.
        self.claim_nudged = False
        #: The look-up streak reminder was given (once per run).
        self.lookup_nudged = False
        #: Messages the user sent while the run was working; the next step carries them (steer).
        self.pending_steers: List[str] = []
        #: The chat message ids of the pending steers, for the timeline mark (see run.ts takeSteer).
        self.pending_steer_ids: List[str] = []
        self.messages: List[Dict[str, Any]] = _with_carried_plan(list(initial_messages), ctx, carried_plan)
        #: Where this turn's own messages start (its final user message): the transcript is from here (cache_continuity.md §3.1).
        self.initial_count = len(initial_messages)
        #: The step that ended the run, not appended to the messages: the transcript's last messages.
        self._final_messages: List[Dict[str, Any]] = []
        self.steps_taken = 0
        self.corrective_used = 0
        self.cancelled = False
        self.finished = False
        self.paused: Optional[Dict[str, Any]] = None
        self.chat_texts: List[str] = []
        self.timeline: List[Dict[str, Any]] = []
        self.trace: List[str] = []
        self.stray = 0
        self.wrote = False
        self.step_dropped = 0
        self.effects: Dict[str, Any] = {"canvasIssue": None, "failedEdits": 0, "reinsertedImages": 0, "selectionGone": False, "producedNothing": False}
        self.usage: Dict[str, int] = {"promptTokens": 0, "completionTokens": 0, "cachedPromptTokens": 0}
        # Per step: the calls it made and what they returned, for the pause guard.
        self.step_log: List[Dict[str, Any]] = []
        # Steps before this index were judged already: a guard looks only at
        # what happened since the last resume, or it would pause again at once.
        self.guard_floor = 0
        self._offered: Optional[List[Tool]] = None
        self._next: Optional[Dict[str, Any]] = None
        ctx.run.write_protocol = write_protocol
        ctx.run.continues_after_writes = policy["continueAfterWrites"]

    # ── driving ──────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._next = {"messages": self.messages, "step": self.steps_taken, "final": steps_left(self.budgets, self.steps_taken) == 1}
        await self._loop()

    async def resume(self) -> None:
        self.paused = None
        self.cancelled = False
        self.guard_floor = len(self.step_log)
        await self._loop()

    async def resume_with_answer(self, answer: str, confirmed: bool = False) -> None:
        """The user answered a question: the answer is the next message, and the run goes on.

        A question delete_chapter asked is settled here first: the chapter is
        deleted only when the Delete button sent the answer (`confirmed`), and
        the message says what happened, because the CHAPTER INDEX the model
        has still shows it.
        """
        question = self.ctx.run.question or {}
        self.ctx.run.question = None
        content = f"The user answered: {answer}"
        if question.get("confirm"):
            content += "\n\n" + await settle_deletion(self.ctx, question["confirm"], confirmed)
        self.messages = [*self.messages, {"role": "user", "content": content}]
        self._next = {"messages": self.messages, "step": self.steps_taken, "final": steps_left(self.budgets, self.steps_taken) == 1}
        await self.resume()

    def cancel(self) -> None:
        self.cancelled = True

    @property
    def has_next(self) -> bool:
        """A step is waiting to be sent (a resume continues with it)."""
        return self._next is not None

    def steer(self, text: str, message_id: Optional[str] = None) -> None:
        """A message the user sent mid-turn: appended as a user message once the step in flight has finished."""
        if js_trim(text):
            self.pending_steers.append(text)
            self.ctx.run.user_urls.extend(u for u in urls_in_text(text) if u not in self.ctx.run.user_urls)
            if message_id:
                self.pending_steer_ids.append(message_id)

    def _take_steer(self) -> Optional[str]:
        if not self.pending_steers:
            return None
        text = "\n\n".join(self.pending_steers)
        # Where the steer landed: the chat starts a new bubble there (display only).
        self.timeline.append({"type": "steer", "messageIds": list(self.pending_steer_ids), "text": text})
        self.pending_steers = []
        self.pending_steer_ids = []
        return steer_message(text)

    async def _loop(self) -> None:
        while self._next is not None and not self.finished and not self.cancelled:
            if self.guard is not None:
                reason = self.guard(self)
                if reason:
                    self.paused = reason
                    await _maybe_await(self.observer.on_paused(reason, self.progress()))
                    return
            pending = self._next
            self._next = None
            try:
                out = await self.driver(pending["messages"], pending["step"], pending["final"])
            except StepUnavailable as exc:
                # Paused, not ended: the step goes back, the snapshot keeps it.
                self._next = pending
                self.paused = exc.pause
                await _maybe_await(self.observer.on_paused(exc.pause, self.progress()))
                return
            await self.step_done(out)
            # A step may leave the run waiting (a question for the user): the
            # next step is kept for the resume, not started now.
            if self.paused is not None:
                return
        if self.cancelled and not self.finished:
            await self.finish({"failedUpdate": None, "exhaustedCorrective": False, "unretriableFailedUpdate": False, "endReason": "cancelled"})

    def offered_tools(self) -> List[Tool]:
        if self._offered is None:
            self._offered = offered_tools_for(self.registry, self.ctx, self.write_protocol, self.agent_tools)
        return self._offered

    # ── a step finished ──────────────────────────────────────────────────────
    async def step_done(self, out: StepOutput) -> None:
        if self.finished:
            return
        # What this step was sent is self.messages, untouched until its results are appended.
        if out.usage and out.usage.get("promptTokens"):
            self.measured = {"tokens": int(out.usage["promptTokens"]), "length": len(self.messages)}
        self.steps_taken += 1
        if out.usage:
            for key in ("promptTokens", "completionTokens", "cachedPromptTokens"):
                self.usage[key] += int(out.usage.get(key) or 0)
        collected = collect_step(out.text, out.native_calls, self.registry.descriptor, self.steps_taken - 1,
                                 {"markupProtocol": self.write_protocol == "markup"})
        failure = detect_step_failure({"text": out.text, "writeProtocol": self.write_protocol, "hadNativeCalls": bool(out.native_calls),
                                       "markupKind": collected["markupKind"], "wroteThisRun": self.wrote})
        if failure:
            await self._handle_protocol_failure(failure, out, collected)
            return

        self.ctx.run.step = self.steps_taken - 1
        # Deletions after everything, renames included: they renumber the chapters after them.
        last = lambda inv: bool((self.registry.get(inv["name"]) or Tool("", "", {}, "read", None)).run_last)  # noqa: E731
        renumbers = lambda inv: bool((self.registry.get(inv["name"]) or Tool("", "", {}, "read", None)).renumbers)  # noqa: E731
        writes = [inv for inv in collected["invocations"] if self._kind_of(inv) == "write" and not last(inv)]
        planned = plan_writes(writes)
        to_run = ([inv for inv in collected["invocations"] if self._kind_of(inv) != "write" and not last(inv)]
                  + planned["run"] + [inv for inv in collected["invocations"] if last(inv) and not renumbers(inv)]
                  + [inv for inv in collected["invocations"] if last(inv) and renumbers(inv)])
        self.stray += collected["strayMarkup"] + planned["dropped"]
        self.step_dropped = collected["strayMarkup"] + planned["dropped"]
        if js_trim(collected["chatText"]):
            self.chat_texts.append(js_trim(collected["chatText"]))
            self.timeline.append({"type": "text", "text": js_trim(collected["chatText"])})

        results = []
        for inv in to_run:
            results.append(await self._invoke(inv))
        await self._after_execute(out, to_run, results)

    def _kind_of(self, inv: Dict[str, Any]) -> str:
        tool = self.registry.get(inv["name"])
        return tool.kind if tool else UNKNOWN_TOOL_KIND

    async def _invoke(self, inv: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.registry.get(inv["name"])
        if tool is None:
            return {"ok": False, "content": f'There is no tool named "{inv["name"]}".', "trace": f'⚠️ unknown tool "{inv["name"]}"'}
        try:
            return await tool.invoke(inv, self.ctx)
        except Exception as e:  # noqa: BLE001 — a tool failure is a result the model reads
            # Logged with its traceback: "analyze_book failed" alone hid a bug for days.
            logger.warning("Tool %s raised", inv["name"], exc_info=True)
            return {"ok": False, "content": f"{inv['name']} failed: {e}", "trace": f"⚠️ {inv['name']} failed"}

    async def _handle_protocol_failure(self, failure: str, out: StepOutput, collected: Dict[str, Any]) -> None:
        can_correct = (self.can_continue and not self.cancelled and self.corrective_used < self.budgets["maxCorrective"]
                       and steps_left(self.budgets, self.steps_taken) > 0)
        if can_correct:
            self.corrective_used += 1
            await _maybe_await(self.observer.on_corrective(failure, self.corrective_used, self.budgets["maxCorrective"]))
            reply: Dict[str, Any] = {"role": "assistant", "content": out.text}
            if out.response_items:
                reply["responseItems"] = out.response_items
            self.messages = [*self.messages, reply, {"role": "user", "content": NO_ACTION_RETRY_INSTRUCTION}]
            self.step_log.append({"calls": [], "results": [], "wrote": False, "failure": failure})
            self._next = {"messages": self.messages, "step": self.steps_taken, "final": steps_left(self.budgets, self.steps_taken) == 1}
            return
        if js_trim(collected["chatText"]):
            self.chat_texts.append(js_trim(collected["chatText"]))
            self.timeline.append({"type": "text", "text": js_trim(collected["chatText"])})
        self.stray += collected["strayMarkup"]
        self._final_messages = [self._reply_of(out)]
        await self.finish({
            "failedUpdate": failure,
            "exhaustedCorrective": self.can_continue,
            "unretriableFailedUpdate": (not self.can_continue) and failure in ("malformed", "claimed"),
            "endReason": "cancelled" if self.cancelled else "protocol_failure",
        })

    async def _after_execute(self, out: StepOutput, ran: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> None:
        if self.finished:
            return
        executed = [{"kind": self._kind_of(inv), "result": r} for inv, r in zip(ran, results)]
        wrote_now = any(e["kind"] == "write" and e["result"]["ok"] for e in executed)
        if wrote_now:
            self.wrote = True
            # Failures in a row, not per turn (run.ts afterExecute).
            self.corrective_used = 0
        for e in executed:
            self.trace.append(e["result"]["trace"])
            self.timeline.append({"type": "tool", "line": e["result"]["trace"], "ok": e["result"]["ok"]})
        self.step_log.append({
            "calls": [{"name": inv["name"], "arguments": arguments_text_of(inv)} for inv in ran],
            "signature": "\n".join(sorted(call_signature(inv["name"], inv.get("args"), inv.get("argumentsText")) for inv in ran)),
            "names": list(dict.fromkeys(inv["name"] for inv in ran)),
            "results": [r["content"] for r in results], "wrote": wrote_now,
        })
        reasoning = int((out.usage or {}).get("reasoningTokens") or 0)
        if self.long_reasoning_tokens > 0 and not wrote_now and reasoning > self.long_reasoning_tokens:
            self.long_reasoning_due = {"atStep": self.steps_taken + 1, "tokens": reasoning}
        if wrote_now:
            self.html_read_pending = None
        for inv, r in zip(ran, results):
            if inv["name"] in ("read", "read_chapter") and (inv.get("args") or {}).get("format") == "html" and r["ok"]:
                self.html_read_pending = r["trace"]

        decision = decide_after_step({"executed": executed, "stepsTaken": self.steps_taken, "correctiveUsed": self.corrective_used,
                                      "budgets": self.budgets, "policy": self.policy})
        continuing = decision["action"] == "continue" and self.can_continue and not self.cancelled
        handed_back = continuing and decision.get("corrective")
        for e in executed:
            eff = e["result"].get("effects")
            if not eff:
                continue
            if eff.get("canvasIssue") and not self.effects["canvasIssue"]:
                self.effects["canvasIssue"] = eff["canvasIssue"]
            self.effects["reinsertedImages"] += eff.get("reinsertedImages") or 0
            self.effects["selectionGone"] = self.effects["selectionGone"] or bool(eff.get("selectionGone"))
            if handed_back and e["result"].get("retryable", not e["result"]["ok"]):
                continue
            self.effects["failedEdits"] += eff.get("failedEdits") or 0
            self.effects["producedNothing"] = self.effects["producedNothing"] or bool(eff.get("producedNothing"))
        # A cut-off rewrite the model then did in parts is not a cut-off turn (see run.ts).
        if self.effects["canvasIssue"] and any(c["kind"] == "write" and c["result"]["ok"] and not (c["result"].get("effects") or {}).get("canvasIssue") for c in executed):
            self.effects["canvasIssue"] = None

        # The model asked the user something: the results are appended so the
        # call is answered, then the run waits for the answer (resume_with_answer).
        if self.ctx.run.question and not self.cancelled:
            self.last_results_start = len(self.messages)
            self.messages = [*self.messages, *self._result_messages(out, ran, results)]
            self._next = {"messages": self.messages, "step": self.steps_taken, "final": steps_left(self.budgets, self.steps_taken) == 1}
            self.paused = {"reason": "question", **self.ctx.run.question}
            await _maybe_await(self.observer.on_asked(self.ctx.run.question, self.progress()))
            return

        # A reply with no action while the plan has work left, or right after
        # an HTML read that no edit followed: the model is reminded and
        # continues (a bounded number of times).
        unfinished = unfinished_plan_items(self.ctx.run.plan)
        may_nudge = (decision["action"] == "end" and decision["reason"] == "answered" and self.can_continue and not self.cancelled
                     and steps_left(self.budgets, self.steps_taken) > 0)
        nudge = None
        if may_nudge and unfinished and self.plan_nudges < PLAN_NUDGE_BUDGET:
            self.plan_nudges += 1
            nudge = plan_unfinished_nudge(self.ctx.run.plan)
        elif may_nudge and self.html_read_pending and not self.html_read_nudged:
            self.html_read_nudged = True
            nudge = html_read_nudge(self.html_read_pending)
        elif may_nudge and not self.wrote and not self.claim_nudged and claims_own_write(out.text):
            # A claim of having written, with nothing written this run: the editor's facts, once.
            self.claim_nudged = True
            nudge = unbacked_claim_nudge({"writes": writes_so_far(self.ctx.run), "reads": len(self.ctx.run.read_ids), "planLeft": len(unfinished)})
        # A message the user sent meanwhile rides with the nudge, or on its own
        # keeps a turn going that would have ended with this reply.
        steer = self._take_steer() if (self.can_continue and not self.cancelled and steps_left(self.budgets, self.steps_taken) > 0) else None
        may_steer = bool(steer) and decision["action"] == "end" and decision["reason"] in ("answered", "writes_done")
        if nudge or may_steer:
            reply: Dict[str, Any] = {"role": "assistant", "content": out.text}
            if out.response_items:
                reply["responseItems"] = out.response_items
            content = "\n\n".join(p for p in (wrap_reminder(nudge) if nudge else "", steer or "") if p)
            self.messages = [*self.messages, reply, {"role": "user", "content": content}]
            self._next = {"messages": self.messages, "step": self.steps_taken, "final": steps_left(self.budgets, self.steps_taken) == 1}
            await _maybe_await(self.observer.on_step_executed(self.progress()))
            return

        if continuing:
            if decision.get("corrective"):
                self.corrective_used += 1
            self.last_results_start = len(self.messages)
            self.messages = [*self.messages, *append_reminders(self._result_messages(out, ran, results), self._collect_reminders())]
            if steer:
                self.messages = [*self.messages, {"role": "user", "content": steer}]
            if decision.get("final"):
                self.messages = [*self.messages, {"role": "user", "content": STEP_LIMIT_NOTE}]
            self._compact_if_needed()
            # Before the observer: a snapshot taken there must know a step follows.
            self._next = {"messages": self.messages, "step": self.steps_taken, "final": decision["final"]}
            await _maybe_await(self.observer.on_step_executed(self.progress()))
            return

        # A last step that called tools ends with its calls answered, or the replay would leave them dangling.
        self._final_messages = self._result_messages(out, ran, results) if ran else [self._reply_of(out)]
        await self.finish({
            "failedUpdate": None, "exhaustedCorrective": False, "unretriableFailedUpdate": False,
            "endReason": "cancelled" if self.cancelled else (decision["reason"] if decision["action"] == "end" else "step_limit"),
        })

    def _compact_if_needed(self) -> None:
        """Keep the next step's prompt inside the window: past the threshold, the oldest read results become a note."""
        if self.prompt_token_limit <= 0 or not self.elidable:
            return
        plan = plan_elisions(self.messages, self.elidable, self.prompt_token_limit, self.last_results_start, self.measured)
        self.elidable = plan["remaining"]
        if not plan["elided"]:
            return
        self.messages = plan["messages"]
        line = elision_trace(plan["elided"], plan["resentTokens"])
        self.trace.append(line)
        self.timeline.append({"type": "tool", "line": line, "ok": True})

    def _collect_reminders(self) -> List[str]:
        """The automated context beside the next step's results (wc_text.reminders)."""
        out: List[str] = []
        run_len = self.identical_run_length()
        if run_len == REPEAT_NUDGE_STEPS:
            out.append(repeat_nudge(self.step_log[-1]["names"], run_len))
        if self.long_reasoning_due and self.long_reasoning_due["atStep"] == self.steps_taken:
            out.append(long_reasoning_reminder(self.long_reasoning_due["tokens"]))
            self.long_reasoning_due = None
        if unfinished_plan_items(self.ctx.run.plan):
            out.append(plan_reminder(self.ctx.run.plan))
        lookups = self.lookup_streak()
        if not self.lookup_nudged and lookups >= LOOKUP_NUDGE_STEPS:
            self.lookup_nudged = True
            out.append(lookup_streak_nudge(lookups, self.ctx.analyze is not None))
        if self.host_reminders is not None:
            out.extend(self.host_reminders(self))
        return out

    def lookup_streak(self) -> int:
        """Steps in a row, ending with the last, that only called look-up tools and wrote nothing."""
        n = 0
        for step in reversed(self.step_log):
            names = step.get("names") or []
            if step.get("wrote") or not names or any(self._kind_of({"name": name}) != "read" for name in names):
                break
            n += 1
        return n

    def identical_run_length(self) -> int:
        """Steps in a row, ending with the last, that made the same calls and wrote nothing (since the last resume)."""
        log = self.step_log[self.guard_floor:]
        if not log or not log[-1].get("signature") or log[-1]["wrote"]:
            return 0
        n = 0
        for step in reversed(log):
            if step.get("signature") != log[-1]["signature"] or step["wrote"]:
                break
            n += 1
        return n

    def _result_messages(self, out: StepOutput, ran: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        native = [(inv, r) for inv, r in zip(ran, results) if inv["source"] == "native"]
        markup = [(inv, r) for inv, r in zip(ran, results) if inv["source"] == "markup"]
        reply: Dict[str, Any] = {"role": "assistant", "content": out.text}
        if native:
            calls = []
            for inv, _ in native:
                call = {"id": inv["id"], "name": inv["name"], "argumentsText": arguments_text_of(inv)}
                if inv.get("signature"):
                    call["signature"] = inv["signature"]
                calls.append(call)
            reply["toolCalls"] = calls
        if out.thinking:
            reply["thinking"] = out.thinking
        if out.response_items:
            reply["responseItems"] = out.response_items
        messages = [reply]
        for inv, r in native:
            if r["ok"] and self._kind_of(inv) == "read":
                self.elidable.append({"index": self.last_results_start + len(messages), "trace": r["trace"]})
            message = {"role": "tool", "toolCallId": inv["id"], "name": inv["name"], "content": r["content"]}
            if r.get("images"):
                # view_images: the pictures travel on the result, each provider rendering them its own way.
                message["images"] = list(r["images"])
            messages.append(message)
        lost = (f"- NOT APPLIED: {self.step_dropped} document block(s) in your reply did not reach the document — a full rewrite beside <edit> blocks for the same chapter, "
                "a rewrite beside a selection rewrite, or markup that could not be read. Write what you still want changed in a reply of its own."
                if self.step_dropped > 0 else "")
        if markup or lost:
            lines = [f"- {inv['name']}: {r['content']}" for inv, r in markup] + ([lost] if lost else [])
            messages.append({"role": "user", "content": "RESULT OF YOUR DOCUMENT CHANGES:\n" + "\n".join(lines)})
        return messages

    # ── state ────────────────────────────────────────────────────────────────
    def progress(self) -> Dict[str, Any]:
        return {
            "chatText": "\n\n".join(self.chat_texts), "timeline": list(self.timeline), "steps": self.steps_taken,
            "trace": list(self.trace), "touched": list(self.ctx.run.touched.values()), "seen": seen_chapters(self.ctx.run),
            "readIds": list(self.ctx.run.read_ids), "usage": dict(self.usage), "plan": list(self.ctx.run.plan),
        }

    async def finish(self, end: Dict[str, Any]) -> None:
        if self.finished:
            return
        self.finished = True
        self._next = None
        if end["endReason"] != "cancelled":
            self._drop_empty_created()
        summary = {"strayMarkup": self.stray, "effects": dict(self.effects), **self.progress(), "question": None, **end,
                   "transcript": self.transcript(), "tokenRatio": self.token_ratio()}
        await _maybe_await(self.observer.on_finish(summary))

    def token_ratio(self) -> Optional[float]:
        """measured ÷ estimated for the last step the provider counted, unclamped (run.ts tokenRatio)."""
        if not self.measured or not self.measured.get("tokens", 0) > 0:
            return None
        estimated = prompt_tokens(self.messages[: self.measured["length"]])
        return self.measured["tokens"] / estimated if estimated > 0 else None

    def transcript(self) -> List[Dict[str, Any]]:
        """What this turn added after the prefix it shared with the conversation — its final user message as
        sent, every step's messages as last sent, and the step that ended it — for the next turn to replay
        verbatim (cache_continuity.md §3.1)."""
        return [dict(m) for m in [*self.messages[max(0, self.initial_count - 1):], *self._final_messages]]

    @staticmethod
    def _reply_of(out: StepOutput) -> Dict[str, Any]:
        reply: Dict[str, Any] = {"role": "assistant", "content": out.text}
        if out.thinking:
            reply["thinking"] = out.thinking
        if out.response_items:
            reply["responseItems"] = out.response_items
        return reply

    def _drop_empty_created(self) -> None:
        run, document = self.ctx.run, self.ctx.document
        for chapter in list(document.chapters()):
            if chapter["id"] not in run.created or not is_blank_content(chapter["content"]):
                continue
            document.remove(chapter["id"])
            run.created.discard(chapter["id"])
            run.touched.pop(chapter["id"], None)

    def snapshot(self) -> Dict[str, Any]:
        """Everything a restart needs to continue from the last completed step."""
        run = self.ctx.run
        return {
            "messages": self.messages, "initialCount": self.initial_count, "stepsTaken": self.steps_taken, "correctiveUsed": self.corrective_used,
            "chatTexts": self.chat_texts, "timeline": self.timeline, "trace": self.trace, "stray": self.stray, "wrote": self.wrote,
            "effects": self.effects, "usage": self.usage, "stepLog": self.step_log[-6:], "guardFloor": max(0, self.guard_floor - (len(self.step_log) - len(self.step_log[-6:]))),
            "next": self._next is not None,
            "final": bool(self._next and self._next["final"]),
            "run": {
                "seen": seen_chapters(run), "created": sorted(run.created), "snapshotted": sorted(run.snapshotted),
                "readIds": list(run.read_ids), "reads": run.reads, "touched": list(run.touched.values()),
                "startOutline": run.start_outline, "lastList": run.last_list,
                "selectionAttempted": run.selection_attempted, "selectionApplied": run.selection_applied,
                "plan": list(run.plan), "question": run.question, "textSeen": dict(run.text_seen), "attachmentChars": run.attachment_chars,
                "rewriteLossWarned": sorted(run.rewrite_loss_warned), "userUrls": list(run.user_urls), "webSaved": sorted(run.web_saved),
            },
            "planNudges": self.plan_nudges, "longReasoningDue": self.long_reasoning_due,
            "htmlReadPending": self.html_read_pending, "htmlReadNudged": self.html_read_nudged, "planBaseline": run.plan_baseline,
            "claimNudged": self.claim_nudged, "pendingSteers": list(self.pending_steers), "pendingSteerIds": list(self.pending_steer_ids), "lookupNudged": self.lookup_nudged,
            "elidable": list(self.elidable), "lastResultsStart": self.last_results_start, "measured": self.measured,
        }

    def restore(self, snap: Dict[str, Any], stored: Callable[[str], Optional[str]]) -> None:
        from .types import restore_seen
        self.messages = list(snap["messages"])
        self.initial_count = int(snap.get("initialCount") or self.initial_count)
        self.steps_taken = snap["stepsTaken"]
        self.corrective_used = snap["correctiveUsed"]
        self.chat_texts = list(snap["chatTexts"])
        self.timeline = list(snap["timeline"])
        self.trace = list(snap["trace"])
        self.stray = snap["stray"]
        self.wrote = snap["wrote"]
        self.effects = dict(snap["effects"])
        self.usage = dict(snap.get("usage") or self.usage)
        self.step_log = list(snap.get("stepLog") or [])
        self.guard_floor = int(snap.get("guardFloor") or 0)
        run = self.ctx.run
        restore_seen(run, snap["run"]["seen"], stored)
        run.created = set(snap["run"]["created"])
        run.snapshotted = set(snap["run"]["snapshotted"])
        run.read_ids = dict.fromkeys(snap["run"]["readIds"])
        run.reads = dict(snap["run"]["reads"])
        run.touched = {t["documentId"]: t for t in snap["run"]["touched"]}
        run.start_outline = snap["run"]["startOutline"]
        run.last_list = snap["run"]["lastList"]
        run.selection_attempted = snap["run"]["selectionAttempted"]
        run.selection_applied = snap["run"]["selectionApplied"]
        run.plan = list(snap["run"].get("plan") or [])
        run.question = snap["run"].get("question")
        run.text_seen = dict(snap["run"].get("textSeen") or {})
        run.user_urls = list(snap["run"].get("userUrls") or [])
        run.web_saved = set(snap["run"].get("webSaved") or [])
        run.rewrite_loss_warned = set(snap["run"].get("rewriteLossWarned") or [])
        run.attachment_chars = int(snap["run"].get("attachmentChars") or 0)
        self.plan_nudges = int(snap.get("planNudges") or 0)
        self.long_reasoning_due = snap.get("longReasoningDue")
        self.html_read_pending = snap.get("htmlReadPending")
        self.html_read_nudged = bool(snap.get("htmlReadNudged"))
        run.plan_baseline = dict(snap.get("planBaseline") or {})
        self.claim_nudged = bool(snap.get("claimNudged"))
        self.pending_steers = list(snap.get("pendingSteers") or [])
        self.pending_steer_ids = list(snap.get("pendingSteerIds") or [])
        self.lookup_nudged = bool(snap.get("lookupNudged"))
        self.elidable = list(snap.get("elidable") or [])
        self.last_results_start = int(snap.get("lastResultsStart") or 0)
        self.measured = snap.get("measured")
        if snap.get("next"):
            self._next = {"messages": self.messages, "step": self.steps_taken, "final": snap.get("final", False)}

"""Server-side resumable LLM generation jobs (docs/features/resumable_generation.md).

Generation normally runs inside the browser tab (`src/services/llm.ts`), so a
tab that is discarded mid-stream loses everything. This module moves the
provider connection into the backend: a job owns the provider stream, buffers
every delta, and any number of SSE readers may attach or re-attach at an
arbitrary character offset.

Exposes:
- `JobRegistry` / `registry` — the in-memory, per-username job store.
- `GenerationJob` — buffer + status + subscriber fan-out for one generation.
- `run_job` — one provider call with retries, classified failures, the journal.
- `router` — an APIRouter with the four /api/generate endpoints, included
  into the app by api_server.
The providers' wire formats — the request builders and stream readers — live
in `wc_providers/`, one module per API family (2026-10-11); `_dispatch_provider`
is its `dispatch`, bound here under the name the run tests replace.

Provider parity note: the request shapes, delta extraction and usage
accounting below are a direct port of `src/services/llm.ts`. When that file
changes, this one must change with it — in particular the Anthropic usage
rules and the Gemini safety-block detection, which carry their own comments.
That includes tool calling (docs/features/agentic_chat_loop.md): tool
definitions arrive OpenAI-shaped in `config["tools"]` and are translated per
provider through `wc_text.document_tools` (the parity-checked port of
src/utils/documentTools.ts); history messages may carry
`toolCalls` (assistant) or be `role: "tool"` results, and every builder
replays them in its provider's native shape; every stream reader reports
tool-call deltas through `GenerationJob.note_tool_call`.

Reasoning artifacts are part of that replay (`providerMessages.ts` header):
an assistant message may carry `thinking` (Anthropic thinking /
redacted_thinking blocks, replayed verbatim BEFORE its text and tool_use
blocks) and a toolCalls entry may carry `signature` (Gemini thoughtSignature,
replayed as a sibling of that call's `functionCall`). The stream readers
capture both: completed thinking blocks become `thinking_block` events
(`GenerationJob.note_thinking_block`), and a call's signature rides on its
`tool_call` event. Both are kept on the job so a late reader gets them too.

Tests that stub the network patch `wc_providers.common._http_stream` (the
module that owns it) and may clear `server_generation.registry`.
"""

import asyncio
import json
import logging
import os
import random
import re
from urllib.parse import urlparse
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from server_auth import get_authenticated_username
# The parity-checked ports of src/utils/documentTools.ts: this module used to keep
# its own copies, which no parity case covered (test audit, 2026-10-10).
from wc_text.provider_profile import uses_responses_api  # noqa: F401 — reached through this module by tests
# The providers' wire formats live in wc_providers (one module per API family); these
# names are re-exported for the callers and tests that reach them through this module.
from wc_providers import dispatch as _dispatch_provider  # noqa: F401 — patched by the run tests
from wc_providers.anthropic import build_anthropic_request  # noqa: F401
from wc_providers.common import (HTTP_READ_TIMEOUT, IDLE_TIMEOUT_SECONDS, ProviderError, _JobAborted,  # noqa: F401
                                 _check_abort, _http_stream, describe_prefix, mask_request_details)
from wc_providers.gemini import build_gemini_request  # noqa: F401
from wc_providers.openai_compat import build_openai_request  # noqa: F401
from wc_providers.responses import _stream_responses, build_responses_request  # noqa: F401
from wc_text.retry_policy import (MAX_RETRY_DELAY_MS, MAX_TRANSPORT_RETRIES, is_context_length_error, is_retryable_status, retry_delay_ms, with_jitter)

logger = logging.getLogger("web_canvas.generation")

router = APIRouter()

# ── Retention and limits (spec §4) ────────────────────────────────────────────
# Problem: a run that looped (grok announced a chapter and called
#   list_chapters 13 times, 2026-10-06) could not be explained afterwards. The
#   83 s of reasoning in which it chose that call existed only in the job's
#   live stream, and a finished job is forgotten after ten minutes.
# Fix: every finished step is appended to a local journal: its reasoning,
#   its visible text and its tool calls. One JSON line per step, one file per
#   day, kept JOURNAL_KEEP_DAYS days, beside the metadata DB (local disk,
#   git-ignored, like the stories themselves).
JOURNAL_DIR = os.path.join(os.environ.get("WEB_CANVAS_DB_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".local_db"),
                           "step-journal")
JOURNAL_KEEP_DAYS = 7
JOURNAL_MAX_CHARS = 200_000
_journal_pruned_on: Optional[str] = None


def journal_step(job: "GenerationJob") -> None:
    """Append a finished job to the step journal. Never raises."""
    global _journal_pruned_on
    try:
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        if _journal_pruned_on != today:
            _journal_pruned_on = today
            cutoff = time.time() - JOURNAL_KEEP_DAYS * 86400
            for name in os.listdir(JOURNAL_DIR):
                path = os.path.join(JOURNAL_DIR, name)
                if name.endswith(".jsonl") and os.path.getmtime(path) < cutoff:
                    os.remove(path)
        record = {
            "at": _now_iso(),
            "job": job.job_id,
            "kind": job.meta.get("kind"),
            "message": job.meta.get("assistantMessageId"),
            "conversation": job.conversation,
            "model": job.model,
            "status": job.status,
            "firstTokenSeconds": round(job.first_delta_latency, 2) if job.first_delta_latency is not None else None,
            "reasoning": job.reasoning_text,
            "text": job.buffer[:JOURNAL_MAX_CHARS],
            "toolCalls": [
                {"name": call.get("name"), "arguments": (call.get("arguments") or "")[:JOURNAL_MAX_CHARS]}
                for _, call in sorted(job.tool_calls.items())
            ],
            "error": job.error,
        }
        with open(os.path.join(JOURNAL_DIR, f"{today}.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # the journal must never fail a job
        logger.warning("Step journal write failed for %s: %s", job.job_id, exc)


FINISHED_JOB_TTL_SECONDS = 10 * 60
# Problem: every finished job expired after ten minutes, read or not. A turn
#   whose only reader was gone when it finished (phone locked, tab discarded,
#   laptop closed) was deleted before anyone could come back for it — the
#   tokens were spent and the reply no longer existed anywhere. With first
#   tokens measured at 230s, "gone for ten minutes" is an ordinary turn.
# Root Cause: retention was keyed on WHEN the job finished, not on whether its
#   result had reached anybody.
# Fix: a result no reader has received the terminal event for is kept a day.
#   Memory stays bounded by the per-user cap below, exactly as before — the
#   cap counts jobs, not minutes — and delivered results still go in ten.
UNDELIVERED_JOB_TTL_SECONDS = 24 * 60 * 60
MAX_JOBS_PER_USER = 20
# The offset contract is expressed in characters (the client resumes from the
# number of characters it has rendered), so the buffer cap is measured in
# characters too — mixing in a byte cap would make offsets ambiguous.
MAX_BUFFER_CHARS = 4 * 1024 * 1024

# Idle SSE keep-alive. Not a poll: readers are woken by their queue, this only
# emits a comment line so intermediaries do not drop an idle connection.
SSE_HEARTBEAT_SECONDS = 15.0

#: Event types that END a stream. Everything else is informational and must
#: NOT close it — see the reader loop in _job_event_stream.
TERMINAL_EVENT_TYPES = frozenset({"done", "error", "aborted"})

SUPPORTED_PROVIDERS = ("openai", "ollama", "runpod", "grok", "gemini", "anthropic", "moonshot")

#: Hosts /api/models will query. Local model servers only — see the endpoint.
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: Additionally allowed for /api/models, over HTTPS only: RunPod's per-pod HTTP
#: proxy. A pod reached through the SSH tunnel is already loopback and needs
#: nothing here; this is for addressing a pod directly.
#:
#: Widening an SSRF guard deserves a reason. This suffix resolves to RunPod's
#: public proxy tier, never to anything on this machine's network, so it does
#: not buy an attacker reach they did not already have from the open internet.
#: The endpoint's whole behaviour is one GET of {base}/models whose parsed
#: names are returned, and reaching it already requires an authenticated
#: session. Matching is on a leading-dot suffix so a lookalike registration
#: like "evilproxy.runpod.net.attacker.com" cannot satisfy it.
REMOTE_MODEL_HOST_SUFFIXES = (".proxy.runpod.net",)


#: The non-local hosts /api/models will query, with the user's key (the official model lists).
OFFICIAL_MODEL_HOSTS = {"anthropic": ("api.anthropic.com",), "openai": ("api.openai.com",), "gemini": ("generativelanguage.googleapis.com",),
                        "moonshot": ("api.moonshot.ai", "api.moonshot.cn"), "grok": ("api.x.ai",)}
_OPENAI_CHAT_MODEL_RE = re.compile(r"^(?:gpt-\d|o\d)")
_GEMINI_TEXT_MODEL_RE = re.compile(r"^gemini-(?:[3-9]|\d\d|flash-latest|flash-lite-latest|pro-latest)")
_GEMINI_NON_TEXT_RE = re.compile(r"tts|image|transcribe|robotics|computer-use|omni|customtools|banana")
_OPENAI_NON_CHAT_RE = re.compile(r"audio|realtime|image|transcribe|tts|search|instruct|live|embedding|diarize|whisper|translate")


def is_queryable_model_host(base_url: str) -> bool:
    """Whether /api/models may fetch a listing from this URL."""
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        return False
    if host in LOCAL_HOSTNAMES:
        return True
    # Remote hosts are HTTPS-only: the listing would otherwise cross the
    # public internet in the clear.
    return parsed.scheme == "https" and any(
        host.endswith(suffix) for suffix in REMOTE_MODEL_HOST_SUFFIXES
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _monotonic() -> float:
    """Wall-clock-independent clock used for retention (patchable in tests)."""
    return time.monotonic()


# ══════════════════════════════════════════════════════════════════════════════
# Job model
# ══════════════════════════════════════════════════════════════════════════════

class GenerationJob:
    """One provider generation: a growing buffer plus subscriber fan-out.

    The buffer is the source of truth for replay; `length` is the total number
    of characters produced and is what every event's `offset` refers to. The
    two only diverge once the buffer cap is hit (`truncated_buffer`).
    """

    def __init__(self, job_id: str, username: str, meta: Optional[Dict[str, Any]] = None):
        self.job_id = job_id
        self.username = username
        self.meta: Dict[str, Any] = meta if isinstance(meta, dict) else {}
        self.status = "running"  # running | done | error | aborted
        self.buffer = ""
        self.length = 0
        self.truncated_buffer = False
        self.usage: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.created_at = _now_iso()
        self.updated_at = self.created_at
        # Time-to-first-token, measured from job creation. The only number that
        # separates provider prefill (large contexts cost seconds before the
        # first byte) from latency this app added; logged once per job.
        self.created_monotonic = _monotonic()
        self.first_delta_latency: Optional[float] = None
        self.finished_at: Optional[float] = None  # monotonic, drives retention
        #: True once some reader has been sent the terminal event. Until then
        #: the result exists nowhere but here (see UNDELIVERED_JOB_TTL_SECONDS).
        self.result_delivered = False
        self.abort_requested = False
        self.task: Optional[asyncio.Task] = None
        #: Prompt size in characters, for reading the latency line in context.
        self.input_chars = 0
        #: Tool calls assembled so far, by index. Kept OUT of `buffer` so
        #: replay offsets stay tied to document text alone — but kept, because
        #: a reconnect that cannot see them loses the whole edit.
        self.tool_calls: Dict[int, Dict[str, Any]] = {}
        #: Completed Anthropic reasoning blocks, in stream order. Kept for the
        #: same reason as tool_calls: the next step must replay them verbatim,
        #: and a reader that attaches after they streamed has no other source.
        self.thinking_blocks: List[Dict[str, Any]] = []
        #: grok (xAI Responses API): this step's output items, verbatim and in
        #: order — reasoning (with its encrypted_content), message,
        #: function_call. The next step sends them back unchanged so the model
        #: keeps its reasoning (see _stream_responses).
        self.response_items: List[Dict[str, Any]] = []
        #: Reasoning the model streamed before (or between) visible tokens.
        #: Counted, never buffered — it is not document text and must not move
        #: the replay offsets.
        self.reasoning_chars = 0
        #: The reasoning text itself, for the step journal only (capped).
        self.reasoning_text = ""
        #: Set at start, for the step journal: the cache conversation and model.
        self.conversation: Optional[str] = None
        self.model: Optional[str] = None
        self.first_reasoning_latency: Optional[float] = None
        #: Why the job failed, for the run engine (agentic_chat_loop.md §0.10):
        #: transient | idle | repetition | context | fatal. None while running or done.
        self.error_kind: Optional[str] = None
        #: The job had streamed text or a tool call when it failed.
        self.failed_after_output = False
        #: xAI repetition-detector triggers seen (cumulative), with `loopCheck` on.
        self.loop_triggers: List[str] = []
        #: Attempts that failed before any output and were sent again.
        self.retries = 0
        # One queue per attached SSE reader. Everything here runs on the single
        # event loop, so plain set mutation is safe without a lock.
        self.subscribers: set = set()

    # ── mutation ──────────────────────────────────────────────────────────────
    def append(self, text: str) -> None:
        """Buffer a provider delta and wake every attached reader."""
        if not text or self.status != "running":
            return
        if self.first_delta_latency is None:
            self.first_delta_latency = _monotonic() - self.created_monotonic
            logger.info(
                "Job %s first token after %.2fs (%s chars of input)",
                self.job_id, self.first_delta_latency, self.input_chars,
            )
        self.length += len(text)
        remaining = MAX_BUFFER_CHARS - len(self.buffer)
        if remaining > 0:
            self.buffer += text[:remaining]
        if len(self.buffer) >= MAX_BUFFER_CHARS and not self.truncated_buffer:
            # Past the cap the job keeps streaming to live subscribers but the
            # buffer stops growing, so a late reader cannot replay the tail.
            self.truncated_buffer = True
        self.updated_at = _now_iso()
        self._publish({"type": "delta", "text": text, "offset": self.length})

    def note_tool_call(
        self,
        index: int,
        call_id: Optional[str],
        name: Optional[str],
        arguments: str,
        signature: Optional[str] = None,
    ) -> None:
        """Forward a tool-call argument delta live.

        Not buffered, for the same reason reasoning is not: it is not document
        text, and putting it in the buffer would shift every replay offset. A
        reconnect re-reads the whole call from the provider's final message
        instead.

        `signature` is Gemini's thoughtSignature for the call. Opaque and
        whole, so it is stored (last non-empty wins), never appended to; the
        key exists only once one arrived, so other providers' entries keep
        their exact shape.
        """
        if self.status != "running":
            return
        entry = self.tool_calls.setdefault(index, {"id": None, "name": None, "arguments": ""})
        if call_id:
            entry["id"] = call_id
        if name:
            entry["name"] = name
        entry["arguments"] += arguments or ""
        if signature:
            entry["signature"] = signature

        event: Dict[str, Any] = {
            "type": "tool_call",
            "index": index,
            "id": call_id,
            "name": name,
            "text": arguments or "",
        }
        if signature:
            event["signature"] = signature
        self._publish(event)

    def note_thinking_block(self, block: Dict[str, Any]) -> None:
        """Keep one COMPLETED Anthropic reasoning block and pass it on.

        Informational (never in TERMINAL_EVENT_TYPES) and never buffered. The
        event carries the block's position so a reader that reconnects
        mid-turn — and is replayed every block again — can skip the ones it
        already has: a duplicated block makes Anthropic reject the replay.
        """
        if self.status != "running" or not isinstance(block, dict):
            return
        index = len(self.thinking_blocks)
        self.thinking_blocks.append(block)
        self._publish({"type": "thinking_block", "index": index, "block": block})

    def note_response_item(self, item: Dict[str, Any]) -> None:
        """Keep one COMPLETED xAI Responses output item and pass it on.

        Same rules as thinking blocks: informational, never buffered, and the
        event carries the item's position so a reconnecting reader skips what
        it already has — a duplicated reasoning item would be replayed twice.
        """
        if self.status != "running" or not isinstance(item, dict):
            return
        index = len(self.response_items)
        self.response_items.append(item)
        self._publish({"type": "response_item", "index": index, "item": item})

    def replace_tool_call_arguments(self, index: int, arguments: str) -> None:
        """The provider's final arguments for a call, when the deltas disagree."""
        entry = self.tool_calls.get(index)
        if self.status != "running" or entry is None or entry.get("arguments") == arguments:
            return
        entry["arguments"] = arguments
        self._publish({
            "type": "tool_call", "index": index, "id": entry.get("id"), "name": entry.get("name"),
            "text": arguments, "replay": True,
        })

    def note_reasoning(self, text: str) -> None:
        """Record a reasoning delta and pass it on live.

        A model can spend a minute reasoning before its first visible token
        (grok-4.6, measured). Without this the server sees that as silence and
        so does the user.
        """
        if not text or self.status != "running":
            return
        if self.first_reasoning_latency is None:
            self.first_reasoning_latency = _monotonic() - self.created_monotonic
            logger.info(
                "Job %s started reasoning after %.2fs",
                self.job_id, self.first_reasoning_latency,
            )
        self.reasoning_chars += len(text)
        if len(self.reasoning_text) < JOURNAL_MAX_CHARS:
            self.reasoning_text += text[: JOURNAL_MAX_CHARS - len(self.reasoning_text)]
        self._publish({"type": "reasoning", "text": text})

    def note_loop_triggers(self, triggers: Any) -> None:
        """Record xAI repetition-detector triggers (each report is cumulative)."""
        if not isinstance(triggers, list):
            return
        new = [str(t) for t in triggers if isinstance(t, str) and t not in self.loop_triggers]
        if new:
            self.loop_triggers.extend(new)
            logger.info("Job %s: repetition detector fired: %s", self.job_id, ", ".join(new))

    @property
    def has_output(self) -> bool:
        """Text or a tool call has streamed: sending the request again would duplicate it."""
        return self.length > 0 or bool(self.tool_calls)

    def reset_for_retry(self) -> None:
        """Forget what a failed attempt that produced nothing left behind."""
        self.tool_calls = {}
        self.thinking_blocks = []
        self.response_items = []
        self.loop_triggers = []

    def finish(
        self,
        status: str,
        usage: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Move the job to a terminal state. First terminal state wins."""
        if self.status != "running":
            return False
        self.status = status
        self.usage = usage
        self.error = error
        self.updated_at = _now_iso()
        # Prompt-cache result belongs in the same line as the latency it
        # explains. A turn that lost its cached prefix is otherwise
        # indistinguishable from a slow model: same log, ten times the wait.
        prompt_tokens = (usage or {}).get("promptTokens") or 0
        cached_tokens = (usage or {}).get("cachedPromptTokens")
        if cached_tokens is not None and prompt_tokens:
            cache_note = f"cache {cached_tokens}/{prompt_tokens} ({100 * cached_tokens / prompt_tokens:.0f}%)"
            written = (usage or {}).get("cacheWriteTokens") or 0
            if written:
                cache_note += f", wrote {written}"
        elif prompt_tokens:
            cache_note = f"cache n/a, {prompt_tokens} prompt tokens"
        else:
            cache_note = "cache n/a"
        logger.info(
            "Job %s %s: %s chars in, first token %s, reasoning %s chars, output %s chars, %s",
            self.job_id,
            status,
            self.input_chars,
            f"{self.first_delta_latency:.2f}s" if self.first_delta_latency is not None else "never",
            self.reasoning_chars,
            self.length,
            cache_note,
        )
        self.finished_at = _monotonic()
        journal_step(self)
        self._publish(self.terminal_event())
        return True

    def abort(self) -> bool:
        """Cancel the provider request; the partial buffer stays readable."""
        if self.status != "running":
            return False
        self.abort_requested = True
        # Whoever stopped the turn has already dropped their reader, so the
        # terminal event usually reaches nobody — but this result was thrown
        # away on purpose and must not be kept a day as "unread".
        self.result_delivered = True
        # Flip the status synchronously so the endpoint's response and the
        # terminal SSE event cannot race the task's own cancellation handling.
        self.finish("aborted")
        if self.task is not None:
            self.task.cancel()
        return True

    # ── reads ────────────────────────────────────────────────────────────────
    def terminal_event(self) -> Dict[str, Any]:
        if self.status == "error":
            return {
                "type": "error",
                "message": self.error or "Generation failed.",
                "offset": self.length,
                "truncatedBuffer": self.truncated_buffer,
            }
        return {
            "type": "done",
            "offset": self.length,
            "usage": self.usage,
            "status": self.status,
            "truncatedBuffer": self.truncated_buffer,
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "jobId": self.job_id,
            "status": self.status,
            "meta": self.meta,
            "length": self.length,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "truncatedBuffer": self.truncated_buffer,
            "delivered": self.result_delivered,
        }

    # ── internals ────────────────────────────────────────────────────────────
    def _publish(self, event: Dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(event)


class JobRegistry:
    """In-memory job store, keyed by job id and scoped by username."""

    def __init__(self):
        self._jobs: Dict[str, GenerationJob] = {}

    def create(self, username: str, meta: Optional[Dict[str, Any]] = None) -> GenerationJob:
        self.prune()
        job = GenerationJob(f"gen-{secrets.token_hex(8)}", username, meta)
        self._jobs[job.job_id] = job
        self._enforce_user_limit(username)
        return job

    def get(self, username: str, job_id: str) -> Optional[GenerationJob]:
        """Jobs are strictly per-user: another user's id looks nonexistent."""
        self.prune()
        job = self._jobs.get(job_id)
        if job is None or job.username != username:
            return None
        return job

    def list_for_user(self, username: str) -> List[GenerationJob]:
        self.prune()
        jobs = [j for j in self._jobs.values() if j.username == username]
        jobs.sort(key=lambda j: j.created_at)
        return jobs

    def prune(self) -> None:
        """Drop finished jobs past their retention window (delivered or not)."""
        now = _monotonic()
        for job_id, job in list(self._jobs.items()):
            if job.finished_at is None:
                continue
            ttl = FINISHED_JOB_TTL_SECONDS if job.result_delivered else UNDELIVERED_JOB_TTL_SECONDS
            if now - job.finished_at > ttl:
                del self._jobs[job_id]

    def clear(self) -> None:
        self._jobs.clear()

    def _enforce_user_limit(self, username: str) -> None:
        user_jobs = [j for j in self._jobs.values() if j.username == username]
        excess = len(user_jobs) - MAX_JOBS_PER_USER
        if excess <= 0:
            return
        # Delivered results go first (a copy exists on some client), then the
        # oldest unread one; a running job is never evicted, so a user with 20
        # live generations simply exceeds the cap for a while.
        finished = sorted(
            (j for j in user_jobs if j.finished_at is not None),
            key=lambda j: (not j.result_delivered, j.finished_at),
        )
        for job in finished[:excess]:
            self._jobs.pop(job.job_id, None)


registry = JobRegistry()


_REASONING_REJECTION_RE = re.compile(
    r"(reasoning_effort|thinking|reasoning)[^\n]{0,120}?"
    r"(unsupported|not supported|unknown|unrecognized|invalid|does not support)"
    r"|(unsupported|unknown|unrecognized|invalid)[^\n]{0,40}?(reasoning_effort|thinking)",
    re.IGNORECASE,
)


def _is_reasoning_effort_rejection(message: str) -> bool:
    """Did the provider refuse the request because of the effort parameter?

    The client's capability table is a best guess about other people's APIs
    (none of them expose it), so a wrong guess must cost one retry rather than
    the whole turn.
    """
    return bool(_REASONING_REJECTION_RE.search(message or ""))


def classify_failure(exc: BaseException, job: GenerationJob) -> str:
    """Why a job failed (agentic_chat_loop.md §0.10): transient | idle | repetition | context | fatal."""
    if job.loop_triggers:
        # The detector ends the generation (measured): whatever the error says, the loop is the cause.
        return "repetition"
    if isinstance(exc, httpx.ReadTimeout):
        return "idle"
    if isinstance(exc, httpx.TransportError):
        return "transient"
    if isinstance(exc, ProviderError):
        if exc.status is not None:
            if is_context_length_error(exc.status, str(exc)):
                return "context"
            return "transient" if is_retryable_status(exc.status, str(exc)) else "fatal"
        return "transient" if exc.transient else "fatal"
    return "fatal"


async def _dispatch_with_retries(job: GenerationJob, provider: str, config: Dict[str, Any], messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One provider call, sent again while it fails transiently before producing anything.

    Problem: a 429, a 503 or a dropped connection ended the turn, and on a
      server run it ended the run, with every finished step's progress.
    Fix: the same request is sent again (wc_text.retry_policy: Retry-After
      when given, else 1, 2, 4, 8 s with jitter, at most four more times) —
      only while nothing has streamed, so nothing is duplicated. A request
      that started streaming is the run engine's to redo (server_runs._step).
    """
    attempt = 0
    while True:
        try:
            return await _dispatch_provider(job, provider, config, messages)
        except (asyncio.CancelledError, _JobAborted):
            raise
        except Exception as exc:  # noqa: BLE001 — classified, then retried or re-raised
            kind = classify_failure(exc, job)
            retry_after = getattr(exc, "retry_after", None)
            # A provider that names a wait far past what a retry may sleep (a daily quota: Gemini said
            # "retryDelay": "10830s") will refuse every retry too: fail now, with its message.
            too_long = retry_after is not None and retry_after * 1000 > MAX_RETRY_DELAY_MS
            if kind != "transient" or too_long or job.has_output or attempt >= MAX_TRANSPORT_RETRIES or job.abort_requested:
                raise
            attempt += 1
            job.retries = attempt
            delay = with_jitter(retry_delay_ms(attempt, retry_after), random.random()) / 1000
            logger.info("Job %s: %s; retry %d/%d in %.1fs", job.job_id, str(exc)[:200] or type(exc).__name__, attempt, MAX_TRANSPORT_RETRIES, delay)
            job.reset_for_retry()
            job._publish({"type": "retry", "attempt": attempt, "max": MAX_TRANSPORT_RETRIES, "delayMs": int(delay * 1000),
                          "reason": (str(exc) or type(exc).__name__)[:200]})
            await asyncio.sleep(delay)
            _check_abort(job)


_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def scrub_surrogates(value: Any, counter: Optional[List[int]] = None) -> Any:
    """`value` with every lone surrogate replaced by U+FFFD, recursively.

    A browser that cut a string in the middle of an emoji sends half of it as
    "\\ud83d"; json.loads keeps it, and encoding the provider request as UTF-8
    then fails ("surrogates not allowed"), the whole step with it (2026-10-11).
    A valid pair is one code point by then, so any surrogate left is a stray.
    """
    if isinstance(value, str):
        if _LONE_SURROGATE_RE.search(value):
            if counter is not None:
                counter[0] += 1
            return _LONE_SURROGATE_RE.sub("\ufffd", value)
        return value
    if isinstance(value, list):
        return [scrub_surrogates(v, counter) for v in value]
    if isinstance(value, dict):
        return {k: scrub_surrogates(v, counter) for k, v in value.items()}
    return value


async def run_job(
    job: GenerationJob,
    provider: str,
    config: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> None:
    """Drive one provider stream to a terminal job state. Never raises."""
    strays = [0]
    messages = scrub_surrogates(messages, strays)
    if strays[0]:
        logger.warning("Job %s: %d string(s) in the request held a lone surrogate (a character cut in half); replaced", job.job_id, strays[0])
    try:
        try:
            usage = await _dispatch_with_retries(job, provider, config, messages)
        except ProviderError as exc:
            # A replayed reasoning item the service can no longer decrypt
            # (key rotation, model change) is a 400 for the whole turn: once
            # more without the items, so only the carried reasoning is lost.
            if job.length == 0 and "encrypted_content" in str(exc) and any(
                isinstance(m, dict) and m.get("responseItems") for m in messages
            ):
                logger.info("Job %s: provider rejected the replayed reasoning; retrying without it", job.job_id)
                stripped = [{k: v for k, v in m.items() if k != "responseItems"} if isinstance(m, dict) else m for m in messages]
                usage = await _dispatch_provider(job, provider, config, stripped)
                job.finish("done", usage=usage)
                return
            # Safe to retry only before any token was buffered — a parameter
            # rejection is a 400 at request time, so nothing has streamed.
            # "default" is the one setting that sends nothing; an unset one may
            # resolve to the app's default level (build_anthropic_request).
            if not (
                config.get("reasoningEffort") != "default"
                and job.length == 0
                and _is_reasoning_effort_rejection(str(exc))
            ):
                raise
            logger.info(
                "Job %s: provider rejected the reasoning effort; retrying without it",
                job.job_id,
            )
            retry_config = {**config, "reasoningEffort": "default"}
            usage = await _dispatch_provider(job, provider, retry_config, messages)
        job.finish("done", usage=usage)
    except (asyncio.CancelledError, _JobAborted):
        # Swallowed deliberately: cancellation here is the abort endpoint's
        # signal (it already flipped the status), and re-raising would only
        # surface as an unretrieved task exception on a job nobody awaits.
        job.finish("aborted")
    except Exception as exc:  # noqa: BLE001 — any provider failure becomes an error event
        message = str(exc) or type(exc).__name__ or "Unknown network error"
        job.error_kind = classify_failure(exc, job)
        job.failed_after_output = job.has_output
        if job.error_kind == "idle":
            message = f"The model sent nothing for {int(IDLE_TIMEOUT_SECONDS.get(provider, HTTP_READ_TIMEOUT))} s; the stream was given up."
        logger.warning("Generation job %s failed (%s%s): %s", job.job_id, job.error_kind,
                       ", after output" if job.failed_after_output else "", message)
        job.finish("error", error=message)


# ══════════════════════════════════════════════════════════════════════════════
# SSE
# ══════════════════════════════════════════════════════════════════════════════

def _sse(event: Dict[str, Any]) -> str:
    # Compact separators: one frame per delta, so the padding adds up.
    return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _job_event_stream(job: GenerationJob, from_offset: int):
    """Replay the buffer past `from_offset`, then stream live events.

    Every event carries the offset *after* it has been applied, so a client
    that reconnects with the last offset it rendered sees neither a duplicate
    nor a gap.
    """
    queue: asyncio.Queue = asyncio.Queue()
    # Subscribing BEFORE snapshotting the buffer is what makes the handover
    # exact: there is no await between the two statements, so no append can
    # slip in unseen — every queued event is strictly newer than the snapshot.
    job.subscribers.add(queue)
    snapshot = job.buffer
    snapshot_offset = job.length
    tool_calls_snapshot = {i: dict(c) for i, c in job.tool_calls.items()}
    thinking_snapshot = list(job.thinking_blocks)
    response_items_snapshot = list(job.response_items)
    terminal = job.terminal_event() if job.status != "running" else None

    try:
        # Flush the response headers immediately.
        #
        # Problem: the client saw NOTHING for 15s after attaching — measured on
        #   a real turn, twice, at exactly SSE_HEARTBEAT_SECONDS.
        # Root cause: the HTTP response headers are written with the first body
        #   chunk. On a job whose first token is slow (grok-4.6 took 40s+ on the
        #   same turn) the generator produced no bytes until the keep-alive
        #   fired, so the browser could not distinguish a live stream from a
        #   stalled connection, and neither could the UI.
        # Fix: one frame up front. It carries no text and advances no offset,
        #   so replay stays exact; unknown types are ignored by older clients.
        yield _sse({"type": "attached", "offset": from_offset, "status": job.status})

        # Completed reasoning blocks, every one, in order and ahead of the
        # calls they preceded: the next step replays them to Anthropic and a
        # missing one is a 400. Each keeps its position so a reconnecting
        # reader can skip what it already has.
        for index, block in enumerate(thinking_snapshot):
            yield _sse({"type": "thinking_block", "index": index, "block": block})
        # grok's output items (reasoning with its ciphertext), for the same
        # reason: the next step sends them back.
        for index, item in enumerate(response_items_snapshot):
            yield _sse({"type": "response_item", "index": index, "item": item})

        # Tool calls are replayed WHOLE, not by offset: they are not document
        # text, so there is no offset to resume from. A reader that reconnects
        # mid-call would otherwise see the tail of an argument it never saw the
        # start of — or, after a reload, nothing at all.
        for index, call in sorted(tool_calls_snapshot.items()):
            replay_event: Dict[str, Any] = {
                "type": "tool_call",
                "index": index,
                "id": call.get("id"),
                "name": call.get("name"),
                "text": call.get("arguments") or "",
                "replay": True,
            }
            if call.get("signature"):
                replay_event["signature"] = call["signature"]
            yield _sse(replay_event)

        sent_offset = from_offset
        start = max(0, min(from_offset, len(snapshot)))
        if start < len(snapshot):
            # With a truncated buffer the replay text is shorter than the
            # offset it advances to; that gap is exactly what truncatedBuffer
            # warns about, and resuming at snapshot_offset keeps the client
            # aligned with the live deltas that follow.
            yield _sse({"type": "delta", "text": snapshot[start:], "offset": snapshot_offset})
            sent_offset = snapshot_offset

        if terminal is not None:
            yield _sse(terminal)
            # Reached only once the frame was written: a reader that vanished
            # cancels this generator AT the yield, leaving the flag unset.
            job.result_delivered = True
            return

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue

            if event["type"] == "delta":
                if event["offset"] <= sent_offset:
                    continue  # already covered by the replay
                yield _sse(event)
                sent_offset = event["offset"]
            elif event["type"] in TERMINAL_EVENT_TYPES:
                yield _sse(event)
                job.result_delivered = True  # same rule as the replay above
                return
            else:
                # Informational (reasoning, tool_call, thinking_block, and
                # anything added later). Passing
                # it through the terminal branch closed the stream on the FIRST
                # reasoning delta — the job kept generating server-side while
                # every client saw "disconnected before completion".
                yield _sse(event)
    finally:
        job.subscribers.discard(queue)


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints (spec §3)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/api/generate")
async def start_generation(request: Request):
    """Register a job, kick off the provider stream, return immediately."""
    username = get_authenticated_username(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    provider = body.get("provider")
    config = body.get("config") or {}
    messages = body.get("messages") or []
    meta = body.get("meta") or {}

    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unsupported LLM provider: {provider}")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="config must be an object.")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty array.")
    if not config.get("apiKey") and provider not in ("ollama", "runpod"):
        raise HTTPException(
            status_code=400,
            detail=f"API key is missing for {provider}. Please configure it in Settings.",
        )

    job = registry.create(username, meta if isinstance(meta, dict) else {})
    job.input_chars = sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
    job.conversation = str(config["conversationId"]) if config.get("conversationId") else None
    job.model = str(config.get("model") or "") or None
    job.task = asyncio.create_task(run_job(job, provider, config, messages))
    logger.info("Started generation job %s (provider=%s)", job.job_id, provider)
    return {"jobId": job.job_id, "createdAt": job.created_at}


@router.post("/api/models")
async def list_provider_models(request: Request):
    """List the models a LOCAL endpoint serves, on the browser's behalf.

    Problem: the model dropdown is populated by a fetch from the page, and the
      dev server is served over HTTPS — so every plain-http local endpoint is
      blocked as mixed content and a locally-served model can never be picked.
      (Measured: 127.0.0.1:8090, a LAN address on :8090 and 127.0.0.1:11434 all
      fail from the page with "Failed to fetch"; the same URLs answer fine from
      this process, which is why generation works and discovery does not.)
    Fix: ask the backend, which is same-origin for the page and on the same
      host as the model server.

    Deliberately restricted to loopback: this is for local model servers, and a
    wide-open fetcher would turn this endpoint into an SSRF proxy.
    """
    get_authenticated_username(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    base_url = str((body or {}).get("baseUrl") or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="baseUrl is required.")

    # Anthropic's API cannot be called from a page (CORS), so the Claude and
    # OpenAI model lists are fetched here too — with the user's key, from the
    # provider's own host only, so this never becomes a proxy for any URL.
    provider = (body or {}).get("provider")
    if provider in OFFICIAL_MODEL_HOSTS:
        hosts = OFFICIAL_MODEL_HOSTS[provider]
        if urlparse(base_url).hostname not in hosts:
            raise HTTPException(status_code=400, detail=f"Only {' or '.join(hosts)} can be queried for {provider} models.")
        api_key = str(body.get("apiKey") or "")
        headers = ({"x-api-key": api_key, "anthropic-version": "2023-06-01"} if provider == "anthropic"
                   else {"x-goog-api-key": api_key} if provider == "gemini"
                   else {"Authorization": f"Bearer {api_key}"})
        params = {"limit": 1000} if provider == "anthropic" else {"pageSize": 1000} if provider == "gemini" else None
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(f"{base_url}/models", params=params, headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001 — a bad key or an outage: the UI keeps its fallback list
            logger.info("%s model listing failed: %s", provider, exc)
            return {"models": []}
        if provider == "gemini":
            # Text models of Gemini 3 and later, newest first, the -latest aliases after them. The
            # list also has speech, image, music and agent models, and 2.5, which it still names but
            # refuses to new keys ("no longer available to new users", measured 2026-10-10).
            names = [str(m.get("name") or "").removeprefix("models/") for m in data.get("models") or []
                     if isinstance(m, dict) and "generateContent" in (m.get("supportedGenerationMethods") or [])]
            text = [n for n in names if _GEMINI_TEXT_MODEL_RE.match(n) and not _GEMINI_NON_TEXT_RE.search(n)]
            return {"models": sorted((n for n in text if not n.endswith("-latest")), reverse=True) + [n for n in text if n.endswith("-latest")]}
        ids = [m["id"] for m in data.get("data") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]
        # Anthropic's Models API states each model's window (`max_input_tokens`): forwarded as
        # `contextWindows`, the shape the tab already takes from a local server's listing, so
        # the window table in utils/contextWindow.ts is only the fallback for an unlisted model.
        windows = {m["id"]: m["max_input_tokens"] for m in data.get("data") or []
                   if isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("max_input_tokens"), int) and m["max_input_tokens"] > 0}
        if provider == "openai":
            # Text models only, the gpt line newest first, then the o-series: the
            # account also lists audio, image and realtime models.
            text = [i for i in ids if _OPENAI_CHAT_MODEL_RE.match(i) and not _OPENAI_NON_CHAT_RE.search(i)]
            ids = sorted((i for i in text if i.startswith("gpt-")), reverse=True) + sorted((i for i in text if not i.startswith("gpt-")), reverse=True)
        return {"models": ids, "contextWindows": windows} if windows else {"models": ids}

    if not is_queryable_model_host(base_url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only local endpoints (localhost / 127.0.0.1) or an https "
                "*.proxy.runpod.net endpoint can be queried this way."
            ),
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base_url}/models")
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 — an unreachable local server is normal
        logger.info("Model listing failed for %s: %s", base_url, exc)
        return {"models": []}

    # "Ollama-compatible" covers two dialects: Ollama's own {models:[{name}]}
    # and OpenAI's {data:[{id}]}. llama.cpp answers with both.
    names: List[str] = []
    if isinstance(data.get("data"), list):
        names = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
    if not names and isinstance(data.get("models"), list):
        names = [
            m.get("name") or m.get("model")
            for m in data["models"]
            if isinstance(m, dict) and (m.get("name") or m.get("model"))
        ]

    # Context window, when the server states it. llama.cpp reports the value it
    # was actually STARTED with (`-c`), which is the only trustworthy source:
    # the client cannot infer it, and it changes whenever the model is
    # relaunched — this endpoint was restarted from 32K to 262144 in one day.
    # Anything that budgets history against "the model's limit" needs this
    # number rather than a table that silently goes stale.
    context_windows: Dict[str, int] = {}
    if isinstance(data.get("data"), list):
        for entry in data["data"]:
            if not isinstance(entry, dict):
                continue
            meta = entry.get("meta")
            n_ctx = meta.get("n_ctx") if isinstance(meta, dict) else None
            if isinstance(n_ctx, int) and n_ctx > 0 and entry.get("id"):
                context_windows[str(entry["id"])] = n_ctx

    return {"models": names, "contextWindows": context_windows}


@router.get("/api/generate/active")
async def list_active_generations(request: Request):
    username = get_authenticated_username(request)
    return [job.summary() for job in registry.list_for_user(username)]


@router.get("/api/generate/{job_id}/stream")
async def stream_generation(
    request: Request,
    job_id: str,
    from_offset: int = Query(0, alias="from", ge=0),
):
    username = get_authenticated_username(request)
    job = registry.get(username, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found.")

    return StreamingResponse(
        _job_event_stream(job, from_offset),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering so deltas are not held back.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/generate/{job_id}/abort")
async def abort_generation(request: Request, job_id: str):
    username = get_authenticated_username(request)
    job = registry.get(username, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found.")
    job.abort()
    return {"success": True}

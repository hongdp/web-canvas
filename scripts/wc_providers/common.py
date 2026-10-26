"""What every provider module shares: errors, the HTTP seam, message helpers, the prefix log.

Split out of server_generation.py (2026-10-11), which had grown to 2,100
lines with every provider's wire format in one file. Tests that stub the
network patch `wc_providers.common._http_stream` — the module that owns it;
the provider modules call it through this module, so the patch takes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import httpx

from wc_text.document_tools import from_openai_tools, to_anthropic_tools, to_gemini_tools
from wc_text.retry_policy import parse_retry_after

if TYPE_CHECKING:
    from server_generation import GenerationJob

logger = logging.getLogger("web_canvas.generation")

# Connect fast, but allow long silences between provider deltas (reasoning
# models can think for minutes before the first token).
HTTP_CONNECT_TIMEOUT = 30.0
HTTP_READ_TIMEOUT = 600.0
# Problem: a stuck stream was noticed only when the 600 s read timeout fired.
# Root cause: one timeout for every provider, sized for the slowest silence —
#   a local server prefilling a long prompt, or an OpenAI reasoning model
#   thinking, sends nothing for minutes.
# Fix: grok streams its reasoning summary throughout (measured 2026-10-09:
#   the longest silence 3.6 s at high effort, 11.7 s in another request), so
#   its streams fail after 180 s without a byte; the others keep 600 s.
#   httpx's read timeout is per read, i.e. exactly the idle gap.
IDLE_TIMEOUT_SECONDS = {"grok": 180.0}
# xAI's repetition detectors (agentic_chat_loop.md §0.10): sent on a server
# run's steps. A trigger ENDS the generation (measured), so a step that
# failed after one is the loop, not a transient error.
LOOP_CHECK_HEADERS = {"x-grok-doom-loop-check": "1024", "x-grok-exact-repetition-check": "64"}

_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z+.-]+);base64,(.+)$")

class ProviderError(Exception):
    """A provider-reported failure that should become the job's error event.

    `status` is the HTTP status when there was one, `retry_after` the
    provider's Retry-After in seconds, `transient` a failure reported inside
    the stream that the same request may survive (xAI's server_error).
    """

    def __init__(self, message: str, status: Optional[int] = None, retry_after: Optional[float] = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.transient = transient


_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def _error_summary(text: str) -> Tuple[str, Optional[float]]:
    """The provider's own error message from a JSON error body, and Google's RetryInfo delay.

    Problem: a Gemini free-tier quota error put 60 lines of JSON into the log
      and the run's pause, and its RetryInfo ("retryDelay": "10830s", three
      hours) went unread — the job retried four times in 17 s for nothing.
    Fix: the message is `error.message` when the body has one, and the delay
      is read so the retry loop can see it is not worth waiting for.
    """
    delay = None
    m = _RETRY_DELAY_RE.search(text or "")
    if m:
        delay = float(m.group(1))
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text, delay
    error = parsed.get("error") if isinstance(parsed, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return (message.strip() if isinstance(message, str) and message.strip() else text), delay


def _http_error(provider_label: str, response: Any, text: str) -> ProviderError:
    """The error for a response that failed before streaming, with what the retry policy needs."""
    headers = getattr(response, "headers", None) or {}
    retry_after = parse_retry_after(headers.get("retry-after") if hasattr(headers, "get") else None)
    message, body_delay = _error_summary(text)
    if retry_after is None:
        retry_after = body_delay
    note = f" (retry after {retry_after:.0f}s)" if retry_after and retry_after >= 60 else ""
    return ProviderError(f"{provider_label} API error ({response.status_code}): {message or 'request failed'}{note}",
                         status=response.status_code, retry_after=retry_after)


class _JobAborted(Exception):
    """Raised inside a streaming loop once the abort endpoint has been hit."""

def mask_request_details(url: str, headers: Dict[str, str], body: Any) -> Dict[str, Any]:
    """Mask credentials in a request for safe debug logging.

    Mirrors `maskRequestDetails` in src/services/llm.ts. API keys must never
    reach the logs (spec §6): bearer tokens, `x-api-key`, and Gemini's
    `?key=` query parameter are all redacted.
    """
    masked_headers = dict(headers)
    if "Authorization" in masked_headers:
        masked_headers["Authorization"] = "Bearer ***"
    if "x-api-key" in masked_headers:
        masked_headers["x-api-key"] = "***"
    return {
        "url": re.sub(r"key=[^&]+", "key=***", url),
        "headers": masked_headers,
        "body": body,
    }

# ══════════════════════════════════════════════════════════════════════════════
# Provider request builders — ported from src/services/llm.ts
# ══════════════════════════════════════════════════════════════════════════════

def _split_data_url(image: str) -> Optional[Tuple[str, str]]:
    match = _DATA_URL_RE.match(image or "")
    if not match:
        return None
    return match.group(1), match.group(2)


# ── Tool calling: definitions and history (docs/features/agentic_chat_loop.md)
#
# Wire contract, per message: an assistant reply may carry
# `toolCalls: [{id, name, argumentsText}]`; a tool result is
# `{role: "tool", content, toolCallId, name}`. Before these helpers existed the
# builders copied only role + content, so an agentic loop's tool history was
# dropped without a word and the model never saw its own calls or their
# results.

def _assistant_tool_calls(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tool calls an assistant message made, or [] for any other message."""
    if message.get("role") != "assistant":
        return []
    calls = message.get("toolCalls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _thinking_blocks(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """An assistant message's Anthropic reasoning blocks, copied verbatim.

    The known fields only, values untouched — Anthropic checks each signature
    and rejects a modified block (400). Anything malformed is skipped rather
    than sent. `afterText` (ours, never sent) says the block streamed after the
    reply's text began: it must be replayed after the text, not ahead of it.
    """
    if message.get("role") != "assistant":
        return []
    raw = message.get("thinking")
    if not isinstance(raw, list):
        return []
    blocks: List[Dict[str, Any]] = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        after = {"afterText": True} if block.get("afterText") else {}
        if block.get("type") == "redacted_thinking" and isinstance(block.get("data"), str):
            blocks.append({"type": "redacted_thinking", "data": block["data"], **after})
        elif (
            block.get("type") == "thinking"
            and isinstance(block.get("thinking"), str)
            and isinstance(block.get("signature"), str)
        ):
            blocks.append({"type": "thinking", "thinking": block["thinking"], "signature": block["signature"], **after})
    return blocks


def _reasoning_content(message: Dict[str, Any]) -> str:
    """The text of an assistant message's thinking blocks, for the providers that take it back as one field (Kimi)."""
    return "".join(b["thinking"] for b in _thinking_blocks(message) if b.get("type") == "thinking")


def _arguments_text(call: Dict[str, Any]) -> str:
    text = call.get("argumentsText")
    return text if isinstance(text, str) else ""


def _parsed_arguments(call: Dict[str, Any]) -> Dict[str, Any]:
    """argumentsText as an object — for providers that take arguments parsed.

    Anything that is not a JSON object becomes {}: the providers that want a
    parsed value (Anthropic `input`, Gemini `args`) reject anything else.
    """
    try:
        parsed = json.loads(_arguments_text(call))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_specs(tools: Any) -> List[Dict[str, Any]]:
    """The OpenAI-shaped tool list a request carries, read back (`fromOpenAITools`;
    malformed entries are skipped rather than failing the whole request)."""
    return from_openai_tools(tools if isinstance(tools, list) else None)


def _anthropic_tools(tools: Any) -> List[Dict[str, Any]]:
    return to_anthropic_tools(_tool_specs(tools))


def _gemini_tools(tools: Any) -> List[Dict[str, Any]]:
    """`toGeminiTools`, or nothing at all for a request without tools."""
    specs = _tool_specs(tools)
    return to_gemini_tools(specs) if specs else []


# ══════════════════════════════════════════════════════════════════════════════
# Reasoning effort
# ══════════════════════════════════════════════════════════════════════════════
#
# Mirrors src/utils/reasoningEffort.ts — the client resolves the level against
# its capability table and sends the RESULT, so this module only has to know
# how each provider spells it. Keep the two in step: a level the client sends
# and this drops is a setting the user changed for nothing.

#: Anthropic and Gemini take a token budget instead of a word.
THINKING_BUDGET_TOKENS = {
    "minimal": 512,
    "low": 1024,
    "medium": 4096,
    "high": 16384,
    "xhigh": 32768,
}


def _reasoning_effort(config: Dict[str, Any]) -> Optional[str]:
    """The level the client resolved, or None when the parameter is to be omitted."""
    effort = config.get("reasoningEffort")
    if not effort or effort == "default":
        return None
    return str(effort) if str(effort) in THINKING_BUDGET_TOKENS else None

# ══════════════════════════════════════════════════════════════════════════════
# Provider streaming
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def _http_stream(url: str, headers: Dict[str, str], body: Dict[str, Any], read_timeout: Optional[float] = None):
    """Open a streaming POST. The single seam tests patch to stub the network."""
    timeout = httpx.Timeout(read_timeout or HTTP_READ_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            yield response


async def _read_error_text(response: Any) -> str:
    try:
        raw = await response.aread()
    except Exception:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def _debug_log(provider: str, url: str, headers: Dict[str, str], body: Dict[str, Any], config: Dict[str, Any]) -> None:
    if config.get("debug"):
        logger.info("Outgoing %s request: %s", provider, mask_request_details(url, headers, body))


def _check_abort(job: GenerationJob) -> None:
    if job.abort_requested:
        raise _JobAborted()


# Problem: a turn whose first step re-sent its whole prompt (cache 512 of
#   17117 tokens, 2026-10-06) looks the same in the log whether OUR request
#   changed (a preset switch, a rebuilt message) or the provider simply
#   missed its cache, so there was nothing to say which one to fix.
# Fix: compare each request with the previous one in the same conversation
#   (the cache routing key) and log where they part: the model, the tools,
#   or the first message that differs.
PREFIX_MEMORY = 64
_last_requests: "OrderedDict[str, Tuple[float, str, str, List[str]]]" = OrderedDict()


def _fingerprint(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def describe_prefix(conversation: str, body: Dict[str, Any], now: Optional[float] = None) -> str:
    """How this request's prefix relates to the conversation's previous one."""
    now = time.monotonic() if now is None else now
    messages = list(body.get("messages") or body.get("input") or body.get("contents") or [])
    if body.get("systemInstruction"):
        # Gemini sends the system prompt beside the contents; it is the prefix's first part all the same.
        messages.insert(0, {"role": "system", "parts": body["systemInstruction"].get("parts")})
    hashes = [_fingerprint(m) for m in messages]
    tools = _fingerprint(body.get("tools") or [])
    model = str(body.get("model") or "")
    previous = _last_requests.pop(conversation, None)
    _last_requests[conversation] = (now, model, tools, hashes)
    while len(_last_requests) > PREFIX_MEMORY:
        _last_requests.popitem(last=False)
    if previous is None:
        return "first request seen in this conversation"
    then, prev_model, prev_tools, prev_hashes = previous
    same = 0
    while same < min(len(hashes), len(prev_hashes)) and hashes[same] == prev_hashes[same]:
        same += 1
    parts = [f"{now - then:.0f}s after the previous request"]
    if prev_model != model:
        parts.append(f"model changed ({prev_model} -> {model})")
    if prev_tools != tools:
        parts.append("tools changed")
    if same == len(prev_hashes):
        parts.append(f"extends it (all {same} earlier messages identical)")
    else:
        differing = messages[same] if same < len(messages) else {}
        content = differing.get("content") if isinstance(differing, dict) else None
        if content is None and isinstance(differing, dict) and isinstance(differing.get("parts"), list):
            content = "".join(p.get("text") or "" for p in differing["parts"] if isinstance(p, dict))
        parts.append(
            f"first {same} of its {len(prev_hashes)} messages identical; "
            f"message #{same} ({differing.get('role') if isinstance(differing, dict) else '?'}, "
            f"{len(content) if isinstance(content, str) else 0} chars) differs"
        )
    return "; ".join(parts)

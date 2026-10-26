"""OpenAI Chat Completions, and the servers that speak it: ollama / llama.cpp, a RunPod pod, Kimi.

The request and the stream reader are one implementation; what differs per
provider is its `Dialect` below — an extra header, the reasoning fields, what
a replayed assistant message carries, whether `stream_options` is accepted.
Adding an OpenAI-compatible provider is one `Dialect` row (plus the tables in
wc_text, which are measured facts about the model, not wire format).

Problem: adding Kimi (2026-10-11) put four `if provider == "moonshot"` branches
  into the shared builder and reader, beside grok's two and llama.cpp's one.
Fix: the branches are rows; the builder and reader ask the row.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from wc_text.reasoning_effort import moonshot_thinking, resolve_reasoning_effort

from . import common
from .common import (_arguments_text, _assistant_tool_calls, _check_abort, _debug_log, _http_error, _read_error_text,
                     _reasoning_content, _reasoning_effort, describe_prefix)

if TYPE_CHECKING:
    from server_generation import GenerationJob

logger = logging.getLogger("web_canvas.generation")

Config = Dict[str, Any]
Message = Dict[str, Any]


def _no_headers(config: Config) -> Dict[str, str]:
    return {}


def _reasoning_effort_field(config: Config) -> Dict[str, Any]:
    """The plain spelling: `reasoning_effort` at the level the client resolved."""
    effort = _reasoning_effort(config)
    return {"reasoning_effort": effort} if effort else {}


def _nothing_replayed(message: Message) -> Dict[str, Any]:
    return {}


def _accepts_stream_options(config: Config) -> bool:
    """llama.cpp rejects stream_options, and it sits behind `ollama` (and `runpod`,
    whose dialect says so outright: a pod addressed directly is neither loopback nor
    keyless). Detected the same way the client detects it (services/llm.ts): the
    sentinel key, or a loopback base URL."""
    api_key = config.get("apiKey") or ""
    base_url = config.get("baseUrl") or ""
    return not (api_key == "ollama-no-key" or "localhost" in base_url or "127.0.0.1" in base_url)


def _never(config: Config) -> bool:
    return False


# xAI routes requests with the same conversation id to the same cache shard,
# which maximizes automatic prompt-cache hits across turns.
def _grok_headers(config: Config) -> Dict[str, str]:
    return {"x-grok-conv-id": str(config["conversationId"])} if config.get("conversationId") else {}


def _moonshot_fields(config: Config) -> Dict[str, Any]:
    """Kimi's reasoning fields per model (wc_text.reasoning_effort.moonshot_thinking), the
    effort resolved here so kimi-k3 gets this app's low rather than its own max; and the
    conversation pinned to its cache (prompt_cache_key is a body field on Kimi)."""
    model = config.get("model") or ""
    fields = dict(moonshot_thinking(model, resolve_reasoning_effort("moonshot", model, config.get("reasoningEffort"))))
    if config.get("conversationId"):
        fields["prompt_cache_key"] = str(config["conversationId"])
    return fields


def _moonshot_replay(message: Message) -> Dict[str, Any]:
    """Kimi returns its reasoning as `reasoning_content` and its docs want it back on the
    assistant message in a tool-calling loop (measured: the step is accepted without it
    too; replayed so the model keeps its plan, as every other provider's reasoning is).
    It rides in the message's thinking blocks."""
    reasoning = _reasoning_content(message)
    return {"reasoning_content": reasoning} if reasoning else {}


@dataclass(frozen=True)
class Dialect:
    """How one OpenAI-compatible provider departs from the plain API."""

    #: The name in error messages ("xAI API error (503): …").
    label: str
    #: Request headers beyond Content-Type and the bearer key.
    headers: Callable[[Config], Dict[str, str]] = _no_headers
    #: Body fields beyond model / messages / stream / max_tokens / tools: the reasoning, a cache key.
    request_fields: Callable[[Config], Dict[str, Any]] = _reasoning_effort_field
    #: Fields a replayed assistant message carries beyond content and tool_calls.
    replay_fields: Callable[[Message], Dict[str, Any]] = _nothing_replayed
    #: Whether the server takes `stream_options: {include_usage}` (llama.cpp does not).
    stream_options: Callable[[Config], bool] = _accepts_stream_options
    #: A step's streamed reasoning is kept as one thinking block, for `replay_fields` to send back.
    keeps_reasoning: bool = False


DIALECTS: Dict[str, Dialect] = {
    "openai": Dialect(label="OpenAI"),
    "ollama": Dialect(label="Ollama"),
    "runpod": Dialect(label="RunPod", stream_options=_never),
    "grok": Dialect(label="xAI", headers=_grok_headers),
    "moonshot": Dialect(label="Moonshot", request_fields=_moonshot_fields, replay_fields=_moonshot_replay, keeps_reasoning=True),
}
PLAIN = Dialect(label="OpenAI")


def dialect_of(provider: str) -> Dialect:
    return DIALECTS.get(provider, PLAIN)


def build_openai_request(
    config: Config,
    messages: List[Message],
    provider: str = "openai",
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """A Chat Completions request in the provider's dialect."""
    dialect = dialect_of(provider)
    api_key = config.get("apiKey") or ""
    base_url = (config.get("baseUrl") or "").rstrip("/")

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key and api_key != "ollama-no-key":
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(dialect.headers(config))

    url = f"{base_url}/chat/completions"

    openai_messages: List[Dict[str, Any]] = []
    # view_images: a tool message carries text only here, so a turn's pictures follow its tool messages as one user message.
    pending_images: List[Dict[str, Any]] = []

    def flush_images() -> None:
        if pending_images:
            openai_messages.append({"role": "user", "content": list(pending_images)})
        pending_images.clear()

    for message in messages:
        images = message.get("images") or []
        content = message.get("content") or ""
        if message.get("role") == "tool":
            openai_messages.append({
                "role": "tool",
                "tool_call_id": message.get("toolCallId") or "",
                "content": content,
            })
            for idx, img in enumerate(images):
                pending_images.append({"type": "text", "text": f"[Image {idx + 1}] of the {message.get('name') or 'tool'} result:"})
                pending_images.append({"type": "image_url", "image_url": {"url": img}})
            continue
        flush_images()
        tool_calls = _assistant_tool_calls(message)
        replayed = dialect.replay_fields(message) if message.get("role") == "assistant" else {}
        if tool_calls:
            openai_messages.append({
                "role": "assistant",
                **replayed,
                # A reply that only called tools has no text; the API's own
                # replay of such a turn uses null, not "".
                "content": content or None,
                # `arguments` is the text exactly as the model streamed it —
                # never parsed and re-dumped. xAI's prompt cache is
                # exact-prefix, so a re-spaced replay turns every later step
                # into a full-price prefill.
                "tool_calls": [
                    {
                        "id": call.get("id") or "",
                        "type": "function",
                        "function": {"name": call.get("name") or "", "arguments": _arguments_text(call)},
                    }
                    for call in tool_calls
                ],
            })
        elif images:
            parts: List[Dict[str, Any]] = [{"type": "text", "text": content}]
            for idx, img in enumerate(images):
                parts.append({"type": "text", "text": f"\n[Image {idx + 1}]:"})
                parts.append({"type": "image_url", "image_url": {"url": img}})
            openai_messages.append({"role": message.get("role"), "content": parts})
        else:
            openai_messages.append({"role": message.get("role"), **replayed, "content": content})

    flush_images()
    body: Dict[str, Any] = {
        "model": config.get("model"),
        "messages": openai_messages,
        "stream": True,
    }
    if config.get("maxOutputTokens"):
        body["max_tokens"] = config["maxOutputTokens"]
    body.update(dialect.request_fields(config))

    # Tools come from the client already in OpenAI shape — the internal
    # representation, since most providers speak it natively.
    if config.get("tools"):
        body["tools"] = config["tools"]
        # The agentic run's last step: tools stay (earlier calls reference
        # them) but no new call is allowed. Mirrors streamOpenAI.
        if config.get("toolChoice") == "none":
            body["tool_choice"] = "none"

    if dialect.stream_options(config):
        body["stream_options"] = {"include_usage": True}

    return url, headers, body


async def _stream_openai(
    job: GenerationJob,
    config: Config,
    messages: List[Message],
    provider: str,
) -> Optional[Dict[str, Any]]:
    dialect = dialect_of(provider)
    url, headers, body = build_openai_request(config, messages, provider)
    _debug_log(dialect.label, url, headers, body, config)
    if config.get("conversationId"):
        logger.info("Job %s prefix: %s", job.job_id, describe_prefix(f"{provider}:{config['conversationId']}", body))

    usage: Optional[Dict[str, Any]] = None
    # A dialect that sends reasoning back on the next request (Kimi) keeps
    # the step's reasoning as one thinking block.
    kept_reasoning: List[str] = []
    async with common._http_stream(url, headers, body) as response:
        if response.status_code >= 400:
            err = await _read_error_text(response)
            raise _http_error(dialect.label, response, err)

        async for line in response.aiter_lines():
            _check_abort(job)
            trimmed = (line or "").strip()
            if not trimmed or not trimmed.startswith("data:"):
                continue
            data = trimmed[5:].strip()
            if data == "[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except ValueError:
                logger.warning("Failed to parse %s SSE chunk for job %s", dialect.label, job.job_id)
                continue

            choices = parsed.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    job.append(content)
                # Reasoning models emit their thinking on a separate key before
                # any visible token. Dropping it made a minute of work look
                # like a dead connection. Two spellings in the wild.
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    job.note_reasoning(reasoning)
                    if dialect.keeps_reasoning:
                        kept_reasoning.append(reasoning)
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    job.note_tool_call(
                        tc.get("index") or 0,
                        tc.get("id"),
                        fn.get("name"),
                        fn.get("arguments") or "",
                    )
            if parsed.get("usage"):
                u = parsed["usage"]
                usage = {
                    "promptTokens": u.get("prompt_tokens") or 0,
                    "completionTokens": u.get("completion_tokens") or 0,
                    "cachedPromptTokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
                    "reasoningTokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
                }
    if kept_reasoning:
        job.note_thinking_block({"type": "thinking", "thinking": "".join(kept_reasoning), "signature": ""})
    return usage

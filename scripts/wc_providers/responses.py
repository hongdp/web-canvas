"""The Responses API: grok (xAI), and OpenAI at its own host (wc_text.provider_profile.uses_responses_api)."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from wc_text.reasoning_effort import resolve_reasoning_effort

from . import common
from .common import (IDLE_TIMEOUT_SECONDS, LOOP_CHECK_HEADERS, ProviderError, _arguments_text, _assistant_tool_calls, _check_abort,
                     _debug_log, _http_error, _read_error_text, _reasoning_effort, _tool_specs, describe_prefix)

if TYPE_CHECKING:
    from server_generation import GenerationJob

logger = logging.getLogger("web_canvas.generation")

def _response_items(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """An assistant message's xAI Responses output items, verbatim.

    Mirrors `responseItemsOf` in providerMessages.ts. The items are opaque
    (the reasoning ciphertext above all) and go back exactly as they came.
    """
    if message.get("role") != "assistant":
        return []
    raw = message.get("responseItems")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and isinstance(item.get("type"), str)]


# grok, and OpenAI at its own host, run on the Responses API (wc_text.provider_profile.uses_responses_api).
# Problem (OpenAI, measured 2026-10-10): from gpt-5.4 on, Chat Completions
#   refuses function tools with reasoning ("use /v1/responses"), and gpt-5.6 /
#   gpt-6.x refuse tools there even with no effort set — every agent step
#   carries tools, so those models could not be used at all.
# Fix: the same Responses path as grok, with OpenAI's own spelling of the
#   reasoning (build_responses_request). A compatible server under the
#   `openai` provider stays on Chat Completions.


def build_responses_request(
    config: Dict[str, Any],
    messages: List[Dict[str, Any]],
    provider: str = "grok",
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """grok over the xAI Responses API, or OpenAI over its own (uses_responses_api).

    Problem: on Chat Completions a step's reasoning is gone by the next step.
      grok planned a chapter for 83 s, called a tool, and the next step — with
      0 reasoning tokens and only its own one-line announcement to go on —
      looped on list_chapters 13 times (2026-10-06). xAI also names omitted
      reasoning "the top cause of cache misses", and lists Chat Completions as
      deprecated.
    Fix: the Responses API returns each step's reasoning as an encrypted item;
      every step sends the previous steps' items back unchanged. `store` is
      false and the ciphertext is requested explicitly, so nothing of the
      book is kept on xAI's side for later retrieval.
    """
    api_key = config.get("apiKey") or ""
    base_url = (config.get("baseUrl") or "").rstrip("/")
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if provider == "grok" and config.get("conversationId"):
        headers["x-grok-conv-id"] = str(config["conversationId"])

    items: List[Dict[str, Any]] = []
    # view_images: the pictures of a turn's results follow its function_call_output items, as one user message.
    pending_images: List[Dict[str, Any]] = []

    def flush_images() -> None:
        if pending_images:
            items.append({"role": "user", "content": list(pending_images)})
        pending_images.clear()

    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message.get("toolCallId") or "", "output": content})
            for idx, img in enumerate(message.get("images") or []):
                pending_images.append({"type": "input_text", "text": f"[Image {idx + 1}] of the {message.get('name') or 'tool'} result:"})
                pending_images.append({"type": "input_image", "image_url": img})
            continue
        flush_images()
        if role == "assistant":
            # Items the reply kept go back verbatim; whatever they do not
            # cover (a history message keeps only its reasoning) is rebuilt
            # from the text and calls, after them.
            replay = _response_items(message)
            items.extend(replay)
            has_message = any(item.get("type") == "message" for item in replay)
            replayed_calls = {item.get("call_id") for item in replay if item.get("type") == "function_call"}
            calls = [c for c in _assistant_tool_calls(message) if c.get("id") not in replayed_calls]
            if not has_message and (content or (not calls and not replay)):
                items.append({"role": "assistant", "content": content})
            for call in calls:
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id") or "",
                    "name": call.get("name") or "",
                    "arguments": _arguments_text(call),
                })
            continue
        images = message.get("images") or []
        if images:
            parts: List[Dict[str, Any]] = [{"type": "input_text", "text": content}]
            for idx, img in enumerate(images):
                parts.append({"type": "input_text", "text": f"\n[Image {idx + 1}]:"})
                parts.append({"type": "input_image", "image_url": img})
            items.append({"role": role, "content": parts})
        else:
            items.append({"role": role, "content": content})

    flush_images()
    body: Dict[str, Any] = {
        "model": config.get("model"),
        "input": items,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if config.get("maxOutputTokens"):
        body["max_output_tokens"] = config["maxOutputTokens"]
    if provider == "openai":
        # Resolved against the model here: OpenAI's families take different
        # sets (gpt-5 has minimal, 5.1 none, 5.4+ xhigh; gpt-4o/4.1 refuse the
        # field), and a summary streams the reasoning to the chat live.
        effort = resolve_reasoning_effort("openai", config.get("model") or "", config.get("reasoningEffort"))
        if effort:
            body["reasoning"] = {"effort": effort, "summary": "auto"}
        else:
            # A model without reasoning has no encrypted reasoning to return.
            body.pop("include", None)
    else:
        effort = _reasoning_effort(config)
        if effort:
            body["reasoning"] = {"effort": effort}
    if config.get("conversationId"):
        body["prompt_cache_key"] = str(config["conversationId"])
    tools = _tool_specs(config.get("tools"))
    if tools:
        # OpenAI's Responses API makes function tools strict by default, and a
        # strict schema has every property required: the model then fills each
        # optional argument with an empty value (`"paragraphs": ""`, measured
        # 2026-10-10), which the tools read as a request. Ours are not strict.
        strict = {"strict": False} if provider == "openai" else {}
        body["tools"] = [{"type": "function", **spec, **strict} for spec in tools]
        if config.get("toolChoice") == "none":
            body["tool_choice"] = "none"
    return f"{base_url}/responses", headers, body


async def _stream_responses(
    job: GenerationJob,
    config: Dict[str, Any],
    messages: List[Dict[str, Any]],
    provider: str = "grok",
) -> Optional[Dict[str, Any]]:
    url, headers, body = build_responses_request(config, messages, provider)
    label = "xAI" if provider == "grok" else "OpenAI"
    _debug_log(f"{label} Responses", url, headers, body, config)
    if config.get("conversationId"):
        logger.info("Job %s prefix: %s", job.job_id, describe_prefix(f"{provider}:{config['conversationId']}", body))

    if provider == "grok" and config.get("loopCheck"):
        headers = {**headers, **LOOP_CHECK_HEADERS}
    usage: Optional[Dict[str, Any]] = None
    async with common._http_stream(url, headers, body, read_timeout=IDLE_TIMEOUT_SECONDS.get(provider)) as response:
        if response.status_code >= 400:
            raise _http_error(label, response, await _read_error_text(response))
        async for line in response.aiter_lines():
            _check_abort(job)
            trimmed = (line or "").strip()
            if not trimmed.startswith("data:"):
                continue
            data = trimmed[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                logger.warning("Failed to parse xAI Responses SSE chunk for job %s", job.job_id)
                continue
            kind = event.get("type")
            index = event.get("output_index") or 0
            if kind == "response.output_text.delta":
                job.append(event.get("delta") or "")
            elif kind == "response.reasoning_summary_text.delta":
                job.note_reasoning(event.get("delta") or "")
            elif kind == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    job.note_tool_call(index, item.get("call_id"), item.get("name"), item.get("arguments") or "")
            elif kind == "response.function_call_arguments.delta":
                job.note_tool_call(index, None, None, event.get("delta") or "")
            elif kind == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    if item.get("type") == "function_call" and isinstance(item.get("arguments"), str):
                        job.replace_tool_call_arguments(index, item["arguments"])
                    job.note_response_item(item)
            elif kind == "response.doom_loop_check":
                job.note_loop_triggers((event.get("doom_loop_check") or {}).get("triggers"))
            elif kind in ("response.completed", "response.incomplete"):
                job.note_loop_triggers(((event.get("response") or {}).get("doom_loop_check") or {}).get("triggers"))
                u = (event.get("response") or {}).get("usage") or {}
                usage = {
                    "promptTokens": u.get("input_tokens") or 0,
                    "completionTokens": u.get("output_tokens") or 0,
                    "cachedPromptTokens": (u.get("input_tokens_details") or {}).get("cached_tokens") or 0,
                    # GPT-5.6 and later bill a cache write at 1.25x the input rate: worth seeing in the log.
                    "cacheWriteTokens": (u.get("input_tokens_details") or {}).get("cache_write_tokens") or 0,
                    "reasoningTokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0,
                }
            elif kind in ("response.failed", "error"):
                failure = (event.get("response") or {}).get("error") or event
                code = str(failure.get("code") or "")
                message = str(failure.get("message") or "")
                raise ProviderError(f"{label} API error: {message or code or 'response failed'}",
                                    transient=code in ("server_error", "internal_error", "overloaded") or "internal error" in message.lower())
    return usage

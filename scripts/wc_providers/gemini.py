"""Gemini generateContent over SSE: the request with function declarations and thinking, and its chunks."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from wc_text.reasoning_effort import gemini_thinking, resolve_reasoning_effort

from . import common
from .common import (ProviderError, _assistant_tool_calls, _check_abort, _debug_log, _gemini_tools, _http_error, _parsed_arguments, describe_prefix,
                     _read_error_text, _split_data_url)

if TYPE_CHECKING:
    from server_generation import GenerationJob

logger = logging.getLogger("web_canvas.generation")

def build_gemini_request(
    config: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    system_message = next((m for m in messages if m.get("role") == "system"), None)

    # Gemini keys a function result by the tool's NAME, not a call id. The
    # client sends `name` on every tool message; recovering it from the call
    # it answers covers a message that arrives without one.
    call_names: Dict[str, str] = {}
    for message in messages:
        for call in _assistant_tool_calls(message):
            if call.get("id") and call.get("name"):
                call_names[str(call["id"])] = str(call["name"])

    contents: List[Dict[str, Any]] = []
    previous_was_tool = False
    # view_images: a result's pictures go after ALL of the turn's functionResponse parts (one user content).
    pending_images: List[Dict[str, Any]] = []

    def flush_images() -> None:
        if pending_images and contents:
            contents[-1]["parts"].extend(pending_images)
        pending_images.clear()

    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        content = message.get("content") or ""
        if role != "tool":
            flush_images()
        if role == "tool":
            response_part = {
                "functionResponse": {
                    "name": message.get("name") or call_names.get(str(message.get("toolCallId") or ""), ""),
                    "response": {"content": content},
                }
            }
            # Every result for one model turn travels in ONE user message.
            if previous_was_tool and contents:
                contents[-1]["parts"].append(response_part)
            else:
                contents.append({"role": "user", "parts": [response_part]})
            for idx, img in enumerate(message.get("images") or []):
                split = _split_data_url(img)
                if split:
                    pending_images.append({"text": f"[Image {idx + 1}] of the {response_part['functionResponse']['name']} result:"})
                    pending_images.append({"inlineData": {"mimeType": split[0], "data": split[1]}})
            previous_was_tool = True
            continue
        previous_was_tool = False

        tool_calls = _assistant_tool_calls(message)
        if tool_calls:
            model_parts: List[Dict[str, Any]] = [{"text": content}] if content else []
            for call in tool_calls:
                part: Dict[str, Any] = {
                    "functionCall": {"name": call.get("name") or "", "args": _parsed_arguments(call)}
                }
                # Back on the part it came on, as a sibling of functionCall.
                # Gemini 3 rejects a step whose first call lacks it; only the
                # first of parallel calls ever has one.
                signature = call.get("signature")
                if isinstance(signature, str) and signature:
                    part["thoughtSignature"] = signature
                model_parts.append(part)
            contents.append({"role": "model", "parts": model_parts})
            continue

        # Gemini refuses an empty text part ("contents.parts must not be empty"),
        # so an empty message (an image-only one keeps its images) is left out.
        parts: List[Dict[str, Any]] = [{"text": content}] if content else []
        for idx, img in enumerate(message.get("images") or []):
            split = _split_data_url(img)
            if split:
                parts.append({"text": f"\n[Image {idx + 1}]:"})
                parts.append({"inlineData": {"mimeType": split[0], "data": split[1]}})
        if not parts:
            continue
        contents.append({
            "role": "model" if message.get("role") == "assistant" else "user",
            "parts": parts,
        })

    flush_images()
    body: Dict[str, Any] = {"contents": contents}
    if system_message:
        body["systemInstruction"] = {"parts": [{"text": system_message.get("content") or ""}]}
    if config.get("geminiSafetySettings"):
        body["safetySettings"] = config["geminiSafetySettings"]
    generation_config: Dict[str, Any] = {}
    if config.get("maxOutputTokens"):
        generation_config["maxOutputTokens"] = config["maxOutputTokens"]
    # A level on Gemini 3.x, a budget on 2.5 (wc_text.reasoning_effort.gemini_thinking), resolved
    # here against the model; thoughts included so they stream as reasoning, never as text.
    thinking = gemini_thinking(config.get("model") or "",
                               resolve_reasoning_effort("gemini", config.get("model") or "", config.get("reasoningEffort")))
    if thinking:
        generation_config["thinkingConfig"] = thinking
    if generation_config:
        body["generationConfig"] = generation_config
    gemini_tools = _gemini_tools(config.get("tools"))
    if gemini_tools:
        body["tools"] = gemini_tools
        if config.get("toolChoice") == "none":
            body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}

    # Support model names with or without the 'models/' prefix.
    model = config.get("model") or ""
    model_name = model[7:] if model.startswith("models/") else model
    # SSE (one JSON chunk per data line), and the key in a header, not the URL,
    # so it never lands in a log line.
    url = f"{(config.get('baseUrl') or '').rstrip('/')}/models/{model_name}:streamGenerateContent?alt=sse"
    return url, {"Content-Type": "application/json", "x-goog-api-key": str(config.get("apiKey") or "")}, body


def _handle_gemini_chunk(
    job: GenerationJob,
    chunk: Dict[str, Any],
    usage: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    # Detect prompt-level safety blocks (e.g. prohibited content).
    block_reason = (chunk.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        raise ProviderError(f"Content generation blocked by safety policy: {block_reason}")

    if chunk.get("usageMetadata"):
        meta = chunk["usageMetadata"]
        usage = {
            "promptTokens": meta.get("promptTokenCount") or 0,
            "completionTokens": (meta.get("candidatesTokenCount") or 0) + (meta.get("thoughtsTokenCount") or 0),
            "cachedPromptTokens": meta.get("cachedContentTokenCount") or 0,
            "reasoningTokens": meta.get("thoughtsTokenCount") or 0,
        }

    candidates = chunk.get("candidates") or []
    if candidates:
        candidate = candidates[0]
        # Detect response-level safety blocks or abnormal termination
        # (e.g. SAFETY, RECITATION).
        finish_reason = candidate.get("finishReason")
        if finish_reason and finish_reason not in ("STOP", "MAX_TOKENS"):
            raise ProviderError(
                f"Content generation blocked or terminated abnormally: {finish_reason}"
            )
        parts = (candidate.get("content") or {}).get("parts") or []
        for part in parts:
            # Every text part, not just the first; a thought part is reasoning,
            # shown live and never written into the document.
            if isinstance(part, dict) and part.get("text"):
                if part.get("thought"):
                    job.note_reasoning(part["text"])
                else:
                    job.append(part["text"])
        for part in parts:
            function_call = part.get("functionCall") if isinstance(part, dict) else None
            if isinstance(function_call, dict):
                # Gemini delivers a call whole, not in fragments, so it is one
                # delta carrying the complete arguments — and each call gets
                # the next index (streamGemini's `functionCallIndex++`), since
                # parallel calls sharing index 0 merge into one garbage call.
                # Compact, non-ASCII-preserving JSON mirrors
                # `JSON.stringify(args ?? {})` byte for byte.
                args = function_call.get("args")
                signature = part.get("thoughtSignature")
                job.note_tool_call(
                    len(job.tool_calls),
                    None,
                    function_call.get("name"),
                    json.dumps(
                        args if args is not None else {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    # Sits beside functionCall on the part; first of parallel
                    # calls only. Must go back on the same part.
                    signature if isinstance(signature, str) else None,
                )
    return usage


async def _stream_gemini(
    job: GenerationJob,
    config: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    url, headers, body = build_gemini_request(config, messages)
    _debug_log("Gemini", url, headers, body, config)
    if config.get("conversationId"):
        # Gemini's cache is implicit (no key to route by): the prefix diagnostic is the only way to tell
        # our request changing from the provider missing (2026-10-17; the other providers had it already).
        logger.info("Job %s prefix: %s", job.job_id, describe_prefix(f"gemini:{config['conversationId']}", body))

    usage: Optional[Dict[str, Any]] = None
    async with common._http_stream(url, headers, body) as response:
        if response.status_code >= 400:
            err = await _read_error_text(response)
            raise _http_error("Gemini", response, err)

        async for line in response.aiter_lines():
            _check_abort(job)
            trimmed = (line or "").strip()
            if not trimmed.startswith("data:"):
                continue
            try:
                chunk = json.loads(trimmed[5:].strip())
            except ValueError:
                logger.warning("Failed to parse Gemini SSE chunk for job %s", job.job_id)
                continue
            usage = _handle_gemini_chunk(job, chunk, usage)
    return usage

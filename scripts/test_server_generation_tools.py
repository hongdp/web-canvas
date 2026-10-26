"""Tool calling in the backend generation transport (server_generation.py).

Covers the request builders replaying tool history in each provider's native
shape, the Anthropic / Gemini tool-definition translation, and the stream
readers reporting tool calls through `GenerationJob.note_tool_call`.

The HTTP layer is stubbed by patching `wc_providers.common._http_stream` — the
module that OWNS it (see test_api_server.py).
"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server_generation  # noqa: E402
import wc_providers.common  # noqa: E402
import wc_providers.anthropic as anthropic_provider  # noqa: E402


# Deliberately unusual spacing and non-ASCII: a builder that parses and
# re-dumps this would change the bytes, which is what the tests catch.
ARGS_TEXT = '{"chapter":  "第三章"}'

UPDATE_TOOL = {
    "type": "function",
    "function": {
        "name": "update_document",
        "description": "Replace the document.",
        "parameters": {
            "type": "object",
            "properties": {
                "html": {"type": "string", "description": "New HTML.", "format": "html"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": ["html"],
            "additionalProperties": False,
        },
    },
}

READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read_chapter",
        "description": "Read a chapter.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _tool_history():
    """system, user, assistant(two calls), two tool results, user."""
    return [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Fix chapter three."},
        {
            "role": "assistant",
            "content": "Reading it first.",
            "toolCalls": [
                {"id": "call_1", "name": "read_chapter", "argumentsText": ARGS_TEXT},
                {"id": "call_2", "name": "read_chapter", "argumentsText": '{"chapter":"第四章"}'},
            ],
        },
        {"role": "tool", "toolCallId": "call_1", "name": "read_chapter", "content": "<p>three</p>"},
        {"role": "tool", "toolCallId": "call_2", "name": "read_chapter", "content": "<p>four</p>"},
        {"role": "user", "content": "Go on.", "cacheHint": True},
    ]


@pytest.fixture(autouse=True)
def _clean_generation_registry():
    server_generation.registry.clear()
    yield
    server_generation.registry.clear()


# ── OpenAI-compatible (grok first) ────────────────────────────────────────────

def test_openai_request_replays_tool_calls_and_results_exactly():
    history = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Fix chapter three."},
        {
            "role": "assistant",
            "content": "Reading it first.",
            "toolCalls": [{"id": "call_1", "name": "read_chapter", "argumentsText": ARGS_TEXT}],
        },
        {"role": "tool", "toolCallId": "call_1", "name": "read_chapter", "content": "<p>three</p>"},
        {"role": "user", "content": "Go on."},
    ]
    _, _, body = server_generation.build_openai_request(
        {"model": "grok-4", "baseUrl": "https://api.x.ai/v1", "apiKey": "k", "tools": [READ_TOOL]},
        history,
        "grok",
    )

    assert body["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Fix chapter three."},
        {
            "role": "assistant",
            "content": "Reading it first.",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_chapter", "arguments": ARGS_TEXT},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "<p>three</p>"},
        {"role": "user", "content": "Go on."},
    ]
    # Byte-identical: xAI's prompt cache is exact-prefix.
    arguments = body["messages"][2]["tool_calls"][0]["function"]["arguments"]
    assert arguments.encode("utf-8") == ARGS_TEXT.encode("utf-8")
    assert body["tools"] == [READ_TOOL]


def test_openai_request_uses_null_content_for_a_tool_only_reply():
    _, _, body = server_generation.build_openai_request(
        {"model": "gpt", "baseUrl": "https://api.openai.com/v1", "apiKey": "k"},
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "toolCalls": [{"id": "c", "name": "update_document", "argumentsText": "{}"}],
            },
        ],
    )
    assistant = body["messages"][1]
    assert assistant == {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c", "type": "function", "function": {"name": "update_document", "arguments": "{}"}}],
    }


def test_openai_request_leaves_plain_and_image_messages_unchanged():
    _, _, body = server_generation.build_openai_request(
        {"model": "gpt", "baseUrl": "https://api.openai.com/v1", "apiKey": "k"},
        [
            {"role": "user", "content": "look", "images": ["data:image/png;base64,AAAA"]},
            {"role": "assistant", "content": "ok"},
        ],
    )
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "text", "text": "\n[Image 1]:"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
        {"role": "assistant", "content": "ok"},
    ]


# ── Anthropic ────────────────────────────────────────────────────────────────

def test_anthropic_request_translates_tools_generically_and_skips_malformed():
    _, _, body = server_generation.build_anthropic_request(
        {
            "model": "claude",
            "baseUrl": "https://api.anthropic.com/v1",
            "apiKey": "k",
            "tools": [
                UPDATE_TOOL,
                {"type": "function", "function": {"name": "no_schema"}},
                {"type": "function"},
                "garbage",
                {"type": "function", "function": {"name": "undescribed", "parameters": {"type": "object"}}},
            ],
        },
        [{"role": "user", "content": "hi"}],
    )
    assert body["tools"] == [
        {
            "name": "update_document",
            "description": "Replace the document.",
            "input_schema": UPDATE_TOOL["function"]["parameters"],
        },
        {"name": "undescribed", "description": "", "input_schema": {"type": "object"}},
    ]


def test_anthropic_request_omits_tools_when_none_are_sent():
    _, _, body = server_generation.build_anthropic_request(
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        [{"role": "user", "content": "hi"}],
    )
    assert "tools" not in body


def test_anthropic_request_replays_tool_use_and_merges_results():
    _, _, body = server_generation.build_anthropic_request(
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        _tool_history(),
    )
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "user"]
    assert messages[1]["content"] == [
        {"type": "text", "text": "Reading it first."},
        {"type": "tool_use", "id": "call_1", "name": "read_chapter", "input": {"chapter": "第三章"}},
        {"type": "tool_use", "id": "call_2", "name": "read_chapter", "input": {"chapter": "第四章"}},
    ]
    # Both results in ONE user message, in order.
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "<p>three</p>"},
        {"type": "tool_result", "tool_use_id": "call_2", "content": "<p>four</p>"},
    ]


def test_anthropic_tool_only_reply_has_no_empty_text_block_and_bad_args_become_empty_input():
    _, _, body = server_generation.build_anthropic_request(
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "toolCalls": [
                    {"id": "a", "name": "update_document", "argumentsText": '{"html": "<p>x'},
                    {"id": "b", "name": "update_document", "argumentsText": "[1, 2]"},
                ],
            },
        ],
    )
    assert body["messages"][1]["content"] == [
        {"type": "tool_use", "id": "a", "name": "update_document", "input": {}},
        {"type": "tool_use", "id": "b", "name": "update_document", "input": {}},
    ]


def test_anthropic_cache_hint_after_merged_tool_results_lands_on_its_own_message():
    history = _tool_history() + [
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "<document context>"},
    ]
    _, _, body = server_generation.build_anthropic_request(
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        history,
    )
    messages = body["messages"]
    # The hinted message is non_system[4]; after the merge it is message 3.
    # Indexing anthropic_messages by the source position would mark message 4
    # ("Done.") instead.
    assert messages[3]["content"] == [
        {"type": "text", "text": "Go on.", "cache_control": {"type": "ephemeral"}},
    ]
    assert messages[4]["content"] == "Done."
    # The request's end carries its own breakpoint, so the next step reads it.
    assert messages[5]["content"] == [
        {"type": "text", "text": "<document context>", "cache_control": {"type": "ephemeral"}},
    ]
    for earlier in messages[:3]:
        content = earlier["content"]
        blocks = content if isinstance(content, list) else []
        assert not any("cache_control" in block for block in blocks)


def test_anthropic_cache_hint_on_a_merged_tool_result_marks_that_merged_message():
    # Mirrors providerMessages.ts `sourceIndex`: a hint maps to the MESSAGE the
    # source landed in, and the breakpoint goes on that message's last block.
    history = _tool_history()
    history[3]["cacheHint"] = True  # the FIRST of the two merged results
    history[5].pop("cacheHint")
    _, _, body = server_generation.build_anthropic_request(
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        history,
    )
    results = body["messages"][2]["content"]
    assert "cache_control" not in results[0]
    assert results[1]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][3]["content"] == [
        {"type": "text", "text": "Go on.", "cache_control": {"type": "ephemeral"}},
    ]


def test_anthropic_step_tail_carries_a_breakpoint_so_the_next_step_reads_it():
    # An agent step: the turn's history (hinted), then the run's own steps.
    # Every step re-sends the turn so far; without a breakpoint at its end it
    # was billed at the full input price on every step (2026-10-14).
    step_one = _tool_history() + [
        {"role": "assistant", "content": "", "toolCalls": [{"id": "call_3", "name": "update_document", "argumentsText": "{}"}]},
        {"role": "tool", "toolCallId": "call_3", "name": "update_document", "content": "written"},
    ]
    step_two = step_one + [
        {"role": "assistant", "content": "", "toolCalls": [{"id": "call_4", "name": "read_chapter", "argumentsText": "{}"}]},
        {"role": "tool", "toolCallId": "call_4", "name": "read_chapter", "content": "<p>five</p>"},
    ]
    config = {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"}

    def marked(body):
        return [
            (i, block.get("text") or block.get("content") or block.get("tool_use_id"))
            for i, message in enumerate(body["messages"])
            if isinstance(message["content"], list)
            for block in message["content"]
            if "cache_control" in block
        ]

    _, _, first = server_generation.build_anthropic_request(config, step_one)
    _, _, second = server_generation.build_anthropic_request(config, step_two)
    assert marked(first) == [(3, "Go on."), (5, "written")]
    assert marked(second) == [(3, "Go on."), (7, "<p>five</p>")]
    # Apart from where the breakpoints sit, the second request extends the first.
    strip = lambda body: json.loads(json.dumps(body["messages"]).replace(', "cache_control": {"type": "ephemeral"}', ""))  # noqa: E731
    assert strip(second)[:len(first["messages"])] == strip(first)


def test_anthropic_breakpoint_skips_a_thinking_block():
    message = {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "thinking", "thinking": "", "signature": "s"}]}
    anthropic_provider._mark_cache_breakpoint(message)
    assert message["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in message["content"][1]


def test_anthropic_cache_hints_still_cap_at_three():
    history = [{"role": "system", "content": "S"}]
    for i in range(5):
        history.append({"role": "user", "content": f"u{i}", "cacheHint": True})
        history.append({
            "role": "assistant",
            "content": "",
            "toolCalls": [{"id": f"c{i}", "name": "read_chapter", "argumentsText": "{}"}],
        })
        history.append({"role": "tool", "toolCallId": f"c{i}", "name": "read_chapter", "content": "r"})
    _, _, body = server_generation.build_anthropic_request(
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        history,
    )
    marked = [
        block
        for message in body["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if "cache_control" in block
    ]
    assert len(marked) == 3


# ── Gemini ───────────────────────────────────────────────────────────────────

def test_gemini_request_cleans_tool_schemas_like_toGeminiTools():
    _, _, body = server_generation.build_gemini_request(
        {
            "model": "gemini-2.5-pro",
            "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
            "apiKey": "k",
            "tools": [UPDATE_TOOL, READ_TOOL, {"type": "function", "function": {"name": "broken"}}],
        },
        [{"role": "user", "content": "hi"}],
    )
    assert body["tools"] == [{
        "functionDeclarations": [
            {
                "name": "update_document",
                "description": "Replace the document.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        # `format` dropped
                        "html": {"type": "STRING", "description": "New HTML."},
                        # `minLength` dropped, `items` cleaned recursively
                        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                    },
                    "required": ["html"],
                    # `additionalProperties` dropped
                },
            },
            {
                "name": "read_chapter",
                "description": "Read a chapter.",
                # Empty properties / required are truthy in JS, so kept.
                "parameters": {"type": "OBJECT", "properties": {}, "required": []},
            },
        ]
    }]


def test_gemini_request_replays_function_calls_and_merges_responses():
    _, _, body = server_generation.build_gemini_request(
        {"model": "gemini-2.5-pro", "baseUrl": "https://g/v1beta", "apiKey": "k"},
        _tool_history(),
    )
    assert body["systemInstruction"] == {"parts": [{"text": "SYSTEM"}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "Fix chapter three."}]},
        {
            "role": "model",
            "parts": [
                {"text": "Reading it first."},
                {"functionCall": {"name": "read_chapter", "args": {"chapter": "第三章"}}},
                {"functionCall": {"name": "read_chapter", "args": {"chapter": "第四章"}}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "read_chapter", "response": {"content": "<p>three</p>"}}},
                {"functionResponse": {"name": "read_chapter", "response": {"content": "<p>four</p>"}}},
            ],
        },
        {"role": "user", "parts": [{"text": "Go on."}]},
    ]
    assert "tools" not in body


def test_gemini_tool_only_reply_has_no_empty_text_part():
    _, _, body = server_generation.build_gemini_request(
        {"model": "gemini-2.5-pro", "baseUrl": "https://g/v1beta", "apiKey": "k"},
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "toolCalls": [{"id": "x", "name": "update_document", "argumentsText": "not json"}],
            },
            # No `name`: recovered from the call it answers.
            {"role": "tool", "toolCallId": "x", "content": "applied"},
        ],
    )
    assert body["contents"][1] == {
        "role": "model",
        "parts": [{"functionCall": {"name": "update_document", "args": {}}}],
    }
    assert body["contents"][2]["parts"][0]["functionResponse"]["name"] == "update_document"


# ── Stream readers ───────────────────────────────────────────────────────────

class _FakeStreamResponse:
    """Stands in for an httpx streaming response."""

    def __init__(self, lines=None, text_chunks=None):
        self.status_code = 200
        self._lines = lines or []
        self._text_chunks = text_chunks or []

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aiter_text(self):
        for chunk in self._text_chunks:
            yield chunk

    async def aread(self):
        return b""


def _run_job(provider, config, messages, response):
    captured = []

    @asynccontextmanager
    async def fake_stream(url, headers, body, read_timeout=None):
        captured.append({"url": url, "headers": headers, "body": body})
        yield response

    job = server_generation.GenerationJob("gen-tools", "alice", {})
    with patch.object(wc_providers.common, "_http_stream", fake_stream):
        asyncio.run(server_generation.run_job(job, provider, config, messages))
    return job, captured


def _sse(event):
    return "data: " + json.dumps(event, ensure_ascii=False)


def test_anthropic_stream_reports_tool_use_by_block_index():
    lines = [
        "event: message_start",
        _sse({"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}}),
        _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Updating."}}),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse({
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "update_document", "input": {}},
        }),
        _sse({"type": "content_block_delta", "index": 1,
              "delta": {"type": "input_json_delta", "partial_json": '{"html": "<p>新'}}),
        _sse({"type": "content_block_delta", "index": 1,
              "delta": {"type": "input_json_delta", "partial_json": '</p>"}'}}),
        _sse({"type": "content_block_stop", "index": 1}),
        _sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}}),
    ]
    job, captured = _run_job(
        "anthropic",
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k", "tools": [UPDATE_TOOL]},
        [{"role": "user", "content": "go"}],
        _FakeStreamResponse(lines=lines),
    )

    assert job.status == "done", job.error
    assert job.buffer == "Updating."  # text deltas unaffected
    assert job.tool_calls == {
        1: {"id": "toolu_1", "name": "update_document", "arguments": '{"html": "<p>新</p>"}'},
    }
    assert captured[0]["body"]["tools"][0]["name"] == "update_document"
    assert job.usage == {"promptTokens": 10, "completionTokens": 20, "cachedPromptTokens": 0, "cacheWriteTokens": 0}


def test_gemini_stream_reports_function_call_like_streamGemini():
    chunk = {
        "candidates": [{
            "content": {"role": "model", "parts": [
                {"text": "Done."},
                {"functionCall": {"name": "update_document", "args": {"html": "<p>新</p>", "n": 2}}},
            ]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 7},
    }
    # Server-sent events (alt=sse): one chunk per data line.
    job, _ = _run_job(
        "gemini",
        {"model": "gemini-2.5-pro", "baseUrl": "https://g/v1beta", "apiKey": "k", "tools": [UPDATE_TOOL]},
        [{"role": "user", "content": "go"}],
        _FakeStreamResponse(lines=["data: " + json.dumps(chunk, ensure_ascii=False)]),
    )

    assert job.status == "done", job.error
    assert job.buffer == "Done."
    # index 0, JSON.stringify spelling: compact separators, non-ASCII kept.
    assert job.tool_calls == {
        0: {"id": None, "name": "update_document", "arguments": '{"html":"<p>新</p>","n":2}'},
    }


# ── Reasoning artifacts: Anthropic thinking blocks, Gemini thoughtSignature ──
#
# Both providers check these on replay when reasoning is on: Anthropic wants
# the tool_use turn back with its thinking blocks verbatim and first, Gemini 3
# wants the first call of each step back with its thoughtSignature (400
# otherwise). Mirrors the TS side (providerMessages.test.ts,
# llmReasoningReplay.test.ts, remoteGeneration.test.ts).

THINKING = {"type": "thinking", "thinking": "Need chapter 3 first.", "signature": "EqQBCgIYAhIM1gbc+/=="}
REDACTED = {"type": "redacted_thinking", "data": "EmwKAhgBEgy3va3pzix/LafPsn4a"}


def _parse_sse_frames(frames):
    return [json.loads(f[len("data: "):]) for f in frames if f.startswith("data: ")]


def test_anthropic_cache_diagnostics_thread_the_previous_message_id_through_a_conversation():
    import wc_providers.anthropic as anth
    anth._last_message_ids.clear()
    cfg = {"model": "claude-sonnet-5-5", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k", "conversationId": "book-7"}
    # First request of the conversation: opted in, nothing to compare against.
    _, _, body = server_generation.build_anthropic_request(cfg, [{"role": "user", "content": "hi"}])
    assert body["diagnostics"] == {"previous_message_id": None}
    # No conversation: no opt-in (nothing to thread it through).
    _, _, lone = server_generation.build_anthropic_request({**cfg, "conversationId": None}, [{"role": "user", "content": "hi"}])
    assert "diagnostics" not in lone
    # The response's id is remembered from message_start…
    lines = [
        _sse({"type": "message_start", "message": {"id": "msg_first", "usage": {"input_tokens": 10, "output_tokens": 1}, "diagnostics": None}}),
        _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
        _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
    ]
    _run_job("anthropic", cfg, [{"role": "user", "content": "hi"}], _FakeStreamResponse(lines=lines))
    # …and the next request of the same conversation carries it; another conversation does not.
    _, _, second = server_generation.build_anthropic_request(cfg, [{"role": "user", "content": "hi"}])
    assert second["diagnostics"] == {"previous_message_id": "msg_first"}
    _, _, other = server_generation.build_anthropic_request({**cfg, "conversationId": "book-8"}, [{"role": "user", "content": "hi"}])
    assert other["diagnostics"] == {"previous_message_id": None}
    # A divergence the API reports is logged as a warning, in its words.
    diverged = [
        _sse({"type": "message_start", "message": {"id": "msg_second", "usage": {"input_tokens": 10, "output_tokens": 1},
                                                   "diagnostics": {"cache_miss_reason": {"type": "system_changed", "cache_missed_input_tokens": 41850}}}}),
        _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
    ]
    with patch.object(anth.logger, "warning") as warned:
        _run_job("anthropic", cfg, [{"role": "user", "content": "hi"}], _FakeStreamResponse(lines=diverged))
    assert any("cache diagnostics" in c.args[0] and c.args[2] == "system_changed (≈41850 input tokens after the divergence)" for c in warned.call_args_list)
    assert anth._last_message_ids["anthropic:book-7"] == "msg_second"
    assert anth.describe_diagnostics({"cache_miss_reason": None}) == "comparison still pending"


def test_anthropic_request_replays_thinking_first_and_verbatim():
    history = _tool_history()
    history[2]["thinking"] = [THINKING, REDACTED]
    _, _, body = server_generation.build_anthropic_request(
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        history,
    )
    assert body["messages"][1]["content"] == [
        THINKING,
        REDACTED,
        {"type": "text", "text": "Reading it first."},
        {"type": "tool_use", "id": "call_1", "name": "read_chapter", "input": {"chapter": "第三章"}},
        {"type": "tool_use", "id": "call_2", "name": "read_chapter", "input": {"chapter": "第四章"}},
    ]
    # Signature bytes untouched.
    assert body["messages"][1]["content"][0]["signature"] == "EqQBCgIYAhIM1gbc+/=="


def test_anthropic_request_keeps_an_omitted_thinking_block_and_drops_malformed_ones():
    # display: "omitted" streams an EMPTY thinking string; the block still has
    # to go back (its signature is what the API checks).
    omitted = {"type": "thinking", "thinking": "", "signature": "sig"}
    _, _, body = server_generation.build_anthropic_request(
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "thinking": [
                    omitted,
                    {"type": "thinking", "thinking": "no signature"},
                    {"type": "redacted_thinking"},
                    "garbage",
                    {"type": "thinking", "thinking": "t", "signature": "s", "extra": "dropped"},
                ],
                "toolCalls": [{"id": "a", "name": "list_chapters", "argumentsText": "{}"}],
            },
        ],
    )
    assert body["messages"][1]["content"] == [
        omitted,
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "tool_use", "id": "a", "name": "list_chapters", "input": {}},
    ]


def test_gemini_request_puts_thought_signature_beside_its_function_call():
    history = _tool_history()
    # Parallel calls: only the FIRST carries a signature.
    history[2]["toolCalls"][0]["signature"] = "CiQBVt+/sig=="
    _, _, body = server_generation.build_gemini_request(
        {"model": "gemini-3-pro", "baseUrl": "https://g/v1beta", "apiKey": "k"},
        history,
    )
    assert body["contents"][1]["parts"] == [
        {"text": "Reading it first."},
        {
            "functionCall": {"name": "read_chapter", "args": {"chapter": "第三章"}},
            "thoughtSignature": "CiQBVt+/sig==",
        },
        {"functionCall": {"name": "read_chapter", "args": {"chapter": "第四章"}}},
    ]


def test_openai_request_ignores_reasoning_artifacts():
    history = _tool_history()
    history[2]["thinking"] = [THINKING]
    history[2]["toolCalls"][0]["signature"] = "sig"
    _, _, body = server_generation.build_openai_request(
        {"model": "grok-4", "baseUrl": "https://api.x.ai/v1", "apiKey": "k"}, history, "grok",
    )
    assistant = body["messages"][2]
    assert set(assistant) == {"role", "content", "tool_calls"}
    assert set(assistant["tool_calls"][0]) == {"id", "type", "function"}


KIMI_REASONING = {"type": "thinking", "thinking": "第三章先读。", "signature": ""}


def test_moonshot_request_sends_kimi_reasoning_fields_and_replays_reasoning_content():
    history = _tool_history()
    history[2]["thinking"] = [KIMI_REASONING]
    # kimi-k3: this app's low unless chosen (Kimi's own default is max), no thinking field,
    # the conversation pinned to its cache with a body field.
    _, _, body = server_generation.build_openai_request(
        {"model": "kimi-k3", "baseUrl": "https://api.moonshot.ai/v1", "apiKey": "k", "conversationId": "book-1"}, history, "moonshot",
    )
    assert body["reasoning_effort"] == "low" and "thinking" not in body and body["prompt_cache_key"] == "book-1"
    assert body["messages"][2]["reasoning_content"] == "第三章先读。"
    assert set(body["messages"][2]) == {"role", "reasoning_content", "content", "tool_calls"}
    assert body["stream_options"] == {"include_usage": True}
    _, _, xhigh = server_generation.build_openai_request(
        {"model": "kimi-k3", "baseUrl": "https://api.moonshot.ai/v1", "apiKey": "k", "reasoningEffort": "xhigh"}, history, "moonshot",
    )
    assert xhigh["reasoning_effort"] == "max"
    # kimi-k2.6: thinking kept across turns, no effort.
    _, _, k26 = server_generation.build_openai_request(
        {"model": "kimi-k2.6", "baseUrl": "https://api.moonshot.cn/v1", "apiKey": "k"}, history, "moonshot",
    )
    assert k26["thinking"] == {"type": "enabled", "keep": "all"} and "reasoning_effort" not in k26
    # A plain assistant reply carries it too; other providers never see the field.
    plain = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok", "thinking": [KIMI_REASONING]}, {"role": "user", "content": "go"}]
    _, _, k3 = server_generation.build_openai_request({"model": "kimi-k3", "baseUrl": "https://api.moonshot.ai/v1", "apiKey": "k"}, plain, "moonshot")
    assert k3["messages"][1] == {"role": "assistant", "reasoning_content": "第三章先读。", "content": "ok"}
    _, _, grok = server_generation.build_openai_request({"model": "grok-4", "baseUrl": "https://api.x.ai/v1", "apiKey": "k"}, plain, "grok")
    assert "reasoning_content" not in grok["messages"][1]


def test_moonshot_stream_keeps_reasoning_content_as_one_thinking_block():
    lines = [
        _sse({"choices": [{"delta": {"reasoning_content": "第三章"}}]}),
        _sse({"choices": [{"delta": {"reasoning_content": "先读。"}}]}),
        _sse({"choices": [{"delta": {"content": "好。"}}]}),
        _sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 8}}}),
        "data: [DONE]",
    ]
    job, events = _run_job_watching("moonshot", {"model": "kimi-k3", "baseUrl": "https://api.moonshot.ai/v1", "apiKey": "k"}, _FakeStreamResponse(lines=lines))
    assert job.buffer == "好。"
    assert job.thinking_blocks == [{"type": "thinking", "thinking": "第三章先读。", "signature": ""}]
    blocks = [e for e in events if e.get("type") == "thinking_block"]
    assert blocks == [{"type": "thinking_block", "index": 0, "block": KIMI_REASONING}]
    # The same deltas on another Chat Completions provider are shown live and not kept.
    job2, _ = _run_job_watching("ollama", {"model": "qwen3", "baseUrl": "http://localhost:11434/v1", "apiKey": "ollama-no-key"}, _FakeStreamResponse(lines=lines))
    assert job2.thinking_blocks == []


def _anthropic_thinking_lines():
    return [
        _sse({"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}}),
        _sse({"type": "content_block_start", "index": 0,
              "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        _sse({"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": "Need chapter "}}),
        _sse({"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": "3 first."}}),
        _sse({"type": "content_block_delta", "index": 0,
              "delta": {"type": "signature_delta", "signature": "EqQBCgIYAhIM1gbc+/=="}}),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse({"type": "content_block_start", "index": 1, "content_block": REDACTED}),
        _sse({"type": "content_block_stop", "index": 1}),
        _sse({"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}}),
        _sse({"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "Reading."}}),
        _sse({"type": "content_block_stop", "index": 2}),
        _sse({"type": "content_block_start", "index": 3,
              "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_chapter", "input": {}}}),
        _sse({"type": "content_block_delta", "index": 3,
              "delta": {"type": "input_json_delta", "partial_json": '{"chapter":"3"}'}}),
        _sse({"type": "content_block_stop", "index": 3}),
        _sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}}),
    ]


def _run_job_watching(provider, config, response):
    """Run a job with a live subscriber attached, return (job, live events)."""
    job = server_generation.GenerationJob("gen-reasoning", "alice", {})
    live: asyncio.Queue = asyncio.Queue()
    job.subscribers.add(live)

    @asynccontextmanager
    async def fake_stream(url, headers, body, read_timeout=None):
        yield response

    with patch.object(wc_providers.common, "_http_stream", fake_stream):
        asyncio.run(server_generation.run_job(job, provider, config, [{"role": "user", "content": "go"}]))
    events = []
    while not live.empty():
        events.append(live.get_nowait())
    return job, events


def test_anthropic_stream_publishes_completed_thinking_blocks_in_order():
    job, events = _run_job_watching(
        "anthropic",
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k", "tools": [READ_TOOL]},
        _FakeStreamResponse(lines=_anthropic_thinking_lines()),
    )
    assert job.status == "done", job.error
    assert job.thinking_blocks == [THINKING, REDACTED]
    published = [e for e in events if e["type"] == "thinking_block"]
    assert published == [
        {"type": "thinking_block", "index": 0, "block": THINKING},
        {"type": "thinking_block", "index": 1, "block": REDACTED},
    ]
    # Text and the tool call are unaffected, and thinking is not document text.
    assert job.buffer == "Reading."
    assert job.tool_calls == {3: {"id": "toolu_1", "name": "read_chapter", "arguments": '{"chapter":"3"}'}}
    # Both blocks completed BEFORE the tool call opened, the terminal event last.
    kinds = [e["type"] for e in events]
    assert kinds.index("thinking_block") < kinds.index("tool_call")
    assert kinds[-1] == "done"
    assert "thinking_block" not in server_generation.TERMINAL_EVENT_TYPES


def test_a_late_reader_receives_thinking_blocks_and_signatures_before_the_terminal_event():
    job, _ = _run_job_watching(
        "anthropic",
        {"model": "claude", "baseUrl": "https://api.anthropic.com/v1", "apiKey": "k"},
        _FakeStreamResponse(lines=_anthropic_thinking_lines()),
    )
    assert job.status == "done", job.error

    async def read_all():
        return [frame async for frame in server_generation._job_event_stream(job, 0)]

    events = _parse_sse_frames(asyncio.run(read_all()))
    assert [e["type"] for e in events] == [
        "attached", "thinking_block", "thinking_block", "tool_call", "delta", "done",
    ]
    assert events[1] == {"type": "thinking_block", "index": 0, "block": THINKING}
    assert events[2] == {"type": "thinking_block", "index": 1, "block": REDACTED}


def test_a_live_reader_keeps_streaming_after_a_thinking_block():
    async def scenario():
        job = server_generation.registry.create("alice", {})
        stream = server_generation._job_event_stream(job, 0)
        seen = [await anext(stream)]                 # attached
        job.note_thinking_block(dict(THINKING))
        seen.append(await anext(stream))             # must NOT end the stream
        job.append("Answer")
        seen.append(await anext(stream))
        job.finish("done")
        seen.append(await anext(stream))
        await stream.aclose()
        return seen

    events = _parse_sse_frames(asyncio.run(scenario()))
    assert [e["type"] for e in events] == ["attached", "thinking_block", "delta", "done"]


def _gemini_parallel_calls_chunk():
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [
                {"functionCall": {"name": "read_chapter", "args": {"chapter": "三"}},
                 "thoughtSignature": "CiQBVt+/sig=="},
                {"functionCall": {"name": "read_chapter", "args": {"chapter": "四"}}},
            ]},
            "finishReason": "STOP",
        }],
    }


def test_gemini_stream_carries_thought_signature_on_its_tool_call_event():
    job, events = _run_job_watching(
        "gemini",
        {"model": "gemini-3-pro", "baseUrl": "https://g/v1beta", "apiKey": "k", "tools": [READ_TOOL]},
        _FakeStreamResponse(lines=["data: " + json.dumps(_gemini_parallel_calls_chunk(), ensure_ascii=False)]),
    )
    assert job.status == "done", job.error
    calls = [e for e in events if e["type"] == "tool_call"]
    # One index per call: parallel calls on a shared index 0 would be merged
    # into a single call with unparseable arguments.
    assert calls == [
        {"type": "tool_call", "index": 0, "id": None, "name": "read_chapter",
         "text": '{"chapter":"三"}', "signature": "CiQBVt+/sig=="},
        {"type": "tool_call", "index": 1, "id": None, "name": "read_chapter", "text": '{"chapter":"四"}'},
    ]
    assert job.tool_calls == {
        0: {"id": None, "name": "read_chapter", "arguments": '{"chapter":"三"}', "signature": "CiQBVt+/sig=="},
        1: {"id": None, "name": "read_chapter", "arguments": '{"chapter":"四"}'},
    }

    # A reader that attaches later is replayed the signature with the call.
    async def read_all():
        return [frame async for frame in server_generation._job_event_stream(job, 0)]

    replayed = [e for e in _parse_sse_frames(asyncio.run(read_all())) if e["type"] == "tool_call"]
    assert replayed[0]["signature"] == "CiQBVt+/sig=="
    assert replayed[0]["replay"] is True
    assert "signature" not in replayed[1]


def test_note_tool_call_keeps_the_last_non_empty_signature():
    job = server_generation.GenerationJob("gen-sig", "alice", {})
    job.note_tool_call(0, None, "read_chapter", "{}", "first")
    job.note_tool_call(0, None, None, "", None)
    assert job.tool_calls[0]["signature"] == "first"
    job.note_tool_call(0, None, None, "", "second")
    assert job.tool_calls[0]["signature"] == "second"


def test_tool_choice_none_keeps_tools_but_forbids_a_new_call_per_provider():
    """The agentic run's final step (agent/run.ts STEP_LIMIT_NOTE): tools stay
    in the request — earlier calls reference them — but no new call is allowed.
    Mirrors streamOpenAI / streamAnthropic / streamGemini."""
    tools = [{"type": "function", "function": {"name": "read_chapter", "description": "d", "parameters": {"type": "object"}}}]
    config = {"apiKey": "k", "model": "m", "baseUrl": "https://x", "tools": tools, "toolChoice": "none"}
    messages = [{"role": "user", "content": "hi"}]

    openai_body = server_generation.build_openai_request(config, messages, "grok")[2]
    assert openai_body["tool_choice"] == "none"
    assert openai_body["tools"] == tools

    assert server_generation.build_anthropic_request(config, messages)[2]["tool_choice"] == {"type": "none"}
    assert server_generation.build_gemini_request(config, messages)[2]["toolConfig"] == {
        "functionCallingConfig": {"mode": "NONE"}
    }

    # Without it, nothing is forced.
    plain = dict(config)
    plain.pop("toolChoice")
    assert "tool_choice" not in server_generation.build_openai_request(plain, messages, "grok")[2]

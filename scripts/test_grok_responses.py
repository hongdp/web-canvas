"""grok over the xAI Responses API (server_generation._stream_responses).

Each step's reasoning comes back as an encrypted output item; the next step
sends it back unchanged, so the model keeps its plan from step to step.
Event shapes are the ones the live API sent (probed 2026-10-07).
"""
import json

import server_generation as gen
import wc_providers.common  # noqa: E402
from test_api_server import _FakeStreamResponse, _new_job, _run_job

CONFIG = {"apiKey": "k", "model": "grok-4.7", "baseUrl": "https://api.x.ai/v1", "conversationId": "book-1"}

REASONING = {"id": "rs_1", "summary": [], "type": "reasoning", "status": "completed", "encrypted_content": "CIPHER=="}
CALL = {"arguments": '{"chapter":2}', "call_id": "call-1", "name": "read_chapter", "type": "function_call", "id": "fc_1", "status": "completed"}


def _lines(*events):
    return [f"data: {json.dumps(e, ensure_ascii=False)}" for e in events] + ["data: [DONE]"]


def test_a_step_streams_text_reasoning_calls_and_keeps_its_output_items():
    job, captured = _new_job(), []
    _run_job(job, "grok", CONFIG, [{"role": "user", "content": "q"}], _FakeStreamResponse(lines=_lines(
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": "rs_1", "type": "reasoning", "summary": []}},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "先读第二章。"},
        {"type": "response.output_item.done", "output_index": 0, "item": REASONING},
        {"type": "response.output_item.added", "output_index": 1, "item": {"arguments": "", "call_id": "call-1", "name": "read_chapter", "type": "function_call", "id": "fc_1"}},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"chapter":2}'},
        {"type": "response.output_item.done", "output_index": 1, "item": CALL},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80}, "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 20}}}},
    )), captured)
    assert job.status == "done"
    assert job.reasoning_text == "先读第二章。"
    assert job.tool_calls[1] == {"id": "call-1", "name": "read_chapter", "arguments": '{"chapter":2}'}
    assert job.response_items == [REASONING, CALL]
    assert job.usage == {"promptTokens": 100, "completionTokens": 30, "cachedPromptTokens": 80, "cacheWriteTokens": 0, "reasoningTokens": 20}


def test_visible_text_goes_to_the_buffer():
    job, captured = _new_job(), []
    _run_job(job, "grok", CONFIG, [{"role": "user", "content": "q"}], _FakeStreamResponse(lines=_lines(
        {"type": "response.output_text.delta", "output_index": 1, "delta": "第二章"},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "写好了。"},
    )), captured)
    assert job.buffer == "第二章写好了。"


def test_the_final_arguments_win_when_the_deltas_disagree():
    job, captured = _new_job(), []
    _run_job(job, "grok", CONFIG, [{"role": "user", "content": "q"}], _FakeStreamResponse(lines=_lines(
        {"type": "response.output_item.added", "output_index": 0, "item": {"arguments": "", "call_id": "call-1", "name": "read_chapter", "type": "function_call"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"chap'},
        {"type": "response.output_item.done", "output_index": 0, "item": CALL},
    )), captured)
    assert job.tool_calls[0]["arguments"] == '{"chapter":2}'


def test_a_failed_response_fails_the_job():
    job, captured = _new_job(), []
    _run_job(job, "grok", CONFIG, [{"role": "user", "content": "q"}], _FakeStreamResponse(lines=_lines(
        {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "boom"}}},
    )), captured)
    assert job.status == "error"
    assert "boom" in (job.error or "")


def test_the_previous_steps_items_go_back_unchanged_and_results_answer_their_calls():
    _, _, body = gen.build_responses_request({**CONFIG, "maxOutputTokens": 4096, "reasoningEffort": "medium", "toolChoice": "none",
                                                   "tools": [{"type": "function", "function": {"name": "read_chapter", "description": "Read.", "parameters": {"type": "object"}}}]}, [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "toolCalls": [{"id": "call-1", "name": "read_chapter", "argumentsText": '{"chapter":2}'}],
         "responseItems": [REASONING, CALL]},
        {"role": "tool", "toolCallId": "call-1", "name": "read_chapter", "content": "第二章正文"},
    ])
    assert body["input"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        REASONING,
        CALL,
        {"type": "function_call_output", "call_id": "call-1", "output": "第二章正文"},
    ]
    assert body["tools"] == [{"type": "function", "name": "read_chapter", "description": "Read.", "parameters": {"type": "object"}}]
    assert body["tool_choice"] == "none"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["max_output_tokens"] == 4096


def test_an_assistant_turn_without_items_is_rebuilt_from_its_text_and_calls():
    # History from earlier turns, or a step that streamed before this API.
    _, _, body = gen.build_responses_request(CONFIG, [
        {"role": "assistant", "content": "好的。"},
        {"role": "assistant", "content": "", "toolCalls": [{"id": "c", "name": "grep", "argumentsText": '{"pattern":"x"}'}]},
    ])
    assert body["input"] == [
        {"role": "assistant", "content": "好的。"},
        {"type": "function_call", "call_id": "c", "name": "grep", "arguments": '{"pattern":"x"}'},
    ]


def test_images_become_input_image_parts():
    _, _, body = gen.build_responses_request(CONFIG, [{"role": "user", "content": "看图", "images": ["data:image/png;base64,AAA"]}])
    assert body["input"][0]["content"] == [
        {"type": "input_text", "text": "看图"},
        {"type": "input_text", "text": "\n[Image 1]:"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
    ]


def test_a_reconnecting_reader_is_given_the_items_again():
    job = _new_job()
    job.note_response_item(REASONING)
    job.status = "done"

    async def collect():
        out = []
        async for frame in gen._job_event_stream(job, 0):
            out.append(frame)
        return out

    import asyncio
    frames = asyncio.run(collect())
    events = [json.loads(line[6:]) for f in frames for line in f.splitlines() if line.startswith("data: ")]
    assert {"type": "response_item", "index": 0, "item": REASONING} in events


def test_a_history_message_replays_its_reasoning_then_its_text():
    _, _, body = gen.build_responses_request(CONFIG, [
        {"role": "user", "content": "pick a fruit"},
        {"role": "assistant", "content": "Ready.", "responseItems": [REASONING]},
        {"role": "user", "content": "which?"},
    ])
    assert body["input"] == [
        {"role": "user", "content": "pick a fruit"},
        REASONING,
        {"role": "assistant", "content": "Ready."},
        {"role": "user", "content": "which?"},
    ]


def test_a_rejected_ciphertext_is_retried_once_without_the_items():
    import asyncio
    from contextlib import asynccontextmanager
    bodies = []

    class _Response:
        calls = 0

        @property
        def status_code(self):
            return 400 if _Response.calls == 1 else 200

        async def aiter_lines(self):
            for line in _lines({"type": "response.output_text.delta", "output_index": 0, "delta": "ok"}):
                yield line

        async def aread(self):
            return b'{"code":"invalid-argument","error":"Could not decrypt the provided encrypted_content."}'

    @asynccontextmanager
    async def fake_stream(url, headers, body, read_timeout=None):
        _Response.calls += 1
        bodies.append(body)
        yield _Response()

    job = _new_job()
    from unittest.mock import patch
    with patch.object(wc_providers.common, "_http_stream", fake_stream):
        asyncio.run(gen.run_job(job, "grok", CONFIG, [
            {"role": "assistant", "content": "a", "responseItems": [{"type": "reasoning", "id": "rs", "encrypted_content": "stale"}]},
            {"role": "user", "content": "q"},
        ]))
    assert job.status == "done" and job.buffer == "ok"
    assert len(bodies) == 2
    assert "stale" in json.dumps(bodies[0]) and "stale" not in json.dumps(bodies[1])


def test_reasoning_items_round_trip_through_the_messages_columns():
    import api_server
    msg = {"reasoningItems": [REASONING]}
    stored = api_server._reasoning_items_json(msg)
    assert json.loads(stored) == [REASONING]

    class _Row(dict):
        def keys(self):
            return super().keys()
    assert api_server._reasoning_items_field(_Row(reasoning_items=stored)) == {"reasoningItems": [REASONING]}
    assert api_server._reasoning_items_field(_Row(reasoning_items=None)) == {}
    assert api_server._reasoning_items_field(_Row()) == {}
    assert api_server._reasoning_items_json({}) is None


def test_init_db_adds_the_reasoning_items_column(tmp_path, monkeypatch):
    import sqlite3
    import server_db
    monkeypatch.setattr(server_db, "DB_PATH", str(tmp_path / "m.db"))
    server_db.init_db()
    conn = sqlite3.connect(server_db.DB_PATH)
    try:
        assert "reasoning_items" in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    finally:
        conn.close()

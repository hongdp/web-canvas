"""A generation job that fails (agentic_chat_loop.md §0.10): what is sent
again before any output, how a failure is classified, and xAI's repetition
detector on a grok stream."""
import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx

import server_generation as gen
import wc_providers.common  # noqa: E402
from test_api_server import _FakeStreamResponse, _new_job, _run_job

GROK = {"apiKey": "k", "model": "grok-4.6", "baseUrl": "https://api.x.ai/v1"}


def _lines(*events):
    return [f"data: {json.dumps(e, ensure_ascii=False)}" for e in events] + ["data: [DONE]"]


def _scripted_dispatch(outcomes):
    """_dispatch_provider replaced: each call takes the next outcome, an exception or text."""
    calls = []

    async def dispatch(job, provider, config, messages):
        calls.append(config)
        outcome = outcomes.pop(0)
        if isinstance(outcome, tuple):
            text, exc = outcome
            if text:
                job.append(text)
            raise exc
        if isinstance(outcome, BaseException):
            raise outcome
        job.append(outcome)
        return {"promptTokens": 10, "completionTokens": 2}
    return dispatch, calls


def _drive(job, outcomes, provider="openai"):
    dispatch, calls = _scripted_dispatch(outcomes)
    events = asyncio.Queue()
    job.subscribers.add(events)
    with patch.object(gen, "_dispatch_provider", dispatch):
        asyncio.run(gen.run_job(job, provider, {"model": "m"}, [{"role": "user", "content": "q"}]))
    seen = []
    while not events.empty():
        seen.append(events.get_nowait())
    return calls, seen


def test_a_call_that_failed_before_output_is_sent_again_until_it_succeeds():
    job = _new_job()
    calls, events = _drive(job, [gen.ProviderError("x (503): busy", status=503), gen.ProviderError("x (429): slow", status=429, retry_after=3),
                                 httpx.ConnectError("refused"), "ok"])
    assert job.status == "done" and job.buffer == "ok"
    assert len(calls) == 4 and job.retries == 3
    assert [e["attempt"] for e in events if e["type"] == "retry"] == [1, 2, 3]


def test_a_rejection_retrying_cannot_fix_is_not_sent_again():
    for exc, kind in [(gen.ProviderError("x (400): bad tool", status=400), "fatal"),
                      (gen.ProviderError("x (401): key", status=401), "fatal"),
                      (gen.ProviderError("x (400): This model's maximum context length is 8192 tokens", status=400), "context")]:
        job = _new_job()
        calls, _ = _drive(job, [exc])
        assert len(calls) == 1 and job.status == "error" and job.error_kind == kind


def test_retries_stop_after_four():
    job = _new_job()
    calls, _ = _drive(job, [gen.ProviderError("x (502): gw", status=502)] * 5)
    assert len(calls) == 5 and job.status == "error" and job.error_kind == "transient" and not job.failed_after_output


def test_a_call_that_broke_after_streaming_is_not_sent_again_and_says_so():
    job = _new_job()
    calls, _ = _drive(job, [("half a sentence", httpx.ReadError("connection reset"))])
    assert len(calls) == 1
    assert job.status == "error" and job.error_kind == "transient" and job.failed_after_output


def test_an_idle_stream_fails_without_a_retry():
    job = _new_job()
    calls, _ = _drive(job, [httpx.ReadTimeout("no bytes")], provider="grok")
    assert len(calls) == 1 and job.error_kind == "idle"
    assert "sent nothing for 180 s" in job.error


def test_grok_streams_go_idle_after_180_seconds():
    captured = []

    def stub(timeouts):
        @asynccontextmanager
        async def fake_stream(url, headers, body, read_timeout=None):
            timeouts.append(read_timeout)
            yield _FakeStreamResponse(lines=_lines({"type": "response.output_text.delta", "output_index": 0, "delta": "ok"}),
                                      text_chunks=['data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'])
        return fake_stream
    with patch.object(wc_providers.common, "_http_stream", stub(captured)):
        asyncio.run(gen.run_job(_new_job(), "grok", GROK, [{"role": "user", "content": "q"}]))
    assert captured == [180.0]


def test_the_repetition_check_is_asked_for_and_its_failure_classified_as_the_loop():
    captured = []
    response = _FakeStreamResponse(lines=_lines(
        {"type": "response.output_text.delta", "output_index": 0, "delta": "The lantern swung. " * 20},
        {"type": "response.doom_loop_check", "doom_loop_check": {"triggers": ["tail_repetition:16@response"]}},
        {"type": "error", "code": None, "message": "Internal error during token generation"},
    ))
    job = _new_job()
    _run_job(job, "grok", {**GROK, "loopCheck": True}, [{"role": "user", "content": "q"}], response, captured)
    assert captured[0]["headers"]["x-grok-doom-loop-check"] == "1024"
    assert captured[0]["headers"]["x-grok-exact-repetition-check"] == "64"
    assert job.status == "error" and job.error_kind == "repetition" and job.loop_triggers == ["tail_repetition:16@response"]
    assert job.failed_after_output


def test_without_the_check_no_headers_are_sent_and_a_server_error_mid_stream_is_transient():
    captured = []
    response = _FakeStreamResponse(lines=_lines(
        {"type": "response.output_text.delta", "output_index": 0, "delta": "part"},
        {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "Internal error during token generation"}}},
    ))
    job = _new_job()
    _run_job(job, "grok", GROK, [{"role": "user", "content": "q"}], response, captured)
    assert "x-grok-doom-loop-check" not in captured[0]["headers"]
    assert job.error_kind == "transient" and job.failed_after_output


def test_a_completed_response_with_a_trigger_is_kept():
    captured = []
    response = _FakeStreamResponse(lines=_lines(
        {"type": "response.output_text.delta", "output_index": 0, "delta": "refrain, refrain"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 3},
                                                    "doom_loop_check": {"triggers": ["exact_repetition:64x3@response"]}}},
    ))
    job = _new_job()
    _run_job(job, "grok", {**GROK, "loopCheck": True}, [{"role": "user", "content": "q"}], response, captured)
    assert job.status == "done" and job.buffer == "refrain, refrain" and job.loop_triggers == ["exact_repetition:64x3@response"]


GEMINI_QUOTA = json.dumps({"error": {"code": 429, "message": "You exceeded your current quota, please check your plan and billing details.",
                                     "status": "RESOURCE_EXHAUSTED",
                                     "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "10830s"}]}}, indent=2)


def test_a_json_error_body_becomes_its_message_and_googles_retry_delay_is_read():
    class Response:
        status_code = 429
        headers = {}
    err = wc_providers.common._http_error("Gemini", Response(), GEMINI_QUOTA)
    assert str(err) == "Gemini API error (429): You exceeded your current quota, please check your plan and billing details. (retry after 10830s)"
    assert err.retry_after == 10830.0
    plain = wc_providers.common._http_error("xAI", Response(), "upstream busy")
    assert str(plain) == "xAI API error (429): upstream busy" and plain.retry_after is None


def test_a_wait_longer_than_a_retry_may_sleep_is_not_retried():
    """2026-10-17: a three-hour daily quota was retried four times in 17 s, then the run paused anyway."""
    job = _new_job()
    calls, events = _drive(job, [gen.ProviderError("Gemini API error (429): quota", status=429, retry_after=10830)] * 5, provider="gemini")
    assert len(calls) == 1 and job.status == "error" and job.error_kind == "transient"
    assert not [e for e in events if e["type"] == "retry"]

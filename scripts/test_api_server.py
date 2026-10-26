import pytest
from unittest.mock import patch
import sys
import os

# Add scripts directory to path to import api_server and its sibling modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import api_server
import server_auth
import server_db
import server_generation
import wc_providers.common  # noqa: E402

# NOTE: patches must target the module that OWNS the state the executing code
# reads (server_db.DB_PATH, server_auth.SESSIONS_FILE).
# Patching a name re-exported on api_server would not affect those code paths.

def test_init_db_migrates_summary_columns(tmp_path):
    """A pre-summary database gains the summary columns on init_db, and
    existing rows survive with NULL summaries."""
    db_file = tmp_path / "metadata.db"

    # Build the OLD schema (documents without summary columns) + one row.
    import sqlite3
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
        CREATE TABLE books (
            id TEXT NOT NULL, username TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'Untitled Book',
            active_document_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (username, id)
        );
        CREATE TABLE documents (
            id TEXT NOT NULL, username TEXT NOT NULL, book_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'Untitled Chapter',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (username, book_id, id)
        );
    """)
    conn.execute(
        "INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at) VALUES ('d1', 'u', 'b', 'Ch 1', 0, 't', 't')"
    )
    conn.commit()
    conn.close()

    with patch.object(server_db, "DB_PATH", str(db_file)):
        server_db.init_db()

        conn = server_db.get_db()
        try:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
            assert "summary" in cols
            assert "summary_content_hash" in cols

            row = conn.execute("SELECT * FROM documents WHERE id = 'd1'").fetchone()
            assert row["summary"] is None

            # Round-trip: a summary written the way update_document writes it.
            conn.execute(
                "UPDATE documents SET summary = ?, summary_content_hash = ? WHERE id = 'd1'",
                ("A short summary.", "abc123"),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM documents WHERE id = 'd1'").fetchone()
            assert row["summary"] == "A short summary."
            assert row["summary_content_hash"] == "abc123"
        finally:
            conn.close()

        # Idempotent: running init_db again must not fail on existing columns.
        server_db.init_db()


def test_get_book_returns_document_metadata_with_summaries(tmp_path, monkeypatch):
    """GET /api/books/{id} must not 500 on the document metadata SELECT.

    Regression: the response reads d["summary"] / d["summary_content_hash"],
    but the SELECT listed only id/title/sort_order/created_at/updated_at.
    sqlite3.Row raises IndexError for an unselected column, so every book
    switch returned 500 and the UI silently refused to change books.
    """
    import asyncio
    import json as _json
    from datetime import datetime, timedelta, timezone
    from starlette.requests import Request

    monkeypatch.setattr(server_db, "DB_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setattr(server_auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    server_db.init_db()

    now = datetime.now(timezone.utc).isoformat()
    conn = server_db.get_db()
    try:
        conn.execute(
            "INSERT INTO books (id, username, title, active_document_id, created_at, updated_at)"
            " VALUES ('book-1', 'alice', 'My Book', 'doc-1', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at,"
            " summary, summary_content_hash)"
            " VALUES ('doc-1', 'alice', 'book-1', 'Chapter 1', 0, ?, ?, 'A short summary.', 'hash-1')",
            (now, now),
        )
        # A chapter that was never summarized must still come back (NULL columns).
        conn.execute(
            "INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at)"
            " VALUES ('doc-2', 'alice', 'book-1', 'Chapter 2', 1, ?, ?)",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with open(server_auth.SESSIONS_FILE, "w", encoding="utf-8") as f:
        _json.dump({"sess-1": {"username": "alice", "expiresAt": expires}}, f)

    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/books/book-1",
        "headers": [(b"cookie", b"web_canvas_session=sess-1")],
        "query_string": b"",
    })

    result = asyncio.run(api_server.get_book(request, "book-1"))

    assert result["bookTitle"] == "My Book"
    assert [d["id"] for d in result["documents"]] == ["doc-1", "doc-2"]
    assert result["documents"][0]["summary"] == "A short summary."
    assert result["documents"][0]["summaryContentHash"] == "hash-1"
    assert result["documents"][1]["summary"] is None


# ==============================================================================
# Resumable generation jobs (docs/features/resumable_generation.md)
#
# The HTTP layer is stubbed by patching wc_providers.common._http_stream — the
# module that OWNS it. Patching an alias elsewhere would not affect the
# executing code.
# ==============================================================================

import asyncio
import json as _json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from starlette.requests import Request as _StarletteRequest

from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _clean_generation_registry():
    server_generation.registry.clear()
    yield
    server_generation.registry.clear()


class _FakeStreamResponse:
    """Stands in for an httpx streaming response."""

    def __init__(self, status_code=200, lines=None, text_chunks=None, error_body=b""):
        self.status_code = status_code
        self._lines = lines or []
        self._text_chunks = text_chunks or []
        self._error_body = error_body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aiter_text(self):
        for chunk in self._text_chunks:
            yield chunk

    async def aread(self):
        return self._error_body


def _stub_http_stream(captured, response):
    @asynccontextmanager
    async def fake_stream(url, headers, body, read_timeout=None):
        captured.append({"url": url, "headers": headers, "body": body})
        yield response
    return fake_stream


def _run_job(job, provider, config, messages, response, captured):
    with patch.object(wc_providers.common, "_http_stream", _stub_http_stream(captured, response)):
        asyncio.run(server_generation.run_job(job, provider, config, messages))


def _new_job(username="alice", meta=None):
    return server_generation.GenerationJob("gen-test", username, meta or {})


def _sse_payloads(chunks):
    """Parse rendered SSE frames into event dicts, dropping keep-alives."""
    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(_json.loads(line[6:]))
    return events


# ── Registry lifecycle ────────────────────────────────────────────────────────

class _FakeRequest:
    """Minimal stand-in: the models endpoint only reads auth + the JSON body."""
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.fixture
def _stub_models_auth(monkeypatch):
    """Auth is covered by its own tests; these exercise the listing logic.
    NOT autouse — patching it module-wide broke the cross-user 404 test."""
    monkeypatch.setattr(server_generation, "get_authenticated_username", lambda request: "alice")


def test_models_endpoint_normalizes_both_dialects(monkeypatch, _stub_models_auth):
    """The browser cannot reach a plain-http local endpoint from an HTTPS page
    (mixed content), so the backend lists models on its behalf."""
    from contextlib import asynccontextmanager

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    async def run(payload):
        captured = {}

        class _Client:
            def __init__(self, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url):
                captured["url"] = url
                return _Resp(payload)

        monkeypatch.setattr(server_generation.httpx, "AsyncClient", _Client)
        request = _FakeRequest({"baseUrl": "http://127.0.0.1:8090/v1"})
        result = await server_generation.list_provider_models(request)
        return result, captured

    # Ollama's own shape. (contextWindows arrived with the cache-first context
    # work: llama.cpp states n_ctx on /v1/models, and only the server knows it.)
    result, captured = asyncio.run(run({"models": [{"name": "local-model-27b"}]}))
    assert result == {"models": ["local-model-27b"], "contextWindows": {}}
    assert captured["url"] == "http://127.0.0.1:8090/v1/models"

    # OpenAI's shape.
    result, _ = asyncio.run(run({"data": [{"id": "local-a"}, {"id": "local-b"}]}))
    assert result == {"models": ["local-a", "local-b"], "contextWindows": {}}


def test_models_endpoint_refuses_non_local_hosts(_stub_models_auth):
    """Without this the endpoint is an open SSRF proxy: the caller picks the URL."""
    for base in ("http://evil.example.com/v1", "https://169.254.169.254/latest", "file:///etc/passwd"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server_generation.list_provider_models(_FakeRequest({"baseUrl": base})))
        assert exc.value.status_code == 400


def test_models_endpoint_reports_an_unreachable_server_as_empty(_stub_models_auth):
    """No local server running is the normal case, not an error."""
    class _Boom:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            raise OSError("connection refused")

    original = server_generation.httpx.AsyncClient
    server_generation.httpx.AsyncClient = _Boom
    try:
        result = asyncio.run(server_generation.list_provider_models(
            _FakeRequest({"baseUrl": "http://127.0.0.1:9999/v1"})
        ))
    finally:
        server_generation.httpx.AsyncClient = original
    assert result == {"models": []}


def test_generation_job_lifecycle_buffers_and_finishes():
    job = server_generation.registry.create("alice", {"kind": "chat"})
    assert job.status == "running"
    assert job.summary()["meta"] == {"kind": "chat"}

    job.append("Hello ")
    job.append("world")
    assert job.buffer == "Hello world"
    assert job.length == 11
    assert job.summary()["length"] == 11

    job.finish("done", usage={"promptTokens": 10, "completionTokens": 3})
    assert job.status == "done"
    assert job.finished_at is not None
    assert job.terminal_event() == {
        "type": "done",
        "offset": 11,
        "usage": {"promptTokens": 10, "completionTokens": 3},
        "status": "done",
        "truncatedBuffer": False,
    }

    # Terminal state is sticky and the buffer stays readable.
    assert job.finish("error", error="late") is False
    job.append("ignored")
    assert job.buffer == "Hello world"


def test_app_logging_lets_info_through_uvicorns_warning_root():
    """uvicorn.run(log_level="warning") sets the ROOT logger to WARNING, so
    every logger.info in this app was dropped — including the one number that
    diagnoses a slow first token."""
    import logging
    import sys
    import api_server

    app_logger = logging.getLogger("web_canvas")
    prior_level, prior_handlers = app_logger.level, list(app_logger.handlers)
    app_logger.handlers.clear()
    try:
        logging.getLogger().setLevel(logging.WARNING)   # what uvicorn does
        api_server._configure_app_logging()

        assert app_logger.isEnabledFor(logging.INFO)
        assert app_logger.handlers, "own handler required — the root has none at INFO"
        # A piped stdout is block-buffered: without line buffering the lines
        # sit in an 8KB buffer and never reach app.log.
        assert sys.stdout.line_buffering or app_logger.handlers[0].stream is sys.stderr
        # Not double-logged through whatever uvicorn installs on the root.
        assert app_logger.propagate is False
        # Child loggers (server_generation) inherit the raised level.
        assert logging.getLogger("web_canvas.generation").isEnabledFor(logging.INFO)
    finally:
        app_logger.handlers[:] = prior_handlers
        app_logger.setLevel(prior_level)


def test_generation_records_time_to_first_token_once():
    """The one number that separates provider prefill from our own latency."""
    job = server_generation.registry.create("alice", {})
    assert job.first_delta_latency is None

    job.append("first")
    latency = job.first_delta_latency
    assert latency is not None and latency >= 0

    # Later deltas must not overwrite it — it is time to FIRST token.
    job.append("second")
    assert job.first_delta_latency == latency

    # An empty delta is not a token.
    fresh = server_generation.registry.create("alice", {})
    fresh.append("")
    assert fresh.first_delta_latency is None


def test_generation_buffer_cap_marks_truncated_but_keeps_streaming():
    job = _new_job()
    cap = server_generation.MAX_BUFFER_CHARS
    job.append("a" * (cap - 5))
    assert job.truncated_buffer is False

    job.append("b" * 20)
    assert job.truncated_buffer is True
    assert len(job.buffer) == cap
    # Live subscribers still advance: total length counts every produced char.
    assert job.length == cap + 15
    assert job.summary()["truncatedBuffer"] is True


# ── Replay from an offset ─────────────────────────────────────────────────────

def test_generation_stream_announces_itself_before_any_token():
    """Headers flush with the first body chunk, so a slow model used to leave
    the client with 15s of dead air (SSE_HEARTBEAT_SECONDS) and no way to tell
    a live stream from a stalled one."""
    async def scenario():
        job = server_generation.registry.create("alice", {})
        job.append("late text")
        job.finish("done")
        return [ev async for ev in server_generation._job_event_stream(job, 0)]

    events = _sse_payloads(asyncio.run(scenario()))
    assert events[0] == {"type": "attached", "offset": 0, "status": "done"}
    # The announcement carries no text and advances no offset.
    assert events[1] == {"type": "delta", "text": "late text", "offset": 9}


def test_generation_replays_tool_calls_to_a_reconnecting_reader():
    """Tool arguments are not document text, so they are kept out of the replay
    buffer — but a reader that reconnects after a reload has to see them, or
    the edit is lost. They are replayed WHOLE, since there is no offset to
    resume a tool call from."""
    async def scenario():
        job = server_generation.registry.create("alice", {})
        job.note_tool_call(0, "call_1", "update_document", '{"html": "<p>a')
        job.note_tool_call(0, None, None, 'b</p>"}')
        job.append("Done.")

        # A fresh reader, as after a reload.
        stream = server_generation._job_event_stream(job, 0)
        seen = [await anext(stream) for _ in range(3)]
        await stream.aclose()
        return seen

    events = _sse_payloads(asyncio.run(scenario()))
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["attached", "tool_call"]

    call = events[1]
    assert call["name"] == "update_document"
    assert call["text"] == '{"html": "<p>ab</p>"}'   # whole, not a fragment
    assert call["replay"] is True


def test_generation_tool_calls_stay_out_of_the_replay_buffer():
    """Putting them in `buffer` would shift every delta offset and push JSON
    into the document."""
    job = _new_job()
    job.note_tool_call(0, "c", "update_document", '{"html": "<p>x</p>"}')

    assert job.buffer == ""
    assert job.length == 0
    assert job.tool_calls[0]["arguments"] == '{"html": "<p>x</p>"}'


def test_generation_stream_survives_a_reasoning_event():
    """Regression: the reader treated every non-delta event as terminal, so the
    FIRST reasoning delta closed the stream. The job kept generating while every
    client reported "disconnected before completion"."""
    async def scenario():
        job = server_generation.registry.create("alice", {})
        stream = server_generation._job_event_stream(job, 0)
        seen = [await anext(stream)]            # attached
        job.note_reasoning("weighing options")
        seen.append(await anext(stream))        # reasoning — must NOT end it
        job.append("Answer")
        seen.append(await anext(stream))        # the stream is still live
        job.finish("done")
        seen.append(await anext(stream))
        await stream.aclose()
        return seen

    events = _sse_payloads(asyncio.run(scenario()))
    assert [e["type"] for e in events] == ["attached", "reasoning", "delta", "done"]
    assert events[1]["text"] == "weighing options"
    assert events[2] == {"type": "delta", "text": "Answer", "offset": 6}


def test_generation_stream_replay_has_no_duplicates_and_no_gaps():
    """A reader that dies mid-stream and re-attaches at its last offset sees
    every character exactly once."""
    async def scenario():
        job = server_generation.registry.create("alice", {})
        job.append("Hello ")

        first = server_generation._job_event_stream(job, 0)
        seen = []
        seen.append(await anext(first))          # "attached" announcement
        seen.append(await anext(first))          # replay of "Hello "
        job.append("brave ")
        seen.append(await anext(first))          # live delta
        await first.aclose()                     # tab dies here

        # Generation continues with nobody listening.
        job.append("world")
        job.finish("done", usage={"promptTokens": 1, "completionTokens": 2})

        events = _sse_payloads(seen)
        last_offset = events[-1]["offset"]

        second = server_generation._job_event_stream(job, last_offset)
        rest = [ev async for ev in second]

        return events + _sse_payloads(rest)

    events = asyncio.run(scenario())

    # Every attach announces itself first; the announcement carries no text.
    assert [e["type"] for e in events if e["type"] == "attached"] == ["attached", "attached"]
    deltas = [e for e in events if e["type"] == "delta"]
    assert "".join(d["text"] for d in deltas) == "Hello brave world"
    # Offsets are monotonic and equal the buffer length after each event.
    assert [d["offset"] for d in deltas] == [6, 12, 17]
    assert events[-1] == {
        "type": "done",
        "offset": 17,
        "usage": {"promptTokens": 1, "completionTokens": 2},
        "status": "done",
        "truncatedBuffer": False,
    }


def test_generation_stream_attaching_to_finished_job_replays_everything():
    async def scenario():
        job = server_generation.registry.create("alice", {})
        job.append("done text")
        job.finish("error", error="boom")
        return [ev async for ev in server_generation._job_event_stream(job, 0)]

    events = _sse_payloads(asyncio.run(scenario()))
    assert events[0]["type"] == "attached"
    assert events[1] == {"type": "delta", "text": "done text", "offset": 9}
    assert events[2]["type"] == "error"
    assert events[2]["message"] == "boom"
    assert events[2]["offset"] == 9


def test_generation_stream_from_current_offset_replays_nothing():
    async def scenario():
        job = server_generation.registry.create("alice", {})
        job.append("already rendered")
        job.finish("done")
        return [ev async for ev in server_generation._job_event_stream(job, 16)]

    events = _sse_payloads(asyncio.run(scenario()))
    assert [e["type"] for e in events] == ["attached", "done"]


# ── Abort ─────────────────────────────────────────────────────────────────────

def test_generation_abort_stops_stream_and_keeps_partial_text():
    job = _new_job()
    captured = []

    lines = [
        'data: {"choices":[{"delta":{"content":"partial"}}]}',
        'data: {"choices":[{"delta":{"content":" more"}}]}',
        "data: [DONE]",
    ]

    class _AbortingResponse(_FakeStreamResponse):
        async def aiter_lines(self):
            for line in self._lines:
                yield line
                # The abort endpoint fires between two provider deltas.
                job.abort()

    _run_job(
        job, "openai",
        # An OpenAI-compatible server: Chat Completions (api.openai.com itself is on Responses).
        {"apiKey": "sk-test", "model": "gpt-4o", "baseUrl": "https://llm.example.com/v1"},
        [{"role": "user", "content": "hi"}],
        _AbortingResponse(lines=lines), captured,
    )

    assert job.status == "aborted"
    assert job.buffer == "partial"
    assert job.terminal_event()["status"] == "aborted"


def test_generation_abort_is_idempotent_and_ignores_finished_jobs():
    job = _new_job()
    job.finish("done")
    assert job.abort() is False
    assert job.status == "done"


# ── Retention and eviction ────────────────────────────────────────────────────

def test_generation_finished_jobs_expire_after_retention_window():
    fresh = server_generation.registry.create("alice", {})
    stale = server_generation.registry.create("alice", {})
    running = server_generation.registry.create("alice", {})

    fresh.finish("done")
    stale.finish("done")
    # The short window applies to results somebody has received; an unread one
    # is kept far longer (see the undelivered-result tests below).
    stale.result_delivered = True
    stale.finished_at -= server_generation.FINISHED_JOB_TTL_SECONDS + 1

    server_generation.registry.prune()

    ids = {j.job_id for j in server_generation.registry.list_for_user("alice")}
    assert fresh.job_id in ids
    assert running.job_id in ids
    assert stale.job_id not in ids


def test_generation_evicts_oldest_finished_job_over_the_cap():
    reg = server_generation.registry
    base = server_generation._monotonic()
    jobs = []
    for i in range(server_generation.MAX_JOBS_PER_USER):
        job = reg.create("alice", {"n": i})
        job.finish("done")
        # Deterministic ordering, still inside the retention window.
        job.finished_at = base + i * 0.001
        jobs.append(job)

    newest = reg.create("alice", {"n": "new"})

    ids = {j.job_id for j in reg.list_for_user("alice")}
    assert len(ids) == server_generation.MAX_JOBS_PER_USER
    assert jobs[0].job_id not in ids   # oldest finished evicted
    assert jobs[1].job_id in ids
    assert newest.job_id in ids


def test_generation_never_evicts_a_running_job():
    reg = server_generation.registry
    running = [reg.create("alice", {}) for _ in range(server_generation.MAX_JOBS_PER_USER)]
    extra = reg.create("alice", {})

    # Nothing is evictable, so the cap is exceeded rather than killing work.
    assert len(reg.list_for_user("alice")) == server_generation.MAX_JOBS_PER_USER + 1

    finished = running[0]
    finished.finish("done")
    reg.create("alice", {})

    ids = {j.job_id for j in reg.list_for_user("alice")}
    assert finished.job_id not in ids
    assert extra.job_id in ids
    assert all(j.job_id in ids for j in running[1:])


# ── Undelivered results ───────────────────────────────────────────────────────
# A job whose only reader was gone when it finished (phone locked, tab
# discarded) used to be deleted ten minutes later, read or not: the tokens were
# spent and the reply existed nowhere. Retention now asks whether the result
# ever reached a reader, not only when the job finished.

def test_generation_keeps_an_unread_result_past_the_short_window():
    reg = server_generation.registry
    unread = reg.create("alice", {})
    unread.finish("done")
    unread.finished_at -= server_generation.FINISHED_JOB_TTL_SECONDS + 1

    reg.prune()
    assert unread.job_id in {j.job_id for j in reg.list_for_user("alice")}

    # ...but not forever: memory is bounded in time as well as by the cap.
    unread.finished_at -= server_generation.UNDELIVERED_JOB_TTL_SECONDS
    reg.prune()
    assert unread.job_id not in {j.job_id for j in reg.list_for_user("alice")}


def test_generation_marks_a_result_delivered_once_a_reader_gets_the_terminal_event():
    async def scenario():
        replayed = server_generation.registry.create("alice", {})
        replayed.append("answer")
        replayed.finish("done")
        assert replayed.result_delivered is False
        _ = [ev async for ev in server_generation._job_event_stream(replayed, 0)]

        live = server_generation.registry.create("alice", {})
        stream = server_generation._job_event_stream(live, 0)
        await anext(stream)                      # attached
        live.append("answer")
        live.finish("done")
        _ = [ev async for ev in stream]          # delta + terminal, read to the end
        return replayed, live

    replayed, live = asyncio.run(scenario())
    assert replayed.result_delivered is True     # replay of a finished job
    assert live.result_delivered is True         # terminal event seen live
    assert live.summary()["delivered"] is True


def test_generation_reader_that_leaves_early_does_not_count_as_delivery():
    """The flag is set on the line after the terminal yield, which a vanished
    reader never reaches — its generator is closed AT the yield."""
    async def scenario():
        job = server_generation.registry.create("alice", {})
        stream = server_generation._job_event_stream(job, 0)
        await anext(stream)                      # attached
        job.append("half an ans")
        await anext(stream)                      # the delta
        await stream.aclose()                    # tab discarded mid-turn
        job.append("wer")
        job.finish("done")                       # finishes with nobody reading
        return job

    job = asyncio.run(scenario())
    assert job.status == "done"
    assert job.result_delivered is False
    assert job.summary()["delivered"] is False


def test_generation_abort_does_not_leave_an_unread_result_behind():
    """Stop drops the reader before the terminal event exists, so no reader
    ever receives it — yet the result was discarded on purpose and must not be
    kept a day as unread."""
    job = _new_job()
    job.append("partial")
    job.abort()
    assert job.result_delivered is True


def test_generation_evicts_delivered_results_before_unread_ones():
    reg = server_generation.registry
    base = server_generation._monotonic()

    unread = reg.create("alice", {"n": "unread"})
    unread.finish("done")
    unread.finished_at = base - 100              # the OLDEST finished job

    for i in range(server_generation.MAX_JOBS_PER_USER - 1):
        job = reg.create("alice", {"n": i})
        job.finish("done")
        job.result_delivered = True
        job.finished_at = base + i * 0.001

    reg.create("alice", {"n": "new"})            # pushes the user over the cap

    ids = {j.job_id for j in reg.list_for_user("alice")}
    assert len(ids) == server_generation.MAX_JOBS_PER_USER
    # Age alone would have evicted it; a copy of the others exists on a client.
    assert unread.job_id in ids


# ── Per-user isolation ────────────────────────────────────────────────────────

def test_generation_jobs_are_scoped_to_the_creating_user():
    job = server_generation.registry.create("alice", {})
    assert server_generation.registry.get("alice", job.job_id) is job
    assert server_generation.registry.get("bob", job.job_id) is None
    assert server_generation.registry.list_for_user("bob") == []


def test_generation_other_users_job_id_404s(tmp_path, monkeypatch):
    monkeypatch.setattr(server_auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with open(server_auth.SESSIONS_FILE, "w", encoding="utf-8") as f:
        _json.dump({
            "sess-alice": {"username": "alice", "expiresAt": expires},
            "sess-bob": {"username": "bob", "expiresAt": expires},
        }, f)

    job = server_generation.registry.create("alice", {})

    def _request(session_id, path):
        return _StarletteRequest({
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"cookie", f"web_canvas_session={session_id}".encode())],
            "query_string": b"",
        })

    # Alice can read her own job.
    response = asyncio.run(server_generation.stream_generation(
        _request("sess-alice", f"/api/generate/{job.job_id}/stream"), job.job_id, 0))
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    # Bob must not learn that the job exists.
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(server_generation.stream_generation(
            _request("sess-bob", f"/api/generate/{job.job_id}/stream"), job.job_id, 0))
    assert excinfo.value.status_code == 404

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(server_generation.abort_generation(
            _request("sess-bob", f"/api/generate/{job.job_id}/abort"), job.job_id))
    assert excinfo.value.status_code == 404

    # ...and it stays out of his active list.
    assert asyncio.run(server_generation.list_active_generations(_request("sess-bob", "/api/generate/active"))) == []
    active = asyncio.run(server_generation.list_active_generations(_request("sess-alice", "/api/generate/active")))
    assert [j["jobId"] for j in active] == [job.job_id]
    assert job.status == "running"  # bob's abort attempt did nothing


# ── Provider request building (all five providers, stubbed HTTP) ──────────────

def test_generation_openai_request_and_usage():
    job = _new_job()
    captured = []
    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":30,"completion_tokens":2,'
        '"prompt_tokens_details":{"cached_tokens":12}}}',
        "data: [DONE]",
    ]
    _run_job(
        job, "openai",
        {
            "apiKey": "sk-secret", "model": "gpt-4o",
            # An OpenAI-compatible server: Chat Completions (api.openai.com itself is on Responses).
            "baseUrl": "https://llm.example.com/v1", "maxOutputTokens": 4096,
        },
        [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Describe", "images": ["data:image/png;base64,AAA"]},
        ],
        _FakeStreamResponse(lines=lines), captured,
    )

    request = captured[0]
    assert request["url"] == "https://llm.example.com/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer sk-secret"
    body = request["body"]
    assert body["model"] == "gpt-4o"
    assert body["stream"] is True
    assert body["max_tokens"] == 4096
    assert body["stream_options"] == {"include_usage": True}
    # System messages stay inline for OpenAI-compatible providers.
    assert body["messages"][0] == {"role": "system", "content": "You are helpful"}
    assert body["messages"][1]["content"] == [
        {"type": "text", "text": "Describe"},
        {"type": "text", "text": "\n[Image 1]:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]

    assert job.status == "done"
    assert job.buffer == "Hello"
    assert job.usage == {
        "promptTokens": 30, "completionTokens": 2, "cachedPromptTokens": 12,
        # Present even at zero: a large value is the proof that a slow first
        # token was the model thinking rather than a stalled connection.
        "reasoningTokens": 0,
    }


def test_generation_streams_reasoning_without_disturbing_the_document_text():
    """Reasoning arrives on its own key and can precede every visible token.
    It is reported live and counted, but never buffered — buffering it would
    shift the replay offsets and push thinking into the document."""
    job = _new_job()
    seen: asyncio.Queue = asyncio.Queue()
    job.subscribers.add(seen)
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"weighing options"}}]}',
        'data: {"choices":[{"delta":{"content":"Answer"}}]}',
        "data: [DONE]",
    ]
    _run_job(
        job, "openai",
        {"apiKey": "sk", "model": "o-reasoner", "baseUrl": "https://llm.example.com/v1"},
        [{"role": "user", "content": "hi"}],
        _FakeStreamResponse(lines=lines), [],
    )

    assert job.buffer == "Answer"                          # not document text
    assert job.length == 6                                 # ...and no offset shift
    assert job.reasoning_chars == len("weighing options")
    assert job.first_reasoning_latency is not None

    events = []
    while not seen.empty():
        events.append(seen.get_nowait())
    assert [e["type"] for e in events] == ["reasoning", "delta", "done"]
    assert events[0]["text"] == "weighing options"


def test_generation_applies_reasoning_effort_per_provider():
    """Each provider spells effort differently; the client sends the resolved
    level and this maps it. A level that never reaches the wire is a setting
    the user changed for nothing."""
    openai_body = server_generation.build_openai_request(
        {"apiKey": "sk", "model": "grok-4.6", "baseUrl": "https://api.x.ai/v1", "reasoningEffort": "low"},
        [{"role": "user", "content": "hi"}], "grok",
    )[2]
    assert openai_body["reasoning_effort"] == "low"

    gemini_body = server_generation.build_gemini_request(
        {"apiKey": "k", "model": "gemini-2.5-pro", "baseUrl": "https://g", "reasoningEffort": "medium"},
        [{"role": "user", "content": "hi"}],
    )[2]
    assert gemini_body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 4096, "includeThoughts": True}
    # Gemini 3.x takes a level (measured 2026-10-10).
    gemini3 = server_generation.build_gemini_request(
        {"apiKey": "k", "model": "gemini-3.8-flash", "baseUrl": "https://g", "reasoningEffort": "high"},
        [{"role": "user", "content": "hi"}],
    )[2]
    assert gemini3["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high", "includeThoughts": True}

    # Claude 4.6+ and 5.x: adaptive thinking with an effort (a budget is a 400 there, measured).
    anthropic_body = server_generation.build_anthropic_request(
        {"apiKey": "k", "model": "claude-sonnet-5", "baseUrl": "https://a",
         "maxOutputTokens": 4096, "reasoningEffort": "high"},
        [{"role": "user", "content": "hi"}],
    )[2]
    assert anthropic_body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert anthropic_body["output_config"] == {"effort": "high"}
    # Thinking counts against max_tokens and adaptive has no budget to subtract: the configured
    # limit is the visible output, and the thinking gets headroom on top, capped at 64k
    # (a whole-chapter rewrite on sonnet-5-5 stopped at 16384 before the chapter's end, 2026-10-11).
    assert anthropic_body["max_tokens"] == 4096 + 32768
    big = server_generation.build_anthropic_request(
        {"apiKey": "k", "model": "claude-sonnet-5", "baseUrl": "https://a", "maxOutputTokens": 65536, "reasoningEffort": "low"},
        [{"role": "user", "content": "hi"}],
    )[2]
    assert big["max_tokens"] == 65536
    no_thinking = server_generation.build_anthropic_request(
        {"apiKey": "k", "model": "claude-sonnet-5", "baseUrl": "https://a", "maxOutputTokens": 4096, "reasoningEffort": "default"},
        [{"role": "user", "content": "hi"}],
    )[2]
    assert no_thinking["max_tokens"] == 4096 and "thinking" not in no_thinking
    # Claude 4.5 and earlier: a budget, clamped under max_tokens.
    old_body = server_generation.build_anthropic_request(
        {"apiKey": "k", "model": "claude-haiku-4-5-20251001", "baseUrl": "https://a",
         "maxOutputTokens": 4096, "reasoningEffort": "high"},
        [{"role": "user", "content": "hi"}],
    )[2]
    assert old_body["thinking"] == {"type": "enabled", "budget_tokens": 2048} and "output_config" not in old_body
    assert old_body["max_tokens"] == 4096  # the budget sits inside the configured limit; no headroom


def test_anthropic_effort_is_resolved_against_the_model():
    """The client forwards the raw setting: unset gets the app's default, 'default' sends nothing,
    and a level the model does not take is not sent (xhigh on 4.6 is a 400)."""
    build = lambda model, effort: server_generation.build_anthropic_request(  # noqa: E731
        {"apiKey": "k", "model": model, "baseUrl": "https://a", "maxOutputTokens": 16384,
         **({} if effort is None else {"reasoningEffort": effort})}, [{"role": "user", "content": "hi"}])[2]
    assert build("claude-sonnet-5-5", None)["output_config"] == {"effort": "low"}
    assert "thinking" not in build("claude-sonnet-5-5", "default") and "output_config" not in build("claude-sonnet-5-5", "default")
    assert "thinking" not in build("claude-opus-4-6", "xhigh")
    assert build("claude-opus-4-7", "xhigh")["output_config"] == {"effort": "xhigh"}
    headers = server_generation.build_anthropic_request({"apiKey": "k", "model": "claude-sonnet-5-5", "baseUrl": "https://a"},
                                                         [{"role": "user", "content": "hi"}])[1]
    assert "anthropic-beta" not in headers


def test_anthropic_image_only_message_has_no_empty_text_block():
    body = server_generation.build_anthropic_request(
        {"apiKey": "k", "model": "claude-sonnet-5-5", "baseUrl": "https://a"},
        [{"role": "user", "content": "", "images": ["data:image/png;base64,AAAA"]}])[2]
    blocks = body["messages"][0]["content"]
    assert all(b.get("text") != "" for b in blocks if b["type"] == "text")
    assert [b["type"] for b in blocks] == ["text", "image"]


def test_generation_retries_without_effort_when_the_provider_rejects_it():
    """The capability table is a guess about someone else's API. A wrong guess
    costs one retry, not the turn."""
    job = _new_job()
    captured = []
    attempts = []

    class _RejectThenAccept:
        """400s the first request, streams the second."""
        def __init__(self):
            self.calls = 0

        @property
        def status_code(self):
            return 400 if self.calls == 1 else 200

        async def aiter_lines(self):
            for line in ['data: {"type":"response.output_text.delta","delta":"ok","output_index":0}', "data: [DONE]"]:
                yield line

        async def aread(self):
            return b'{"error":"Unsupported parameter: reasoning_effort"}'

    response = _RejectThenAccept()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_stream(url, headers, body, read_timeout=None):
        response.calls += 1
        attempts.append(body)
        captured.append({"url": url, "headers": headers, "body": body})
        yield response

    with patch.object(wc_providers.common, "_http_stream", fake_stream):
        asyncio.run(server_generation.run_job(
            job, "grok",
            {"apiKey": "sk", "model": "grok-4.6", "baseUrl": "https://api.x.ai/v1", "reasoningEffort": "xhigh"},
            [{"role": "user", "content": "hi"}],
        ))

    assert len(attempts) == 2
    # grok goes over the Responses API: the effort is `reasoning.effort`.
    assert attempts[0]["reasoning"] == {"effort": "xhigh"}
    assert "reasoning" not in attempts[1]   # dropped, not repeated
    assert job.status == "done"
    assert job.buffer == "ok"


def test_generation_reasoning_rejection_matcher_is_not_a_blanket_retry():
    assert server_generation._is_reasoning_effort_rejection("Unsupported parameter: reasoning_effort")
    assert server_generation._is_reasoning_effort_rejection("thinking is not supported for this model")
    # Unrelated failures must still fail — retrying them just doubles the cost.
    assert not server_generation._is_reasoning_effort_rejection("Incorrect API key provided.")
    assert not server_generation._is_reasoning_effort_rejection("429 rate limit exceeded")


def test_generation_omits_reasoning_effort_when_not_chosen():
    """'default' and absence both mean: send nothing, let the provider decide."""
    for config in (
        {"apiKey": "sk", "model": "grok-4.6", "baseUrl": "https://api.x.ai/v1"},
        {"apiKey": "sk", "model": "grok-4.6", "baseUrl": "https://api.x.ai/v1", "reasoningEffort": "default"},
    ):
        body = server_generation.build_openai_request(config, [{"role": "user", "content": "hi"}], "grok")[2]
        assert "reasoning_effort" not in body

    # Gemini: 'default' sends no level, only the request to stream the thoughts as reasoning.
    gemini = server_generation.build_gemini_request(
        {"apiKey": "k", "model": "gemini-3.8-flash", "baseUrl": "https://g", "reasoningEffort": "default"},
        [{"role": "user", "content": "hi"}],
    )[2]
    assert gemini["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}


def test_generation_tolerates_a_trailing_slash_in_the_base_url():
    """Typing the URL with a trailing slash is normal; the resulting `//path`
    is not. llama.cpp answers it with a bare 404 "File Not Found", which reads
    like a missing model rather than a stray character."""
    url, _, _ = server_generation.build_openai_request(
        {"apiKey": "sk", "model": "m", "baseUrl": "http://127.0.0.1:8090/v1/"},
        [{"role": "user", "content": "hi"}], "ollama",
    )
    assert url == "http://127.0.0.1:8090/v1/chat/completions"

    gemini_url, _, _ = server_generation.build_gemini_request(
        {"apiKey": "k", "model": "gemini-2.5-pro", "baseUrl": "https://g/v1beta/"},
        [{"role": "user", "content": "hi"}],
    )
    assert "//models/" not in gemini_url

    anthropic_url, _, _ = server_generation.build_anthropic_request(
        {"apiKey": "k", "model": "claude-sonnet-5", "baseUrl": "https://a/v1/"},
        [{"role": "user", "content": "hi"}],
    )
    assert anthropic_url == "https://a/v1/messages"


def test_generation_ollama_request_omits_auth_and_stream_options():
    job = _new_job()
    captured = []
    _run_job(
        job, "ollama",
        {"apiKey": "ollama-no-key", "model": "llama3", "baseUrl": "http://localhost:11434/v1"},
        [{"role": "user", "content": "hi"}],
        _FakeStreamResponse(lines=['data: {"choices":[{"delta":{"content":"yo"}}]}', "data: [DONE]"]),
        captured,
    )

    request = captured[0]
    assert request["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in request["headers"]
    # Ollama rejects stream_options; it must never be sent.
    assert "stream_options" not in request["body"]
    assert job.buffer == "yo"
    assert job.usage is None


def test_generation_grok_request_sets_conversation_cache_header():
    job = _new_job()
    captured = []
    _run_job(
        job, "grok",
        {
            "apiKey": "xai-key", "model": "grok-4.3",
            "baseUrl": "https://api.x.ai/v1", "conversationId": "conv-9",
        },
        [{"role": "user", "content": "hi"}],
        _FakeStreamResponse(lines=['data: {"type":"response.output_text.delta","delta":"ok","output_index":0}']),
        captured,
    )

    request = captured[0]
    assert request["url"] == "https://api.x.ai/v1/responses"
    assert request["headers"]["x-grok-conv-id"] == "conv-9"
    assert request["headers"]["Authorization"] == "Bearer xai-key"
    assert request["body"]["prompt_cache_key"] == "conv-9"
    # Nothing kept on xAI's side; the reasoning comes back to us instead.
    assert request["body"]["store"] is False
    assert request["body"]["include"] == ["reasoning.encrypted_content"]
    assert job.status == "done"


def test_generation_gemini_request_and_chunked_json_parsing():
    job = _new_job()
    captured = []
    # Server-sent events (alt=sse); a thought part is reasoning, never text.
    lines = [
        'data: {"candidates":[{"content":{"parts":[{"text":"Weighing it.","thought":true},{"text":"Hel"},{"text":"lo"}]}}]}',
        "",
        'data: {"candidates":[{"content":{"parts":[{"text":" there"}]},'
        '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":7,'
        '"candidatesTokenCount":3,"cachedContentTokenCount":2,"thoughtsTokenCount":4}}',
    ]
    _run_job(
        job, "gemini",
        {
            "apiKey": "gem-secret", "model": "models/gemini-3.8-flash",
            "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
            "maxOutputTokens": 8192,
            "geminiSafetySettings": [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}],
        },
        [
            {"role": "system", "content": "Be brief"},
            {"role": "assistant", "content": "prior"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "Look", "images": ["data:image/jpeg;base64,ZZZ"]},
        ],
        _FakeStreamResponse(lines=lines), captured,
    )

    request = captured[0]
    # The 'models/' prefix is stripped so the path is not doubled; the key is a header, not in the URL.
    assert request["url"] == (
        "https://generativelanguage.googleapis.com/v1beta"
        "/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
    )
    assert request["headers"]["x-goog-api-key"] == "gem-secret"
    body = request["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "Be brief"}]}
    assert body["safetySettings"][0]["threshold"] == "BLOCK_NONE"
    assert body["generationConfig"] == {"maxOutputTokens": 8192, "thinkingConfig": {"thinkingLevel": "low", "includeThoughts": True}}
    # The empty reply is left out: Gemini refuses an empty text part.
    assert [c["role"] for c in body["contents"]] == ["model", "user"]
    assert body["contents"][1]["parts"] == [
        {"text": "Look"},
        {"text": "\n[Image 1]:"},
        {"inlineData": {"mimeType": "image/jpeg", "data": "ZZZ"}},
    ]

    assert job.status == "done"
    assert job.buffer == "Hello there" and "Weighing it." in job.reasoning_text
    assert job.usage == {"promptTokens": 7, "completionTokens": 7, "cachedPromptTokens": 2, "reasoningTokens": 4}


def test_generation_gemini_prompt_safety_block_becomes_error():
    job = _new_job()
    captured = []
    _run_job(
        job, "gemini",
        {"apiKey": "k", "model": "gemini-2.5-flash", "baseUrl": "https://g/v1beta"},
        [{"role": "user", "content": "bad"}],
        _FakeStreamResponse(lines=['data: {"promptFeedback":{"blockReason":"SAFETY"}}']),
        captured,
    )
    assert job.status == "error"
    assert job.error == "Content generation blocked by safety policy: SAFETY"


def test_generation_gemini_abnormal_finish_reason_becomes_error():
    job = _new_job()
    captured = []
    _run_job(
        job, "gemini",
        {"apiKey": "k", "model": "gemini-2.5-flash", "baseUrl": "https://g/v1beta"},
        [{"role": "user", "content": "hi"}],
        _FakeStreamResponse(lines=[
            'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]},"finishReason":"RECITATION"}]}'
        ]),
        captured,
    )
    assert job.status == "error"
    assert job.error == "Content generation blocked or terminated abnormally: RECITATION"
    # The partial text stays readable even though the job errored.
    assert job.buffer == ""


def test_generation_anthropic_request_caching_and_usage():
    job = _new_job()
    captured = []
    lines = [
        "event: message_start",
        'data: {"type":"message_start","message":{"usage":{"input_tokens":10,'
        '"cache_creation_input_tokens":5,"cache_read_input_tokens":100,"output_tokens":1}}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" there"}}',
        'data: {"type":"message_delta","usage":{"output_tokens":7}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":11}}',
    ]
    _run_job(
        job, "anthropic",
        {
            "apiKey": "sk-ant-secret", "model": "claude-sonnet-5",
            "baseUrl": "https://api.anthropic.com/v1", "maxOutputTokens": 16384,
        },
        [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "old turn", "cacheHint": True},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "See this", "images": ["data:image/png;base64,QQQ"], "cacheHint": True},
        ],
        _FakeStreamResponse(lines=lines), captured,
    )

    request = captured[0]
    assert request["url"] == "https://api.anthropic.com/v1/messages"
    assert request["headers"]["x-api-key"] == "sk-ant-secret"
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    assert "anthropic-beta" not in request["headers"]  # caching is GA

    body = request["body"]
    # 16384 configured, plus the headroom adaptive thinking gets on top (it counts against max_tokens).
    assert body["max_tokens"] == 16384 + 32768
    # System prompt is a structured block with its own cache breakpoint.
    assert body["system"] == [{
        "type": "text", "text": "System prompt", "cache_control": {"type": "ephemeral"},
    }]
    # The system message is excluded from messages[]; indexes still line up.
    assert len(body["messages"]) == 3
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "old turn", "cache_control": {"type": "ephemeral"}},
    ]
    assert body["messages"][1]["content"] == "answer"  # no hint, stays a string
    # For a multi-part message the breakpoint lands on the LAST part.
    image_parts = body["messages"][2]["content"]
    assert image_parts[0] == {"type": "text", "text": "See this"}
    assert image_parts[-1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QQQ"},
        "cache_control": {"type": "ephemeral"},
    }

    assert job.buffer == "Hi there"
    # Total input = input + cache_creation + cache_read; output is the
    # CUMULATIVE message_delta value, not a sum of the deltas.
    assert job.usage == {
        "promptTokens": 115,
        "completionTokens": 11,
        "cachedPromptTokens": 100,
        "cacheWriteTokens": 5,
    }


def test_generation_anthropic_cache_breakpoints_capped_at_three():
    messages = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": f"m{i}", "cacheHint": True} for i in range(5)
    ]
    _url, _headers, body = server_generation.build_anthropic_request(
        {"apiKey": "k", "model": "claude-sonnet-5", "baseUrl": "https://api.anthropic.com/v1"},
        messages,
    )
    marked = [m for m in body["messages"] if isinstance(m["content"], list)]
    # The system block takes the 4th slot, so only 3 message-level ones remain:
    # the last message, and the two latest hints before it.
    assert len(marked) == 3
    assert [m["content"][0]["text"] for m in marked] == ["m2", "m3", "m4"]


def test_generation_provider_http_error_becomes_error_event():
    job = _new_job()
    captured = []
    _run_job(
        job, "openai",
        {"apiKey": "sk", "model": "gpt-4o", "baseUrl": "https://api.openai.com/v1"},
        [{"role": "user", "content": "hi"}],
        _FakeStreamResponse(status_code=429, error_body=b"rate limited"), captured,
    )
    assert job.status == "error"
    assert job.error == "OpenAI API error (429): rate limited"


def test_generation_unsupported_provider_errors_without_a_request():
    job = _new_job()
    captured = []
    _run_job(job, "mystery", {}, [{"role": "user", "content": "hi"}], _FakeStreamResponse(), captured)
    assert captured == []
    assert job.status == "error"
    assert job.error == "Unsupported LLM provider: mystery"


def test_generation_masking_never_leaks_credentials():
    masked = server_generation.mask_request_details(
        "https://g/v1beta/models/m:streamGenerateContent?key=super-secret",
        {"Authorization": "Bearer sk-secret", "x-api-key": "sk-ant-secret"},
        {"model": "m"},
    )
    dumped = _json.dumps(masked)
    assert "super-secret" not in dumped
    assert "sk-secret" not in dumped
    assert "sk-ant-secret" not in dumped
    assert masked["url"].endswith("key=***")


# --- RunPod provider ---------------------------------------------------------

def test_model_host_allowlist_admits_loopback_and_https_runpod_only():
    ok = server_generation.is_queryable_model_host
    # Loopback, unchanged behaviour.
    assert ok("http://127.0.0.1:8100/v1")
    assert ok("http://localhost:11434/v1")
    # A pod addressed directly, over HTTPS.
    assert ok("https://abc123-8000.proxy.runpod.net/v1")
    # ...but not in the clear: the listing would cross the internet plaintext.
    assert not ok("http://abc123-8000.proxy.runpod.net/v1")
    # The guard still has to hold for everything else.
    assert not ok("http://169.254.169.254/latest/meta-data")
    assert not ok("http://192.168.0.5:3000/v1")
    assert not ok("https://api.openai.com/v1")
    assert not ok("file:///etc/passwd")


def test_model_host_allowlist_rejects_suffix_lookalikes():
    # The whole point of matching a leading-dot suffix rather than a substring:
    # anyone can register these, and a naive `in` test would admit them.
    ok = server_generation.is_queryable_model_host
    assert not ok("https://evil.proxy.runpod.net.attacker.com/v1")
    assert not ok("https://notproxy.runpod.net.evil.com/v1")
    assert not ok("https://myproxy.runpod.net.co/v1")


def test_generation_runpod_direct_pod_url_omits_auth_and_stream_options():
    # The case the pre-RunPod detection could not see: a pod addressed
    # DIRECTLY is neither loopback nor keyless, so a URL-only test would send
    # stream_options to a llama.cpp that rejects it. The provider must decide.
    job = _new_job()
    captured = []
    _run_job(
        job, "runpod",
        {"apiKey": "", "model": "local-model-q4",
         "baseUrl": "https://abc123-8000.proxy.runpod.net/v1"},
        [{"role": "user", "content": "hi"}],
        _FakeStreamResponse(lines=['data: {"choices":[{"delta":{"content":"yo"}}]}', "data: [DONE]"]),
        captured,
    )
    request = captured[0]
    assert request["url"] == "https://abc123-8000.proxy.runpod.net/v1/chat/completions"
    assert "Authorization" not in request["headers"]
    assert "stream_options" not in request["body"]
    assert job.buffer == "yo"


def test_runpod_is_a_supported_provider_needing_no_api_key():
    assert "runpod" in server_generation.SUPPORTED_PROVIDERS


# --- Focus-check baseline: mutations must return the stamp they wrote --------

def _seed_book(tmp_path, monkeypatch):
    """One book, one document, one live session for 'alice'. Returns a
    request factory bound to that session."""
    monkeypatch.setattr(server_db, "DB_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setattr(server_auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    server_db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    conn = server_db.get_db()
    try:
        conn.execute(
            "INSERT INTO books (id, username, title, active_document_id, created_at, updated_at)"
            " VALUES ('book-1', 'alice', 'My Book', 'doc-1', ?, ?)", (now, now))
        conn.execute(
            "INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at)"
            " VALUES ('doc-1', 'alice', 'book-1', 'Chapter 1', 0, ?, ?)", (now, now))
        conn.commit()
    finally:
        conn.close()
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    session = {"sess-1": {"username": "alice", "expiresAt": expires, "csrfToken": "tok-1"}}
    with open(server_auth.SESSIONS_FILE, "w", encoding="utf-8") as f:
        _json.dump(session, f)

    def make_request(method, path, body=None):
        payload = _json.dumps(body or {}).encode()
        sent = {"done": False}
        async def receive():
            if sent["done"]:
                return {"type": "http.disconnect"}
            sent["done"] = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return _StarletteRequest({
            "type": "http", "method": method, "path": path,
            "headers": [(b"cookie", b"web_canvas_session=sess-1"),
                        (b"x-csrf-token", b"tok-1"),
                        (b"content-type", b"application/json")],
            "query_string": b"",
        }, receive)
    return make_request


def _book_stamp():
    conn = server_db.get_db()
    try:
        return conn.execute(
            "SELECT updated_at FROM books WHERE id = 'book-1'").fetchone()["updated_at"]
    finally:
        conn.close()


def test_document_put_returns_the_book_stamp_it_wrote(tmp_path, monkeypatch):
    """The focus-time check compares the client's last-seen stamp against
    GET /api/books. The doc PUT bumps that stamp AFTER the metadata PUT whose
    response the client records — so unless the doc PUT returns its own stamp,
    every post-sync focus event looks like another device's write and reloads
    the book (resetting the reader to the top of the chapter)."""
    make_request = _seed_book(tmp_path, monkeypatch)
    monkeypatch.setattr(api_server, "save_document_content", lambda *a: None)

    result = asyncio.run(api_server.update_document(
        make_request("PUT", "/api/books/book-1/documents/doc-1",
                     {"title": "Renamed", "content": "<p>x</p>"}),
        "book-1", "doc-1"))

    assert result["success"] is True
    assert result["updatedAt"] == _book_stamp()


def test_delete_and_reorder_return_the_book_stamp(tmp_path, monkeypatch):
    make_request = _seed_book(tmp_path, monkeypatch)
    monkeypatch.setattr(api_server, "delete_document_content", lambda *a: None)

    reordered = asyncio.run(api_server.reorder_documents(
        make_request("PUT", "/api/books/book-1/documents/reorder",
                     {"documentIds": ["doc-1"]}),
        "book-1"))
    assert reordered["updatedAt"] == _book_stamp()

    deleted = asyncio.run(api_server.delete_document_endpoint(
        make_request("DELETE", "/api/books/book-1/documents/doc-1"),
        "book-1", "doc-1"))
    assert deleted["updatedAt"] == _book_stamp()


# ==============================================================================
# Per-user last active book (Decision Log 2026-09-06)
#
# A new device has no localStorage, so the session must say which book to
# open. The pointer is written by every book write and read back with a JOIN
# so it can never name a book that no longer exists.
# ==============================================================================

def _session_request(method, path, body=None, cookie="web_canvas_session=sess-1"):
    """A Request with a JSON body, addressed to the handler directly (no ASGI app)."""
    import json as _json
    from starlette.requests import Request
    payload = _json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"cookie", cookie.encode()), (b"content-type", b"application/json")],
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


def _isolated_db_with_alice(tmp_path, monkeypatch, books):
    """Fresh DB + session file; `books` is a list of (id, updated_at) for alice."""
    import json as _json
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(server_db, "DB_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setattr(server_auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    server_db.init_db()
    conn = server_db.get_db()
    try:
        for book_id, updated_at in books:
            conn.execute(
                "INSERT INTO books (id, username, title, active_document_id, created_at, updated_at)"
                " VALUES (?, 'alice', ?, '', ?, ?)",
                (book_id, f"Title {book_id}", updated_at, updated_at),
            )
        conn.commit()
    finally:
        conn.close()
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with open(server_auth.SESSIONS_FILE, "w", encoding="utf-8") as f:
        _json.dump({"sess-1": {"username": "alice", "expiresAt": expires}}, f)


def _bump_updated_at(book_id, stamp):
    """A write that bypasses the pointer (a chapter PUT from another device, a
    background summary save): only the book's timestamp moves."""
    conn = server_db.get_db()
    try:
        conn.execute("UPDATE books SET updated_at = ? WHERE username = 'alice' AND id = ?", (stamp, book_id))
        conn.commit()
    finally:
        conn.close()


def _session_last_book():
    import asyncio as _asyncio
    from starlette.responses import Response
    result = _asyncio.run(server_auth.get_session(_session_request("GET", "/api/auth/session"), Response()))
    assert result["loggedIn"] is True
    return result["lastActiveBookId"]


def test_session_reports_the_book_last_written(tmp_path, monkeypatch):
    import asyncio as _asyncio
    _isolated_db_with_alice(tmp_path, monkeypatch, [
        ("book-a", "2026-09-01T00:00:00Z"),
        ("book-b", "2026-09-02T00:00:00Z"),
    ])

    # Nothing recorded yet (an account that predates the pointer): the most
    # recently updated book stands in, so the feature works on first login.
    assert _session_last_book() == "book-b"

    # The metadata PUT every debounced save sends records the book on screen.
    _asyncio.run(api_server.update_book(
        _session_request("PUT", "/api/books/book-a", body={"bookTitle": "Title book-a"}), "book-a"))
    assert _session_last_book() == "book-a"

    _asyncio.run(api_server.update_book(
        _session_request("PUT", "/api/books/book-b", body={"activeDocumentId": "doc-x"}), "book-b"))
    assert _session_last_book() == "book-b"

    # The pointer decides, not recency: a later stamp on another book must not
    # move it. (Without this the recency fallback alone passes every check
    # above, because the PUT also bumps the book it writes.)
    _bump_updated_at("book-a", "2099-01-01T00:00:00Z")
    assert _session_last_book() == "book-b"


def test_session_never_points_at_a_deleted_book(tmp_path, monkeypatch):
    import asyncio as _asyncio
    _isolated_db_with_alice(tmp_path, monkeypatch, [
        ("book-a", "2026-09-01T00:00:00Z"),
        ("book-b", "2026-09-02T00:00:00Z"),
    ])
    _asyncio.run(api_server.update_book(_session_request("PUT", "/api/books/book-a", body={}), "book-a"))
    assert _session_last_book() == "book-a"

    # The book vanishes underneath the pointer (deleted from another device,
    # or by a path that never went through the endpoint).
    conn = server_db.get_db()
    try:
        conn.execute("DELETE FROM books WHERE username = 'alice' AND id = 'book-a'")
        conn.commit()
    finally:
        conn.close()

    # Fall through to the newest surviving book — never hand the client a 404.
    assert _session_last_book() == "book-b"


def test_creating_a_book_makes_it_the_last_active_one(tmp_path, monkeypatch):
    import asyncio as _asyncio
    _isolated_db_with_alice(tmp_path, monkeypatch, [("book-a", "2026-09-01T00:00:00Z")])

    _asyncio.run(api_server.create_book(
        _session_request("POST", "/api/books", body={"id": "book-c", "title": "C", "documents": []})))

    assert _session_last_book() == "book-c"
    # Pointer over recency here too.
    _bump_updated_at("book-a", "2099-01-01T00:00:00Z")
    assert _session_last_book() == "book-c"


def test_session_has_no_book_for_an_empty_account(tmp_path, monkeypatch):
    _isolated_db_with_alice(tmp_path, monkeypatch, [])
    assert _session_last_book() is None


def test_init_db_adds_user_state_to_an_existing_database(tmp_path, monkeypatch):
    """Upgrading a live DB must create the table, not only fresh installs."""
    import sqlite3
    db_file = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
        CREATE TABLE books (
            id TEXT NOT NULL, username TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'Untitled Book',
            active_document_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (username, id)
        );
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(server_db, "DB_PATH", str(db_file))
    server_db.init_db()

    conn = server_db.get_db()
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    assert "user_state" in tables


# ==============================================================================
# Document ordering regressions (found by a batch import, 2026-09-20)
#
# Two independent bugs let a batch import land chapters at ever wider gaps in
# sort_order and left chapter reordering silently unsaved on the server:
#   1. create_documents re-read MAX(sort_order) inside its insert loop.
#   2. PUT /documents/reorder was registered AFTER PUT /documents/{doc_id}, so
#      FastAPI matched the latter with doc_id="reorder" and 404'd.
# The pre-existing reorder test calls the handler directly, which cannot see (2)
# — the test below goes through the real ASGI router on purpose.
# ==============================================================================

def _seed_documents(book_id, username, doc_ids):
    """Append documents to an already-seeded book, numbered from 0."""
    conn = server_db.get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        for idx, doc_id in enumerate(doc_ids):
            conn.execute(
                "INSERT OR REPLACE INTO documents"
                " (id, username, book_id, title, sort_order, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc_id, username, book_id, f"Chapter {idx + 1}", idx, now, now))
        conn.commit()
    finally:
        conn.close()


def _sort_orders(book_id="book-1", username="alice"):
    conn = server_db.get_db()
    try:
        return [(r["id"], r["sort_order"]) for r in conn.execute(
            "SELECT id, sort_order FROM documents WHERE username = ? AND book_id = ?"
            " ORDER BY sort_order", (username, book_id)).fetchall()]
    finally:
        conn.close()


def test_reorder_route_is_not_shadowed_by_the_document_id_route(tmp_path, monkeypatch):
    """PUT /documents/reorder must reach reorder_documents through the real
    router. Registered after /documents/{doc_id} it was captured as a document
    id and 404'd, and the client swallows a non-OK response — so chapter
    reordering looked fine locally and never persisted."""
    from fastapi.testclient import TestClient

    _seed_book(tmp_path, monkeypatch)
    _seed_documents("book-1", "alice", ["doc-1", "doc-2", "doc-3"])

    client = TestClient(api_server.app)
    client.cookies.update({"web_canvas_session": "sess-1", "csrf_token": "tok-1"})
    res = client.put(
        "/api/books/book-1/documents/reorder",
        json={"documentIds": ["doc-3", "doc-1", "doc-2"]},
        headers={"x-csrf-token": "tok-1"},
    )

    assert res.status_code == 200, f"reorder route did not resolve: {res.status_code} {res.text}"
    assert res.json()["success"] is True
    assert _sort_orders() == [("doc-3", 0), ("doc-1", 1), ("doc-2", 2)]


def test_batch_append_assigns_contiguous_sort_orders(tmp_path, monkeypatch):
    """A batch append continues the existing numbering one step at a time."""
    make_request = _seed_book(tmp_path, monkeypatch)
    _seed_documents("book-1", "alice", ["doc-1", "doc-2"])
    monkeypatch.setattr(api_server, "save_document_content", lambda *a: None)

    body = {"documents": [
        {"id": f"doc-new-{n}", "title": f"Chapter {n}", "content": "<h1>x</h1>"}
        for n in range(3)
    ]}
    result = asyncio.run(api_server.create_documents(
        make_request("POST", "/api/books/book-1/documents", body), "book-1"))

    assert result["success"] is True
    assert _sort_orders() == [
        ("doc-1", 0), ("doc-2", 1),
        ("doc-new-0", 2), ("doc-new-1", 3), ("doc-new-2", 4),
    ]


def test_replace_all_numbers_documents_from_zero(tmp_path, monkeypatch):
    """replaceAll still restarts numbering at 0 after the hoisted base query."""
    make_request = _seed_book(tmp_path, monkeypatch)
    _seed_documents("book-1", "alice", ["doc-1", "doc-2", "doc-3"])
    monkeypatch.setattr(api_server, "save_document_content", lambda *a: None)
    monkeypatch.setattr(api_server, "delete_document_content", lambda *a: None)

    body = {"replaceAll": True, "documents": [
        {"id": f"doc-fresh-{n}", "title": f"Chapter {n}", "content": "<h1>x</h1>"}
        for n in range(2)
    ]}
    asyncio.run(api_server.create_documents(
        make_request("POST", "/api/books/book-1/documents", body), "book-1"))

    assert _sort_orders() == [("doc-fresh-0", 0), ("doc-fresh-1", 1)]


def test_book_save_keeps_an_agentic_turns_record(tmp_path, monkeypatch):
    """The messages table stored only fixed columns, so an agentic turn's
    record (its tool timeline and the chapters it changed) vanished on every
    reload — the bubble fell back to plain text. It is now one JSON column."""
    make_request = _seed_book(tmp_path, monkeypatch)
    agent = {
        "status": "done", "steps": 2,
        "trace": ['📖 read #3 "故事线"'],
        "touched": [{"documentId": "doc-1", "titleAtRun": "第一章", "kind": "created", "changes": 1, "failed": 0}],
        "timeline": [{"type": "text", "text": "先看大纲。"}, {"type": "tool", "line": '📖 read #3 "故事线"', "ok": True}],
    }
    messages = [
        {"id": "u1", "role": "user", "content": "写第一章", "timestamp": "2026-10-05T00:00:00Z"},
        {"id": "a1", "role": "assistant", "content": "先看大纲。", "timestamp": "2026-10-05T00:00:01Z", "agent": agent},
    ]
    asyncio.run(api_server.update_book(make_request("PUT", "/api/books/book-1", {"messages": messages}), "book-1"))

    loaded = asyncio.run(api_server.get_book(make_request("GET", "/api/books/book-1"), "book-1"))
    by_id = {m["id"]: m for m in loaded["messages"]}
    assert by_id["a1"]["agent"] == agent
    # An ordinary message gets no agent key at all.
    assert "agent" not in by_id["u1"]


def test_init_db_adds_the_agent_column_to_an_existing_messages_table(tmp_path, monkeypatch):
    import sqlite3
    db_file = tmp_path / "metadata.db"
    monkeypatch.setattr(server_db, "DB_PATH", str(db_file))
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE messages (id TEXT NOT NULL, username TEXT NOT NULL, book_id TEXT NOT NULL,"
        " role TEXT NOT NULL, content TEXT NOT NULL, timestamp TEXT NOT NULL, thinking TEXT, model TEXT,"
        " input_tokens INTEGER, output_tokens INTEGER, cache_hit_tokens INTEGER,"
        " sort_order INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (username, book_id, id))")
    conn.execute("INSERT INTO messages (id, username, book_id, role, content, timestamp)"
                 " VALUES ('m1', 'alice', 'b', 'user', 'hi', 'now')")
    conn.commit()
    conn.close()

    server_db.init_db()
    server_db.init_db()  # idempotent

    conn = sqlite3.connect(str(db_file))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert "agent" in columns
    assert conn.execute("SELECT content, agent FROM messages WHERE id = 'm1'").fetchone() == ("hi", None)
    conn.close()


def test_models_endpoint_lists_claude_models_only_from_anthropic(_stub_models_auth):
    """Claude's list comes through the backend (no CORS), with the user's key, and only from api.anthropic.com."""
    for base in ("http://127.0.0.1:9/v1", "https://api.anthropic.com.evil.example/v1", "https://evil.example/v1", "https://api.openai.com/v1"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server_generation.list_provider_models(_FakeRequest({"provider": "anthropic", "apiKey": "k", "baseUrl": base})))
        assert exc.value.status_code == 400
    seen = {}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            seen.update(url=url, headers=headers)

            class _R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"data": [{"id": "claude-sonnet-5-5", "max_input_tokens": 1000000}, {"id": "claude-opus-5-5"}]}
            return _R()
    with patch.object(server_generation.httpx, "AsyncClient", _Client):
        result = asyncio.run(server_generation.list_provider_models(
            _FakeRequest({"provider": "anthropic", "apiKey": "sk-ant", "baseUrl": "https://api.anthropic.com/v1"})))
    # The Models API's max_input_tokens rides along as the window the tab should believe.
    assert result == {"models": ["claude-sonnet-5-5", "claude-opus-5-5"], "contextWindows": {"claude-sonnet-5-5": 1000000}}
    assert seen["url"] == "https://api.anthropic.com/v1/models" and seen["headers"]["x-api-key"] == "sk-ant"


def _anthropic_job(lines):
    job = _new_job()
    _run_job(job, "anthropic", {"apiKey": "k", "model": "claude-sonnet-5-5", "baseUrl": "https://api.anthropic.com/v1"},
             [{"role": "user", "content": "hi"}], _FakeStreamResponse(lines=lines), [])
    return job


def test_anthropic_refusal_is_an_error_not_a_silent_turn():
    job = _anthropic_job(['data: {"type":"message_start","message":{"usage":{"input_tokens":3,"output_tokens":1}}}',
                          'data: {"type":"message_delta","delta":{"stop_reason":"refusal"},"usage":{"output_tokens":1}}'])
    assert job.status == "error" and "refusal" in (job.error or "")


def test_anthropic_error_inside_the_stream_fails_the_job():
    job = _anthropic_job(['data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
                          'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'])
    assert job.status == "error" and "overloaded_error" in (job.error or "")


def test_anthropic_thinking_streams_live_and_is_kept_whole():
    job = _anthropic_job([
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Count the sheep."}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"SIG"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"9"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}'])
    assert job.status == "done" and job.buffer == "9"
    assert job.thinking_blocks == [{"type": "thinking", "thinking": "Count the sheep.", "signature": "SIG"}]
    assert "Count the sheep." in job.reasoning_text


def test_models_endpoint_lists_openai_text_models_newest_first(_stub_models_auth):
    with pytest.raises(HTTPException):
        asyncio.run(server_generation.list_provider_models(_FakeRequest({"provider": "openai", "apiKey": "k", "baseUrl": "https://api.anthropic.com/v1"})))

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            assert headers == {"Authorization": "Bearer sk"}

            class _R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"data": [{"id": i} for i in ("gpt-4o", "gpt-realtime", "gpt-6.1-sol", "o3", "gpt-image-1", "dall-e-3", "gpt-4o-mini-tts")]}
            return _R()
    with patch.object(server_generation.httpx, "AsyncClient", _Client):
        result = asyncio.run(server_generation.list_provider_models(_FakeRequest({"provider": "openai", "apiKey": "sk", "baseUrl": "https://api.openai.com/v1"})))
    assert result == {"models": ["gpt-6.1-sol", "gpt-4o", "o3"]}


def test_openai_runs_on_the_responses_api_with_its_own_reasoning_spelling():
    """gpt-5.4+ refuse tools with reasoning on Chat Completions (measured): OpenAI's own host uses Responses."""
    assert server_generation.uses_responses_api("openai", "https://api.openai.com/v1")
    assert not server_generation.uses_responses_api("openai", "https://llm.example.com/v1")
    url, headers, body = server_generation.build_responses_request(
        {"apiKey": "sk", "model": "gpt-6.1-sol", "baseUrl": "https://api.openai.com/v1", "maxOutputTokens": 4096,
         "reasoningEffort": "xhigh", "conversationId": "book-1",
         "tools": [{"type": "function", "function": {"name": "read", "description": "", "parameters": {"type": "object"}}}]},
        [{"role": "user", "content": "hi"}], "openai")
    assert url == "https://api.openai.com/v1/responses" and "x-grok-conv-id" not in headers
    assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"] and body["store"] is False
    assert body["prompt_cache_key"] == "book-1" and body["max_output_tokens"] == 4096
    assert body["tools"][0]["name"] == "read" and body["tools"][0]["strict"] is False
    # Unset: the app's default; a model without reasoning gets neither the field nor the ciphertext.
    assert server_generation.build_responses_request({"model": "gpt-5.5", "baseUrl": "https://api.openai.com/v1"},
                                                     [{"role": "user", "content": "hi"}], "openai")[2]["reasoning"]["effort"] == "low"
    plain = server_generation.build_responses_request({"model": "gpt-4o", "baseUrl": "https://api.openai.com/v1", "reasoningEffort": "high"},
                                                      [{"role": "user", "content": "hi"}], "openai")[2]
    assert "reasoning" not in plain and "include" not in plain


def test_openai_responses_job_streams_text_reasoning_and_calls():
    job = _new_job()
    captured = []
    _run_job(job, "openai", {"apiKey": "sk", "model": "gpt-5.5", "baseUrl": "https://api.openai.com/v1"},
             [{"role": "user", "content": "hi"}], _FakeStreamResponse(lines=[
                 'data: {"type":"response.reasoning_summary_text.delta","delta":"Plan.","output_index":0}',
                 'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"rs_1","encrypted_content":"C=="}}',
                 'data: {"type":"response.output_text.delta","delta":"Ok","output_index":1}',
                 'data: {"type":"response.completed","response":{"usage":{"input_tokens":20,"output_tokens":4,'
                 '"input_tokens_details":{"cached_tokens":16},"output_tokens_details":{"reasoning_tokens":2}}}}',
             ]), captured)
    assert captured[0]["url"] == "https://api.openai.com/v1/responses"
    assert job.status == "done" and job.buffer == "Ok" and "Plan." in job.reasoning_text
    assert job.response_items == [{"type": "reasoning", "id": "rs_1", "encrypted_content": "C=="}]
    assert job.usage == {"promptTokens": 20, "completionTokens": 4, "cachedPromptTokens": 16, "cacheWriteTokens": 0, "reasoningTokens": 2}


def test_models_endpoint_lists_gemini_text_models_without_closed_or_media_ones(_stub_models_auth):
    seen = {}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            seen.update(url=url, headers=headers, params=params)

            class _R:
                def raise_for_status(self):
                    pass

                def json(self):
                    gen = ["generateContent"]
                    return {"models": [{"name": f"models/{n}", "supportedGenerationMethods": gen} for n in (
                        "gemini-2.5-flash", "gemini-flash-latest", "gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.8-flash-tts",
                        "gemini-3.1-flash-image", "lyria-3.5", "gemini-3.1-pro-preview")] + [{"name": "models/text-embedding-9", "supportedGenerationMethods": ["embedContent"]}]}
            return _R()
    with patch.object(server_generation.httpx, "AsyncClient", _Client):
        result = asyncio.run(server_generation.list_provider_models(
            _FakeRequest({"provider": "gemini", "apiKey": "AIza-k", "baseUrl": "https://generativelanguage.googleapis.com/v1beta"})))
    assert result == {"models": ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-flash-latest"]}
    assert seen["headers"] == {"x-goog-api-key": "AIza-k"} and "key=" not in seen["url"]


def test_init_db_folds_per_book_settings_into_user_settings(tmp_path):
    """The per-book settings table goes: each user keeps the "__global__" row (the one read since
    2026-06-29), or the "default" book's when there was none; the per-book copies are dropped."""
    import sqlite3
    db_file = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
        CREATE TABLE book_settings (
            username TEXT NOT NULL, book_id TEXT NOT NULL, active_provider TEXT, provider_configs TEXT,
            custom_system_prompts TEXT, active_system_prompt_id TEXT, theme TEXT, debug_mode INTEGER DEFAULT 0,
            PRIMARY KEY (username, book_id));
        INSERT INTO book_settings VALUES ('alice', '__global__', 'openai', '{"openai":{"apiKey":"k"}}', NULL, NULL, 'dark', 1);
        INSERT INTO book_settings VALUES ('alice', 'book-1', 'grok', '{"openai":{"apiKey":""}}', NULL, NULL, 'light', 0);
        INSERT INTO book_settings VALUES ('alice', 'default', 'gemini', NULL, NULL, NULL, NULL, 0);
        INSERT INTO book_settings VALUES ('bob', 'default', 'anthropic', '{"anthropic":{}}', NULL, NULL, 'light', 0);
        INSERT INTO book_settings VALUES ('bob', 'book-9', 'grok', NULL, NULL, NULL, NULL, 0);
    """)
    conn.commit()
    conn.close()

    with patch.object(server_db, "DB_PATH", str(db_file)):
        server_db.init_db()
        server_db.init_db()  # idempotent
        conn = server_db.get_db()
        try:
            rows = {r["username"]: dict(r) for r in conn.execute("SELECT * FROM user_settings")}
            assert set(rows) == {"alice", "bob"}
            assert rows["alice"]["active_provider"] == "openai" and rows["alice"]["theme"] == "dark" and rows["alice"]["debug_mode"] == 1
            assert rows["bob"]["active_provider"] == "anthropic"
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'book_settings'").fetchone()
            # Saving updates the user's row; absent fields keep their values.
            server_db.upsert_user_settings(conn, "alice", {"activeProvider": "anthropic"})
            server_db.upsert_user_settings(conn, "carol", {"theme": "light"})
            row = conn.execute("SELECT * FROM user_settings WHERE username = 'alice'").fetchone()
            assert row["active_provider"] == "anthropic" and row["provider_configs"] == '{"openai":{"apiKey":"k"}}'
            assert conn.execute("SELECT theme FROM user_settings WHERE username = 'carol'").fetchone()[0] == "light"
        finally:
            conn.close()


def test_each_book_keeps_its_provider_model_effort_and_preset_by_reference(tmp_path, monkeypatch):
    """A book remembers what it uses; keys and preset texts stay the user's (one copy)."""
    from fastapi.testclient import TestClient

    _seed_book(tmp_path, monkeypatch)
    conn = server_db.get_db()
    try:
        now = "2026-10-10T00:00:00Z"
        conn.execute("INSERT INTO books (id, username, title, active_document_id, created_at, updated_at) VALUES ('book-2', 'alice', 'Two', NULL, ?, ?)", (now, now))
        conn.commit()
    finally:
        conn.close()
    client = TestClient(api_server.app)
    client.cookies.update({"web_canvas_session": "sess-1", "csrf_token": "tok-1"})
    presets = [{"id": "p-1", "name": "One", "content": "text one"}, {"id": "p-2", "name": "Two", "content": "text two"}]

    def save(book, provider, model, effort, preset):
        configs = {"anthropic": {"apiKey": "sk-ant", "model": "claude-sonnet-5-5"}, "openai": {"apiKey": "sk-oa", "model": "gpt-5.5"}}
        configs[provider] = {**configs[provider], "model": model, "reasoningEffort": effort}
        res = client.put(f"/api/books/{book}", headers={"x-csrf-token": "tok-1"}, json={
            "activeProvider": provider, "providerConfigs": configs, "customSystemPrompts": presets, "activeSystemPromptId": preset})
        assert res.status_code == 200, res.text

    save("book-1", "anthropic", "claude-opus-5-5", "high", "p-1")
    save("book-2", "openai", "gpt-6.1-sol", "xhigh", "p-2")  # the user's settings now say openai / p-2
    one = client.get("/api/books/book-1").json()
    assert one["activeProvider"] == "anthropic" and one["activeSystemPromptId"] == "p-1"
    assert one["providerConfigs"]["anthropic"]["model"] == "claude-opus-5-5"
    assert one["providerConfigs"]["anthropic"]["reasoningEffort"] == "high"
    assert one["providerConfigs"]["anthropic"]["apiKey"] == "sk-ant"  # the user's key, not a per-book copy
    two = client.get("/api/books/book-2").json()
    assert two["activeProvider"] == "openai" and two["providerConfigs"]["openai"]["model"] == "gpt-6.1-sol" and two["activeSystemPromptId"] == "p-2"

    # A preset deleted since: the user's current choice stands. Preset texts are only the user's.
    res = client.put("/api/books/book-2", headers={"x-csrf-token": "tok-1"},
                     json={"customSystemPrompts": [presets[1]], "activeSystemPromptId": "p-2"})
    assert res.status_code == 200
    assert client.get("/api/books/book-1").json()["activeSystemPromptId"] == "p-2"
    conn = server_db.get_db()
    try:
        assert [c[1] for c in conn.execute("PRAGMA table_info(book_preferences)")] == [
            "username", "book_id", "active_provider", "model", "reasoning_effort", "active_system_prompt_id"]
    finally:
        conn.close()

    # Deleting the book takes its preferences with it.
    monkeypatch.setattr(api_server, "delete_book_content_dir", lambda *a: None)
    monkeypatch.setattr(api_server.server_attachments, "delete_book_attachments", lambda *a: None)
    monkeypatch.setattr(api_server.server_runs.server_context, "delete_book_context", lambda *a: None)
    assert client.delete("/api/books/book-2", headers={"x-csrf-token": "tok-1"}).status_code == 200
    conn = server_db.get_db()
    try:
        assert not conn.execute("SELECT 1 FROM book_preferences WHERE book_id = 'book-2'").fetchone()
    finally:
        conn.close()


def test_anthropic_thinking_after_the_text_is_replayed_after_the_text():
    """Claude 5.x thinks again mid-reply. Replaying every block ahead of the text moves the second one,
    which Anthropic rejects as modified (seen 2026-10-11: "messages.25.content.1")."""
    job = _anthropic_job([
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"First."}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"S1"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Draft."}}',
        'data: {"type":"content_block_start","index":2,"content_block":{"type":"thinking","thinking":"","signature":""}}',
        'data: {"type":"content_block_delta","index":2,"delta":{"type":"thinking_delta","thinking":"Again."}}',
        'data: {"type":"content_block_delta","index":2,"delta":{"type":"signature_delta","signature":"S2"}}',
        'data: {"type":"content_block_stop","index":2}',
        'data: {"type":"content_block_start","index":3,"content_block":{"type":"tool_use","id":"toolu_1","name":"plan"}}',
        'data: {"type":"content_block_delta","index":3,"delta":{"type":"input_json_delta","partial_json":"{}"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":9}}'])
    assert job.thinking_blocks == [{"type": "thinking", "thinking": "First.", "signature": "S1"},
                                   {"type": "thinking", "thinking": "Again.", "signature": "S2", "afterText": True}]
    body = server_generation.build_anthropic_request(
        {"apiKey": "k", "model": "claude-sonnet-5-5", "baseUrl": "https://a"},
        [{"role": "user", "content": "go"},
         {"role": "assistant", "content": "Draft.", "thinking": job.thinking_blocks,
          "toolCalls": [{"id": "toolu_1", "name": "plan", "argumentsText": "{}"}]},
         {"role": "tool", "toolCallId": "toolu_1", "name": "plan", "content": "ok"}])[2]
    assert body["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "First.", "signature": "S1"},
        {"type": "text", "text": "Draft."},
        {"type": "thinking", "thinking": "Again.", "signature": "S2"},
        {"type": "tool_use", "id": "toolu_1", "name": "plan", "input": {}},
    ]


def test_a_lone_surrogate_from_the_browser_is_replaced_before_the_provider_request():
    """A JS string cut in the middle of an emoji arrives as "\\ud83d"; encoding the request as UTF-8
    then failed the whole step ('surrogates not allowed', 2026-10-11)."""
    import json as _j
    messages = _j.loads('[{"role":"user","content":"fire \\ud83d","images":["ok"]},{"role":"tool","content":"\\ud83d\\ude25 whole"}]')
    counter = [0]
    out = server_generation.scrub_surrogates(messages, counter)
    assert out[0]["content"] == "fire �" and out[1]["content"] == "\U0001f625 whole" and counter == [1]
    _j.dumps(out, ensure_ascii=False).encode("utf-8")  # what httpx does
    job = _new_job()
    captured = []
    _run_job(job, "openai", {"apiKey": "sk", "model": "gpt-4o", "baseUrl": "https://llm.example.com/v1"},
             messages, _FakeStreamResponse(lines=['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"]), captured)
    assert job.status == "done" and captured[0]["body"]["messages"][0]["content"][0] == {"type": "text", "text": "fire \ufffd"}

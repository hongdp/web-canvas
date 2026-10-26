"""The server-side run engine (server_runs) against a scripted provider:
runs that write the book, queue, stop, pause and survive a restart."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

import api_server  # noqa: F401 — registers the router
import server_auth
import server_content
import server_context
import server_db
import server_documents
import server_events
import server_generation
import server_runs
from wc_text.diff import strip_diff_markup


# ── a book, a session, a scripted provider ────────────────────────────────────

@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(server_db, "DB_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setattr(server_auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server_content, "CONTENT_DIR", str(tmp_path / "content"))
    server_db.init_db()
    server_runs.ensure_tables()
    now = datetime.now(timezone.utc).isoformat()
    conn = server_db.get_db()
    try:
        conn.execute("INSERT INTO books (id, username, title, active_document_id, created_at, updated_at) VALUES ('book-1', 'alice', 'My Book', 'doc-1', ?, ?)", (now, now))
        for i, (doc_id, title) in enumerate([("doc-1", "Chapter 1"), ("doc-2", "Chapter 2")]):
            conn.execute("INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at) VALUES (?, 'alice', 'book-1', ?, ?, ?, ?)",
                         (doc_id, title, i, now, now))
        conn.commit()
    finally:
        conn.close()
    server_content.save_document_content("alice", "book-1", "doc-1", "<p>alpha</p>")
    server_content.save_document_content("alice", "book-1", "doc-2", "<p>two</p>")
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with open(server_auth.SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump({"sess-1": {"username": "alice", "expiresAt": expires, "csrfToken": "tok-1"}}, f)
    engine = server_runs.RunEngine()
    engine._loaded = True
    monkeypatch.setattr(server_runs, "engine", engine)
    server_generation.registry.clear()
    yield engine
    server_generation.registry.clear()


class Scripted:
    """Replies in order; each is text, or a dict {text, calls:[(id,name,args_json)]}."""

    def __init__(self, replies: List[Any], by_step: bool = False) -> None:
        self.replies = list(replies)
        self.by_step = by_step
        self.requests: List[Dict[str, Any]] = []
        self.gate: "asyncio.Event | None" = None

    async def __call__(self, job, provider, config, messages):
        self.requests.append({"config": config, "messages": messages})
        if self.gate is not None:
            await self.gate.wait()
        if self.by_step:
            step = (job.meta or {}).get("step") or 0
            reply = self.replies[step] if step < len(self.replies) else "<doc_status>unchanged</doc_status>"
        else:
            reply = self.replies.pop(0) if self.replies else "<doc_status>unchanged</doc_status>"
        if isinstance(reply, str):
            reply = {"text": reply}
        for i, (call_id, name, args) in enumerate(reply.get("calls", [])):
            job.note_tool_call(i, call_id, name, args)
        for piece in reply.get("chunks") or ([reply["text"]] if reply.get("text") else []):
            job.append(piece)
            await asyncio.sleep(0)
        if reply.get("triggers"):
            job.note_loop_triggers(reply["triggers"])
        if reply.get("raise") is not None:
            raise reply["raise"]
        return {"promptTokens": 100, "completionTokens": 10, "cachedPromptTokens": 50}


def request(prompt="写第一章", **over) -> Dict[str, Any]:
    return {"prompt": prompt, "provider": "grok", "config": {"apiKey": "k", "model": "grok-4.6", "baseUrl": "https://x", "agentMaxSteps": 0},
            "activeDocumentId": "doc-1", "history": [], "userMessageId": f"u-{prompt}", "assistantMessageId": f"a-{prompt}",
            "images": None, "selectedText": "", "customInstructions": None, "polishPrompt": None, "contextWindowTokens": None, "clientId": "tab-1", **over}


def content_of(doc_id: str) -> str:
    return server_content.load_document_content("alice", "book-1", doc_id)


def message_row(msg_id: str) -> sqlite3.Row:
    conn = server_db.get_db()
    try:
        return conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    finally:
        conn.close()


def events_of(queue: asyncio.Queue) -> List[Dict[str, Any]]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def settle(run: server_runs.Run) -> None:
    while run.task is not None and not run.task.done():
        await run.task
    await asyncio.sleep(0)


# ── runs ──────────────────────────────────────────────────────────────────────

def test_a_markup_turn_writes_the_chapter_and_the_bubble(book, monkeypatch):
    provider = Scripted(["好的。\n<canvas><h1>第一章</h1><p>新的正文。</p></canvas>\n<doc_status>updated</doc_status>", "写完了。"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        queue = server_events.hub.subscribe("alice", "book-1")
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run, events_of(queue)
    run, events = asyncio.run(main())
    assert run.status == "done", run.error
    assert strip_diff_markup(content_of("doc-1")) == "<h1>第一章</h1><p>新的正文。</p>"
    assert "diff-addition" in content_of("doc-1")
    kinds = [e["kind"] for e in events if e.get("type") == "run"]
    assert kinds[0] == "started" and kinds[-1] == "finished" and "preview" in kinds and "step" in kinds
    assert any(e["type"] == "document" and e["kind"] == "updated" for e in events)
    row = message_row("a-写第一章")
    assert "好的。" in row["content"] and "写完了。" in row["content"]
    record = json.loads(row["agent"])
    assert record["status"] == "done" and record["steps"] == 2 and record["touched"][0]["documentId"] == "doc-1"
    assert run.result["usage"]["promptTokens"] == 200
    # The system prompt went first, the request last, with the chapter index in it.
    first = provider.requests[0]["messages"]
    assert first[0]["role"] == "system" and "CHAPTER INDEX" in first[-1]["content"] and first[-1]["content"].endswith("写第一章")
    assert provider.requests[1]["messages"][: len(first)] == first


def test_a_second_request_queues_and_sees_the_first_turn_in_its_history(book, monkeypatch):
    provider = Scripted(["<canvas><p>one</p></canvas>", "第一轮写完了。", "<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        queue = server_events.hub.subscribe("alice", "book-1")
        first = book.submit("alice", "book-1", request("A"))
        second = book.submit("alice", "book-1", request("B", history=[
            {"id": "u-A", "role": "user", "content": "A"}, {"id": "a-A", "role": "assistant", "content": "Thinking..."}]))
        assert second.status == "queued"
        await settle(first)
        await asyncio.sleep(0.01)
        await settle(second)
        return first, second, events_of(queue)
    first, second, events = asyncio.run(main())
    assert first.status == "done" and second.status == "done"
    assert [e["kind"] for e in events if e.get("type") == "run" and e["runId"] == second.id][0] == "queued"
    history = provider.requests[2]["messages"]
    assistant_turns = [m for m in history if m["role"] == "assistant"]
    assert any("第一轮写完了" in m["content"] for m in assistant_turns), [m["content"][:40] for m in history]


def test_the_next_turn_replays_the_finished_turn_and_extends_its_last_request(book, monkeypatch):
    # cache_continuity.md §3.1: the transcript is stored when the run ends and replayed verbatim.
    provider = Scripted([{"text": "", "calls": [("c1", "read", '{"chapters":["2"]}')]}, "读完了。<doc_status>unchanged</doc_status>",
                         "好的。<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        first = book.submit("alice", "book-1", request("A"))
        await settle(first)
        second = book.submit("alice", "book-1", request("B", history=[
            {"id": "u-A", "role": "user", "content": "A"}, {"id": "a-A", "role": "assistant", "content": "读完了。"}]))
        await settle(second)
        return first, second
    first, second = asyncio.run(main())
    assert first.status == "done" and second.status == "done", (first.error, second.error)
    stored = server_context.load_transcripts("alice", "book-1", ["a-A"], "grok|grok-4.6")["a-A"]
    assert stored["userMessageId"] == "u-A"
    assert [m["role"] for m in stored["messages"]] == ["user", "assistant", "tool", "assistant"]
    last_of_first = provider.requests[1]["messages"]
    turn_two = provider.requests[2]["messages"]
    assert turn_two[: len(last_of_first)] == last_of_first
    assert turn_two[len(last_of_first)]["content"] == "读完了。<doc_status>unchanged</doc_status>"
    # The active chapter's text is in the replayed turn; the index changed (chapter 2 was read) and goes in full.
    assert 'CURRENT ACTIVE DOCUMENT: #1 "Chapter 1" — unchanged since your last turn' in turn_two[-1]["content"]
    assert "read earlier, not in context" in turn_two[-1]["content"]


def test_stop_aborts_the_step_and_holds_the_queue(book, monkeypatch):
    provider = Scripted(["<canvas><p>never</p></canvas>"])
    provider.gate = asyncio.Event()
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        provider.gate = asyncio.Event()
        first = book.submit("alice", "book-1", request("A"))
        second = book.submit("alice", "book-1", request("B"))
        await asyncio.sleep(0.01)
        book.stop(first)
        await settle(first)
        await asyncio.sleep(0.01)
        return first, second
    first, second = asyncio.run(main())
    assert first.status == "stopped" and second.status == "queued"
    assert book.queue_is_held("alice", "book-1")
    assert "Stopped" in message_row("a-A")["content"]
    assert content_of("doc-1") == "<p>alpha</p>"


# ── the server's analyze and polish ports (agentic_chat_loop.md §0.11) ──────

def test_analyze_book_runs_on_a_server_run(book, monkeypatch):
    """Regression: the port kept the run as `self.run`, hiding its run() method — every call failed."""
    # By its old name (an alias of read, read_and_list.md §4), for the whole book: read in batches.
    call = {"text": "", "calls": [("a1", "analyze_book", json.dumps({"task": "审阅"}))]}
    provider = Scripted([call, "NOTES: 两章都在。", "好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "done"
    assert any(line.startswith("📚 read the book for a task") and "(failed)" not in line for line in run.record["trace"]), run.record["trace"]
    assert "NOTES: 两章都在。" in json.dumps(provider.requests[2]["messages"], ensure_ascii=False)


def _polish_request(language="zh"):
    return request("润色《Chapter 1》", polish={"documentId": "doc-1", "language": language})


def test_the_polish_button_runs_on_the_server(book, monkeypatch):
    """Phase 4: the Polish button is a run — no agent loop, one polish call per chunk, the tab's record."""
    server_content.save_document_content("alice", "book-1", "doc-1", "<p>她推开门，屋里没有人。</p>")
    provider = Scripted(["<p>她推开了门，屋里空无一人。</p>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        queue = server_events.hub.subscribe("alice", "book-1")
        run = book.submit("alice", "book-1", _polish_request())
        await settle(run)
        return run, events_of(queue)
    run, events = asyncio.run(main())
    assert run.status == "done", run.error
    assert strip_diff_markup(content_of("doc-1")) == "<p>她推开了门，屋里空无一人。</p>"
    assert "diff-addition" in content_of("doc-1")
    assert len(provider.requests) == 1 and provider.requests[0]["config"]["model"] == "grok-4.20-0309-reasoning"
    assert run.record["trace"] == ['✨ polished #1 "Chapter 1" (1 of 1 chunk(s) rewritten)']
    assert run.record["touched"][0]["kind"] == "polished"
    runs = [e for e in events if e.get("type") == "run"]
    assert any(e["kind"] == "progress" and "润色中" in (e.get("line") or "") for e in runs)
    assert any(e["kind"] == "lock" and e.get("documentIds") == ["doc-1"] for e in runs)
    row = message_row("a-润色《Chapter 1》")
    assert row["content"].startswith("✨ polished") and json.loads(row["agent"])["status"] == "done"
    assert message_row("u-润色《Chapter 1》")["content"] == "润色《Chapter 1》"
    assert run.snapshots, "a version snapshot before the change"


def _offered_tools(book, monkeypatch, provider, config):
    scripted = Scripted(["好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", scripted)

    async def main():
        run = book.submit("alice", "book-1", request(provider=provider, config={"apiKey": "k", "baseUrl": "https://x", **config}))
        await settle(run)
    asyncio.run(main())
    return {t.get("function", t).get("name") for t in scripted.requests[0]["config"].get("tools") or []}


def test_polish_chapter_is_offered_only_with_a_polish_model_other_than_the_chat_model(book, monkeypatch):
    """2026-10-19: on Claude the polish model was Claude itself, re-writing its own prose in chunks."""
    assert "polish_chapter" in _offered_tools(book, monkeypatch, "grok", {"model": "grok-4.6"})
    assert "polish_chapter" not in _offered_tools(book, monkeypatch, "anthropic", {"model": "claude-sonnet-5-5"})
    assert "polish_chapter" not in _offered_tools(book, monkeypatch, "anthropic", {"model": "claude-sonnet-5-5", "polishModel": " claude-sonnet-5-5 "})
    assert "polish_chapter" in _offered_tools(book, monkeypatch, "anthropic", {"model": "claude-sonnet-5-5", "polishModel": "claude-haiku-5-5"})


def test_a_run_splits_a_chapter_and_the_new_one_lands_right_after_it(book, monkeypatch):
    """copy_paragraphs through the real document store: the new chapter is placed by sort order, text moved as stored."""
    server_content.save_document_content("alice", "book-1", "doc-1", "<p>甲。</p><p>乙。</p><p>丙。</p>")
    args = {"from_chapter": "1", "paragraphs": "2-3", "starts_with": "乙", "last_starts_with": "丙", "new_chapter": "Chapter 1b"}
    provider = Scripted([{"text": "", "calls": [("m1", "copy_paragraphs", json.dumps(args, ensure_ascii=False))]}, "好了。"], by_step=True)
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        queue = server_events.hub.subscribe("alice", "book-1")
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run, events_of(queue)
    run, events = asyncio.run(main())
    assert run.status == "done", run.error
    order = [d["title"] for d in server_documents.list_documents("alice", "book-1")]
    assert order == ["Chapter 1", "Chapter 1b", "Chapter 2"]
    new_id = next(d["id"] for d in server_documents.list_documents("alice", "book-1") if d["title"] == "Chapter 1b")
    assert strip_diff_markup(content_of(new_id)) == "<p>乙。</p><p>丙。</p>"
    assert strip_diff_markup(content_of("doc-1")) == "<p>甲。</p>"
    assert any(e.get("type") == "documents" and e.get("kind") == "reordered" for e in events)


def test_stop_ends_a_polish_and_keeps_the_draft(book, monkeypatch):
    server_content.save_document_content("alice", "book-1", "doc-1", "<p>她推开门，屋里没有人。</p>")
    provider = Scripted(["<p>她推开了门，屋里空无一人。</p>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        provider.gate = asyncio.Event()
        run = book.submit("alice", "book-1", _polish_request("en"))
        await asyncio.sleep(0.01)
        book.stop(run)
        provider.gate.set()
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "stopped"
    assert content_of("doc-1") == "<p>她推开门，屋里没有人。</p>"
    assert "Stopped" in message_row("a-润色《Chapter 1》")["content"]


def test_a_finished_run_remembers_its_token_ratio_for_the_next_budget(book, monkeypatch):
    """tokenCalibration.ts on the server: measured ÷ estimated per provider|model, clamped to [1, 3]."""
    class Counting(Scripted):
        async def __call__(self, job, provider, config, messages):
            usage = await super().__call__(job, provider, config, messages)
            return {**usage, "promptTokens": 10_000_000}
    provider = Counting(["好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "done", run.error
    state = server_context.load_state("alice", "book-1", "grok|grok-4.6")
    assert state["tokenRatio"] == 3.0
    assert server_context._token_ratio(state) == 3.0
    assert server_context._token_ratio({}) == 1.0 and server_context._token_ratio({"tokenRatio": 0.5}) == 1.0


def test_text_kept_for_a_missing_chapter_and_never_placed_is_noted_in_the_bubble(book, monkeypatch):
    call = {"text": "", "calls": [("c1", "update_document", json.dumps({"chapter": "9", "html": "<p>迷路的正文。</p>"}, ensure_ascii=False))]}
    provider = Scripted([call, "好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "done", run.error
    assert "KEPT" in json.dumps(provider.requests[1]["messages"], ensure_ascii=False)
    assert "was never placed, so it was discarded" in message_row("a-写第一章")["content"]


# ── a step that fails (agentic_chat_loop.md §0.10) ────────────────────────────

def test_a_step_that_broke_mid_reply_is_written_again(book, monkeypatch):
    import httpx
    provider = Scripted([{"text": "<canvas><p>半", "raise": httpx.ReadError("connection reset")},
                         "<canvas><p>beta</p></canvas>\n<doc_status>updated</doc_status>", "好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        queue = server_events.hub.subscribe("alice", "book-1")
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run, events_of(queue)
    run, events = asyncio.run(main())
    assert run.status == "done"
    assert strip_diff_markup(content_of("doc-1")) == "<p>beta</p>"
    runs = [e for e in events if e.get("type") == "run"]
    assert [e["step"] for e in runs if e["kind"] == "step_started"][:2] == [0, 0]
    assert any(e["kind"] == "progress" and "connection dropped" in (e.get("line") or "") for e in runs)


def test_a_looping_reply_is_retried_with_a_reminder_then_pauses_and_resume_goes_without_the_check(book, monkeypatch):
    loop = {"text": "The lantern swung. " * 30, "triggers": ["tail_repetition:16@response"],
            "raise": server_generation.ProviderError("xAI API error: Internal error during token generation", transient=True)}
    provider = Scripted([loop, loop, "<canvas><p>gamma</p></canvas>\n<doc_status>updated</doc_status>", "好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        paused = (run.status, dict(run.pause or {}), bool((run.snapshot or {}).get("next")))
        assert book.resume(run)
        await settle(run)
        return run, paused
    run, (status, pause, has_next) = asyncio.run(main())
    assert status == "paused" and pause["reason"] == "repeating_output" and pause["triggers"] == ["tail_repetition:16@response"]
    assert has_next
    assert provider.requests[0]["config"].get("loopCheck") is True
    assert "stopped because it began repeating" in provider.requests[1]["messages"][-1]["content"]
    assert provider.requests[2]["config"].get("loopCheck") is None
    assert provider.requests[3]["config"].get("loopCheck") is True
    assert run.status == "done" and strip_diff_markup(content_of("doc-1")) == "<p>gamma</p>"


def test_a_step_that_never_gets_through_pauses_the_run_and_resume_sends_it_again(book, monkeypatch):
    busy = {"raise": server_generation.ProviderError("xAI API error (503): busy", status=503)}
    provider = Scripted([busy] * 5 + ["<canvas><p>delta</p></canvas>\n<doc_status>updated</doc_status>", "好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        paused = (run.status, dict(run.pause or {}))
        book.resume(run)
        await settle(run)
        return run, paused
    run, (status, pause) = asyncio.run(main())
    assert status == "paused" and pause["reason"] == "step_failed" and "503" in pause["message"]
    assert len(provider.requests) == 7
    assert run.status == "done" and strip_diff_markup(content_of("doc-1")) == "<p>delta</p>"


def test_a_step_paused_before_its_first_reply_resumes_after_a_restart(book, monkeypatch):
    busy = {"raise": server_generation.ProviderError("xAI API error (503): busy", status=503)}
    provider = Scripted([busy] * 5 + ["<canvas><p>epsilon</p></canvas>\n<doc_status>updated</doc_status>", "好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def first():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run.id, run.status
    run_id, status = asyncio.run(first())
    assert status == "paused"
    fresh = server_runs.RunEngine()
    monkeypatch.setattr(server_runs, "engine", fresh)

    async def second():
        await fresh.recover()
        run = fresh.get("alice", run_id)
        assert run is not None and run.status == "paused"
        assert fresh.resume(run)
        await settle(run)
        return run
    run = asyncio.run(second())
    assert run.status == "done" and strip_diff_markup(content_of("doc-1")) == "<p>epsilon</p>"


def test_a_rejection_retrying_cannot_fix_still_ends_the_run(book, monkeypatch):
    provider = Scripted([{"raise": server_generation.ProviderError("xAI API error (401): bad key", status=401)}])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "error" and "401" in (run.error or "") and len(provider.requests) == 1


def test_a_steer_reaches_the_running_run_and_is_refused_otherwise(book, monkeypatch):
    provider = Scripted(["<canvas><p>beta</p></canvas>\n<doc_status>updated</doc_status>", "好的。\n<doc_status>unchanged</doc_status>"])
    provider.gate = asyncio.Event()
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        await asyncio.sleep(0.01)
        taken = book.steer(run, "标题改成《初雪》")
        snap_has_it = (run.snapshot or {}).get("pendingSteers")
        provider.gate.set()
        await settle(run)
        refused = book.steer(run, "late")
        return run, taken, snap_has_it, refused
    run, taken, snap_has_it, refused = asyncio.run(main())
    assert taken is True and snap_has_it == ["标题改成《初雪》"] and refused is False
    assert run.status == "done"
    second = provider.requests[1]["messages"]
    assert second[-1]["role"] == "user" and second[-1]["content"].endswith("USER MESSAGE:\n标题改成《初雪》")
    assert "The user sent this message while you were working" in second[-1]["content"]


def test_history_past_the_window_is_summarized_and_the_note_stored(book, monkeypatch):
    provider = Scripted(["<summary>\n1. Requests: everything so far.\n</summary>", "好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)
    history = [{"id": f"h{i}", "role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "word " * 1200} for i in range(16)]

    async def main():
        run = book.submit("alice", "book-1", request("继续", history=history, contextWindowTokens=8000))
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "done"
    assert "You summarize the earlier part of a conversation" in provider.requests[0]["messages"][0]["content"]
    turn = provider.requests[1]["messages"]
    note_at = next(i for i, m in enumerate(turn) if "<conversation_summary>\n1. Requests: everything so far.\n</conversation_summary>" in (m.get("content") or ""))
    assert turn[note_at + 1]["content"] == "Understood. I will continue from this summary."
    assert not any((m.get("content") or "").startswith("turn 0 ") for m in turn)
    assert any((m.get("content") or "").startswith("turn 14 ") for m in turn)
    stored = server_context.load_chat_summary("alice", "book-1")
    assert stored["text"] == "1. Requests: everything so far."
    # The note's key is the first message kept verbatim, right after the acknowledgement.
    kept_first = turn[note_at + 2]
    assert kept_first["role"] == "user" and kept_first["content"].startswith(f"turn {stored['upToId'][1:]} ")


def test_the_turn_after_a_stop_carries_the_interrupt_reminder(book, monkeypatch):
    provider = Scripted(["好的。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)
    history = [{"id": "u0", "role": "user", "content": "写第一章"},
               {"id": "a0", "role": "assistant", "content": "写到一半\n\n⏹️ Stopped.", "agent": {"status": "stopped", "steps": 1, "trace": [], "touched": [], "timeline": []}}]

    async def main():
        run = book.submit("alice", "book-1", request("继续", history=history))
        await settle(run)
        return run
    asyncio.run(main())
    last = provider.requests[0]["messages"][-1]["content"]
    assert "USER REQUEST:\n继续" in last and "The user stopped your previous turn before it finished" in last


def test_repeating_steps_are_nudged_then_paused_and_resume_continues(book, monkeypatch):
    same = {"text": "", "calls": [("c", "list_chapters", "{}")]}
    provider = Scripted([same] * 6 + ["完成。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        paused_status = run.status
        pause = dict(run.pause or {})
        assert book.resume(run)
        await settle(run)
        return run, paused_status, pause
    run, paused_status, pause = asyncio.run(main())
    # The loop told the model at the third identical step (in that step's results)…
    nudged = provider.requests[3]["messages"][-1]["content"]
    assert "<system-reminder>" in nudged and "the same call (list_chapters)" in nudged
    assert "<system-reminder>" not in provider.requests[2]["messages"][-1]["content"]
    # …and the engine paused at the sixth, with the last steps for the user.
    assert paused_status == "paused" and pause["reason"] == "repeating" and len(pause["steps"]) == 3
    assert run.status == "done" and run.record["steps"] == 7


def test_ask_user_pauses_the_run_and_the_answer_continues_it(book, monkeypatch):
    provider = Scripted([{"text": "", "calls": [("q1", "ask_user", '{"question":"删掉第二章？","options":["删 (Recommended)","留着"]}')]},
                         "好，留着。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        queue = server_events.hub.subscribe("alice", "book-1")
        run = book.submit("alice", "book-1", request())
        await settle(run)
        status, pause = run.status, dict(run.pause or {})
        assert book.answer(run, "留着")
        await settle(run)
        return run, status, pause, events_of(queue)
    run, status, pause, events = asyncio.run(main())
    assert status == "paused" and pause["reason"] == "question" and pause["question"] == "删掉第二章？" and pause["options"] == ["删 (Recommended)", "留着"]
    assert run.status == "done"
    answered = provider.requests[1]["messages"]
    assert answered[-1] == {"role": "user", "content": "The user answered: 留着"}
    assert any(m.get("role") == "tool" and m.get("toolCallId") == "q1" for m in answered)
    assert any(e.get("kind") == "paused" and e["pause"]["reason"] == "question" for e in events if e.get("type") == "run")


def test_a_question_asked_before_a_restart_can_still_be_answered(book, monkeypatch):
    """2026-10-18: a deploy restarted the API three seconds after the run asked; every answer got 409."""
    provider = Scripted([{"text": "", "calls": [("q1", "ask_user", '{"question":"继续哪一章？","options":["第二章","第五章"]}')]},
                         "好，写第五章。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def first():
        run = book.submit("alice", "book-1", request())
        await settle(run)
        return run.id, run.status
    run_id, status = asyncio.run(first())
    assert status == "paused"
    fresh = server_runs.RunEngine()
    monkeypatch.setattr(server_runs, "engine", fresh)

    async def second():
        await fresh.recover()
        run = fresh.get("alice", run_id)
        assert run is not None and run.status == "paused" and run.agent is None
        assert fresh.answer(run, "第五章")
        await settle(run)
        return run
    run = asyncio.run(second())
    assert run.status == "done", run.error
    answered = provider.requests[-1]["messages"]
    assert answered[-1] == {"role": "user", "content": "The user answered: 第五章"}
    assert any(m.get("role") == "tool" and m.get("toolCallId") == "q1" for m in answered)


def test_a_chapter_with_text_is_deleted_only_by_the_delete_button(book, monkeypatch):
    import server_documents

    def scripted():
        return Scripted([{"text": "", "calls": [("d1", "delete_chapter", '{"chapter":"2"}')]}, "好。\n<doc_status>unchanged</doc_status>"])

    async def main(answer, confirm):
        provider = scripted()
        monkeypatch.setattr(server_generation, "_dispatch_provider", provider)
        run = book.submit("alice", "book-1", request())
        await settle(run)
        pause = dict(run.pause or {})
        assert book.answer(run, answer, confirm=confirm)
        await settle(run)
        return run, pause, provider
    # A typed answer keeps the chapter, whatever it says.
    run, pause, provider = asyncio.run(main("yes delete it", False))
    assert pause["reason"] == "question" and pause["confirm"]["documentId"] == "doc-2" and pause["confirm"]["chars"] == 3
    assert [d["id"] for d in server_documents.list_documents("alice", "book-1")] == ["doc-1", "doc-2"]
    assert '#2 "Chapter 2" was not deleted.' in provider.requests[1]["messages"][-1]["content"]
    # The Delete button deletes it, and the model is told.
    run, pause, provider = asyncio.run(main("删除 #2「Chapter 2」。", True))
    assert run.status == "done" and [d["id"] for d in server_documents.list_documents("alice", "book-1")] == ["doc-1"]
    assert '#2 "Chapter 2" was deleted, as the user confirmed.' in provider.requests[1]["messages"][-1]["content"]


def test_a_user_edit_during_the_run_is_reported_in_the_next_results(book, monkeypatch):
    provider = Scripted([{"text": "", "calls": [("c1", "read_chapter", '{"chapters":["2"],"format":"html"}')]},
                         {"text": "", "calls": [("c2", "grep", '{"pattern":"x"}')]}, "done.\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        while run.record.get("steps", 0) < 1:
            await asyncio.sleep(0.001)
        # The user saves chapter 2 while the second step runs.
        import server_documents
        server_documents.write_document("alice", "book-1", "doc-2", content="<p>two, edited by the user</p>", client_id="tab-9")
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "done"
    third = provider.requests[2]["messages"][-1]["content"]
    assert "<system-reminder>" in third and 'The user changed #2 "Chapter 2"' in third


def test_a_run_survives_a_restart_at_its_last_completed_step(book, monkeypatch):
    provider = Scripted([{"text": "", "calls": [("c1", "read_chapter", '{"chapters":["2"]}')]}, "<canvas><p>after restart</p></canvas>", "done."], by_step=True)
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request())
        # Let the first step finish, then "die" before the second completes.
        while run.record.get("steps", 0) < 1:
            await asyncio.sleep(0.001)
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
        assert run.status == "running"
        fresh = server_runs.RunEngine()
        monkeypatch.setattr(server_runs, "engine", fresh)
        await fresh.recover()
        revived = fresh.get("alice", run.id)
        await settle(revived)
        return revived
    revived = asyncio.run(main())
    assert revived.status == "done", revived.error
    assert strip_diff_markup(content_of("doc-1")) == "<p>after restart</p>"
    assert revived.record["steps"] == 3 and revived.record["trace"][0].startswith("📖 read")
    # The resumed step's request carried the read's result under its call id.
    replayed = provider.requests[-2]["messages"]
    assert any(m.get("role") == "tool" and m.get("toolCallId") == "c1" for m in replayed)


def test_endpoints_start_list_and_stop(book, monkeypatch):
    from fastapi.testclient import TestClient
    provider = Scripted(["<canvas><p>via http</p></canvas>", "ok"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)
    with TestClient(api_server.app) as client:
        client.cookies.update({"web_canvas_session": "sess-1", "csrf_token": "tok-1"})
        res = client.post("/api/books/book-1/runs", json=request(), headers={"x-csrf-token": "tok-1", "x-client-id": "tab-1"})
        assert res.status_code == 200, res.text
        run_id = res.json()["run"]["id"]
        for _ in range(200):
            listed = client.get("/api/books/book-1/runs").json()
            if listed["runs"][0]["status"] == "done":
                break
            import time
            time.sleep(0.01)
        assert listed["runs"][0]["status"] == "done" and listed["runs"][0]["result"]["record"]["steps"] == 2
        assert client.get(f"/api/books/book-1/runs/{run_id}").json()["run"]["id"] == run_id
        assert client.post(f"/api/books/book-1/runs/{run_id}/stop", headers={"x-csrf-token": "tok-1"}).status_code == 200
        # Gone from memory (the TTL, a restart): the run alone is still read, with its result, from its row.
        server_runs.engine.runs.pop(run_id)
        alone = client.get(f"/api/books/book-1/runs/{run_id}")
        assert alone.status_code == 200 and alone.json()["run"]["status"] == "done"
        assert alone.json()["run"]["result"]["record"]["steps"] == 2
        assert client.get("/api/books/book-1/runs/run-missing").status_code == 404
        bad = client.post("/api/books/book-1/runs", json={"prompt": ""}, headers={"x-csrf-token": "tok-1"})
        assert bad.status_code == 400
    assert strip_diff_markup(content_of("doc-1")) == "<p>via http</p>"

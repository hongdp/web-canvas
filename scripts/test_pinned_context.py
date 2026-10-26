"""Pinned context (docs/features/pinned_context.md): with the agent tools on,
a run carries only the chapters the writer pinned, names no label in the
bubble, and a pin is chapter metadata that does not bump the revision."""
import asyncio
import json

import server_db
import server_events
import server_generation
from test_runs import Scripted, book, message_row, request, settle  # noqa: F401 — `book` is a fixture


def pin(doc_id: str, value: int = 1) -> None:
    conn = server_db.get_db()
    try:
        conn.execute("UPDATE documents SET pinned = ? WHERE id = ?", (value, doc_id))
        conn.commit()
    finally:
        conn.close()


def run_once(engine, monkeypatch, prompt: str):
    provider = Scripted(["好。\n<doc_status>unchanged</doc_status>"])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = engine.submit("alice", "book-1", request(prompt))
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "done", run.error
    return provider.requests[0]["messages"], run


def test_a_pinned_chapter_rides_ahead_of_the_history(book, monkeypatch):  # noqa: F811
    pin("doc-2")
    messages, run = run_once(book, monkeypatch, "写第一章")
    ledger = [m for m in messages if "REFERENCED CHAPTERS" in (m.get("content") or "")]
    assert ledger and "two" in ledger[0]["content"]
    assert "two" not in messages[-1]["content"].split("USER REQUEST")[1]
    # No [Attached Context] label on an agent turn.
    assert "[Attached Context" not in message_row(f"a-写第一章")["content"]
    assert not run.request.get("attachmentsText")


def test_a_chapter_the_request_names_is_not_attached_unless_pinned(book, monkeypatch):  # noqa: F811
    messages, _ = run_once(book, monkeypatch, "照着 Chapter 2 写")
    assert not any("REFERENCED CHAPTERS" in (m.get("content") or "") for m in messages)
    # The index still lists it, for the model to read.
    assert "Chapter 2" in messages[-1]["content"]


def test_a_pin_is_metadata_and_tells_the_other_tabs(book):  # noqa: F811
    from fastapi.testclient import TestClient
    import api_server

    async def subscribe():
        return server_events.hub.subscribe("alice", "book-1")
    queue = asyncio.run(subscribe())
    with TestClient(api_server.app) as client:
        client.cookies.update({"web_canvas_session": "sess-1", "csrf_token": "tok-1"})
        before = client.get("/api/books/book-1").json()["documents"]
        rev = next(d for d in before if d["id"] == "doc-2")["revision"]
        assert next(d for d in before if d["id"] == "doc-2")["pinned"] is False
        res = client.put("/api/books/book-1/documents/doc-2", json={"pinned": True}, headers={"x-csrf-token": "tok-1", "x-client-id": "tab-9"})
        assert res.status_code == 200, res.text
        after = client.get("/api/books/book-1").json()["documents"]
        d2 = next(d for d in after if d["id"] == "doc-2")
        assert d2["pinned"] is True and d2["revision"] == rev
        assert client.get("/api/books/book-1/documents/doc-2").json()["pinned"] is True
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert any(e.get("type") == "document" and e.get("kind") == "pinned" and e.get("documentId") == "doc-2" for e in events)


def test_an_open_pinned_chapter_keeps_its_place_and_an_edit_is_flagged(book, monkeypatch):  # noqa: F811
    import server_content
    pin("doc-2")

    def run_with(prompt, active):
        provider = Scripted(["好。\n<doc_status>unchanged</doc_status>"])
        monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

        async def main():
            run = book.submit("alice", "book-1", request(prompt, activeDocumentId=active))
            await settle(run)
            return run
        run = asyncio.run(main())
        assert run.status == "done", run.error
        msgs = provider.requests[0]["messages"]
        return next(m["content"] for m in msgs if "REFERENCED CHAPTERS" in (m.get("content") or "")), msgs[-1]["content"]

    ledger1, _ = run_with("第一轮", "doc-1")
    ledger2, tail2 = run_with("第二轮", "doc-2")  # opened: still in place, same bytes
    assert ledger2 == ledger1 and "is older" not in tail2
    server_content.save_document_content("alice", "book-1", "doc-2", "<p>two, revised</p>")
    ledger3, tail3 = run_with("第三轮", "doc-2")
    assert ledger3 == ledger1
    assert "Its copy under REFERENCED CHAPTERS is older: this is its current text" in tail3

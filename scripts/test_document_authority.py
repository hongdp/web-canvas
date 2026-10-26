"""Revisioned chapter writes and book events (backend_authority.md phase 1).

The server decides what a chapter is: a write that names the revision it
was based on is refused when another write landed first, and every change
is published to the book's open tabs.
"""
import asyncio
import json
import os
import sqlite3

import api_server
import server_content
import server_db
import server_events
from test_api_server import _seed_book


def _call(coro):
    return asyncio.run(coro)


def _body(response):
    """A route's result: a plain dict, or a JSONResponse's decoded body."""
    if isinstance(response, dict):
        return 200, response
    return response.status_code, json.loads(response.body)


def _setup(tmp_path, monkeypatch):
    make_request = _seed_book(tmp_path, monkeypatch)
    monkeypatch.setattr(server_content, "CONTENT_DIR", str(tmp_path / "content"))
    return make_request


def _put(make_request, body):
    return _body(_call(api_server.update_document(
        make_request("PUT", "/api/books/book-1/documents/doc-1", body), "book-1", "doc-1")))


def _revision():
    conn = server_db.get_db()
    try:
        return conn.execute("SELECT revision FROM documents WHERE id = 'doc-1'").fetchone()["revision"]
    finally:
        conn.close()


def test_a_write_based_on_the_current_revision_lands_and_bumps_it(tmp_path, monkeypatch):
    make_request = _setup(tmp_path, monkeypatch)
    status, body = _put(make_request, {"content": "<p>v2</p>", "baseRevision": 1})
    assert status == 200
    assert body["revision"] == 2
    assert server_content.load_document_content("alice", "book-1", "doc-1") == "<p>v2</p>"


def test_a_stale_write_is_refused_with_the_chapter_as_it_is_now(tmp_path, monkeypatch):
    make_request = _setup(tmp_path, monkeypatch)
    _put(make_request, {"content": "<p>from tab A</p>", "baseRevision": 1})
    # Tab B still holds revision 1.
    status, body = _put(make_request, {"content": "<p>from tab B</p>", "baseRevision": 1})
    assert status == 409
    assert body["revision"] == 2
    assert body["content"] == "<p>from tab A</p>"
    # Nothing of B's write landed.
    assert server_content.load_document_content("alice", "book-1", "doc-1") == "<p>from tab A</p>"
    assert _revision() == 2


def test_a_write_without_a_base_still_wins_as_before(tmp_path, monkeypatch):
    make_request = _setup(tmp_path, monkeypatch)
    _put(make_request, {"content": "<p>a</p>", "baseRevision": 1})
    status, body = _put(make_request, {"content": "<p>older client</p>"})
    assert status == 200
    assert body["revision"] == 3


def test_a_summary_save_does_not_bump_the_revision(tmp_path, monkeypatch):
    # A background summary would otherwise turn every open tab's next write
    # into a conflict.
    make_request = _setup(tmp_path, monkeypatch)
    status, body = _put(make_request, {"summary": "S", "summaryContentHash": "h", "baseRevision": 7})
    assert status == 200
    assert body["revision"] == 1


def test_a_non_integer_base_is_a_bad_request(tmp_path, monkeypatch):
    make_request = _setup(tmp_path, monkeypatch)
    try:
        _put(make_request, {"content": "<p>x</p>", "baseRevision": "1"})
    except api_server.HTTPException as e:
        assert e.status_code == 400
    else:
        raise AssertionError("expected a 400")


def test_reads_carry_the_revision(tmp_path, monkeypatch):
    make_request = _setup(tmp_path, monkeypatch)
    _put(make_request, {"content": "<p>v2</p>", "baseRevision": 1})
    book = _call(api_server.get_book(make_request("GET", "/api/books/book-1"), "book-1"))
    assert book["documents"][0]["revision"] == 2
    doc = _call(api_server.get_document(make_request("GET", "/api/books/book-1/documents/doc-1"), "book-1", "doc-1"))
    assert doc["revision"] == 2


def test_writes_publish_events_for_the_book(tmp_path, monkeypatch):
    make_request = _setup(tmp_path, monkeypatch)

    async def scenario():
        queue = server_events.hub.subscribe("alice", "book-1")
        try:
            await api_server.update_document(
                make_request("PUT", "/api/books/book-1/documents/doc-1", {"content": "<p>v2</p>", "baseRevision": 1}),
                "book-1", "doc-1")
            # A summary-only save is not a change to the chapter: no event.
            await api_server.update_document(
                make_request("PUT", "/api/books/book-1/documents/doc-1", {"summary": "S"}), "book-1", "doc-1")
            await api_server.delete_document_endpoint(
                make_request("DELETE", "/api/books/book-1/documents/doc-1"), "book-1", "doc-1")
            return [queue.get_nowait() for _ in range(queue.qsize())]
        finally:
            server_events.hub.unsubscribe("alice", "book-1", queue)

    events = _call(scenario())
    assert [(e["type"], e["kind"]) for e in events] == [("document", "updated"), ("document", "deleted")]
    assert events[0]["revision"] == 2 and events[0]["documentId"] == "doc-1"
    assert events[1]["id"] > events[0]["id"]


def test_creating_an_existing_document_is_a_conflict_not_a_crash(tmp_path, monkeypatch):
    make_request = _setup(tmp_path, monkeypatch)
    try:
        _call(api_server.create_documents(make_request("POST", "/api/books/book-1/documents", {
            "documents": [{"id": "doc-1", "title": "again", "content": "<p>x</p>"}]
        }), "book-1"))
    except api_server.HTTPException as e:
        assert e.status_code == 409
    else:
        raise AssertionError("expected a 409")


def test_the_content_file_is_replaced_atomically(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    server_content.save_document_content("alice", "book-1", "doc-1", "<p>one</p>")
    server_content.save_document_content("alice", "book-1", "doc-1", "<p>two</p>")
    folder = tmp_path / "content" / "alice" / "book-1"
    assert sorted(os.listdir(folder)) == ["doc-doc-1.json"]
    assert server_content.load_document_content("alice", "book-1", "doc-1") == "<p>two</p>"


def test_init_db_adds_the_revision_column_to_an_existing_database(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE books (id TEXT, username TEXT, title TEXT, active_document_id TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY (username, id));
        CREATE TABLE documents (id TEXT NOT NULL, username TEXT NOT NULL, book_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'Untitled Chapter', sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (username, book_id, id));
        INSERT INTO documents (id, username, book_id, created_at, updated_at) VALUES ('d', 'u', 'b', 't', 't');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(server_db, "DB_PATH", str(db_path))
    server_db.init_db()
    conn = server_db.get_db()
    try:
        assert conn.execute("SELECT revision FROM documents WHERE id = 'd'").fetchone()["revision"] == 1
    finally:
        conn.close()


def test_a_full_subscriber_is_dropped_rather_than_grown_without_bound():
    hub = server_events.BookEventHub()
    queue = hub.subscribe("u", "b")
    for _ in range(server_events.QUEUE_LIMIT + 1):
        hub.publish("u", "b", {"type": "x"})
    assert queue.qsize() == server_events.QUEUE_LIMIT
    assert hub.subscriber_count("u", "b") == 0

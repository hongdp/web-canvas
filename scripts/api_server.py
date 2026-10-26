"""Web Canvas backend entry point: FastAPI app + books/documents/versions routes.

The helper layers live in focused sibling modules (scripts/ is NOT a package;
running `python3 scripts/api_server.py` puts this directory on sys.path, so
plain sibling imports work — the pytest flow does the same via sys.path.append):

- server_config    — CLI args, storage paths, JSON-file + validation helpers
- server_db        — SQLite path/pragmas, get_db, init_db (schema migrations)
- server_content   — document/version content-file save/load/delete
- server_auth      — sessions, CSRF middleware, /api/auth/* endpoints
- server_generation — resumable LLM generation jobs, /api/generate/*
- server_runs       — server-side agent runs, /api/books/{id}/runs*
- server_migration — one-time legacy state_*.json migration

Note for tests: patch state on the module that OWNS it (server_db.DB_PATH,
server_auth.SESSIONS_FILE) — patching an alias
re-exported here would not affect the executing code.
"""

import os
import sys
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

import server_config
import server_auth
import server_db
import server_static
import server_generation
import server_events
import server_runs
import server_documents
import server_attachments
import server_web
from server_config import sanitize_id
from server_db import (get_db, init_db, record_last_active_book, clear_last_active_book, upsert_user_settings, upsert_book_preferences,
                       apply_book_preferences)
from server_auth import get_authenticated_username
from server_content import (
    _get_content_dir,
    save_document_content,
    load_document_content,
    delete_document_content,
    save_version_content,
    load_version_content,
    delete_book_content_dir,
)
from server_migration import migrate_legacy_files

# Initialize FastAPI app

def _agent_json(msg):
    """An agentic turn's record (AgentTurnRecord), serialized for the messages
    table — or None for an ordinary message."""
    agent = msg.get("agent")
    return json.dumps(agent, ensure_ascii=False) if isinstance(agent, dict) else None


def _agent_field(row):
    """The record back out of a messages row, as the client sent it. A value
    that no longer parses is dropped rather than failing the whole load."""
    raw = row["agent"] if "agent" in row.keys() else None
    if not raw:
        return {}
    try:
        agent = json.loads(raw)
    except ValueError:
        return {}
    return {"agent": agent} if isinstance(agent, dict) else {}

def _reasoning_items_json(msg):
    """grok's reasoning items of a turn (ChatMessage.reasoningItems), as JSON, or None."""
    items = msg.get("reasoningItems")
    return json.dumps(items, ensure_ascii=False) if isinstance(items, list) and items else None


def _reasoning_items_field(row):
    raw = row["reasoning_items"] if "reasoning_items" in row.keys() else None
    if not raw:
        return {}
    try:
        items = json.loads(raw)
    except ValueError:
        return {}
    return {"reasoningItems": items} if isinstance(items, list) and items else {}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Runs that were running or queued when the process died continue."""
    await server_runs.engine.recover()
    yield


app = FastAPI(title="Web Canvas Backend API", version="2.0.0", lifespan=_lifespan)

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Initialize DB and run migration on startup ────────────────────────────────
init_db()
migrate_legacy_files()

# CSRF middleware + auth (/api/auth/*) routes
app.middleware("http")(server_auth.csrf_middleware)
app.include_router(server_auth.router)
# Resumable server-side generation jobs (/api/generate/*)
app.include_router(server_generation.router)
# Server-side agent runs (/api/books/{id}/runs*, backend_authority.md §4.3)
server_runs.ensure_tables()
server_attachments.ensure_tables()
app.include_router(server_runs.router)
app.include_router(server_attachments.router)
app.include_router(server_web.router)


# ============================================================
# NEW BOOK/DOCUMENT API (SQLite + Content Files)
# ============================================================

# ── List Books ─────────────────────────────────────────────────────────────────
@app.get("/api/books")
async def list_books(request: Request):
    username = get_authenticated_username(request)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM books WHERE username = ? ORDER BY updated_at DESC",
            (username,)
        ).fetchall()
        return [{"id": r["id"], "title": r["title"], "updatedAt": r["updated_at"]} for r in rows]
    finally:
        conn.close()

# ── Create Book ────────────────────────────────────────────────────────────────
@app.post("/api/books")
async def create_book(request: Request):
    username = get_authenticated_username(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    book_id = sanitize_id(body.get("id", f"book-{int(datetime.now().timestamp() * 1000)}"), "bookId")
    title = body.get("title", "New Book")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    conn = get_db()
    try:
        # Create book
        conn.execute(
            "INSERT INTO books (id, username, title, active_document_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (book_id, username, title, "", now, now)
        )

        # Create default document if provided
        documents = body.get("documents", [])
        for idx, doc in enumerate(documents):
            doc_id = sanitize_id(doc.get("id", f"doc-{int(datetime.now().timestamp() * 1000)}-{idx}"), "docId")
            doc_title = doc.get("title", f"Chapter {idx + 1}")
            doc_content = doc.get("content", "")
            doc_created = doc.get("createdAt", now)
            doc_updated = doc.get("updatedAt", now)

            conn.execute(
                "INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at, pinned) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, username, book_id, doc_title, idx, doc_created, doc_updated, 1 if doc.get("pinned") else 0)
            )
            save_document_content(username, book_id, doc_id, doc_content)

        # Set active document
        active_doc_id = body.get("activeDocumentId", documents[0]["id"] if documents else "")
        if active_doc_id:
            conn.execute(
                "UPDATE books SET active_document_id = ? WHERE username = ? AND id = ?",
                (active_doc_id, username, book_id)
            )

        record_last_active_book(conn, username, book_id, now)

        # Settings are the user's, shared by every book (user_settings); the book keeps
        # which provider, model, effort and preset it uses (book_preferences).
        upsert_user_settings(conn, username, body)
        upsert_book_preferences(conn, username, book_id, body)

        # Save initial messages
        messages = body.get("messages", [])
        for idx, msg in enumerate(messages):
            msg_id = msg.get("id", f"msg-{int(datetime.now().timestamp() * 1000)}-{idx}")
            conn.execute(
                """INSERT INTO messages (id, username, book_id, role, content, timestamp,
                   thinking, model, input_tokens, output_tokens, cache_hit_tokens, sort_order, agent, reasoning_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg_id, username, book_id,
                    msg.get("role", "assistant"),
                    msg.get("content", ""),
                    msg.get("timestamp", now),
                    msg.get("thinking"),
                    msg.get("model"),
                    msg.get("inputTokens"),
                    msg.get("outputTokens"),
                    msg.get("cacheHitTokens"),
                    idx,
                    _agent_json(msg),
                    _reasoning_items_json(msg)
                )
            )

        conn.commit()
        return {"success": True, "id": book_id}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Book already exists.")
    finally:
        conn.close()


# ── Get Book (metadata + document list + settings, NO content) ─────────────────
@app.get("/api/books/{book_id}")
async def get_book(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")

    conn = get_db()
    try:
        book = conn.execute(
            "SELECT * FROM books WHERE username = ? AND id = ?",
            (username, safe_book_id)
        ).fetchone()
        if not book:
            return {}

        # Get document list (metadata only, no content). Every column read in
        # the response below must be listed here — sqlite3.Row raises
        # IndexError for an unselected column, which 500s the whole endpoint
        # (this is how `summary`/`summary_content_hash` broke book switching).
        docs = conn.execute(
            "SELECT id, title, sort_order, created_at, updated_at, summary, summary_content_hash, revision, pinned "
            "FROM documents WHERE username = ? AND book_id = ? ORDER BY sort_order",
            (username, safe_book_id)
        ).fetchall()

        # The user's settings (shared by every book)
        settings = conn.execute("SELECT * FROM user_settings WHERE username = ?", (username,)).fetchone()

        # Get versions
        versions = conn.execute(
            "SELECT id, document_id, title, timestamp FROM versions WHERE username = ? AND book_id = ? ORDER BY timestamp DESC",
            (username, safe_book_id)
        ).fetchall()

        # Get messages
        msgs = conn.execute(
            "SELECT * FROM messages WHERE username = ? AND book_id = ? ORDER BY sort_order",
            (username, safe_book_id)
        ).fetchall()

        result = {
            "bookTitle": book["title"],
            "activeDocumentId": book["active_document_id"],
            "updatedAt": book["updated_at"],
            "documents": [
                {
                    "id": d["id"],
                    "title": d["title"],
                    "sortOrder": d["sort_order"],
                    "createdAt": d["created_at"],
                    "updatedAt": d["updated_at"],
                    "summary": d["summary"],
                    "summaryContentHash": d["summary_content_hash"],
                    "revision": d["revision"],
                    "pinned": bool(d["pinned"]),
                }
                for d in docs
            ],
            "versions": [
                {
                    "id": v["id"],
                    "documentId": v["document_id"],
                    "title": v["title"],
                    "timestamp": v["timestamp"],
                }
                for v in versions
            ],
            "messages": [
                {
                    "id": m["id"],
                    "role": m["role"],
                    "content": m["content"],
                    "timestamp": m["timestamp"],
                    **({"thinking": m["thinking"]} if m["thinking"] else {}),
                    **({"model": m["model"]} if m["model"] else {}),
                    **({"inputTokens": m["input_tokens"]} if m["input_tokens"] else {}),
                    **({"outputTokens": m["output_tokens"]} if m["output_tokens"] else {}),
                    **({"cacheHitTokens": m["cache_hit_tokens"]} if m["cache_hit_tokens"] else {}),
                    **_agent_field(m),
                    **_reasoning_items_field(m),
                }
                for m in msgs
            ],
        }

        # Add settings if they exist
        if settings:
            if settings["active_provider"]:
                result["activeProvider"] = settings["active_provider"]
            if settings["provider_configs"]:
                try:
                    result["providerConfigs"] = json.loads(settings["provider_configs"])
                except Exception:
                    pass
            if settings["custom_system_prompts"]:
                try:
                    result["customSystemPrompts"] = json.loads(settings["custom_system_prompts"])
                except Exception:
                    pass
            if settings["active_system_prompt_id"]:
                result["activeSystemPromptId"] = settings["active_system_prompt_id"]
            if settings["theme"]:
                result["theme"] = settings["theme"]
            result["debugMode"] = bool(settings["debug_mode"])
        # …with this book's own provider, model, effort and preset over them.
        apply_book_preferences(conn, username, safe_book_id, result)

        return result
    finally:
        conn.close()


# ── Update Book (metadata + settings) ─────────────────────────────────────────
@app.put("/api/books/{book_id}")
async def update_book(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn = get_db()
    try:
        # Ensure book exists
        book = conn.execute(
            "SELECT id FROM books WHERE username = ? AND id = ?",
            (username, safe_book_id)
        ).fetchone()
        if not book:
            raise HTTPException(status_code=404, detail="Book not found.")

        # Update book metadata
        updates = []
        params = []
        if "bookTitle" in body:
            updates.append("title = ?")
            params.append(body["bookTitle"])
        if "activeDocumentId" in body:
            updates.append("active_document_id = ?")
            params.append(body["activeDocumentId"])
        updates.append("updated_at = ?")
        params.append(now)
        params.extend([username, safe_book_id])

        conn.execute(
            f"UPDATE books SET {', '.join(updates)} WHERE username = ? AND id = ?",
            params
        )
        # This PUT is what every debounced save sends for the book on screen, so
        # it is the natural place to remember which book the user is in.
        record_last_active_book(conn, username, safe_book_id, now)

        # Settings are the user's, shared by every book (user_settings); the book keeps
        # which provider, model, effort and preset it uses (book_preferences).
        upsert_user_settings(conn, username, body)
        upsert_book_preferences(conn, username, safe_book_id, body)

        # Update messages if provided (full replace)
        if "messages" in body:
            conn.execute(
                "DELETE FROM messages WHERE username = ? AND book_id = ?",
                (username, safe_book_id)
            )
            for idx, msg in enumerate(body["messages"]):
                msg_id = msg.get("id", f"msg-{int(datetime.now().timestamp() * 1000)}-{idx}")
                conn.execute(
                    """INSERT INTO messages (id, username, book_id, role, content, timestamp,
                       thinking, model, input_tokens, output_tokens, cache_hit_tokens, sort_order, agent, reasoning_items)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        msg_id, username, safe_book_id,
                        msg.get("role", "assistant"),
                        msg.get("content", ""),
                        msg.get("timestamp", now),
                        msg.get("thinking"),
                        msg.get("model"),
                        msg.get("inputTokens"),
                        msg.get("outputTokens"),
                        msg.get("cacheHitTokens"),
                        idx,
                        _agent_json(msg),
                    _reasoning_items_json(msg)
                    )
                )

        # Update document ordering if provided
        if "documentOrder" in body:
            doc_ids = body["documentOrder"]
            for idx, doc_id in enumerate(doc_ids):
                safe_doc_id = sanitize_id(doc_id, "docId")
                conn.execute(
                    "UPDATE documents SET sort_order = ? WHERE username = ? AND book_id = ? AND id = ?",
                    (idx, username, safe_book_id, safe_doc_id)
                )

        conn.commit()
        # Return the server-side timestamp so the client can tell ITS OWN write
        # apart from another device's. Comparing a server timestamp against
        # the client clock is unreliable — devices drift by minutes — and a
        # false "someone else changed this" verdict forces a full reload.
        return {"success": True, "updatedAt": now}
    finally:
        conn.close()


# ── Delete Book ────────────────────────────────────────────────────────────────
@app.delete("/api/books/{book_id}")
async def delete_book_endpoint(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")

    conn = get_db()
    try:
        conn.execute("DELETE FROM books WHERE username = ? AND id = ?", (username, safe_book_id))
        conn.execute("DELETE FROM messages WHERE username = ? AND book_id = ?", (username, safe_book_id))
        conn.execute("DELETE FROM book_preferences WHERE username = ? AND book_id = ?", (username, safe_book_id))
        clear_last_active_book(conn, username, safe_book_id)
        # documents and versions cascade-deleted via FK
        conn.commit()
    finally:
        conn.close()

    # Delete content files
    server_attachments.delete_book_attachments(username, safe_book_id)
    server_runs.server_context.delete_book_context(username, safe_book_id)
    delete_book_content_dir(username, safe_book_id)
    return {"success": True}


# ── Reorder Documents ──────────────────────────────────────────────────────────
# Registered BEFORE the `/documents/{doc_id}` routes on purpose: FastAPI matches
# in registration order, so `PUT /documents/reorder` would otherwise be captured
# by `PUT /documents/{doc_id}` as doc_id="reorder" and 404 — which silently broke
# chapter reordering (the client swallows a non-OK response). Keep it ahead.
@app.put("/api/books/{book_id}/documents/reorder")
async def reorder_documents(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    doc_ids = body.get("documentIds", [])
    if not doc_ids:
        raise HTTPException(status_code=400, detail="documentIds is required.")

    now = server_documents.reorder_documents(username, safe_book_id, [sanitize_id(d, "docId") for d in doc_ids],
                                             client_id=server_events.client_id_of(request))
    return {"success": True, "updatedAt": now}


# ── Get Document Content ───────────────────────────────────────────────────────
@app.get("/api/books/{book_id}/documents/{doc_id}")
async def get_document(request: Request, book_id: str, doc_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    safe_doc_id = sanitize_id(doc_id, "docId")

    conn = get_db()
    try:
        doc = conn.execute(
            "SELECT * FROM documents WHERE username = ? AND book_id = ? AND id = ?",
            (username, safe_book_id, safe_doc_id)
        ).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")
    finally:
        conn.close()

    content = load_document_content(username, safe_book_id, safe_doc_id)
    return {
        "id": doc["id"],
        "title": doc["title"],
        "content": content,
        "sortOrder": doc["sort_order"],
        "createdAt": doc["created_at"],
        "updatedAt": doc["updated_at"],
        "summary": doc["summary"],
        "summaryContentHash": doc["summary_content_hash"],
        "revision": doc["revision"],
        "pinned": bool(doc["pinned"]),
    }


# ── Book change events (backend_authority.md §2.2) ────────────────────────────
@app.get("/api/books/{book_id}/events")
async def book_events(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    return StreamingResponse(
        server_events.event_stream(username, safe_book_id, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Save Document Content ─────────────────────────────────────────────────────
@app.put("/api/books/{book_id}/documents/{doc_id}")
async def update_document(request: Request, book_id: str, doc_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    safe_doc_id = sanitize_id(doc_id, "docId")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Text and title are the chapter; a summary is metadata about it. Only a
    # change to the chapter bumps its revision — a background summary save
    # must not turn every open tab's next write into a conflict.
    changes_chapter = "content" in body or "title" in body
    base_revision = body.get("baseRevision")
    if base_revision is not None and not isinstance(base_revision, int):
        raise HTTPException(status_code=400, detail="baseRevision must be an integer.")

    conn = get_db()
    try:
        doc = conn.execute(
            "SELECT id FROM documents WHERE username = ? AND book_id = ? AND id = ?",
            (username, safe_book_id, safe_doc_id)
        ).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")

        updates = []
        params = []
        if "title" in body:
            updates.append("title = ?")
            params.append(body["title"])
        if "summary" in body:
            updates.append("summary = ?")
            params.append(body["summary"])
        if "summaryContentHash" in body:
            updates.append("summary_content_hash = ?")
            params.append(body["summaryContentHash"])
        if "pinned" in body:
            # Metadata like the summary: no revision bump (pinned_context.md §2).
            updates.append("pinned = ?")
            params.append(1 if body["pinned"] else 0)
        updates.append("updated_at = ?")
        params.append(now)
        if changes_chapter:
            updates.append("revision = revision + 1")

        # The revision check and the write are one statement: no other
        # writer can land between them (backend_authority.md §2.1). A write
        # without a base revision (an older client) still wins, as before.
        where = "username = ? AND book_id = ? AND id = ?"
        where_params = [username, safe_book_id, safe_doc_id]
        if changes_chapter and base_revision is not None:
            where += " AND revision = ?"
            where_params.append(base_revision)
        cursor = conn.execute(f"UPDATE documents SET {', '.join(updates)} WHERE {where}", params + where_params)

        if cursor.rowcount == 0:
            # Another write landed since the client's base: send it the
            # chapter as it is now, so it can keep its own text aside and
            # adopt this one instead of overwriting it.
            current = conn.execute(
                "SELECT title, revision, updated_at FROM documents WHERE username = ? AND book_id = ? AND id = ?",
                (username, safe_book_id, safe_doc_id)
            ).fetchone()
            return JSONResponse(status_code=409, content={
                "error": "conflict",
                "revision": current["revision"],
                "title": current["title"],
                "content": load_document_content(username, safe_book_id, safe_doc_id),
                "updatedAt": current["updated_at"],
            })

        conn.execute(
            "UPDATE books SET updated_at = ? WHERE username = ? AND id = ?",
            (now, username, safe_book_id)
        )
        revision = conn.execute(
            "SELECT revision FROM documents WHERE username = ? AND book_id = ? AND id = ?",
            (username, safe_book_id, safe_doc_id)
        ).fetchone()["revision"]
        # Written before the commit: a revision is never visible ahead of the
        # text it stands for. (The file write is atomic; see server_content.)
        if "content" in body:
            save_document_content(username, safe_book_id, safe_doc_id, body["content"])
        conn.commit()
    finally:
        conn.close()

    if changes_chapter:
        server_events.hub.publish(username, safe_book_id, {
            "type": "document", "kind": "updated", "documentId": safe_doc_id,
            "revision": revision, "clientId": server_events.client_id_of(request)
        })
    elif "pinned" in body:
        # Other tabs resync the chapter list, which carries the pin.
        server_events.hub.publish(username, safe_book_id, {
            "type": "document", "kind": "pinned", "documentId": safe_doc_id,
            "revision": revision, "clientId": server_events.client_id_of(request)
        })
    # The write bumped the book's updated_at; return the stamp so the client
    # can adopt it as "seen". Without this every focus-time check after a
    # document sync compared a stale baseline against our own write and
    # reloaded the whole book — resetting the reader to the top of the chapter.
    return {"success": True, "updatedAt": now, "revision": revision}


# ── Batch Create/Replace Documents ─────────────────────────────────────────────
@app.post("/api/books/{book_id}/documents")
async def create_documents(request: Request, book_id: str):
    """Create or batch-replace documents in a book."""
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    documents = body.get("documents", [])
    replace_all = body.get("replaceAll", False)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    conn = get_db()
    try:
        # Ensure book exists
        book = conn.execute(
            "SELECT id FROM books WHERE username = ? AND id = ?",
            (username, safe_book_id)
        ).fetchone()
        if not book:
            raise HTTPException(status_code=404, detail="Book not found.")

        if replace_all:
            # Delete existing documents
            existing_docs = conn.execute(
                "SELECT id FROM documents WHERE username = ? AND book_id = ?",
                (username, safe_book_id)
            ).fetchall()
            for d in existing_docs:
                delete_document_content(username, safe_book_id, d["id"])
            conn.execute(
                "DELETE FROM documents WHERE username = ? AND book_id = ?",
                (username, safe_book_id)
            )

        # Problem: a batch append landed documents at 14, 16, 19, 23, 28, ...
        #   instead of 14, 15, 16, ...; a long append ended with sort_order
        #   far past its chapter count.
        # Root Cause: MAX(sort_order) was re-read on every iteration, so it
        #   already counted the rows this same request had just inserted, and
        #   the `+ idx` term then counted them a second time.
        # Fix: read the append base once, before inserting anything.
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) as m FROM documents WHERE username = ? AND book_id = ?",
            (username, safe_book_id)
        ).fetchone()["m"]

        created_ids = []
        for idx, doc in enumerate(documents):
            doc_id = sanitize_id(doc.get("id", f"doc-{int(datetime.now().timestamp() * 1000)}-{idx}"), "docId")
            doc_title = doc.get("title", f"Chapter {idx + 1}")
            doc_content = doc.get("content", "")
            doc_created = doc.get("createdAt", now)
            doc_updated = doc.get("updatedAt", now)

            # replace_all wiped the table above, so the base is -1 either way.
            sort_order = idx if replace_all else max_order + 1 + idx

            try:
                conn.execute(
                    "INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at, summary, summary_content_hash, pinned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, username, safe_book_id, doc_title, sort_order, doc_created, doc_updated,
                     doc.get("summary"), doc.get("summaryContentHash"), 1 if doc.get("pinned") else 0)
                )
            except sqlite3.IntegrityError:
                # Used to surface as a 500. The id exists: it is a conflict,
                # as it is when creating a book (create_book).
                raise HTTPException(status_code=409, detail=f"Document {doc_id} already exists.")
            save_document_content(username, safe_book_id, doc_id, doc_content)
            created_ids.append(doc_id)

        # Update book updated_at
        conn.execute(
            "UPDATE books SET updated_at = ? WHERE username = ? AND id = ?",
            (now, username, safe_book_id)
        )

        conn.commit()
        server_events.hub.publish(username, safe_book_id, {
            "type": "documents", "kind": "replaced" if replace_all else "created",
            "documentIds": created_ids, "clientId": server_events.client_id_of(request)
        })
        return {"success": True, "ids": created_ids, "updatedAt": now, "revision": 1}
    finally:
        conn.close()


# ── Delete Document ────────────────────────────────────────────────────────────
@app.delete("/api/books/{book_id}/documents/{doc_id}")
async def delete_document_endpoint(request: Request, book_id: str, doc_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    safe_doc_id = sanitize_id(doc_id, "docId")

    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM documents WHERE username = ? AND book_id = ? AND id = ?",
            (username, safe_book_id, safe_doc_id)
        )
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        conn.execute(
            "UPDATE books SET updated_at = ? WHERE username = ? AND id = ?",
            (now, username, safe_book_id)
        )
        conn.commit()
    finally:
        conn.close()

    delete_document_content(username, safe_book_id, safe_doc_id)
    server_events.hub.publish(username, safe_book_id, {
        "type": "document", "kind": "deleted", "documentId": safe_doc_id,
        "clientId": server_events.client_id_of(request)
    })
    return {"success": True, "updatedAt": now}


# ── Get Version Content ────────────────────────────────────────────────────────
@app.get("/api/books/{book_id}/versions/{version_id}")
async def get_version(request: Request, book_id: str, version_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    safe_ver_id = sanitize_id(version_id, "versionId")

    conn = get_db()
    try:
        ver = conn.execute(
            "SELECT * FROM versions WHERE username = ? AND book_id = ? AND id = ?",
            (username, safe_book_id, safe_ver_id)
        ).fetchone()
        if not ver:
            raise HTTPException(status_code=404, detail="Version not found.")
    finally:
        conn.close()

    content = load_version_content(username, safe_book_id, safe_ver_id)
    return {
        "id": ver["id"],
        "documentId": ver["document_id"],
        "title": ver["title"],
        "timestamp": ver["timestamp"],
        "content": content,
    }


# ── Create Version Snapshot ────────────────────────────────────────────────────
@app.post("/api/books/{book_id}/versions")
async def create_version(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    ver_id = sanitize_id(body.get("id", f"ver-{int(datetime.now().timestamp() * 1000)}"), "versionId")
    doc_id = body.get("documentId", "")
    title = body.get("title", "Untitled Version")
    content = body.get("content", "")
    timestamp = body.get("timestamp", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO versions (id, username, book_id, document_id, title, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (ver_id, username, safe_book_id, doc_id, title, timestamp)
        )
        conn.commit()
    finally:
        conn.close()

    save_version_content(username, safe_book_id, ver_id, content)
    return {"success": True, "id": ver_id}


# ── Delete Version ─────────────────────────────────────────────────────────────
@app.delete("/api/books/{book_id}/versions/{version_id}")
async def delete_version(request: Request, book_id: str, version_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    safe_ver_id = sanitize_id(version_id, "versionId")

    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM versions WHERE username = ? AND book_id = ? AND id = ?",
            (username, safe_book_id, safe_ver_id)
        )
        conn.commit()
    finally:
        conn.close()

    # Delete version content file
    content_dir = _get_content_dir(username, safe_book_id)
    ver_file = os.path.join(content_dir, f"ver-{safe_ver_id}.json")
    if os.path.exists(ver_file):
        os.remove(ver_file)

    return {"success": True}


def _configure_app_logging() -> None:
    """Let this app's own INFO lines through.

    Problem: nothing this server logged with logger.info ever reached the log —
      time-to-first-token, job starts, all silently dropped.
    Root cause: uvicorn.run(log_level="warning") configures the ROOT logger at
      WARNING, and every "web_canvas.*" logger inherits it.
    Fix: give the app namespace its own INFO handler on stdout (which
      start.sh redirects into app.log). uvicorn's access-log noise stays off,
      because only this namespace is raised.
    """
    # start-server.js pipes our stdout, and a piped stdout is BLOCK-buffered:
    # log lines would sit in an 8KB buffer instead of reaching app.log. (Which
    # is why even this server's startup prints never appeared there.) Line
    # buffering fixes the prints too; stderr is the fallback because it stays
    # line-buffered when piped no matter what.
    stream = sys.stdout
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        stream = sys.stderr

    app_logger = logging.getLogger("web_canvas")
    app_logger.setLevel(logging.INFO)
    if not app_logger.handlers:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
        app_logger.addHandler(handler)
    # Already handled here; do not also bubble up to whatever uvicorn installs.
    app_logger.propagate = False


# The built frontend, last: its catch-all route must not shadow any API route.
if server_config.args.serve_dist is not None:
    server_static.mount_frontend(app, server_config.args.serve_dist or server_static.DEFAULT_DIST_DIR)


def main() -> None:
    """Run the server with the parsed arguments (server_config.args); the Windows launcher calls this too."""
    import uvicorn
    _configure_app_logging()
    scheme = "https" if server_config.args.ssl_certfile else "http"
    print(f"[Storage Server] Listening on {scheme}://{server_config.args.host}:{server_config.args.port}"
          + (" (serving the app)" if server_config.args.serve_dist is not None else ""))
    print(f"[Storage Server] Storage directory: {server_config.STORAGE_DIR}")
    print(f"[Storage Server] Database: {server_db.DB_PATH}")
    uvicorn.run(app, host=server_config.args.host, port=server_config.args.port, log_level="warning",
                ssl_certfile=server_config.args.ssl_certfile, ssl_keyfile=server_config.args.ssl_keyfile)


if __name__ == "__main__":
    main()

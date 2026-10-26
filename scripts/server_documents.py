"""Document-store operations shared by the routes and the run engine
(backend_authority.md §2.1): revision-checked writes, creation, deletion and
version snapshots, each publishing its book event.

The PUT /documents/{id} route in api_server predates this module and keeps
its own copy of the revision check; the two must agree (see the test that
pins both on a stale base).
"""
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import server_events
from server_content import delete_document_content, load_document_content, save_document_content, save_version_content
from server_db import get_db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def list_documents(username: str, book_id: str) -> List[Dict[str, Any]]:
    """Chapter metadata in book order (no content)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, title, sort_order, revision, summary, summary_content_hash, created_at, updated_at, pinned "
            "FROM documents WHERE username = ? AND book_id = ? ORDER BY sort_order",
            (username, book_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def write_document(username: str, book_id: str, doc_id: str, *, content: Optional[str] = None, title: Optional[str] = None,
                   base_revision: Optional[int] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
    """Write a chapter's text and/or title. Returns {"ok": True, "revision"} or
    {"ok": False, "conflict": {...current chapter...}} when `base_revision` is stale."""
    now = now_iso()
    conn = get_db()
    try:
        updates, params = [], []
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        updates.append("updated_at = ?")
        params.append(now)
        updates.append("revision = revision + 1")
        where = "username = ? AND book_id = ? AND id = ?"
        where_params: List[Any] = [username, book_id, doc_id]
        if base_revision is not None:
            where += " AND revision = ?"
            where_params.append(base_revision)
        cursor = conn.execute(f"UPDATE documents SET {', '.join(updates)} WHERE {where}", params + where_params)
        if cursor.rowcount == 0:
            current = conn.execute("SELECT title, revision, updated_at FROM documents WHERE username = ? AND book_id = ? AND id = ?",
                                   (username, book_id, doc_id)).fetchone()
            if current is None:
                return {"ok": False, "missing": True}
            return {"ok": False, "conflict": {"revision": current["revision"], "title": current["title"],
                                              "content": load_document_content(username, book_id, doc_id), "updatedAt": current["updated_at"]}}
        conn.execute("UPDATE books SET updated_at = ? WHERE username = ? AND id = ?", (now, username, book_id))
        revision = conn.execute("SELECT revision FROM documents WHERE username = ? AND book_id = ? AND id = ?",
                                (username, book_id, doc_id)).fetchone()["revision"]
        if content is not None:
            save_document_content(username, book_id, doc_id, content)
        conn.commit()
    finally:
        conn.close()
    server_events.hub.publish(username, book_id, {"type": "document", "kind": "updated", "documentId": doc_id,
                                                  "revision": revision, "clientId": client_id})
    return {"ok": True, "revision": revision, "updatedAt": now}


def create_document(username: str, book_id: str, title: str, content: str, client_id: Optional[str] = None,
                    doc_id: Optional[str] = None) -> Dict[str, Any]:
    """Append a chapter at the end of the book."""
    now = now_iso()
    doc_id = doc_id or f"doc-{int(datetime.now().timestamp() * 1000)}-{secrets.token_hex(2)}"
    conn = get_db()
    try:
        max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) as m FROM documents WHERE username = ? AND book_id = ?",
                                 (username, book_id)).fetchone()["m"]
        conn.execute("INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (doc_id, username, book_id, title, max_order + 1, now, now))
        conn.execute("UPDATE books SET updated_at = ? WHERE username = ? AND id = ?", (now, username, book_id))
        save_document_content(username, book_id, doc_id, content)
        conn.commit()
    finally:
        conn.close()
    server_events.hub.publish(username, book_id, {"type": "documents", "kind": "created", "documentIds": [doc_id], "clientId": client_id})
    return {"id": doc_id, "title": title, "revision": 1, "createdAt": now, "updatedAt": now}


def reorder_documents(username: str, book_id: str, doc_ids: List[str], client_id: Optional[str] = None) -> str:
    """Put the book's chapters in this order (the reorder endpoint and a run's move_chapter / new chapter placement)."""
    now = now_iso()
    conn = get_db()
    try:
        for idx, doc_id in enumerate(doc_ids):
            conn.execute("UPDATE documents SET sort_order = ? WHERE username = ? AND book_id = ? AND id = ?", (idx, username, book_id, doc_id))
        conn.execute("UPDATE books SET updated_at = ? WHERE username = ? AND id = ?", (now, username, book_id))
        conn.commit()
    finally:
        conn.close()
    server_events.hub.publish(username, book_id, {"type": "documents", "kind": "reordered", "clientId": client_id})
    return now


def delete_document(username: str, book_id: str, doc_id: str, client_id: Optional[str] = None) -> None:
    now = now_iso()
    conn = get_db()
    try:
        conn.execute("DELETE FROM documents WHERE username = ? AND book_id = ? AND id = ?", (username, book_id, doc_id))
        conn.execute("UPDATE books SET updated_at = ? WHERE username = ? AND id = ?", (now, username, book_id))
        conn.commit()
    finally:
        conn.close()
    delete_document_content(username, book_id, doc_id)
    server_events.hub.publish(username, book_id, {"type": "document", "kind": "deleted", "documentId": doc_id, "clientId": client_id})


def snapshot_document(username: str, book_id: str, doc_id: str, label: str, content: str) -> Dict[str, Any]:
    """A version of the chapter as it is now, before the run changes it."""
    now = now_iso()
    ver_id = f"ver-{int(datetime.now().timestamp() * 1000)}-{secrets.token_hex(2)}"
    conn = get_db()
    try:
        conn.execute("INSERT INTO versions (id, username, book_id, document_id, title, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                     (ver_id, username, book_id, doc_id, label, now))
        conn.commit()
    finally:
        conn.close()
    save_version_content(username, book_id, ver_id, content)
    return {"id": ver_id, "documentId": doc_id, "title": label, "timestamp": now}

"""A book's reference files — a long reference .txt — stored beside its
chapters and read by the agent (docs/features/attachments_and_web.md §1).

Endpoints under /api/books/{book_id}/attachments; the loop reaches the same
data through `list_attachments` / `attachment_paragraphs` (server runs) or the
endpoints (tabs).
"""
import json
import os
import secrets
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from server_auth import get_authenticated_username
from server_config import sanitize_id
from server_content import _get_content_dir
from server_db import get_db
from wc_text.attachments import attachment_paragraphs as split_paragraphs
from wc_text.attachments import attachment_sections, normalize_attachment_text

router = APIRouter()

MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
ALLOWED_EXTENSIONS = (".txt", ".md", ".markdown", ".text")
# Paragraph lists of recently read files, by path and modification time: a run
# reads the same file many times, and splitting 1 M characters is not free.
_PARAGRAPH_CACHE: "OrderedDict[str, List[str]]" = OrderedDict()
_CACHE_ENTRIES = 8
_cache_lock = threading.Lock()


def ensure_tables() -> None:
    conn = get_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS attachments (
            id TEXT NOT NULL, username TEXT NOT NULL, book_id TEXT NOT NULL, name TEXT NOT NULL,
            chars INTEGER NOT NULL, paragraphs INTEGER NOT NULL, sections TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            PRIMARY KEY (username, book_id, id))""")
        conn.commit()
    finally:
        conn.close()


def decode_text(raw: bytes) -> str:
    """The text of an uploaded file, decoded the way Chinese .txt files are actually encoded.

    A BOM decides; then UTF-8; then GB18030 (a superset of GBK and GB2312,
    the usual encoding of a downloaded Chinese .txt); then Big5; then UTF-8
    with replacement characters rather than a refusal.
    """
    for bom, codec in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            return raw.decode(codec, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # GB18030 decodes almost any byte sequence, so it cannot fail its way to
    # Big5: Big5 text read as GB18030 comes out full of private-use code
    # points. Decode both and keep the one with fewer.
    candidates = []
    for codec in ("gb18030", "big5"):
        try:
            text = raw.decode(codec)
        except UnicodeDecodeError:
            continue
        candidates.append((sum(1 for c in text if "\ue000" <= c <= "\uf8ff"), len(candidates), text))
    if candidates:
        return min(candidates)[2]
    return raw.decode("utf-8", errors="replace")


def _path(username: str, book_id: str, attachment_id: str) -> str:
    folder = os.path.join(_get_content_dir(username, book_id), "attachments")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{sanitize_id(attachment_id, 'attachmentId')}.txt")


def _images_path(username: str, book_id: str, attachment_id: str) -> str:
    """The pictures a saved page brought (web_save): beside the text, numbered as its "[image N]" markers."""
    return _path(username, book_id, attachment_id)[:-len(".txt")] + ".images.json"


def attachment_images(username: str, book_id: str, attachment_id: str) -> Dict[int, Dict[str, Any]]:
    """{n: {n, alt, dataUrl, src}} — empty for an uploaded file, which has none."""
    path = _images_path(username, book_id, attachment_id)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return {int(i["n"]): i for i in json.load(f)}


def _book_exists(username: str, book_id: str) -> bool:
    conn = get_db()
    try:
        return conn.execute("SELECT 1 FROM books WHERE username = ? AND id = ?", (username, book_id)).fetchone() is not None
    finally:
        conn.close()


def list_attachments(username: str, book_id: str) -> List[Dict[str, Any]]:
    """The book's attachments in order, each with its reference (A1, A2…)."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM attachments WHERE username = ? AND book_id = ? ORDER BY sort_order, created_at",
                            (username, book_id)).fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "ref": f"A{i + 1}", "name": r["name"], "chars": r["chars"], "paragraphs": r["paragraphs"],
             "sections": json.loads(r["sections"] or "[]"), "createdAt": r["created_at"]} for i, r in enumerate(rows)]


def attachment_text(username: str, book_id: str, attachment_id: str) -> Optional[str]:
    path = _path(username, book_id, attachment_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def attachment_paragraphs(username: str, book_id: str, attachment_id: str) -> List[str]:
    path = _path(username, book_id, attachment_id)
    if not os.path.exists(path):
        return []
    key = f"{path}|{os.path.getmtime(path)}"
    with _cache_lock:
        if key in _PARAGRAPH_CACHE:
            _PARAGRAPH_CACHE.move_to_end(key)
            return _PARAGRAPH_CACHE[key]
    with open(path, "r", encoding="utf-8") as f:
        paragraphs = split_paragraphs(f.read())
    with _cache_lock:
        _PARAGRAPH_CACHE[key] = paragraphs
        while len(_PARAGRAPH_CACHE) > _CACHE_ENTRIES:
            _PARAGRAPH_CACHE.popitem(last=False)
    return paragraphs


def store_attachment(username: str, book_id: str, name: str, raw: bytes, images: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    text = normalize_attachment_text(decode_text(raw))
    paragraphs = split_paragraphs(text)
    sections = attachment_sections(paragraphs)
    attachment_id = f"att-{int(datetime.now().timestamp() * 1000)}-{secrets.token_hex(3)}"
    with open(_path(username, book_id, attachment_id), "w", encoding="utf-8") as f:
        f.write(text)
    if images:
        with open(_images_path(username, book_id, attachment_id), "w", encoding="utf-8") as f:
            json.dump([{"n": i["n"], "alt": i.get("alt") or "", "dataUrl": i["dataUrl"], "src": i.get("src") or ""} for i in images], f, ensure_ascii=False)
    conn = get_db()
    try:
        order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM attachments WHERE username = ? AND book_id = ?",
                             (username, book_id)).fetchone()["m"] + 1
        conn.execute("""INSERT INTO attachments (id, username, book_id, name, chars, paragraphs, sections, sort_order, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (attachment_id, username, book_id, name, sum(len(p) for p in paragraphs), len(paragraphs),
                      json.dumps(sections, ensure_ascii=False), order, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()
    return next(a for a in list_attachments(username, book_id) if a["id"] == attachment_id)


def delete_attachment(username: str, book_id: str, attachment_id: str) -> bool:
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM attachments WHERE username = ? AND book_id = ? AND id = ?", (username, book_id, attachment_id))
        conn.commit()
        removed = cur.rowcount > 0
    finally:
        conn.close()
    for path in (_path(username, book_id, attachment_id), _images_path(username, book_id, attachment_id)):
        if os.path.exists(path):
            os.remove(path)
    return removed


def delete_book_attachments(username: str, book_id: str) -> None:
    """With the book: the rows (the files go with the book's content directory)."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM attachments WHERE username = ? AND book_id = ?", (username, book_id))
        conn.commit()
    finally:
        conn.close()


def _clean_name(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    return base[:120] or "attachment.txt"


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/api/books/{book_id}/attachments")
async def list_endpoint(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    return {"attachments": list_attachments(username, safe_book_id)}


@router.post("/api/books/{book_id}/attachments")
async def upload_endpoint(request: Request, book_id: str, name: str = ""):
    """The file's raw bytes as the body; its name in `?name=`. Plain text or Markdown."""
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    if not _book_exists(username, safe_book_id):
        raise HTTPException(status_code=404, detail="Book not found.")
    clean = _clean_name(name)
    if not clean.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Only .txt and .md files can be attached.")
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail=f"The file is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.")
    raw = await request.body()
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail=f"The file is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="The file is empty.")
    return {"attachment": store_attachment(username, safe_book_id, clean, raw)}


@router.get("/api/books/{book_id}/attachments/{attachment_id}/text")
async def text_endpoint(request: Request, book_id: str, attachment_id: str):
    username = get_authenticated_username(request)
    text = attachment_text(username, sanitize_id(book_id, "bookId"), sanitize_id(attachment_id, "attachmentId"))
    if text is None:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return {"id": attachment_id, "text": text}


@router.delete("/api/books/{book_id}/attachments/{attachment_id}")
async def delete_endpoint(request: Request, book_id: str, attachment_id: str):
    username = get_authenticated_username(request)
    if not delete_attachment(username, sanitize_id(book_id, "bookId"), sanitize_id(attachment_id, "attachmentId")):
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return {"success": True}

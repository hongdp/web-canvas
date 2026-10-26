"""Content-file helpers: save/load/delete document and version content.

Document/version bodies live as JSON files under CONTENT_DIR (metadata is
in SQLite, see server_db). All paths are sanitized and confined to
CONTENT_DIR to prevent directory traversal.
"""

import os
import json
import shutil
from fastapi import HTTPException

from server_config import CONTENT_DIR, sanitize_username, sanitize_id

# ── Content file helpers ───────────────────────────────────────────────────────
def _get_content_dir(username: str, book_id: str) -> str:
    """Get the content directory for a book, creating it if needed."""
    safe_username = sanitize_username(username)
    safe_book_id = sanitize_id(book_id, "bookId")
    path = os.path.join(CONTENT_DIR, safe_username, safe_book_id)
    resolved = os.path.abspath(path)
    # Security: ensure within CONTENT_DIR
    if not resolved.startswith(os.path.abspath(CONTENT_DIR) + os.sep):
        raise HTTPException(status_code=403, detail="Access denied: Directory traversal detected.")
    os.makedirs(resolved, exist_ok=True)
    return resolved

def save_document_content(username: str, book_id: str, doc_id: str, content: str):
    """Save document content to a file, atomically.

    Problem: the file was rewritten in place (open "w" + dump), so a crash or
    a full disk mid-write left a truncated chapter — and readers could catch
    one half-written.
    Fix: write a temporary file beside it and rename it over the old one;
    os.replace is atomic on the same filesystem, so a reader sees the old
    chapter or the new one, never a mix.
    """
    content_dir = _get_content_dir(username, book_id)
    safe_doc_id = sanitize_id(doc_id, "docId")
    file_path = os.path.join(content_dir, f"doc-{safe_doc_id}.json")
    tmp_path = f"{file_path}.tmp-{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"content": content}, f, ensure_ascii=False)
        os.replace(tmp_path, file_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def load_document_content(username: str, book_id: str, doc_id: str) -> str:
    """Load document content from file."""
    content_dir = _get_content_dir(username, book_id)
    safe_doc_id = sanitize_id(doc_id, "docId")
    file_path = os.path.join(content_dir, f"doc-{safe_doc_id}.json")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("content", "")
        except Exception:
            return ""
    return ""

def delete_document_content(username: str, book_id: str, doc_id: str):
    """Delete a document content file."""
    content_dir = _get_content_dir(username, book_id)
    safe_doc_id = sanitize_id(doc_id, "docId")
    file_path = os.path.join(content_dir, f"doc-{safe_doc_id}.json")
    if os.path.exists(file_path):
        os.remove(file_path)

def save_version_content(username: str, book_id: str, version_id: str, content: str):
    """Save version snapshot content to a file."""
    content_dir = _get_content_dir(username, book_id)
    safe_ver_id = sanitize_id(version_id, "versionId")
    file_path = os.path.join(content_dir, f"ver-{safe_ver_id}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump({"content": content}, f, ensure_ascii=False)

def load_version_content(username: str, book_id: str, version_id: str) -> str:
    """Load version snapshot content from file."""
    content_dir = _get_content_dir(username, book_id)
    safe_ver_id = sanitize_id(version_id, "versionId")
    file_path = os.path.join(content_dir, f"ver-{safe_ver_id}.json")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("content", "")
        except Exception:
            return ""
    return ""

def delete_book_content_dir(username: str, book_id: str):
    """Delete the entire content directory for a book."""
    content_dir = _get_content_dir(username, book_id)
    if os.path.exists(content_dir):
        shutil.rmtree(content_dir, ignore_errors=True)

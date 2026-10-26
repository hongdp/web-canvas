"""Legacy migration: one-time import of state_*.json files into SQLite + content files."""

import os
import re
import json
import sqlite3
from datetime import datetime, timezone

from server_config import STORAGE_DIR, USERS_FILE, load_json_file
from server_db import get_db
from server_content import save_document_content, save_version_content

# ── Legacy migration ──────────────────────────────────────────────────────────
def migrate_legacy_files():
    """Migrate legacy state_*.json files to SQLite + content files.

    This runs once at startup. Legacy files are preserved as backups
    (renamed to state_*.json.migrated).
    """
    legacy_pattern = re.compile(r"^state_([a-zA-Z0-9_-]+?)(?:_([a-zA-Z0-9_-]+))?\.json$")

    try:
        files = os.listdir(STORAGE_DIR)
    except Exception:
        return

    legacy_files = []
    for f in files:
        m = legacy_pattern.match(f)
        if m:
            legacy_files.append((f, m.group(1), m.group(2) or "default"))

    # Also handle bare "state.json" (pre-multi-user legacy format)
    bare_state = os.path.join(STORAGE_DIR, "state.json")
    if os.path.exists(bare_state):
        # Assign to the first registered user, or 'default_user'
        users = load_json_file(USERS_FILE)
        fallback_username = next(iter(users.keys()), None) if users else None
        if not fallback_username:
            fallback_username = "default_user"
        legacy_files.append(("state.json", fallback_username, "default"))

    if not legacy_files:
        return

    print(f"[Migration] Found {len(legacy_files)} legacy file(s) to migrate...")
    conn = get_db()
    migrated = 0
    skipped = 0
    failed = 0
    try:
        for filename, username, book_id in legacy_files:
            file_path = os.path.join(STORAGE_DIR, filename)
            if not os.path.exists(file_path):
                skipped += 1
                continue
            try:
                _migrate_single_legacy_file(conn, file_path, username, book_id)
                conn.commit()
                # Rename to .migrated as backup
                backup_path = file_path + ".migrated"
                try:
                    os.rename(file_path, backup_path)
                except OSError:
                    pass  # File rename may fail on some FS but migration is done
                migrated += 1
                print(f"[Migration] ✓ ({migrated}/{len(legacy_files)}) Migrated {filename}")
            except sqlite3.IntegrityError:
                # Already migrated in a previous run
                skipped += 1
                try:
                    os.rename(file_path, file_path + ".migrated")
                except OSError:
                    pass
            except Exception as e:
                failed += 1
                print(f"[Migration] ✗ Failed to migrate {filename}: {e}")
                conn.rollback()
    finally:
        conn.close()
    print(f"[Migration] Complete: {migrated} migrated, {skipped} skipped, {failed} failed")

def _migrate_single_legacy_file(conn: sqlite3.Connection, file_path: str, username: str, book_id: str):
    """Migrate a single legacy JSON file into the new storage schema."""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    book_title = data.get("bookTitle", "Untitled Book")
    updated_at = data.get("updatedAt", now)
    active_doc_id = data.get("activeDocumentId", "")

    # Check if book already exists in DB (skip if already migrated)
    existing = conn.execute(
        "SELECT id FROM books WHERE username = ? AND id = ?",
        (username, book_id)
    ).fetchone()
    if existing:
        return

    # Insert book
    conn.execute(
        "INSERT INTO books (id, username, title, active_document_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (book_id, username, book_title, active_doc_id, updated_at, updated_at)
    )

    # Insert documents and save their content to files
    documents = data.get("documents", [])
    for idx, doc in enumerate(documents):
        doc_id = doc.get("id", f"doc-migrated-{idx}")
        doc_title = doc.get("title", f"Chapter {idx + 1}")
        doc_content = doc.get("content", "")
        doc_created = doc.get("createdAt", now)
        doc_updated = doc.get("updatedAt", now)

        conn.execute(
            "INSERT INTO documents (id, username, book_id, title, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, username, book_id, doc_title, idx, doc_created, doc_updated)
        )

        # Save content to file
        save_document_content(username, book_id, doc_id, doc_content)

    # Insert versions and save their content to files
    versions = data.get("versions", [])
    for ver in versions:
        ver_id = ver.get("id", f"ver-migrated-{ver.get('timestamp', now)}")
        ver_doc_id = ver.get("documentId", "")
        ver_title = ver.get("title", "Untitled Version")
        ver_timestamp = ver.get("timestamp", now)
        ver_content = ver.get("content", "")

        conn.execute(
            "INSERT INTO versions (id, username, book_id, document_id, title, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (ver_id, username, book_id, ver_doc_id, ver_title, ver_timestamp)
        )
        save_version_content(username, book_id, ver_id, ver_content)

    # Settings are the user's: the first migrated book's become theirs, later ones never overwrite them.
    conn.execute(
        """INSERT OR IGNORE INTO user_settings (username, active_provider, provider_configs,
           custom_system_prompts, active_system_prompt_id, theme, debug_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            username,
            data.get("activeProvider"),
            json.dumps(data.get("providerConfigs")) if data.get("providerConfigs") else None,
            json.dumps(data.get("customSystemPrompts")) if data.get("customSystemPrompts") else None,
            data.get("activeSystemPromptId"),
            data.get("theme"),
            1 if data.get("debugMode") else 0
        )
    )

    # Insert chat messages
    messages = data.get("messages", [])
    for idx, msg in enumerate(messages):
        msg_id = msg.get("id", f"msg-migrated-{idx}")
        conn.execute(
            """INSERT INTO messages (id, username, book_id, role, content, timestamp,
               thinking, model, input_tokens, output_tokens, cache_hit_tokens, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                idx
            )
        )

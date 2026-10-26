"""SQLite database layer: DB path/config, connections, schema init/migrations.

Tests that need an isolated database should patch `server_db.DB_PATH`
(patching a re-exported alias on another module does not affect the code
here, which reads this module's global).
"""

import json
import os
import sqlite3
from typing import Optional

# SQLite DB is stored locally (not on network mounts like SMB/NFS)
# because SQLite requires proper file locking which network filesystems don't support.
# The DB file is placed in a local directory that mirrors the storage dir name.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
# WEB_CANVAS_DB_DIR moves the metadata DB, its key and the step journal out of the program's folder:
# an installed app (the Windows installer) keeps them in the user's profile, where it may write.
_LOCAL_DB_DIR = os.environ.get("WEB_CANVAS_DB_DIR") or os.path.join(_PROJECT_DIR, ".local_db")
if not os.path.exists(_LOCAL_DB_DIR):
    os.makedirs(_LOCAL_DB_DIR, exist_ok=True)
DB_PATH = os.path.join(_LOCAL_DB_DIR, "metadata.db")

# ── SQLite Database Layer ──────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    """Get a SQLite connection with foreign keys enabled.

    Uses DELETE journal mode instead of WAL for compatibility with
    network filesystems (SMB/NFS) where WAL's shared-memory mmap fails.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """Create database tables if they don't exist."""
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS books (
                id TEXT NOT NULL,
                username TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'Untitled Book',
                active_document_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (username, id)
            );
            CREATE INDEX IF NOT EXISTS idx_books_username ON books(username);

            CREATE TABLE IF NOT EXISTS documents (
                id TEXT NOT NULL,
                username TEXT NOT NULL,
                book_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'Untitled Chapter',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (username, book_id, id),
                FOREIGN KEY (username, book_id) REFERENCES books(username, id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS versions (
                id TEXT NOT NULL,
                username TEXT NOT NULL,
                book_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                title TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                PRIMARY KEY (username, book_id, id)
            );

            -- A user's settings (API keys, provider configs, system prompts, theme):
            -- one row per user, shared by every book.
            CREATE TABLE IF NOT EXISTS user_settings (
                username TEXT PRIMARY KEY,
                active_provider TEXT,
                provider_configs TEXT,
                custom_system_prompts TEXT,
                active_system_prompt_id TEXT,
                theme TEXT,
                debug_mode INTEGER DEFAULT 0
            );

            -- What a book uses, by reference to the user's settings: the provider,
            -- its model and reasoning effort, the system prompt preset's id. Keys
            -- and preset texts stay in user_settings.
            CREATE TABLE IF NOT EXISTS book_preferences (
                username TEXT NOT NULL,
                book_id TEXT NOT NULL,
                active_provider TEXT,
                model TEXT,
                reasoning_effort TEXT,
                active_system_prompt_id TEXT,
                PRIMARY KEY (username, book_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT NOT NULL,
                username TEXT NOT NULL,
                book_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                thinking TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_hit_tokens INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0,
                agent TEXT,
                PRIMARY KEY (username, book_id, id)
            );

            -- The book to reopen on the next device (see record_last_active_book).
            CREATE TABLE IF NOT EXISTS user_state (
                username TEXT PRIMARY KEY,
                last_active_book_id TEXT,
                updated_at TEXT NOT NULL
            );
        """)

        migrate_user_settings(conn)

        # Schema migration: chapter summary metadata (smart context selection).
        # SQLite has no IF NOT EXISTS for columns, so probe table_info first.
        doc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        if "summary" not in doc_columns:
            conn.execute("ALTER TABLE documents ADD COLUMN summary TEXT")
            conn.execute("ALTER TABLE documents ADD COLUMN summary_content_hash TEXT")
            print("[Init] Migrated documents table: added summary columns.")

        # Schema migration: a chapter's revision (backend_authority.md §2.1).
        # Every write of its text or title bumps it; a write that names the
        # revision it was based on is refused when another landed first, so a
        # stale tab can no longer silently overwrite a newer chapter.
        doc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        if "revision" not in doc_columns:
            conn.execute("ALTER TABLE documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            print("[Init] Migrated documents table: added revision column.")

        # Schema migration: a chapter the writer pinned rides along on every
        # agent turn (docs/features/pinned_context.md). Metadata: setting it
        # does not bump the revision.
        doc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        if "pinned" not in doc_columns:
            conn.execute("ALTER TABLE documents ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            print("[Init] Migrated documents table: added pinned column.")

        # Schema migration: an agentic turn's record (timeline, chapters it
        # changed — src/types/chat.ts AgentTurnRecord), stored as JSON. Without
        # it the bubble lost its tool timeline and "changed this turn" block on
        # every reload, because only these fixed columns were ever saved.
        message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "agent" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN agent TEXT")
            print("[Init] Migrated messages table: added agent column.")
        # grok's encrypted reasoning of a turn's final step, replayed by later
        # turns (src/types/chat.ts ChatMessage.reasoningItems), as JSON.
        if "reasoning_items" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN reasoning_items TEXT")
            print("[Init] Migrated messages table: added reasoning_items column.")

        conn.commit()
    finally:
        conn.close()


# ── Per-user state: the book to reopen on the next device ─────────────────────
# Problem: a new device (or a browser whose cache was cleared, or an account
#   switch, which clears the same keys) always opened the book with id
#   'default'. The client chose the book from localStorage alone, and the
#   server kept no per-user pointer to the book last worked in — only a
#   per-book active_document_id.
# Fix: every write to a book (metadata PUT, create, legacy save) records it as
#   the user's last active book, and /api/auth/session hands the pointer back
#   so the client can open that book before it has any local state.

def record_last_active_book(conn: sqlite3.Connection, username: str, book_id: str, now: str) -> None:
    """Upsert the pointer inside the caller's transaction."""
    conn.execute(
        """INSERT INTO user_state (username, last_active_book_id, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(username) DO UPDATE SET
           last_active_book_id = excluded.last_active_book_id, updated_at = excluded.updated_at""",
        (username, book_id, now),
    )


def clear_last_active_book(conn: sqlite3.Connection, username: str, book_id: str) -> None:
    """Forget the pointer when the book it names is deleted."""
    conn.execute(
        "UPDATE user_state SET last_active_book_id = NULL WHERE username = ? AND last_active_book_id = ?",
        (username, book_id),
    )


def lookup_last_active_book_id(username: str) -> Optional[str]:
    """The recorded book if it still exists; otherwise the most recently
    updated book, so accounts that predate the pointer (and pointers left
    behind by a deletion) still get a sensible answer; None for an account
    with no books. Never raises — a session check must not fail over this."""
    try:
        conn = get_db()
        try:
            row = conn.execute(
                """SELECT b.id FROM user_state s
                   JOIN books b ON b.username = s.username AND b.id = s.last_active_book_id
                   WHERE s.username = ?""",
                (username,),
            ).fetchone()
            if row:
                return row["id"]
            row = conn.execute(
                "SELECT id FROM books WHERE username = ? ORDER BY updated_at DESC LIMIT 1",
                (username,),
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()
    except Exception as e:
        print(f"[user_state] last-active-book lookup failed for {username!r}: {e}")
        return None


_USER_SETTINGS_COLUMNS = ("active_provider", "provider_configs", "custom_system_prompts",
                          "active_system_prompt_id", "theme", "debug_mode")


def migrate_user_settings(conn) -> None:
    """Fold the old per-book `book_settings` table into `user_settings`, then drop it.

    Settings have been user-level since 2026-06-29, kept in the row with book_id
    "__global__"; the other rows were copies from the JSON-to-SQLite migration
    that nothing read. A user who never got a "__global__" row keeps the
    "default" book's settings, as the old migration would have copied them.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'book_settings'").fetchone():
        return
    cols = ", ".join(_USER_SETTINGS_COLUMNS)
    for book_id in ("__global__", "default"):
        conn.execute(f"INSERT OR IGNORE INTO user_settings (username, {cols}) "
                     f"SELECT username, {cols} FROM book_settings WHERE book_id = ?", (book_id,))
    dropped = conn.execute("SELECT COUNT(*) FROM book_settings").fetchone()[0]
    conn.execute("DROP TABLE book_settings")
    print(f"[Init] Moved settings to user_settings; dropped book_settings ({dropped} rows).")


def upsert_user_settings(conn, username: str, body: dict) -> None:
    """Save the settings fields a book save carries (absent or null ones keep their stored value)."""
    values = {
        "active_provider": body.get("activeProvider"),
        "provider_configs": json.dumps(body["providerConfigs"]) if body.get("providerConfigs") is not None else None,
        "custom_system_prompts": json.dumps(body["customSystemPrompts"]) if body.get("customSystemPrompts") is not None else None,
        "active_system_prompt_id": body.get("activeSystemPromptId"),
        "theme": body.get("theme"),
        "debug_mode": (1 if body["debugMode"] else 0) if body.get("debugMode") is not None else None,
    }
    if all(v is None for v in values.values()):
        return
    cols = ", ".join(_USER_SETTINGS_COLUMNS)
    updates = ", ".join(f"{c} = COALESCE(excluded.{c}, user_settings.{c})" for c in _USER_SETTINGS_COLUMNS)
    conn.execute(f"INSERT INTO user_settings (username, {cols}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                 f"ON CONFLICT(username) DO UPDATE SET {updates}",
                 (username, *(values[c] for c in _USER_SETTINGS_COLUMNS)))


def upsert_book_preferences(conn, username: str, book_id: str, body: dict) -> None:
    """Remember what this book uses (a book save carries the settings on screen): the active
    provider, that provider's model and reasoning effort, the preset id. Absent fields keep theirs."""
    provider = body.get("activeProvider")
    configs = body.get("providerConfigs") if isinstance(body.get("providerConfigs"), dict) else {}
    config = configs.get(provider) if provider and isinstance(configs.get(provider), dict) else {}
    values = {
        "active_provider": provider,
        "model": config.get("model") or None,
        "reasoning_effort": config.get("reasoningEffort") or None,
        "active_system_prompt_id": body.get("activeSystemPromptId"),
    }
    if all(v is None for v in values.values()):
        return
    cols = ", ".join(values)
    updates = ", ".join(f"{c} = COALESCE(excluded.{c}, book_preferences.{c})" for c in values)
    conn.execute(f"INSERT INTO book_preferences (username, book_id, {cols}) VALUES (?, ?, ?, ?, ?, ?) "
                 f"ON CONFLICT(username, book_id) DO UPDATE SET {updates}",
                 (username, book_id, *values.values()))


def apply_book_preferences(conn, username: str, book_id: str, result: dict) -> None:
    """Lay a book's own choices over the user's settings in a book response. A preset id the user
    has since deleted is ignored (the user's current one stands); keys and preset texts are never per book."""
    row = conn.execute("SELECT * FROM book_preferences WHERE username = ? AND book_id = ?", (username, book_id)).fetchone()
    if not row:
        return
    provider = row["active_provider"]
    if provider:
        result["activeProvider"] = provider
        configs = result.get("providerConfigs")
        if isinstance(configs, dict):
            config = dict(configs.get(provider) or {})
            if row["model"]:
                config["model"] = row["model"]
            if row["reasoning_effort"]:
                config["reasoningEffort"] = row["reasoning_effort"]
            configs[provider] = config
    preset = row["active_system_prompt_id"]
    presets = result.get("customSystemPrompts")
    known = {p.get("id") for p in presets if isinstance(p, dict)} if isinstance(presets, list) else set()
    if preset and (preset in known or preset == "prompt-none"):
        result["activeSystemPromptId"] = preset

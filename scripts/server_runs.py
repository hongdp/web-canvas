"""The server-side run engine (backend_authority.md §2.3, §4.3).

A run is one chat turn executed on the server: the request is assembled
here (server_context), each step is a generation job (server_generation),
the tools write the document store (server_documents), and everything the
browser needs to render it arrives as `run.*` events on the book's event
stream (server_events). A run survives the tab: its state is persisted
after every step, so an API restart resumes it at its last completed step.

One run at a time per book; further requests queue. A run that does no
useful work is paused, never killed (the "Runaway runs" decision).

Exposes `engine` (the per-process RunEngine) and `router` (/api/books/{id}/runs*).
"""
import asyncio
import json
import re
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

import server_attachments
import server_context
import server_documents
import server_events
import server_generation
import server_secrets
import server_web
from server_auth import get_authenticated_username
from server_config import sanitize_id
from server_content import load_document_content
from server_db import get_db
from wc_agent.polish import analyze_in_batches, look_in_batches, plan_analysis, polish_html, polish_model_for, polish_offered
from wc_agent.registry import ToolRegistry, to_tool_specs
from wc_agent.run import AgentRun, StepOutput, StepUnavailable, offered_tools_for
from wc_agent.tools.ask_user import ask_user_tool
from wc_agent.tools.book_reads import BOOK_TOOLS
from wc_agent.tools.document_writes import (DOCUMENT_WRITE_TOOLS, commit_doc, doc_state, edit_paragraphs_tool, preview_rewrite, preview_selection_with_count,
                                            replace_text_tool, user_edited)
from wc_agent.tools.paragraph_moves import PARAGRAPH_MOVE_TOOLS
from wc_agent.tools.plan import plan_tool
from wc_agent.tools.polish_chapter import polish_chapter_tool
from wc_agent.tools.view_images import supports_vision, view_images_tool
from wc_agent.tools.web import WEB_TOOLS
from wc_agent.types import urls_in_text
from wc_agent.types import ToolContext, chapter_outline, create_run_state
from wc_text.reminders import REPEAT_PAUSE_STEPS, queued_request_reminder, structure_changed_reminder, user_edited_reminder, wrap_reminder
from wc_text.plan import previous_turn_plan
from wc_text.context_window import resolve_context_window_tokens
from wc_text.policy import resolve_run_settings
from wc_text.protocol_choice import resolve_document_protocol
from wc_text.provider_profile import get_cache_profile, target_prompt_tokens
from wc_text.document_tools import to_openai_tools
from wc_text.image_preservation import register_image, replace_images_with_placeholders, restore_image_placeholders
from wc_text.chapters import cite_chapter
from wc_text.paragraphs import chapter_chars
from wc_text.jsstr import js_trim
from wc_text.stream_handlers import ASSISTANT_PLACEHOLDER, build_completion_warnings, split_streaming_response
from wc_text.text import is_blank_content, strip_incomplete_end_tag, trim_incomplete_html_tail
from wc_text.tool_call_stream import finish_tool_calls

logger = logging.getLogger("web_canvas.runs")
router = APIRouter()

CHAT_TOOLS = ToolRegistry([*DOCUMENT_WRITE_TOOLS, edit_paragraphs_tool, replace_text_tool, *PARAGRAPH_MOVE_TOOLS, *BOOK_TOOLS, view_images_tool, polish_chapter_tool, *WEB_TOOLS, plan_tool, ask_user_tool])

# ── Limits (the "Runaway runs" decision) ─────────────────────────────────────
#: Steps that repeated the same calls and wrote nothing before the run is
#: paused — after the loop's own nudge at REPEAT_NUDGE_STEPS (wc_text.reminders).
REPEAT_STEPS = REPEAT_PAUSE_STEPS
#: Steps shown in a pause notice, for the user to judge.
PAUSE_DETAIL_STEPS = 3
# A step that broke after it started streaming, or looped, is redone this many
# times before the run pauses (agentic_chat_loop.md §0.10).
STEP_REDO_BUDGET = 1
REPETITION_RETRY_NOTE = ("Your previous attempt at this reply was stopped because it began repeating the same text over and over. "
                         "Write the reply again from the start without repeating yourself. If the user asked for something repetitive, "
                         "write only as much of it as they asked for. If you cannot go on, say so and ask the user.")
#: Steps a run may take with no tab attached to the book before it pauses on
#: its own; the user's own limit applies while someone is watching.
UNATTENDED_STEP_CEILING = 12
#: Finished runs stay listed this long, so a reloading tab can settle its bubble.
FINISHED_RUN_TTL_SECONDS = 10 * 60
PREVIEW_THROTTLE_S = 0.25
SELECTION_PREVIEW_THROTTLE_S = 0.06
SPLIT_THROTTLE_S = 0.1
ACTIVE_STATUSES = ("queued", "running", "paused")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RunStopped(Exception):
    """The step in flight was aborted by a stop request."""


class RunFailed(Exception):
    """The provider failed the step."""


# ── Persistence ──────────────────────────────────────────────────────────────

#: Statuses a run can still continue from; any other is final.
LIVE_STATUSES = ("queued", "running", "paused")


def stored_request(request: Dict[str, Any], status: str) -> Dict[str, Any]:
    """The request as it is written to disk: a live run's API key sealed, a finished run's dropped
    (backend_authority.md §4.3, "API keys at rest")."""
    config = dict(request.get("config") or {})
    key = config.pop("apiKey", None)
    sealed = config.pop("apiKeySealed", None)
    if status in LIVE_STATUSES:
        if key:
            config["apiKeySealed"] = server_secrets.seal(key)
        elif sealed:
            config["apiKeySealed"] = sealed
    return {**request, "config": config}


def restored_request(stored: Dict[str, Any]) -> Dict[str, Any]:
    """The request a run executes with: its sealed key opened. A key that cannot be opened stays
    missing, and the run's next call fails and pauses it rather than running without one."""
    config = dict(stored.get("config") or {})
    sealed = config.pop("apiKeySealed", None)
    if sealed and not config.get("apiKey"):
        key = server_secrets.unseal(sealed)
        if key:
            config["apiKey"] = key
    return {**stored, "config": config}


def _scrub_stored_keys() -> None:
    """Once, for rows written before keys were sealed: a finished run's key is dropped, a live
    run's sealed. The file is then vacuumed, or the old text would stay in its free pages."""
    conn = get_db()
    changed = 0
    try:
        rows = conn.execute("""SELECT id, status, request FROM runs WHERE request LIKE '%"apiKey"%'""").fetchall()
        for row in rows:
            try:
                request = json.loads(row["request"])
            except (TypeError, ValueError):
                continue
            if "apiKey" not in (request.get("config") or {}):
                continue
            conn.execute("UPDATE runs SET request = ? WHERE id = ?",
                         (json.dumps(stored_request(request, row["status"]), ensure_ascii=False), row["id"]))
            changed += 1
        conn.commit()
        if changed:
            conn.execute("VACUUM")
            logger.info("Removed or sealed the API keys stored with %d earlier runs", changed)
    finally:
        conn.close()


def ensure_tables() -> None:
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                book_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                request TEXT NOT NULL,
                snapshot TEXT,
                record TEXT,
                result TEXT,
                pause TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_book ON runs(username, book_id, status);
            CREATE TABLE IF NOT EXISTS run_books (
                username TEXT NOT NULL, book_id TEXT NOT NULL, queue_held INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (username, book_id)
            );
        """)
        conn.commit()
    finally:
        conn.close()
    _scrub_stored_keys()
    server_context.ensure_tables()
    server_attachments.ensure_tables()


class Run:
    """One run's persisted state plus what only lives while it executes."""

    def __init__(self, run_id: str, username: str, book_id: str, request: Dict[str, Any], status: str = "queued",
                 created_at: Optional[str] = None) -> None:
        self.id = run_id
        self.username = username
        self.book_id = book_id
        self.request = request
        self.status = status
        self.created_at = created_at or _now_iso()
        self.updated_at = self.created_at
        self.finished_at: Optional[str] = None
        self.snapshot: Optional[Dict[str, Any]] = None
        self.record: Dict[str, Any] = {"status": "running", "steps": 0, "trace": [], "touched": [], "timeline": []}
        self.result: Optional[Dict[str, Any]] = None
        self.pause: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        # Live only
        self.task: Optional[asyncio.Task] = None
        self.agent: Optional[AgentRun] = None
        self.ports: Optional["ServerPorts"] = None
        self.job: Optional[server_generation.GenerationJob] = None
        self.live_text = ""
        self.live_reasoning = ""
        self.reasoning_heads: List[str] = []
        self.unattended_steps = 0
        #: The next step goes without xAI's repetition check (a resume after `repeating_output`).
        self.skip_loop_check_once = False
        self.snapshots: List[Dict[str, Any]] = []
        self.last_response_items: List[Any] = []
        self.finished_monotonic: Optional[float] = None
        self.stop_requested = False
        #: Set by Stop: a polish (the button's run, or the polish_chapter tool) checks it between chunks.
        self.stop_event = asyncio.Event()
        #: Side calls in flight (polish chunks, analysis batches): Stop aborts them too.
        self.side_jobs: set = set()

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id, "bookId": self.book_id, "status": self.status, "createdAt": self.created_at, "updatedAt": self.updated_at,
            "finishedAt": self.finished_at, "userMessageId": self.request.get("userMessageId"),
            "assistantMessageId": self.request.get("assistantMessageId"), "prompt": self.request.get("prompt"),
            "activeDocumentId": self.request.get("activeDocumentId"), "selectedText": self.request.get("selectedText") or "",
            "clientId": self.request.get("clientId"), "record": self.record, "pause": self.pause, "error": self.error,
        }

    def detail(self) -> Dict[str, Any]:
        return {**self.summary(), "liveText": self.live_text, "liveReasoning": self.live_reasoning[-400:],
                "result": self.result, "snapshots": self.snapshots}

    def persist(self) -> None:
        self.updated_at = _now_iso()
        conn = get_db()
        try:
            conn.execute("""INSERT INTO runs (id, username, book_id, status, created_at, updated_at, finished_at, request, snapshot, record, result, pause, error)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at, finished_at = excluded.finished_at,
                            request = excluded.request, snapshot = excluded.snapshot, record = excluded.record, result = excluded.result, pause = excluded.pause, error = excluded.error""",
                         (self.id, self.username, self.book_id, self.status, self.created_at, self.updated_at, self.finished_at,
                          json.dumps(stored_request(self.request, self.status), ensure_ascii=False), json.dumps(self.snapshot, ensure_ascii=False) if self.snapshot else None,
                          json.dumps(self.record, ensure_ascii=False), json.dumps(self.result, ensure_ascii=False) if self.result else None,
                          json.dumps(self.pause, ensure_ascii=False) if self.pause else None, self.error))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def from_row(row) -> "Run":
        run = Run(row["id"], row["username"], row["book_id"], restored_request(json.loads(row["request"])), row["status"], row["created_at"])
        run.updated_at = row["updated_at"]
        run.finished_at = row["finished_at"]
        run.snapshot = json.loads(row["snapshot"]) if row["snapshot"] else None
        run.record = json.loads(row["record"]) if row["record"] else run.record
        run.result = json.loads(row["result"]) if row["result"] else None
        run.pause = json.loads(row["pause"]) if row["pause"] else None
        run.error = row["error"]
        return run


# ── Ports over the document store ────────────────────────────────────────────

class ServerPorts:
    """The run's ports (wc_agent.types.ToolContext) over SQLite + content files + the event hub."""

    def __init__(self, engine: "RunEngine", run: Run) -> None:
        self.engine = engine
        self.run = run
        self.username = run.username
        self.book_id = run.book_id
        self.start_id: str = run.request["activeDocumentId"]
        self.original: str = ""
        self.view: str = self.start_id        # what the sending tab shows
        self.expected: str = self.start_id    # what the run expects it to show
        self.moved = False
        self._contents: Dict[str, Dict[str, Any]] = {}   # id → {revision, content}
        self._last_preview = 0.0
        self._last_selection_preview = 0.0
        self.previewing: Optional[str] = None
        self.writing: Optional[str] = None
        self.image_registry: List[Dict[str, str]] = []
        #: The book's image numbers as loaded (server_context.stable_image_registry), to save back.
        self.image_keys: List[str] = []
        self.selected_text: str = run.request.get("selectedText") or ""
        # For the reminders the model gets when the book moves under it: the
        # revision of each chapter as the model last saw or wrote it, the
        # chapter list it was given, and how many requests waited last time.
        self.watched: Dict[str, int] = {}
        self.last_outline: Optional[str] = None
        self.last_queued = 0

    # DocumentPort
    def chapters(self) -> List[Dict[str, Any]]:
        out = []
        for meta in server_documents.list_documents(self.username, self.book_id):
            cached = self._contents.get(meta["id"])
            if cached is None or cached["revision"] != meta["revision"]:
                cached = {"revision": meta["revision"], "content": load_document_content(self.username, self.book_id, meta["id"])}
                self._contents[meta["id"]] = cached
            out.append({"id": meta["id"], "title": meta["title"], "content": cached["content"],
                        "updatedAt": meta.get("updated_at"), "revision": meta["revision"], "loaded": True,
                        "pinned": bool(meta.get("pinned"))})
        return out

    def open_id(self) -> str:
        return self.view

    def user_moved(self) -> bool:
        if not self.moved and self.view != self.expected:
            self.moved = True
        return self.moved

    async def ensure_loaded(self, ids: List[str]) -> None:
        return None

    def commit(self, doc_id: str, html: str) -> None:
        cached = self._contents.get(doc_id)
        out = server_documents.write_document(self.username, self.book_id, doc_id, content=html,
                                              base_revision=cached["revision"] if cached else None, client_id=self._client_id())
        if not out.get("ok"):
            raise RuntimeError("the chapter changed while the write was being made")
        self._contents[doc_id] = {"revision": out["revision"], "content": html}
        self.watched[doc_id] = out["revision"]

    def open(self, doc_id: str) -> None:
        self.expected = doc_id
        self.view = doc_id
        self.engine.publish(self.run, "open", {"documentId": doc_id})

    def create(self, title: str) -> str:
        created = server_documents.create_document(self.username, self.book_id, title, "<p></p>", client_id=self._client_id())
        self._contents[created["id"]] = {"revision": 1, "content": "<p></p>"}
        self.watched[created["id"]] = 1
        self.last_outline = chapter_outline(self.chapters())
        return created["id"]

    def move(self, doc_id: str, index: int) -> None:
        """Put a chapter at `index` (0-based) in the book's order; the others keep theirs around it."""
        ids = [c["id"] for c in server_documents.list_documents(self.username, self.book_id)]
        if doc_id not in ids:
            return
        ids.remove(doc_id)
        ids.insert(max(0, min(index, len(ids))), doc_id)
        server_documents.reorder_documents(self.username, self.book_id, ids, client_id=self._client_id())
        self.last_outline = chapter_outline(self.chapters())

    def rename(self, doc_id: str, title: str) -> None:
        out = server_documents.write_document(self.username, self.book_id, doc_id, title=title, client_id=self._client_id())
        if out.get("ok") and doc_id in self._contents:
            self._contents[doc_id]["revision"] = out["revision"]
        if out.get("ok"):
            self.watched[doc_id] = out["revision"]
        self.last_outline = chapter_outline(self.chapters())

    def remove(self, doc_id: str) -> None:
        server_documents.delete_document(self.username, self.book_id, doc_id, client_id=self._client_id())
        self._contents.pop(doc_id, None)
        self.watched.pop(doc_id, None)
        self.last_outline = chapter_outline(self.chapters())
        if self.view == doc_id:
            remaining = server_documents.list_documents(self.username, self.book_id)
            self.view = remaining[0]["id"] if remaining else ""
            if not self.moved:
                self.expected = self.view

    def snapshot(self, doc_id: str, label: str) -> None:
        content = next((c["content"] for c in self.chapters() if c["id"] == doc_id), "")
        self.run.snapshots.append(server_documents.snapshot_document(self.username, self.book_id, doc_id, label, content))

    # EditorPort
    def preview_document(self, html: str) -> None:
        now = time.monotonic()
        target = self.view
        if self.previewing != target:
            self.previewing = target
            self._last_preview = 0.0
            self.publish_lock()
        if now - self._last_preview < PREVIEW_THROTTLE_S:
            return
        self._last_preview = now
        self.engine.publish(self.run, "preview", {"documentId": target, "html": restore_image_placeholders(html, self.image_registry)})

    def preview_selection(self, html: str) -> None:
        now = time.monotonic()
        if now - self._last_selection_preview < SELECTION_PREVIEW_THROTTLE_S:
            return
        self._last_selection_preview = now
        self.engine.publish(self.run, "preview_selection", {"documentId": self.start_id, "html": restore_image_placeholders(html, self.image_registry)})

    def discard_preview(self) -> None:
        if self.previewing is None:
            return
        self.engine.publish(self.run, "preview", {"documentId": self.previewing, "html": None})
        self.previewing = None
        self.publish_lock()

    def settle_preview(self) -> None:
        """A step's writes landed: the preview is over, the client converges on the store."""
        if self.previewing is None:
            return
        self.engine.publish(self.run, "preview", {"documentId": self.previewing, "html": None, "settled": True})
        self.previewing = None
        self._last_preview = 0.0
        self.publish_lock()

    # SelectionPort
    def range(self):
        return True if self.selected_text else None

    def original_text(self) -> str:
        return self.selected_text

    # ImagePort
    def preserve(self, html: str) -> str:
        # By the image, not its spelling: a tag re-spelled on the way (view_images reads it out of a split
        # paragraph, quotes normalized) is the picture the book already numbered, not a new one.
        def same_image(m) -> str:
            tag = m.group(0)
            if any(e["tag"] == tag for e in self.image_registry):
                return tag
            key = server_context.image_key(tag)
            known = next((e for e in self.image_registry if e["tag"] and server_context.image_key(e["tag"]) == key), None)
            return known["placeholder"] if known else tag
        return replace_images_with_placeholders(re.sub(r"<img[^>]+>", same_image, html), self.image_registry)

    def restore(self, html: str) -> str:
        return restore_image_placeholders(html, self.image_registry)

    def register(self, data_url: str, alt: str = "") -> str:
        return register_image(self.image_registry, data_url, alt)

    def load_image_numbers(self, chapters: List[Dict[str, Any]]) -> None:
        """Start the registry from the book's own image numbers, so a token means the same picture in every turn."""
        self.image_keys = server_context.load_image_numbers(self.username, self.book_id)
        self.image_registry[:] = server_context.stable_image_registry(self.image_keys, chapters)

    def save_image_numbers(self) -> None:
        keys = server_context.registry_keys(self.image_registry, self.image_keys)
        if keys != self.image_keys:
            server_context.save_image_numbers(self.username, self.book_id, keys, _now_iso())
            self.image_keys = keys

    # UiPort
    def progress(self, line: Optional[str]) -> None:
        self.engine.publish(self.run, "progress", {"line": line})

    def set_writing(self, doc_id: Optional[str]) -> None:
        self.writing = doc_id
        self.publish_lock()

    def publish_lock(self) -> None:
        ids = [i for i in (self.start_id if self.selected_text else None, self.previewing, self.writing) if i]
        self.engine.publish(self.run, "lock", {"documentIds": sorted(set(ids), key=ids.index)})

    def _client_id(self) -> Optional[str]:
        # The run's writes are not the sending tab's own: every tab, that one
        # included, must apply them.
        return None


class _Ui:
    def __init__(self, ports: ServerPorts) -> None:
        self.ports = ports

    def progress(self, line: Optional[str]) -> None:
        self.ports.progress(line)

    def writing(self, doc_id: Optional[str]) -> None:
        self.ports.set_writing(doc_id)


# ── The engine ───────────────────────────────────────────────────────────────

class RunEngine:
    def __init__(self) -> None:
        self.runs: Dict[str, Run] = {}
        self.queue_held: Dict[tuple, bool] = {}
        self._loaded = False

    # ── lookup ────────────────────────────────────────────────────────────
    def _key(self, run: Run) -> tuple:
        return (run.username, run.book_id)

    def get(self, username: str, run_id: str) -> Optional[Run]:
        run = self.runs.get(run_id)
        return run if run and run.username == username else None

    def active(self, username: str, book_id: str) -> List[Run]:
        self.prune()
        return sorted((r for r in self.runs.values() if r.username == username and r.book_id == book_id and r.status in ACTIVE_STATUSES),
                      key=lambda r: r.created_at)

    def listed(self, username: str, book_id: str) -> List[Run]:
        self.prune()
        return sorted((r for r in self.runs.values() if r.username == username and r.book_id == book_id), key=lambda r: r.created_at)

    def running(self, username: str, book_id: str) -> Optional[Run]:
        return next((r for r in self.runs.values() if r.username == username and r.book_id == book_id and r.status == "running"), None)

    def prune(self) -> None:
        now = time.monotonic()
        for run_id, run in list(self.runs.items()):
            if run.finished_monotonic is not None and now - run.finished_monotonic > FINISHED_RUN_TTL_SECONDS:
                del self.runs[run_id]

    def queue_is_held(self, username: str, book_id: str) -> bool:
        return self.queue_held.get((username, book_id), False)

    def set_queue_held(self, username: str, book_id: str, held: bool) -> None:
        self.queue_held[(username, book_id)] = held
        conn = get_db()
        try:
            conn.execute("INSERT INTO run_books (username, book_id, queue_held) VALUES (?, ?, ?) ON CONFLICT(username, book_id) DO UPDATE SET queue_held = excluded.queue_held",
                         (username, book_id, 1 if held else 0))
            conn.commit()
        finally:
            conn.close()

    # ── events ────────────────────────────────────────────────────────────
    def publish(self, run: Run, kind: str, payload: Dict[str, Any]) -> None:
        server_events.hub.publish(run.username, run.book_id, {"type": "run", "kind": kind, "runId": run.id, **payload})

    def attended(self, run: Run) -> bool:
        return server_events.hub.subscriber_count(run.username, run.book_id) > 0

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def recover(self) -> None:
        """At startup: runs that were running or queued when the process died continue."""
        if self._loaded:
            return
        self._loaded = True
        ensure_tables()
        conn = get_db()
        try:
            rows = conn.execute("SELECT * FROM runs WHERE status IN ('queued', 'running', 'paused') ORDER BY created_at").fetchall()
            held = conn.execute("SELECT username, book_id, queue_held FROM run_books").fetchall()
        finally:
            conn.close()
        for h in held:
            self.queue_held[(h["username"], h["book_id"])] = bool(h["queue_held"])
        for row in rows:
            run = Run.from_row(row)
            self.runs[run.id] = run
        for run in list(self.runs.values()):
            if run.status == "running":
                logger.info("Run %s: resuming after restart at step %s", run.id, (run.snapshot or {}).get("stepsTaken", 0))
                self._launch(run)
        for key in {self._key(r) for r in self.runs.values() if r.status == "queued"}:
            self._advance(*key)

    def submit(self, username: str, book_id: str, request: Dict[str, Any]) -> Run:
        run = Run(f"run-{secrets.token_hex(6)}", username, book_id, request)
        self.runs[run.id] = run
        self._write_messages_at_start(run)
        if self.running(username, book_id) is None:
            self._launch(run)
        else:
            run.persist()
            position = len([r for r in self.active(username, book_id) if r.status == "queued"])
            self.publish(run, "queued", {"run": run.summary(), "position": position})
        return run

    def _advance(self, username: str, book_id: str) -> None:
        if self.running(username, book_id) is not None or self.queue_is_held(username, book_id):
            return
        queued = [r for r in self.active(username, book_id) if r.status == "queued"]
        if queued:
            self._launch(queued[0])

    def _launch(self, run: Run) -> None:
        run.status = "running"
        run.stop_requested = False
        run.stop_event = asyncio.Event()
        run.persist()
        run.task = asyncio.create_task(self._execute(run))

    def stop(self, run: Run) -> None:
        """Stop pauses the queue: the current run stops, queued requests stay."""
        if run.status == "queued":
            self._finalize(run, "stopped", content_note="⏹️ Removed before it started.")
            return
        if run.status == "paused":
            self._finalize(run, "stopped", content_note="⏹️ Stopped while paused.")
            return
        if run.status != "running":
            return
        self.set_queue_held(run.username, run.book_id, True)
        run.stop_requested = True
        run.stop_event.set()
        for side in list(run.side_jobs):
            side.abort()
        if run.agent:
            run.agent.cancel()
        if run.job is not None:
            run.job.abort()

    def resume(self, run: Run) -> bool:
        if run.status != "paused":
            return False
        if self.running(run.username, run.book_id) is not None:
            return False
        run.status = "running"
        # The user saw what it was repeating and chose to go on: that step goes without the check.
        run.skip_loop_check_once = (run.pause or {}).get("reason") == "repeating_output"
        run.pause = None
        run.unattended_steps = 0
        run.persist()
        run.task = asyncio.create_task(self._execute(run, resuming=True))
        return True

    def start_now(self, run: Run) -> bool:
        if run.status != "queued" or self.running(run.username, run.book_id) is not None:
            return False
        self._launch(run)
        return True

    def remove(self, run: Run) -> bool:
        if run.status not in ("queued", "paused"):
            return False
        self._finalize(run, "stopped", content_note="⏹️ Removed.")
        return True

    def set_view(self, run: Run, document_id: str) -> None:
        if run.ports is not None:
            run.ports.view = document_id

    def steer(self, run: Run, text: str, message_id: Optional[str] = None) -> bool:
        """A message sent while the run works goes to the run (agentic_chat_loop.md §0.8); only a running run takes it."""
        if run.status != "running" or run.agent is None or run.stop_requested:
            return False
        run.agent.steer(text, message_id)
        # In the snapshot, so a restart between steps keeps it.
        run.snapshot = run.agent.snapshot()
        run.persist()
        return True

    # ── execution ─────────────────────────────────────────────────────────
    async def _execute(self, run: Run, resuming: bool = False) -> None:
        try:
            if run.request.get("polish"):
                await self._execute_polish(run)
                return
            if run.agent is None:
                await self._build(run)
            agent = run.agent
            assert agent is not None
            if run.snapshot and run.snapshot.get("stepsTaken") and not agent.steps_taken:
                # A restart: continue from the last completed step.
                agent.restore(run.snapshot, lambda doc_id: next((c["content"] for c in run.ports.chapters() if c["id"] == doc_id), None))
                run.record = {**run.record, "status": "running"}
                self.publish(run, "started", {"run": run.summary(), "resumed": True})
                await agent.resume()
            elif resuming and (agent.has_next or agent.steps_taken):
                self.publish(run, "started", {"run": run.summary(), "resumed": True})
                await agent.resume()
            elif resuming:
                # Paused before its first step got through, then the process
                # restarted: the rebuilt run has that step still to send.
                self.publish(run, "started", {"run": run.summary(), "resumed": True})
                await agent.start()
            else:
                self.publish(run, "started", {"run": run.summary()})
                await agent.start()
        except RunStopped:
            self._finalize(run, "stopped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — any failure ends the run with its error on the bubble
            logger.warning("Run %s failed: %s", run.id, exc)
            run.error = str(exc) or "Run failed"
            self._finalize(run, "error")
        finally:
            run.job = None

    async def _execute_polish(self, run: Run) -> None:
        """The Polish button's run: one chapter through the polish model, no agent loop.

        It was the one turn left in the tab (backend_authority.md phase 4): the
        button called the model from the browser, so closing the page lost it.
        Here it is a run like any other — queued behind a running turn, stopped
        by Stop, rejoined by a reload — whose record is the one the tab built:
        a user line, one tool line, the chapter in "changed this turn".
        """
        doc_id = run.request["polish"]["documentId"]
        zh = run.request["polish"].get("language") == "zh"
        ports = ServerPorts(self, run)
        run.ports = ports
        chapters = ports.chapters()
        index = next((i for i, c in enumerate(chapters) if c["id"] == doc_id), -1)
        if index < 0:
            raise RunFailed("The chapter to polish is gone.")
        ports.start_id = ports.view = ports.expected = doc_id
        ports.original = chapters[index]["content"]
        ports.load_image_numbers(chapters)
        ctx = ToolContext(ports, ports, ports, ports, _Ui(ports), create_run_state(doc_id), polish=_PolishPort(self, run))
        # Not the start chapter for commit_doc: the polish takes its version snapshot here, before the change.
        target = {"id": doc_id, "title": chapters[index]["title"], "number": index + 1, "isStart": False}
        self.publish(run, "started", {"run": run.summary()})
        st = doc_state(ctx, target)
        if is_blank_content(st.html):
            raise RunFailed("The chapter is empty.")
        cite = cite_chapter(target)
        ctx.ui.writing(doc_id)
        try:
            outcome = await ctx.polish.run(ctx.images.preserve(st.html), lambda done, total: ports.progress(
                f"✨ 润色中 {done}/{total}…" if zh else f"✨ Polishing {done}/{total}…"))
        finally:
            ctx.ui.writing(None)
            ports.progress(None)
        discarded = user_edited(ctx, doc_id)
        if outcome["polished"] > 0 and not discarded:
            st.html = ctx.images.restore(outcome["html"])
            st.dirty = True
            commit_doc(ctx, target, st)
        polished = 0 if discarded else outcome["polished"]
        line = f"✨ polished {cite} ({polished} of {outcome['chunks']} chunk(s) rewritten)"
        notes = "\n\n".join(n for n in [
            ((f"保留初稿的段落：{'；'.join(outcome['kept'])}" if zh else f"Kept as drafted — {'; '.join(outcome['kept'])}") if outcome["kept"] else ""),
            (("⏹️ 已停止，未完成的段落保留初稿。" if zh else "⏹️ Stopped; unfinished chunks kept their draft.") if outcome["stopped"] else ""),
            (("你在润色期间改动了这一章，润色结果已丢弃。" if zh else "You edited the chapter while it was being polished, so the polish was discarded.") if discarded else ""),
        ] if n)
        status = "stopped" if outcome["stopped"] else "done"
        run.record = {"status": status, "steps": 1, "trace": [line],
                      "touched": [{"documentId": doc_id, "titleAtRun": target["title"], "kind": "polished",
                                   "changes": polished, "failed": outcome["chunks"] - polished}],
                      "timeline": [{"type": "tool", "line": line, "ok": polished > 0}],
                      "prefix": None, "suffix": notes or None, "plan": []}
        run.result = {"content": _join(line, notes), "record": run.record, "reasoningItems": [], "readIds": [],
                      "usage": None, "snapshots": run.snapshots}
        self._finalize(run, status)

    async def _build(self, run: Run) -> None:
        req = run.request
        ports = ServerPorts(self, run)
        run.ports = ports
        chapters = ports.chapters()
        if not any(c["id"] == ports.start_id for c in chapters):
            if not chapters:
                raise RunFailed("The book has no chapters.")
            ports.start_id = chapters[0]["id"]
            ports.view = ports.expected = ports.start_id
        ports.original = next(c["content"] for c in chapters if c["id"] == ports.start_id)
        ports.load_image_numbers(chapters)
        provider = req["provider"]
        config = req.get("config") or {}
        scope = f"{provider}|{config.get('model') or ''}"
        state = server_context.load_state(run.username, run.book_id, scope)
        ctx = ToolContext(ports, ports, ports, ports, _Ui(ports), create_run_state(ports.start_id),
                          polish=_PolishPort(self, run) if polish_offered(provider, config) else None, analyze=_AnalyzePort(self, run),
                          attachments=_AttachmentsPort(run.username, run.book_id),
                          web=_WebPort(run.username) if config.get("webAccess") is not False and server_web.available() else None,
                          vision=_VisionPort(run.username) if supports_vision(provider, config.get("model") or "") else None)
        history = self._history_for(run)
        # A summary asked for at the end of the live conversation offers the turn's tools, as the turn's steps
        # do: the tools are part of the cached prefix (cache_continuity.md §3.4).
        offered = offered_tools_for(CHAT_TOOLS, ctx, resolve_document_protocol(provider, config.get("documentProtocol")),
                                    resolve_run_settings(provider, config)["agentTools"])
        assembled = await server_context.assemble_request(
            provider=provider, config=config, prompt_text=req["prompt"], images=req.get("images"),
            history=history, documents=chapters, active_document_id=ports.start_id,
            selected_text=ports.selected_text, custom_instructions=req.get("customInstructions"),
            context_window_tokens=req.get("contextWindowTokens"), state=state, image_registry=ports.image_registry,
            attachments=server_attachments.list_attachments(run.username, run.book_id),
            stored_summary=server_context.load_chat_summary(run.username, run.book_id),
            summarize=_ModelCall(self, run, None, "low", f"{run.book_id}:summary"),
            transcripts=server_context.load_transcripts(run.username, run.book_id,
                                                        [str(m.get("id")) for m in history if m.get("role") == "assistant" and m.get("id")], scope),
            turn=str(req.get("assistantMessageId") or ""),
            summarize_live=_ConversationCall(self, run, to_tool_specs(offered)))
        server_context.save_state(run.username, run.book_id, scope, state, _now_iso())
        if assembled.get("chatSummary"):
            server_context.save_chat_summary(run.username, run.book_id, assembled["chatSummary"], _now_iso())
        run.request["attachmentsText"] = assembled["attachmentsText"]
        run.record["prefix"] = assembled["attachmentsText"] or None
        settings = assembled["settings"]
        ctx.run = create_run_state(ports.start_id, assembled["inContextIds"], ports.original, chapter_outline(chapters),
                                   text_seen=server_context.text_seen_in_context(chapters, assembled["inContextIds"]))
        # The pages the user linked are the turn's sources: web_read keeps them as attachments (attachments_and_web.md §2d).
        ctx.run.user_urls = urls_in_text(req["prompt"])
        ports.last_outline = chapter_outline(chapters)
        for doc_id in ctx.run.known:
            ports.watched[doc_id] = next((c["revision"] for c in chapters if c["id"] == doc_id), 0)
        long_reasoning = config.get("longReasoningReminderTokens")
        # The prompt a step may use: the window's target less the output (wc_text.run_compaction).
        prompt_limit = (target_prompt_tokens(get_cache_profile(provider), resolve_context_window_tokens(provider, config.get("model") or "", req.get("contextWindowTokens")))
                        - int(config.get("maxOutputTokens") or 16_384))
        run.agent = AgentRun(
            registry=CHAT_TOOLS, ctx=ctx, write_protocol=assembled["protocol"],
            driver=lambda messages, step, final: self._step(run, messages, step, final),
            observer=_Observer(self, run), budgets=settings["budgets"], policy=settings["policy"],
            can_continue=True, agent_tools=settings["agentTools"], initial_messages=assembled["apiMessages"],
            guard=lambda agent: self._guard(run, agent),
            long_reasoning_tokens=int(long_reasoning) if isinstance(long_reasoning, (int, float)) and long_reasoning > 0 else 0,
            reminders=lambda agent: self._reminders(run, agent), prompt_token_limit=max(0, prompt_limit),
            # A new turn picks up the plan the previous one left unfinished (wc_text.reminders.carried_plan_reminder).
            carried_plan=previous_turn_plan(history, str(req.get("assistantMessageId") or "")))

    def _history_for(self, run: Run) -> List[Dict[str, Any]]:
        """The conversation before this turn.

        The client's copy, with two corrections for a request that waited in
        the queue: a bubble that was still a placeholder when it was sent is
        read from the messages table (the server wrote the finished turn
        there), and turns that landed after the snapshot are appended.
        """
        history = list(run.request.get("history") or [])
        own = {run.request.get("userMessageId"), run.request.get("assistantMessageId")}
        conn = get_db()
        try:
            rows = conn.execute("SELECT * FROM messages WHERE username = ? AND book_id = ? ORDER BY sort_order",
                                (run.username, run.book_id)).fetchall()
        finally:
            conn.close()
        by_id = {m["id"]: m for m in rows}

        def from_row(m) -> Dict[str, Any]:
            entry: Dict[str, Any] = {"id": m["id"], "role": m["role"], "content": m["content"]}
            for column, key in (("agent", "agent"), ("reasoning_items", "reasoningItems")):
                if m[column]:
                    try:
                        entry[key] = json.loads(m[column])
                    except ValueError:
                        pass
            return entry

        merged: List[Dict[str, Any]] = []
        last_order = -1
        for m in history:
            row = by_id.get(m.get("id"))
            if row is not None:
                last_order = max(last_order, row["sort_order"])
                if m.get("role") == "assistant" and m.get("content") == ASSISTANT_PLACEHOLDER and row["content"] != ASSISTANT_PLACEHOLDER:
                    merged.append({**m, **from_row(row)})
                    continue
            merged.append(m)
        known = {m.get("id") for m in history} | own
        for m in rows:
            if m["id"] in known or m["sort_order"] <= last_order or m["role"] not in ("user", "assistant") or m["content"] == ASSISTANT_PLACEHOLDER:
                continue
            merged.append(from_row(m))
        return merged

    async def _step(self, run: Run, messages: List[Dict[str, Any]], step: int, final: bool) -> StepOutput:
        """One model call, as a generation job whose events are forwarded live.

        A job that broke after it started streaming, or that xAI's repetition
        detector stopped, is sent once more (the preview discarded, the tab
        told the step starts over; a loop gets a reminder). When that is not
        enough, or the stream went idle, the step is unavailable and the run
        pauses (agentic_chat_loop.md §0.10). Other failures end the run.
        """
        if run.stop_requested:
            raise RunStopped()
        provider = run.request["provider"]
        # A resume after `repeating_output` sends the step without the check: the user chose to go on.
        loop_check = provider == "grok" and not run.skip_loop_check_once
        run.skip_loop_check_once = False
        request_messages = messages
        notice: Optional[str] = None
        redone = 0
        while True:
            job = await self._run_step_job(run, request_messages, step, final, loop_check, notice)
            if job.status == "aborted":
                raise RunStopped()
            if job.status != "error":
                break
            kind = job.error_kind or "fatal"
            redo = kind == "repetition" or (kind == "transient" and job.failed_after_output)
            if redo and redone < STEP_REDO_BUDGET and not run.stop_requested:
                redone += 1
                if run.ports is not None:
                    run.ports.discard_preview()
                if kind == "repetition":
                    request_messages = [*messages, {"role": "user", "content": wrap_reminder(REPETITION_RETRY_NOTE)}]
                    notice = "🔁 The reply started repeating itself; writing it again."
                else:
                    notice = "🔁 The connection dropped mid-reply; writing it again."
                logger.info("Run %s step %s: %s (%s); redoing it", run.id, step, kind, job.error)
                continue
            if kind == "repetition":
                raise StepUnavailable({"reason": "repeating_output", "triggers": list(job.loop_triggers),
                                       "message": f"The reply kept repeating itself ({', '.join(job.loop_triggers)})."})
            if kind in ("transient", "idle"):
                raise StepUnavailable({"reason": "step_failed", "message": job.error or "The model call failed."})
            raise RunFailed(job.error or "Generation failed.")
        calls = finish_tool_calls({i: {"id": c.get("id"), "name": c.get("name"), "argumentsText": c.get("arguments") or "",
                                       **({"signature": c["signature"]} if c.get("signature") else {})}
                                   for i, c in job.tool_calls.items()})
        run.last_response_items = list(job.response_items)
        return StepOutput(job.buffer, calls, thinking=list(job.thinking_blocks) or None,
                          response_items=list(job.response_items) or None, usage=job.usage)

    async def _run_step_job(self, run: Run, messages: List[Dict[str, Any]], step: int, final: bool, loop_check: bool,
                            notice: Optional[str]) -> server_generation.GenerationJob:
        """Stream one attempt of a step to a terminal job, forwarding its events."""
        agent, ports = run.agent, run.ports
        assert agent is not None and ports is not None
        req = run.request
        provider = req["provider"]
        offered = agent.offered_tools()
        config = {**(req.get("config") or {}), "conversationId": run.book_id,
                  "tools": to_openai_tools(to_tool_specs(offered)) if offered else None,
                  "toolChoice": "none" if final and offered else None,
                  "loopCheck": True if loop_check else None}
        config = {k: v for k, v in config.items() if v is not None}
        job = server_generation.registry.create(run.username, {"kind": "run", "runId": run.id, "step": step, "bookId": run.book_id})
        job.input_chars = sum(len(str(m.get("content") or "")) for m in messages)
        job.conversation = run.book_id
        job.model = str(config.get("model") or "") or None
        run.job = job
        run.live_text = ""
        run.live_reasoning = ""
        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.add(queue)
        task = asyncio.create_task(server_generation.run_job(job, provider, config, messages))
        job.task = task  # what GenerationJob.abort cancels
        tool_acc: Dict[int, Dict[str, Any]] = {}
        last_split = 0.0
        self.publish(run, "step_started", {"step": step, "final": final})
        if notice:
            ports.progress(notice)
        try:
            while True:
                event = await queue.get()
                kind = event.get("type")
                if kind == "delta":
                    run.live_text += event["text"]
                    self.publish(run, "delta", {"step": step, "text": event["text"]})
                    now = time.monotonic()
                    if now - last_split >= SPLIT_THROTTLE_S:
                        last_split = now
                        self._preview_markup(run, run.live_text)
                elif kind == "reasoning":
                    run.live_reasoning = (run.live_reasoning + event["text"])[-2000:]
                    self.publish(run, "reasoning", {"step": step, "text": event["text"]})
                elif kind == "retry":
                    ports.progress(f"⏳ The model call failed ({event.get('reason') or 'error'}); retry {event.get('attempt')}/{event.get('max')}…")
                elif kind == "tool_call":
                    index = event.get("index") or 0
                    acc = tool_acc.setdefault(index, {"argumentsText": ""})
                    if event.get("id"):
                        acc["id"] = event["id"]
                    if event.get("name"):
                        acc["name"] = event["name"]
                    acc["argumentsText"] = event["text"] if event.get("replay") else acc["argumentsText"] + (event.get("text") or "")
                    tool = CHAT_TOOLS.get(acc.get("name"))
                    if tool and tool.preview:
                        try:
                            tool.preview(acc["argumentsText"], agent.ctx)
                        except Exception as exc:  # noqa: BLE001 — a preview must never fail the step
                            logger.debug("Run %s preview failed: %s", run.id, exc)
                elif kind in server_generation.TERMINAL_EVENT_TYPES:
                    break
        finally:
            job.subscribers.discard(queue)
            await task
            job.result_delivered = True
        run.reasoning_heads.append(job.reasoning_text[:400])
        run.reasoning_heads = run.reasoning_heads[-6:]
        return job

    def _preview_markup(self, run: Run, raw: str) -> None:
        agent = run.agent
        if agent is None:
            return
        try:
            split = split_streaming_response(raw)
            if split["isSelectionEdit"]:
                cleaned = strip_incomplete_end_tag(split["selectionReplaceText"])
                if cleaned:
                    preview_selection_with_count(agent.ctx, cleaned)
            elif js_trim(split["canvasText"]):
                ref = {"create": split["canvasNewChapter"]} if split.get("canvasNewChapter") else split.get("canvasChapter")
                preview_rewrite(agent.ctx, ref, trim_incomplete_html_tail(split["canvasText"]))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Run %s markup preview failed: %s", run.id, exc)

    def _reminders(self, run: Run, agent: AgentRun) -> List[str]:
        """What moved under the run since its last step: the user's edits to
        chapters the model has seen, the chapter list, and requests waiting
        behind this turn (wc_text.reminders)."""
        ports = run.ports
        if ports is None:
            return []
        out: List[str] = []
        chapters = ports.chapters()
        current = {c["id"]: c for c in chapters}
        edited = []
        for doc_id in [*agent.ctx.run.known, *[i for i in agent.ctx.run.read_ids if i not in agent.ctx.run.known]]:
            now = current.get(doc_id)
            seen = ports.watched.get(doc_id)
            if now is None:
                continue
            if seen is None:
                ports.watched[doc_id] = now["revision"]
                continue
            if now["revision"] > seen:
                ports.watched[doc_id] = now["revision"]
                # Accepting the run's diff saves the chapter too; it is not an
                # edit to read again (user_edited adopts it).
                if doc_id in agent.ctx.run.known and not user_edited(agent.ctx, doc_id):
                    continue
                edited.append({"number": chapters.index(now) + 1, "title": now["title"]})
        if edited:
            out.append(user_edited_reminder(edited))
        outline = chapter_outline(chapters)
        if ports.last_outline is not None and outline != ports.last_outline:
            out.append(structure_changed_reminder("\n".join(f'{i + 1}. "{c["title"]}"' for i, c in enumerate(chapters))))
        ports.last_outline = outline
        queued = len([r for r in self.active(run.username, run.book_id) if r.status == "queued"])
        if queued != ports.last_queued and queued > 0:
            out.append(queued_request_reminder(queued))
        ports.last_queued = queued
        return out

    def _guard(self, run: Run, agent: AgentRun) -> Optional[Dict[str, Any]]:
        """Pause, never kill: the reasons a run is suspended between steps."""
        if run.stop_requested:
            return None
        # The calls, not their answers: a repeated list_chapters answers with
        # "identical to your previous result" from the second time on, and
        # the loop this guards against (13× list_chapters, 2026-10-06) is
        # the same call with the same arguments, whatever the tool says back.
        # The loop itself nudged the model at REPEAT_NUDGE_STEPS; this is
        # the pause after the nudge went unheeded.
        if agent.identical_run_length() >= REPEAT_STEPS:
            return self._pause_detail(run, agent, "repeating",
                                      f"The last {REPEAT_STEPS} steps made the same calls with the same arguments, and wrote nothing, after being told so.")
        if self.attended(run):
            run.unattended_steps = 0
        else:
            run.unattended_steps += 1
            if run.unattended_steps > UNATTENDED_STEP_CEILING:
                return self._pause_detail(run, agent, "unattended",
                                          f"No tab has been watching this book for {run.unattended_steps} steps.")
        budget = (run.request.get("config") or {}).get("runTokenBudget")
        if isinstance(budget, (int, float)) and budget > 0:
            spent = agent.usage["promptTokens"] + agent.usage["completionTokens"]
            if spent > budget:
                return self._pause_detail(run, agent, "token_budget", f"This run has used {spent:,} tokens, past its budget of {int(budget):,}.")
        return None

    def _pause_detail(self, run: Run, agent: AgentRun, reason: str, message: str) -> Dict[str, Any]:
        steps = []
        heads = run.reasoning_heads[-PAUSE_DETAIL_STEPS:]
        for i, s in enumerate(agent.step_log[-PAUSE_DETAIL_STEPS:]):
            steps.append({"calls": [c["name"] for c in s["calls"]], "results": [r[:200] for r in s["results"]],
                          "reasoning": heads[i] if i < len(heads) else ""})
        return {"reason": reason, "message": message, "steps": steps, "usage": dict(agent.usage)}

    # ── observers ─────────────────────────────────────────────────────────
    def on_corrective(self, run: Run, failure: str, attempt: int, mx: int) -> None:
        run.ports.settle_preview() if run.ports else None
        self.publish(run, "corrective", {"failure": failure, "attempt": attempt, "max": mx})

    def on_step_executed(self, run: Run, progress: Dict[str, Any]) -> None:
        if run.ports:
            run.ports.progress(None)
            run.ports.settle_preview()
            # A restart rebuilds the registry from the saved numbers: a number given this step must be among them.
            run.ports.save_image_numbers()
        run.record = {**run.record, "status": "running", "steps": progress["steps"], "trace": progress["trace"], "touched": progress["touched"],
                      "timeline": progress["timeline"], "seen": progress["seen"], "plan": progress.get("plan") or []}
        run.snapshot = run.agent.snapshot() if run.agent else None
        run.persist()
        self._update_assistant_message(run, content=run.request.get("attachmentsText") or ASSISTANT_PLACEHOLDER)
        self.publish(run, "step", {"record": run.record, "usage": progress.get("usage")})

    def on_asked(self, run: Run, question: Dict[str, Any], progress: Dict[str, Any]) -> None:
        """The model asked the user (ask_user): the run waits as a pause with the question."""
        reason = {"reason": "question", "message": question["question"], "question": question["question"], "options": question.get("options") or []}
        if question.get("confirm"):
            reason["confirm"] = question["confirm"]
        self.on_paused(run, reason, progress)

    def answer(self, run: Run, answer: str, confirm: bool = False) -> bool:
        """Continue a run paused on a question. `confirm` is the Delete button of a delete_chapter question."""
        # Problem: a run paused on a question when the API restarted came back from the database with no
        #   agent, and every answer was refused (409 "not waiting for an answer") — the question could never
        #   be answered (2026-10-18: a deploy restarted the API three seconds after the run asked).
        # Fix: the run is rebuilt from its snapshot (which keeps the question) when the answer arrives.
        if run.status != "paused" or not run.pause or run.pause.get("reason") != "question":
            return False
        if run.agent is None and not (run.snapshot and run.snapshot.get("stepsTaken")):
            return False
        if self.running(run.username, run.book_id) is not None:
            return False
        run.status = "running"
        run.pause = None
        run.unattended_steps = 0
        run.persist()
        run.task = asyncio.create_task(self._execute_answer(run, answer, confirm))
        return True

    async def _execute_answer(self, run: Run, answer: str, confirm: bool = False) -> None:
        try:
            if run.agent is None:
                # Paused across a restart: rebuild it and restore the step it asked at.
                await self._build(run)
                run.agent.restore(run.snapshot, lambda doc_id: next((c["content"] for c in run.ports.chapters() if c["id"] == doc_id), None))
            self.publish(run, "started", {"run": run.summary(), "resumed": True})
            await run.agent.resume_with_answer(answer, confirmed=confirm)
        except RunStopped:
            self._finalize(run, "stopped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run %s failed: %s", run.id, exc)
            run.error = str(exc) or "Run failed"
            self._finalize(run, "error")
        finally:
            run.job = None

    def on_paused(self, run: Run, reason: Dict[str, Any], progress: Dict[str, Any]) -> None:
        if run.ports:
            run.ports.settle_preview()
        run.status = "paused"
        run.pause = reason
        run.record = {**run.record, "status": "paused", "steps": progress["steps"], "trace": progress["trace"], "touched": progress["touched"],
                      "timeline": progress["timeline"], "seen": progress["seen"], "plan": progress.get("plan") or []}
        run.snapshot = run.agent.snapshot() if run.agent else None
        run.persist()
        self._update_assistant_message(run, content=_join(run.request.get("attachmentsText"), progress["chatText"]) or ASSISTANT_PLACEHOLDER)
        self.publish(run, "paused", {"record": run.record, "pause": reason})
        logger.info("Run %s paused: %s", run.id, reason.get("reason"))

    def on_finish(self, run: Run, summary: Dict[str, Any]) -> None:
        if run.ports:
            run.ports.progress(None)
            run.ports.settle_preview()
        end = summary["endReason"]
        status = "stopped" if end == "cancelled" else "done"
        record_status = "step_limit" if end == "step_limit" else "stopped" if end == "cancelled" else "done"
        warning = build_completion_warnings({
            "canvasIssue": summary["effects"]["canvasIssue"], "editFailedCount": summary["effects"]["failedEdits"],
            "strayMarkup": summary["strayMarkup"], "selectionGone": summary["effects"]["selectionGone"],
            "toolCallProducedNothing": summary["effects"]["producedNothing"], "exhaustedNoActionRetries": summary["exhaustedCorrective"],
            "unretriableFailedUpdate": summary["unretriableFailedUpdate"], "reinsertedImages": summary["effects"]["reinsertedImages"],
        })
        limit_note = (f"\n\nℹ️ This turn stopped at its step limit ({summary['steps']} steps). Reply \"continue\" to let it go on, or raise the limit in Settings."
                      if end == "step_limit" else "")
        # Text kept for a chapter that does not exist and never placed (backend_authority.md §6.2) is discarded, and said so.
        held = run.agent.ctx.run.held_text if run.agent else None
        if held:
            limit_note += (f"\n\nℹ️ Text written for a chapter that does not exist (\"{held['ref']}\", "
                           f"{chapter_chars(held['html'])} characters) was never placed, so it was discarded.")
        chat_text = summary["chatText"] or "Document updated successfully."
        if end == "cancelled":
            chat_text = _join(summary["chatText"], "⏹️ Stopped.")
        content = _join(run.request.get("attachmentsText"), chat_text) + warning + limit_note
        reasoning_items = [i for i in run.last_response_items if isinstance(i, dict) and i.get("type") == "reasoning"]
        run.record = {"status": record_status, "steps": summary["steps"], "trace": summary["trace"], "touched": summary["touched"],
                      "timeline": summary["timeline"], "prefix": run.request.get("attachmentsText") or None,
                      "suffix": js_trim(warning + limit_note) or None, "plan": summary.get("plan") or []}
        run.result = {"content": content, "record": run.record, "reasoningItems": reasoning_items, "readIds": summary["readIds"],
                      "usage": summary.get("usage"), "snapshots": run.snapshots}
        run.snapshot = None
        self._remember_reads(run, summary)
        # The turn as sent, for the next one to replay verbatim (cache_continuity.md §3.1).
        if summary.get("transcript") and run.request.get("assistantMessageId"):
            server_context.save_transcript(run.username, run.book_id, run.request["assistantMessageId"], run.request.get("userMessageId"),
                                           summary["transcript"], _now_iso(),
                                           f"{run.request['provider']}|{(run.request.get('config') or {}).get('model') or ''}")
        self._finalize(run, status)

    def _remember_reads(self, run: Run, summary: Dict[str, Any]) -> None:
        """What the model read and wrote this turn feeds the next turn's context (D4, D7, D8)."""
        from wc_text.freshness import record_seen
        req = run.request
        scope = f"{req['provider']}|{(req.get('config') or {}).get('model') or ''}"
        state = server_context.load_state(run.username, run.book_id, scope)
        state["modelReadIds"] = list(summary["readIds"])
        # How far this provider|model counts from the estimate, for the next turn's history budget
        # (utils/tokenCalibration: below 1 is taken as 1, above 3 as 3).
        if isinstance(summary.get("tokenRatio"), (int, float)) and summary["tokenRatio"] > 0:
            state["tokenRatio"] = min(3.0, max(1.0, float(summary["tokenRatio"])))
        if run.ports:
            chapters = {c["id"]: c for c in run.ports.chapters()}
            for doc_id in [*summary["readIds"], *[t["documentId"] for t in summary["touched"]]]:
                if doc_id in chapters:
                    record_seen(state.setdefault("seen", {}), doc_id, chapters[doc_id]["content"], int(state.get("turn") or 0))
        server_context.save_state(run.username, run.book_id, scope, state, _now_iso())

    def _finalize(self, run: Run, status: str, content_note: Optional[str] = None) -> None:
        if run.ports:
            run.ports.save_image_numbers()
        run.status = status
        run.finished_at = _now_iso()
        run.finished_monotonic = time.monotonic()
        if run.result is None:
            progress = run.agent.progress() if run.agent else {"chatText": "", "steps": run.record.get("steps", 0), "trace": run.record.get("trace", []),
                                                             "touched": run.record.get("touched", []), "timeline": run.record.get("timeline", []), "readIds": []}
            note = content_note or ("⏹️ Stopped." if status == "stopped" else f"⚠️ Error during stream: {run.error}")
            run.record = {**run.record, "status": "stopped", "steps": progress["steps"], "trace": progress["trace"], "touched": progress["touched"],
                          "timeline": progress["timeline"], "suffix": note}
            run.record.pop("seen", None)
            run.result = {"content": _join(run.request.get("attachmentsText"), progress["chatText"], note), "record": run.record,
                          "reasoningItems": [], "readIds": progress.get("readIds", []), "snapshots": run.snapshots}
        run.snapshot = None
        run.pause = None
        if run.ports:
            run.ports.previewing = None
            run.ports.writing = None
            self.publish(run, "lock", {"documentIds": []})
        run.persist()
        self._update_assistant_message(run, content=run.result["content"], final=True)
        self.publish(run, "finished", {"status": status, "run": run.summary(), "result": run.result})
        logger.info("Run %s %s after %s steps", run.id, status, run.record.get("steps", 0))
        self._advance(run.username, run.book_id)

    # ── messages ──────────────────────────────────────────────────────────
    def _write_messages_at_start(self, run: Run) -> None:
        """The turn's two messages, as the client holds them, so another device loads them."""
        req = run.request
        now = _now_iso()
        conn = get_db()
        try:
            base = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM messages WHERE username = ? AND book_id = ?",
                                (run.username, run.book_id)).fetchone()["m"]
            for offset, (msg_id, role, content, images) in enumerate([
                (req.get("userMessageId"), "user", req["prompt"], req.get("images")),
                (req.get("assistantMessageId"), "assistant", ASSISTANT_PLACEHOLDER, None),
            ]):
                if not msg_id:
                    continue
                exists = conn.execute("SELECT 1 FROM messages WHERE username = ? AND book_id = ? AND id = ?", (run.username, run.book_id, msg_id)).fetchone()
                if exists:
                    continue
                conn.execute("""INSERT INTO messages (id, username, book_id, role, content, timestamp, model, sort_order, agent)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (msg_id, run.username, run.book_id, role, content, now, (req.get("config") or {}).get("model"), base + 1 + offset,
                              json.dumps(self._linked_record(run), ensure_ascii=False) if role == "assistant" else None))
            conn.commit()
        finally:
            conn.close()

    def _linked_record(self, run: Run) -> Dict[str, Any]:
        return {**run.record, "run": {"id": run.id, "status": run.status}}

    def _update_assistant_message(self, run: Run, content: str, final: bool = False) -> None:
        msg_id = run.request.get("assistantMessageId")
        if not msg_id:
            return
        # Always a record, with the run behind it: a reloading tab tells a
        # bubble that waits on a run from one that waits on nothing by it.
        record = self._linked_record(run)
        conn = get_db()
        try:
            if final and run.result:
                usage = run.result.get("usage") or {}
                conn.execute("""UPDATE messages SET content = ?, agent = ?, reasoning_items = ?, input_tokens = ?, output_tokens = ?, cache_hit_tokens = ?
                                WHERE username = ? AND book_id = ? AND id = ?""",
                             (content, json.dumps(record, ensure_ascii=False) if record else None,
                              json.dumps(run.result["reasoningItems"], ensure_ascii=False) if run.result.get("reasoningItems") else None,
                              usage.get("promptTokens"), usage.get("completionTokens"), usage.get("cachedPromptTokens"),
                              run.username, run.book_id, msg_id))
            else:
                conn.execute("UPDATE messages SET content = ?, agent = ? WHERE username = ? AND book_id = ? AND id = ?",
                             (content, json.dumps(record, ensure_ascii=False) if record else None, run.username, run.book_id, msg_id))
            conn.commit()
        finally:
            conn.close()


def _join(*parts: Optional[str]) -> str:
    return "\n\n".join(js_trim(p) for p in parts if p and js_trim(p))


class _Observer:
    def __init__(self, engine: RunEngine, run: Run) -> None:
        self.engine, self.run = engine, run

    def on_corrective(self, failure, attempt, mx):
        self.engine.on_corrective(self.run, failure, attempt, mx)

    def on_step_executed(self, progress):
        self.engine.on_step_executed(self.run, progress)

    def on_paused(self, reason, progress):
        self.engine.on_paused(self.run, reason, progress)

    def on_asked(self, question, progress):
        self.engine.on_asked(self.run, question, progress)

    def on_finish(self, summary):
        self.engine.on_finish(self.run, summary)


class _ModelCall:
    """One side call (a polish chunk, an analysis batch) as its own generation job."""

    def __init__(self, engine: RunEngine, run: Run, model: Optional[str], reasoning_effort: Optional[str], conversation: str) -> None:
        self.engine, self.run, self.model, self.effort, self.conversation = engine, run, model, reasoning_effort, conversation

    async def __call__(self, system: str, user: str, images: Optional[List[str]] = None) -> str:
        req = self.run.request
        config = {k: v for k, v in (req.get("config") or {}).items() if k not in ("tools", "toolChoice")}
        if self.model:
            config["model"] = self.model
        config["reasoningEffort"] = self.effort
        config["conversationId"] = self.conversation
        config = {k: v for k, v in config.items() if v is not None}
        job = server_generation.registry.create(self.run.username, {"kind": "batch", "runId": self.run.id, "bookId": self.run.book_id})
        job.conversation = self.conversation
        job.model = config.get("model")
        if self.run.stop_event.is_set():
            raise RunFailed("stopped")
        self.run.side_jobs.add(job)
        try:
            user_message: Dict[str, Any] = {"role": "user", "content": user}
            if images:
                user_message["images"] = images
            await server_generation.run_job(job, req["provider"], config, [{"role": "system", "content": system}, user_message])
        finally:
            self.run.side_jobs.discard(job)
        job.result_delivered = True
        if job.status != "done":
            raise RunFailed(job.error or "stopped")
        return job.buffer


class _ConversationCall:
    """The summary asked for at the end of the conversation as the turn would send it (cache_continuity.md §3.4):
    the turn's model, reasoning effort, tools (none to be called) and conversation id, so it reads from the cache."""

    def __init__(self, engine: RunEngine, run: Run, tool_specs: List[Dict[str, Any]]) -> None:
        self.engine, self.run, self.tool_specs = engine, run, tool_specs

    async def __call__(self, messages: List[Dict[str, Any]]) -> str:
        req = self.run.request
        config = {**(req.get("config") or {}), "conversationId": self.run.book_id,
                  "tools": to_openai_tools(self.tool_specs) if self.tool_specs else None,
                  "toolChoice": "none" if self.tool_specs else None}
        config["maxOutputTokens"] = min(int(config.get("maxOutputTokens") or 16_384), server_context.SUMMARY_MAX_OUTPUT_TOKENS)
        config = {k: v for k, v in config.items() if v is not None}
        job = server_generation.registry.create(self.run.username, {"kind": "batch", "runId": self.run.id, "bookId": self.run.book_id})
        job.conversation = self.run.book_id
        job.model = str(config.get("model") or "") or None
        await server_generation.run_job(job, req["provider"], config, messages)
        job.result_delivered = True
        if job.status != "done":
            raise RunFailed(job.error or "stopped")
        return job.buffer


class _PolishPort:
    # Problem: the run was kept as `self.run`, which hid this class's own
    #   `run()` method — every server-side polish_chapter raised "'Run' object
    #   is not callable" (found in the logs, 2026-10-09).
    # Fix: the run is `self.owner`; `run` is the port's method, as the tool calls it.
    def __init__(self, engine: RunEngine, run: Run) -> None:
        self.engine, self.owner = engine, run

    async def run(self, html: str, on_progress):
        req = self.owner.request
        cfg = req.get("config") or {}
        model = polish_model_for(req["provider"], cfg)
        transport = _ModelCall(self.engine, self.owner, model, "default", f"{self.owner.book_id}:polish")
        prompt = req.get("polishPrompt") or {"system": "", "template": "{part}"}
        return await polish_html(html, transport, prompt, req.get("customInstructions"), self.owner.stop_event, on_progress)


class _AnalyzePort:
    # The run is `self.owner` for the reason given on _PolishPort.
    def __init__(self, engine: RunEngine, run: Run) -> None:
        self.engine, self.owner = engine, run

    def plan(self, task: str, chapters):
        return plan_analysis(task, chapters, self.owner.request["provider"])

    async def run(self, task: str, chapters, on_progress):
        req = self.owner.request
        transport = _ModelCall(self.engine, self.owner, None, (req.get("config") or {}).get("reasoningEffort"), f"{self.owner.book_id}:analyze")
        # Batches sized under the provider's window and long-context price line (ANALYZE_BATCH_TOKENS).
        return await analyze_in_batches(task, chapters, self.plan(task, chapters)["batchChars"], transport, None, on_progress)

    async def look(self, task: str, items, on_progress):
        """view_images with a task: the pictures in batches, outside the conversation (polish.look_in_batches)."""
        req = self.owner.request
        transport = _ModelCall(self.engine, self.owner, None, (req.get("config") or {}).get("reasoningEffort"), f"{self.owner.book_id}:look")
        return await look_in_batches(task, items, transport, None, on_progress)


class _AttachmentsPort:
    """The book's reference files (attachments_and_web.md §1)."""

    def __init__(self, username: str, book_id: str) -> None:
        self.username, self.book_id = username, book_id

    def list(self):
        return server_attachments.list_attachments(self.username, self.book_id)

    async def paragraphs(self, attachment_id: str):
        return await asyncio.to_thread(server_attachments.attachment_paragraphs, self.username, self.book_id, attachment_id)

    async def images(self, attachment_id: str):
        return await asyncio.to_thread(server_attachments.attachment_images, self.username, self.book_id, attachment_id)

    async def save(self, name: str, text: str, images=None):
        """A new attachment from text the run fetched (web_save), with its pictures; the book's tabs reload their list."""
        saved = await asyncio.to_thread(server_attachments.store_attachment, self.username, self.book_id, name, text.encode("utf-8"), images)
        server_events.hub.publish(self.username, self.book_id, {"type": "attachments", "kind": "created", "clientId": None})
        return saved


class _VisionPort:
    """A chapter's images for view_images: inline ones shrunk, linked ones fetched like a page's (server_web.view_images)."""

    def __init__(self, username: str) -> None:
        self.username = username

    async def view(self, srcs):
        return await server_web.view_images(self.username, srcs)


class _WebPort:
    """The anonymous browser (attachments_and_web.md §2)."""

    def __init__(self, username: str) -> None:
        self.username = username

    async def search(self, query: str, max_results: int):
        return await server_web.web_search(self.username, query, max_results)

    async def read(self, url: str):
        return await server_web.web_read(self.username, url)

    async def images(self, url: str, numbers):
        return await server_web.web_images(self.username, url, numbers)


engine = RunEngine()


# ── Endpoints ────────────────────────────────────────────────────────────────

def _validate_request(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not js_trim(prompt):
        raise HTTPException(status_code=400, detail="prompt must be a non-empty string.")
    provider = body.get("provider")
    if provider not in server_generation.SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unsupported LLM provider: {provider}")
    config = body.get("config") or {}
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="config must be an object.")
    if not config.get("apiKey") and provider not in ("ollama", "runpod"):
        raise HTTPException(status_code=400, detail=f"API key is missing for {provider}. Please configure it in Settings.")
    active = body.get("activeDocumentId")
    if not isinstance(active, str) or not active:
        raise HTTPException(status_code=400, detail="activeDocumentId is required.")
    history = body.get("history") or []
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="history must be an array.")
    return {
        "prompt": prompt, "provider": provider, "config": config, "activeDocumentId": sanitize_id(active, "docId"),
        "images": body.get("images") if isinstance(body.get("images"), list) else None,
        "selectedText": body.get("selectedText") if isinstance(body.get("selectedText"), str) else "",
        "history": [m for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)],
        "userMessageId": body.get("userMessageId") if isinstance(body.get("userMessageId"), str) else None,
        "assistantMessageId": body.get("assistantMessageId") if isinstance(body.get("assistantMessageId"), str) else None,
        "customInstructions": body.get("customInstructions") if isinstance(body.get("customInstructions"), str) else None,
        "polishPrompt": body.get("polishPrompt") if isinstance(body.get("polishPrompt"), dict) else None,
        "contextWindowTokens": body.get("contextWindowTokens") if isinstance(body.get("contextWindowTokens"), int) else None,
        "clientId": body.get("clientId") if isinstance(body.get("clientId"), str) else None,
        "polish": _polish_request(body.get("polish")),
    }


def _polish_request(value: Any) -> Optional[Dict[str, Any]]:
    """The Polish button's chapter (and the bubble's language), or None for a chat turn."""
    if not isinstance(value, dict) or not isinstance(value.get("documentId"), str) or not value["documentId"]:
        return None
    return {"documentId": sanitize_id(value["documentId"], "docId"), "language": "zh" if value.get("language") == "zh" else "en"}


@router.post("/api/books/{book_id}/runs")
async def start_run(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    req = _validate_request(body)
    req["clientId"] = server_events.client_id_of(request) or req.get("clientId")
    await engine.recover()
    run = engine.submit(username, safe_book_id, req)
    position = len([r for r in engine.active(username, safe_book_id) if r.status == "queued" and r.created_at < run.created_at])
    return {"run": run.summary(), "position": position if run.status == "queued" else 0, "queueHeld": engine.queue_is_held(username, safe_book_id)}


@router.get("/api/books/{book_id}/runs")
async def list_runs(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    await engine.recover()
    runs = engine.listed(username, safe_book_id)
    return {"runs": [r.detail() if r.status in ACTIVE_STATUSES else r.summary() | {"result": r.result} for r in runs],
            "queueHeld": engine.queue_is_held(username, safe_book_id)}


def _run_or_404(username: str, run_id: str) -> Run:
    run = engine.get(username, sanitize_id(run_id, "runId"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


def _stored_run(username: str, run_id: str) -> Optional[Run]:
    """A run no longer in memory (pruned after FINISHED_RUN_TTL_SECONDS, or finished before a restart), from its row."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM runs WHERE id = ? AND username = ?", (sanitize_id(run_id, "runId"), username)).fetchone()
    finally:
        conn.close()
    return Run.from_row(row) if row else None


@router.get("/api/books/{book_id}/runs/{run_id}")
async def get_run(request: Request, book_id: str, run_id: str):
    username = get_authenticated_username(request)
    await engine.recover()
    # Problem: a tab that missed a run's `finished` (its stream dropped, the API restarted) asks for the run
    #   it follows; once the run had left memory it got 404, settled nothing, and kept the chapter locked
    #   and the chat "working" (2026-10-18: Accept all disabled on a chapter the finished run had written).
    # Fix: a finished run is read from its row; its result settles the tab as the live event would have.
    run = engine.get(username, sanitize_id(run_id, "runId")) or _stored_run(username, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"run": run.detail() if run.status in ACTIVE_STATUSES else run.summary() | {"result": run.result}}


@router.post("/api/books/{book_id}/runs/{run_id}/stop")
async def stop_run(request: Request, book_id: str, run_id: str):
    username = get_authenticated_username(request)
    run = _run_or_404(username, run_id)
    engine.stop(run)
    return {"success": True, "run": run.summary(), "queueHeld": engine.queue_is_held(username, run.book_id)}


@router.post("/api/books/{book_id}/runs/{run_id}/resume")
async def resume_run(request: Request, book_id: str, run_id: str):
    username = get_authenticated_username(request)
    run = _run_or_404(username, run_id)
    if not engine.resume(run):
        raise HTTPException(status_code=409, detail="This run is not paused, or another run is active.")
    return {"success": True, "run": run.summary()}


@router.post("/api/books/{book_id}/runs/{run_id}/answer")
async def answer_run(request: Request, book_id: str, run_id: str):
    username = get_authenticated_username(request)
    run = _run_or_404(username, run_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    answer = body.get("answer") if isinstance(body, dict) else None
    if not isinstance(answer, str) or not js_trim(answer):
        raise HTTPException(status_code=400, detail="answer must be a non-empty string.")
    if not engine.answer(run, js_trim(answer), confirm=body.get("confirm") is True):
        raise HTTPException(status_code=409, detail="This run is not waiting for an answer, or another run is active.")
    return {"success": True, "run": run.summary()}


@router.post("/api/books/{book_id}/runs/{run_id}/steer")
async def steer_run(request: Request, book_id: str, run_id: str):
    username = get_authenticated_username(request)
    run = _run_or_404(username, run_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not js_trim(text):
        raise HTTPException(status_code=400, detail="text must be a non-empty string.")
    message_id = body.get("messageId") if isinstance(body.get("messageId"), str) else None
    if not engine.steer(run, js_trim(text), message_id):
        raise HTTPException(status_code=409, detail="This run is not running; send the request as a new turn.")
    return {"success": True, "run": run.summary()}


@router.post("/api/books/{book_id}/runs/{run_id}/start")
async def start_queued_run(request: Request, book_id: str, run_id: str):
    username = get_authenticated_username(request)
    run = _run_or_404(username, run_id)
    if not engine.start_now(run):
        raise HTTPException(status_code=409, detail="This run is not queued, or another run is active.")
    return {"success": True, "run": run.summary()}


@router.post("/api/books/{book_id}/runs/{run_id}/view")
async def report_view(request: Request, book_id: str, run_id: str):
    username = get_authenticated_username(request)
    run = _run_or_404(username, run_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    doc_id = body.get("documentId") if isinstance(body, dict) else None
    if isinstance(doc_id, str) and doc_id:
        engine.set_view(run, sanitize_id(doc_id, "docId"))
    return {"success": True}


@router.delete("/api/books/{book_id}/runs/{run_id}")
async def remove_run(request: Request, book_id: str, run_id: str):
    username = get_authenticated_username(request)
    run = _run_or_404(username, run_id)
    if not engine.remove(run):
        raise HTTPException(status_code=409, detail="Only a queued or paused run can be removed.")
    return {"success": True, "run": run.summary()}


@router.post("/api/books/{book_id}/runs/queue")
async def set_queue(request: Request, book_id: str):
    username = get_authenticated_username(request)
    safe_book_id = sanitize_id(book_id, "bookId")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    held = bool((body or {}).get("held"))
    engine.set_queue_held(username, safe_book_id, held)
    if not held:
        engine._advance(username, safe_book_id)
    return {"success": True, "queueHeld": held}

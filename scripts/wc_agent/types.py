"""Port of src/agent/types.ts — the loop's vocabulary.

Tool results and invocations are plain dicts (they are persisted and sent as
events); the run's working state is a dataclass. Ports are duck-typed: see
the server's implementation in server_runs.py and the fake in test_agent.py.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from wc_text.context_ledger import hash_content


def result(ok: bool, content: str, trace: str, retryable: Optional[bool] = None, effects: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": ok, "content": content, "trace": trace}
    if retryable is not None:
        out["retryable"] = retryable
    if effects is not None:
        out["effects"] = effects
    return out


@dataclass
class DocState:
    original: str
    base: str
    review_base: str
    html: str
    dirty: bool = False


@dataclass
class RunState:
    step: int = 0
    write_protocol: Optional[str] = None
    continues_after_writes: Optional[bool] = None
    docs: Dict[str, DocState] = field(default_factory=dict)
    known: Dict[str, str] = field(default_factory=dict)
    html_shown: Set[str] = field(default_factory=set)
    in_context: Set[str] = field(default_factory=set)
    created: Set[str] = field(default_factory=set)
    snapshotted: Set[str] = field(default_factory=set)
    read_ids: Dict[str, None] = field(default_factory=dict)  # ordered set
    reads: Dict[str, int] = field(default_factory=dict)
    touched: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    start_outline: Optional[str] = None
    last_list: Optional[str] = None
    selection_attempted: bool = False
    selection_applied: bool = False
    #: The model's checklist for this turn (the `plan` tool).
    plan: List[Dict[str, str]] = field(default_factory=list)
    #: Writes landed (writes_so_far) when each plan item started: a "done" needs more since.
    plan_baseline: Dict[str, int] = field(default_factory=dict)
    #: A question the model asked the user this step (`ask_user`): {question, options}.
    question: Optional[Dict[str, Any]] = None
    #: Chapters whose whole current text the model has seen this run, by the hash of
    #: the accepted reading (agentic_chat_loop.md §0.11): a plain one may be rewritten
    #: without an HTML read.
    text_seen: Dict[str, str] = field(default_factory=dict)
    #: Characters of attachments read into the conversation this run (capped: ATTACHMENT_RUN_READ_CAP).
    attachment_chars: int = 0
    #: Chapters whose shrinking rewrite was held back once (wc_text.paragraphs.rewrite_loss); a second send applies.
    rewrite_loss_warned: Set[str] = field(default_factory=set)
    #: A whole rewrite written for a chapter that does not exist (backend_authority.md §6.2):
    #: {"ref", "html"}. The next update_document with an empty html writes it, unsent again.
    held_text: Optional[Dict[str, str]] = None
    #: Images view_images has shown this run (capped: view_images.VIEW_PER_RUN).
    images_viewed: int = 0
    #: Pages web_save or web_read stored this run (requested and final urls).
    web_saved: Set[str] = field(default_factory=set)
    #: Addresses the user wrote in this turn's message or a steer: web_read keeps those pages as attachments.
    user_urls: List[str] = field(default_factory=list)


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'，。！？、）)\]]+", re.I)


def urls_in_text(text: str) -> List[str]:
    """The http(s) addresses a message names, in order, each once (trailing punctuation dropped)."""
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in _URL_IN_TEXT.findall(text or "")))


def writes_so_far(run: RunState) -> int:
    """How much the run has written so far: chapters touched plus the changes in them."""
    return sum(1 + t["changes"] for t in run.touched.values())


def chapter_outline(chapters: List[Dict[str, Any]]) -> str:
    return "\u0001".join(f"{c['id']}\u0000{c['title']}" for c in chapters)


def create_run_state(start_id: str, in_context: Optional[List[str]] = None, start_content: Optional[str] = None,
                     start_outline: Optional[str] = None, text_seen: Optional[Dict[str, str]] = None) -> RunState:
    st = RunState(start_outline=start_outline, text_seen=dict(text_seen or {}))
    if start_content is not None:
        st.known[start_id] = start_content
    st.html_shown.add(start_id)
    st.in_context = {start_id, *(in_context or [])}
    return st


def seen_chapters(run: RunState) -> List[Dict[str, str]]:
    return [{"id": i, "hash": hash_content(run.known[i])} for i in run.html_shown if i in run.known]


def restore_seen(run: RunState, seen: Optional[List[Dict[str, str]]], stored: Callable[[str], Optional[str]]) -> RunState:
    for entry in seen or []:
        content = stored(entry["id"])
        if content is None or hash_content(content) != entry["hash"]:
            continue
        run.html_shown.add(entry["id"])
        run.known[entry["id"]] = content
        run.in_context.add(entry["id"])
    return run


class ToolContext:
    """The ports one run's tools work through (src/agent/types.ts ToolContext).

    document: start_id, original, chapters(), open_id(), user_moved(), ensure_loaded(ids) [async],
              commit(id, html), open(id), create(title) -> id, move(id, index), rename(id, title), remove(id), snapshot(id, label)
    editor:   preview_document(html), preview_selection(html), discard_preview()
    selection: range() -> bool-ish (a selection exists), original_text() -> str
    images:   preserve(html), restore(html), register(data_url, alt) -> placeholder
    ui:       progress(line|None), writing(id|None)
    polish:   optional, run(html, on_progress) -> outcome dict [async]
    analyze:  optional, run(task, chapters, on_progress) -> outcome dict [async]
    attachments: optional, list() -> [meta with ref A1…], paragraphs(id) -> [str] [async]
    web:      optional, search(query, max) -> [{title, url, snippet}] [async], read(url) -> {url, title, paragraphs, images} [async],
              images(url, numbers) -> [{n, alt, dataUrl, bytes} | {n, error}] [async]
    vision:   optional — the run's model takes image input: view(srcs) -> [{dataUrl, bytes} | {error}] [async]
    """

    def __init__(self, document, editor, selection, images, ui, run: RunState, polish=None, analyze=None, attachments=None, web=None, vision=None) -> None:
        self.document = document
        self.editor = editor
        self.selection = selection
        self.images = images
        self.ui = ui
        self.run = run
        self.polish = polish
        self.analyze = analyze
        self.attachments = attachments
        self.web = web
        self.vision = vision

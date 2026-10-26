"""Port of src/hooks/chat/dynamicContext.ts — the ledger block and the volatile tail."""
from typing import Callable, Dict, List, Optional, Union

from .chapter_index import build_chapter_index
from .context_ledger import hash_content
from .diff import strip_diff_markup
from .llm_context import html_to_plain_text, truncate_with_notice
from .pending_changes import pending_changes, render_pending_changes

MAX_REFERENCE_DOC_CHARS = 20_000


def render_ledger_chapter(title: str, content: str, per_doc_chars: int) -> str:
    return f"--- DOCUMENT: {title} ---\n{truncate_with_notice(html_to_plain_text(content), per_doc_chars)}\n"


def ledger_block(doc: Dict, kind: str = "fresh", per_doc_chars: int = MAX_REFERENCE_DOC_CHARS) -> str:
    title = (f"{doc['title']} (UPDATED — this version replaces the earlier copy of this chapter above; disregard that one)"
             if kind == "update" else doc["title"])
    return render_ledger_chapter(title, strip_diff_markup(doc["content"]), per_doc_chars)


def build_ledger_messages(documents: List[Dict], ledger: List[Union[str, Dict]], per_doc_chars: Optional[int] = None,
                          opts: Optional[Dict] = None) -> List[Dict]:
    per_doc_chars = MAX_REFERENCE_DOC_CHARS if per_doc_chars is None else per_doc_chars
    opts = opts or {}
    if not ledger:
        return []
    by_id = {d["id"]: d for d in documents}
    parts = []
    for item in ledger:
        entry = {"id": item} if isinstance(item, str) else item
        if entry.get("text"):
            parts.append(entry["text"])
            continue
        doc = by_id.get(entry["id"])
        parts.append(ledger_block(doc, "fresh", per_doc_chars) if doc else "")
    chapters = "\n".join(p for p in parts if p)
    if not chapters:
        return []
    header = ("REFERENCED CHAPTERS (full text, as plain text, for details and consistency; to change one, read its HTML with read and name it in your write):"
              if opts.get("agentTools") else
              "REFERENCED CHAPTERS (read-only; use them for details and consistency, never edit them):")
    return [
        {"role": "user", "content": f"{header}\n\n{chapters}", "cacheHint": True},
        {"role": "assistant", "content": "Understood. I have read these chapters and will use them as reference."},
    ]


def build_volatile_tail(documents: List[Dict], active_document_id: Optional[str], selected_text: str,
                        preserve_images: Callable[[str], str], opts: Optional[Dict] = None) -> str:
    opts = opts or {}
    chapter_index = (opts["indexOverride"] if opts.get("indexOverride") is not None else
                     build_chapter_index(documents, active_document_id, {"agentTools": opts.get("agentTools"), "markers": opts.get("markers")}))
    index_block = (f"{chapter_index}\n\n" if chapter_index else "") + (f"{opts['pinnedBlock']}\n\n" if opts.get("pinnedBlock") else "")
    active = next((d for d in documents if d["id"] == active_document_id), None)
    active_content = (active or {}).get("content") or ""
    clean_active = preserve_images(strip_diff_markup(active_content))
    pending = render_pending_changes(pending_changes(active_content))
    pending_block = f"\n\n{pending}" if pending else ""
    if selected_text:
        clean_selected = preserve_images(selected_text)
        return ("I have selected the following text in the document. I want you to focus your action on this specific text.\n"
                f"{index_block}\nCURRENT SELECTED TEXT:\n\"\"\"\n{clean_selected}\n\"\"\"\n\n"
                f"CURRENT ACTIVE DOCUMENT CONTENT (For context):\n\"\"\"\n{clean_active}\n\"\"\"{pending_block}")
    if opts.get("activeOverride") is not None:
        return f"Here is the current state of my document.\n{index_block}\n{opts['activeOverride']}{pending_block}"
    label = ("Your writes change this chapter unless you name another" if opts.get("agentTools")
             else "This is the ONLY document you can update")
    if opts.get("agentTools") and opts.get("activeCopyOlder"):
        label += ". Its copy under REFERENCED CHAPTERS is older: this is its current text"
    return (f"Here is the current state of my document.\n{index_block}\n"
            f"CURRENT ACTIVE DOCUMENT CONTENT ({label}):\n\"\"\"\n{clean_active}\n\"\"\"{pending_block}")


# ── Context as differences (cache_continuity.md §3.2, §3.3) ─────────────────

def diff_tail_parts(full: Dict, previous: Optional[Dict], present: List[str], turn: str) -> Dict:
    """Port of diffTailParts: each part in full, or one line when the copy the model has is replayed."""
    previous = previous or {}

    def has(p: Optional[Dict], h: str) -> bool:
        return bool(p) and p["hash"] == h and p["turn"] in present

    sent: Dict = {}
    index = full["index"]
    if full["index"]:
        h = hash_content(full["index"])
        if has(previous.get("index"), h):
            index = "CHAPTER INDEX: unchanged since your last turn (the copy earlier in this conversation is current)."
            sent["index"] = previous["index"]
        else:
            sent["index"] = {"hash": h, "turn": turn}
    attachments = full["attachments"]
    if full["attachments"]:
        h = hash_content(full["attachments"])
        if has(previous.get("attachments"), h):
            attachments = "ATTACHMENTS: unchanged since your last turn (the list earlier in this conversation is current)."
            sent["attachments"] = previous["attachments"]
        else:
            sent["attachments"] = {"hash": h, "turn": turn}
    active = None
    a = full.get("active")
    if a:
        prev = previous.get("active")
        if prev and prev.get("id") == a["id"] and has(prev, a["hash"]):
            active = (f'CURRENT ACTIVE DOCUMENT: #{a["number"]} "{a["title"]}" — unchanged since your last turn; its text is earlier '
                      "in this conversation. Your writes change this chapter unless you name another.")
            sent["active"] = prev
        else:
            sent["active"] = {"id": a["id"], "hash": a["hash"], "turn": turn}
    return {"index": index, "attachments": attachments, "active": active, "sent": sent}


def pinned_updates(pins: List[Dict], ledger: List[Dict], previous: Optional[Dict], present: List[str], turn: str,
                   names: Dict[str, str], keep: List[str]) -> Dict:
    """Port of pinnedUpdates: pinned chapters while the ledger is frozen."""
    previous = previous or {}
    sent: Dict = {}
    blocks: List[str] = []
    pinned_ids = {p["id"] for p in pins}

    def ledger_hash(doc_id: str) -> Optional[str]:
        return next((e["hash"] for e in reversed(ledger) if e["id"] == doc_id), None)

    def live(doc_id: str) -> Optional[Dict]:
        p = previous.get(doc_id)
        return p if p and p["turn"] in present else None

    for p in pins:
        prev = live(p["id"])
        if prev and prev["hash"] != "unpinned":
            if prev["hash"] == p["hash"]:
                sent[p["id"]] = prev
                continue
        elif not prev and ledger_hash(p["id"]) == p["hash"]:
            continue
        known = ledger_hash(p["id"]) is not None or (prev is not None and prev["hash"] != "unpinned")
        blocks.append(ledger_block({"id": p["id"], "title": f"#{p['number']} {p['title']}", "content": p["content"]}, "update" if known else "fresh"))
        sent[p["id"]] = {"hash": p["hash"], "turn": turn}
    candidates = [i for i in dict.fromkeys([*(e["id"] for e in ledger), *previous.keys()]) if i not in pinned_ids and i not in keep]
    dropped: List[str] = []
    for doc_id in candidates:
        prev = live(doc_id)
        if prev and prev["hash"] == "unpinned":
            sent[doc_id] = prev
            continue
        if not prev and ledger_hash(doc_id) is None:
            continue
        dropped.append(doc_id)
        sent[doc_id] = {"hash": "unpinned", "turn": turn}
    parts = ([f"PINNED CHAPTERS — changed or pinned since the copy you have (each replaces any earlier copy):\n\n{chr(10).join(blocks)}"]
             if blocks else [])
    if dropped:
        parts.append(f"No longer pinned — disregard the earlier copies of: {', '.join(names.get(i, i) for i in dropped)} (read them again if you need them).")
    return {"block": "\n\n".join(parts), "sent": sent}

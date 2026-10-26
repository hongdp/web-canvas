"""Port of src/agent/freshness.ts — what the model has been shown of each chapter."""
from typing import Dict, Iterable, List, Optional

from .context_ledger import hash_content
from .diff import strip_diff_markup

IN_CONTEXT = "in context"
IN_CONTEXT_CHANGED = "in context — CHANGED since you last saw it; the text here replaces the earlier one"
CHANGED = "changed since you read it"
NOT_IN_CONTEXT = "read earlier, not in context"


def accepted_hash(html: str) -> str:
    return hash_content(strip_diff_markup(html))


def record_seen(seen: Dict[str, Dict], doc_id: str, html: str, turn: int) -> None:
    seen[doc_id] = {"hash": accepted_hash(html), "turn": turn}


def freshness_markers(documents: List[Dict], active_document_id: Optional[str], in_context_ids: Iterable[str],
                      seen: Dict[str, Dict], turn: int) -> Dict[str, str]:
    in_context = set(in_context_ids)
    markers: Dict[str, str] = {}
    for doc in documents:
        if doc["id"] == active_document_id:
            continue
        prev = seen.get(doc["id"])
        unloaded = doc.get("contentLoaded") is False and not doc.get("content")
        if doc["id"] in in_context:
            changed = prev is not None and not unloaded and prev["hash"] != accepted_hash(doc.get("content", ""))
            markers[doc["id"]] = IN_CONTEXT_CHANGED if changed else IN_CONTEXT
            continue
        if not prev:
            continue
        if unloaded:
            markers[doc["id"]] = NOT_IN_CONTEXT
            continue
        markers[doc["id"]] = NOT_IN_CONTEXT if prev["hash"] == accepted_hash(doc.get("content", "")) else CHANGED
    for doc in documents:
        if doc["id"] == active_document_id or doc["id"] in in_context:
            record_seen(seen, doc["id"], doc.get("content", ""), turn)
    return markers

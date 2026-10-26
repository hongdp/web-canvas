"""Port of src/utils/contextSelection.ts — the LLM-free chapter prefetch scorer."""
import re
from typing import Dict, List, Optional

from .llm_context import detect_referenced_doc_ids

DEFAULT_SELECTION_OPTIONS = {"maxTotalChars": 60_000, "perDocChars": 20_000, "scoreThreshold": 40}
SCORE_TITLE_IN_PROMPT = 100
SCORE_TITLE_IN_HISTORY = 60
SCORE_ADJACENT = 40
SCORE_PREVIOUS_TURN = 30
SCORE_READ_BY_MODEL = 60
SCORE_KEYWORD_MAX = 50
SCORE_PER_KEYWORD_HIT = 10
HISTORY_TAIL_MESSAGES = 4
LATIN_STOPWORDS = {"this", "that", "with", "from", "have", "what", "when", "where", "which", "about", "chapter", "please",
                   "write", "make", "more", "them", "they", "will", "would", "could", "should", "into", "your", "their"}


def extract_keywords(text: str, cap: int = 80) -> List[str]:
    keywords: Dict[str, None] = {}
    lower = text.lower()
    for m in re.finditer(r"[a-z0-9]{4,}", lower):
        if m.group(0) not in LATIN_STOPWORDS:
            keywords[m.group(0)] = None
        if len(keywords) >= cap:
            return list(keywords)
    for run in re.finditer(r"[一-鿿぀-ヿ]{2,}", lower):
        chars = run.group(0)
        for i in range(len(chars) - 1):
            keywords[chars[i:i + 2]] = None
            if len(keywords) >= cap:
                return list(keywords)
    return list(keywords)


def select_reference_chapters(inp: Dict, options: Optional[Dict] = None) -> Dict:
    opts = {**DEFAULT_SELECTION_OPTIONS, **(options or {})}
    max_total, per_doc, threshold = opts["maxTotalChars"], opts["perDocChars"], opts["scoreThreshold"]
    documents: List[Dict] = inp["documents"]
    active_id = inp.get("activeDocumentId")
    previous = inp.get("previousAttachedIds") or []
    model_read = inp.get("modelReadIds") or []
    ledger_ids = inp.get("ledgerIds") or []

    candidates = [d for d in documents if d["id"] != active_id]
    attachable = lambda d: d.get("contentLoaded") is not False and len(d.get("content", "")) > 0  # noqa: E731
    history_text = "\n".join((inp.get("recentHistory") or [])[-HISTORY_TAIL_MESSAGES:])
    in_prompt = set(detect_referenced_doc_ids(inp["promptText"], candidates, active_id))
    in_history = set(detect_referenced_doc_ids(history_text, candidates, active_id))
    active_idx = next((i for i, d in enumerate(documents) if d["id"] == active_id), -1)
    adjacent = set()
    if active_idx != -1:
        if active_idx - 1 >= 0:
            adjacent.add(documents[active_idx - 1]["id"])
        if active_idx + 1 < len(documents):
            adjacent.add(documents[active_idx + 1]["id"])
    keywords = extract_keywords(inp["promptText"])

    scores: Dict[str, int] = {}
    for doc in candidates:
        score = 0
        if doc["id"] in in_prompt:
            score += SCORE_TITLE_IN_PROMPT
        if doc["id"] in in_history:
            score += SCORE_TITLE_IN_HISTORY
        if doc["id"] in adjacent:
            score += SCORE_ADJACENT
        if doc["id"] not in ledger_ids and doc["id"] in previous:
            score += SCORE_PREVIOUS_TURN
        if doc["id"] not in ledger_ids and doc["id"] in model_read:
            score += SCORE_READ_BY_MODEL
        if keywords:
            digest = doc["title"].lower()  # titles only: chapter summaries are no longer used
            hits = sum(1 for kw in keywords if kw in digest)
            score += min(SCORE_KEYWORD_MAX, hits * SCORE_PER_KEYWORD_HIT)
        scores[doc["id"]] = score

    kept_ledger = [i for i in ledger_ids if i != active_id]
    used = 0
    auto_candidates = sorted(
        [d for d in candidates if attachable(d) and d["id"] not in kept_ledger and scores[d["id"]] >= threshold],
        key=lambda d: -scores[d["id"]])
    auto_ids: List[str] = []
    dropped: List[str] = []
    for doc in auto_candidates:
        cost = min(len(doc["content"]), per_doc)
        if used + cost <= max_total:
            auto_ids.append(doc["id"]); used += cost
        else:
            dropped.append(doc["id"])
    return {"attachedIds": [*kept_ledger, *auto_ids], "autoIds": auto_ids, "droppedForBudget": dropped,
            "scores": scores, "estimatedChars": used}


#: Most chapter text the pinned chapters may bring into a turn (pinned_context.md §2).
PINNED_CONTEXT_CHARS = 60_000


def pinned_context_ids(documents: List[Dict], active_document_id: Optional[str], max_chars: int = PINNED_CONTEXT_CHARS) -> List[str]:
    """Port of pinnedContextIds: the pinned chapters in book order, the active one excluded, within the budget."""
    out: List[str] = []
    used = 0
    for d in documents:
        if not d.get("pinned") or d["id"] == active_document_id:
            continue
        if used + d["chars"] > max_chars:
            continue
        out.append(d["id"])
        used += d["chars"]
    return out

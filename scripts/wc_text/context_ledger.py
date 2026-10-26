"""Port of src/utils/contextLedger.ts — the append-only ledger planner."""
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

MAX_SAFE_INTEGER = 9007199254740991
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


def hash_content(text: str) -> str:
    """FNV-1a over UTF-16 code units, as JavaScript's charCodeAt walks a string."""
    h = 0x811C9DC5
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        h = ((h ^ (units[i] | (units[i + 1] << 8))) * 0x01000193) & 0xFFFFFFFF
    return f"{_to_base36(h)}-{len(units) // 2}"


def plan_ledger_turn(current: Dict, desired_ids: List[str], docs: List[Dict], active_document_id: Optional[str],
                     options: Optional[Dict] = None) -> Dict:
    options = options or {}
    by_id = {d["id"]: d for d in docs}
    desired = set(desired_ids)
    keep = set(options.get("keepIds") or [])
    render: Optional[Callable[[str, str], str]] = options.get("render")
    entries: List[Dict] = current.get("entries", [])

    def drop_reason(entry: Dict) -> Optional[str]:
        if entry["id"] in keep and entry["id"] in by_id:
            return None
        if entry["id"] == active_document_id:
            return "now-active"
        doc = by_id.get(entry["id"])
        if not doc:
            return "user-removed"
        if entry["id"] not in desired:
            return "user-removed"
        if entry.get("stale"):
            return None
        if doc["hash"] != entry["hash"]:
            return "edited"
        return None

    def chars_from(i: int) -> int:
        return sum(e["chars"] for e in entries[i:])

    cut = len(entries)
    updates: List[int] = []
    for i, entry in enumerate(entries):
        reason = drop_reason(entry)
        if reason is None:
            continue
        if reason == "edited" and render and chars_from(i + 1) > (by_id.get(entry["id"], {}).get("chars") or 0):
            updates.append(i)
            continue
        cut = i
        break

    ledger_chars = chars_from(0)
    budget = options["maxStaleChars"] if options.get("maxStaleChars") is not None else max(20_000, int(ledger_chars * 0.3))

    def outdated(i: int) -> bool:
        return bool(entries[i].get("stale")) or i in updates

    def stale_before(limit: int) -> int:
        return sum(e["chars"] for i, e in enumerate(entries[:limit]) if outdated(i))

    if stale_before(cut) > budget:
        first = next((i for i in range(len(entries)) if outdated(i)), -1)
        if first != -1 and first < cut:
            cut = first
    active_updates = [i for i in updates if i < cut]

    kept = [({**e, "stale": True} if i in active_updates else e) for i, e in enumerate(entries[:cut])]
    cached_prefix_chars = sum(e["chars"] for e in kept)
    kept_stale_ids = {e["id"] for e in kept if e.get("stale")}

    def entry_for(doc_id: str, doc: Dict) -> Dict:
        kind = "update" if doc_id in kept_stale_ids else "fresh"
        if render:
            return {"id": doc_id, "hash": doc["hash"], "chars": doc["chars"], "text": render(doc_id, kind)}
        return {"id": doc_id, "hash": doc["hash"], "chars": doc["chars"]}

    drops: List[Dict] = []
    resent_ids: List[str] = []
    tail: List[Dict] = []
    for entry in entries[cut:]:
        if entry.get("stale"):
            continue
        reason = drop_reason(entry)
        if reason is not None:
            drops.append({"id": entry["id"], "reason": reason})
            continue
        tail.append(entry_for(entry["id"], by_id[entry["id"]]))
        resent_ids.append(entry["id"])

    present = {e["id"] for e in kept if not e.get("stale")} | {e["id"] for e in tail}
    updated_ids: List[str] = []
    for i in active_updates:
        doc_id = entries[i]["id"]
        if doc_id in present:
            continue
        tail.append(entry_for(doc_id, by_id[doc_id]))
        updated_ids.append(doc_id)
        present.add(doc_id)
    appended_ids: List[str] = []
    for doc_id in desired_ids:
        if doc_id in present or doc_id == active_document_id:
            continue
        doc = by_id.get(doc_id)
        if not doc:
            continue
        tail.append(entry_for(doc_id, doc))
        appended_ids.append(doc_id)
        present.add(doc_id)

    all_entries = kept + tail
    return {
        "ledger": {"entries": all_entries},
        "cachedPrefixCount": len(kept),
        "cachedPrefixChars": cached_prefix_chars,
        "resentIds": resent_ids,
        "appendedIds": appended_ids,
        "drops": drops,
        "resendChars": sum(e["chars"] for e in tail),
        "updatedIds": updated_ids,
        "staleChars": sum(e["chars"] for e in all_entries if e.get("stale")),
    }


def ledger_chapter_ids(ledger: Dict) -> List[str]:
    return list(dict.fromkeys(e["id"] for e in ledger.get("entries", [])))


def _parse_ms(raw: Optional[str]) -> int:
    """Date.parse of an ISO timestamp in milliseconds; 0 when missing or unparseable."""
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def order_admissions_by_stability(ids: List[str], docs: List[Dict], book_order: List[str], active_document_id: Optional[str]) -> List[str]:
    by_id = {d["id"]: d for d in docs}
    active_idx = book_order.index(active_document_id) if active_document_id in book_order else -1

    def distance(doc_id: str) -> int:
        idx = book_order.index(doc_id) if doc_id in book_order else -1
        if idx == -1 or active_idx == -1:
            return MAX_SAFE_INTEGER
        return abs(idx - active_idx)

    return sorted(ids, key=lambda i: (_parse_ms(by_id.get(i, {}).get("updatedAt")), -distance(i)))

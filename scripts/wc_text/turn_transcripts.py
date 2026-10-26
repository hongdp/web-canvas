"""Port of src/utils/turnTranscripts.ts — earlier turns replayed as they were sent (cache_continuity.md §3.1)."""
from typing import Any, Dict, List, Optional

from .llm_context import trim_history_for_context

_NO_CAP = 2 ** 53 - 1  # Number.MAX_SAFE_INTEGER


def plan_history_units(entries: List[Dict[str, Any]], transcripts: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Which entries are replayed from a transcript: a user message followed by the reply whose transcript names it."""
    units: List[Dict[str, Any]] = []
    i = 0
    while i < len(entries):
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        t = transcripts.get(nxt["id"]) if nxt else None
        if (entries[i]["role"] == "user" and nxt is not None and nxt["role"] == "assistant" and t
                and t.get("userMessageId") == entries[i]["id"] and t.get("messages")):
            units.append({"start": i, "count": 2, "turn": nxt["id"]})
            i += 2
            continue
        units.append({"start": i, "count": 1, "turn": None})
        i += 1
    return units


def transcript_weight(messages: List[Dict[str, Any]]) -> int:
    """The chars a transcript costs: every message's text and its tool calls' argument bytes."""
    total = 0
    for m in messages:
        total += len(m.get("content") or "")
        for call in m.get("toolCalls") or []:
            total += len(call.get("argumentsText") or "")
    return total


def summarizable_history(entries: List[Dict[str, Any]], units: List[Dict[str, Any]],
                         transcripts: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The history as the summary planner counts it: a replayed turn weighs what its transcript sends."""
    out = [{"id": m["id"], "role": m["role"], "content": m["content"], **({"images": m["images"]} if m.get("images") else {})}
           for m in entries]
    for u in units:
        if u["turn"] is None:
            continue
        messages = transcripts[u["turn"]]["messages"]
        total = transcript_weight(messages)
        last = messages[-1]
        reply = min(total, len(last.get("content") or "")) if last.get("role") == "assistant" else 0
        out[u["start"]] = {**out[u["start"]], "weight": max(1, total - reply)}
        out[u["start"] + 1] = {**out[u["start"] + 1], "weight": reply}
    return out


def _unit_weight(entries: List[Dict[str, Any]], u: Dict[str, Any], transcripts: Dict[str, Dict[str, Any]]) -> int:
    if u["turn"] is not None:
        return transcript_weight(transcripts[u["turn"]]["messages"])
    return len(entries[u["start"]]["content"])


def _without_id(m: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in m.items() if k != "id"}


def history_window(entries: List[Dict[str, Any]], units: List[Dict[str, Any]], transcripts: Dict[str, Dict[str, Any]],
                   start: int, max_chars: Optional[int]) -> Dict[str, Any]:
    """The history messages from entry `start` on, within `max_chars` (None = no cap); see historyWindow."""
    kept = [u for u in units if u["start"] >= start]
    if not any(u["turn"] is not None for u in kept):
        return {"messages": trim_history_for_context([_without_id(m) for m in entries[start:]],
                                                     {"maxChars": _NO_CAP if max_chars is None else max_chars}),
                "present": []}
    if max_chars is not None:
        used = count = 0
        first = len(kept)
        for i in range(len(kept) - 1, -1, -1):
            w = _unit_weight(entries, kept[i], transcripts)
            if count >= 2 and used + w > max_chars:
                break
            used += w
            count += kept[i]["count"]
            first = i
        kept = kept[first:]
    messages: List[Dict[str, Any]] = []
    present: List[str] = []
    pending: List[Dict[str, Any]] = []

    def flush() -> None:
        if pending:
            messages.extend(trim_history_for_context(list(pending), {"maxChars": _NO_CAP, "minKeepMessages": len(pending)}))
            pending.clear()

    for u in kept:
        if u["turn"] is None:
            pending.append(_without_id(entries[u["start"]]))
            continue
        flush()
        present.append(u["turn"])
        for m in transcripts[u["turn"]]["messages"]:
            messages.append({k: v for k, v in m.items() if k != "cacheHint"})
    flush()
    while messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    return {"messages": messages, "present": present}

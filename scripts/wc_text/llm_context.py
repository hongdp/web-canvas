"""Port of src/utils/llmContext.ts — plain text, display artifacts, references, the history budget."""
import re
from typing import Iterable, List, Optional, Sequence


def html_to_plain_text(html: str) -> str:
    """htmlToPlainText: block boundaries become newlines, entities decoded."""
    out = re.sub(r"<(?:br|hr)\s*/?>", "\n", html, flags=re.I)
    out = re.sub(r"</(?:p|h[1-6]|li|blockquote|div|tr|pre)>", "\n", out, flags=re.I)
    out = re.sub(r"<li\b[^>]*>", "- ", out, flags=re.I)
    out = re.sub(r"<[^>]*>?", "", out, flags=re.M)
    out = out.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
    out = out.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return _js_trim(out)


from .jsstr import js_trim as _js_trim  # noqa: E402 — the ports import the trim from here


def strip_chat_display_artifacts(content: str) -> str:
    """stripChatDisplayArtifacts: UI-only prefixes and trailing status notes removed."""
    out = re.sub(r"^(?:\[Attached Context: [^\]\n]*\]\n?)+\n*", "", content)
    out = re.sub(
        r"(?:^|\n+)⚠️ (?:Error(?: during stream)?:|The response was cut off|The response abbreviated|The model answered without|[0-9]+ suggested changes? could not be located|This reply also contained [0-9]+ document changes?)[\s\S]*$",
        "", out)
    out = re.sub(r"^[📚🔁] [^\n]*\n?", "", out, flags=re.M)
    return _js_trim(out)


def detect_referenced_doc_ids(prompt_text: str, documents: Sequence[dict], active_document_id: Optional[str]) -> List[str]:
    prompt = prompt_text.lower()
    ids: List[str] = []
    for doc in documents:
        if doc["id"] == active_document_id:
            continue
        title = _js_trim(doc["title"]).lower()
        clean = _js_trim(re.sub(r"chapter\s*[0-9]+\s*:\s*", "", title))
        if (len(title) > 1 and title in prompt) or (len(clean) > 3 and clean in prompt):
            ids.append(doc["id"])
    return ids


def build_attachments_label(ids: Iterable[str], documents: Sequence[dict], auto_ids: Sequence[str] = ()) -> str:
    by_id = {d["id"]: d for d in documents}
    lines = []
    for doc_id in ids:
        doc = by_id.get(doc_id)
        if not doc:
            continue
        lines.append(f"[Attached Context: {doc['title']}{' (auto)' if doc_id in auto_ids else ''}]")
    return "\n".join(lines)


def truncate_with_notice(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n…[truncated: showing first {max_chars} of {len(text)} characters]"


IMAGE_PLACEHOLDER_TEXT = "[image sent earlier in this conversation]"
_STOPPED_RE = re.compile(r"⏹️ Stopped\b")


def was_turn_interrupted(history: List[dict]) -> bool:
    """wasTurnInterrupted: the last assistant message's record says stopped, or its text carries the Stop note."""
    for m in reversed(history):
        if m.get("role") != "assistant":
            continue
        agent = m.get("agent") or {}
        return agent.get("status") == "stopped" or bool(_STOPPED_RE.search(m.get("content") or ""))
    return False


def trim_history_for_context(messages: List[dict], options: dict) -> List[dict]:
    """trimHistoryForContext: normalize the whole history (drop empties, merge
    same-role runs, settle images), then cut whole messages from the front."""
    max_chars = options["maxChars"]
    min_keep = options.get("minKeepMessages", 2)
    keep_images = options.get("keepImages", False)

    normalized: List[dict] = []
    for msg in messages:
        if not _js_trim(msg["content"]) and not msg.get("images"):
            continue
        carried = dict(msg)
        if not keep_images and "images" in carried:
            del carried["images"]
            if not _js_trim(carried["content"]):
                carried["content"] = IMAGE_PLACEHOLDER_TEXT
        prev = normalized[-1] if normalized else None
        if prev and prev["role"] == carried["role"]:
            prev["content"] = _js_trim(f"{prev['content']}\n\n{carried['content']}")
            if carried.get("images"):
                prev["images"] = [*(prev.get("images") or []), *carried["images"]]
            if carried.get("responseItems"):
                prev["responseItems"] = [*(prev.get("responseItems") or []), *carried["responseItems"]]
            continue
        normalized.append(carried)

    kept: List[dict] = []
    used = 0
    for msg in reversed(normalized):
        must_keep = len(kept) < min_keep
        if not must_keep and used + len(msg["content"]) > max_chars:
            break
        used += len(msg["content"])
        kept.insert(0, msg)

    while kept and kept[0]["role"] == "assistant":
        kept.pop(0)
    return kept

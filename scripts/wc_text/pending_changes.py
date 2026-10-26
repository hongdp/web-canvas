"""Port of src/utils/pendingChanges.ts — the review diff as the model reads it."""
import re
from typing import Dict, List

from .diff_resolution import resolve_diff_markup_in_html
from .paragraphs import block_text, top_level_blocks

_MARKUP_RE = re.compile(r"diff-addition|diff-deletion")
MAX_TEXT_CHARS = 1500
MAX_LIST_CHARS = 8000


def pending_changes(html: str) -> List[Dict[str, str]]:
    if not html or not _MARKUP_RE.search(html):
        return []
    out: List[Dict[str, str]] = []
    for block in top_level_blocks(html):
        if not _MARKUP_RE.search(block):
            continue
        was = block_text(resolve_diff_markup_in_html(block, "reject"))
        now = block_text(resolve_diff_markup_in_html(block, "accept"))
        if was != now:
            out.append({"was": was, "now": now})
    return out


def _quote(text: str) -> str:
    if not text:
        return "(none)"
    flat = re.sub(r"\n+", " / ", text)
    return f'"{flat[:MAX_TEXT_CHARS]}…" (cut)' if len(flat) > MAX_TEXT_CHARS else f'"{flat}"'


def render_pending_changes(changes: List[Dict[str, str]]) -> str:
    if not changes:
        return ""
    lines: List[str] = []
    used = listed = 0
    for c in changes:
        entry = (f"{listed + 1}. now: {_quote(c['now']) if c['now'] else '(removed by the change)'}\n"
                 f"   was: {_quote(c['was']) if c['was'] else '(added by the change)'}")
        if listed > 0 and used + len(entry) > MAX_LIST_CHARS:
            break
        lines.append(entry); used += len(entry); listed += 1
    if listed < len(changes):
        lines.append(f"({len(changes) - listed} more pending change(s) not listed)")
    return ("PENDING CHANGES IN THIS CHAPTER — earlier edits the user has NOT accepted yet. The content above shows them as if accepted; the user can still accept or reject each one.\n"
            + "\n".join(lines)
            + '\nIf the user wants part of the earlier version back, change ONLY that part: replace its "now" text with its "was" text, and leave every other pending change as it is.')

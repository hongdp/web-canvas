"""Port of src/hooks/chat/streamHandlers.ts (the pure parts) and the run's fixed notes."""
import re
from typing import Dict, Optional

from .jsstr import js_trim
from .text import chapter_attribute, new_chapter_attribute, strip_doc_status

ASSISTANT_PLACEHOLDER = "Thinking..."
INTERRUPTED_NOTICE = "⚠️ Interrupted before the model replied (the page reloaded). Send again to retry."
RECONNECT_FAILED_NOTICE = "⚠️ Could not reconnect to this generation — it may still be running on the server. Reload the page to try again."
MAX_NO_ACTION_RETRIES = 3
NO_ACTION_RETRY_INSTRUCTION = """Your previous reply did not follow the output protocol, so NOTHING reached the document — the user saw only your message.

Redo this turn. Exactly one of these two shapes is acceptable:
- You are changing the document: emit the change inside <canvas>, <edit> or <selection_replace>, with the full content (no summaries, no "as above"), and end with <doc_status>updated</doc_status>.
- You are NOT changing the document: answer normally, say what you need from the user, and end with <doc_status>unchanged</doc_status>. This is a perfectly good answer — but you may not say or imply that you edited anything.

The <doc_status> line is required either way, and it must match what you actually emitted."""
# src/agent/run.ts STEP_LIMIT_NOTE
STEP_LIMIT_NOTE = "This turn has used its step budget. Do not call any more tools: answer the user now with what you have, and say what is left undone."

_MARKUP_START_RE = re.compile(r"<edits?\b|<{5,}\s*SEARCH|<canvas\b|<selection_replace>", re.I)
_CANVAS_OPEN_RE = re.compile(r"<canvas\b[^>]*>", re.I)
_EDIT_START_RE = re.compile(r"<edit\b|<{5,}\s*SEARCH", re.I)


def _chat_part(text: str) -> str:
    m = _MARKUP_START_RE.search(text)
    return js_trim(text if not m else text[:m.start()])


def split_streaming_response(raw: str) -> Dict:
    canvas_text = ""
    canvas_chapter: Optional[str] = None
    canvas_new_chapter: Optional[str] = None
    selection_text = ""
    is_selection = False
    canvas_end = "</canvas>"
    sel_start = "<selection_replace>"
    sel_end = "</selection_replace>"

    canvas_open = _CANVAS_OPEN_RE.search(raw)
    canvas_idx = canvas_open.start() if canvas_open else -1
    selection_idx = raw.find(sel_start)
    edit_m = _EDIT_START_RE.search(raw)
    edit_idx = edit_m.start() if edit_m else -1

    if selection_idx != -1:
        is_selection = True
        chat = _chat_part(raw[:selection_idx])
        rest = raw[selection_idx + len(sel_start):]
        end = rest.find(sel_end)
        if end != -1:
            selection_text = rest[:end]
            chat += "\n\n" + _chat_part(rest[end + len(sel_end):])
        else:
            selection_text = rest
    elif edit_idx != -1 and (canvas_idx == -1 or edit_idx < canvas_idx):
        chat = js_trim(raw[:edit_idx])
    elif canvas_idx != -1:
        chat = js_trim(raw[:canvas_idx])
        canvas_chapter = chapter_attribute(canvas_open.group(0))
        canvas_new_chapter = new_chapter_attribute(canvas_open.group(0))
        rest = raw[canvas_idx + len(canvas_open.group(0)):]
        end = rest.find(canvas_end)
        if end != -1:
            canvas_text = rest[:end]
            chat += "\n\n" + _chat_part(rest[end + len(canvas_end):])
        else:
            canvas_text = rest
    else:
        chat = _chat_part(raw) if _MARKUP_START_RE.search(raw) else raw

    out: Dict = {"chatText": strip_doc_status(chat), "canvasText": canvas_text}
    # Absent, not null, when no canvas tag opened: an `undefined` has no JSON form.
    if canvas_chapter is not None:
        out["canvasChapter"] = canvas_chapter
    if canvas_new_chapter is not None:
        out["canvasNewChapter"] = canvas_new_chapter
    out.update({"selectionReplaceText": selection_text, "isSelectionEdit": is_selection})
    return out


def build_completion_warnings(params: Dict) -> str:
    canvas_issue = params.get("canvasIssue")
    edit_failed = params.get("editFailedCount") or 0
    exhausted = bool(params.get("exhaustedNoActionRetries"))
    selection_gone = bool(params.get("selectionGone"))
    unretriable = bool(params.get("unretriableFailedUpdate"))
    produced_nothing = bool(params.get("toolCallProducedNothing"))
    stray = params.get("strayMarkup") or 0
    reinserted = params.get("reinsertedImages") or 0

    note = ("\n\n⚠️ The response was cut off before the document update finished, so no changes were applied (your document is unchanged). Please retry — for long documents, try editing a smaller selection at a time."
            if canvas_issue == "truncated" else
            "\n\n⚠️ The response abbreviated unchanged parts of the document, so applying it would have deleted content. No changes were applied. Please retry — for long documents, try editing a smaller selection at a time."
            if canvas_issue == "elided" else "")
    if edit_failed > 0:
        note += (f"\n\n⚠️ {edit_failed} suggested change{'s' if edit_failed > 1 else ''} could not be located in the current document and "
                 f"{'were' if edit_failed > 1 else 'was'} skipped. The text to change may have moved or differ from what was matched.")
    if stray > 0 and not exhausted and not unretriable:
        many = stray > 1
        note += (f"\n\n⚠️ This reply also contained {stray} document change{'s' if many else ''} that could not be applied alongside the main one, so "
                 f"{'they were' if many else 'it was'} left out of this message. Ask again if {'they matter' if many else 'it matters'}.")
    if exhausted:
        note += (f"\n\n⚠️ The model never produced a valid document update or a clear \"no change\" declaration (retried {MAX_NO_ACTION_RETRIES} times), "
                 "so your document is unchanged. Ask again — naming the chapter or section usually helps.")
    if produced_nothing:
        note += "\n\n⚠️ The model started a document change but its request could not be used (empty or malformed arguments), so your document is unchanged. Try again — a shorter, more specific instruction usually helps."
    if unretriable:
        note += "\n\n⚠️ This reply produced no usable document update, and it was resumed after a reload — so there is no request left to retry with. Your document is unchanged; send the instruction again to have another go."
    if selection_gone:
        note += "\n\n⚠️ The text you had selected is no longer where it was (the chapter changed while this ran), so nothing was written. The rewrite is above — paste it where you want it, or select the text again and retry."
    if reinserted > 0:
        many = reinserted > 1
        note += (f"\n\nℹ️ {reinserted} image{'s' if many else ''} missing from the rewrite {'were' if many else 'was'} restored near "
                 f"{'their' if many else 'its'} original position. Delete {'them' if many else 'it'} manually if the removal was intended.")
    return note

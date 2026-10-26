"""Port of src/agent/tools/documentWrites.ts — update_document, edit_document,
replace_selection and rename_chapter, over the server's document port.

Two things differ from the client, by construction: there is no editor, so a
selection rewrite is always placed by its text in the stored chapter (the
client's "on screen" path does not exist here), and edits beside a selection
read the stored chapter rather than an editor.
"""
import math
import re
from typing import Any, Dict, List, Optional, Union

from wc_text.chapters import cite_chapter, resolve_chapter
from wc_text.diff import diff_html, strip_diff_markup
from wc_text.diff_resolution import resolve_diff_markup_in_html
from wc_text.document_tools import DOCUMENT_TOOLS
from wc_text.edit_hints import nearest_hint
from wc_text.image_preservation import reinsert_missing_images
from wc_text.jsstr import js_trim
from wc_text.llm_context import html_to_plain_text
from wc_text.context_ledger import hash_content
from wc_text.text_replace import replace_in_html_text
from wc_text.paragraphs import (anchor_matches, apply_paragraph_edits, chapter_chars, is_text_rewritable_html, numbered_paragraph_spans, rewrite_loss,
                                rewrite_loss_note, strip_image_marker_paragraphs)
from wc_text.text import (apply_edit_blocks, apply_edit_blocks_locally, is_blank_content, strip_blank_paragraphs,
                          strip_incomplete_end_tag, trim_incomplete_html_tail, validate_canvas_replacement)
from wc_text.title_sync import content_with_renamed_heading, leading_h1_text
from wc_text.tool_call_stream import partial_string_argument

from ..registry import Tool
from ..types import DocState, ToolContext, result
from .plan import PLAN_DONE_PARAMETER, plan_done_arg, with_plan_done

Target = Dict[str, Any]  # {id, title, number, isStart}


def _schema_of(name: str) -> Dict[str, Any]:
    tool = next(t for t in DOCUMENT_TOOLS if t["name"] == name)
    # Every write may finish plan items (agentic_chat_loop.md §0.11).
    params = {**tool["parameters"], "properties": {**tool["parameters"]["properties"], "plan_done": PLAN_DONE_PARAMETER}}
    return {"name": tool["name"], "description": tool["description"], "parameters": params}


# ── Targets ──────────────────────────────────────────────────────────────────

def is_new_chapter_ref(ref: Any) -> bool:
    return isinstance(ref, dict) and isinstance(ref.get("create"), str) and js_trim(ref["create"]) != ""


def title_key(title: str) -> str:
    return re.sub(r"\s+", "", title)


_CHAPTER_MARKER_RE = re.compile(r"^(第[零〇一二三四五六七八九十百千两\d]+[章节回卷部集篇]|chapter\s*\d+|ch\.?\s*\d+|序章|楔子|尾声|后记)", re.I)
_PUNCT_RE = re.compile(r"[\s\W_]+", re.U)


def _loose_key(title: str) -> str:
    # JS: lowercased, whitespace/punctuation/symbols removed (\p{P}\p{S}).
    return "".join(ch for ch in title.lower() if not (ch.isspace() or _is_punct_or_symbol(ch)))


def _is_punct_or_symbol(ch: str) -> bool:
    import unicodedata
    return unicodedata.category(ch)[0] in ("P", "S")


def _bigrams(text: str) -> List[str]:
    return [text[i:i + 2] for i in range(len(text) - 1)]


def _title_similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0
    pool = list(y)
    shared = 0
    for g in x:
        if g in pool:
            shared += 1
            pool.remove(g)
    return 2 * shared / (len(x) + len(y))


def similar_chapters(title: str, exclude_id: str, chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    key = _loose_key(title)
    m = _CHAPTER_MARKER_RE.search(js_trim(title))
    marker = re.sub(r"\s+", "", m.group(1)).lower() if m else None
    out = []
    for i, c in enumerate(chapters):
        if c["id"] == exclude_id:
            continue
        other = _loose_key(c["title"])
        if not other or not key:
            continue
        om = _CHAPTER_MARKER_RE.search(js_trim(c["title"]))
        other_marker = re.sub(r"\s+", "", om.group(1)).lower() if om else None
        if marker is not None and other_marker is not None and marker != other_marker:
            continue
        similar = ((marker is not None and marker == other_marker)
                   or (min(len(key), len(other)) >= 2 and (other in key or key in other))
                   or _title_similarity(key, other) >= 0.6)
        if similar:
            out.append({"number": i + 1, "title": c["title"]})
    return out[:3]


def _open_for_writing(ctx: ToolContext, doc_id: str) -> None:
    selection_pending = ctx.selection.range() is not None and not ctx.run.selection_applied
    if not selection_pending and not ctx.document.user_moved() and ctx.document.open_id() != doc_id:
        ctx.document.open(doc_id)


def _claim_new_chapter(title: str, ctx: ToolContext) -> Union[Target, str]:
    chapters = ctx.document.chapters()
    for index, chapter in enumerate(chapters):
        if title_key(chapter["title"]) == title_key(title):
            if chapter["id"] not in ctx.run.created and chapter.get("loaded") is not False and is_blank_content(chapter["content"]):
                ctx.run.html_shown.add(chapter["id"])
                ctx.run.in_context.add(chapter["id"])
                ctx.run.known.setdefault(chapter["id"], chapter["content"])
                _open_for_writing(ctx, chapter["id"])
            return {"id": chapter["id"], "title": chapter["title"], "number": index + 1, "isStart": chapter["id"] == ctx.document.start_id}
    doc_id = ctx.document.create(js_trim(title))
    after = ctx.document.chapters()
    number = next((i for i, c in enumerate(after) if c["id"] == doc_id), -1) + 1
    ctx.run.created.add(doc_id)
    ctx.run.html_shown.add(doc_id)
    ctx.run.in_context.add(doc_id)
    ctx.run.known[doc_id] = after[number - 1]["content"] if number > 0 else ""
    ctx.run.touched[doc_id] = {"documentId": doc_id, "titleAtRun": js_trim(title), "kind": "created", "changes": 0, "failed": 0}
    _open_for_writing(ctx, doc_id)
    return {"id": doc_id, "title": js_trim(title), "number": number, "isStart": False}


def resolve_target(ref: Any, ctx: ToolContext) -> Union[Target, str]:
    if is_new_chapter_ref(ref):
        return _claim_new_chapter(ref["create"], ctx)
    chapters = ctx.document.chapters()
    if ref is None or ref == "":
        index = next((i for i, c in enumerate(chapters) if c["id"] == ctx.document.start_id), -1)
        if index == -1:
            return "The chapter this turn started on no longer exists."
        return {"id": chapters[index]["id"], "title": chapters[index]["title"], "number": index + 1, "isStart": True}
    resolved = resolve_chapter(ref, chapters)
    if isinstance(resolved, str):
        return resolved
    return {"id": resolved["id"], "title": resolved["title"], "number": resolved["number"], "isStart": resolved["id"] == ctx.document.start_id}


def _stored_content(ctx: ToolContext, doc_id: str) -> str:
    return next((c["content"] for c in ctx.document.chapters() if c["id"] == doc_id), "")


def _selection_turn(ctx: ToolContext) -> bool:
    return bool(ctx.selection.original_text())


def doc_state(ctx: ToolContext, target: Target) -> DocState:
    st = ctx.run.docs.get(target["id"])
    if st is None:
        original = ctx.document.original if target["isStart"] and _selection_turn(ctx) else _stored_content(ctx, target["id"])
        base = strip_diff_markup(original)
        review_base = base if base == original else resolve_diff_markup_in_html(original, "reject")
        st = DocState(original=original, base=base, review_base=review_base, html=base, dirty=False)
        ctx.run.docs[target["id"]] = st
        ctx.run.known[target["id"]] = original
    return st


def user_edited(ctx: ToolContext, doc_id: str) -> bool:
    if doc_id == ctx.document.start_id and _selection_turn(ctx):
        return False
    known = ctx.run.known.get(doc_id)
    if known is None:
        return False
    stored = _stored_content(ctx, doc_id)
    if stored == known:
        return False
    if not _review_resolved(known, stored):
        return True
    _adopt_reviewed(ctx, doc_id, stored)
    return False


def _accepted_text_key(html: str) -> str:
    return re.sub(r"\s+", "", html_to_plain_text(strip_diff_markup(html)))


_INLINE_TAG = re.compile(r"<(strong|b|em|i|u|s|code|a|mark)\b", re.IGNORECASE)
_INLINE_ALIAS = {"b": "strong", "i": "em"}


def _inline_key(html: str) -> str:
    return ",".join(
        _INLINE_ALIAS.get(m.group(1).lower(), m.group(1).lower())
        for m in _INLINE_TAG.finditer(strip_diff_markup(html))
    )


def _review_resolved(known: str, stored: str) -> bool:
    """Accepting a pending diff, or the editor re-spelling the same reading,
    is not an edit (documentWrites.ts reviewResolved): same text, same inline
    formatting."""
    return _accepted_text_key(known) == _accepted_text_key(stored) and _inline_key(known) == _inline_key(stored)


def _adopt_reviewed(ctx: ToolContext, doc_id: str, stored: str) -> None:
    ctx.run.known[doc_id] = stored
    st = ctx.run.docs.get(doc_id)
    if st is None:
        return
    st.original = stored
    st.base = strip_diff_markup(stored)
    st.review_base = stored if st.base == stored else resolve_diff_markup_in_html(stored, "reject")
    st.html = st.base


def forget_chapter(ctx: ToolContext, doc_id: str) -> None:
    ctx.run.docs.pop(doc_id, None)
    ctx.run.html_shown.discard(doc_id)
    ctx.run.known.pop(doc_id, None)


def edited_meanwhile(ctx: ToolContext, target: Target) -> Dict[str, Any]:
    forget_chapter(ctx, target["id"])
    return result(False,
                  f"The user edited {cite_chapter(target)} while you were working, so your copy of it is out of date and this change was NOT applied. "
                  f"Read it again (read with chapters=[{target['number']}] and format=\"html\") and make the change on its current text, keeping the user's edits.",
                  f"⚠️ {cite_chapter(target)} was edited by the user meanwhile — not written", retryable=True)


def commit_doc(ctx: ToolContext, target: Target, st: DocState) -> None:
    if st.dirty and not target["isStart"] and target["id"] not in ctx.run.created and target["id"] not in ctx.run.snapshotted:
        ctx.document.snapshot(target["id"], f'Auto-save before the assistant changed "{target["title"]}"')
        ctx.run.snapshotted.add(target["id"])
    commit_html(ctx, target["id"], diff_html(st.review_base, st.html) if st.dirty else st.original)


def commit_html(ctx: ToolContext, doc_id: str, html: str) -> None:
    ctx.document.commit(doc_id, html)
    ctx.run.known[doc_id] = _stored_content(ctx, doc_id)


_RANK = {"created": 4, "rewrite": 3, "polished": 2, "selection": 1, "edits": 0, "renamed": -1}


def touch(ctx: ToolContext, target: Target, kind: str, changes: int, failed: int) -> None:
    prev = ctx.run.touched.get(target["id"])
    if target["id"] in ctx.run.created:
        next_kind = "created"
    elif prev and _RANK[prev["kind"]] > _RANK[kind]:
        next_kind = prev["kind"]
    else:
        next_kind = kind
    ctx.run.touched[target["id"]] = {
        "documentId": target["id"], "titleAtRun": target["title"], "kind": next_kind,
        "changes": (prev["changes"] if prev else 0) + changes, "failed": (prev["failed"] if prev else 0) + failed,
    }


def _open_if_created(ctx: ToolContext, target: Target) -> None:
    if target["id"] in ctx.run.created:
        _open_for_writing(ctx, target["id"])


async def with_loaded(ctx: ToolContext, target: Target, fn):
    if not target["isStart"]:
        await ctx.document.ensure_loaded([target["id"]])
    out = fn()
    if hasattr(out, "__await__"):
        out = await out
    return out


def seen_enough_to_rewrite(ctx: ToolContext, doc_id: str, accepted_html: str) -> bool:
    """Port of seenEnoughToRewrite: the HTML this run, or the whole current text of a plain chapter (§0.11)."""
    if doc_id in ctx.run.html_shown:
        return True
    seen = ctx.run.text_seen.get(doc_id)
    return seen is not None and seen == hash_content(accepted_html) and is_text_rewritable_html(accepted_html)


def _unseen(target: Target) -> Dict[str, Any]:
    return result(False,
                  f"You have not seen the current HTML of {cite_chapter(target)} in this turn, so SEARCH text cannot be copied from it. "
                  f"Call read with chapters=[{target['number']}] and format=\"html\" first.",
                  f"⛔ edit of {cite_chapter(target)} refused — not read yet", retryable=True)


def preview_rewrite(ctx: ToolContext, chapter_ref: Any, html: str) -> None:
    open_before = ctx.document.open_id()
    target = resolve_target(chapter_ref, ctx)
    chars = f"{len(html_to_plain_text(html)):,}"
    if isinstance(target, str):
        name = chapter_ref["create"] if is_new_chapter_ref(chapter_ref) else str(chapter_ref)
        ctx.ui.progress(f'✍️ "{name}" … {chars} chars')
        return
    if not user_edited(ctx, target["id"]):
        _open_if_created(ctx, target)
    # The running count shows whether or not the text paints on screen (previewRewrite in documentWrites.ts).
    ctx.ui.progress(f"✍️ {cite_chapter(target)} … {chars} chars")
    if ctx.document.open_id() != target["id"] or user_edited(ctx, target["id"]):
        return
    if open_before != target["id"]:
        return
    ctx.editor.preview_document(html)


_SCALAR_RE_TEMPLATE = r'"{key}"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(-?\d+)\s*[,}}])'


def _complete_scalar_argument(text: str, key: str) -> Optional[Union[str, int]]:
    import json
    m = re.search(_SCALAR_RE_TEMPLATE.format(key=re.escape(key)), text)
    if not m:
        return None
    if m.group(1) is not None:
        try:
            return json.loads(f'"{m.group(1)}"')
        except ValueError:
            return None
    return int(m.group(2))


def _chapter_arg(raw: Optional[Dict[str, Any]]) -> Any:
    return raw.get("chapter") if raw else None


def _rewrite_target(raw: Optional[Dict[str, Any]]) -> Any:
    if raw and isinstance(raw.get("new_chapter"), str) and js_trim(raw["new_chapter"]):
        return {"create": js_trim(raw["new_chapter"])}
    return _chapter_arg(raw)


def _html_arg(raw: Optional[Dict[str, Any]]) -> str:
    return raw["html"] if raw and isinstance(raw.get("html"), str) else ""


# ── update_document ──────────────────────────────────────────────────────────

def _update_preview(text: str, ctx: ToolContext) -> None:
    partial = partial_string_argument(text, "html")
    if partial is None:
        return
    html_at = text.find('"html"')

    def before(key: str) -> bool:
        at = text.find(f'"{key}"')
        return at != -1 and at < html_at

    created = _complete_scalar_argument(text, "new_chapter") if before("new_chapter") else None
    if isinstance(created, str) and js_trim(created):
        ref: Any = {"create": js_trim(created)}
    else:
        ref = _complete_scalar_argument(text, "chapter") if before("chapter") else None
    preview_rewrite(ctx, ref, trim_incomplete_html_tail(partial))


async def _update_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    return with_plan_done(ctx, await _update_write(args, ctx, call), args.get("planDone") or [])


_MISSING_CHAPTER = ("There is no chapter #", "No chapter is titled ")


def _hold_missing(ctx: ToolContext, chapter: Any, html: str, refusal: str, closed: bool) -> Optional[Dict[str, Any]]:
    """Keep a whole rewrite written for a chapter that does not exist (backend_authority.md §6.2).

    Problem: a model that wrote a whole chapter under a number or title the
      book does not have (#7 of 6, a title it misremembered) lost all of it to
      the refusal, and wrote it again — thousands of tokens for one wrong name.
    Fix: the text is kept on the run; the model is told so, and confirms where
      it goes with an empty-html update_document — new_chapter="title" for a
      new chapter, or a chapter's number — which writes the kept text there.
    """
    if is_new_chapter_ref(chapter) or not closed or not js_trim(html) or not refusal.startswith(_MISSING_CHAPTER):
        return None
    ctx.run.held_text = {"ref": str(chapter), "html": html}
    chars = chapter_chars(html)
    return result(False,
                  f"{refusal.splitlines()[0]} Your text ({chars} characters) is KEPT — do not send it again. "
                  'To put it in a new chapter, call update_document with new_chapter="<its title>" and html=""; '
                  'to put it in an existing chapter, call update_document with chapter="<its number>" and html="". The kept text is written there.',
                  f'📥 kept {chars} chars written for missing chapter "{chapter}"', retryable=True)


async def _update_write(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    html, chapter, arguments_lost = args["html"], args["chapter"], args["argumentsLost"]
    target = resolve_target(chapter, ctx)
    if isinstance(target, str):
        held = _hold_missing(ctx, chapter, html, target, not arguments_lost and call.get("unclosed") is not True)
        return held or result(False, target, f"⚠️ rewrite: {target.splitlines()[0]}", retryable=True)
    kept = ctx.run.held_text if not js_trim(html) and not arguments_lost else None
    if kept:
        # The confirmation of §6.2: the text written for the missing chapter goes here.
        out = await _update_body(kept["html"], chapter, False, ctx, call, target)
        if out["ok"]:
            ctx.run.held_text = None
        return out
    return await _update_body(html, chapter, arguments_lost, ctx, call, target)


async def _update_body(html: str, chapter: Any, arguments_lost: bool, ctx: ToolContext, call: Dict[str, Any], target: Target) -> Dict[str, Any]:
    ctx.ui.progress(None)
    if ctx.document.open_id() != target["id"]:
        ctx.editor.discard_preview()

    def body() -> Dict[str, Any]:
        if user_edited(ctx, target["id"]):
            return edited_meanwhile(ctx, target)
        closed = not arguments_lost and call.get("unclosed") is not True
        if closed and not js_trim(html):
            return result(False, "update_document had an empty html argument; nothing was written.", "⚠️ rewrite: empty", effects={"producedNothing": True})
        if target["isStart"] and ctx.run.selection_attempted:
            return result(False, f"{cite_chapter(target)} has a selection rewrite in this turn; a full rewrite would overwrite it.",
                          "⚠️ rewrite skipped: it would overwrite the selection rewrite", retryable=False)
        st = doc_state(ctx, target)
        if not seen_enough_to_rewrite(ctx, target["id"], st.html) and not is_blank_content(st.html):
            if is_new_chapter_ref(chapter):
                return result(False,
                              f'A chapter titled "{target["title"]}" already exists as #{target["number"]} and has text you have not read in this turn, so it was NOT overwritten and no chapter was added. '
                              f"To rewrite it, read it first (read with chapters=[{target['number']}]); to add a new chapter, give it a title no chapter has.",
                              f'⛔ new_chapter "{target["title"]}" is #{target["number"]}, not read — not overwritten', retryable=True)
            how = (f"Read it (read with chapters=[{target['number']}]; text format is enough for this chapter), then write it."
                   if is_text_rewritable_html(st.html) else
                   f'It has formatting or images a text read does not show: read its HTML (read with chapters=[{target["number"]}] and format="html"), then write it.')
            return result(False, f"You have not seen the whole current text of {cite_chapter(target)} in this turn, so it was not rewritten. " + how,
                          f"⛔ rewrite of {cite_chapter(target)} refused — not read yet", retryable=True)
        candidate = strip_blank_paragraphs(strip_image_marker_paragraphs(ctx.images.restore(html)))
        issue = validate_canvas_replacement(candidate, closed)
        if issue:
            commit_doc(ctx, target, st)
            return result(False,
                          "The rewrite was cut off before it finished, so it was not applied." if issue == "truncated"
                          else "The rewrite abbreviated unchanged parts of the document, so applying it would have deleted content. It was not applied.",
                          f"⚠️ rewrite of {cite_chapter(target)} not applied ({issue})", retryable=False, effects={"canvasIssue": issue})
        # A rewrite that would drop a fifth of a chapter, or its headings or list items, is held back
        # once (rewriteLoss; a live run, 2026-10-10); a second send applies it.
        loss = None if (target["id"] in ctx.run.created or target["id"] in ctx.run.rewrite_loss_warned) else rewrite_loss(st.html, candidate)
        if loss:
            ctx.run.rewrite_loss_warned.add(target["id"])
            commit_doc(ctx, target, st)
            pct = math.floor((1 - loss["after"] / loss["before"]) * 100 + 0.5)
            return result(False, rewrite_loss_note(cite_chapter(target), loss),
                          f"⛔ rewrite of {cite_chapter(target)} held back — would drop {pct}% (send again to apply)", retryable=True)
        _open_if_created(ctx, target)
        chars_before = chapter_chars(st.html)
        reinserted = reinsert_missing_images(candidate, st.original)
        st.html = reinserted["html"]
        st.dirty = True
        commit_doc(ctx, target, st)
        ctx.run.html_shown.add(target["id"])
        touch(ctx, target, "rewrite", 1, 0)
        chars = chapter_chars(st.html)
        effects = {"reinsertedImages": reinserted["reinserted"]}
        if is_new_chapter_ref(chapter) and target["id"] in ctx.run.created:
            chapters = ctx.document.chapters()
            similar = similar_chapters(target["title"], target["id"], chapters)
            hint = ""
            if similar:
                names = ", ".join(f'#{c["number"]} "{c["title"]}"' for c in similar)
                hint = (f" Note: {names} {'has a similar title' if len(similar) == 1 else 'have similar titles'}. "
                        f"If you meant to rewrite #{similar[0]['number']} rather than add a chapter, do not write it again: call rename_chapter with chapter=\"{target['number']}\", title=\"{similar[0]['title']}\" and replace=true — "
                        f"the text you just wrote takes #{similar[0]['number']}'s place (as a change the user reviews) and #{target['number']} is removed. If a new chapter is what you meant, ignore this.")
            return result(True, f"Created a NEW chapter {cite_chapter(target)} at the end of the book and wrote it ({chars} characters). The book now has {len(chapters)} chapters.{hint}",
                          f"➕ wrote new {cite_chapter(target)} ({chars} chars)", effects=effects)
        prefix = f'A chapter titled "{target["title"]}" already existed as #{target["number"]}, so your new_chapter text rewrote it. ' if is_new_chapter_ref(chapter) else ""
        return result(True, f"{prefix}Rewrote the EXISTING chapter {cite_chapter(target)}: it had {chars_before} characters and now has {chars}. No chapter was added.",
                      f"✏️ rewrote {cite_chapter(target)} ({chars} chars)", effects=effects)

    return await with_loaded(ctx, target, body)


update_document_tool = Tool(
    **_schema_of("update_document"), kind="write", markup_form=True, native_on_markup=True,
    parse=lambda raw: {"html": _html_arg(raw), "chapter": _rewrite_target(raw), "argumentsLost": raw is None, "planDone": plan_done_arg(raw)},
    preview=_update_preview, execute=_update_execute,
)


# ── edit_document ────────────────────────────────────────────────────────────

def _edit_parse(raw: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    if raw is None:
        return "its arguments could not be parsed"
    items = raw.get("edits") if isinstance(raw.get("edits"), list) else []
    edits = [{"search": e["search"], "replace": e["replace"] if isinstance(e.get("replace"), str) else ""}
             for e in items if isinstance(e, dict) and isinstance(e.get("search"), str)]
    return {"edits": edits, "chapter": _chapter_arg(raw), "planDone": plan_done_arg(raw)} if edits else 'it contained no usable edit (each needs a "search" string)'


async def _edit_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    return with_plan_done(ctx, await _edit_write(args, ctx, call), args.get("planDone") or [])


async def _edit_write(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    edits: List[Dict[str, str]] = args["edits"]
    target = resolve_target(args["chapter"], ctx)
    if isinstance(target, str):
        return result(False, target, f"⚠️ edit: {target.splitlines()[0]}", retryable=True)
    if target["id"] not in ctx.run.html_shown:
        return _unseen(target)

    def report(failed: List[Dict[str, str]], where: str, chars: Optional[int] = None, under_review: Optional[List[Dict[str, str]]] = None,
               haystack: str = "") -> Dict[str, Any]:
        under_review = under_review or []
        applied = len(edits) - len(failed)
        if applied > 0 or failed:
            touch(ctx, target, "edits", applied, len(failed))
        not_found = [f for f in failed if f not in under_review]
        listing = lambda blocks: "\n".join(f"- {b['search']}" for b in blocks)  # noqa: E731
        # The closest paragraph, as exact HTML, and what kept the two apart (wc_text.edit_hints).
        near = lambda blocks: "\n".join(l for b in blocks for l in (f"- {b['search']}", nearest_hint(haystack, b["search"]) if haystack else "") if l)  # noqa: E731
        if not failed:
            content = f"Applied {applied} edit(s) to {cite_chapter(target)}." + ("" if chars is None else f" It now has {chars} characters.")
        else:
            content = f"Applied {applied} of {len(edits)} edit(s) to {cite_chapter(target)}."
            if not_found:
                content += f" These SEARCH texts were not found in its current HTML — copy them exactly from the document, or read it again:\n{near(not_found)}"
            if under_review:
                content += ("\nThese were found, but they change text that is still under review — text a pending change deletes, or across the edge of a pending change — so they were not applied. "
                            f"Change only text that lies wholly inside the new wording, or wholly outside any pending change:\n{listing(under_review)}")
        trace = (f"✏️ edited {cite_chapter(target)} ({applied} change{'' if applied == 1 else 's'})" if not failed
                 else f"⚠️ edited {cite_chapter(target)}: {applied} of {len(edits)} located")
        return result(not failed, content, trace, effects={"failedEdits": len(failed)})

    if target["isStart"] and ctx.run.selection_attempted:
        if not ctx.run.selection_applied:
            return {**report(edits, " beside the selection"), "retryable": False}
        current = _stored_content(ctx, target["id"])
        local = apply_edit_blocks_locally(ctx.images.preserve(current), edits)
        if len(local["failed"]) < len(edits):
            commit_html(ctx, target["id"], ctx.images.restore(local["html"]))
        return report(local["failed"], " beside the selection", None, local["underReview"], ctx.images.preserve(current))

    def body() -> Dict[str, Any]:
        if user_edited(ctx, target["id"]):
            return edited_meanwhile(ctx, target)
        st = doc_state(ctx, target)
        before = ctx.images.preserve(st.html)
        out = apply_edit_blocks(before, edits)
        if len(out["failed"]) < len(edits):
            st.html = strip_blank_paragraphs(ctx.images.restore(out["html"]))
            st.dirty = True
        commit_doc(ctx, target, st)
        return report(out["failed"], "", chapter_chars(st.html), [], before)

    return await with_loaded(ctx, target, body)


edit_document_tool = Tool(**_schema_of("edit_document"), kind="write", markup_form=True, parse=_edit_parse, execute=_edit_execute)


# ── replace_selection ────────────────────────────────────────────────────────

_BLOCK_START_RE = re.compile(r"^\s*<(?:p|h[1-6]|blockquote|ul|ol|li|div)\b", re.I)
_SINGLE_P_RE = re.compile(r"^\s*<p(?:\s[^>]*)?>((?:(?!</?p\b)[\s\S])*)</p>\s*$", re.I)


def align_selection_blocks(base: str, search: str, replace: str) -> Optional[Dict[str, str]]:
    if not js_trim(search) or _BLOCK_START_RE.search(search):
        return {"search": search, "replace": replace}
    if f"<p>{search}</p>" in base:
        return {"search": f"<p>{search}</p>", "replace": replace}
    single = _SINGLE_P_RE.search(replace)
    if single:
        return {"search": search, "replace": single.group(1)}
    return None if _BLOCK_START_RE.search(replace) else {"search": search, "replace": replace}


_LEADING_LAYOUT_RE = re.compile(r"^[ \t]*\r?\n\s*")
_TRAILING_LAYOUT_RE = re.compile(r"\s*\r?\n[ \t]*$")


def trim_tag_layout(text: str) -> str:
    """The rewrite without the line breaks that only lay out its tag (trimTagLayout in documentWrites.ts)."""
    return _TRAILING_LAYOUT_RE.sub("", _LEADING_LAYOUT_RE.sub("", text, count=1), count=1)


def preview_selection_with_count(ctx: ToolContext, html: str) -> None:
    """A selection rewrite's live preview, with its running character count (previewSelectionWithCount)."""
    ctx.ui.progress(f"✍️ the selection … {len(html_to_plain_text(html)):,} chars")
    ctx.editor.preview_selection(html)


def _selection_preview(text: str, ctx: ToolContext) -> None:
    partial = partial_string_argument(text, "html")
    if partial is not None:
        preview_selection_with_count(ctx, _LEADING_LAYOUT_RE.sub("", trim_incomplete_html_tail(partial), count=1))


async def _selection_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    ctx.run.selection_attempted = True
    ctx.ui.progress(None)
    cleaned = trim_tag_layout(strip_incomplete_end_tag(args["html"]))
    if not js_trim(cleaned):
        return result(False, "replace_selection had an empty html argument; nothing was written.", "⚠️ selection rewrite: empty", effects={"producedNothing": True})
    target = resolve_target(None, ctx)

    def gone() -> Dict[str, Any]:
        # Problem: the lock that keeps other writes off a chapter whose selection was rewritten stayed on
        #   when the rewrite never landed — the selection was stale, from a turn that had already rewritten
        #   it — so every other way to change the chapter was refused for the rest of the turn, and
        #   replace_text skipped it as "0 found" (2026-10-25: ten steps, nothing written).
        # Fix: nothing was written, so nothing needs protecting; the chapter is open to the other tools.
        if not ctx.run.selection_applied:
            ctx.run.selection_attempted = False
        return result(False, "The selected text is no longer where it was (it may have been rewritten already), so nothing was written. "
                             "Change the chapter with edit_paragraphs or the other write tools instead; they are not blocked.",
                      "⚠️ selection rewrite: the selection is gone", retryable=False, effects={"selectionGone": True})
    if isinstance(target, str) or not ctx.selection.original_text():
        return gone()
    restored = strip_blank_paragraphs(ctx.images.restore(cleaned))
    # Server-side there is no editor: the rewrite is placed in the stored
    # chapter by its text, from the turn's original (the chapter is locked
    # for the turn, so only this run has changed it since).
    base = ctx.images.preserve(ctx.document.original)
    aligned = align_selection_blocks(base, ctx.images.preserve(ctx.selection.original_text()), ctx.images.preserve(restored))
    placed = apply_edit_blocks_locally(base, [aligned]) if aligned else None
    if placed and not placed["failed"]:
        commit_html(ctx, target["id"], ctx.images.restore(placed["html"]))
        ctx.run.selection_applied = True
        touch(ctx, target, "selection", 1, 0)
        return result(True, "The selection was rewritten.", "✏️ rewrote the selection")
    if _stored_content(ctx, target["id"]) != ctx.document.original:
        commit_html(ctx, target["id"], ctx.document.original)
    return gone()


replace_selection_tool = Tool(
    **_schema_of("replace_selection"), kind="write", markup_form=True,
    is_available=lambda ctx: ctx.selection.range() is not None,
    parse=lambda raw: {"html": _html_arg(raw)}, preview=_selection_preview, execute=_selection_execute,
)

DOCUMENT_WRITE_TOOLS = [update_document_tool, edit_document_tool, replace_selection_tool]



# ── edit_paragraphs ──────────────────────────────────────────────────────────
# Port of editParagraphsTool (src/agent/tools/documentWrites.ts, §0.11).

_PARAGRAPH_ACTIONS = ["replace", "insert_before", "insert_after", "delete"]


def _paragraph_number(raw: Any) -> Optional[int]:
    m = re.match(r"^\s*¶?\s*(\d+)\s*$", str(raw if raw is not None else ""))
    return int(m.group(1)) if m else None


def _paragraphs_parse(raw: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    if raw is None:
        return "its arguments could not be parsed"
    items = raw.get("edits") if isinstance(raw.get("edits"), list) else []
    if not items:
        return 'no edits were given (pass "edits": [{paragraph, action, html, starts_with}])'
    edits: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        e = item if isinstance(item, dict) else {}
        paragraph = _paragraph_number(e.get("paragraph"))
        if paragraph is None:
            return f"edit {i + 1} has no paragraph number"
        action = js_trim(str(e.get("action") or "")).lower()
        if action not in _PARAGRAPH_ACTIONS:
            return f"edit {i + 1}: action must be one of {', '.join(_PARAGRAPH_ACTIONS)}"
        starts = e.get("starts_with") if isinstance(e.get("starts_with"), str) else (e.get("startsWith") if isinstance(e.get("startsWith"), str) else "")
        edit: Dict[str, Any] = {"paragraph": paragraph, "action": action, "startsWith": starts}
        if isinstance(e.get("html"), str):
            edit["html"] = e["html"]
        through = _paragraph_number(e.get("through"))
        if action == "delete" and through is not None and through != paragraph:
            if through < paragraph:
                return f"edit {i + 1}: through (¶{through}) comes before paragraph (¶{paragraph})"
            last = e.get("through_starts_with") if isinstance(e.get("through_starts_with"), str) else ""
            if not js_trim(last):
                return f"edit {i + 1}: a range delete needs through_starts_with, the first words of ¶{through}"
            edit["through"] = through
            edit["throughStartsWith"] = last
        edits.append(edit)
    return {"chapter": _chapter_arg(raw), "edits": edits, "planDone": plan_done_arg(raw)}


_EMPTY_LIST = re.compile(r"<(ul|ol)\b[^>]*>\s*</\1>", re.I)


def drop_empty_lists(html: str) -> str:
    """A list whose every entry was deleted or moved leaves no empty <ul></ul> behind."""
    return _EMPTY_LIST.sub("", html)


def _expand_range_deletes(html: str, edits: List[Dict[str, Any]]) -> Union[List[Dict[str, Any]], str]:
    """A delete with `through` becomes one delete per ¶, once its last ¶ reads as the model said.

    Problem: removing a long passage took one edit per paragraph, each with its own anchor.
    Fix: `through` (+ `through_starts_with`) deletes a whole range; the paragraphs between the two
      anchored ends are taken as they are (paragraph_moves.own_anchor's rule).
    """
    if not any("through" in e for e in edits):
        return edits
    spans = numbered_paragraph_spans(html)
    if spans is None:
        return edits  # apply_paragraph_edits reports it
    out: List[Dict[str, Any]] = []
    for e in edits:
        if "through" not in e:
            out.append(e)
            continue
        first, last = e["paragraph"], e["through"]
        if last > len(spans):
            return f"¶{first}–{last}: the chapter has {len(spans)} paragraphs"
        end = spans[last - 1]
        anchor = js_trim(e["throughStartsWith"])
        if not (anchor_matches(end["text"], anchor) or (end["kind"] == "image" and re.match(r"^\[?image\]?$", anchor, re.I))):
            shown = "[image]" if end["kind"] == "image" else end["text"][:160]
            return f"¶{last} (the end of the range) does not start with what you gave; it now reads: {shown}"
        out.append({"paragraph": first, "action": "delete", "startsWith": e["startsWith"]})
        for span in spans[first:last]:
            out.append({"paragraph": span["number"], "action": "delete",
                        "startsWith": "[image]" if span["kind"] == "image" else span["text"][:24]})
    return out


async def _paragraphs_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    return with_plan_done(ctx, await _paragraphs_write(args, ctx), args.get("planDone") or [])


async def _paragraphs_write(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    chapter, edits = args["chapter"], args["edits"]
    if is_new_chapter_ref(chapter):
        return result(False, "edit_paragraphs changes an existing chapter; write a new one with update_document.", "⚠️ edit_paragraphs: not a new chapter", retryable=True)
    target = resolve_target(chapter, ctx)
    if isinstance(target, str):
        return result(False, target, f"⚠️ edit_paragraphs: {target.splitlines()[0]}", retryable=True)
    if target["isStart"] and ctx.run.selection_attempted:
        return result(False, f"{cite_chapter(target)} has a selection rewrite in this turn; change other parts of it with edit_document.",
                      "⚠️ edit_paragraphs: beside a selection rewrite", retryable=True)

    async def write() -> Dict[str, Any]:
        if user_edited(ctx, target["id"]):
            return edited_meanwhile(ctx, target)
        st = doc_state(ctx, target)
        expanded = _expand_range_deletes(st.html, edits)
        if isinstance(expanded, str):
            return result(False, f"edit_paragraphs on {cite_chapter(target)}: {expanded}. Nothing was applied.",
                          f"⚠️ edit_paragraphs on {cite_chapter(target)}: range delete not applied", retryable=True)
        outcome = apply_paragraph_edits(st.html, [e if "html" not in e else {**e, "html": ctx.images.restore(e["html"])} for e in expanded])
        if not outcome["ok"]:
            stale = len(outcome["stale"])
            return result(False, f"edit_paragraphs on {cite_chapter(target)}: {outcome['error']}",
                          f"⚠️ edit_paragraphs on {cite_chapter(target)}: nothing applied"
                          + (f" ({stale} anchor{'' if stale == 1 else 's'} out of date)" if stale else ""), retryable=True)
        st.html = drop_empty_lists(strip_blank_paragraphs(outcome["html"]))
        st.dirty = True
        commit_doc(ctx, target, st)
        touch(ctx, target, "edits", outcome["applied"], 0)
        shift = outcome["paragraphsAfter"] - outcome["paragraphsBefore"]
        moved = (f" Paragraphs after ¶{outcome['firstChanged']} moved by {'+' if shift > 0 else ''}{shift}: take their new numbers from grep or a read before editing them again."
                 if shift else "")
        n = outcome["applied"]
        left = len(outcome["skipped"])
        rest = (f"\nNOT applied ({left}) — redo these with numbers and anchors from a fresh read:\n" + "\n".join(outcome["skipped"])) if left else ""
        return result(True, f"Applied {n} paragraph edit(s) to {cite_chapter(target)}. It now has {outcome['paragraphsAfter']} paragraphs and {chapter_chars(st.html)} characters." + moved + rest,
                      f"✏️ edited {cite_chapter(target)} by paragraph ({n} change{'' if n == 1 else 's'}{f'; {left} not applied' if left else ''})")

    return await with_loaded(ctx, target, write)


edit_paragraphs_tool = Tool(
    name="edit_paragraphs",
    description=(
        "Change paragraphs of a chapter by their ¶ numbers — the numbers grep and text reads show — with no HTML read and no SEARCH text. "
        'Each edit: paragraph (its ¶ number), action ("replace", "insert_before", "insert_after" or "delete"), html (the new paragraph(s) for replace and inserts; plain text becomes <p> paragraphs, a blank line separating them), '
        "and starts_with: the first words of that paragraph as you read it. Numbers refer to the chapter as it was before this call. "
        "An edit whose starts_with no longer matches (the paragraph moved or changed) is not applied — the others are — and you get that paragraph's current text. A list entry is a paragraph of its own and stays a list entry. "
        "A whole range goes in one edit: action \"delete\" with through (the last ¶) and through_starts_with. "
        "To move or copy paragraphs elsewhere, use copy_paragraphs — never delete and re-type them. "
        "For changes to most of a chapter, rewrite it instead."),
    parameters={"type": "object", "properties": {
        "chapter": {"type": "string", "description": "The chapter's position in the CHAPTER INDEX (#n — never a number from its title), or its exact title (default: the active chapter)."},
        "edits": {"type": "array", "description": "The changes, in any order.", "items": {"type": "object", "properties": {
            "paragraph": {"type": "integer", "description": "The ¶ number, as grep or read showed it."},
            "action": {"type": "string", "description": '"replace", "insert_before", "insert_after" or "delete".'},
            "html": {"type": "string", "description": "The new paragraph(s): HTML blocks, or plain text. Not used by delete."},
            "starts_with": {"type": "string", "description": "The first few words of that paragraph as you read it."},
            "through": {"type": "integer", "description": "delete only: delete from paragraph through this ¶ (a whole range in one edit)."},
            "through_starts_with": {"type": "string", "description": "With through: the first words of the range's last ¶."},
        }, "required": ["paragraph", "action", "starts_with"]}},
        "plan_done": PLAN_DONE_PARAMETER,
    }, "required": ["edits"]},
    kind="write", parse=_paragraphs_parse, execute=_paragraphs_execute,
)


# ── replace_text ─────────────────────────────────────────────────────────────

def _replace_parse(raw: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    if not raw:
        return "its arguments could not be parsed"
    find = raw.get("find") if isinstance(raw.get("find"), str) else ""
    if not find:
        return "find was empty"
    if not isinstance(raw.get("replace"), str):
        return "replace is required (an empty string deletes the text)"
    chapters = raw.get("chapters")
    items = chapters if isinstance(chapters, list) else ([] if chapters is None else [chapters])
    if not items:
        return 'no chapters were named (pass "chapters": ["3", "4"] or ["all"])'
    every = any(isinstance(c, str) and js_trim(c).lower() == "all" for c in items)
    expect = raw.get("expect")
    expect = expect if isinstance(expect, int) and not isinstance(expect, bool) and expect >= 0 else None
    return {"find": find, "replace": raw["replace"], "chapters": "all" if every else items,
            "regex": raw.get("regex") is True or raw.get("regex") == "true", "expect": expect,
            "preview": raw.get("preview") is True or raw.get("preview") == "true"}


async def _replace_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    """Port of replaceTextTool (documentWrites.ts): find and replace across chapters, in their text only."""
    find, replace, regex, expect, preview = args["find"], args["replace"], args["regex"], args["expect"], args["preview"]
    book = ctx.document.chapters()
    refs = list(range(1, len(book) + 1)) if args["chapters"] == "all" else args["chapters"]
    targets: List[Target] = []
    problems: List[str] = []
    for ref in refs:
        t = resolve_target(ref, ctx)
        if isinstance(t, str):
            problems.append(t.splitlines()[0])
        elif not any(x["id"] == t["id"] for x in targets):
            targets.append(t)
    if not targets:
        return result(False, f"replace_text: {' '.join(problems) or 'no chapter matched'}", "⚠️ replace_text: no chapter", retryable=True)
    await ctx.document.ensure_loaded([t["id"] for t in targets if not t["isStart"]])

    planned: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for target in targets:
        if target["isStart"] and ctx.run.selection_attempted:
            skipped.append(f"{cite_chapter(target)}: it has a selection rewrite in this turn")
            continue
        if next((c for c in ctx.document.chapters() if c["id"] == target["id"]), {}).get("loaded") is False:
            skipped.append(f"{cite_chapter(target)}: could not be loaded")
            continue
        if user_edited(ctx, target["id"]):
            forget_chapter(ctx, target["id"])
            skipped.append(f"{cite_chapter(target)}: the user changed it while you worked — read it again")
            continue
        st = doc_state(ctx, target)
        outcome = replace_in_html_text(st.html, find, replace, regex)
        if outcome.get("error"):
            return result(False, f"replace_text: {outcome['error']}. Nothing was changed.", f"⚠️ replace_text: {outcome['error']}", retryable=True)
        if outcome["count"] > 0:
            planned.append({"target": target, "html": outcome["html"], "count": outcome["count"], "samples": outcome["samples"]})
    total = sum(p["count"] for p in planned)
    report = "\n".join(f"{cite_chapter(p['target'])}: {p['count']}\n" + "\n".join(f"  {x}" for x in p["samples"]) for p in planned)
    notes = "\n".join(f"Not searched — {x}" for x in [*problems, *skipped])
    label = f"{find[:20] + '…' if len(find) > 20 else find}→{replace[:20] + '…' if len(replace) > 20 else replace}"

    if preview or (expect is not None and expect != total):
        why = ("Preview only: nothing was changed." if preview else
               f"You expected {expect} replacement(s); there are {total}, so nothing was changed. Narrow find (or use regex), or pass the right expect.")
        body = f"{why}\n{total} match(es) in {len(planned)} chapter(s)" + (f":\n{report}" if report else ".") + (f"\n{notes}" if notes else "")
        trace = (f"🔍 replace_text preview {label}: {total} in {len(planned)} chapter(s)" if preview else
                 f"⚠️ replace_text {label}: {total} found, {expect} expected — nothing changed")
        # A chapter skipped (locked, unloaded, edited meanwhile) is not a chapter without the text: say so.
        if skipped or problems:
            trace += f" ({len(skipped) + len(problems)} chapter(s) not searched)"
        return result(preview, body, trace, retryable=not preview)
    if total == 0:
        return result(False, f'No "{find}" in the {len(targets) - len(skipped) - len(problems)} chapter(s) searched; nothing was changed.' + (f"\n{notes}" if notes else ""),
                      f"⚠️ replace_text {label}: not found" + (f" ({len(skipped) + len(problems)} chapter(s) not searched)" if skipped or problems else ""), retryable=True)
    for p in planned:
        st = doc_state(ctx, p["target"])
        st.html = p["html"]
        st.dirty = True
        commit_doc(ctx, p["target"], st)
        touch(ctx, p["target"], "edits", p["count"], 0)
    not_searched = len(skipped) + len(problems)
    return result(True, f"Replaced {total} occurrence(s) in {len(planned)} chapter(s), each a change the user reviews:\n{report}" + (f"\n{notes}" if notes else ""),
                  f"🔁 replaced {label}: {total} in {len(planned)} chapter{'' if len(planned) == 1 else 's'}" + (f" ({not_searched} not searched)" if not_searched else ""))


replace_text_tool = Tool(
    name="replace_text",
    description=("Find and replace text across chapters in one call — renaming a character, a place, a term — in the text only, never in markup, links or images. No read is needed first. "
                 "Each chapter changed becomes a change the user reviews. A short name can sit inside longer words (王 in 王小明): run it with preview first, or pass expect (the number of replacements you intend; if the count differs nothing is changed and you get the matches). "
                 "For changes that need judgement occurrence by occurrence, use edit_paragraphs instead."),
    parameters={"type": "object", "properties": {
        "find": {"type": "string", "description": "The text to find (exactly as it reads), or a regular expression when regex is true."},
        "replace": {"type": "string", "description": "The replacement text. With regex, $1, $2… insert groups and $& the whole match."},
        "chapters": {"type": "array", "description": 'The chapters to change: positions from the CHAPTER INDEX or exact titles; ["all"] for every chapter.', "items": {"type": "string"}},
        "regex": {"type": "boolean", "description": "Optional: find is a regular expression (JavaScript syntax)."},
        "expect": {"type": "integer", "description": "Optional: the total number of replacements you intend; a different count changes nothing."},
        "preview": {"type": "boolean", "description": "Optional: only list the matches; change nothing."},
    }, "required": ["find", "replace", "chapters"]},
    kind="write", parse=_replace_parse, execute=_replace_execute,
)

# ── rename_chapter ───────────────────────────────────────────────────────────

def _rename_parse(raw: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    if raw is None or raw.get("chapter") is None:
        return "no chapter was named"
    title = js_trim(raw["title"]) if isinstance(raw.get("title"), str) else ""
    if not title:
        return "the new title was empty"
    return {"chapter": raw["chapter"], "title": title, "replace": raw.get("replace") is True or raw.get("replace") == "true"}


def _rename_heading(ctx: ToolContext, target: Target, title: str) -> bool:
    """A leading <h1> that read the old title follows the rename (renameHeading in documentWrites.ts)."""
    if user_edited(ctx, target["id"]):
        return False
    stored = _stored_content(ctx, target["id"])
    if leading_h1_text(stored) != js_trim(target["title"]):
        return False
    renamed = content_with_renamed_heading(stored, title)
    if renamed is None:
        return False
    st = ctx.run.docs.get(target["id"])
    if st:
        def follow(html: str) -> str:
            return content_with_renamed_heading(html, title) or html
        st.original, st.base, st.review_base, st.html = follow(st.original), follow(st.base), follow(st.review_base), follow(st.html)
        commit_html(ctx, target["id"], diff_html(st.review_base, st.html) if st.dirty else st.original)
    else:
        commit_html(ctx, target["id"], renamed)
    return True


async def _rename_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    title, replace = args["title"], args["replace"]
    source = resolve_target(args["chapter"], ctx)
    if isinstance(source, str):
        write = f'<canvas new_chapter="{title}">…</canvas>' if ctx.run.write_protocol == "markup" else f'update_document with new_chapter="{title}"'
        return result(False, f"rename_chapter only renames a chapter that exists, and nothing was renamed. {source}\nTo add a new chapter titled \"{title}\", write it: {write}.",
                      f"⚠️ rename: {source.splitlines()[0]}", retryable=True)
    chapters = ctx.document.chapters()
    holder_index = next((i for i, c in enumerate(chapters) if c["id"] != source["id"] and title_key(c["title"]) == title_key(title)), -1)

    if holder_index == -1:
        if source["title"] == title:
            return result(True, f"{cite_chapter(source)} already has that title.", f"✏️ {cite_chapter(source)} already so titled")
        if not source["isStart"]:
            await ctx.document.ensure_loaded([source["id"]])
        ctx.document.rename(source["id"], title)
        touch(ctx, {**source, "title": title}, "renamed", 0, 0)
        heading_moved = _rename_heading(ctx, source, title)
        heading = leading_h1_text(_stored_content(ctx, source["id"]))
        note = (" Its first heading, which read the old title, now reads the new one." if heading_moved
                else f' Its first heading still reads "{heading}"; change it with an edit if it should match.' if heading and heading != title else "")
        return result(True, f'Renamed #{source["number"]} from "{source["title"]}" to "{title}".{note}',
                      f'🏷 renamed #{source["number"]} "{source["title"]}" → "{title}"')

    holder = {**chapters[holder_index], "number": holder_index + 1}
    holder_target: Target = {"id": holder["id"], "title": holder["title"], "number": holder["number"], "isStart": holder["id"] == ctx.document.start_id}
    if not replace:
        hint = (f"If {cite_chapter(source)} was meant as a rewrite of #{holder['number']}, call rename_chapter again with replace=true: its text takes #{holder['number']}'s place and {cite_chapter(source)} is removed."
                if source["id"] in ctx.run.created else "Choose another title.")
        return result(False, f'#{holder["number"]} "{holder["title"]}" already has that title, and two chapters must not share one. {hint}',
                      f"⚠️ rename of {cite_chapter(source)} refused — the title is taken by #{holder['number']}", retryable=True)

    def refuse(content: str, why: str) -> Dict[str, Any]:
        return result(False, content, f"⚠️ {cite_chapter(source)} did not replace #{holder['number']} — {why}", retryable=False)

    if source["id"] not in ctx.run.created or user_edited(ctx, source["id"]):
        return refuse(f"Only a chapter you created in this turn can take another chapter's place; {cite_chapter(source)} was not, so nothing changed. To rewrite #{holder['number']}, write to it.", "not created this turn")
    if holder_target["isStart"] and ctx.run.selection_attempted:
        return refuse(f"#{holder['number']} has a selection rewrite in this turn; replacing its text would overwrite it.", "it has a selection rewrite")

    await ctx.document.ensure_loaded([holder["id"]])
    if next((c for c in ctx.document.chapters() if c["id"] == holder["id"]), {}).get("loaded") is False:
        return refuse(f"#{holder['number']} could not be loaded, so its text was not replaced.", "not loaded")
    if user_edited(ctx, holder["id"]):
        return edited_meanwhile(ctx, holder_target)
    text = doc_state(ctx, source).html
    if is_blank_content(text):
        return refuse(f"{cite_chapter(source)} is empty: there is no text to move.", "it is empty")

    was_open = ctx.document.open_id() == source["id"]
    st = doc_state(ctx, holder_target)
    st.html = content_with_renamed_heading(text, holder["title"]) or text
    st.dirty = True
    commit_doc(ctx, holder_target, st)
    ctx.run.html_shown.add(holder["id"])
    touch(ctx, holder_target, "rewrite", 1, 0)

    ctx.document.remove(source["id"])
    forget_chapter(ctx, source["id"])
    ctx.run.created.discard(source["id"])
    ctx.run.in_context.discard(source["id"])
    ctx.run.touched.pop(source["id"], None)
    if was_open:
        _open_for_writing(ctx, holder["id"])

    holder_now = next((i for i, c in enumerate(ctx.document.chapters()) if c["id"] == holder["id"]), -1) + 1
    after = len(chapters) - source["number"]
    return result(True,
                  f'#{holder_now} "{holder["title"]}" now has the text of {cite_chapter(source)} ({chapter_chars(st.html)} characters), as a change the user reviews; {cite_chapter(source)} was removed.'
                  + (f" The {after} chapter(s) after #{source['number']} moved up by one." if after > 0 else ""),
                  f'🔀 #{source["number"]} "{source["title"]}" → replaced #{holder_now} "{holder["title"]}"')


rename_chapter_tool = Tool(
    name="rename_chapter",
    description="Rename an existing chapter. Rename only a chapter the user asked you to rename, or one you added in this turn. "
                "It never adds a chapter: a new chapter is added by writing it (new_chapter).",
    parameters={"type": "object", "properties": {
        "chapter": {"type": "string", "description": "The chapter to rename: its number in the CHAPTER INDEX, or its exact title."},
        "title": {"type": "string", "description": "The new title."},
        "replace": {"type": "boolean", "description": "Only for a chapter you added in this turn, when another chapter already has this title because that is the one you meant to rewrite: true moves this chapter's text into it (in place, as a change the user reviews) and removes this chapter, so nothing is written again."},
    }, "required": ["chapter", "title"]},
    kind="write", run_last=True, parse=_rename_parse, execute=_rename_execute,
)

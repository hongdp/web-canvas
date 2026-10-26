"""Moving text the server already holds: copy_paragraphs and move_chapter.

Problem: a run asked to split a chapter in two (2026-10-24) had no way to put
  a chapter anywhere but the end of the book, nor to carry a passage from one
  chapter to another: it re-typed the following chapter whole (~7,000
  characters) into a new last chapter, then re-typed the second half of the
  first into the one it had emptied — over 20,000 characters of output to
  move text it never meant to change, with every character at risk.
Fix: the server moves the HTML itself. `copy_paragraphs` copies or moves a
  range of ¶ (images included) to a place in another chapter, the same one,
  or a new chapter placed where it belongs; `move_chapter` reorders chapters.
  Both follow the rules a write has: anchors (`starts_with`) guard the
  numbers, a chapter the user changed meanwhile is refused, every change is a
  reviewable diff.
"""
import html as html_lib
import re
from typing import Any, Dict, List, Optional, Union

from wc_text.chapters import cite_chapter
from wc_text.jsstr import js_trim
from wc_text.paragraphs import anchor_matches, apply_paragraph_edits, chapter_chars, numbered_paragraph_spans
from wc_text.text import is_blank_content, strip_blank_paragraphs

from ..registry import Tool
from ..types import ToolContext, result
from .document_writes import (_claim_new_chapter, commit_doc, doc_state, drop_empty_lists, edited_meanwhile, resolve_target,
                              touch, user_edited)
from .plan import PLAN_DONE_PARAMETER, plan_done_arg, with_plan_done

_IMAGE_ANCHOR = re.compile(r"^\[?image\]?$", re.I)
_ATTACHMENT_REF = re.compile(r"^(?:A|附件)\s*\d+$", re.I)


def _number(raw: Any) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) and float(raw).is_integer():
        return int(raw)
    m = re.match(r"^\s*¶?\s*(\d+)\s*$", str(raw if raw is not None else ""))
    return int(m.group(1)) if m else None


def _range_of(raw: Any) -> Optional[List[int]]:
    """"12-40", "12", 12 → [12, 40] / [12, 12]."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = _number(raw)
        return [n, n] if n else None
    m = re.match(r"^\s*¶?\s*(\d+)\s*(?:[-–—~～到至]\s*¶?\s*(\d+))?\s*$", str(raw if raw is not None else ""))
    if not m:
        return None
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    return [first, last] if 1 <= first <= last else None


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""


def own_anchor(span: Dict[str, Any]) -> str:
    """An anchor that matches this paragraph as it is now (for paragraphs the model did not name)."""
    return "[image]" if span["kind"] == "image" else span["text"][:24]


def _anchor_ok(span: Dict[str, Any], anchor: str) -> bool:
    anchor = js_trim(anchor)
    if not anchor:
        return False
    return anchor_matches(span["text"], anchor) or (span["kind"] == "image" and bool(_IMAGE_ANCHOR.match(anchor)))


def _shown(span: Dict[str, Any]) -> str:
    text = "[image]" if span["kind"] == "image" else span["text"]
    return text[:160] + "…" if len(text) > 160 else text


def checked_range(spans: List[Dict[str, Any]], first: int, last: int, first_anchor: str, last_anchor: str) -> Union[List[Dict[str, Any]], str]:
    """The spans ¶first..¶last, once both ends read as the model said; else why not."""
    if last > len(spans):
        return f"the chapter has {len(spans)} paragraphs, not {last}"
    if not js_trim(first_anchor):
        return f"starts_with is empty — give the first words of ¶{first} as you read it"
    if not _anchor_ok(spans[first - 1], first_anchor):
        return f"¶{first} does not start with what you gave; it now reads: {_shown(spans[first - 1])}"
    if last != first:
        if not js_trim(last_anchor):
            return f"last_starts_with is empty — give the first words of ¶{last} (the range's last paragraph) as you read it"
        if not _anchor_ok(spans[last - 1], last_anchor):
            return f"¶{last} does not start with what you gave; it now reads: {_shown(spans[last - 1])}"
    return spans[first - 1:last]


def blocks_of(html: str, chosen: List[Dict[str, Any]]) -> str:
    """The chosen paragraphs as HTML blocks, exactly as stored; list entries keep a list around them."""
    out: List[str] = []
    items: List[str] = []
    for span in chosen:
        block = f"<p>{js_trim(span['html'])}</p>" if span.get("loose") else html[span["start"]:span["end"]]
        if span["kind"] == "item":
            items.append(block)
            continue
        if items:
            out.append("<ul>" + "".join(items) + "</ul>")
            items = []
        out.append(block)
    if items:
        out.append("<ul>" + "".join(items) + "</ul>")
    return "".join(out)


def _deletes(chosen: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"paragraph": s["number"], "action": "delete", "startsWith": own_anchor(s)} for s in chosen]


def _insert_edit(spans: List[Dict[str, Any]], after: Optional[int], anchor: str, blocks: str) -> Union[Dict[str, Any], str, None]:
    """Where the blocks go in a chapter: after ¶after (0 = at the start, None = at the end). None: the chapter is empty."""
    if not spans:
        return None
    if after is None:
        return {"paragraph": len(spans), "action": "insert_after", "startsWith": own_anchor(spans[-1]), "html": blocks}
    if after == 0:
        return {"paragraph": 1, "action": "insert_before", "startsWith": own_anchor(spans[0]), "html": blocks}
    if after > len(spans):
        return f"the destination has {len(spans)} paragraphs, not {after}"
    if not _anchor_ok(spans[after - 1], anchor):
        return (f"after_starts_with must be the first words of ¶{after} of the destination; it now reads: {_shown(spans[after - 1])}"
                if js_trim(anchor) else f"after_starts_with is empty — give the first words of ¶{after} of the destination as you read it")
    return {"paragraph": after, "action": "insert_after", "startsWith": anchor, "html": blocks}


# ── copy_paragraphs ──────────────────────────────────────────────────────────

def _copy_parse(raw: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    if raw is None:
        return "its arguments could not be parsed"
    rng = _range_of(raw.get("paragraphs"))
    if rng is None:
        return 'paragraphs must be a ¶ number or a range like "12-40"'
    new_title = js_trim(_text(raw.get("new_chapter")))
    to = raw.get("to_chapter")
    if not new_title and (to is None or js_trim(str(to)) == ""):
        return "name the destination: to_chapter (an existing chapter) or new_chapter (a title for a new one)"
    after = raw.get("after_paragraph")
    after_n = None if after is None or (isinstance(after, str) and not js_trim(after)) else _number(after)
    if after is not None and after_n is None and not (isinstance(after, str) and not js_trim(after)):
        return "after_paragraph must be a ¶ number (0 = at the start)"
    after_chapter = raw.get("after_chapter")
    after_chapter_n = None if after_chapter is None else _number(after_chapter)
    if after_chapter is not None and after_chapter_n is None:
        return "after_chapter must be a chapter's position in the CHAPTER INDEX (0 = before the first)"
    return {"from": raw.get("from_chapter"), "first": rng[0], "last": rng[1],
            "startsWith": _text(raw.get("starts_with")), "lastStartsWith": _text(raw.get("last_starts_with")),
            "to": to, "newChapter": new_title, "after": after_n, "afterStartsWith": _text(raw.get("after_starts_with")),
            "afterChapter": after_chapter_n, "move": raw.get("move") is not False, "planDone": plan_done_arg(raw)}


async def _copy_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    return with_plan_done(ctx, await _copy_write(args, ctx), args.get("planDone") or [])


def _fail(text: str, retryable: bool = True) -> Dict[str, Any]:
    return result(False, text, f"⚠️ copy_paragraphs: {text.splitlines()[0][:120]}", retryable=retryable)


def _attachment_of(ref: Any, ctx: ToolContext) -> Optional[Dict[str, Any]]:
    # By reference only (A1, 附件1): a page saved under its title must not shadow a chapter of that title.
    if ctx.attachments is None or not isinstance(ref, str) or not _ATTACHMENT_REF.match(js_trim(ref)):
        return None
    from wc_text.attachments import resolve_attachment_ref
    return resolve_attachment_ref(ref, ctx.attachments.list())


_ATTACHMENT_IMAGE = re.compile(r"^\[image (\d+)(?::[^\]]*)?\]$")


def attachment_blocks(paragraphs: List[str], images: Optional[Dict[int, Dict[str, Any]]] = None) -> str:
    """An attachment's paragraphs as chapter HTML: an "[image N]" whose picture the attachment kept (web_save) becomes that picture."""
    out: List[str] = []
    for p in paragraphs:
        m = _ATTACHMENT_IMAGE.match(p)
        img = (images or {}).get(int(m.group(1))) if m else None
        if img and img.get("dataUrl"):
            out.append(f'<p><img src="{html_lib.escape(img["dataUrl"], quote=True)}" alt="{html_lib.escape(img.get("alt") or "", quote=True)}"></p>')
        else:
            out.append(f"<p>{html_lib.escape(p, quote=False)}</p>")
    return "".join(out)


async def _copy_write(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    attachment = _attachment_of(args["from"], ctx)
    if attachment is not None:
        return await _copy_from_attachment(args, ctx, attachment)
    verb = "move" if args["move"] else "copy"
    source = resolve_target(args["from"], ctx)
    if isinstance(source, str):
        return _fail(source)
    if source["isStart"] and ctx.run.selection_attempted:
        return _fail(f"{cite_chapter(source)} has a selection rewrite in this turn; {verb} its paragraphs in another turn.")
    ids = [source["id"]]
    if not args["newChapter"]:
        dest_ref = resolve_target(args["to"], ctx)
        if isinstance(dest_ref, str):
            return _fail(dest_ref)
        if dest_ref["isStart"] and ctx.run.selection_attempted and dest_ref["id"] != source["id"]:
            return _fail(f"{cite_chapter(dest_ref)} has a selection rewrite in this turn; put the paragraphs elsewhere.")
        ids.append(dest_ref["id"])
    await ctx.document.ensure_loaded(list(dict.fromkeys(ids)))
    for doc_id in dict.fromkeys(ids):
        if user_edited(ctx, doc_id):
            target = source if doc_id == source["id"] else dest_ref
            return edited_meanwhile(ctx, target)

    src_state = doc_state(ctx, source)
    src_spans = numbered_paragraph_spans(src_state.html)
    if src_spans is None:
        return _fail(f"{cite_chapter(source)}'s HTML could not be split into paragraphs reliably; edit it with edit_document instead.")
    chosen = checked_range(src_spans, args["first"], args["last"], args["startsWith"], args["lastStartsWith"])
    if isinstance(chosen, str):
        return _fail(f"{verb} from {cite_chapter(source)}: {chosen}. Nothing was changed.")
    blocks = blocks_of(src_state.html, chosen)
    span_label = f"¶{args['first']}" if args["first"] == args["last"] else f"¶{args['first']}–{args['last']}"
    count, chars = len(chosen), chapter_chars(blocks)

    # Into the same chapter: one pass, so the numbers the model gave all mean the chapter as it read it.
    if not args["newChapter"] and dest_ref["id"] == source["id"]:
        after = args["after"]
        if after is not None and args["first"] <= after <= args["last"] or (after == 0 and args["first"] == 1 and args["move"]) \
                or (after is None and args["last"] == len(src_spans) and args["move"]):
            return _fail(f"{span_label} is already there; nothing to {verb}.")
        insert = _insert_edit(src_spans, after, args["afterStartsWith"], blocks)
        if isinstance(insert, str):
            return _fail(f"{verb} within {cite_chapter(source)}: {insert}. Nothing was changed.")
        edits = ([*_deletes(chosen)] if args["move"] else []) + [insert]
        outcome = apply_paragraph_edits(src_state.html, edits)
        if not outcome["ok"] or outcome["skipped"]:
            return _fail(f"{verb} within {cite_chapter(source)}: {outcome.get('error') or '; '.join(outcome['skipped'])}. Nothing was changed.")
        src_state.html = drop_empty_lists(strip_blank_paragraphs(outcome["html"]))
        src_state.dirty = True
        commit_doc(ctx, source, src_state)
        touch(ctx, source, "edits", 1, 0)
        where = "the start" if after == 0 else "the end" if after is None else f"after ¶{after}"
        return result(True, f"{'Moved' if args['move'] else 'Copied'} {span_label} of {cite_chapter(source)} ({count} paragraph(s), {chars} characters) to {where} of the same chapter. "
                      f"Its paragraph numbers changed: take them from a fresh grep or read before editing it again.",
                      f"{'✂️ moved' if args['move'] else '📄 copied'} {span_label} within {cite_chapter(source)}")

    # Into another chapter, existing or new: both changes are computed before either is written.
    if args["newChapter"]:
        existing = next((c for c in ctx.document.chapters() if js_trim(c["title"]) == args["newChapter"]), None)
        if existing and not is_blank_content(existing["content"]):
            return _fail(f'A chapter titled "{args["newChapter"]}" already exists and has text; give the new chapter a title no chapter has, or name it with to_chapter.')
    else:
        dest_state = doc_state(ctx, dest_ref)
        dest_spans = numbered_paragraph_spans(dest_state.html)
        if dest_spans is None:
            return _fail(f"{cite_chapter(dest_ref)}'s HTML could not be split into paragraphs reliably; put the paragraphs elsewhere.")
        insert = _insert_edit(dest_spans, args["after"], args["afterStartsWith"], blocks)
        if isinstance(insert, str):
            return _fail(f"{verb} into {cite_chapter(dest_ref)}: {insert}. Nothing was changed.")
        if insert is None:
            new_dest_html = blocks
        else:
            outcome = apply_paragraph_edits(dest_state.html, [insert])
            if not outcome["ok"] or outcome["skipped"]:
                return _fail(f"{verb} into {cite_chapter(dest_ref)}: {outcome.get('error') or '; '.join(outcome['skipped'])}. Nothing was changed.")
            new_dest_html = outcome["html"]
    new_src_html = None
    if args["move"]:
        outcome = apply_paragraph_edits(src_state.html, _deletes(chosen))
        if not outcome["ok"] or outcome["skipped"]:
            return _fail(f"move from {cite_chapter(source)}: {outcome.get('error') or '; '.join(outcome['skipped'])}. Nothing was changed.")
        new_src_html = drop_empty_lists(strip_blank_paragraphs(outcome["html"]))

    if args["newChapter"]:
        dest_ref = _claim_new_chapter(args["newChapter"], ctx)
        if isinstance(dest_ref, str):
            return _fail(dest_ref)
        position = args["afterChapter"] if args["afterChapter"] is not None else source["number"]
        ctx.document.move(dest_ref["id"], position)
        number = next(i for i, c in enumerate(ctx.document.chapters()) if c["id"] == dest_ref["id"]) + 1
        dest_ref = {**dest_ref, "number": number}
        dest_state = doc_state(ctx, dest_ref)
        new_dest_html = blocks
    dest_state.html = strip_blank_paragraphs(new_dest_html)
    dest_state.dirty = True
    commit_doc(ctx, dest_ref, dest_state)
    touch(ctx, dest_ref, "created" if args["newChapter"] else "edits", 1, 0)
    if new_src_html is not None:
        # The source keeps its number unless the new chapter went in ahead of it.
        source = {**source, "number": next(i for i, c in enumerate(ctx.document.chapters()) if c["id"] == source["id"]) + 1}
        src_state.html = new_src_html
        src_state.dirty = True
        commit_doc(ctx, source, src_state)
        touch(ctx, source, "edits", 1, 0)

    where = (f"a NEW chapter {cite_chapter(dest_ref)}" if args["newChapter"] else
             f"{'the start' if args['after'] == 0 else 'the end' if args['after'] is None else 'after ¶' + str(args['after'])} of {cite_chapter(dest_ref)}")
    notes: List[str] = []
    if args["newChapter"]:
        notes.append(f"It sits at #{dest_ref['number']}; the chapters after it moved down by one, so the CHAPTER INDEX in your request shows old numbers.")
    if new_src_html is not None:
        if is_blank_content(new_src_html):
            notes.append(f"{cite_chapter(source)} is now empty: delete it with delete_chapter if it is not needed.")
        else:
            notes.append(f"Paragraph numbers in {cite_chapter(source)} after ¶{args['first']} moved up by {count}.")
    if not args["newChapter"]:
        notes.append(f"Paragraph numbers in {cite_chapter(dest_ref)} after the insertion moved down by {count}.")
    return result(True, f"{'Moved' if args['move'] else 'Copied'} {span_label} of {cite_chapter(source)} ({count} paragraph(s), {chars} characters, exactly as stored) to {where}. " + " ".join(notes),
                  f"{'✂️ moved' if args['move'] else '📄 copied'} {span_label} of {cite_chapter(source)} → {'new ' if args['newChapter'] else ''}{cite_chapter(dest_ref)} ({count} ¶)")


copy_paragraphs_tool = Tool(
    name="copy_paragraphs",
    description=(
        "Move or copy paragraphs the book already has — by their ¶ numbers, exactly as stored, images included — without writing them again. "
        "Use it to split a chapter (move ¶n to the end into new_chapter, placed after it), merge chapters, move a scene, or copy a passage as a draft. "
        "Never re-type existing text to move it: this is one call, and the text cannot change on the way. "
        "from_chapter may be an attachment (A1…, e.g. pages web_save stored): its paragraphs are copied into a chapter, the attachment left as it is. "
        "Source: from_chapter, paragraphs (\"12\" or \"12-40\"), starts_with (the first words of the first) and last_starts_with (of the last). "
        "Destination: to_chapter (an existing chapter, the same one included) with after_paragraph (its ¶ number; 0 = at the start; omit = at the end) and "
        "after_starts_with (the first words of that ¶), or new_chapter (a title) with after_chapter (its position in the CHAPTER INDEX; 0 = first; omit = right after the source). "
        "move=false copies and leaves the source as it is; the default moves. Each change is a reviewable diff."),
    parameters={"type": "object", "properties": {
        "from_chapter": {"type": "string", "description": "The source chapter's position in the CHAPTER INDEX (#n) or exact title (default: the active chapter), or an attachment (A1)."},
        "paragraphs": {"type": "string", "description": 'The ¶ range, as grep or read numbered it: "12" or "12-40".'},
        "starts_with": {"type": "string", "description": "The first words of the range's first paragraph."},
        "last_starts_with": {"type": "string", "description": "The first words of the range's last paragraph (not needed for one paragraph)."},
        "to_chapter": {"type": "string", "description": "An existing chapter to put them in (#n or exact title)."},
        "after_paragraph": {"type": "integer", "description": "Put them after this ¶ of the destination; 0 = at its start; omit = at its end."},
        "after_starts_with": {"type": "string", "description": "The first words of the destination ¶ named by after_paragraph."},
        "new_chapter": {"type": "string", "description": "Instead of to_chapter: the title of a new chapter to put them in."},
        "after_chapter": {"type": "integer", "description": "Where the new chapter goes: after this position in the CHAPTER INDEX (0 = first). Default: right after the source chapter."},
        "move": {"type": "boolean", "description": "true (default): remove them from the source. false: copy, leaving the source as it is."},
        "plan_done": PLAN_DONE_PARAMETER,
    }, "required": ["paragraphs", "starts_with"]},
    kind="write", parse=_copy_parse, execute=_copy_execute,
)


async def _copy_from_attachment(args: Dict[str, Any], ctx: ToolContext, attachment: Dict[str, Any]) -> Dict[str, Any]:
    """Paragraphs of an attachment (A1…) into a chapter: always a copy — attachments are not written."""
    label = f'{attachment["ref"]} "{attachment["name"]}"'
    paragraphs = await ctx.attachments.paragraphs(attachment["id"])
    spans = [{"number": i + 1, "kind": "text", "text": p} for i, p in enumerate(paragraphs)]
    chosen = checked_range(spans, args["first"], args["last"], args["startsWith"], args["lastStartsWith"])
    if isinstance(chosen, str):
        return _fail(f"copy from {label}: {chosen}. Nothing was changed.")
    texts = [s["text"] for s in chosen]
    images = await ctx.attachments.images(attachment["id"]) if hasattr(ctx.attachments, "images") and any(_ATTACHMENT_IMAGE.match(t) for t in texts) else {}
    blocks = attachment_blocks(texts, images)
    pictures = sum(1 for t in texts if (m := _ATTACHMENT_IMAGE.match(t)) and int(m.group(1)) in images)
    span_label = f"¶{args['first']}" if args["first"] == args["last"] else f"¶{args['first']}–{args['last']}"
    count, chars = len(chosen), sum(len(s["text"]) for s in chosen)
    if args["newChapter"]:
        existing = next((c for c in ctx.document.chapters() if js_trim(c["title"]) == args["newChapter"]), None)
        if existing and not is_blank_content(existing["content"]):
            return _fail(f'A chapter titled "{args["newChapter"]}" already exists and has text; give the new chapter a title no chapter has, or name it with to_chapter.')
        dest_ref = _claim_new_chapter(args["newChapter"], ctx)
        if isinstance(dest_ref, str):
            return _fail(dest_ref)
        if args["afterChapter"] is not None:
            ctx.document.move(dest_ref["id"], args["afterChapter"])
        dest_ref = {**dest_ref, "number": next(i for i, c in enumerate(ctx.document.chapters()) if c["id"] == dest_ref["id"]) + 1}
        dest_state = doc_state(ctx, dest_ref)
        new_html = blocks
    else:
        dest_ref = resolve_target(args["to"], ctx)
        if isinstance(dest_ref, str):
            return _fail(dest_ref)
        if dest_ref["isStart"] and ctx.run.selection_attempted:
            return _fail(f"{cite_chapter(dest_ref)} has a selection rewrite in this turn; put the paragraphs elsewhere.")
        await ctx.document.ensure_loaded([dest_ref["id"]])
        if user_edited(ctx, dest_ref["id"]):
            return edited_meanwhile(ctx, dest_ref)
        dest_state = doc_state(ctx, dest_ref)
        dest_spans = numbered_paragraph_spans(dest_state.html)
        if dest_spans is None:
            return _fail(f"{cite_chapter(dest_ref)}'s HTML could not be split into paragraphs reliably; put the paragraphs elsewhere.")
        insert = _insert_edit(dest_spans, args["after"], args["afterStartsWith"], blocks)
        if isinstance(insert, str):
            return _fail(f"copy into {cite_chapter(dest_ref)}: {insert}. Nothing was changed.")
        if insert is None:
            new_html = blocks
        else:
            outcome = apply_paragraph_edits(dest_state.html, [insert])
            if not outcome["ok"] or outcome["skipped"]:
                return _fail(f"copy into {cite_chapter(dest_ref)}: {outcome.get('error') or '; '.join(outcome['skipped'])}. Nothing was changed.")
            new_html = outcome["html"]
    dest_state.html = strip_blank_paragraphs(new_html)
    dest_state.dirty = True
    commit_doc(ctx, dest_ref, dest_state)
    touch(ctx, dest_ref, "created" if args["newChapter"] else "edits", 1, 0)
    where = (f"a NEW chapter {cite_chapter(dest_ref)} at #{dest_ref['number']} (the chapters after it moved down by one)" if args["newChapter"] else
             f"{'the start' if args['after'] == 0 else 'the end' if args['after'] is None else 'after ¶' + str(args['after'])} of {cite_chapter(dest_ref)} "
             f"(its paragraph numbers after the insertion moved down by {count})")
    return result(True, f"Copied {span_label} of attachment {label} ({count} paragraph(s), {chars} characters, exactly as the file has them"
                  + (f", {pictures} picture(s)" if pictures else "") + f") to {where}. "
                  "The attachment is unchanged (attachments are never written).",
                  f"📄 copied {span_label} of {attachment['ref']} → {'new ' if args['newChapter'] else ''}{cite_chapter(dest_ref)} ({count} ¶)")


# ── move_chapter ─────────────────────────────────────────────────────────────

def _move_parse(raw: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    if raw is None:
        return "its arguments could not be parsed"
    position = _number(raw.get("position"))
    if position is None or position < 1:
        return "position must be the place to put it, 1 = first"
    return {"chapter": raw.get("chapter"), "position": position}


async def _move_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    target = resolve_target(args["chapter"], ctx)
    if isinstance(target, str):
        return result(False, target, f"⚠️ move_chapter: {target.splitlines()[0]}", retryable=True)
    total = len(ctx.document.chapters())
    position = min(args["position"], total)
    if position == target["number"]:
        return result(True, f"{cite_chapter(target)} is already at #{position}.", f"↕️ {cite_chapter(target)} already at #{position}")
    ctx.document.move(target["id"], position - 1)
    low, high = sorted((target["number"], position))
    shift = "moved down by one" if position < target["number"] else "moved up by one"
    return result(True, f'Moved "{target["title"]}" from #{target["number"]} to #{position}. The chapters between #{low} and #{high} {shift}; '
                        "the CHAPTER INDEX in your request still shows the old numbers.",
                  f"↕️ moved {cite_chapter(target)} → #{position}")


move_chapter_tool = Tool(
    name="move_chapter",
    description="Put a chapter at another place in the book (1 = first); the chapters in between shift by one. Its text is not touched.",
    parameters={"type": "object", "properties": {
        "chapter": {"type": "string", "description": "The chapter's position in the CHAPTER INDEX (#n) or exact title."},
        "position": {"type": "integer", "description": "Where it goes: 1 = first."},
    }, "required": ["chapter", "position"]},
    kind="write", parse=_move_parse, execute=_move_execute,
)

PARAGRAPH_MOVE_TOOLS = [copy_paragraphs_tool, move_chapter_tool]

"""web_save: web pages into the book, written by the server, never re-typed (attachments_and_web.md §3).

By default the pages become ONE attachment — a text snapshot, each page under
its title and address — that the model reads like any attachment and copies
the parts that belong in the book into chapters with copy_paragraphs
(from_chapter="A3"). into="chapter" writes one page straight into a chapter,
its pictures inlined. Either way the model gets back a short receipt, not the
text again.
"""

import html as html_lib
import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

from wc_text.chapters import cite_chapter
from wc_text.jsstr import js_trim
from wc_text.paragraphs import apply_paragraph_edits, numbered_paragraph_spans
from wc_text.text import is_blank_content, strip_blank_paragraphs

from ..registry import Tool
from ..types import ToolContext, result
from .document_writes import _claim_new_chapter, commit_doc, doc_state, edited_meanwhile, resolve_target, touch, user_edited
from .paragraph_moves import _insert_edit, _number, _text
from .plan import PLAN_DONE_PARAMETER, plan_done_arg, with_plan_done

#: Characters of page text one web_save may write (a long chapter is ~30k).
WEB_SAVE_CAP = 150_000
#: Images one web_save may fetch and inline (web_images' limit per fetch is 20).
WEB_SAVE_IMAGES = 150
#: Pages one web_save may put into an attachment (each read paced per host, ≥1 s apart).
MAX_SAVE_URLS = 20
#: Characters one attachment snapshot may hold (an upload's limit is far larger; this is one call's).
WEB_ATTACHMENT_CAP = 1_000_000
_IMAGE_MARKER = re.compile(r"^\[image (\d+)(?::[^\]]*)?\]$")


def _fail(text: str, retryable: bool = True) -> Dict[str, Any]:
    return result(False, text, f"⚠️ web_save: {text.splitlines()[0][:120]}", retryable=retryable)


def _parse(raw: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    from .book_reads import _parse_range
    if raw is None:
        return "its arguments could not be parsed"
    urls = [js_trim(u) for u in raw.get("urls") if isinstance(u, str) and js_trim(u)] if isinstance(raw.get("urls"), list) else []
    single = js_trim(raw.get("url") or "") if isinstance(raw.get("url"), str) else ""
    urls = list(dict.fromkeys(urls or ([single] if single else [])))
    if not urls:
        return "no url was given"
    into = js_trim(str(raw.get("into") or "")).lower()
    to_chapterish = bool(js_trim(_text(raw.get("new_chapter")))) or (raw.get("to_chapter") is not None and bool(js_trim(str(raw.get("to_chapter")))))
    into = into if into in ("attachment", "chapter") else ("chapter" if to_chapterish else "attachment")
    if into == "attachment":
        if len(urls) > MAX_SAVE_URLS:
            return f"at most {MAX_SAVE_URLS} urls in one call ({len(urls)} were given); save the rest in another call"
        return {"into": "attachment", "urls": urls, "name": js_trim(_text(raw.get("name"))), "images": raw.get("images") is not False,
                "planDone": plan_done_arg(raw)}
    if len(urls) > 1:
        return 'into="chapter" saves one page; give one url (or save several into an attachment and copy from it)'
    url = urls[0]
    rng = _parse_range(raw.get("paragraphs"))
    if isinstance(rng, str):
        return rng
    new_title = js_trim(_text(raw.get("new_chapter")))
    to = raw.get("to_chapter")
    if new_title and to is not None and js_trim(str(to)):
        return "give to_chapter or new_chapter, not both"
    after = raw.get("after_paragraph")
    after_n = None if after is None or (isinstance(after, str) and not js_trim(after)) else _number(after)
    if after is not None and after_n is None and not (isinstance(after, str) and not js_trim(after)):
        return "after_paragraph must be a ¶ number (0 = at the start)"
    after_chapter = raw.get("after_chapter")
    after_chapter_n = None if after_chapter is None else _number(after_chapter)
    if after_chapter is not None and after_chapter_n is None:
        return "after_chapter must be a chapter's position in the CHAPTER INDEX (0 = before the first)"
    return {"into": "chapter", "url": url, "range": rng, "newChapter": new_title, "to": to if not new_title else None,
            "after": after_n, "afterStartsWith": _text(raw.get("after_starts_with")), "afterChapter": after_chapter_n,
            "images": raw.get("images") is not False, "source": raw.get("source_link") is not False, "planDone": plan_done_arg(raw)}


def page_html(paragraphs: List[str], images: Dict[int, Dict[str, Any]], url: str, source: bool) -> str:
    """The page's paragraphs as chapter HTML: "# x" a heading, "[image n]" its picture (dropped when not fetched)."""
    out: List[str] = []
    for p in paragraphs:
        m = _IMAGE_MARKER.match(p)
        if m:
            img = images.get(int(m.group(1)))
            if img and img.get("dataUrl"):
                out.append(f'<p><img src="{html_lib.escape(img["dataUrl"], quote=True)}" alt="{html_lib.escape(img.get("alt") or "", quote=True)}"></p>')
            continue
        if p.startswith("# "):
            out.append(f"<h2>{html_lib.escape(p[2:].strip(), quote=False)}</h2>")
        else:
            out.append(f"<p>{html_lib.escape(p, quote=False)}</p>")
    if source and out:
        safe = html_lib.escape(url, quote=True)
        out.append(f'<p>Source: <a href="{safe}">{html_lib.escape(url, quote=False)}</a></p>')
    return "".join(out)


#: Pictures one attachment snapshot keeps (each ≤ 1200 px JPEG, so a few MB in all).
WEB_ATTACHMENT_IMAGES = 150


def snapshot_text(pages: List[Dict[str, Any]], numbers: Optional[List[Dict[int, int]]] = None) -> str:
    """Pages as one attachment's text: each under its title and address, a ¶ per paragraph (blank lines between).

    `numbers[i]` maps page i's image numbers to the attachment's (one count
    through all its pages); a marker whose picture was not kept becomes
    "[image not saved: alt]", so no number points at nothing.
    """
    out: List[str] = []
    for i, page in enumerate(pages):
        out.append(js_trim(page.get("title") or "") or page["url"])
        out.append(f"Source: {page['url']}")
        for p in page["paragraphs"]:
            m = _IMAGE_MARKER.match(p)
            if m and numbers is not None:
                alt = p[m.end(1):-1].lstrip(":").strip()
                n = numbers[i].get(int(m.group(1)))
                out.append(f"[image {n}: {alt}]" if n and alt else f"[image {n}]" if n else f"[image not saved{': ' + alt if alt else ''}]")
            else:
                out.append(p[2:].strip() if p.startswith("# ") else p)
    return "\n\n".join(out) + "\n"


async def _page_pictures(ctx: ToolContext, pages: List[Dict[str, Any]]) -> Any:
    """Every page's pictures fetched (20 per call, as web_images), numbered through the attachment; with what was missed."""
    numbers: List[Dict[int, int]] = []
    kept: List[Dict[str, Any]] = []
    missed = 0
    for page in pages:
        mapping: Dict[int, int] = {}
        wanted = [int(m.group(1)) for p in page["paragraphs"] if (m := _IMAGE_MARKER.match(p))]
        room = WEB_ATTACHMENT_IMAGES - len(kept)
        missed += max(0, len(wanted) - room)
        wanted = wanted[:max(0, room)]
        by_n = {im["n"]: im for im in page.get("images") or []}
        for i in range(0, len(wanted), 20):
            if len(wanted) > 20:
                # A page can carry a hundred pictures: a minute or two of fetching, so the bubble says where it is.
                ctx.ui.progress(f"🖼️ saving pictures {i + 1}–{min(i + 20, len(wanted))} of {len(wanted)}")
            try:
                fetched = await ctx.web.images(page["url"], wanted[i:i + 20])
            except Exception:  # noqa: BLE001 — the text is saved without them
                missed += len(wanted) - i
                break
            for r in fetched:
                if r.get("dataUrl"):
                    kept.append({"n": len(kept) + 1, "page": r["n"], "alt": r.get("alt") or "", "dataUrl": r["dataUrl"],
                                 "src": (by_n.get(r["n"]) or {}).get("url") or ""})
                    mapping[r["n"]] = len(kept)
                else:
                    missed += 1
        numbers.append(mapping)
    return numbers, kept, missed


async def _execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    out = await (_save_attachment(args, ctx) if args["into"] == "attachment" else _write(args, ctx))
    return with_plan_done(ctx, out, args.get("planDone") or [])


def page_name(page: Dict[str, Any]) -> str:
    return js_trim(page.get("title") or "") or (urlparse(page["url"]).hostname or "web page")


async def snapshot_page(ctx: ToolContext, page: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A page the USER linked, kept as an attachment when web_read reads it — once: an earlier snapshot of it is reused.

    Returns {attachment, images: [{n, alt, dataUrl}], reused}, or None where attachments cannot be saved.
    Live (2026-10-25): told in the read result to save the page first, Sonnet 5.5 read the page the user
    linked three times and wrote each time; the source never reached the book. So the server keeps it.
    """
    if ctx.attachments is None or not hasattr(ctx.attachments, "save") or not page.get("paragraphs"):
        return None
    name = page_name(page)
    for a in ctx.attachments.list():
        if a["name"] != name:
            continue
        paras = await ctx.attachments.paragraphs(a["id"])
        if len(paras) > 1 and paras[1] == f"Source: {page['url']}":
            images = await ctx.attachments.images(a["id"]) if hasattr(ctx.attachments, "images") else {}
            return {"attachment": a, "images": [images[n] for n in sorted(images)], "reused": True}
    if len(snapshot_text([page])) > WEB_ATTACHMENT_CAP:
        return None
    numbers, kept, _missed = await _page_pictures(ctx, [page])
    saved = await ctx.attachments.save(name, snapshot_text([page], numbers), kept)
    return {"attachment": saved, "images": kept, "reused": False}


async def _save_attachment(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    if ctx.attachments is None or not hasattr(ctx.attachments, "save"):
        return _fail('attachments cannot be saved here; save the page with into="chapter".', retryable=False)
    pages: List[Dict[str, Any]] = []
    failed: List[str] = []
    for url in args["urls"]:
        try:
            page = await ctx.web.read(url)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{url}: {str(e).splitlines()[0][:160]}")
            continue
        if not page.get("paragraphs"):
            failed.append(f"{url}: no readable text")
            continue
        pages.append(page)
        ctx.run.web_saved.update({url, page["url"]})
    if not pages:
        return _fail("Nothing was saved — no page could be read:\n" + "\n".join(failed), retryable=False)
    total = len(snapshot_text(pages))
    if total > WEB_ATTACHMENT_CAP:
        return _fail(f"the pages hold {total} characters, more than one attachment snapshot takes ({WEB_ATTACHMENT_CAP}); save fewer pages per call.")
    name = args["name"] or page_name(pages[0])
    numbers, kept, missed = await _page_pictures(ctx, pages) if args["images"] else ([{} for _ in pages], [], 0)
    text = snapshot_text(pages, numbers)
    try:
        saved = await ctx.attachments.save(name, text, kept)
    except Exception as e:  # noqa: BLE001
        return _fail(f"the attachment could not be stored: {e}", retryable=False)
    # Where each page sits in the attachment, by the same paragraph split read uses.
    lines: List[str] = []
    at = 1
    for page in pages:
        n = len(page["paragraphs"]) + 2
        lines.append(f"¶{at}–{at + n - 1}: {js_trim(page.get('title') or '') or page['url']} ({page['url']})")
        at += n
    note = ("\nNot saved:\n" + "\n".join(failed)) if failed else ""
    image_note = ""
    if kept:
        image_note = (f" Its {len(kept)} pictures are saved with it as [image N]: copy_paragraphs carries them into a chapter, "
                      f'and view_images chapter="{saved["ref"]}" shows them to you.')
    if missed:
        image_note += f" {missed} picture(s) could not be fetched or were past {WEB_ATTACHMENT_IMAGES}, and are marked \"[image not saved]\"."
    if not args["images"]:
        image_note = " Pictures were left out (images=false)."
    return result(True, f'Saved {len(pages)} page(s) as attachment {saved["ref"]} "{saved["name"]}" ({saved["paragraphs"]} paragraphs, {saved["chars"]} characters). '
                  f'Read it with read chapters=["{saved["ref"]}"], search it with grep; copy what belongs in the book into a chapter with '
                  f'copy_paragraphs from_chapter="{saved["ref"]}" — never re-type it.{image_note}\n' + "\n".join(lines) + note,
                  f"💾 saved {len(pages)} page(s) → {saved['ref']} {saved['name'][:40]}" + (f", {len(kept)} image(s)" if kept else "")
                  + (f" ({len(failed)} failed)" if failed else ""),
                  retryable=False)


async def _write(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    try:
        page = await ctx.web.read(args["url"])
    except Exception as e:  # noqa: BLE001
        return _fail(f"web_save could not read {args['url']}: {e}", retryable=False)
    paragraphs: List[str] = page.get("paragraphs") or []
    ctx.run.web_saved.update({args["url"], page["url"]})
    if not paragraphs:
        return _fail(f"{page['url']} has no readable text; nothing was saved.", retryable=False)
    rng = args["range"]
    first = rng["from"] if rng else 1
    last = min(rng["to"] if rng and rng["to"] is not None else len(paragraphs), len(paragraphs))
    if first > len(paragraphs) or first > last:
        return _fail(f"the page has {len(paragraphs)} paragraphs; ¶{first}–{last} is not among them. Nothing was saved.")
    chosen = paragraphs[first - 1:last]
    text_chars = sum(len(p) for p in chosen if not _IMAGE_MARKER.match(p))
    if text_chars > WEB_SAVE_CAP:
        return _fail(f"¶{first}–{last} is {text_chars} characters, more than one web_save writes ({WEB_SAVE_CAP}); save it in parts with paragraphs.")
    title = args["newChapter"] or (None if args["to"] is not None else js_trim(page.get("title") or "") or (urlparse(page["url"]).hostname or "Web page"))

    # Where it goes, checked before anything is fetched or written.
    dest_ref = None
    insert_at = None
    if title is None:
        dest_ref = resolve_target(args["to"], ctx)
        if isinstance(dest_ref, str):
            return _fail(dest_ref)
        if dest_ref["isStart"] and ctx.run.selection_attempted:
            return _fail(f"{cite_chapter(dest_ref)} has a selection rewrite in this turn; save the page elsewhere.")
        await ctx.document.ensure_loaded([dest_ref["id"]])
        if user_edited(ctx, dest_ref["id"]):
            return edited_meanwhile(ctx, dest_ref)
        dest_state = doc_state(ctx, dest_ref)
        spans = numbered_paragraph_spans(dest_state.html)
        if spans is None:
            return _fail(f"{cite_chapter(dest_ref)}'s HTML could not be split into paragraphs reliably; save the page into a new chapter.")
        probe = _insert_edit(spans, args["after"], args["afterStartsWith"], "<p>x</p>")
        if isinstance(probe, str):
            return _fail(f"save into {cite_chapter(dest_ref)}: {probe}. Nothing was saved.")
        insert_at = spans
    else:
        existing = next((c for c in ctx.document.chapters() if js_trim(c["title"]) == title), None)
        if existing and not is_blank_content(existing["content"]):
            return _fail(f'A chapter titled "{title}" already exists and has text; give new_chapter a title no chapter has, or name it with to_chapter.')

    # The pictures the chosen paragraphs show, fetched like web_images.
    wanted = [int(m.group(1)) for p in chosen if (m := _IMAGE_MARKER.match(p))]
    fetched: Dict[int, Dict[str, Any]] = {}
    image_note = ""
    if wanted and args["images"]:
        take = wanted[:WEB_SAVE_IMAGES]
        for i in range(0, len(take), 20):
            try:
                for r in await ctx.web.images(page["url"], take[i:i + 20]):
                    fetched[r["n"]] = r
            except Exception as e:  # noqa: BLE001
                image_note = f" Fetching its images failed ({str(e).splitlines()[0][:120]}); they were left out."
                break
        missed = [n for n in wanted if not (fetched.get(n) or {}).get("dataUrl")]
        if missed and not image_note:
            image_note = f" {len(missed)} of its {len(wanted)} images could not be fetched or were past the limit of {WEB_SAVE_IMAGES}, and were left out."
    elif wanted:
        image_note = f" Its {len(wanted)} images were left out (images=false)."
    blocks = page_html(chosen, fetched, page["url"], args["source"])
    got = sum(1 for n in wanted if (fetched.get(n) or {}).get("dataUrl"))
    span = f"¶{first}–{last}" if (first, last) != (1, len(paragraphs)) else "the whole page"
    host = urlparse(page["url"]).hostname or page["url"]

    if dest_ref is None:
        dest_ref = _claim_new_chapter(title, ctx)
        if isinstance(dest_ref, str):
            return _fail(dest_ref)
        if args["afterChapter"] is not None:
            ctx.document.move(dest_ref["id"], args["afterChapter"])
        number = next(i for i, c in enumerate(ctx.document.chapters()) if c["id"] == dest_ref["id"]) + 1
        dest_ref = {**dest_ref, "number": number}
        dest_state = doc_state(ctx, dest_ref)
        dest_state.html = strip_blank_paragraphs(blocks)
        where = f"a NEW chapter {cite_chapter(dest_ref)} (#{number}; chapters after it moved down by one)"
        kind = "created"
    else:
        insert = _insert_edit(insert_at, args["after"], args["afterStartsWith"], blocks)
        if insert is None:
            new_html = blocks
        else:
            outcome = apply_paragraph_edits(dest_state.html, [insert])
            if not outcome["ok"] or outcome["skipped"]:
                return _fail(f"save into {cite_chapter(dest_ref)}: {outcome.get('error') or '; '.join(outcome['skipped'])}. Nothing was saved.")
            new_html = outcome["html"]
        dest_state.html = strip_blank_paragraphs(new_html)
        after = args["after"]
        where = f"{'the start' if after == 0 else 'the end' if after is None else 'after ¶' + str(after)} of {cite_chapter(dest_ref)} (its ¶ numbers after that moved)"
        kind = "edits"
    dest_state.dirty = True
    commit_doc(ctx, dest_ref, dest_state)
    touch(ctx, dest_ref, kind, 1, 0)
    pictures = f", {got} image(s)" if got else ""
    return result(True, f"Saved {span} of {page['url']} ({len(chosen)} paragraph(s), {text_chars} characters{pictures}, exactly as the page has it) to {where}. "
                  f"It is a reviewable diff; do not write the text again.{image_note}",
                  f"💾 saved {host} {span} → {'new ' if kind == 'created' else ''}{cite_chapter(dest_ref)}{pictures}")


web_save_tool = Tool(
    name="web_save",
    description=(
        "Save web pages into the book without writing them out yourself. "
        f"By default (into=\"attachment\") up to {MAX_SAVE_URLS} pages (url, or urls — e.g. the chapter links web_read listed) become ONE new attachment, "
        "a text snapshot with each page under its title and address; then read it like any attachment and copy the parts that belong in the book "
        "into chapters with copy_paragraphs from_chapter=\"A…\". "
        "into=\"chapter\" writes one page straight into a chapter, its pictures inlined: new_chapter (a title; default the page's title) with after_chapter "
        "(its position in the CHAPTER INDEX; 0 = first; omit = at the end), or to_chapter (an existing chapter) with after_paragraph (0 = at its start; omit = at its end) "
        f"and after_starts_with (that ¶'s first words); paragraphs picks a ¶ range; at most {WEB_SAVE_CAP} characters and {WEB_SAVE_IMAGES} images per call. "
        "Save only what the user asked for."),
    parameters={"type": "object", "properties": {
        "url": {"type": "string", "description": "The page's address, as given to web_read."},
        "urls": {"type": "array", "items": {"type": "string"}, "description": f"Instead of url, for an attachment: up to {MAX_SAVE_URLS} pages, in the order they go in."},
        "into": {"type": "string", "description": '"attachment" (default): a new attachment. "chapter": into a chapter (implied by new_chapter or to_chapter).'},
        "name": {"type": "string", "description": "The attachment's name (default: the first page's title)."},
        "paragraphs": {"type": "string", "description": 'Optional ¶ range of the page, e.g. "3-120" or "3-". Default: the whole page.'},
        "new_chapter": {"type": "string", "description": "The title of a new chapter to save it as (default: the page's title)."},
        "after_chapter": {"type": "integer", "description": "Where the new chapter goes: after this position in the CHAPTER INDEX (0 = first). Default: at the end."},
        "to_chapter": {"type": "string", "description": "Instead of a new chapter: an existing chapter (#n or exact title) to put the text in."},
        "after_paragraph": {"type": "integer", "description": "With to_chapter: after this ¶ (0 = at its start; omit = at its end)."},
        "after_starts_with": {"type": "string", "description": "The first words of the ¶ named by after_paragraph."},
        "images": {"type": "boolean", "description": "true (default): keep the pages' pictures (in the attachment, or inlined in the chapter). false: text only."},
        "source_link": {"type": "boolean", "description": "true (default): end with a link to the page."},
        "plan_done": PLAN_DONE_PARAMETER,
    }, "required": ["url"]},
    kind="write", is_available=lambda ctx: ctx.web is not None, parse=_parse, execute=_execute,
)

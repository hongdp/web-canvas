"""view_images: the agent looks at the pictures in a chapter (2026-10-18).

Problem: a chapter's pictures reached the model only as "[image]" in a text
  read (or a placeholder token in an HTML one), so writing around them — a
  caption, a scene a picture illustrates, a description of what it shows —
  meant guessing. A chapter can hold several pictures between its
  paragraphs, some linked from another site.
Fix: a read tool that sends a chapter's images to the model itself, shrunk
  (server_web.view_images), on the tool's result message as `images` — each
  provider renders them its own way (wc_providers). Offered only to a model
  that takes image input (`supports_vision`); at most VIEW_PER_CALL per call
  and VIEW_PER_RUN per run, since every image costs about a thousand tokens
  each step it stays in the conversation.
"""
import re
from typing import Any, Dict, List, Optional, Union

from wc_text.chapters import cite_chapter
from wc_text.diff import strip_diff_markup
from wc_text.paragraphs import block_images, chapter_paragraphs

from ..registry import Tool
from ..types import ToolContext, result
from .book_reads import _parse_range
from .document_writes import resolve_target, with_loaded

VIEW_PER_CALL = 6
VIEW_PER_RUN = 30
#: Pictures one view_images with a task looks at, in batches outside the conversation (polish.look_in_batches).
LOOK_PER_CALL = 150
_SRC_RE = re.compile(r"""\bsrc\s*=\s*("([^"]*)"|'([^']*)')""", re.I)

#: Providers whose chat models take image input; a local or unknown model is offered no view_images.
_VISION_PROVIDERS = {"anthropic", "openai", "grok", "gemini"}


def supports_vision(provider: str, model: str) -> bool:
    """Whether a run's model can be shown images (view_images is offered only then)."""
    if provider not in _VISION_PROVIDERS:
        return False
    # The few text-only names those providers still serve.
    return not re.search(r"(^|[-_])(mini-tts|embedding|audio|realtime|transcribe)([-_]|$)", model or "", re.I)


def chapter_images(html: str) -> List[Dict[str, Any]]:
    """Every <img> of a chapter's accepted reading in order: its number, the ¶ it sits in (as `read` numbers them), its src."""
    out: List[Dict[str, Any]] = []
    for p in chapter_paragraphs(strip_diff_markup(html)):
        for tag in block_images(p["html"]):
            m = _SRC_RE.search(tag)
            src = (m.group(2) if m.group(2) is not None else m.group(3)) if m else ""
            if src:
                out.append({"n": len(out) + 1, "paragraph": p["number"], "src": src, "tag": tag})
    return out


def _parse(raw: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    raw = raw or {}
    rng = _parse_range(raw.get("paragraphs"))
    if isinstance(rng, str):
        return rng
    wanted = raw.get("images")
    if isinstance(wanted, (int, float)) and not isinstance(wanted, bool):
        wanted = [wanted]
    elif isinstance(wanted, str) and wanted.strip():
        wanted = [w for w in re.split(r"[\s,，、]+", wanted.strip()) if w]
    numbers: Optional[List[int]] = None
    if isinstance(wanted, list) and wanted:
        try:
            numbers = [int(n) for n in wanted]
        except (TypeError, ValueError):
            return "images must be image numbers, like [1, 2, 3]"
    task = raw.get("task")
    return {"chapter": raw.get("chapter"), "range": rng, "numbers": numbers, "task": task.strip() if isinstance(task, str) and task.strip() else None}


def _choose(images: List[Dict[str, Any]], args: Dict[str, Any], cite: str, ctx: ToolContext) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """The images asked for, by ¶ range and/or number, within the turn's limit; or the result saying why none."""
    if not images:
        return result(False, f"{cite} has no images.", f"🖼️ {cite}: no images", retryable=False)
    rng = args["range"]
    chosen = images
    if rng:
        chosen = [i for i in chosen if i["paragraph"] >= rng["from"] and (rng["to"] is None or i["paragraph"] <= rng["to"])]
    if args["numbers"]:
        by_n = {i["n"]: i for i in images}
        missing = [n for n in args["numbers"] if n not in by_n]
        if missing:
            return result(False, f"{cite} has {len(images)} images; there is no image {', '.join(map(str, missing))}.",
                          f"⚠️ view_images: no image {missing[0]} in {cite}", retryable=True)
        chosen = [by_n[n] for n in args["numbers"] if not rng or by_n[n] in chosen]
    if not chosen:
        return result(False, f"{cite} has no images in those paragraphs. Its images are in ¶"
                      + ", ¶".join(str(p) for p in sorted({i["paragraph"] for i in images})) + ".",
                      f"🖼️ {cite}: no images there", retryable=True)
    if not args.get("task") and VIEW_PER_RUN - ctx.run.images_viewed <= 0:
        return result(False, f"This turn has already been shown {VIEW_PER_RUN} images, the most one turn may see. Work from what you have seen.",
                      "⛔ view_images: the turn's image limit is reached", retryable=False)
    return chosen


def _shown_result(cite: str, total: int, chosen: List[Dict[str, Any]], shown_now: List[Dict[str, Any]],
                  fetched: List[Dict[str, Any]], tokens: List[str], ctx: ToolContext) -> Dict[str, Any]:
    shown: List[str] = []
    lines: List[str] = []
    for img, got, token in zip(shown_now, fetched, tokens):
        if got.get("dataUrl"):
            shown.append(got["dataUrl"])
            lines.append(f"[Image {len(shown)}] = image {img['n']} of {total}, in ¶{img['paragraph']} — its token: {token}")
        else:
            lines.append(f"image {img['n']} (¶{img['paragraph']}, token {token}): not shown — {got.get('error') or 'it could not be fetched'}")
    ctx.run.images_viewed += len(shown)
    rest = [i for i in chosen if i not in shown_now]
    more = (f" {len(rest)} more of those are not shown: ask for them with images=[{', '.join(str(i['n']) for i in rest[:VIEW_PER_CALL])}]." if rest else "")
    head = (f"{cite} has {total} images. "
            + (f"Shown below, in this order:" if shown else "None of the requested images could be shown:"))
    content = head + "\n" + "\n".join(lines) + more
    nums = [str(i["n"]) for i, g in zip(shown_now, fetched) if g.get("dataUrl")]
    out = result(bool(shown), content,
                 f"🖼️ viewed {len(shown)} image{'' if len(shown) == 1 else 's'} of {cite}" + (f" (#{', #'.join(nums)})" if nums else ""),
                 retryable=False)
    if shown:
        out["images"] = shown
    return out


async def _look(args: Dict[str, Any], ctx: ToolContext, cite: str, total: int, chosen: List[Dict[str, Any]],
                tokens: List[str], data_urls: List[Optional[str]], errors: List[str]) -> Dict[str, Any]:
    """The chosen pictures looked at outside the conversation for the task; one line each comes back, with its token."""
    items = [{"label": f"image {img['n']} (¶{img['paragraph']})", "dataUrl": d} for img, d in zip(chosen, data_urls) if d]
    if not items:
        return result(False, f"None of those pictures of {cite} could be loaded." + ("\n" + "\n".join(errors[:5]) if errors else ""),
                      f"⚠️ view_images: {cite}: nothing to look at", retryable=False)

    def progress(done: int, of: int) -> None:
        ctx.ui.progress(f"🖼️ looking at {cite}'s pictures: batch {min(done + 1, of)} of {of}" if done < of else None)
    outcome = await ctx.analyze.look(args["task"], items, progress)
    lines = outcome.get("lines") or []
    key = "\n".join(f"image {img['n']} (¶{img['paragraph']}) — token {t}" for img, t, d in zip(chosen, tokens, data_urls) if d)
    looked = sum(1 for d in data_urls if d)
    status = ""
    if outcome.get("failed"):
        status = f"\nStopped after batch {outcome['batches']} of {outcome['total']}: {outcome['failed'][:200]}. The lines above are what was seen."
    elif outcome.get("stopped"):
        status = f"\nStopped after batch {outcome['batches']} of {outcome['total']}."
    content = (f"Looked at {looked} of {cite}'s {total} pictures outside the conversation for: {args['task']}\n"
               "(These lines are what the pictures show; the pictures themselves are not in this conversation.)\n\n"
               + "\n".join(lines) + status
               + ("\n\nNot loaded: " + "; ".join(errors[:5]) if errors else "")
               + "\n\nTo place one in a chapter, write its token:\n" + key)
    return result(bool(lines), content, f"🖼️ looked at {looked} pictures of {cite} ({outcome.get('batches', 0)} batch(es))", retryable=False)


async def _view_attachment(args: Dict[str, Any], ctx: ToolContext, attachment: Dict[str, Any]) -> Dict[str, Any]:
    """The pictures a saved page brought (web_save keeps them beside the attachment's text, by its "[image N]" markers)."""
    from .paragraph_moves import _ATTACHMENT_IMAGE
    cite = f'{attachment["ref"]} "{attachment["name"]}"'
    stored = await ctx.attachments.images(attachment["id"]) if hasattr(ctx.attachments, "images") else {}
    paragraphs = await ctx.attachments.paragraphs(attachment["id"]) if stored else []
    images = []
    for i, p in enumerate(paragraphs):
        m = _ATTACHMENT_IMAGE.match(p)
        if m and int(m.group(1)) in stored:
            images.append({"n": int(m.group(1)), "paragraph": i + 1, **stored[int(m.group(1))]})
    chosen = _choose(images, args, cite, ctx)
    if isinstance(chosen, dict):
        return chosen
    if args.get("task"):
        chosen = chosen[:LOOK_PER_CALL]
        return await _look(args, ctx, cite, len(images), chosen, [ctx.images.register(i["dataUrl"], i.get("alt") or "") for i in chosen],
                           [i["dataUrl"] for i in chosen], [])
    shown_now = chosen[:min(VIEW_PER_CALL, VIEW_PER_RUN - ctx.run.images_viewed)]
    # Its token places the picture in a chapter, as a written {{IMAGE_PLACEHOLDER_n}} does any image.
    tokens = [ctx.images.register(i["dataUrl"], i.get("alt") or "") for i in shown_now]
    return _shown_result(cite, len(images), chosen, shown_now, [{"dataUrl": i["dataUrl"]} for i in shown_now], tokens, ctx)


async def _execute_checked(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    if args.get("task") and not hasattr(getattr(ctx, "analyze", None), "look"):
        return result(False, "Looking at pictures with a task is not available here; view them without a task (6 at a time).",
                      "⚠️ view_images: no task here", retryable=False)
    return await _execute(args, ctx, call)


async def _execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    from .paragraph_moves import _attachment_of
    attachment = _attachment_of(args["chapter"], ctx)
    if attachment is not None:
        return await _view_attachment(args, ctx, attachment)
    target = resolve_target(args["chapter"], ctx)
    if isinstance(target, str):
        return result(False, target, f"⚠️ view_images: {target.splitlines()[0]}", retryable=True)

    async def body() -> Dict[str, Any]:
        html = next((c["content"] for c in ctx.document.chapters() if c["id"] == target["id"]), "")
        images = chapter_images(html)
        cite = cite_chapter(target)
        chosen = _choose(images, args, cite, ctx)
        if isinstance(chosen, dict):
            return chosen
        if args.get("task"):
            chosen = chosen[:LOOK_PER_CALL]
            data_urls: List[Optional[str]] = []
            errors: List[str] = []
            for k in range(0, len(chosen), 20):
                if len(chosen) > 20:
                    ctx.ui.progress(f"🖼️ loading {cite}'s pictures {k + 1}–{min(k + 20, len(chosen))} of {len(chosen)}")
                for img, got in zip(chosen[k:k + 20], await ctx.vision.view([i["src"] for i in chosen[k:k + 20]])):
                    data_urls.append(got.get("dataUrl"))
                    if not got.get("dataUrl"):
                        errors.append(f"image {img['n']}: {got.get('error') or 'could not be fetched'}")
            return await _look(args, ctx, cite, len(images), chosen, [ctx.images.preserve(i["tag"]) for i in chosen], data_urls, errors)
        shown_now = chosen[:min(VIEW_PER_CALL, VIEW_PER_RUN - ctx.run.images_viewed)]
        fetched = await ctx.vision.view([i["src"] for i in shown_now])
        # Its token: written into any chapter, it places this picture there (the book's own numbering,
        # the same in every turn — server_context.stable_image_registry).
        tokens = [ctx.images.preserve(i["tag"]) for i in shown_now]
        return _shown_result(cite, len(images), chosen, shown_now, fetched, tokens, ctx)

    return await with_loaded(ctx, target, body)


view_images_tool = Tool(
    name="view_images",
    description=("Look at the pictures in a chapter: they are sent to you as images, in order. A text read shows a picture only as [image]; "
                 "use this when what you write depends on what a picture shows (describing it, writing the scene it illustrates, a caption). "
                 f"Pick them by paragraph (the ¶ numbers of a read) or by their number in the chapter; at most {VIEW_PER_CALL} per call, {VIEW_PER_RUN} per turn — "
                 "each image costs about a thousand tokens, so look only at the ones you need. "
                 f"With a task (e.g. \"for each picture, what it shows\"), the pictures — up to {LOOK_PER_CALL}, all of them by default — "
                 "are looked at in batches OUTSIDE the conversation and only a line per picture comes back: use that for many pictures (a page of a hundred pictures). "
                 "Each comes with its {{IMAGE_PLACEHOLDER_n}} token: write that token in a chapter (its own paragraph, or the end of the one it illustrates) "
                 "to place the picture there — in another chapter too, as a copy; the original stays where it is."),
    parameters={"type": "object", "properties": {
        "chapter": {"type": "string", "description": "Optional. Its number in the CHAPTER INDEX or its exact title, or an attachment (A1) whose pictures web_save kept; omit for the active chapter."},
        "paragraphs": {"type": "string", "description": 'Optional. The images in these paragraphs, like "12-30" or "45".'},
        "images": {"type": "array", "items": {"type": "integer"}, "description": "Optional. Images by their number in the chapter (1 = its first image)."},
        "task": {"type": "string", "description": "Optional. What to note about each picture: they are then looked at outside the conversation, a line each back."},
    }},
    kind="read", is_available=lambda ctx: getattr(ctx, "vision", None) is not None, parse=_parse, execute=_execute_checked,
)

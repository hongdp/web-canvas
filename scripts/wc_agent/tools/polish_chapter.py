"""Port of src/agent/tools/polishChapter.ts."""
from typing import Any, Dict

from wc_text.chapters import cite_chapter
from wc_text.paragraphs import chapter_chars

from ..registry import Tool
from ..types import ToolContext, result
from .document_writes import commit_doc, doc_state, edited_meanwhile, resolve_target, touch, user_edited, with_loaded


async def _polish_execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    target = resolve_target(args.get("chapter"), ctx)
    if isinstance(target, str):
        return result(False, target, f"⚠️ polish: {target.splitlines()[0]}", retryable=True)
    polish = ctx.polish
    if polish is None:
        return result(False, "No polish model is configured.", "⚠️ polish: not configured", retryable=False)

    async def body() -> Dict[str, Any]:
        if user_edited(ctx, target["id"]):
            return edited_meanwhile(ctx, target)
        st = doc_state(ctx, target)
        ctx.ui.writing(target["id"])
        try:
            outcome = await polish.run(ctx.images.preserve(st.html), lambda done, total: ctx.ui.progress(f"✨ polishing {cite_chapter(target)} … {done}/{total}"))
        finally:
            ctx.ui.writing(None)
        ctx.ui.progress(None)
        if user_edited(ctx, target["id"]):
            refused = edited_meanwhile(ctx, target)
            return {**refused, "retryable": False,
                    "content": f"The user edited {cite_chapter(target)} while it was being polished, so the polish was discarded. Tell the user; polish it again only if they ask."}
        if outcome["polished"] > 0:
            st.html = ctx.images.restore(outcome["html"])
            st.dirty = True
            commit_doc(ctx, target, st)
            ctx.run.html_shown.discard(target["id"])
        touch(ctx, target, "polished", outcome["polished"], outcome["chunks"] - outcome["polished"])
        summary = f"{outcome['polished']} of {outcome['chunks']} chunk(s) rewritten"
        return result(outcome["polished"] > 0,
                      f"Polished {cite_chapter(target)}: {summary}."
                      + (f" It now has {chapter_chars(st.html)} characters." if outcome["polished"] > 0 else "")
                      + (f" Kept as drafted — {'; '.join(outcome['kept'])}." if outcome["kept"] else "")
                      + (' Its text changed: read it again (format "html") before editing it.' if outcome["polished"] > 0 else ""),
                      f"✨ polished {cite_chapter(target)} ({summary})", retryable=False)

    return await with_loaded(ctx, target, body)


polish_chapter_tool = Tool(
    name="polish_chapter",
    description=("Polish a chapter's prose: rewrite choppy narration into fuller sentences, chunk by chunk, with a separate polish model; every line of dialogue is kept, and a chunk that fails the checks keeps its draft. "
                 "Call this ONLY when the user explicitly asks to polish (润色) a chapter — never on your own initiative, and never right after writing a chapter unless asked."),
    parameters={"type": "object", "properties": {"chapter": {"type": "string", "description": "Optional. Its number in the CHAPTER INDEX or its exact title; omit for the active chapter."}}},
    kind="write", is_available=lambda ctx: ctx.polish is not None, parse=lambda raw: {"chapter": raw.get("chapter") if raw else None},
    execute=_polish_execute,
)

"""Port of src/agent/tools/analyzeBook.ts — reading for a task: what `read` does when
what it was asked for does not fit in one call and a `task` says what the notes are
for (read_and_list.md §2; formerly the analyze_book tool)."""
from typing import Any, Dict, List, Union

from wc_text.attachments import attachment_chunks, find_attachment_range
from wc_text.chapters import cite_chapter
from wc_text.jsstr import js_trim
from wc_text.paragraphs import chapter_paragraphs

from ..polish import ANALYZE_CONFIRM_TOKENS
from ..types import ToolContext, result

NOTES_CAP = 20_000
#: An attachment is cut into pieces of at most this many characters before batching (§1).
ATTACHMENT_CHUNK_CHARS = 40_000


def _thousands(n: int) -> str:
    return f"{int(n / 1000 + 0.5)}k" if n >= 10_000 else str(n)


def _refused(content: str, line: str) -> Dict[str, Any]:
    return result(False, f"read was not run: {content}", f"⚠️ read: {line}", retryable=True)


async def _collect(items: List[Dict[str, Any]], ctx: ToolContext) -> Union[Dict[str, Any], None]:
    """What a task-read would send. Returns {"chapters", "labels", "unloaded"} or {"error": result}."""
    from .book_reads import accepted_html, attachment_list, resolve_ref
    all_chapters = ctx.document.chapters()
    known = attachment_list(ctx)
    wanted: List[Dict[str, Any]] = []
    if not items:
        wanted = [{"chapter": {"id": c["id"], "title": c["title"], "number": i + 1}, "range": None} for i, c in enumerate(all_chapters)]
    else:
        for item in items:
            r = resolve_ref(item["ref"], all_chapters, known)
            if "error" in r:
                return {"error": _refused(r["error"], r["error"].splitlines()[0])}
            if "attachment" in r:
                wanted.append({"attachment": r["attachment"], "range": item.get("range"), "section": item.get("section")})
            elif item.get("section"):
                return {"error": _refused(f'section="{item["section"]}" names a part of an attachment; for a chapter, pass paragraphs.', "section on a chapter")}
            else:
                wanted.append({"chapter": r["chapter"], "range": item.get("range")})
    await ctx.document.ensure_loaded([w["chapter"]["id"] for w in wanted if "chapter" in w])
    now = ctx.document.chapters()
    out: Dict[str, Any] = {"chapters": [], "labels": [], "unloaded": []}
    for w in wanted:
        if "chapter" in w:
            c, rng = w["chapter"], w["range"]
            if next((n for n in now if n["id"] == c["id"]), {}).get("loaded") is False:
                out["unloaded"].append(cite_chapter(c))
                continue
            html = accepted_html(ctx, c["id"])
            if not rng:
                if js_trim(html):
                    out["chapters"].append({"id": c["id"], "title": c["title"], "content": html})
                out["labels"].append(f"#{c['number']}")
                continue
            paras = chapter_paragraphs(html)
            to = min(rng["to"] if rng.get("to") is not None else len(paras), len(paras))
            text = "\n".join(p["text"] for p in paras if rng["from"] <= p["number"] <= to)
            if js_trim(text):
                out["chapters"].append({"id": f"{c['id']}#{rng['from']}", "title": f"{c['title']} (¶{rng['from']}–{to})", "content": text})
            out["labels"].append(f"#{c['number']} ¶{rng['from']}–{to}")
            continue
        att = w["attachment"]
        paras = await ctx.attachments.paragraphs(att["id"])
        span = None
        if w.get("range"):
            rng = w["range"]
            span = {"from": rng["from"], "to": min(rng["to"] if rng.get("to") is not None else len(paras), len(paras))}
            if span["from"] > len(paras):
                return {"error": _refused(f"{att['ref']} has {len(paras)} paragraphs; there is no ¶{span['from']}.", f"{att['ref']} has no ¶{span['from']}")}
            out["labels"].append(f"{att['ref']} ¶{span['from']}–{span['to']}")
        elif w.get("section"):
            hit = find_attachment_range(att["sections"], w["section"])
            if hit is None:
                sample = ", ".join(f'"{s["title"]}"' for s in att["sections"][:6])
                return {"error": _refused(
                    f'{att["ref"]} "{att["name"]}" has no section matching "{w["section"]}".'
                    + (f' Its sections begin {sample}…; list source="{att["ref"]}" shows them all, or pass paragraphs.' if sample
                       else " It has no section headings; pass paragraphs instead."),
                    f'no section "{w["section"]}" in {att["ref"]}')}
            span = {"from": hit["from"], "to": hit["to"]}
            out["labels"].append(f"{att['ref']} {w['section']}")
        else:
            out["labels"].append(att["ref"])
        for i, chunk in enumerate(attachment_chunks(att, paras, ATTACHMENT_CHUNK_CHARS, span)):
            out["chapters"].append({"id": f"{att['id']}#{span['from'] if span else 0}-{i}", "title": chunk["title"], "content": chunk["text"]})
    return out


async def read_for_task(task: str, items: List[Dict[str, Any]], confirmed: bool, ctx: ToolContext) -> Dict[str, Any]:
    """Port of readForTask: batches outside the conversation, notes back; past ANALYZE_CONFIRM_TOKENS only with confirmed."""
    analyze = ctx.analyze
    if analyze is None:
        return result(False, "This is more than one read returns, and no model is available here to read it in batches: read it part by part, by paragraph ranges.",
                      "⚠️ read: too long for one read, no batch reading here", retryable=False)
    got = await _collect(items, ctx)
    if "error" in got:
        return got["error"]
    labels = got["labels"]
    where = "the book" if not items else (", ".join(labels) if len(labels) <= 4 else f"{len(labels)} parts")
    plan = analyze.plan(task, got["chapters"]) if hasattr(analyze, "plan") else None
    if plan and plan["inputTokens"] > ANALYZE_CONFIRM_TOKENS and not confirmed:
        return result(False,
                      f"read was not run: reading {where} for this task would send about {plan['inputTokens']} input tokens in {plan['calls']} model call{'' if plan['calls'] == 1 else 's'}, more than {ANALYZE_CONFIRM_TOKENS} — ask first. "
                      "Ask the user with ask_user whether to spend that, giving these numbers. To spend less, read only what the task needs: grep for it and read those paragraphs, or name a part (section=\"第10–20章\" or paragraphs=\"400-900\" for an attachment — list source=\"A1\" shows its sections — or fewer chapters). "
                      "If the user agrees, call read again with the same arguments and confirmed: true.",
                      f"📚 read for a task: {where} ≈ {_thousands(plan['inputTokens'])} tokens in {plan['calls']} calls — asks first", retryable=False)
    out = await analyze.run(task, got["chapters"], lambda done, total: ctx.ui.progress(f"📚 reading for the task … batch {min(done + 1, total)}/{total}"))
    ctx.ui.progress(None)
    notes = out["notes"] if len(out["notes"]) <= NOTES_CAP else f"{out['notes'][:NOTES_CAP]}\n[notes cut at {NOTES_CAP} characters]"
    ended = (f" Stopped by the user after {out['batches']} of {out['total']} batches." if out.get("stopped")
             else f" Batch {out['batches'] + 1} of {out['total']} failed ({out['failed']}); the notes cover the batches before it." if out.get("failed") else "")
    skipped = f"\nNot read — their text could not be loaded: {', '.join(got['unloaded'])}." if got["unloaded"] else ""
    n = len(got["chapters"])
    return result(out["batches"] > 0 and bool(out["notes"]),
                  f"NOTES from reading {n} part(s) of {where} in {out['batches']} batch(es) for: {task}.{ended}\n{notes or '(no notes)'}{skipped}",
                  f"📚 read {where} for a task — {n} part{'' if n == 1 else 's'}, {out['batches']} model call{'' if out['batches'] == 1 else 's'}"
                  + (" (stopped)" if out.get("stopped") else " (failed)" if out.get("failed") else ""), retryable=False)


async def rest_of_read_note(items: List[Dict[str, Any]], ctx: ToolContext) -> str:
    """Port of restOfReadNote: what reading all of an over-long request would cost."""
    analyze = ctx.analyze
    if analyze is None or not hasattr(analyze, "plan"):
        return ""
    got = await _collect(items, ctx)
    if "error" in got or not got["chapters"]:
        return ""
    plan = analyze.plan("(estimate)", got["chapters"])
    return ("[Not all of it fit in one read. To have all of it read and get notes, call read again with the same arguments and task=\"what the notes are for\": "
            f"{plan['calls']} batch{'' if plan['calls'] == 1 else 'es'} outside the conversation, ≈{_thousands(plan['inputTokens'])} input tokens"
            + (", which needs the user's yes first" if plan["inputTokens"] > ANALYZE_CONFIRM_TOKENS else "") + ".]")


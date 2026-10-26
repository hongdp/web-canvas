"""Port of src/agent/polish.ts and analyzeBook.ts — the model-call halves of
polish_chapter and analyze_book, over async transports."""
import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

import re

from wc_text.chapter_index import analyze_batch_chars, pack_chapters_into_batches
from wc_text.context_window import cjk_ratio_of, estimate_tokens
from wc_text.jsstr import js_trim
from wc_text.llm_context import html_to_plain_text, truncate_with_notice
from wc_text.polish import assemble_polished, build_polish_prompt, parse_polished, split_for_polish, validate_polished

Transport = Callable[[str, str], Awaitable[str]]


def default_polish_model(provider: str, chat_model: str) -> str:
    return "grok-4.20-0309-reasoning" if provider == "grok" else chat_model


def polish_model_for(provider: str, config: Dict[str, Any]) -> str:
    """The model a polish uses: the setting, else the provider's default."""
    chosen = (config.get("polishModel") or "").strip()
    return chosen or default_polish_model(provider, config.get("model") or "")


def polish_offered(provider: str, config: Dict[str, Any]) -> bool:
    """Polishing is offered only with a model other than the chat model.

    Problem: the polish exists for grok, whose narration came out choppy; a
      separate polish model rewrites it chunk by chunk (D9). On every other
      provider the polish model defaulted to the chat model itself, so
      polish_chapter had Claude re-write its own prose in small chunks
      without their context — no better than its own rewrite of the chapter,
      and a second bill for it (user decision, 2026-10-19).
    Fix: with the same model the tool is not offered (and the button is
      hidden); setting a different polish model turns it back on.
    """
    return polish_model_for(provider, config).strip() != (config.get("model") or "").strip()


async def polish_html(html: str, transport: Transport, prompt: Dict[str, str], writing_preset: Optional[str] = None,
                      stopped: Optional[asyncio.Event] = None, on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    segments = split_for_polish(html)
    chunks = [s["paras"] for s in segments if s["kind"] == "chunk"]
    system = f"{prompt['system']}\n\n{js_trim(writing_preset)}" if writing_preset and js_trim(writing_preset) else prompt["system"]
    done = 0
    if on_progress:
        on_progress(0, len(chunks))

    async def one(i: int, chunk: List[str]) -> Dict[str, Any]:
        nonlocal done
        try:
            if stopped is not None and stopped.is_set():
                return {"rewrite": None, "why": "stopped"}
            reply = await transport(system, build_polish_prompt(prompt["template"], chunk, chunks[i - 1] if i > 0 else None))
            rewrite = parse_polished(reply)
            check = validate_polished(chunk, rewrite)
            return {"rewrite": rewrite, "why": None} if check["ok"] else {"rewrite": None, "why": ", ".join(check["reasons"])}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a failed chunk keeps its draft
            return {"rewrite": None, "why": "stopped" if stopped is not None and stopped.is_set() else f"failed: {e}"}
        finally:
            done += 1
            if on_progress:
                on_progress(done, len(chunks))

    results = await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)))
    return {
        "html": assemble_polished(segments, [r["rewrite"] for r in results]),
        "chunks": len(chunks),
        "polished": sum(1 for r in results if r["rewrite"] is not None),
        "kept": [f"chunk {i + 1}: {r['why']}" for i, r in enumerate(results) if r["why"]],
        "stopped": bool(stopped is not None and stopped.is_set()),
    }


ANALYZE_SYSTEM = ("You are analyzing a book chapter-by-chapter in batches to complete a task. Each round you receive your running notes and a new batch of chapters. "
                  "Update and extend the notes with everything from this batch that matters for the task (structure, plot, entities, facts, quotes). "
                  "Output ONLY the updated complete notes as plain text. Do NOT produce a final answer.")


#: Above this many input tokens, analyze_book asks first (planAnalysis; a live run, 2026-10-10).
ANALYZE_CONFIRM_TOKENS = 200_000
_NOTES_ALLOWANCE_TOKENS = 4_000


def _rough_text(html: str) -> str:
    return re.sub(r"<[^>]*>", "", html)


def plan_analysis(task: str, chapters: List[Dict[str, str]], provider: str) -> Dict[str, int]:
    """Port of planAnalysis: calls, estimated input tokens and batch size, before any call."""
    sample = "".join(_rough_text(c["content"])[:20_000] for c in chapters[:5])
    batch_chars = analyze_batch_chars(provider, cjk_ratio_of(sample))
    batches = pack_chapters_into_batches(chapters, batch_chars)
    fixed = estimate_tokens(ANALYZE_SYSTEM) + estimate_tokens(task) + 60
    tokens = 0
    for i, batch in enumerate(batches):
        tokens += fixed + (_NOTES_ALLOWANCE_TOKENS if i > 0 else 0) + sum(estimate_tokens(_rough_text(c["content"])) for c in batch)
    return {"calls": len(batches), "inputTokens": tokens, "batchChars": batch_chars}


async def analyze_in_batches(task: str, chapters: List[Dict[str, str]], budget_chars: int, transport: Transport,
                             stopped: Optional[asyncio.Event] = None, on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    batches = pack_chapters_into_batches(chapters, budget_chars)
    notes = ""
    for i, batch in enumerate(batches):
        if stopped is not None and stopped.is_set():
            return {"notes": notes, "batches": i, "total": len(batches), "stopped": True}
        if on_progress:
            on_progress(i, len(batches))
        text = "\n\n".join(f"--- DOCUMENT: {c['title']} ---\n{truncate_with_notice(html_to_plain_text(c['content']), budget_chars)}" for c in batch)
        user = (f"TASK (do not answer it — only update the notes):\n{task}\n\n"
                f"RUNNING NOTES (from previous batches):\n{notes or '(none yet — this is the first batch)'}\n\n"
                f"NEW CHAPTERS (batch {i + 1} of {len(batches)}):\n{text}")
        try:
            reply = js_trim(await transport(ANALYZE_SYSTEM, user))
            if reply:
                notes = reply
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if stopped is not None and stopped.is_set():
                return {"notes": notes, "batches": i, "total": len(batches), "stopped": True}
            return {"notes": notes, "batches": i, "total": len(batches), "stopped": False, "failed": str(e)}
    if on_progress:
        on_progress(len(batches), len(batches))
    return {"notes": notes, "batches": len(batches), "total": len(batches), "stopped": False}


#: Pictures one look-batch sends (each ~1k tokens on the vision APIs at 1,024 px).
LOOK_BATCH_IMAGES = 8
LOOK_SYSTEM = ("You are shown pictures from a book's material, in batches, for a task. For EACH picture in this batch write exactly one line: "
               "its label as given, a colon, then what it shows that matters for the task — people, setting, action, clothing, mood, any text in it — "
               "concretely, in one or two sentences, in the language of the task. Output only those lines, one per picture, in order.")


async def look_in_batches(task: str, items: List[Dict[str, str]], transport: Callable[..., Awaitable[str]],
                          stopped: Optional[asyncio.Event] = None, on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    """view_images with a task: the pictures looked at outside the conversation, a line per picture back.

    Problem: a page or attachment can carry a hundred pictures; shown in the conversation, each
      stays and is re-sent every step (~1k tokens apiece), so a turn may see 30.
    Fix: like read with a task, batches of LOOK_BATCH_IMAGES go to the run's model as side calls; only
      their one-line descriptions come back. `items`: [{label, dataUrl}].
    """
    batches = [items[i:i + LOOK_BATCH_IMAGES] for i in range(0, len(items), LOOK_BATCH_IMAGES)]
    lines: List[str] = []
    for i, batch in enumerate(batches):
        if stopped is not None and stopped.is_set():
            return {"lines": lines, "batches": i, "total": len(batches), "stopped": True}
        if on_progress:
            on_progress(i, len(batches))
        labels = "\n".join(it["label"] for it in batch)
        user = (f"TASK:\n{task}\n\nPICTURES (batch {i + 1} of {len(batches)}): {len(batch)} attached, in this order. "
                f"Begin each line with its label exactly as written here:\n{labels}")
        try:
            reply = js_trim(await transport(LOOK_SYSTEM, user, [it["dataUrl"] for it in batch]))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return {"lines": lines, "batches": i, "total": len(batches), "stopped": bool(stopped and stopped.is_set()), "failed": str(e)}
        got = [js_trim(line) for line in reply.splitlines() if js_trim(line)]
        lines.extend(got or [f"{it['label']}: (no description came back)" for it in batch])
    if on_progress:
        on_progress(len(batches), len(batches))
    return {"lines": lines, "batches": len(batches), "total": len(batches), "stopped": False}


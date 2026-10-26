"""Request assembly for a server-side run (backend_authority.md §4.3): the
port of useChatLLM.assembleChatRequest over wc_text, with the conversation's
context state — the append-only ledger and what the model has seen — kept
per book on the server instead of in one browser tab.
"""
import json
import logging
import math
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from server_db import get_db
from wc_text.chapter_index import build_chapter_index
from wc_text.context_ledger import hash_content, ledger_chapter_ids, order_admissions_by_stability, plan_ledger_turn
from wc_text.context_selection import pinned_context_ids, select_reference_chapters
from wc_text.attachments import render_attachment_index
from wc_text.context_window import cjk_ratio_of, estimate_tokens, history_budget_chars, resolve_context_window_tokens
from wc_text.conversation_summary import build_summary_instruction, build_summary_request, parse_summary_reply, plan_conversation_summary, summary_messages
from wc_text.diff import strip_diff_markup
from wc_text.dynamic_context import build_ledger_messages, build_volatile_tail, diff_tail_parts, ledger_block, pinned_updates
from wc_text.freshness import freshness_markers
from wc_text.image_preservation import replace_images_with_placeholders
from wc_text.llm_context import build_attachments_label, html_to_plain_text, strip_chat_display_artifacts, was_turn_interrupted
from wc_text.reminders import interrupted_turn_reminder, wrap_reminder
from wc_text.policy import resolve_run_settings
from wc_text.protocol_choice import resolve_document_protocol
from wc_text.provider_profile import get_cache_profile, target_prompt_tokens, uses_responses_api
from wc_text.system_prompt import build_chat_system_prompt
from wc_text.turn_transcripts import history_window, plan_history_units, summarizable_history

MAX_LEDGER_DOC_CHARS = 20_000
REASONING_HISTORY_TURNS = 8
#: Turn transcripts kept per book (cache_continuity.md §3.1); older turns replay as their collapsed pair.
MAX_TRANSCRIPTS_PER_BOOK = 200
#: The output a summary call may use: the note is short, and the window left for the live prefix is what matters.
SUMMARY_MAX_OUTPUT_TOKENS = 8_192
logger = logging.getLogger("web_canvas.context")


def empty_state() -> Dict[str, Any]:
    return {"ledger": {"entries": []}, "seen": {}, "previousAttachedIds": [], "modelReadIds": [], "turn": 0}


def ensure_tables() -> None:
    conn = get_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS run_context (
            username TEXT NOT NULL, book_id TEXT NOT NULL, scope TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (username, book_id, scope))""")
        # A finished turn's messages as sent, for the next turn to replay (cache_continuity.md §3.1).
        conn.execute("""CREATE TABLE IF NOT EXISTS turn_transcripts (
            username TEXT NOT NULL, book_id TEXT NOT NULL, message_id TEXT NOT NULL, user_message_id TEXT,
            transcript TEXT NOT NULL, created_at TEXT NOT NULL, scope TEXT,
            PRIMARY KEY (username, book_id, message_id))""")
        # The provider|model a transcript was sent to: replayed only to the same one, since its
        # reasoning items and thinking blocks are another provider's ciphertext (a 400 there).
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(turn_transcripts)").fetchall()}
        if "scope" not in columns:
            conn.execute("ALTER TABLE turn_transcripts ADD COLUMN scope TEXT")
        conn.commit()
    finally:
        conn.close()


def save_transcript(username: str, book_id: str, message_id: str, user_message_id: Optional[str],
                    transcript: List[Dict[str, Any]], now: str, scope: str = "") -> None:
    """Store a finished turn's transcript under its reply's message id (cache_continuity.md §3.1)."""
    conn = get_db()
    try:
        conn.execute("""INSERT INTO turn_transcripts (username, book_id, message_id, user_message_id, transcript, created_at, scope)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(username, book_id, message_id) DO UPDATE SET user_message_id = excluded.user_message_id,
                        transcript = excluded.transcript, created_at = excluded.created_at, scope = excluded.scope""",
                     (username, book_id, message_id, user_message_id, json.dumps(transcript, ensure_ascii=False), now, scope))
        # Only the turns after the summary's cut are ever replayed: the oldest go.
        conn.execute("""DELETE FROM turn_transcripts WHERE username = ? AND book_id = ? AND message_id NOT IN (
                        SELECT message_id FROM turn_transcripts WHERE username = ? AND book_id = ? ORDER BY created_at DESC LIMIT ?)""",
                     (username, book_id, username, book_id, MAX_TRANSCRIPTS_PER_BOOK))
        conn.commit()
    finally:
        conn.close()


def load_transcripts(username: str, book_id: str, message_ids: List[str], scope: str = "") -> Dict[str, Dict[str, Any]]:
    """The stored transcripts of these replies sent to `scope` (provider|model): {message_id: {"userMessageId", "messages"}}."""
    ids = [i for i in message_ids if i]
    if not ids:
        return {}
    conn = get_db()
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = conn.execute(f"SELECT message_id, user_message_id, transcript FROM turn_transcripts WHERE username = ? AND book_id = ? "
                                f"AND scope = ? AND message_id IN ({','.join('?' * len(chunk))})", (username, book_id, scope, *chunk)).fetchall()
            for r in rows:
                try:
                    messages = json.loads(r["transcript"])
                except ValueError:
                    continue
                if isinstance(messages, list) and messages:
                    out[r["message_id"]] = {"userMessageId": r["user_message_id"], "messages": messages}
    finally:
        conn.close()
    return out


def delete_book_context(username: str, book_id: str) -> None:
    """With the book: its context state and transcripts."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM run_context WHERE username = ? AND book_id = ?", (username, book_id))
        conn.execute("DELETE FROM turn_transcripts WHERE username = ? AND book_id = ?", (username, book_id))
        conn.commit()
    except Exception:  # noqa: BLE001 — a book deleted before these tables existed
        pass
    finally:
        conn.close()


def load_state(username: str, book_id: str, scope: str) -> Dict[str, Any]:
    conn = get_db()
    try:
        row = conn.execute("SELECT state FROM run_context WHERE username = ? AND book_id = ? AND scope = ?",
                           (username, book_id, scope)).fetchone()
    finally:
        conn.close()
    if not row:
        return empty_state()
    try:
        state = json.loads(row["state"])
    except ValueError:
        return empty_state()
    return {**empty_state(), **state} if isinstance(state, dict) else empty_state()


def save_state(username: str, book_id: str, scope: str, state: Dict[str, Any], now: str) -> None:
    conn = get_db()
    try:
        conn.execute("""INSERT INTO run_context (username, book_id, scope, state, updated_at) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(username, book_id, scope) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at""",
                     (username, book_id, scope, json.dumps(state, ensure_ascii=False), now))
        conn.commit()
    finally:
        conn.close()


CHAT_SUMMARY_SCOPE = "chat-summary"


def load_chat_summary(username: str, book_id: str) -> Optional[Dict[str, str]]:
    """The server's copy of the conversation summary for a book (agentic_chat_loop.md §0.9)."""
    conn = get_db()
    try:
        row = conn.execute("SELECT state FROM run_context WHERE username = ? AND book_id = ? AND scope = ?",
                           (username, book_id, CHAT_SUMMARY_SCOPE)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        data = json.loads(row["state"])
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("upToId"), str) and isinstance(data.get("text"), str) and data["text"].strip():
        return {"upToId": data["upToId"], "text": data["text"]}
    return None


def save_chat_summary(username: str, book_id: str, summary: Dict[str, str], now: str) -> None:
    save_state(username, book_id, CHAT_SUMMARY_SCOPE, summary, now)


# ── a book's image numbers ────────────────────────────────────────────────────
# Problem: {{IMAGE_PLACEHOLDER_n}} was numbered afresh by every run, in the
#   order its context and reads met the images. A token the model carried from
#   an earlier turn (its transcript is replayed) could name another picture
#   this turn, or none — a picture copied into another chapter came out wrong
#   or silently missing (2026-10-18).
# Fix: the numbers belong to the book. Each image is keyed by its src, the
#   keys are kept in token order, and every run starts its registry from them;
#   an image the book never showed before takes the next number.
# "-v2": keyed by the whole tag (2026-10-25); the first scope keyed by src alone.
IMAGE_NUMBERS_SCOPE = "image-numbers-v2"
_IMG_TAG_RE = re.compile(r"<img[^>]+>")
_IMG_ATTR_RE = re.compile(r"""([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""")


def image_key(tag: str) -> str:
    """What identifies an image across runs: its tag's attributes, however the tag is spelled.

    Problem: keyed by src alone, the same picture shown twice with different attributes was one
      number restoring to the tag first seen. A chapter can end with 60x60 thumbnails
      (style="width: 60px; height: 60px; …") of pictures shown full-size elsewhere; when it comes first,
      every picture is first seen as its thumbnail — and a rewrite that carried a chapter's full-size
      pictures through their tokens wrote them back as thumbnails (2026-10-25).
    Fix: the key is the attribute set (names lower-cased, values as written, order ignored), so a
      thumbnail and the full picture are two tokens, each restoring to itself; a tag only re-spelled
      (quotes, spacing, attribute order — view_images reads tags out of a split paragraph) keeps its number.
    """
    import hashlib
    body = re.sub(r"^<img\b|/?>$", "", (tag or "").strip(), flags=re.I)
    attrs = sorted((m.group(1).lower(), next((g for g in m.groups()[1:] if g is not None), ""))
                   for m in _IMG_ATTR_RE.finditer(body))
    return hashlib.sha1(json.dumps(attrs, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def load_image_numbers(username: str, book_id: str) -> List[str]:
    state = load_state(username, book_id, IMAGE_NUMBERS_SCOPE)
    keys = state.get("keys") if isinstance(state, dict) else None
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def save_image_numbers(username: str, book_id: str, keys: List[str], now: str) -> None:
    save_state(username, book_id, IMAGE_NUMBERS_SCOPE, {"keys": keys}, now)


def stable_image_registry(keys: List[str], documents: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """A run's image registry ({placeholder, tag} by token number) from the book's numbers.

    A number whose image is in no chapter now keeps its slot with no tag (its token restores to nothing);
    an image no number names yet is appended, in book order.
    """
    registry = [{"placeholder": f"{{{{IMAGE_PLACEHOLDER_{i}}}}}", "tag": ""} for i in range(len(keys))]
    index = {k: i for i, k in enumerate(keys)}
    for doc in documents:
        for tag in _IMG_TAG_RE.findall(doc.get("content") or ""):
            key = image_key(tag)
            if key in index:
                if not registry[index[key]]["tag"]:
                    registry[index[key]]["tag"] = tag
                continue
            index[key] = len(registry)
            registry.append({"placeholder": f"{{{{IMAGE_PLACEHOLDER_{len(registry)}}}}}", "tag": tag})
    return registry


def registry_keys(registry: List[Dict[str, str]], previous: List[str]) -> List[str]:
    """The keys to keep for a registry: an empty slot keeps the key it had."""
    return [image_key(e["tag"]) if e.get("tag") else (previous[i] if i < len(previous) else "") for i, e in enumerate(registry)]


def text_seen_in_context(documents: List[Dict[str, Any]], in_context_ids: List[str]) -> Dict[str, str]:
    """Port of useChatLLM's textSeenInContext: the chapters whose whole current text the request carries (§0.11)."""
    out: Dict[str, str] = {}
    for doc_id in in_context_ids:
        doc = next((d for d in documents if d["id"] == doc_id), None)
        if doc is None:
            continue
        accepted = strip_diff_markup(doc["content"])
        if len(html_to_plain_text(accepted)) <= MAX_LEDGER_DOC_CHARS:
            out[doc_id] = hash_content(accepted)
    return out


def agent_history_note(message: Dict[str, Any]) -> str:
    trace = (message.get("agent") or {}).get("trace") if message.get("role") == "assistant" else None
    if not trace:
        return ""
    line = "; ".join(trace)
    return f"\n\n[Tools used in this turn: {line[:400] + '…' if len(line) > 400 else line}]"


def _estimate_messages(messages: List[Dict[str, Any]]) -> int:
    return sum(estimate_tokens(m.get("content") or "") + sum(estimate_tokens(c.get("argumentsText") or "") for c in m.get("toolCalls") or [])
               for m in messages)


_LEDGER_TITLE_RE = re.compile(r"^--- DOCUMENT: (.*?)(?: \(UPDATED — .*)? ---$", re.M)


def _chapter_names(documents: List[Dict[str, Any]], ledger: List[Dict[str, Any]], ids: List[str]) -> Dict[str, str]:
    """How the tail names a chapter: its number and title, or the title its ledger copy carried when it is gone."""
    numbers = {d["id"]: (i + 1, d.get("title") or "") for i, d in enumerate(documents)}
    names: Dict[str, str] = {}
    for doc_id in ids:
        if doc_id in numbers:
            n, title = numbers[doc_id]
            names[doc_id] = f'#{n} "{title}"'
            continue
        text = next((e.get("text") for e in reversed(ledger) if e["id"] == doc_id and e.get("text")), "")
        m = _LEDGER_TITLE_RE.search(text or "")
        names[doc_id] = f'"{m.group(1)}" (deleted)' if m else "a deleted chapter"
    return names


def _token_ratio(state: Dict[str, Any]) -> float:
    ratio = state.get("tokenRatio")
    return min(3.0, max(1.0, float(ratio))) if isinstance(ratio, (int, float)) and ratio > 0 else 1.0


async def assemble_request(*, provider: str, config: Dict[str, Any], prompt_text: str, images: Optional[List[str]], history: List[Dict[str, Any]],
                           documents: List[Dict[str, Any]], active_document_id: str, selected_text: str, custom_instructions: Optional[str],
                           context_window_tokens: Optional[int], state: Dict[str, Any], image_registry: List[Dict[str, str]],
                           attachments: Optional[List[Dict[str, Any]]] = None,
                           stored_summary: Optional[Dict[str, str]] = None,
                           summarize: Optional[Callable[[str, str], Awaitable[str]]] = None,
                           transcripts: Optional[Dict[str, Dict[str, Any]]] = None, turn: str = "",
                           summarize_live: Optional[Callable[[List[Dict[str, Any]]], Awaitable[str]]] = None) -> Dict[str, Any]:
    """The messages of a run's first step, and the context state after it.

    `documents` carry their content (the accepted reading is derived here).
    `state` is mutated: the ledger, the seen record and the continuity lists
    advance as the client's refs did. History past the budget is summarized
    (agentic_chat_loop.md §0.9); a new note comes back as `chatSummary` for
    the caller to store.

    With the agent tools on, the request extends the previous turn's
    (cache_continuity.md): earlier turns replay from `transcripts` (by reply
    id), the ledger stays frozen until the summary changes, the tail sends
    only what changed since the copy the model has (`turn` is this turn's
    reply id, under which those copies are recorded), and a summary is asked
    for at the end of the live conversation through `summarize_live`. Else
    `summarize(system, user)` makes it from a transcript of the dropped part.
    """
    settings = resolve_run_settings(provider, config)
    protocol = resolve_document_protocol(provider, config.get("documentProtocol"))
    continuity = bool(settings["agentTools"])
    # Agent tools on: only the chapters the writer pinned ride ahead of the history, and the model
    # reads the rest (pinned_context.md). Tools off: the scorer's prefetch, as before.
    selection = None if settings["agentTools"] else select_reference_chapters({
        "promptText": prompt_text,
        "recentHistory": [m["content"] for m in history if m.get("id") != "welcome"],
        "documents": documents,
        "activeDocumentId": active_document_id,
        "previousAttachedIds": state.get("previousAttachedIds") or [],
        "modelReadIds": state.get("modelReadIds") or [],
        "ledgerIds": [e["id"] for e in state["ledger"]["entries"]],
    })
    if selection is not None:
        state["previousAttachedIds"] = selection["attachedIds"]

    system_prompt = {"role": "system", "content": build_chat_system_prompt({
        "customInstructions": custom_instructions, "agentTools": settings["agentTools"],
        "continueAfterWrites": settings["policy"]["continueAfterWrites"], "protocol": protocol,
    })}

    # grok and OpenAI (Responses API) keep a turn's reasoning items on its message and get them back.
    replays_reasoning = uses_responses_api(provider, config.get("baseUrl") or "")
    with_reasoning = {m["id"] for m in [m for m in history if m.get("role") == "assistant" and m.get("reasoningItems")][-REASONING_HISTORY_TURNS:]}
    entries: List[Dict[str, Any]] = []
    for m in history:
        if m.get("id") == "welcome":
            continue
        entry: Dict[str, Any] = {"id": str(m.get("id") or ""), "role": m["role"],
                                 "content": strip_chat_display_artifacts(m.get("content") or "") + agent_history_note(m)}
        if m.get("images"):
            entry["images"] = m["images"]
        if replays_reasoning and m.get("id") in with_reasoning:
            entry["responseItems"] = m["reasoningItems"]
        entries.append(entry)
    replay = (transcripts or {}) if continuity else {}
    units = plan_history_units(entries, replay)
    active_content = next((d["content"] for d in documents if d["id"] == active_document_id), "")
    profile = get_cache_profile(provider)
    window_tokens = resolve_context_window_tokens(provider, config.get("model") or "", context_window_tokens)
    max_output = config.get("maxOutputTokens") or 16_384
    budget = history_budget_chars({
        # Sized in this app's estimated tokens: a provider that counts more per character (Sonnet 5.5:
        # 1.14 on Chinese prose) gets a window shrunk by the ratio its last run measured (tokenCalibration.ts).
        "contextTokens": math.floor(target_prompt_tokens(profile, window_tokens) / _token_ratio(state)),
        "maxOutputTokens": max_output,
        "fixedTokens": estimate_tokens(system_prompt["content"]) + estimate_tokens(active_content)
        + sum(math.ceil(e["chars"] * 0.9) for e in state["ledger"]["entries"]),
        "cjkRatio": cjk_ratio_of("".join(m["content"] for m in entries) or active_content),
    })
    summarizable = summarizable_history(entries, units, replay)
    plan = plan_conversation_summary(summarizable, budget, stored_summary)
    chat_summary: Optional[Dict[str, str]] = None
    if plan["needs"]:
        made: Optional[str] = None
        try:
            live = None
            if continuity and summarize_live is not None:
                # The conversation as the last request carried it, then the instruction (cache_continuity.md §3.4).
                prior = plan["needs"]["previousSummary"]
                prior_at = next((i for i, m in enumerate(entries) if stored_summary and m["id"] == stored_summary["upToId"]), 0) if prior else 0
                window = history_window(entries, units, replay, prior_at, None)
                live = [system_prompt,
                        *build_ledger_messages(documents, state["ledger"]["entries"], None, {"agentTools": settings["agentTools"]}),
                        *(summary_messages(prior) if prior else []), *window["messages"],
                        {"role": "user", "content": build_summary_instruction(entries[plan["cutIndex"]]["content"], bool(prior))}]
                if _estimate_messages(live) > int(window_tokens * 0.95) - min(max_output, SUMMARY_MAX_OUTPUT_TOKENS):
                    live = None
            if live is not None:
                made = parse_summary_reply(await summarize_live(live))
            elif summarize is not None:
                req = build_summary_request(plan["needs"]["previousSummary"], plan["needs"]["messages"])
                made = parse_summary_reply(await summarize(req["system"], req["user"]))
        except Exception as exc:  # noqa: BLE001 — a failed summary falls back to the plain cut
            logger.warning("Conversation summary failed; the oldest history is cut instead: %s", exc)
            made = None
        if made and plan["upToId"]:
            chat_summary = {"upToId": plan["upToId"], "text": made}
            plan = {**plan, "summary": made, "needs": None}
        else:
            plan = {"cutIndex": 0, "summary": None, "needs": None, "upToId": None}
    summary_prefix = summary_messages(plan["summary"]) if plan["summary"] else []
    window = history_window(entries, units, replay, plan["cutIndex"] if plan["summary"] else 0, None if plan["summary"] else budget)
    history_messages = window["messages"]
    present: List[str] = window["present"]
    if history_messages:
        history_messages[-1]["cacheHint"] = True

    state["turn"] = int(state.get("turn") or 0) + 1
    seen = state.setdefault("seen", {})
    book_order = [d["id"] for d in documents]
    docs_for_plan = []
    for d in documents:
        accepted = strip_diff_markup(d["content"])
        docs_for_plan.append({"id": d["id"], "chars": min(len(accepted), MAX_LEDGER_DOC_CHARS), "hash": hash_content(accepted)})
    hash_of = {d["id"]: d["hash"] for d in docs_for_plan}
    # Pins are budgeted on the text the ledger renders, not the HTML (pinned_context.md §2).
    wanted = selection["attachedIds"] if selection is not None else pinned_context_ids(
        [{"id": d["id"], "pinned": bool(d.get("pinned")),
          "chars": min(len(html_to_plain_text(strip_diff_markup(d["content"]))), MAX_LEDGER_DOC_CHARS) if d.get("pinned") else 0} for d in documents],
        active_document_id)
    # A pinned chapter the writer has open keeps its place in the ledger (§2.1).
    active_doc = next((d for d in documents if d["id"] == active_document_id), None)
    keep_ids = [active_doc["id"]] if selection is None and active_doc is not None and active_doc.get("pinned") else []
    desired = order_admissions_by_stability(wanted, [{"id": d["id"], "updatedAt": d.get("updatedAt")} for d in documents],
                                            book_order, active_document_id)
    by_id = {d["id"]: d for d in documents}
    plan_options = {"render": lambda doc_id, kind: ledger_block(by_id[doc_id], kind) if doc_id in by_id else "", "keepIds": keep_ids}
    pinned_block = ""
    if continuity:
        # The ledger is frozen between summaries (§3.3): rebuilt when the summary changed, or when no history
        # follows it (nothing cached after it to lose). A state from before this rule keeps its ledger.
        summary_key = plan["upToId"] if plan["summary"] else ""
        if "epochSummary" not in state:
            state["epochSummary"] = summary_key
        sent = dict(state.get("sent") or {})
        if state["epochSummary"] != summary_key or not history_messages:
            state["ledger"] = plan_ledger_turn({"entries": []}, desired, docs_for_plan, active_document_id, plan_options)["ledger"]
            state["epochSummary"] = summary_key
            sent.pop("pinned", None)
        ledger_entries = state["ledger"]["entries"]
        prefix = build_ledger_messages(documents, ledger_entries, None, {"agentTools": True})
        pins = [{"id": i, "number": book_order.index(i) + 1, "title": by_id[i].get("title") or "", "content": by_id[i]["content"],
                 "hash": hash_of[i]} for i in desired if i in by_id]
        previous_pins = sent.get("pinned") or {}
        names = _chapter_names(documents, ledger_entries, [*(e["id"] for e in ledger_entries), *previous_pins.keys()])
        pinned = pinned_updates(pins, ledger_entries, previous_pins, present, turn, names, keep_ids)
        pinned_block = pinned["block"]
        ledger_ids = ledger_chapter_ids(state["ledger"])
        attached_ids = [*(p["id"] for p in pins), *(k for k in keep_ids if k in ledger_ids)]
        # The open pinned chapter's latest copy (ledger or tail) may be older than its text now: say which is current.
        latest = None
        if keep_ids:
            live_copy = previous_pins.get(keep_ids[0])
            if live_copy and live_copy["turn"] in present and live_copy["hash"] != "unpinned":
                latest = live_copy["hash"]
            else:
                latest = next((e["hash"] for e in reversed(ledger_entries) if e["id"] == keep_ids[0]), None)
        active_copy_older = bool(latest and latest != hash_of.get(keep_ids[0]))
    else:
        ledger_plan = plan_ledger_turn(state["ledger"], desired, docs_for_plan, active_document_id, plan_options)
        attached_ids = ledger_chapter_ids(ledger_plan["ledger"])
        prefix = build_ledger_messages(documents, ledger_plan["ledger"]["entries"], None, {"agentTools": settings["agentTools"]})
        state["ledger"] = ledger_plan["ledger"]
        active_copy = next((e for e in reversed(ledger_plan["ledger"]["entries"]) if keep_ids and e["id"] == keep_ids[0]), None)
        active_copy_older = bool(active_copy and active_copy["hash"] != hash_of.get(keep_ids[0]))
    state["previousAttachedIds"] = attached_ids
    markers = freshness_markers(documents, active_document_id, attached_ids, seen, state["turn"]) if settings["agentTools"] else None
    tail_opts: Dict[str, Any] = {"agentTools": True, "markers": markers} if settings["agentTools"] else {}
    if active_copy_older:
        tail_opts["activeCopyOlder"] = True
    # The book's reference files, by reference (attachments_and_web.md §1).
    index = render_attachment_index(attachments or []) if settings["agentTools"] else ""
    if continuity:
        # Each part in full only when the copy the model has is gone or stale (§3.2).
        full = {
            "index": build_chapter_index(documents, active_document_id, {"agentTools": True, "markers": markers}),
            "attachments": index,
            "active": None if selected_text or active_doc is None else {
                "id": active_doc["id"], "number": book_order.index(active_doc["id"]) + 1, "title": active_doc.get("title") or "",
                "hash": f"{hash_of[active_doc['id']]}{'|older' if active_copy_older else ''}"},
        }
        diff = diff_tail_parts(full, state.get("sent"), present, turn)
        tail_opts["indexOverride"] = diff["index"]
        if pinned_block:
            tail_opts["pinnedBlock"] = pinned_block
        if diff["active"] is not None:
            tail_opts["activeOverride"] = diff["active"]
        index = diff["attachments"]
        state["sent"] = {**diff["sent"], "pinned": pinned["sent"]}
    tail = build_volatile_tail(documents, active_document_id, selected_text,
                               lambda html: replace_images_with_placeholders(html, image_registry), tail_opts)
    # Agent turns carry no label: the pins in the sidebar are what rides along.
    attachments_text = "" if selection is None else build_attachments_label(attached_ids, documents, selection["autoIds"])
    # The turn after a Stop says so (agentic_chat_loop.md §0.8).
    interrupted = f"\n\n{wrap_reminder(interrupted_turn_reminder())}" if was_turn_interrupted(history) else ""
    files = f"\n\n{index}" if index else ""
    final_user: Dict[str, Any] = {"role": "user", "content": f"{tail}{files}\n\nUSER REQUEST:\n{prompt_text}{interrupted}"}
    if images:
        final_user["images"] = images
    api_messages = [system_prompt, *prefix, *summary_prefix, *history_messages, final_user]
    return {
        "apiMessages": api_messages, "attachmentsText": attachments_text,
        "inContextIds": [*attached_ids, active_document_id], "protocol": protocol, "settings": settings,
        "chatSummary": chat_summary,
    }

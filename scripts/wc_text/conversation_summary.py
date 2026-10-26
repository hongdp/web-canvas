"""Port of src/utils/conversationSummary.ts — the conversation past the window, summarized."""
import re
from typing import Any, Dict, List, Optional

from .jsstr import JS_WS, js_trim
from .llm_context import IMAGE_PLACEHOLDER_TEXT

KEEP_FRACTION = 0.4
SUMMARY_RESERVE_CHARS = 6_000
SUMMARY_INPUT_CHARS = 60_000
SUMMARY_MESSAGE_CHARS = 4_000
SUMMARY_MIN_KEEP = 2

_SUMMARY_FORMAT = """Write the summary inside a single <summary>...</summary> block with exactly these numbered sections, each heading present even when its content is "None":

1. The user's requests and intent: every explicit request, with its constraints, scope and stated preferences, and what the user turned down.
2. Decisions about the book: setting, characters (names, relationships, traits), plot points and their order, the chapter plan, what is established as fact in the story.
3. Voice and style: language, tense, point of view, tone, length targets, and any example the user held up or rejected.
4. What was done: which chapters were written or changed, what each change was, and how each chapter stands now (finished, draft, awaiting the user's review).
5. Problems and how they were resolved, including corrections the user made and why.
6. All user messages, in order, each in one line.
7. Unfinished work and open questions: what the user asked for that is not done, and what is waiting on the user."""

_SUMMARY_RULES = ("Prefer tight prose and short references over verbatim quotes; names, titles and numbers verbatim. "
                  "Do not call tools, do not add an analysis before the block, and write nothing after the closing tag.")

SUMMARY_SYSTEM_PROMPT = ("You summarize the earlier part of a conversation between a writer and an assistant that edits the writer's book, "
                         "so that the assistant can continue after those earlier turns are dropped from its context. The assistant will see "
                         "the book's current text, this summary and the most recent turns verbatim; what you leave out is lost.\n\n"
                         f"{_SUMMARY_FORMAT}\n\nA prior summary, when given, is authoritative for the history it covers: carry its "
                         f"still-relevant content forward. {_SUMMARY_RULES}")

_JS_WS_RUN = re.compile(f"[{re.escape(JS_WS)}\u2028\u2029]+")


def build_summary_instruction(kept_from: Optional[str], has_prior: bool) -> str:
    """The summary asked for at the end of the live conversation (cache_continuity.md §3.4)."""
    words = js_trim(_JS_WS_RUN.sub(" ", kept_from or ""))
    preview = f"{words[:80]}…" if len(words) > 80 else words
    scope = (f'everything before the turn in which the user wrote "{preview}" will be replaced by a summary; '
             "that turn and everything after it stay verbatim") if preview else "the earlier part of it will be replaced by a summary"
    prior = (" The summary earlier in this conversation is authoritative for the history it covers: carry its still-relevant content forward."
             if has_prior else "")
    return (f"STOP — this is not a request to continue the work. This conversation is about to be compacted: {scope}. "
            "Summarize the part being replaced, so that you can continue once it is dropped. You will still see the book's current text, "
            f"the summary and the turns kept verbatim; what the summary leaves out is lost.{prior}\n\n{_SUMMARY_FORMAT}\n\n{_SUMMARY_RULES}")


_SUMMARY_OPEN = "<conversation_summary>"
_SUMMARY_CLOSE = "</conversation_summary>"
_SUMMARY_RE = re.compile(r"<summary>([\s\S]*?)</summary>", re.I)
_SUMMARY_TAG_RE = re.compile(r"</?summary>", re.I)


def _weight(m: Dict[str, Any]) -> int:
    if m.get("weight") is not None:
        return int(m["weight"])
    if js_trim(m.get("content") or ""):
        return len(m["content"])
    return len(IMAGE_PLACEHOLDER_TEXT) if m.get("images") else 0


def _tail_weight(messages: List[Dict[str, Any]], start: int) -> int:
    return sum(_weight(m) for m in messages[start:])


def _cut_for(messages: List[Dict[str, Any]], target: int) -> int:
    kept = used = 0
    cut = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        w = _weight(messages[i])
        if w == 0:
            cut = i
            continue
        if kept >= SUMMARY_MIN_KEEP and used + w > target:
            break
        used += w
        kept += 1
        cut = i
    while cut < len(messages) and messages[cut]["role"] != "user":
        cut += 1
    return cut


def plan_conversation_summary(messages: List[Dict[str, Any]], budget_chars: int, stored: Optional[Dict[str, str]]) -> Dict[str, Any]:
    none = {"cutIndex": 0, "summary": None, "needs": None, "upToId": None}
    if _tail_weight(messages, 0) <= budget_chars:
        return none
    usable = max(0, budget_chars - SUMMARY_RESERVE_CHARS)
    stored_at = next((i for i, m in enumerate(messages) if stored and m["id"] == stored["upToId"]), -1) if stored else -1
    if stored and stored_at > 0 and _tail_weight(messages, stored_at) <= usable:
        return {"cutIndex": stored_at, "summary": stored["text"], "needs": None, "upToId": stored["upToId"]}
    cut = _cut_for(messages, int(usable * KEEP_FRACTION))
    if cut <= 0 or cut >= len(messages):
        return none
    start = stored_at if (stored and 0 <= stored_at <= cut) else 0
    return {
        "cutIndex": cut, "summary": None,
        "needs": {"previousSummary": stored["text"] if start > 0 else None, "messages": messages[start:cut]},
        "upToId": messages[cut]["id"],
    }


def build_summary_request(previous_summary: Optional[str], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    lines = []
    for m in messages:
        content = js_trim(m.get("content") or "") or (IMAGE_PLACEHOLDER_TEXT if m.get("images") else "")
        capped = f"{content[:SUMMARY_MESSAGE_CHARS]}… [truncated]" if len(content) > SUMMARY_MESSAGE_CHARS else content
        lines.append(f"[{m['role']}]\n{capped}")
    omitted = 0
    total = sum(len(line) + 2 for line in lines)
    while len(lines) > 1 and total > SUMMARY_INPUT_CHARS:
        total -= len(lines[0]) + 2
        lines.pop(0)
        omitted += 1
    prior = (f"PRIOR SUMMARY (authoritative for the history before this transcript):\n{_SUMMARY_OPEN}\n{previous_summary}\n{_SUMMARY_CLOSE}\n\n"
             if previous_summary else "")
    note = f"; {omitted} earlier message{'' if omitted == 1 else 's'} omitted for length" if omitted > 0 else ""
    user = f"{prior}TRANSCRIPT ({len(lines)} message{'' if len(lines) == 1 else 's'}, oldest first{note}):\n\n" + "\n\n".join(lines) + "\n\nWrite the summary now."
    return {"system": SUMMARY_SYSTEM_PROMPT, "user": user, "omitted": omitted}


def parse_summary_reply(text: str) -> Optional[str]:
    m = _SUMMARY_RE.search(text or "")
    inner = js_trim(m.group(1) if m else _SUMMARY_TAG_RE.sub("", text or ""))
    return inner or None


def summary_messages(summary: str) -> List[Dict[str, Any]]:
    return [
        {"role": "user", "content": f"EARLIER IN THIS CONVERSATION (summarized; the messages after this are verbatim):\n{_SUMMARY_OPEN}\n{summary}\n{_SUMMARY_CLOSE}",
         "cacheHint": True},
        {"role": "assistant", "content": "Understood. I will continue from this summary."},
    ]

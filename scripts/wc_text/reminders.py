"""Port of src/agent/reminders.ts — the loop's automated context."""
import re
from typing import Any, Dict, List

from .plan import render_plan, unfinished_plan_items

_REMINDER_TAG_RE = re.compile(r"<(/?system-reminder\b)", re.I)

REMINDER_TAG = "system-reminder"
REPEAT_NUDGE_STEPS = 3
REPEAT_PAUSE_STEPS = 6
DEFAULT_LONG_REASONING_TOKENS = 0
PLAN_NUDGE_BUDGET = 2
REMINDERS_ARE_CONTEXT = "<system-reminder> blocks inside tool results are automated context from the editor, not messages from the user."


def escape_reminder_tags(text: str) -> str:
    return _REMINDER_TAG_RE.sub(r"&lt;\1", text)


def wrap_reminder(text: str) -> str:
    return f"<{REMINDER_TAG}>\n{escape_reminder_tags(text)}\n</{REMINDER_TAG}>"


def append_reminders(messages: List[Dict[str, Any]], reminders: List[str]) -> List[Dict[str, Any]]:
    if not reminders or not messages:
        return messages
    last = messages[-1]
    block = "\n".join(wrap_reminder(r) for r in reminders)
    content = f"{last['content']}\n\n{block}" if last.get("content") else block
    return [*messages[:-1], {**last, "content": content}]


def repeat_nudge(calls: List[str], run_len: int) -> str:
    what = f"the same call ({calls[0]})" if len(calls) == 1 else f"the same calls ({', '.join(calls)})"
    tail = "" if run_len >= REPEAT_PAUSE_STEPS - REPEAT_NUDGE_STEPS + 1 else f"This run is paused for the user if the identical calls reach {REPEAT_PAUSE_STEPS}."
    return (f"You have made {what} with the same arguments {run_len} times in a row, and nothing was written in between. The answer will not change by asking again. "
            "Do something else with what you already have: write the chapter, or read a different part of the book. "
            "If you cannot make progress, stop and tell the user what you are missing. " + tail)


def long_reasoning_reminder(tokens: int) -> str:
    return (f"Your previous step used a very long hidden reasoning trace (about {tokens:,} tokens) and wrote nothing. "
            "From here on, do not reason at length between tool results: read the result, decide the single next action, and emit it, with a few sentences of reasoning at most. "
            "Verify by reading or searching the book, never by thinking at length. Plan a chapter in the reply that writes it.")


def plan_reminder(plan: List[Dict[str, str]]) -> str:
    return render_plan(plan)


def carried_plan_reminder(plan: List[Dict[str, str]]) -> str:
    """Port of carriedPlanReminder: the previous turn's unfinished plan, on the next turn's first message."""
    return (f"Your plan from the previous turn is not finished:\n{render_plan(plan)}\n"
            "If this message continues that work, carry on with the next item and keep the plan up to date. "
            "If it asks for something else, mark the items that no longer apply dropped with the plan tool.")


def plan_unfinished_nudge(plan: List[Dict[str, str]]) -> str:
    left = unfinished_plan_items(plan)
    return (f"Your plan still has {len(left)} unfinished item{'' if len(left) == 1 else 's'}:\n" + "\n".join(f"- {i['title']}" for i in left) + "\n"
            "Continue with the next one in your next reply. If an item no longer applies, mark it dropped with the plan tool and say why; a reply with no action ends the turn only once every item is done or dropped.")


def plan_not_written_note(title: str) -> str:
    return f'"{title}" was marked done, but nothing has been written to the book since it started. It stays in_progress: do the write in this reply, then mark it done.'


def html_read_nudge(trace: str) -> str:
    return (f"Your last read of a chapter's HTML ({trace}) is the step before an edit, and no change followed it. "
            "Make the edit now, or say in your reply why it is not needed.")


def user_edited_reminder(chapters: List[Dict[str, Any]]) -> str:
    names = ", ".join(f'#{c["number"]} "{c["title"]}"' for c in chapters)
    one = len(chapters) == 1
    it = "it" if one else "them"
    return (f"The user changed {names} while you were working. What you read of {it} is out of date: read {it} again before relying on {it} or writing to {it}, and keep the user's edits.")


def structure_changed_reminder(outline: str) -> str:
    return f"The user changed the book's chapter list while you were working. It is now:\n{outline}\nChapter numbers in your earlier replies may be off; use these."


def queued_request_reminder(count: int) -> str:
    many = "another request that is" if count == 1 else f"{count} more requests that are"
    return f"The user has sent {many} waiting for this turn to finish. Finish the work of this turn; do not start anything beyond it."


def interrupted_turn_reminder() -> str:
    return ("The user stopped your previous turn before it finished. What that reply described as done may not have happened: the book holds only what was actually written, and nothing more. "
            "If this message asks for a reply, answer it first. Then take up what remains of the earlier request only if this message still wants it.")


def steer_message(text: str) -> str:
    return (wrap_reminder("The user sent this message while you were working. If it asks for a reply, answer it first. "
                          "Then continue the work of this turn with it taken into account, and do not start anything beyond it.")
            + f"\n\nUSER MESSAGE:\n{text}")


LOOKUP_NUDGE_STEPS = 8


def lookup_streak_nudge(steps: int, analyze_offered: bool) -> str:
    return (f"The last {steps} steps only looked things up and wrote nothing, and every step sends the whole conversation again. "
            "Check several places in ONE call: read with parts, grep with patterns"
            + ("; for a question that spans many chapters, read them with a task and get notes back" if analyze_offered else "")
            + ". Then write.")


def unbacked_claim_nudge(facts: Dict[str, int]) -> str:
    writes, reads, left = facts["writes"], facts["reads"], facts["planLeft"]
    return (f"Editor facts for this turn: {writes} write{'' if writes == 1 else 's'} reached the book, {reads} chapter{'' if reads == 1 else 's'} read, "
            f"{left} plan item{'' if left == 1 else 's'} left. "
            "Your reply says you wrote or changed something, but nothing was written this turn. "
            "Either make the write now, in this reply, or correct the claim and tell the user what was actually done.")

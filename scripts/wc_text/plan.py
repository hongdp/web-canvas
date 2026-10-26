"""Port of src/utils/plan.ts — the run's checklist."""
from typing import Any, Dict, List, Optional, Union

STATUSES = ("pending", "in_progress", "done", "dropped")
_MARK = {"pending": "☐", "in_progress": "▶", "done": "☑", "dropped": "✕"}


def _parse_item(raw: Any, index: int) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    title = raw["title"].strip() if isinstance(raw.get("title"), str) else raw["content"].strip() if isinstance(raw.get("content"), str) else ""
    rid = raw.get("id")
    if isinstance(rid, str) and rid.strip():
        item_id = rid.strip()
    elif isinstance(rid, (int, float)) and not isinstance(rid, bool):
        item_id = str(int(rid)) if float(rid).is_integer() else str(rid)
    else:
        item_id = f"p{index + 1}" if title else ""
    if not item_id:
        return None
    status = raw.get("status")
    if status not in STATUSES:
        status = "done" if status == "completed" else "dropped" if status == "cancelled" else "pending"
    return {"id": item_id, "title": title, "status": status}


def apply_plan_update(current: List[Dict[str, str]], raw_items: Any, merge: Optional[bool]) -> Union[List[Dict[str, str]], str]:
    items = [i for i in (_parse_item(r, n) for n, r in enumerate(raw_items if isinstance(raw_items, list) else [])) if i is not None]
    if not items:
        return "no items were given (each needs a title; a status update needs an id)"
    ids = set()
    for item in items:
        if item["id"] in ids:
            return f'duplicate item id "{item["id"]}"'
        ids.add(item["id"])
    by_id = {i["id"]: i for i in current}
    status_only = bool(current) and all(i["id"] in by_id and not i["title"] for i in items)
    if merge or status_only:
        # A new id with a title is appended (applyPlanUpdate in plan.ts).
        for item in items:
            if item["id"] not in by_id and not item["title"]:
                return f'item "{item["id"]}" is not in the plan; give it a title to add it, or send the whole list'
        out = []
        for i in current:
            update = next((u for u in items if u["id"] == i["id"]), None)
            out.append({**i, "status": update["status"], **({"title": update["title"]} if update["title"] else {})} if update else i)
        return out + [i for i in items if i["id"] not in by_id]
    if any(not i["title"] for i in items):
        return "an item has no title"
    return items


def previous_turn_plan(messages: List[Dict[str, Any]], assistant_message_id: str) -> Optional[List[Dict[str, str]]]:
    """Port of previousTurnPlan: the plan the turn before this one left unfinished, or None."""
    at = next((n for n, m in enumerate(messages) if m.get("id") == assistant_message_id), -1)
    before = messages if at == -1 else messages[:at]
    prev = next((m for m in reversed(before) if m.get("role") == "assistant"), None)
    agent = (prev or {}).get("agent")
    plan = agent.get("plan") if isinstance(agent, dict) else None
    if not isinstance(plan, list) or not unfinished_plan_items(plan):
        return None
    return [{"id": i["id"], "title": i["title"], "status": i["status"]} for i in plan]


def next_plan_item(items: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    return next((i for i in items if i["status"] == "in_progress"), None) or next((i for i in items if i["status"] == "pending"), None)


def unfinished_plan_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [i for i in items if i["status"] in ("pending", "in_progress")]


def render_plan(items: List[Dict[str, str]]) -> str:
    done = sum(1 for i in items if i["status"] in ("done", "dropped"))
    lines = "\n".join(f"{_MARK[i['status']]} {i['title']}" for i in items)
    nxt = next_plan_item(items)
    return f"PLAN ({done}/{len(items)} done):\n{lines}" + (f"\nNext: {nxt['title']}" if nxt else "\nAll items are done.")

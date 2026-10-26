"""Port of src/agent/policy.ts — when a run continues, and when it ends."""
import math
from typing import Any, Dict, List, Optional

from .stream_handlers import MAX_NO_ACTION_RETRIES
from .text import detect_failed_document_update

DEFAULT_BUDGETS = {"maxSteps": 6, "maxCorrective": MAX_NO_ACTION_RETRIES}
DEFAULT_POLICY = {"continueAfterWrites": False, "feedBackFailedWrites": False}


def default_max_steps(provider: str) -> int:
    return 10 if provider in ("ollama", "runpod") else 6


def default_continue_after_writes(provider: str) -> bool:
    return True


def resolve_run_settings(provider: str, config: Optional[Dict[str, Any]], can_continue: bool = True) -> Dict[str, Any]:
    config = config or {}
    agent_tools = config.get("agentTools") is not False
    configured = config.get("agentMaxSteps")
    if isinstance(configured, (int, float)) and not isinstance(configured, bool) and math.isfinite(configured) and configured >= 0:
        max_steps = math.floor(configured)
    else:
        max_steps = default_max_steps(provider)
    continue_after = config.get("continueAfterWrites")
    if continue_after is None:
        continue_after = default_continue_after_writes(provider)
    return {
        "agentTools": agent_tools,
        "budgets": {"maxSteps": max_steps if can_continue else 1, "maxCorrective": MAX_NO_ACTION_RETRIES},
        "policy": {
            "continueAfterWrites": bool(can_continue and agent_tools and continue_after),
            "feedBackFailedWrites": agent_tools,
        },
    }


def detect_step_failure(params: Dict[str, Any]) -> Optional[str]:
    if params.get("hadNativeCalls") or params.get("markupKind") != "chat":
        return None
    failure = detect_failed_document_update(params["text"])
    if failure == "undeclared" and params["writeProtocol"] == "tools":
        return None
    if params.get("wroteThisRun") and failure != "malformed":
        return None
    return failure


def steps_left(budgets: Dict[str, Any], steps_taken: int) -> float:
    return budgets["maxSteps"] - steps_taken if budgets["maxSteps"] > 0 else math.inf


def decide_after_step(params: Dict[str, Any]) -> Dict[str, Any]:
    executed: List[Dict[str, Any]] = params["executed"]
    budgets, policy = params["budgets"], params["policy"]
    wants_more = any(e["kind"] != "write" for e in executed)
    writes = [e for e in executed if e["kind"] == "write"]
    failed_write = any(not e["result"]["ok"] and e["result"].get("retryable", True) for e in writes)
    corrective_left = budgets["maxCorrective"] - params["correctiveUsed"]

    if not executed:
        return {"action": "end", "reason": "answered"}
    would_continue = (wants_more or (failed_write and policy["feedBackFailedWrites"] and corrective_left > 0)
                      or (not failed_write and policy["continueAfterWrites"]))
    if not would_continue:
        return {"action": "end", "reason": "corrective_exhausted" if failed_write and policy["feedBackFailedWrites"] else "writes_done"}
    left = steps_left(budgets, params["stepsTaken"])
    if left <= 0:
        return {"action": "end", "reason": "step_limit" if wants_more or (failed_write and policy["feedBackFailedWrites"]) else "writes_done"}
    return {"action": "continue", "corrective": (not wants_more) and failed_write, "final": left == 1}

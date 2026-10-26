"""Port of src/utils/reasoningEffort.ts — which effort levels a model takes, and how a Claude model is asked to think.

The backend resolves the level itself (the client forwards the user's raw setting), so a level a model does
not take is never sent and the app's default applies when the user never chose one.
"""
import math
import re
from typing import Any, Dict, List, Optional

REASONING_EFFORTS = ["default", "minimal", "low", "medium", "high", "xhigh"]
DEFAULT_REASONING_EFFORT = "low"

_SUPPORT_TABLE: Dict[str, List[Dict[str, Any]]] = {
    "grok": [
        {"match": re.compile(r"grok-4\.20", re.I), "levels": ["default"]},
        {"match": re.compile(r"grok-4\.(?:3|6|7)(?!\d)", re.I), "levels": ["default", "low", "medium", "high", "xhigh"]},
        {"match": re.compile(r"grok-4\.5", re.I), "levels": ["default", "low", "medium", "high"]},
        {"match": re.compile(r"grok-3-mini", re.I), "levels": ["default", "low", "high"]},
    ],
    "openai": [
        {"match": re.compile(r"^gpt-5(?:-mini|-nano)?(?:-\d{4}-\d{2}-\d{2})?$", re.I), "levels": ["default", "minimal", "low", "medium", "high"]},
        {"match": re.compile(r"^gpt-5\.1(?!\d)", re.I), "levels": ["default", "low", "medium", "high"]},
        {"match": re.compile(r"^gpt-(?:5\.(?:[2-9]|\d\d)|[6-9])", re.I), "levels": ["default", "low", "medium", "high", "xhigh"]},
        {"match": re.compile(r"^o[1-9]", re.I), "levels": ["default", "low", "medium", "high"]},
    ],
    "anthropic": [
        {"match": re.compile(r"claude-(?:opus|sonnet|haiku|fable)-[5-9]|claude-opus-4-[7-9]", re.I),
         "levels": ["default", "low", "medium", "high", "xhigh"]},
        {"match": re.compile(r"claude-(?:opus|sonnet)-4-6", re.I), "levels": ["default", "low", "medium", "high"]},
        {"match": re.compile(r"claude-(?:3-7|opus-4|sonnet-4|haiku-4)", re.I), "levels": ["default", "low", "medium", "high"]},
    ],
    "gemini": [
        {"match": re.compile(r"gemini-(?:[3-9]|\d\d)|gemini-(?:flash|flash-lite|pro)-latest", re.I), "levels": ["default", "low", "medium", "high"]},
        {"match": re.compile(r"gemini-2\.5", re.I), "levels": ["default", "minimal", "low", "medium", "high"]},
    ],
    "ollama": [],
    "runpod": [],
    "moonshot": [
        {"match": re.compile(r"kimi-k3", re.I), "levels": ["default", "low", "high", "xhigh"]},
    ],
}

_THINKING_BUDGET_TOKENS = {"minimal": 512, "low": 1024, "medium": 4096, "high": 16384, "xhigh": 32768}

_ADAPTIVE_RE = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-[5-9]|claude-(?:opus|sonnet)-4-[6-9]", re.I)


def supported_reasoning_efforts(provider: str, model: str) -> List[str]:
    for entry in _SUPPORT_TABLE.get(provider, []):
        if entry["match"].search(model or ""):
            return list(entry["levels"])
    return ["default"]


def resolve_reasoning_effort(provider: str, model: str, effort: Optional[str]) -> Optional[str]:
    """The level worth sending: the app's default when never chosen, None for 'default' or a level the model does not take."""
    chosen = effort if effort is not None else DEFAULT_REASONING_EFFORT
    if chosen == "default":
        return None
    return chosen if chosen in supported_reasoning_efforts(provider, model) else None


def reasoning_budget_tokens(effort: str) -> int:
    return _THINKING_BUDGET_TOKENS[effort]


def anthropic_thinking(model: str, effort: Optional[str], max_tokens: int) -> Dict[str, Any]:
    """Port of anthropicThinking: adaptive thinking with an effort on Opus 4.6+/5.x, a token budget before that."""
    if not effort:
        return {}
    if _ADAPTIVE_RE.search(model or ""):
        return {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "low" if effort == "minimal" else effort}}
    budget = min(_THINKING_BUDGET_TOKENS[effort], math.floor(max_tokens / 2))
    return {"thinking": {"type": "enabled", "budget_tokens": budget}} if budget >= 1024 else {}


def moonshot_thinking(model: str, effort: Optional[str]) -> Dict[str, Any]:
    """Port of moonshotThinking: kimi-k3's reasoning_effort (xhigh sent as max), k2.6's thinking kept across turns."""
    name = model or ""
    if re.search(r"kimi-k3", name, re.I):
        return {"reasoning_effort": "max" if effort == "xhigh" else effort} if effort else {}
    if re.search(r"kimi-k2\.6", name, re.I):
        return {"thinking": {"type": "enabled", "keep": "all"}}
    return {}


def gemini_thinking(model: str, effort: Optional[str]) -> Optional[Dict[str, Any]]:
    """Port of geminiThinking: a level on Gemini 3.x, a budget on 2.5, thoughts included; None when it does not think."""
    if len(supported_reasoning_efforts("gemini", model)) <= 1:
        return None
    if not effort:
        return {"includeThoughts": True}
    if re.search(r"gemini-2\.5", model or "", re.I):
        return {"thinkingBudget": _THINKING_BUDGET_TOKENS[effort], "includeThoughts": True}
    return {"thinkingLevel": effort, "includeThoughts": True}

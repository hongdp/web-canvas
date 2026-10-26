"""Port of src/utils/contextWindow.ts — the model's window and the history budget."""
import math
import re
from typing import Dict, Optional

MODEL_CONTEXT_TOKENS = [
    ("gpt-4.1", 1_047_576), ("gpt-4o", 128_000), ("gpt-4-turbo", 128_000), ("gpt-4", 8_192), ("gpt-3.5", 16_385),
    ("o1", 200_000), ("o3", 200_000),
    ("claude-opus-5", 1_000_000), ("claude-sonnet-5", 1_000_000), ("claude-haiku-5", 1_000_000), ("claude-fable-5", 1_000_000), ("claude-mythos-5", 1_000_000),
    ("claude-opus-4-6", 1_000_000), ("claude-opus-4-7", 1_000_000), ("claude-opus-4-8", 1_000_000), ("claude-sonnet-4-6", 1_000_000),
    ("claude-opus-4", 200_000), ("claude-sonnet-4", 200_000), ("claude-3", 200_000), ("claude-", 200_000),
    ("gemini-1.5-pro", 2_097_152), ("gemini-1.5", 1_048_576), ("gemini-2", 1_048_576), ("gemini-", 1_048_576),
    ("grok-4", 256_000), ("grok-3", 131_072), ("grok-", 131_072),
    ("kimi-k3", 1_048_576), ("kimi-", 262_144),
]
PROVIDER_FALLBACK_TOKENS = {"openai": 128_000, "anthropic": 200_000, "gemini": 1_048_576, "grok": 131_072, "moonshot": 262_144, "ollama": 32_768}
DEFAULT_CONTEXT_TOKENS = 32_768
MIN_HISTORY_CHARS = 4_000
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def resolve_context_window_tokens(provider: str, model: str, discovered: Optional[int] = None) -> int:
    if discovered and discovered > 0:
        return discovered
    name = (model or "").lower()
    best = None
    best_len = -1
    for prefix, tokens in MODEL_CONTEXT_TOKENS:
        if prefix in name and len(prefix) > best_len:
            best, best_len = tokens, len(prefix)
    if best is not None:
        return best
    return PROVIDER_FALLBACK_TOKENS.get(provider, DEFAULT_CONTEXT_TOKENS)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return math.ceil(cjk + (len(text) - cjk) / 4)


def tokens_to_chars(tokens: int, cjk_ratio: float) -> int:
    clamped = min(1.0, max(0.0, cjk_ratio))
    return math.floor(tokens * (clamped * 1 + (1 - clamped) * 4))


def history_budget_chars(inp: Dict) -> int:
    context_tokens = inp["contextTokens"]
    safety = math.ceil(context_tokens * 0.1)
    available = context_tokens - inp["maxOutputTokens"] - inp["fixedTokens"] - safety
    if available <= 0:
        return MIN_HISTORY_CHARS
    return max(MIN_HISTORY_CHARS, tokens_to_chars(available, inp["cjkRatio"]))


def cjk_ratio_of(text: str) -> float:
    if not text:
        return 0
    return len(_CJK_RE.findall(text)) / len(text)

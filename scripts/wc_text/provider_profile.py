"""Port of src/utils/providerProfile.ts — what each provider's prompt cache does."""
import re
from typing import Any, Dict

PROFILES: Dict[str, Dict[str, Any]] = {
    "grok": {"mode": "automatic", "maxBreakpoints": 0, "routing": {"kind": "header", "name": "x-grok-conv-id"},
             "longContextThreshold": 200_000, "cachedTokensPath": ["prompt_tokens_details", "cached_tokens"],
             "exclusiveCache": False, "windowDiscovered": False},
    "ollama": {"mode": "automatic", "maxBreakpoints": 0, "exclusiveCache": True, "windowDiscovered": True},
    "anthropic": {"mode": "explicit", "maxBreakpoints": 4, "cachedTokensPath": ["cache_read_input_tokens"],
                  "exclusiveCache": False, "windowDiscovered": False},
    "openai": {"mode": "automatic", "maxBreakpoints": 0, "routing": {"kind": "header", "name": "prompt-cache-key"},
               "cachedTokensPath": ["prompt_tokens_details", "cached_tokens"], "exclusiveCache": False, "windowDiscovered": False},
    "gemini": {"mode": "automatic", "maxBreakpoints": 0, "cachedTokensPath": ["cachedContentTokenCount"],
               "exclusiveCache": False, "windowDiscovered": False},
    "moonshot": {"mode": "automatic", "maxBreakpoints": 0, "cachedTokensPath": ["prompt_tokens_details", "cached_tokens"],
                 "exclusiveCache": False, "windowDiscovered": False},
}
UNKNOWN_PROFILE: Dict[str, Any] = {"mode": "none", "maxBreakpoints": 0, "exclusiveCache": False, "windowDiscovered": False}


def get_cache_profile(provider: str) -> Dict[str, Any]:
    return PROFILES.get(provider, UNKNOWN_PROFILE)


def target_prompt_tokens(profile: Dict[str, Any], context_window_tokens: int) -> int:
    threshold = profile.get("longContextThreshold")
    if threshold and threshold < context_window_tokens:
        return threshold
    return context_window_tokens


_OPENAI_HOST_RE = re.compile(r"^https://api\.openai\.com(?:[/:?#]|$)", re.I)


def uses_responses_api(provider: str, base_url: str) -> bool:
    """Port of usesResponsesApi: grok, and OpenAI at its own host, run on a Responses API."""
    if provider == "grok":
        return True
    return provider == "openai" and bool(_OPENAI_HOST_RE.match(base_url or ""))

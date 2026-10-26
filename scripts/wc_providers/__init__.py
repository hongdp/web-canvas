"""Per-provider wire formats, one module per API family (docs/design.md, 2026-10-11).

`dispatch` is the one entry: the Responses API for grok and OpenAI at its own
host, Chat Completions for the rest of the OpenAI-shaped servers, Gemini and
Anthropic their own. server_generation binds it as `_dispatch_provider`, the
name the run tests replace with a scripted provider.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from wc_text.provider_profile import uses_responses_api

from . import anthropic, gemini, openai_compat, responses
from .common import ProviderError

if TYPE_CHECKING:
    from server_generation import GenerationJob

#: The providers on plain Chat Completions (openai at any other host too, see uses_responses_api).
CHAT_COMPLETIONS_PROVIDERS = ("openai", "ollama", "runpod", "moonshot")


async def dispatch(job: "GenerationJob", provider: str, config: Dict[str, Any], messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if uses_responses_api(provider, config.get("baseUrl") or ""):
        return await responses._stream_responses(job, config, messages, provider)
    if provider in CHAT_COMPLETIONS_PROVIDERS:
        return await openai_compat._stream_openai(job, config, messages, provider)
    if provider == "gemini":
        return await gemini._stream_gemini(job, config, messages)
    if provider == "anthropic":
        return await anthropic._stream_anthropic(job, config, messages)
    raise ProviderError(f"Unsupported LLM provider: {provider}")

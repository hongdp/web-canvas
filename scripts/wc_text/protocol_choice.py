"""Port of src/utils/protocolChoice.ts — tools or markup, per provider."""
from typing import Optional

COARSE_TOOL_STREAMING = {"grok", "gemini"}
NEEDS_TOOLS = {"ollama", "runpod"}


def resolve_document_protocol(provider: str, protocol: Optional[str]) -> str:
    if protocol in ("tools", "markup"):
        return protocol
    if provider in NEEDS_TOOLS:
        return "tools"
    if provider in COARSE_TOOL_STREAMING:
        return "markup"
    return "markup"

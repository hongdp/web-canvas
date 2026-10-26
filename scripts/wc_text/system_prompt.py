"""Port of src/utils/systemPrompt.ts — buildChatSystemPrompt.

The fixed texts come from data/prompt_texts.json, written from the TypeScript
constants by src/parity/__tests__/parity.test.ts (`promptTexts`). The
assembly — which sections, in what order, with the user's preset between
them — is ported here. One source for the bytes: a one-character drift would
cost every cached prefix on grok.
"""
import json
import os
from typing import Dict, Optional

from .llm_context import _js_trim

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prompt_texts.json")
_texts: Optional[Dict] = None


def prompt_texts() -> Dict:
    global _texts
    if _texts is None:
        with open(_DATA, encoding="utf-8") as fh:
            _texts = json.load(fh)
    return _texts


def build_chat_system_prompt(options: Dict) -> str:
    t = prompt_texts()
    protocol = options["protocol"]
    agent_tools = bool(options.get("agentTools"))
    mode = "continue" if options.get("continueAfterWrites") else "stop"
    agent_markup = protocol == "markup" and agent_tools
    if agent_markup:
        sections = [t["agentMarkupPrompt"][mode]]
    else:
        sections = [t["toolRules"] if protocol == "tools" else t["markupRules"]]
        if agent_tools:
            sections.append(t["agentRules"][protocol][mode])
    custom = _js_trim(options.get("customInstructions") or "")
    if custom:
        sections.append(f"USER'S CUSTOM WRITING INSTRUCTIONS (apply these to all content you write):\n{custom}")
    sections.append(t["agentMarkupFormatReminder"] if agent_markup else t["formatReminderTools"] if protocol == "tools" else t["formatReminderMarkup"])
    return "\n\n".join(sections)

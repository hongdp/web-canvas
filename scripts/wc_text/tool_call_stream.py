"""Port of src/utils/toolCallStream.ts — reading a tool call while it arrives."""
import json
from typing import Any, Dict, List, Optional

from .jsstr import js_is_space

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def partial_string_argument(arguments_text: str, key: str) -> Optional[str]:
    text = arguments_text or ""
    marker = f'"{key}"'
    at = text.find(marker)
    if at == -1:
        return None
    i = at + len(marker)
    while i < len(text) and js_is_space(text[i]):
        i += 1
    if i >= len(text) or text[i] != ":":
        return None
    i += 1
    while i < len(text) and js_is_space(text[i]):
        i += 1
    if i >= len(text) or text[i] != '"':
        return None
    i += 1
    out = []
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text):
                break
            nxt = text[i + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)


def finish_tool_calls(accumulators: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for _, acc in sorted(accumulators.items()):
        if not acc.get("name"):
            continue
        args = None
        try:
            parsed = json.loads(acc.get("argumentsText") or "{}")
            if isinstance(parsed, dict):
                args = parsed
        except ValueError:
            args = None
        finished: Dict[str, Any] = {"name": acc["name"], "args": args, "argumentsText": acc.get("argumentsText", "")}
        if acc.get("id") is not None:
            finished["id"] = acc["id"]
        if acc.get("signature"):
            finished["signature"] = acc["signature"]
        out.append(finished)
    return out


def call_signature(name: str, args: Any, arguments_text: Optional[str] = None) -> str:
    """The name plus the arguments with keys sorted at every level (JSON.stringify's compact form)."""
    def canonical(value: Any) -> Any:
        if isinstance(value, list):
            return [canonical(v) for v in value]
        if isinstance(value, dict):
            return {k: canonical(value[k]) for k in sorted(value)}
        return value
    if isinstance(args, dict):
        return f"{name}:{json.dumps(canonical(args), ensure_ascii=False, separators=(',', ':'))}"
    return f"{name}:{arguments_text or ''}"

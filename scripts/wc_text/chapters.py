"""Port of src/agent/chapters.ts — how the model names a chapter."""
import math
import re
from typing import Any, Dict, List, Union

from .jsstr import js_trim

MAX_LISTED = 12
_NUMERIC_RE = re.compile(r"^\s*#?\d+\s*$")


def cite_chapter(c: Dict[str, Any]) -> str:
    return f'#{c["number"]} "{c["title"]}"'


def _list_chapters(chapters: List[Dict[str, Any]]) -> str:
    lines = [f'#{i + 1} "{c["title"]}"' for i, c in enumerate(chapters[:MAX_LISTED])]
    if len(chapters) > MAX_LISTED:
        lines.append(f"… {len(chapters) - MAX_LISTED} more (see the CHAPTER INDEX)")
    return "\n".join(lines)


def _js_number_string(ref: Any) -> str:
    if isinstance(ref, float):
        return str(int(ref)) if ref.is_integer() and abs(ref) < 1e21 else repr(ref)
    return str(ref)


def resolve_chapter(ref: Any, chapters: List[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    def at(index: int) -> Dict[str, Any]:
        return {**chapters[index], "number": index + 1}

    is_number = isinstance(ref, (int, float)) and not isinstance(ref, bool)
    if is_number or (isinstance(ref, str) and _NUMERIC_RE.match(ref)):
        n = ref if is_number else int(re.sub(r"[#\s]", "", ref))
        if (isinstance(n, int) or (isinstance(n, float) and n.is_integer())) and 1 <= n <= len(chapters):
            return at(int(n) - 1)
        shown = js_trim(_js_number_string(ref)) if is_number else js_trim(ref)
        return f"There is no chapter #{shown}; the book has {len(chapters)} chapters."

    if not isinstance(ref, str) or not js_trim(ref):
        return "No chapter was named. Pass its number from the CHAPTER INDEX or its exact title."

    wanted = js_trim(ref).lower()
    for i, c in enumerate(chapters):
        if js_trim(c["title"]).lower() == wanted:
            return at(i)
    partial = [(c, i) for i, c in enumerate(chapters) if wanted in c["title"].lower()]
    if len(partial) == 1:
        return at(partial[0][1])
    if len(partial) > 1:
        return (f'"{ref}" matches {len(partial)} chapters — pass a number instead:\n'
                + "\n".join(f'#{i + 1} "{c["title"]}"' for c, i in partial[:MAX_LISTED]))
    return f'No chapter is titled "{ref}". The chapters are:\n{_list_chapters(chapters)}'


def _unused() -> None:  # keeps math imported for callers that expect it
    return math.inf and None

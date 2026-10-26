"""Port of src/utils/textReplace.ts — find-and-replace in a chapter's text, never its markup."""
import re
from typing import Any, Dict

SAMPLE_LIMIT = 3
_CONTEXT_CHARS = 12
_TAG_RE = re.compile(r"(<[^>]*>)")


def escape_html_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sample(text: str, at: int, length: int, replacement: str) -> str:
    before = text[max(0, at - _CONTEXT_CHARS):at]
    after = text[at + length:at + length + _CONTEXT_CHARS]
    return f"…{before}[{text[at:at + length]}→{replacement}]{after}…"


def expand_replacement(template: str, match: str, groups: list) -> str:
    """Port of expandReplacement: $$, $&, $n / $nn (two digits when that group exists)."""
    out = []
    i = 0
    while i < len(template):
        c = template[i]
        if c != "$" or i + 1 >= len(template):
            out.append(c)
            i += 1
            continue
        n = template[i + 1]
        if n == "$":
            out.append("$")
            i += 2
            continue
        if n == "&":
            out.append(match)
            i += 2
            continue
        pair = template[i + 1:i + 3]
        two = int(pair) if len(pair) == 2 and pair.isascii() and pair.isdigit() else -1
        if 1 <= two <= len(groups):
            out.append(groups[two - 1] or "")
            i += 3
            continue
        one = int(n) if n.isascii() and n.isdigit() else -1
        if 1 <= one <= len(groups):
            out.append(groups[one - 1] or "")
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def replace_in_html_text(html: str, find: str, replacement: str, regex: bool = False) -> Dict[str, Any]:
    if not find:
        return {"html": html, "count": 0, "samples": [], "error": "find was empty"}
    if regex:
        try:
            pattern = re.compile(find)
        except re.error:
            return {"html": html, "count": 0, "samples": [], "error": "invalid regular expression (check brackets, parentheses and escapes)"}
        if pattern.search("") is not None:
            return {"html": html, "count": 0, "samples": [], "error": "the pattern matches empty text; it would insert the replacement everywhere"}
    else:
        pattern = re.compile(re.escape(escape_html_text(find)))
        literal = escape_html_text(replacement)
    count = 0
    samples = []
    parts = _TAG_RE.split(html)
    for i in range(0, len(parts), 2):
        text = parts[i]
        if not text:
            continue

        def one(m: "re.Match[str]", text: str = text) -> str:
            nonlocal count
            out = expand_replacement(replacement, m.group(0), list(m.groups())) if regex else literal
            count += 1
            if len(samples) < SAMPLE_LIMIT:
                samples.append(_sample(text, m.start(), len(m.group(0)), out))
            return out
        parts[i] = pattern.sub(one, text)
    return {"html": "".join(parts), "count": count, "samples": samples}

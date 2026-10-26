"""Port of src/utils/editHints.ts — the nearest paragraph to a failed SEARCH."""
import re
from typing import Dict, List, Optional

from .paragraphs import chapter_paragraphs
from .text import _html_to_comparable_text, _quote_blind_text

NEAREST_MIN_SCORE = 0.35
NEAREST_HTML_CAP = 600


def _bigrams(text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i in range(len(text) - 1):
        g = text[i:i + 2]
        out[g] = out.get(g, 0) + 1
    return out


def text_similarity(a: str, b: str) -> float:
    if len(a) < 2 or len(b) < 2:
        return 1 if a == b and a else 0
    x, y = _bigrams(a), _bigrams(b)
    shared = sum(min(n, y.get(g, 0)) for g, n in x.items())
    return 2 * shared / ((len(a) - 1) + (len(b) - 1))


_CONFUSABLE_PAIRS = [
    ("quotes", re.compile("[“”]"), re.compile('"'), "curly quotes “ ”", 'straight quotes "'),
    ("quotes", re.compile('"'), re.compile("[“”]"), 'straight quotes "', "curly quotes “ ”"),
    ("apostrophes", re.compile("[‘’]"), re.compile("'"), "curly apostrophes ‘ ’", "straight apostrophes '"),
    ("apostrophes", re.compile("&#39;|&apos;", re.I), re.compile("'"), "the entity &#39;", "a literal '"),
    ("spaces", re.compile("&nbsp;", re.I), re.compile(" "), "&nbsp; entities", "plain spaces"),
    ("spaces", re.compile(" "), re.compile(" "), "non-breaking spaces (U+00A0)", "plain spaces"),
    ("dashes", re.compile("—"), re.compile("-|--"), "em dashes —", "hyphens"),
    ("dashes", re.compile("–"), re.compile("-"), "en dashes –", "hyphens"),
    ("ellipses", re.compile("…"), re.compile(r"\.\.\."), "the ellipsis character …", "three dots"),
    ("ampersands", re.compile("&amp;", re.I), re.compile(r"&(?!amp;|lt;|gt;|quot;|nbsp;|#)"), "&amp;", "a literal &"),
]
_INLINE_TAG_RE = re.compile(r"<(strong|em|b|i|u|s|a|span|code)\b", re.I)


def describe_differences(html: str, search: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for label, chapter_re, search_re, chapter_name, search_name in _CONFUSABLE_PAIRS:
        if label in seen:
            continue
        if chapter_re.search(html) and search_re.search(search) and not chapter_re.search(search):
            out.append(f"the chapter uses {chapter_name} where your SEARCH has {search_name}")
            seen.add(label)
    chapter_tags = {m.group(1).lower() for m in _INLINE_TAG_RE.finditer(html)}
    search_tags = {m.group(1).lower() for m in _INLINE_TAG_RE.finditer(search)}
    missing = [t for t in sorted(chapter_tags, key=lambda t: html.lower().find(f"<{t}")) if t not in search_tags]
    if missing:
        out.append(f"the chapter has inline <{'>, <'.join(missing)}> tags your SEARCH leaves out")
    if re.search(r"<(?:p|h[1-6]|li|blockquote)\b[^>]*\s[a-z-]+=", html, re.I) and not re.search(r"<[a-z0-9]+\s[a-z-]+=", search, re.I):
        out.append("the chapter's tag carries attributes your SEARCH leaves out")
    return out


def nearest_paragraph(html: str, search: str) -> Optional[Dict]:
    first_block = re.split(r"</(?:p|h[1-6]|li|blockquote)>", search, maxsplit=1, flags=re.I)[0]
    wanted = _quote_blind_text(first_block)
    if not wanted:
        return None
    best: Optional[Dict] = None
    for para in chapter_paragraphs(html):
        if para["kind"] == "image":
            continue
        score = text_similarity(wanted, _quote_blind_text(para["html"]))
        if best is None or score > best["score"]:
            best = {"number": para["number"], "html": para["html"], "score": score, "differences": []}
    if best is None or best["score"] < NEAREST_MIN_SCORE:
        return None
    normalized_match = _html_to_comparable_text(best["html"]) == _html_to_comparable_text(first_block) or _quote_blind_text(best["html"]) == wanted
    best["differences"] = describe_differences(best["html"], first_block) if normalized_match or best["score"] >= 0.9 else []
    return best


def nearest_hint(html: str, search: str) -> str:
    near = nearest_paragraph(html, search)
    if not near:
        return ""
    shown = near["html"][:NEAREST_HTML_CAP] + "…" if len(near["html"]) > NEAREST_HTML_CAP else near["html"]
    why = f" It differs only in spelling: {'; '.join(near['differences'])}." if near["differences"] else ""
    return f"  Nearest: ¶{near['number']} — copy this HTML exactly: {shown}{why}"

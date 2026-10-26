"""Port of src/utils/attachments.ts — a book's reference files as the agent reads them."""
import re
from typing import Any, Dict, List, Optional

from .jsstr import JS_WS, js_trim

_WS = "[" + "".join("\\" + c if c in "\\]^-" else c for c in JS_WS) + "]"
_BLOCK_SPLIT = re.compile(r"\n" + _WS + r"*\n")
_CJK = re.compile("[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")
_HEADING = re.compile(r"^(?:第[零〇一二三四五六七八九十百千万两\d０-９]+[章节回卷部集篇话]|chapter" + _WS + r"+[\divxlc]+\b|序章|序言|楔子|引子|尾声|后记|番外)", re.I)
_MAX_HEADING_CHARS = 40
_REF = re.compile(r"^(?:A|附件)" + _WS + r"*(\d+)$", re.I)
ATTACHMENT_INDEX_LINES = 80
ATTACHMENT_RUN_READ_CAP = 100_000


def attachment_budget_note(ref: str, used: int) -> str:
    return (f"{ref} was not read: this turn has already read {used} characters of attachments, the most one turn may — a whole file is never read into the conversation. "
            f'Find the passages you need with grep chapters=["{ref}"] and read only those paragraphs, or read a range with a task (chapters=["{ref}"] section="第10–20章" task="…") to get notes from batches outside the conversation.')


def normalize_attachment_text(text: str) -> str:
    out = text[1:] if text.startswith("﻿") else text
    return out.replace("\r\n", "\n").replace("\r", "\n")


def attachment_paragraphs(text: str) -> List[str]:
    normalized = normalize_attachment_text(text)
    blocks = [[js_trim(l) for l in b.split("\n") if js_trim(l)] for b in _BLOCK_SPLIT.split(normalized)]
    blocks = [b for b in blocks if b]
    multi = sum(1 for b in blocks if len(b) > 1)
    if blocks and multi >= max(3, len(blocks) * 0.3):
        out = []
        for lines in blocks:
            acc = ""
            for line in lines:
                if not acc:
                    acc = line
                else:
                    glue = "" if _CJK.match(acc[-1]) or _CJK.match(line[0]) else " "
                    acc = acc + glue + line
            out.append(acc)
        return [piece for p in out for piece in split_long_paragraph(p)]
    return [piece for l in normalized.split("\n") if js_trim(l) for piece in split_long_paragraph(js_trim(l))]


#: Longest paragraph an attachment is split into (attachments_and_web.md §1): a file with
#: no line breaks would otherwise be ONE paragraph, and every read of it the whole file.
MAX_PARAGRAPH_CHARS = 4000
_SENTENCE_ENDS = set("。！？!?.…」”』；;")


def split_long_paragraph(paragraph: str) -> List[str]:
    if len(paragraph) <= MAX_PARAGRAPH_CHARS:
        return [paragraph]
    out: List[str] = []
    i = 0
    while len(paragraph) - i > MAX_PARAGRAPH_CHARS:
        cut = i + MAX_PARAGRAPH_CHARS
        k = cut - 1
        while k >= i + MAX_PARAGRAPH_CHARS / 2:
            if paragraph[k] in _SENTENCE_ENDS:
                cut = k + 1
                break
            k -= 1
        piece = js_trim(paragraph[i:cut])
        if piece:
            out.append(piece)
        i = cut
    rest = js_trim(paragraph[i:])
    if rest:
        out.append(rest)
    return out


def attachment_sections(paragraphs: List[str]) -> List[Dict[str, Any]]:
    heads = [i for i, p in enumerate(paragraphs) if len(p) <= _MAX_HEADING_CHARS and _HEADING.match(p)]
    if not heads:
        return []
    out: List[Dict[str, Any]] = []
    if heads[0] > 0:
        out.append({"title": "(opening)", "from": 1, "to": heads[0]})
    for k, h in enumerate(heads):
        out.append({"title": paragraphs[h], "from": h + 1, "to": heads[k + 1] if k + 1 < len(heads) else len(paragraphs)})
    return out


def resolve_attachment_ref(ref: Any, items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    value = js_trim(str(ref if ref is not None else ""))
    if not value:
        return None
    m = _REF.match(value)
    if m:
        want = f"a{int(m.group(1))}"
        return next((a for a in items if a["ref"].lower() == want), None)

    def base(name: str) -> str:
        return re.sub(r"\.[^.]+$", "", name)
    return next((a for a in items if a["name"] == value), None) or next((a for a in items if base(a["name"]) == value), None)


def render_attachment_index(items: List[Dict[str, Any]], max_lines: int = ATTACHMENT_INDEX_LINES) -> str:
    if not items:
        return ""
    lines: List[str] = []
    hidden = 0
    for a in items:
        lines.append(f'{a["ref"]} "{a["name"]}" — {a["chars"]} characters, {a["paragraphs"]} paragraphs'
                     + (f', {len(a["sections"])} sections' if a["sections"] else ""))
        for s in a["sections"]:
            if len(lines) >= max_lines:
                hidden += 1
                continue
            lines.append(f"  ¶{s['from']}–{s['to']} {s['title']}")
    return ('ATTACHMENTS (reference files the user attached to this book — not chapters: read a section with read chapters=["A1"] section="第三十章" (or a paragraph range), '
            'search with grep chapters=["A1"], or read a range with a task (chapters=["A1"] section="第10–20章" task="…") for notes; a turn reads at most 100,000 characters of them, and they cannot be written):\n'
            + "\n".join(lines) + (f"\n  … {hidden} more sections (grep for a heading to find one)" if hidden else ""))


def attachment_chunks(meta: Dict[str, Any], paragraphs: List[str], budget_chars: int, rng: Optional[Dict[str, int]] = None) -> List[Dict[str, str]]:
    sections = attachment_sections(paragraphs)
    every = sections if sections else [{"title": meta["name"], "from": 1, "to": len(paragraphs)}]
    spans = every if rng is None else [
        {"title": s["title"], "from": max(s["from"], rng["from"]), "to": min(s["to"], rng["to"], len(paragraphs))}
        for s in every if s["to"] >= rng["from"] and s["from"] <= rng["to"]]
    out: List[Dict[str, str]] = []
    for s in spans:
        start = s["from"]
        buf: List[str] = []
        used = 0
        for n in range(s["from"], s["to"] + 1):
            p = paragraphs[n - 1]
            if buf and used + len(p) + 1 > budget_chars:
                out.append({"title": f'{meta["ref"]} "{meta["name"]}" — {s["title"]} (¶{start}–{n - 1})', "text": "\n".join(buf)})
                buf, used, start = [], 0, n
            buf.append(p)
            used += len(p) + 1
        if buf:
            out.append({"title": f'{meta["ref"]} "{meta["name"]}" — {s["title"]} (¶{start}–{s["to"]})', "text": "\n".join(buf)})
    return out


def render_attachment_part(meta: Dict[str, Any], paragraphs: List[str], start: int, to: Optional[int], cap: int) -> Dict[str, Any]:
    end = min(to if to is not None else len(paragraphs), len(paragraphs))
    lines: List[str] = []
    used = 0
    last = start - 1
    for n in range(start, end + 1):
        line = f"¶{n} {paragraphs[n - 1]}"
        # Strict: the cap is never passed (a paragraph is at most MAX_PARAGRAPH_CHARS).
        if used + len(line) > cap:
            break
        lines.append(line)
        used += len(line)
        last = n
    whole = start == 1 and last == len(paragraphs)
    span = "" if whole else f", ¶{start}–¶{last} of {len(paragraphs)}"
    more = (f'\n[Stopped at ¶{last} to stay under {cap} characters. Continue with chapters=["{meta["ref"]}"], paragraphs="{last + 1}-{to if to is not None else ""}".]'
            if last < end else "")
    return {"content": f'=== {meta["ref"]} "{meta["name"]}" (attachment) — {len(paragraphs)} paragraphs, {meta["chars"]} characters{span} ===\n' + "\n".join(lines) + more,
            "last": last, "used": used}


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_NUMBERED = re.compile(r"^(?:第" + _WS + r"*([零〇一二三四五六七八九十百千万两\d０-９]+)" + _WS + r"*[章节回卷部集篇话]|chapter" + _WS + r"+(\d+))", re.I)


def parse_chapter_number(text: str) -> Optional[int]:
    value = "".join(chr(ord(c) - 0xFEE0) if "０" <= c <= "９" else c for c in js_trim(text))
    if value and all("0" <= c <= "9" for c in value):
        return int(value)
    if not value or not all(c in _CN_DIGITS or c in _CN_UNITS for c in value):
        return None
    total = section = digit = 0
    for ch in value:
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
            continue
        unit = _CN_UNITS[ch]
        if unit == 10000:
            total += (section + digit) * unit
            section = 0
        else:
            section += (1 if digit == 0 else digit) * unit
        digit = 0
    return total + section + digit


def section_number_of(text: str) -> Optional[int]:
    value = js_trim(text)
    m = _NUMBERED.match(value)
    if m:
        return parse_chapter_number(m.group(1) if m.group(1) is not None else m.group(2))
    return parse_chapter_number(value)


def find_attachment_section(sections: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    q = js_trim(query or "")
    if not q:
        return None
    n = section_number_of(q)
    if n is not None:
        hit = next((s for s in sections if section_number_of(s["title"]) == n), None)
        if hit is not None:
            return hit
    key = re.sub(_WS + "+", "", q)
    return next((s for s in sections if key in re.sub(_WS + "+", "", s["title"])), None)


_RANGE = re.compile(r"^第?" + _WS + r"*([零〇一二三四五六七八九十百千万两\d０-９]+)" + _WS + r"*[章节回卷部集篇话]?" + _WS + r"*[-–—~～至到]" + _WS
                    + r"*第?" + _WS + r"*([零〇一二三四五六七八九十百千万两\d０-９]+)" + _WS + r"*[章节回卷部集篇话]?$")


def find_attachment_range(sections: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    """Port of findAttachmentRange: a section, or a run of numbered sections ("第10–20章")."""
    q = js_trim(query or "")
    m = _RANGE.match(q)
    if m:
        a, b = parse_chapter_number(m.group(1)), parse_chapter_number(m.group(2))
        if a is not None and b is not None:
            lo, hi = min(a, b), max(a, b)
            hits = [s for s in sections if (lambda n: n is not None and lo <= n <= hi)(section_number_of(s["title"]))]
            if hits:
                return {"title": hits[0]["title"] if len(hits) == 1 else f'{hits[0]["title"]} … {hits[-1]["title"]}',
                        "from": min(h["from"] for h in hits), "to": max(h["to"] for h in hits)}
    return find_attachment_section(sections, q)


#: Sections one `list source="A1"` call shows (read_and_list.md §3).
LIST_SECTION_LINES = 200


def render_section_list(meta: Dict[str, Any], sections: List[Dict[str, Any]], start: int = 1, max_lines: int = LIST_SECTION_LINES,
                        continue_args: str = "") -> str:
    """Port of renderSectionList."""
    head = f'=== {meta["ref"]} "{meta["name"]}" — {meta["paragraphs"]} paragraphs, {meta["chars"]} characters ==='
    if not sections:
        return (f'{head}\nNo section headings here. Find places with grep chapters=["{meta["ref"]}"] and read them by ¶ '
                f'(read chapters=["{meta["ref"]}"] paragraphs="…").')
    first = max(1, min(start, len(sections)))
    shown = sections[first - 1:first - 1 + max_lines]
    last = first - 1 + len(shown)
    lines = [f"{first + i}. ¶{s['from']}–{s['to']} {s['title']}" for i, s in enumerate(shown)]
    if last < len(sections):
        more = f'\n[Sections {first}–{last} of {len(sections)}. Continue with list source="{meta["ref"]}"{continue_args} from={last + 1}.]'
    else:
        more = f"\n[Sections {first}–{last} of {len(sections)}.]" if first > 1 else ""
    return f'{head}\n' + "\n".join(lines) + f'{more}\nRead one by its ¶ range: read chapters=["{meta["ref"]}"] paragraphs="a-b" (or section="…").'

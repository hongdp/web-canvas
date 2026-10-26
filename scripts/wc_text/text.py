"""Port of src/utils/text.ts — markup parsing, edit application, validation.

Regex notes for the port: JavaScript's \\w and \\d are ASCII-only, so they are
written as explicit classes here (Python's match CJK and other scripts);
`trim()` is JavaScript's whitespace set (_js_trim). The browser-only helpers
(blob/GIF conversion) are not ported.
"""
import re
from typing import Callable, Dict, List, Optional, Tuple

from .diff import diff_html
from .jsstr import JS_WS as _JS_WS, js_trim as _js_trim

_W = "A-Za-z0-9_"  # JavaScript \w


def strip_incomplete_end_tag(text: str) -> str:
    target = "</selection_replace>"
    for i in range(len(target), 0, -1):
        prefix = target[:i]
        if text.endswith(prefix):
            return text[: len(text) - len(prefix)]
    return text


def chapter_attribute(opening_tag: str) -> Optional[str]:
    m = re.search(r"\bchapter\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", opening_tag, re.I)
    value = _js_trim((m.group(1) if m and m.group(1) is not None else (m.group(2) if m else "")) or "")
    return value or None


def new_chapter_attribute(opening_tag: str) -> Optional[str]:
    m = re.search(r"\bnew_chapter\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", opening_tag, re.I)
    value = _js_trim((m.group(1) if m and m.group(1) is not None else (m.group(2) if m else "")) or "")
    return value or None


_FENCE_RE = re.compile(r"^\s*```(?:html)?\s*([\s\S]*?)\s*```\s*$", re.I)


def extract_tagged_block(text: str, tag: str) -> Dict:
    open_match = re.search(rf"<{tag}(?:\s[^>]*)?>", text, re.I)
    if not open_match:
        return {"found": False, "closed": False, "inner": "", "before": text, "after": ""}
    named: Dict[str, str] = {}
    chapter = chapter_attribute(open_match.group(0))
    new_chapter = new_chapter_attribute(open_match.group(0))
    if chapter:
        named["chapter"] = chapter
    if new_chapter:
        named["newChapter"] = new_chapter
    before = text[: open_match.start()]
    rest = text[open_match.end():]
    close_match = re.search(rf"</{tag}\s*>", rest, re.I)
    if not close_match:
        return {"found": True, "closed": False, "inner": rest, "before": before, "after": "", **named}
    inner = rest[: close_match.start()]
    after = rest[close_match.end():]
    fenced = _FENCE_RE.match(inner)
    return {"found": True, "closed": True, "inner": fenced.group(1) if fenced else inner, "before": before, "after": after, **named}


_ELISION_KEYWORD = (r"(?:unchanged|omitted|omit|continues?|rest of (?:the |your )?(?:document|text|content|chapter)|"
                    r"remains? (?:the )?same|same as (?:before|above|previous)|as before|truncat[" + _W + r"]*|abbreviat[" + _W + r"]*|"
                    r"previous content|earlier content)")


def has_elision_markers(html: str) -> bool:
    if re.search(rf"<!--[\s\S]*?{_ELISION_KEYWORD}[\s\S]*?-->", html, re.I):
        return True
    if re.search(rf"\[[^\]]{{0,60}}?{_ELISION_KEYWORD}[^\]]{{0,40}}?\]", html, re.I):
        return True
    if re.search(rf"\([^)]{{0,60}}?{_ELISION_KEYWORD}[^)]{{0,40}}?\)", html, re.I):
        return True
    if re.search(r"<p>\s*(?:\.\.\.|…)\s*</p>", html, re.I):
        return True
    # 5–8: the same shortcuts in Chinese (hasElisionMarkers; a live run, 2026-10-10).
    if _ZH_ELISION_REFERENCE.search(html):
        return True
    if re.search(r"[（(【\[]\s*(?:略|同上|不变|保持不变|未变|省略|下同|内容不变|此处略)[^）)】\]]{0,20}[）)】\]]", html):
        return True
    if re.search(r"<p[^>]*>\s*(?:略|同上|下略|从略|……略|…略)[。.]?\s*</p>", html):
        return True
    if re.search(r"[:：]\s*(?:同上|略|不变|保持不变|见上|未变)[。.；;]?\s*(?=<|$)", html):
        return True
    return False


_ZH_ELISION_REFERENCE = re.compile(r"见原(?:条目|文|稿|版|内容)|见上文|见前文|见上一版|此处省略|以下省略|其余省略|余下省略|中间省略|与(?:前文|原文|上文|原稿|之前)(?:相同|一致)|其余(?:内容)?不变|其余同上")


def validate_canvas_replacement(new_html: str, closing_tag_found: bool) -> Optional[str]:
    if not closing_tag_found:
        return "truncated"
    if has_elision_markers(new_html):
        return "elided"
    return None


_EDIT_SEARCH_RE = re.compile(r"<{5,}\s*SEARCH[^\n]*\n", re.I)
_EDIT_DIVIDER_RE = re.compile(r"\n={3,}[^\n]*\n")
_EDIT_TERMINATOR_RE = re.compile(r"\n?>{5,}\s*REPLACE[^\n]*|\n?</edits?\s*>|\n<{5,}\s*SEARCH", re.I)
_EDIT_WRAPPER_RE = re.compile(r"</?edits?\b[^>]*>", re.I)
_EDIT_SUGAR_RE = re.compile(r"</?edits?(?:\s[^>]*)?>", re.I)


def parse_edit_blocks(text: str) -> Dict:
    blocks: List[Dict] = []
    first_start = -1
    last_end = -1
    current_chapter: Optional[str] = None
    scanned_to = 0
    pos = 0
    while True:
        m = _EDIT_SEARCH_RE.search(text, pos)
        if not m:
            break
        gap = text[scanned_to:m.start()]
        for tag in _EDIT_WRAPPER_RE.findall(gap):
            current_chapter = None if tag.startswith("</") else chapter_attribute(tag)
        search_start = m.end()
        rest = text[search_start:]
        divider = _EDIT_DIVIDER_RE.search(rest)
        if not divider:
            pos = m.end()
            continue
        search = rest[: divider.start()]
        after_divider_start = divider.end()
        after_divider = rest[after_divider_start:]
        terminator = _EDIT_TERMINATOR_RE.search(after_divider)
        if not terminator:
            pos = m.end()
            continue
        if _js_trim(search):
            block: Dict = {"search": search, "replace": after_divider[: terminator.start()]}
            if current_chapter:
                block["chapter"] = current_chapter
            blocks.append(block)
        if first_start == -1:
            first_start = m.start()
        starts_next = re.search(r"SEARCH", terminator.group(0), re.I) is not None
        last_end = search_start + after_divider_start + (terminator.start() if starts_next else terminator.end())
        if re.search(r"</edits?", terminator.group(0), re.I):
            current_chapter = None
        scanned_to = last_end
        pos = last_end
    if not blocks:
        return {"blocks": blocks, "before": "", "after": ""}
    strip_sugar = lambda s: _js_trim(_EDIT_SUGAR_RE.sub("", s))  # noqa: E731
    return {"blocks": blocks, "before": strip_sugar(text[:first_start]), "after": strip_sugar(text[last_end:])}


_STRAY_MARKUP_PATTERNS = [
    re.compile(r"<edits?(?:\s[^>]*)?>[\s\S]*?(?:</edits?>|$)", re.I),
    re.compile(r"<{5,}\s*SEARCH[\s\S]*?(?:>{5,}\s*REPLACE[^\n]*|$)", re.I),
    re.compile(r"<canvas(?:\s[^>]*)?>[\s\S]*?(?:</canvas>|$)", re.I),
    re.compile(r"<selection_replace>[\s\S]*?(?:</selection_replace>|$)", re.I),
]
_LONE_MARKUP_TAG_RE = re.compile(r"</?(?:edits?|canvas|selection_replace)(?:\s[^>]*)?>", re.I)


def strip_stray_document_markup(text: str) -> Dict:
    removed = 0
    out = text
    for pattern in _STRAY_MARKUP_PATTERNS:
        def repl(_m):
            nonlocal removed
            removed += 1
            return "\n\n"
        out = pattern.sub(repl, out)
    out = _LONE_MARKUP_TAG_RE.sub("", out)
    return {"text": _js_trim(re.sub(r"\n{3,}", "\n\n", out)), "removed": removed}


_CANVAS_OPEN_WITH_ATTRS_RE = re.compile(r"<canvas\s[^>]*>", re.I)
_CANVAS_CLOSE_RE = re.compile(r"</canvas\s*>", re.I)


def _take_chapter_canvases(text: str) -> Tuple[List[Dict], str]:
    blocks: List[Dict] = []
    rest = text
    pos = 0
    while True:
        m = _CANVAS_OPEN_WITH_ATTRS_RE.search(rest, pos)
        if not m:
            break
        chapter = chapter_attribute(m.group(0))
        new_chapter = new_chapter_attribute(m.group(0))
        if not chapter and not new_chapter:
            pos = m.end()
            continue
        after = rest[m.end():]
        close = _CANVAS_CLOSE_RE.search(after)
        inner = after[: close.start()] if close else after
        fenced = _FENCE_RE.match(inner)
        block: Dict = {"text": fenced.group(1) if fenced else inner, "closed": bool(close)}
        if new_chapter:
            block["newChapter"] = new_chapter
        else:
            block["chapter"] = chapter
        blocks.append(block)
        tail = after[close.end():] if close else ""
        rest = _js_trim(_js_trim(rest[: m.start()]) + "\n\n" + _js_trim(tail))
        pos = 0
        if not close:
            break
    return blocks, rest


def parse_assistant_response(full_text: str) -> Dict:
    full_text = strip_doc_status(full_text)
    result: Dict = {"kind": "chat", "chatText": full_text, "selectionText": "", "editBlocks": [],
                    "canvasText": "", "canvasClosed": False, "extraCanvases": [], "strayMarkup": 0}

    def join_around(before: str, after: str) -> str:
        text = _js_trim(before)
        if _js_trim(after):
            text += ("\n\n" if text else "") + _js_trim(after)
        return text

    selection = extract_tagged_block(full_text, "selection_replace")
    parsed_edits = parse_edit_blocks(full_text)
    canvas = extract_tagged_block(full_text, "canvas")

    if selection["found"]:
        result["kind"] = "selection"
        result["selectionText"] = selection["inner"]
        rest = join_around(selection["before"], selection["after"])
        rest_edits = parse_edit_blocks(rest)
        result["editBlocks"] = rest_edits["blocks"]
        result["chatText"] = join_around(rest_edits["before"], rest_edits["after"]) if rest_edits["blocks"] else rest
    elif parsed_edits["blocks"]:
        result["kind"] = "edits"
        result["editBlocks"] = parsed_edits["blocks"]
        result["chatText"] = join_around(parsed_edits["before"], parsed_edits["after"])
    elif canvas["found"]:
        result["kind"] = "canvas"
        result["canvasText"] = canvas["inner"]
        result["canvasClosed"] = canvas["closed"]
        if canvas.get("newChapter"):
            result["canvasNewChapter"] = canvas["newChapter"]
        elif canvas.get("chapter"):
            result["canvasChapter"] = canvas["chapter"]
        result["chatText"] = join_around(canvas["before"], canvas["after"])

    extra, rest = _take_chapter_canvases(result["chatText"])
    result["extraCanvases"] = extra
    result["chatText"] = rest

    if result["kind"] == "edits" and all(b.get("chapter") for b in result["editBlocks"]):
        own = extract_tagged_block(result["chatText"], "canvas")
        if own["found"] and not own.get("chapter") and not own.get("newChapter"):
            result["extraCanvases"].append({"text": own["inner"], "closed": own["closed"]})
            result["chatText"] = join_around(own["before"], own["after"])

    stray = strip_stray_document_markup(result["chatText"])
    result["chatText"] = stray["text"]
    result["strayMarkup"] = stray["removed"]
    return result


# ── Edit application ────────────────────────────────────────────────────────

_FUZZY_ESCAPE = set(".*+?^${}()|[]\\")


def _build_fuzzy_pattern(search: str) -> str:
    out: List[str] = []
    i = 0
    n = len(search)

    def is_ws(idx: int) -> bool:
        return search[idx] in _JS_WS or search.startswith("&nbsp;", idx)

    while i < n:
        if is_ws(i):
            out.append(r"(?:\s|&nbsp;)+")
            while i < n and is_ws(i):
                i += 6 if search.startswith("&nbsp;", i) else 1
            continue
        ch = search[i]
        if ch in "'‘’" or search.startswith("&#39;", i) or search.startswith("&apos;", i):
            out.append("(?:'|‘|’|&#39;|&apos;)")
            i += 5 if search.startswith("&#39;", i) else 6 if search.startswith("&apos;", i) else 1
            continue
        if ch in '"“”' or search.startswith("&quot;", i):
            out.append('(?:"|“|”|&quot;)')
            i += 6 if search.startswith("&quot;", i) else 1
            continue
        if ch == "&":
            out.append("(?:&amp;|&)")
            i += 5 if search.startswith("&amp;", i) else 1
            continue
        out.append("\\" + ch if ch in _FUZZY_ESCAPE else ch)
        i += 1
    return "".join(out)


def _html_to_comparable_text(html: str) -> str:
    out = re.sub(r"<[^>]*>", " ", html)
    out = re.sub(r"&nbsp;", " ", out, flags=re.I)
    out = re.sub(r"&amp;", "&", out, flags=re.I)
    out = re.sub(r"&#39;|&apos;|[‘’]", "'", out, flags=re.I)
    out = re.sub(r"&quot;|[“”]", '"', out, flags=re.I)
    out = re.sub(r"&lt;", "<", out, flags=re.I)
    out = re.sub(r"&gt;", ">", out, flags=re.I)
    out = re.sub(r"\s+", " ", out)
    return _js_trim(out)


_EDIT_BLOCK_SPLIT_RE = re.compile(r"</(?:p|h[1-6]|blockquote|pre|ul|ol|table|figure|div)>", re.I)


def _block_text_runs(haystack: str, search: str, normalize: Callable[[str], str], limit: int) -> List[Tuple[int, int]]:
    search_text = normalize(search)
    if not search_text:
        return []
    segments: List[Tuple[int, int, str]] = []
    seg_start = 0
    for m in _EDIT_BLOCK_SPLIT_RE.finditer(haystack):
        end = m.end()
        segments.append((seg_start, end, normalize(haystack[seg_start:end])))
        seg_start = end
    if seg_start < len(haystack):
        segments.append((seg_start, len(haystack), normalize(haystack[seg_start:])))
    runs: List[Tuple[int, int]] = []
    for i in range(len(segments)):
        if len(runs) >= limit:
            break
        if not segments[i][2]:
            continue
        acc = ""
        for j in range(i, len(segments)):
            if segments[j][2]:
                acc = acc + " " + segments[j][2] if acc else segments[j][2]
            if acc == search_text:
                runs.append((segments[i][0], segments[j][1]))
                break
            if len(acc) > len(search_text):
                break
    return runs


def _quote_blind_text(html: str) -> str:
    return _js_trim(re.sub(r"\s+", " ", re.sub(r"[\"'“”‘’「」『』]", "", _html_to_comparable_text(html))))


def _replace_by_block_text(haystack: str, search: str, replace: str) -> Optional[str]:
    def splice(run: Tuple[int, int]) -> str:
        return haystack[: run[0]] + replace + haystack[run[1]:]
    exact = _block_text_runs(haystack, search, _html_to_comparable_text, 1)
    if exact:
        return splice(exact[0])
    quote_blind = _block_text_runs(haystack, search, _quote_blind_text, 2)
    return splice(quote_blind[0]) if len(quote_blind) == 1 else None


MIN_EXCERPT_CHARS = 8
DUPLICATION_WINDOW = 10
_OUTER_OPEN_RE = re.compile(r"^<(p|h[1-6])(?:\s[^>]*)?>", re.I)
_OUTER_CLOSE_RE = re.compile(r"</(p|h[1-6])>$", re.I)
_BLOCK_OPEN_RE = re.compile(r"<(?:p|h[1-6]|li|blockquote)(?:\s[^>]*)?>", re.I)
_BLOCK_CLOSE_RE = re.compile(r"</(?:p|h[1-6]|li|blockquote)>", re.I)
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "col", "area", "base", "embed", "param", "track"}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(/?)>")


def _is_balanced_html(html: str) -> bool:
    stack: List[str] = []
    for m in _TAG_RE.finditer(html):
        name = m.group(2).lower()
        if name in VOID_TAGS or m.group(3) == "/":
            continue
        if m.group(1):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _is_inside_tag(html: str, index: int) -> bool:
    if index <= 0:
        return False
    return html.rfind("<", 0, index) > html.rfind(">", 0, index)


def _find_all_matches(haystack: str, needle: str) -> List[Tuple[int, int]]:
    exact: List[Tuple[int, int]] = []
    i = haystack.find(needle)
    while i != -1:
        exact.append((i, i + len(needle)))
        i = haystack.find(needle, i + 1)
    if exact:
        return exact
    try:
        pattern = re.compile(_build_fuzzy_pattern(needle))
    except re.error:
        return []
    return [(m.start(), m.end()) for m in pattern.finditer(haystack) if m.end() > m.start()]


def _replace_by_unwrapped_excerpt(haystack: str, search: str, replace: str) -> Optional[str]:
    open_m = _OUTER_OPEN_RE.search(search)
    close_m = _OUTER_CLOSE_RE.search(search)
    if not open_m and not close_m:
        return None
    needle = search[(open_m.end() if open_m else 0):(close_m.start() if close_m else len(search))]
    if len(_html_to_comparable_text(needle)) < MIN_EXCERPT_CHARS:
        return None
    payload = _js_trim(replace)
    r_open = _OUTER_OPEN_RE.search(payload)
    if r_open:
        if not open_m or r_open.group(1).lower() != open_m.group(1).lower():
            return None
        payload = payload[r_open.end():]
    r_close = _OUTER_CLOSE_RE.search(payload)
    if r_close:
        if not close_m or r_close.group(1).lower() != close_m.group(1).lower():
            return None
        payload = payload[: r_close.start()]
    hits = _find_all_matches(haystack, needle)
    if len(hits) != 1:
        return None
    start, end = hits[0]
    if _is_inside_tag(haystack, start):
        return None
    block_start = 0
    for bo in _BLOCK_OPEN_RE.finditer(haystack):
        if bo.start() >= start:
            break
        block_start = bo.end()
    close_after = _BLOCK_CLOSE_RE.search(haystack[end:])
    block_end = end + close_after.start() if close_after else len(haystack)
    leftover = _html_to_comparable_text(haystack[block_start:start] + " " + haystack[end:block_end])
    payload_text = _html_to_comparable_text(payload)
    for i in range(0, len(leftover) - DUPLICATION_WINDOW + 1):
        if leftover[i:i + DUPLICATION_WINDOW] in payload_text:
            return None
    result = haystack[:start] + payload + haystack[end:]
    return result if _is_balanced_html(result) else None


def _apply_one_edit(haystack: str, search: str, replace: str) -> Optional[str]:
    idx = haystack.find(search)
    if idx != -1:
        return haystack[:idx] + replace + haystack[idx + len(search):]
    trimmed = _js_trim(search)
    if not trimmed:
        return None
    idx = haystack.find(trimmed)
    if idx != -1:
        return haystack[:idx] + replace + haystack[idx + len(trimmed):]
    try:
        m = re.search(_build_fuzzy_pattern(trimmed), haystack)
        if m:
            return haystack[: m.start()] + replace + haystack[m.end():]
    except re.error:
        pass
    by_block = _replace_by_block_text(haystack, trimmed, replace)
    if by_block is not None:
        return by_block
    return _replace_by_unwrapped_excerpt(haystack, trimmed, replace)


def apply_edit_blocks(original_html: str, blocks: List[Dict]) -> Dict:
    html = original_html
    failed: List[Dict] = []
    for block in blocks:
        result = _apply_one_edit(html, block["search"], block["replace"])
        if result is None:
            failed.append(block)
        else:
            html = result
    return {"html": html, "failed": failed}


_DIFF_MARKUP_RE = re.compile(r'class="[^"]*diff-(?:addition|deletion)')


def _split_top_level_nodes(html: str) -> List[str]:
    nodes: List[str] = []
    depth = 0
    start = 0
    for m in _TAG_RE.finditer(html):
        name = m.group(2).lower()
        if name in VOID_TAGS or m.group(3) == "/":
            continue
        if m.group(1):
            depth = max(0, depth - 1)
            if depth == 0:
                nodes.append(html[start:m.end()])
                start = m.end()
        else:
            depth += 1
    if start < len(html):
        nodes.append(html[start:])
    return nodes


_DIFF_ELEMENT_EDGE_RE = re.compile(r'<(?:ins|del)\b[^>]*class="[^"]*diff-(?:addition|deletion)[^"]*"[^>]*>|</(?:ins|del)>', re.I)


def _pending_diff_at(html: str, index: int) -> Optional[str]:
    inside = None
    for m in _DIFF_ELEMENT_EDGE_RE.finditer(html):
        if m.start() >= index:
            break
        tok = m.group(0)
        inside = None if tok.startswith("</") else ("ins" if tok.lower().startswith("<ins") else "del")
    return inside


_WORD_CH = re.compile(rf"[{_W}]")


def _is_w(s: str, i: int) -> bool:
    return 0 <= i < len(s) and bool(_WORD_CH.match(s[i]))


def _diff_changed_span(old_part: str, new_part: str) -> Optional[str]:
    mx = min(len(old_part), len(new_part))
    start = 0
    while start < mx and old_part[start] == new_part[start]:
        start += 1
    tail = 0
    while tail < mx - start and old_part[len(old_part) - 1 - tail] == new_part[len(new_part) - 1 - tail]:
        tail += 1
    while start > 0 and _is_w(old_part, start - 1) and (_is_w(old_part, start) or _is_w(new_part, start)):
        start -= 1
    while (tail > 0 and _is_w(old_part, len(old_part) - tail)
           and (_is_w(old_part, len(old_part) - tail - 1) or _is_w(new_part, len(new_part) - tail - 1))):
        tail -= 1
    if old_part.rfind("<", 0, start) > old_part.rfind(">", 0, start):
        start = old_part.rfind("<", 0, start)
    amp = old_part.rfind("&", 0, start)
    if amp != -1 and re.fullmatch(r"&[a-zA-Z0-9#]*", old_part[amp:start]):
        start = amp
    suffix = old_part[len(old_part) - tail:]
    gt = suffix.find(">")
    if gt != -1 and (suffix.find("<") == -1 or gt < suffix.find("<")):
        tail -= gt + 1
    semi = re.match(r"^[a-zA-Z0-9#]*;", old_part[len(old_part) - tail:])
    if semi and re.search(r"&[a-zA-Z0-9#]*$", old_part[: len(old_part) - tail]):
        tail -= len(semi.group(0))
    old_mid = old_part[start:len(old_part) - tail]
    new_mid = new_part[start:len(new_part) - tail]
    if _DIFF_MARKUP_RE.search(old_mid) or _DIFF_MARKUP_RE.search(new_mid):
        return None
    if not _is_balanced_html(old_mid) or not _is_balanced_html(new_mid):
        return None
    inside = _pending_diff_at(old_part, start)
    if inside == "del":
        return None
    replacement = new_mid if inside == "ins" else diff_html(old_mid, new_mid)
    return old_part[:start] + replacement + old_part[len(old_part) - tail:]


def apply_edit_blocks_locally(html: str, blocks: List[Dict]) -> Dict:
    current = html
    failed: List[Dict] = []
    under_review: List[Dict] = []
    for block in blocks:
        nxt = _apply_one_edit(current, block["search"], strip_blank_paragraphs(block["replace"]))
        if nxt is None:
            failed.append(block)
            continue
        before = _split_top_level_nodes(current)
        after = _split_top_level_nodes(nxt)
        head = 0
        while head < len(before) and head < len(after) and before[head] == after[head]:
            head += 1
        tail = 0
        while (tail < len(before) - head and tail < len(after) - head
               and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]):
            tail += 1
        old_part = "".join(before[head:len(before) - tail])
        new_part = "".join(after[head:len(after) - tail])
        replacement = _diff_changed_span(old_part, new_part)
        if replacement is None:
            replacement = None if _DIFF_MARKUP_RE.search(old_part) else diff_html(old_part, new_part)
        if replacement is None:
            failed.append(block)
            under_review.append(block)
            continue
        current = "".join(before[:head]) + replacement + "".join(before[len(before) - tail:])
    return {"html": current, "failed": failed, "underReview": under_review}


def strip_blank_paragraphs(html: str) -> str:
    out = re.sub(r"<p>\s*(<br\s*/?>)?\s*</p>", "", html, flags=re.I)
    out = re.sub(r"<p>(\s|&nbsp;)+</p>", "", out, flags=re.I)
    return re.sub(r"(</(p|h[1-6]|blockquote|ul|ol|li|div)>)\s+(<(p|h[1-6]|blockquote|ul|ol|li|div)[\s>])", r"\1\3", out, flags=re.I)


_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿぀-ゟ゠-ヿ가-힯]")
# \p{L}\p{N}: letters and numbers. Python's \w (str) is alphanumerics per
# str.isalnum() plus underscore; removing the underscore gives the same set.
_WORD_RE = re.compile(r"[^\W_]+(?:[''‑][^\W_]+)*")


# ── Status declaration ──────────────────────────────────────────────────────

_DOC_STATUS_RE = re.compile(r"<doc_status>\s*(updated|unchanged)\s*</doc_status>", re.I)
_TRAILER_OPEN = "<doc_status>"
_TRAILER_CLOSE = "</doc_status>"


def parse_doc_status(full_text: str) -> Optional[str]:
    m = _DOC_STATUS_RE.search(full_text or "")
    return m.group(1).lower() if m else None


def _is_partial_trailer(tail: str) -> bool:
    t = tail.lower()
    if _TRAILER_OPEN.startswith(t):
        return True
    m = re.match(r"^<doc_status>\s*([a-z]*)(</?[a-z_]*)?$", t)
    if not m:
        return False
    word, close = m.group(1), m.group(2)
    if word and not "updated".startswith(word) and not "unchanged".startswith(word):
        return False
    if close and not _TRAILER_CLOSE.startswith(close):
        return False
    return True


def strip_doc_status(text: str) -> str:
    out = _DOC_STATUS_RE.sub("", text or "", count=1)
    lt = out.rfind("<")
    if lt != -1 and lt < len(out) - 1 and _is_partial_trailer(out[lt:]):
        out = out[:lt]
    return out.rstrip(_JS_WS)


_SELF_CLAIM_PATTERNS = [
    re.compile(r"\bi(?:'ve| have)?\s+(?:just\s+)?(?:updated|rewritten|rewrote|revised|edited|expanded|added|inserted|removed|deleted|replaced|continued|drafted)\b", re.I),
    re.compile(r"\bhere(?:'s| is)\s+the\s+(?:updated|revised|rewritten|new)\b", re.I),
    re.compile(r"我已(?:经)?[^。！？；\n]{0,10}?(?:更新|改写|重写|修改|润色|扩写|续写|写好|写完|写入|改好|补上|添加|删除|替换)"),
    re.compile(r"(?<![你您])已(?:经)?(?:帮你|为你|把|将)[^。！？；\n]{0,10}?(?:更新|改写|重写|修改|润色|扩写|续写|写好|写完|写入|改好|补上|添加|删除|替换)"),
]


def claims_own_write(text: str) -> bool:
    return any(p.search(text or "") for p in _SELF_CLAIM_PATTERNS)


def detect_failed_document_update(full_text: str) -> Optional[str]:
    text = _js_trim(full_text or "")
    if not text:
        return None
    if re.search(r"<edits?\b|<{5,}\s*SEARCH", text, re.I):
        return "malformed"
    if re.search(r"<(?:canvas|selection_replace)\b", text, re.I):
        return "malformed"
    declared = parse_doc_status(text)
    if declared == "updated":
        return "claimed"
    if declared == "unchanged":
        return "claimed" if any(p.search(text) for p in _SELF_CLAIM_PATTERNS) else None
    return "undeclared"


def trim_incomplete_html_tail(html: str) -> str:
    out = html
    last_lt = out.rfind("<")
    if last_lt != -1 and out.find(">", last_lt) == -1:
        out = out[:last_lt]
    last_amp = out.rfind("&")
    if last_amp != -1 and out.find(";", last_amp) == -1 and len(out) - last_amp <= 10:
        out = out[:last_amp]
    return out


def is_blank_content(html: str) -> bool:
    if re.search(r"<(img|video|audio|iframe)\b", html, re.I):
        return False
    return not re.sub(r"&nbsp;|\s", "", re.sub(r"<[^>]+>", "", html))

"""Port of src/utils/paragraphs.ts — a chapter as numbered paragraphs."""
import math
import re
from typing import Dict, List, Optional

from .dom import Element, parse_fragment, serialize
from .llm_context import _js_trim


def _decode_entities(s: str) -> str:
    return (s.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&"))


def block_text(html: str) -> str:
    """blockText: tags dropped, list items and line breaks kept apart."""
    out = re.sub(r"<(?:br|hr)\s*/?>", "\n", html, flags=re.I)
    out = re.sub(r"</li>", "\n", out, flags=re.I)
    out = re.sub(r"<[^>]+>", "", out)
    out = _decode_entities(out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return _js_trim(out)


def top_level_blocks(html: str) -> List[str]:
    """topLevelBlocks: body children re-serialized; loose text becomes a <p> of its text."""
    blocks: List[str] = []
    for node in parse_fragment(html).children:
        if isinstance(node, Element):
            blocks.append(serialize(node))
        else:
            text = _js_trim(node.data)
            if text:
                blocks.append(f"<p>{text}</p>")
    return blocks


_IMAGE_RE = re.compile(r"<img\b|\{\{IMAGE_PLACEHOLDER_[0-9]+\}\}", re.I)


def paragraph_blocks(html: str) -> List[str]:
    """Port of paragraphBlocks: the top-level blocks, a list counting each of its items."""
    out: List[str] = []

    def add(node) -> None:
        if isinstance(node, Element):
            out.append(serialize(node))
        else:
            text = _js_trim(node.data)
            if text:
                out.append(f"<p>{text}</p>")
    for node in parse_fragment(html).children:
        if isinstance(node, Element) and node.tag.lower() in ("ul", "ol"):
            for child in node.children:
                add(child)
        else:
            add(node)
    return out


def chapter_paragraphs(html: str) -> List[Dict]:
    out: List[Dict] = []
    number = 0
    for block in paragraph_blocks(html):
        text = _js_trim(re.sub(r"\{\{IMAGE_PLACEHOLDER_[0-9]+\}\}", "", block_text(block)))
        image = bool(_IMAGE_RE.search(block))
        if not text and not image:
            continue
        number += 1
        if re.match(r"<h[1-6]\b", block, re.I):
            kind = "heading"
        elif image and not text:
            kind = "image"
        elif re.match(r"<p\b", block, re.I):
            kind = "paragraph"
        elif re.match(r"<li\b", block, re.I):
            kind = "item"
        else:
            kind = "other"
        entry = {"number": number, "html": block, "text": text, "kind": kind}
        if image and text:
            entry["image"] = True  # a text paragraph carrying an image (see ChapterParagraph.image)
        out.append(entry)
    return out


def chapter_chars(html: str) -> int:
    return sum(len(p["text"]) for p in chapter_paragraphs(html))


def numbered_line(p: Dict) -> str:
    if p["kind"] == "image":
        return f"¶{p['number']} [image]"
    mark = "# " if p["kind"] == "heading" else "• " if p["kind"] == "item" else ""
    return f"¶{p['number']} {mark}{re.sub(r'\n+', ' / ', searchable_text(p))}"


def searchable_text(p: Dict) -> str:
    """Port of searchableText: the text as grep searches it and the read shows it, with "[image]" when it carries one."""
    if p["kind"] == "image":
        return "[image]"
    return f"{p['text']} [image]" if p.get("image") else p["text"]


_BLOCK_IMAGE_RE = re.compile(r"<img\b[^>]*>|\{\{IMAGE_PLACEHOLDER_[0-9]+\}\}", re.I)


def block_images(html: str) -> List[str]:
    """Port of blockImages: the image tags of a block, to keep when its text is replaced."""
    return _BLOCK_IMAGE_RE.findall(html)


# ── Paragraphs by position (agentic_chat_loop.md §0.11) ─────────────────────
# Port of the same section of src/utils/paragraphs.ts.

from .jsstr import JS_WS  # noqa: E402

_WS_CLASS = "[" + "".join("\\" + c if c in "\\]^-" else c for c in JS_WS) + "]"
_WS_RUN = re.compile(_WS_CLASS + "+")
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_OPEN_NAME = re.compile(r"^<([a-zA-Z][\w-]*)")
_PLAIN_BLOCK = re.compile(r"^<(p|h[1-6])>(?:[^<]|<br" + _WS_CLASS + r"*/?>)*</\1>$", re.I)
_BLOCK_HTML = re.compile(r"^" + _WS_CLASS + r"*<(?:p|h[1-6]|blockquote|ul|ol|div|hr|figure|img|pre|table)\b", re.I)
_ACTIONS = ["replace", "insert_before", "insert_after", "delete"]


def _tag_end(html: str, start: int) -> int:
    quote = None
    for i in range(start + 1, len(html)):
        ch = html[i]
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == ">":
            return i + 1
    return -1


def paragraph_spans(html: str) -> List[Dict]:
    out: List[Dict] = []
    i = 0
    n = len(html)
    while i < n:
        if html[i] != "<":
            nxt = html.find("<", i)
            end = n if nxt == -1 else nxt
            if _js_trim(html[i:end]):
                out.append({"start": i, "end": end, "html": html[i:end], "loose": True})
            i = end
            continue
        if html.startswith("<!--", i):
            close = html.find("-->", i + 4)
            i = n if close == -1 else close + 3
            continue
        m = _OPEN_NAME.match(html[i:i + 40])
        close_at = _tag_end(html, i)
        if not m or close_at == -1:
            i = n if close_at == -1 else close_at
            continue
        name = m.group(1).lower()
        if name in _VOID_TAGS or html[close_at - 2] == "/":
            out.append({"start": i, "end": close_at, "html": html[i:close_at], "loose": False})
            i = close_at
            continue
        pattern = re.compile(r"<(/?)" + re.escape(name) + r"(?=[" + "".join("\\" + c if c in "\\]^-" else c for c in JS_WS) + r"/>])", re.I)
        depth = 1
        end = n
        pos = close_at
        while True:
            mm = pattern.search(html, pos)
            if not mm:
                break
            at = _tag_end(html, mm.start())
            if at == -1:
                break
            if mm.group(1):
                depth -= 1
            elif html[at - 2] != "/":
                depth += 1
            if depth == 0:
                end = at
                break
            pos = at
        if name in ("ul", "ol"):
            # A list counts its items (paragraph_blocks): their spans, inside it.
            closing = re.search(r"</" + name + _WS_CLASS + r"*>$", html[i:end], re.I)
            inner_end = end - len(closing.group(0)) if closing else end
            for s in paragraph_spans(html[close_at:inner_end]):
                out.append({**s, "start": s["start"] + close_at, "end": s["end"] + close_at})
            i = end
            continue
        out.append({"start": i, "end": end, "html": html[i:end], "loose": False})
        i = end
    return out


def numbered_paragraph_spans(html: str):
    numbered = chapter_paragraphs(html)
    out: List[Dict] = []
    for span in paragraph_spans(html):
        block = f"<p>{_js_trim(span['html'])}</p>" if span["loose"] else span["html"]
        text = _js_trim(re.sub(r"\{\{IMAGE_PLACEHOLDER_[0-9]+\}\}", "", block_text(block)))
        if not text and not _IMAGE_RE.search(block):
            continue
        p = numbered[len(out)] if len(out) < len(numbered) else None
        if p is None or p["text"] != text:
            return None
        out.append({**span, "number": p["number"], "text": text, "kind": p["kind"]})
    return out if len(out) == len(numbered) else None


def is_plain_chapter_html(html: str) -> bool:
    spans = paragraph_spans(html)
    if numbered_paragraph_spans(html) is None:
        return False
    return all(not s["loose"] and _PLAIN_BLOCK.match(_js_trim(s["html"])) for s in spans)


_BR = "<br" + _WS_CLASS + r"*/?>"
_IMAGE_ONLY_BLOCK = re.compile(r"^<p>(?:" + _WS_CLASS + "|" + _BR + r")*(?:<img\b[^>]*>(?:" + _WS_CLASS + "|" + _BR + r")*)+</p>$", re.I)
_IMAGE_MARKER_PARAGRAPH = re.compile(r"<p>(?:" + _WS_CLASS + r"|\[image\])*\[image\](?:" + _WS_CLASS + r"|\[image\])*</p>")


def is_text_rewritable_html(html: str) -> bool:
    """Port of isTextRewritableHtml: plain, or plain but for paragraphs that hold only images."""
    spans = paragraph_spans(html)
    if numbered_paragraph_spans(html) is None:
        return False
    return all(not s["loose"] and (_PLAIN_BLOCK.match(_js_trim(s["html"])) or _IMAGE_ONLY_BLOCK.match(_js_trim(s["html"]))) for s in spans)


def strip_image_marker_paragraphs(html: str) -> str:
    """Port of stripImageMarkerParagraphs."""
    return _IMAGE_MARKER_PARAGRAPH.sub("", html)


def _squash(s: str) -> str:
    return _js_trim(_WS_RUN.sub(" ", s))


def _anchor_of(s: str) -> str:
    out = re.sub(r"^¶\d+" + _WS_CLASS + "*", "", _squash(s))
    out = re.sub(r"^#" + _WS_CLASS + "+", "", out)
    return re.sub(r"^•" + _WS_CLASS + "*", "", out)


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i]
        for j in range(1, len(b) + 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1)))
        prev = cur
    return prev[len(b)]


def anchor_matches(text: str, anchor: str) -> bool:
    """Port of anchorMatches: the paragraph starts with the anchor, a slip or two allowed in a long one."""
    own = _squash(text)
    # A paragraph whose own text starts with "•" is compared without it too (anchorMatches in paragraphs.ts).
    bare = re.sub(r"^•" + _WS_CLASS + "*", "", own, count=1)
    if bare != own and anchor_matches(bare, anchor):
        return True
    t = own
    if t.startswith(anchor):
        return True
    k = 2 if len(anchor) >= 16 else 1 if len(anchor) >= 6 else 0
    if k == 0:
        return False
    for length in range(len(anchor) - k, len(anchor) + k + 1):
        if 0 < length <= len(t) and _levenshtein(anchor, t[:length]) <= k:
            return True
    return False


def as_blocks(html: str) -> str:
    if _BLOCK_HTML.match(html):
        return _js_trim(html)
    parts = [_js_trim(p) for p in re.split(r"\n" + _WS_CLASS + r"*\n", html)]
    return "".join("<p>" + p.replace("\n", "<br>") + "</p>" for p in parts if p)


def as_items(html: str) -> str:
    """Port of asItems: content beside or in place of a list entry stays an entry."""
    return _js_trim(html) if re.match(_WS_CLASS + r"*<li\b", html, re.I) else f"<li>{as_blocks(html)}</li>"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def apply_paragraph_edits(html: str, edits: List[Dict]) -> Dict:
    """Port of applyParagraphEdits: each edit checked on its own; the ones that fit apply, the rest are reported."""
    spans = numbered_paragraph_spans(html)
    if spans is None:
        return {"ok": False, "error": "this chapter's HTML could not be split into paragraphs reliably; use edit_document with SEARCH text from its HTML instead", "stale": []}
    if not edits:
        return {"ok": False, "error": "no edits were given", "stale": []}
    stale: List[Dict] = []
    errors: List[str] = []
    removed = set()
    valid: List[Dict] = []
    for e in edits:
        n = e["paragraph"]
        span = spans[n - 1] if _is_int(n) and 1 <= n <= len(spans) else None
        if e["action"] not in _ACTIONS:
            errors.append(f'¶{n}: unknown action "{e["action"]}"')
            continue
        if span is None:
            errors.append(f"¶{n}: the chapter has {len(spans)} paragraphs")
            continue
        if (e["action"] == "replace" or e["action"].startswith("insert")) and not _js_trim(e.get("html") or ""):
            errors.append(f"¶{n}: {e['action']} needs html")
            continue
        anchor = _anchor_of(e.get("startsWith") or "")
        if not anchor:
            errors.append(f"¶{n}: starts_with is empty — give the first words of the paragraph as you read them")
            continue
        if not anchor_matches(span["text"], anchor) and not (span["kind"] == "image" and re.match(r"^\[?image\]?$", anchor, re.I)):
            stale.append({"number": span["number"], "text": "[image]" if span["kind"] == "image" else span["text"]})
            continue
        if e["action"] == "replace" and span["kind"] == "image":
            errors.append(f"¶{n} is an image: delete it, or insert next to it")
            continue
        if e["action"] in ("replace", "delete"):
            if n in removed:
                errors.append(f"¶{n} is replaced or deleted twice")
                continue
            removed.add(n)
        valid.append(e)
    skipped = errors + [f"¶{s['number']} does not start with what you gave; it now reads: {s['text'][:160] + '…' if len(s['text']) > 160 else s['text']}" for s in stale]
    if not valid:
        return {"ok": False, "error": "nothing was applied:\n" + "\n".join(skipped), "stale": stale}
    rank = {"insert_after": 0, "replace": 1, "delete": 1, "insert_before": 2}
    splices = []
    for order, e in enumerate(valid):
        span = spans[e["paragraph"] - 1]
        at = span["end"] if e["action"] == "insert_after" else span["start"]
        to = span["end"] if e["action"] in ("replace", "delete") else at
        text = "" if e["action"] == "delete" else as_items(e.get("html") or "") if span["kind"] == "item" else as_blocks(e.get("html") or "")
        images = block_images(html[span["start"]:span["end"]]) if e["action"] == "replace" else []
        if images and not block_images(text):
            closing = re.compile(r"(</(?:p|h[1-6]|li|blockquote|div)>)\s*$", re.I)
            text = closing.sub(lambda m: "".join(images) + m.group(1), text, count=1) if closing.search(text) else f"{text}<p>{''.join(images)}</p>"
        splices.append({"at": at, "to": to, "text": text, "paragraph": e["paragraph"], "rank": rank[e["action"]], "order": order})
    splices.sort(key=lambda s: (-s["at"], -s["paragraph"], s["rank"], -s["order"]))
    out = html
    for s in splices:
        out = out[:s["at"]] + s["text"] + out[s["to"]:]
    return {"ok": True, "html": out, "paragraphsBefore": len(spans), "paragraphsAfter": len(chapter_paragraphs(out)),
            "firstChanged": min(e["paragraph"] for e in valid), "applied": len(valid), "skipped": skipped, "stale": stale}


#: A whole rewrite keeps at least this share of a chapter's text unless the model insists (rewriteLoss).
REWRITE_KEEP_RATIO = 0.85


def rewrite_loss(old_html: str, new_html: str) -> Optional[Dict[str, int]]:
    """Port of rewriteLoss: what a whole rewrite would drop, or None."""
    before = chapter_chars(old_html)
    if before < 200:
        return None
    after = chapter_chars(new_html)
    lost_headings = max(0, len(re.findall(r"<h[1-6][\s>]", old_html, re.I)) - len(re.findall(r"<h[1-6][\s>]", new_html, re.I)))
    lost_items = max(0, len(re.findall(r"<li[\s>]", old_html, re.I)) - len(re.findall(r"<li[\s>]", new_html, re.I)))
    if after >= before * REWRITE_KEEP_RATIO and lost_headings == 0 and lost_items < 2:
        return None
    return {"before": before, "after": after, "lostHeadings": lost_headings, "lostItems": lost_items}


def rewrite_loss_note(cite: str, loss: Dict[str, int]) -> str:
    """Port of rewriteLossNote."""
    pct = math.floor((1 - loss["after"] / loss["before"]) * 100 + 0.5)  # Math.round
    dropped = [x for x in [
        f"{loss['lostHeadings']} heading{'' if loss['lostHeadings'] == 1 else 's'}" if loss["lostHeadings"] > 0 else "",
        f"{loss['lostItems']} list item{'' if loss['lostItems'] == 1 else 's'}" if loss["lostItems"] > 0 else "",
    ] if x]
    return (f"The rewrite of {cite} was NOT applied: it would take the chapter from {loss['before']} to {loss['after']} characters"
            + (f" (−{pct}%)" if pct > 0 else "") + (f", dropping {' and '.join(dropped)}" if dropped else "") + ". "
            "A whole rewrite replaces everything, so whatever it leaves out is deleted. "
            "To add to the chapter or bring it up to date, keep its text and change only what changes: edit_paragraphs (by ¶ number) or edit_document / <edit> blocks. "
            "If the user asked for it to be shorter, or its text moved to other chapters, send the same rewrite again and it will be applied.")

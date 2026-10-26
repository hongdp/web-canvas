"""Port of src/utils/titleSync.ts — a chapter's title and its leading <h1>."""
import re
from typing import Optional

from .dom import Element, parse_fragment
from .jsstr import js_replace, js_trim

_LEADING_H1 = re.compile(r"^\s*<h1[^>]*>([\s\S]{0,2000}?)</h1>", re.I)


def _dom_text(html: str) -> str:
    """convert.htmlToPlainText: <del> removed, then the body's textContent."""
    body = parse_fragment(html)

    def strip_del(el: Element) -> None:
        el.children = [c for c in el.children if not (isinstance(c, Element) and c.tag == "del")]
        for c in el.children:
            if isinstance(c, Element):
                strip_del(c)
    strip_del(body)
    return body.text_content()


def leading_h1_text(html: str) -> Optional[str]:
    m = _LEADING_H1.search(html[:4096])
    if not m:
        return None
    text = js_trim(_dom_text(m.group(1)))
    return text or None


def _escape_html_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def content_with_renamed_heading(content: str, new_title: str) -> Optional[str]:
    title = js_trim(new_title)
    if not title:
        return None
    m = _LEADING_H1.search(content[:4096])
    if not m:
        return None
    if "data-diff" in m.group(0):
        return None
    if js_trim(_dom_text(m.group(1))) == title:
        return None
    start = content.find(m.group(0))
    rewritten = js_replace(m.group(0), m.group(1), _escape_html_text(title), 1)
    return content[:start] + rewritten + content[start + len(m.group(0)):]

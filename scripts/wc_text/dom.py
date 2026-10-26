"""A small HTML fragment DOM with browser-style serialization.

The TypeScript helpers go through DOMParser and read `outerHTML` /
`innerHTML` back, which re-serializes: entities in text are decoded and only
&, <, > and U+00A0 re-escaped; attribute values are double-quoted with &, "
and U+00A0 escaped; void elements lose their slash and have no end tag; a
block left open is closed. Everything that compares with those bytes goes
through here rather than slicing the source.
"""
from html.parser import HTMLParser
from typing import List, Optional, Tuple, Union

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
# Opening one of these closes an open <p> (HTML5 "closes a p element" set).
CLOSES_P = {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset", "figcaption", "figure", "footer",
            "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol", "p", "pre",
            "section", "table", "ul"}


class Text:
    __slots__ = ("data", "parent")

    def __init__(self, data: str) -> None:
        self.data = data
        self.parent: Optional["Element"] = None


class Comment:
    __slots__ = ("data", "parent")

    def __init__(self, data: str) -> None:
        self.data = data
        self.parent: Optional["Element"] = None


class Element:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: List[Node] = []
        self.parent: Optional["Element"] = None

    def append(self, node: "Node") -> None:
        node.parent = self
        self.children.append(node)

    def get(self, name: str) -> Optional[str]:
        for k, v in self.attrs:
            if k == name:
                return v if v is not None else ""
        return None

    def text_content(self) -> str:
        return "".join(c.data if isinstance(c, Text) else c.text_content() if isinstance(c, Element) else "" for c in self.children)

    def descendants(self):
        for c in self.children:
            if isinstance(c, Element):
                yield c
                yield from c.descendants()


Node = Union[Element, Text, Comment]


def escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace(" ", "&nbsp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace(" ", "&nbsp;").replace('"', "&quot;")


def serialize(node: Node) -> str:
    if isinstance(node, Text):
        return escape_text(node.data)
    if isinstance(node, Comment):
        return f"<!--{node.data}-->"
    attrs = "".join(f' {k}="{escape_attr(v if v is not None else "")}"' for k, v in node.attrs)
    if node.tag in VOID:
        return f"<{node.tag}{attrs}>"
    return f"<{node.tag}{attrs}>{inner_html(node)}</{node.tag}>"


def inner_html(node: Element) -> str:
    return "".join(serialize(c) for c in node.children)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body = Element("body", [])
        self.stack: List[Element] = [self.body]

    @property
    def current(self) -> Element:
        return self.stack[-1]

    def _close_p_if_open(self) -> None:
        if self.current.tag == "p":
            self.stack.pop()

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in CLOSES_P:
            self._close_p_if_open()
        if tag == "li" and self.current.tag == "li":
            self.stack.pop()
        el = Element(tag, [(k.lower(), v) for k, v in attrs])
        self.current.append(el)
        if tag not in VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # A stray end tag is ignored, as browsers ignore it.

    def handle_data(self, data):
        # Whitespace before the first node is consumed by the "before body"
        # insertion modes; a browser's body never starts with it.
        if self.current is self.body and not self.body.children:
            data = data.lstrip(" \t\n\r\f")
        if data:
            self.current.append(Text(data))

    def handle_comment(self, data):
        self.current.append(Comment(data))


def parse_fragment(html: str) -> Element:
    """The <body> of `html` parsed as a fragment, children in document order."""
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.body

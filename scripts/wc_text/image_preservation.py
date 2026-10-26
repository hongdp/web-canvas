"""Port of src/utils/imagePreservation.ts — placeholders in, tags out, and
the safety net that puts back images a rewrite dropped."""
import math
import re
from typing import Dict, List

from .dom import Element, Node, inner_html, parse_fragment
from .llm_context import _js_trim

_PLACEHOLDER_TOKEN_RE = re.compile(r"\{{0,3}\s*IMAGE[_\s-]?PLACEHOLDER[_\s-]?([0-9]+)\s*\}{0,3}", re.I)
_PLACEHOLDER_IMG_TAG_RE = re.compile(r"<img\b[^>]*?IMAGE[_\s-]?PLACEHOLDER[_\s-]?([0-9]+)[^>]*>", re.I)


def replace_images_with_placeholders(html: str, registry: List[Dict[str, str]]) -> str:
    """Registry entries are {"placeholder", "tag"}; new tags are appended (mutated, like the TS)."""
    out = html
    for match in re.findall(r"<img[^>]+>", html):
        existing = next((e for e in registry if e["tag"] == match), None)
        if existing is None:
            existing = {"placeholder": f"{{{{IMAGE_PLACEHOLDER_{len(registry)}}}}}", "tag": match}
            registry.append(existing)
        out = out.replace(match, existing["placeholder"], 1)
    return out


def register_image(registry: List[Dict[str, str]], data_url: str, alt: str = "") -> str:
    """Port of registerImage: an image the run fetched (web_images) joins the registry as a tag; its placeholder is returned."""
    safe_alt = (alt or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    tag = f'<img src="{data_url}" alt="{safe_alt}">' if safe_alt else f'<img src="{data_url}">'
    existing = next((e for e in registry if e["tag"] == tag), None)
    if existing is not None:
        return existing["placeholder"]
    entry = {"placeholder": f"{{{{IMAGE_PLACEHOLDER_{len(registry)}}}}}", "tag": tag}
    registry.append(entry)
    return entry["placeholder"]


def restore_image_placeholders(html: str, registry: List[Dict[str, str]]) -> str:
    def lookup(m):
        index = int(m.group(1))
        return registry[index]["tag"] if 0 <= index < len(registry) else ""
    out = _PLACEHOLDER_IMG_TAG_RE.sub(lookup, html)
    return _PLACEHOLDER_TOKEN_RE.sub(lookup, out)


def _normalize_text(s: str) -> str:
    return _js_trim(re.sub(r"\s+", " ", s or ""))


def _clone(node: Node) -> Node:
    if isinstance(node, Element):
        copy = Element(node.tag, list(node.attrs))
        for child in node.children:
            copy.append(_clone(child))
        return copy
    return type(node)(node.data)


def _insert_after(ref: Element, node: Node) -> None:
    parent = ref.parent
    assert parent is not None
    node.parent = parent
    parent.children.insert(parent.children.index(ref) + 1, node)


def _element_children(el: Element) -> List[Element]:
    return [c for c in el.children if isinstance(c, Element)]


def reinsert_missing_images(new_html: str, original_html: str) -> Dict:
    """reinsertMissingImages: every <img> of the original whose src the rewrite
    lost is put back after the block whose text anchors it, else by position."""
    orig_body = parse_fragment(original_html)
    orig_imgs = [e for e in orig_body.descendants() if e.tag == "img"]
    if not orig_imgs:
        return {"html": new_html, "reinserted": 0}

    new_body = parse_fragment(new_html)
    present = {(e.get("src") or "") for e in new_body.descendants() if e.tag == "img"}
    orig_blocks = _element_children(orig_body)
    reinserted = 0

    for img in orig_imgs:
        src = img.get("src") or ""
        if not src or src in present:
            continue
        block: Element = img
        while block.parent is not None and block.parent is not orig_body:
            block = block.parent
        block_idx = orig_blocks.index(block) if block in orig_blocks else -1

        anchor_text = ""
        for i in range(block_idx - 1, -1, -1):
            t = _normalize_text(orig_blocks[i].text_content())
            if t:
                anchor_text = t
                break

        clone = _clone(img)
        placed = False
        if anchor_text:
            anchor_key = anchor_text[-80:]
            for nb in _element_children(new_body):
                if anchor_key in _normalize_text(nb.text_content()):
                    _insert_after(nb, clone)
                    placed = True
                    break

        if not placed and block_idx >= 0:
            new_blocks = _element_children(new_body)
            if block_idx == 0 or not new_blocks:
                clone.parent = new_body
                new_body.children.insert(0, clone)
            else:
                # Math.round: halves go up, unlike Python's round().
                at = min(len(new_blocks) - 1, math.floor(block_idx / len(orig_blocks) * len(new_blocks) + 0.5))
                _insert_after(new_blocks[at], clone)
            placed = True

        if not placed:
            new_body.append(clone)
        present.add(src)
        reinserted += 1

    if reinserted == 0:
        return {"html": new_html, "reinserted": 0}
    return {"html": inner_html(new_body), "reinserted": reinserted}

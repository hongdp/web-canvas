"""Port of src/utils/diffResolution.ts — the string half only.

`collectDiffRanges` walks ProseMirror nodes in the editor and stays there.
"""
import re


def resolve_diff_markup_in_html(html: str, action: str) -> str:
    """resolveDiffMarkupInHtml: action 'accept' | 'reject'."""
    removed = "ins" if action == "reject" else "del"
    kept = "del" if action == "reject" else "ins"
    body = rf"(?:(?!</{removed}\b)[\s\S])*"
    emptied_block = re.compile(
        rf'<(p|h[1-6]|li|blockquote)\b[^>]*>\s*(?:<{removed}[^>]*data-diff-id="[^"]*"[^>]*>{body}</{removed}>\s*)+</\1>', re.I)
    stripped = emptied_block.sub("", html)
    if stripped != html:
        previous = None
        while previous != stripped:
            previous = stripped
            stripped = re.sub(r"<(li|ul|ol|blockquote)\b[^>]*>\s*</\1>", "", stripped, flags=re.I)
    stripped = re.sub(rf'<{removed}[^>]*data-diff-id="[^"]*"[^>]*>[\s\S]*?</{removed}>', "", stripped, flags=re.I)
    return re.sub(rf'<{kept}[^>]*data-diff-id="[^"]*"[^>]*>([\s\S]*?)</{kept}>', r"\1", stripped, flags=re.I)

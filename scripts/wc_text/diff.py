"""Port of src/utils/diff.ts — the block-then-token LCS diff and stripDiffMarkup."""
import random
import re
import string
from typing import Callable, List, Optional, Tuple

_TOKEN_RE = re.compile(r"(<[^>]+>|[^\s<]+|\s+)")
_BLOCK_CLOSE_RE = re.compile(r"^</(p|h[1-6]|li|blockquote|pre|ul|ol|div|td|th|tr|table|figure|figcaption|section|article|header|footer)>$", re.I)
MAX_LCS_CELLS = 1_500_000

Item = Tuple[str, str]  # (type, value)


def _is_tag(token: str) -> bool:
    return token.startswith("<") and token.endswith(">")


def _compute_lcs(xs: List[str], ys: List[str]) -> List[Item]:
    m, n = len(xs), len(ys)
    table = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        xi = xs[i - 1]
        row, prev = table[i], table[i - 1]
        for j in range(1, n + 1):
            if xi == ys[j - 1]:
                row[j] = prev[j - 1] + 1
            else:
                a, b = prev[j], row[j - 1]
                row[j] = a if a > b else b
    i, j = m, n
    result: List[Item] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and xs[i - 1] == ys[j - 1]:
            result.append(("equal", xs[i - 1])); i -= 1; j -= 1
        elif j > 0 and (i == 0 or table[i][j - 1] >= table[i - 1][j]):
            result.append(("insert", ys[j - 1])); j -= 1
        else:
            result.append(("delete", xs[i - 1])); i -= 1
    result.reverse()
    return result


def _group_into_blocks(tokens: List[str]) -> List[List[str]]:
    blocks: List[List[str]] = []
    current: List[str] = []
    for tok in tokens:
        current.append(tok)
        if _BLOCK_CLOSE_RE.match(tok):
            blocks.append(current); current = []
    if current:
        blocks.append(current)
    return blocks


def _diff_tokens(old: List[str], new: List[str]) -> List[Item]:
    prefix = 0
    while prefix < len(old) and prefix < len(new) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < len(old) - prefix and suffix < len(new) - prefix and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    middle_old = old[prefix:len(old) - suffix]
    middle_new = new[prefix:len(new) - suffix]
    if len(middle_old) * len(middle_new) > MAX_LCS_CELLS:
        middle = [("delete", v) for v in middle_old] + [("insert", v) for v in middle_new]
    else:
        middle = _compute_lcs(middle_old, middle_new)
    return [("equal", v) for v in old[:prefix]] + middle + [("equal", v) for v in old[len(old) - suffix:]]


def _random_id() -> str:
    alphabet = string.digits + string.ascii_lowercase
    return "diff-" + "".join(random.choice(alphabet) for _ in range(7))


def diff_html(old_html: str, new_html: str, next_id: Optional[Callable[[], str]] = None) -> str:
    """diffHtml. `next_id` replaces the random group id (tests pass a counter)."""
    make_id = next_id or _random_id
    old_tokens = [m.group(0) for m in _TOKEN_RE.finditer(old_html)]
    new_tokens = [m.group(0) for m in _TOKEN_RE.finditer(new_html)]
    old_blocks = _group_into_blocks(old_tokens)
    new_blocks = _group_into_blocks(new_tokens)
    block_diff = _compute_lcs(["".join(b) for b in old_blocks], ["".join(b) for b in new_blocks])

    full: List[Item] = []
    oi = ni = 0
    pending_old: List[str] = []
    pending_new: List[str] = []

    def flush() -> None:
        nonlocal pending_old, pending_new
        if pending_old and pending_new:
            full.extend(_diff_tokens(pending_old, pending_new))
        elif pending_old:
            full.extend(("delete", v) for v in pending_old)
        elif pending_new:
            full.extend(("insert", v) for v in pending_new)
        pending_old, pending_new = [], []

    for kind, _ in block_diff:
        if kind == "equal":
            flush()
            full.extend(("equal", v) for v in old_blocks[oi]); oi += 1; ni += 1
        elif kind == "delete":
            pending_old.extend(old_blocks[oi]); oi += 1
        else:
            pending_new.extend(new_blocks[ni]); ni += 1
    flush()

    out: List[str] = []
    idx = 0
    while idx < len(full):
        kind, value = full[idx]
        if kind == "equal":
            out.append(value); idx += 1
            continue
        diff_id = make_id()
        deleted: List[str] = []
        inserted: List[str] = []
        while idx < len(full) and full[idx][0] != "equal":
            k, v = full[idx]
            (deleted if k == "delete" else inserted).append(v)
            idx += 1
        # An <img> is content, not structure: marked like text (diff.ts isMarkedContent).
        deleted_text = "".join(t for t in deleted if _is_marked_content(t))
        inserted_html: List[str] = []
        current_text: List[str] = []

        def flush_insert() -> None:
            if current_text:
                inserted_html.append(f'<ins class="diff-addition" data-diff-id="{diff_id}">{"".join(current_text)}</ins>')
                current_text.clear()

        for token in inserted:
            if not _is_marked_content(token):
                flush_insert(); inserted_html.append(token)
            else:
                current_text.append(token)
        flush_insert()
        if deleted_text:
            out.append(f'<del class="diff-deletion" data-diff-id="{diff_id}">{deleted_text}</del>')
        out.extend(inserted_html)
    return "".join(out)


def _is_marked_content(token: str) -> bool:
    """isMarkedContent: text, or an image — what the review markup carries."""
    return not _is_tag(token) or bool(re.match(r"<img\b", token, re.I))


def strip_diff_markup(html: str) -> str:
    """stripDiffMarkup: the accepted reading — insertions kept, deletions dropped."""
    if not html or ("diff-addition" not in html and "diff-deletion" not in html):
        return html or ""
    out = re.sub(r'<del\b[^>]*class="[^"]*diff-deletion[^"]*"[^>]*>[\s\S]*?</del>', "", html, flags=re.I)
    out = re.sub(r'<ins\b[^>]*class="[^"]*diff-addition[^"]*"[^>]*>([\s\S]*?)</ins>', r"\1", out, flags=re.I)
    return out

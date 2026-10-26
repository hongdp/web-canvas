"""Port of src/utils/polish.ts — the polish pass's pure half: chunking, prompt,
parsing, validation and reassembly."""
import math
import re
from typing import Dict, List, Optional

from .jsstr import js_replace
from .llm_context import _js_trim
from .paragraphs import top_level_blocks

POLISH_CHUNK_CHARS = 1000
POLISH_LENGTH_MIN = 0.9
POLISH_LENGTH_MAX = 1.3
POLISH_MAX_CLAUSE = 30

_PUNCT_RE = re.compile(r"[，。！？、；：…—「」“”‘’（）《》\s·,.!?]")
_BREAK_RE = re.compile(r"[，,。！？!?；;：:…—]+")
_DIALOGUE_RE = re.compile(r"“([^”]{2,})”")
_P_WHOLE_RE = re.compile(r"<p(?:\s[^>]*)?>([\s\S]*)</p>", re.I)
_P_ANY_RE = re.compile(r"<p(?:\s[^>]*)?>([\s\S]*?)</p>", re.I)
_IMAGE_RE = re.compile(r"<img\b|\{\{IMAGE_PLACEHOLDER_\d+\}\}", re.I)
_SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?…”」]*")


def bare(s: str) -> str:
    return _PUNCT_RE.sub("", s)


def _decode_entities(s: str) -> str:
    return (s.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&"))


def _text_of(html: str) -> str:
    return _js_trim(_decode_entities(re.sub(r"<[^>]+>", "", html)))


def split_for_polish(html: str, target: int = POLISH_CHUNK_CHARS) -> List[Dict]:
    segments: List[Dict] = []
    current: List[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            segments.append({"kind": "chunk", "paras": current})
        current = []

    for block in top_level_blocks(html):
        m = _P_WHOLE_RE.fullmatch(block)
        plain = m.group(1) if m and not _IMAGE_RE.search(m.group(1)) else None
        if plain is None or not _text_of(plain):
            flush()
            segments.append({"kind": "fixed", "html": block})
            continue
        current.append(plain)
        if len(_text_of("".join(current))) >= target:
            flush()
    flush()
    return segments


def _last_sentence(paras: List[str]) -> str:
    text = _text_of(paras[-1] if paras else "")
    sentences = _SENTENCE_RE.findall(text) or [text]
    return _js_trim(sentences[-1] if sentences else text)


def build_polish_prompt(template: str, chunk: List[str], previous: Optional[List[str]]) -> str:
    part = "\n".join(f"<p>{p}</p>" for p in chunk)
    out = js_replace(template, "{n}", str(len(bare(_text_of("".join(chunk))))))
    out = js_replace(out, "{prev}", _last_sentence(previous) if previous is not None else "（本段是开头）")
    return js_replace(out, "{part}", part)


def parse_polished(output: str) -> List[str]:
    tagged = [_js_trim(m.group(1)) for m in _P_ANY_RE.finditer(output)]
    paras = tagged if tagged else [_js_trim(line) for line in output.split("\n")]
    return [p for p in paras if _text_of(p)]


def validate_polished(draft: List[str], rewrite: List[str]) -> Dict:
    in_text = "".join(_text_of(p) for p in draft)
    out_text = "".join(_text_of(p) for p in rewrite)
    in_len = len(bare(in_text))
    ratio = len(bare(out_text)) / in_len if in_len > 0 else 0
    reasons: List[str] = []
    if not rewrite:
        reasons.append("empty rewrite")
    if ratio < POLISH_LENGTH_MIN or ratio > POLISH_LENGTH_MAX:
        # Math.round: halves go toward +infinity.
        reasons.append(f"length {'+' if ratio >= 1 else ''}{math.floor((ratio - 1) * 100 + 0.5)}%")
    out_bare = bare(out_text)
    lost = [m for m in _DIALOGUE_RE.finditer(in_text) if bare(m.group(1)) not in out_bare]
    if lost:
        reasons.append(f"{len(lost)} dialogue line(s) changed")
    longest = max([0] + [len(bare(c)) for p in rewrite for c in _BREAK_RE.split(re.sub(r"“[^”]*”", "", _text_of(p)))])
    if longest > POLISH_MAX_CLAUSE:
        reasons.append(f"{longest}-char run-on")
    if len(rewrite) < len(draft) - 1:
        reasons.append(f"{len(draft) - len(rewrite)} paragraphs lost")
    return {"ok": not reasons, "reasons": reasons, "ratio": ratio}


def assemble_polished(segments: List[Dict], rewrites: List[Optional[List[str]]]) -> str:
    chunk = 0
    parts = []
    for seg in segments:
        if seg["kind"] == "fixed":
            parts.append(seg["html"])
            continue
        out = rewrites[chunk] if chunk < len(rewrites) else None
        chunk += 1
        parts.append("".join(f"<p>{p}</p>" for p in (out if out is not None else seg["paras"])))
    return "".join(parts)

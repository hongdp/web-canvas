"""Port of src/utils/retryPolicy.ts — when a failed model call is sent again, and how long to wait."""
import re
from typing import Optional

MAX_TRANSPORT_RETRIES = 4
MAX_RETRY_DELAY_MS = 30_000
_BASE_DELAY_MS = 1_000
RETRYABLE_STATUSES = [408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529]

_CONTEXT_LENGTH_RE = re.compile(r"context[ _-]?length|maximum context|context window|too many tokens|prompt is too long|input is too long|"
                                r"exceeds the (?:model'?s )?(?:maximum|context)", re.I)
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def is_context_length_error(status: int, message: str) -> bool:
    return status in (400, 413, 422) and bool(_CONTEXT_LENGTH_RE.search(message or ""))


def is_retryable_status(status: int, message: str) -> bool:
    return status in RETRYABLE_STATUSES and not is_context_length_error(status, message)


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    trimmed = (value or "").strip()
    if not _NUMBER_RE.match(trimmed):
        return None
    seconds = float(trimmed)
    if seconds <= 0:
        return None
    return int(seconds) if seconds == int(seconds) else seconds


def retry_delay_ms(attempt: int, retry_after_seconds: Optional[float]) -> int:
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return min(_js_round(retry_after_seconds * 1000), MAX_RETRY_DELAY_MS)
    return min(_BASE_DELAY_MS * 2 ** max(0, attempt - 1), MAX_RETRY_DELAY_MS)


def with_jitter(ms: float, unit: float) -> int:
    return _js_round(ms * (0.8 + 0.4 * min(max(unit, 0), 1)))


def _js_round(x: float) -> int:
    """Math.round: halves round up (Python's round() rounds them to even)."""
    import math
    return int(math.floor(x + 0.5))

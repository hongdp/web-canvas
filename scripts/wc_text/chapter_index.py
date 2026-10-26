"""Port of src/utils/chapterIndex.ts."""
from typing import Dict, List, Optional


def build_chapter_index(documents: List[Dict], active_document_id: Optional[str], options: Optional[Dict] = None) -> str:
    """Every chapter by number and title, the open one, and the freshness markers; no summaries (see chapterIndex.ts)."""
    options = options or {}
    if len(documents) < 2:
        return ""
    markers = options.get("markers") or {}
    agent_tools = bool(options.get("agentTools"))
    lines = []
    for idx, doc in enumerate(documents):
        active = doc["id"] == active_document_id
        if active:
            marker = (" [ACTIVE — open in the editor; writes go here unless you name another chapter]" if agent_tools
                      else " [ACTIVE — this is the document you can edit]")
        else:
            marker = ""
        freshness = f" [{markers[doc['id']]}]" if markers.get(doc["id"]) else ""
        lines.append(f'#{idx + 1} "{doc["title"]}"{marker}{freshness}')
    return ("CHAPTER INDEX (all chapters in this book, by position — #4 is the fourth chapter whatever its title says; full text NOT included unless it appears in REFERENCED DOCUMENT CONTEXTS or is the active document):\n"
            + "\n".join(lines))


#: Tokens of chapter text per analyze_book batch, under each provider's window and price line (ANALYZE_BATCH_TOKENS).
ANALYZE_BATCH_TOKENS = {"gemini": 140_000, "anthropic": 120_000, "openai": 80_000, "grok": 140_000, "moonshot": 140_000, "ollama": 20_000}


def analyze_batch_chars(provider: str, cjk_ratio: float) -> int:
    """Port of analyzeBatchChars."""
    from .context_window import tokens_to_chars
    return tokens_to_chars(ANALYZE_BATCH_TOKENS.get(provider, 80_000), cjk_ratio)


def pack_chapters_into_batches(docs: List[Dict], max_chars_per_batch: int) -> List[List[Dict]]:
    batches: List[List[Dict]] = []
    current: List[Dict] = []
    current_chars = 0
    for doc in docs:
        cost = len(doc["content"])
        if current and current_chars + cost > max_chars_per_batch:
            batches.append(current); current = []; current_chars = 0
        current.append(doc); current_chars += cost
    if current:
        batches.append(current)
    return batches

# Cache continuity across turns

Status: built 2026-10-10. Registered in the Decision Log of `docs/design.md`.
Follows [cache_first_context.md](cache_first_context.md) and
[pinned_context.md](pinned_context.md).

## 1. The problem

Within a run every step only appends, so steps hit grok's prefix cache
(85% on average). Across turns they do not: the first step of a turn
diverged from the previous turn's last request — "first 46 of its 62
messages identical", 1% cached — because the conversation was rebuilt:

- the previous turn's final user message had carried the CHAPTER INDEX,
  the active chapter and the ATTACHMENTS index, but history kept only the
  user's words;
- its tool calls, tool results and reasoning were collapsed into the reply
  text and a one-line "[Tools used in this turn: …]";
- a change to a pinned chapter re-planned the ledger, which sits ahead of
  the history, so the whole history was re-read;
- the conversation summary was a separate request that read the dropped
  history again at full price.

grok's partial-prefix hits are unreliable (81 of 98 messages identical
came back 1% cached), so a divergence anywhere usually means the whole
prompt is re-read.

## 2. What Codex and Grok Build do

Read from their source (`openai/codex`, `xai-org/grok-build`): every
request is the previous one plus new messages, across turns too. Session context is sent once and changes are
appended (Codex's world-state diffs; Grok Build's reminders, which even
keep the start-up date and append a note when the day changes). The whole
history — tool calls, results, encrypted reasoning — is replayed as sent.
Tool output is cut once, when recorded. Compaction appends its instruction
to the live conversation, with the same tools, so the summarizing call
itself reads from the cache.

## 3. The design

All of it applies to agent turns (tools on). A tools-off turn keeps the
scorer's ledger and the collapsed history, as before.

### 3.1 Turn transcripts

When a turn ends, its **transcript** is stored: the messages its last
request added after the shared prefix — the final user message exactly as
sent, every step's assistant message (text, tool calls with their argument
bytes, thinking blocks, grok's response items), tool results (as last sent,
elisions included), reminders — plus the step that ended the turn: its
reply with its response items, or, when that step called tools (a step
limit, a write with continue-after-writes off), the calls with their
results, so no call is left unanswered. The next turn's history replays
transcripts verbatim instead of the collapsed user/assistant pair, so its
request extends the previous one (`AgentRun.transcript()` in both loops;
the history window is `utils/turnTranscripts` / `wc_text.turn_transcripts`,
parity-tested).

- **Server runs**: `turn_transcripts(username, book_id, message_id,
  user_message_id, transcript, created_at)`, written when the run finishes
  (done or stopped), read by `server_context.assemble_request`; the newest
  200 per book are kept, and they go with the book.
- **Tab runs**: kept in memory by the chat hook (the last 60 turns, cleared
  with the book, provider or model); after a reload, the previous turns
  fall back to the collapsed pairs (correct, one miss). A rejoined run
  keeps none: it never saw the request it continues.
- A transcript is used only when its user message id is the one right
  before its reply in the history; otherwise the collapsed pair is used.
  Turns without a transcript (older ones, the other transport's) keep the
  collapsed form. A chat cleared or a message deleted simply drops out.
- The summary planner counts a transcript's real size (`weight` on the
  summarizable message), so the history budget stays honest.
- The collapsed messages between transcripts are normalized as before
  (`trimHistoryForContext`); a transcript's messages go out as stored. A
  plain cut (no summary) drops whole units from the front.

### 3.2 Context as differences

With transcripts, each turn's tail stays in history; sending the full
CHAPTER INDEX and active chapter every turn would pile up copies. The tail
of the final user message (agent turns) now sends a part in full only
when the copy the model has is gone or stale:

- **CHAPTER INDEX**, **ATTACHMENTS index**: full when changed, else one
  line, "unchanged since …".
- **Active chapter**: full when it is another chapter, or its text changed,
  else "#n "title" — unchanged since your last turn; its text is earlier in
  this conversation".
- **Pinned chapters**: see 3.3.

"The copy the model has" is recorded per part with the turn (assistant
message id) whose transcript carries it. A part is abbreviated only when
that turn's transcript is replayed verbatim in this request — a turn
summarized away, a tab reload or a missing transcript means the full part
again. So the abbreviation can only save tokens, never hide text.
Selected text and pending changes are per turn, as before.

### 3.3 The ledger is frozen between summaries

The ledger (pinned chapters ahead of the history) is rebuilt only at an
**epoch** boundary: a new, refreshed or dropped conversation summary
(`epochSummary`, the summary's key, in the context state), a request with
no history after the ledger, a provider/model change, or — in a tab — the
first turn after a load. A server state from before this rule keeps its
ledger. Between boundaries its bytes stay put. A pinned chapter edited since it was sent goes into the turn's tail
as "PINNED CHAPTER UPDATED — replaces its earlier copy"; a newly pinned one
as "PINNED CHAPTER"; an unpinned one as a line saying to disregard its
copy. Each is then recorded like the parts in 3.2 (`pinnedUpdates`). The
open pinned chapter is never called unpinned: its text is in the tail. The
chapters the request carries (`inContextIds`, what the run may rewrite
from its text) are the pins — every one current after the updates — and
the open one.

### 3.4 Summaries from the live conversation

When the history passes its budget, the summary is made by appending the
summary instruction (the same fixed sections) to the conversation as it
stands — system prompt, ledger, previous summary, history — with the same
tools (tool choice none), conversation id, model and reasoning effort as
the turn. The summarizer reads that prefix from the cache. The new summary
starts a new epoch: the next request (summary, kept turns, rebuilt ledger,
full tail) is one cold read, as in Codex and Grok Build after compaction.

The instruction (`buildSummaryInstruction`) carries the same seven
sections as the separate summarizer and names the first kept turn by the
user's words. The live prefix is used when it fits the window
(`0.95 × window − min(max output, 8192)`; the call's output is capped at
8192 tokens) and, in a tab, when this tab sent the conversation's last
request (its ledger and tools are known). Otherwise the separate
summarizer of agentic_chat_loop.md §0.9 runs, as before.

## 4. Costs and limits

- History holds more (tool results, written text, tails): the summary is
  made more often.
- On grok the live prefix can pass the 200k long-context line (it is the
  conversation plus one turn), so that one call pays the doubled rate on
  mostly cached tokens. Each summary is one cold read; between them, every
  turn's first step is cached.
- Transcripts carry images (base64) of the turns that sent them.
- Not built: a cache key per sub-call (polish, analyze, chapter summaries
  stay on their own keys — their content shares nothing with the
  conversation); cache warming; server-side storage of responses
  (`store` stays false).

# Pinned context: the agent explores, the user pins

Status: built 2026-10-10. Registered in the Decision Log of `docs/design.md`.
Supersedes, for turns with the agent tools on, the auto-selection of
[smart_context_selection.md](smart_context_selection.md) §4 and the sticky
admissions of [cache_first_context.md](cache_first_context.md). The ledger
itself (append-only, insertion-ordered, append-updates) is kept.

## 1. Why

Every turn still attached chapters by a keyword scorer, and every chapter
once attached stayed in the ledger for good: nothing removed it after manual
selection was retired (agentic_chat_loop.md D7). The bubble listed them as
`[Attached Context: …]`. Measured over the agent turns since 2026-10-05
(assistant messages with an agent record):

| | share |
|---|---|
| Turns that carried attached chapters | about three in five |
| Attached chapters (under half added by the scorer that turn) the turn read or wrote | about one in seven |
| Reads of chapters not attached / already attached | about 4.5 : 1 |
| Writes to chapters not attached / attached | about 3.5 : 1 |

And when the attached set changed, the request diverged at message #1 and
lost its cache (api-server.log since the prefix log began):

| Request | cache hit | median first token |
|---|---|---|
| ledger changed (message #1 differs) | 8% | 37 s |
| extends the previous request | 85% | 5 s |

The model works from the chapter index and its read tools; what the scorer
pushed in was mostly unused, and its churn cost the cache. What the model
does need on every turn are the book's standing references — character
cards, the plot outline, the setting — and only the writer knows which
chapters those are.

## 2. The design

- **A pin per chapter.** `documents.pinned` (server, `INTEGER 0/1`) and
  `CanvasDocument.pinned` (client). Set from the chapters sidebar (a pin
  button on each row, shown filled on pinned rows); `PUT
  /api/books/{id}/documents/{doc}` takes `{"pinned": bool}` as metadata —
  it does not bump the revision — and publishes a `document`/`pinned` event
  so other tabs resync. A user who is not signed in keeps pins locally.
- **What a turn carries (agent tools on).** The pinned chapters, in book
  order, the active chapter excluded (it is in the tail already), up to
  `PINNED_CONTEXT_CHARS` (60,000) of chapter text — each chapter counted as
  the text the ledger renders (the accepted reading as plain text, at most
  20,000) — and the rest of the pins skipped until some are unpinned.
  (Counted as HTML at first: four pinned reference chapters of 25k
  characters of text used 48k of the budget, a formatted character card
  being twice its text in HTML.) `pinnedContextIds` /
  `pinned_context_ids` (`utils/contextSelection`, parity-tested) decide it.
  That list is the ledger's `desiredIds`: an unpinned chapter leaves the
  ledger, a newly pinned one is appended, an edited one is appended as an
  update — all as before. No scorer, no "previously attached", no "the
  model read it last turn".
- **Nothing else changes in the request.** The CHAPTER INDEX (with
  summaries and freshness markers) and the active chapter stay in the
  volatile tail; the model reads anything else with `read_chapter` / `grep`
  / `analyze_book`. A chapter read last turn is read again when needed: its
  tool result is not replayed across turns (D4), and grep and paragraph
  ranges keep that to one cheap step.
- **An open pinned chapter keeps its place** (§2.1, added the same day).
  Opening a pinned chapter used to drop it from the ledger (the tail holds
  the open chapter) and closing it re-added it: two cache misses for a look
  at the outline. Now `planLedgerTurn`'s `keepIds` keeps its entry — position
  and bytes — while it is open, edited or not. When it was edited while
  open, the tail's header says so: "Its copy under REFERENCED CHAPTERS is
  older: this is its current text", so the model edits against the tail.
  Once the writer leaves it, an edited chapter is updated in the ledger as
  any edit is (one miss); an unedited one costs nothing. A chapter pinned
  while open enters the ledger when it is left.
- **No label.** The bubble no longer carries `[Attached Context: …]` lines
  on agent turns; the pins in the sidebar are what rides along. History
  stripping of the old labels stays, for old messages.
- **The first turn after this ships** drops whatever the scorer had put in
  the ledger (one re-prefill) unless the writer pins it.
- **Tools off** (the legacy single-document configuration): unchanged. That
  model cannot read anything itself, so auto-selection stays its only way to
  see other chapters.
- **Both transports**: `useChatLLM.assembleChatRequest` (tab runs) and
  `server_context.assemble_request` (server runs) branch the same way.

## 3. Also fixed with it

Server runs never added to the footer's Session Tokens (or "Last turn"
cache line): the counter was only fed by the tab's own steps. The run's
`step` events carry its cumulative usage; the tab now adds each step's
difference and shows that step's cache share.

## 4. Not built

Pins for attachments (they are read by reference, §1 of
attachments_and_web.md); pinning a paragraph range instead of a chapter; a
per-turn "include this chapter" control in the composer.

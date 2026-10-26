# Feature Specification — Smart Context Selection

> **Manual control retired (2026-10-06,
> [agentic_chat_loop.md D7](agentic_chat_loop.md)).** Pinning, blocking, the
> tag bar and whole-book mode (Layer 3, §6) are gone. The Layer 1 scorer
> remains as the prefetch, without its pinned/blocked inputs, and the
> batched reading of §6 lives on as the `analyze_book` tool. The sections
> below describe the design as it was.

> Layer 2 (agentic chapter lookup) was **removed on 2026-08-17** after shipping
> unused — see §5. Layers 0, 1 and 3 are live.

Status: **Implemented** (branch `feat/smart-context-selection`) — see §11 for
v1 deviations from this design.

This spec upgrades the multi-chapter reference system described in
[multi_document_references.md](multi_document_references.md) from a purely
manual selector into a layered system:

| Layer | What | Cost profile |
|---|---|---|
| **0 — Chapter index** | Title + short summary of every chapter, always in context | ~100–200 tokens/chapter per turn |
| **1 — Auto-selection** | Deterministic scorer pre-attaches relevant chapters; user pins/blocks | Scoring is free (no LLM); attached full text same price as today's manual attach |
| ~~**2 — Agentic lookup**~~ | ~~Model requests missing chapters mid-turn~~ | **Removed 2026-08-17 — never fired once in production (§5)** |
| **3 — Whole-book mode** | Escalation ladder for global tasks (outline, consistency) | Free digest → single big call → confirmed map-reduce |

The design mirrors the **skill pattern** from agentic frameworks: skill
*metadata* (name + description) is always in context while the skill *body*
loads on demand. Chapter *summaries* are the metadata; chapter *full text* is
the body.

**Control precedence** (user always wins): `pinned` > `blocked` > auto
selection > index-only. Manual selection is never overridden or removed by
automation.

---

## 1. Current State (before this feature)

- `selectedReferenceIds` (per document) is toggled manually via reference
  tags under the chat input and **cleared after every send**.
- `detectReferencedDocIds()` (`src/utils/llmContext.ts`) auto-attaches
  chapters whose *title* appears verbatim in the prompt text.
- Attached reference docs are plain-texted, truncated at
  `MAX_REFERENCE_DOC_CHARS = 20_000`, and merged into the **final user
  message** (after chat history) to preserve provider prompt-cache prefixes.
- **Gap**: `multi_document_references.md` §3.1 describes an always-on
  "CHAPTER OUTLINE" block, but no such block is built anywhere — the model
  has no awareness of unattached chapters. Layer 0 closes this gap.
- `services/llm.ts` supports 5 streaming providers with **no native
  tool-calling plumbing**. This drives the Layer 2 protocol choice (§5.3).

---

## 2. Foundation: Chapter Summaries — **generation removed 2026-08-17**

> Summaries were built to feed Layer 0's index and Layer 2's lookup. Layer 2
> was deleted as dead code (§5), and a hand-written outline chapter serves
> the purpose the index was meant to serve — better, because it is written
> deliberately.
>
> **Generation is gone**: the background summarizer, its queue, the
> `summaryProvider` setting, the Settings control, the sidebar and overview
> buttons, and the staleness machinery (`isSummaryStale`,
> `hashDocumentContent`, `detectSummaryLanguage`, `buildSummaryInput`).
>
> **The data is not.** `summary` and `summaryContentHash` remain on
> `CanvasDocument`, are still persisted and server-synced, still render in the
> sidebar and the overview drawer, and still feed both the chapter index and
> the scorer's keyword-overlap signal. Existing summaries keep working; new
> chapters simply have none, and the index falls back to a content digest.
>
> Deleting it also removed the last background producer of requests against the
> single-slot local endpoint — see context_engine.md §3.
>
> The original design follows, for whoever revisits this.

### 2.1 Schema

Extend `CanvasDocument` (`src/types/document.ts`):

```typescript
export interface CanvasDocument {
  // ...existing fields...
  /** LLM-generated short summary (~120 words) + key entities. */
  summary?: string
  /** Hash of `content` when `summary` was generated. Mismatch = stale. */
  summaryContentHash?: string
}
```

Persisted documents live in a versioned envelope in IndexedDB — this bumps
the envelope version with a migration merging the new optional fields.
**A migration test is mandatory** (versioned-persistence rule).

Summaries ride along in the existing document payload, so they sync to the
server and across devices automatically once they are fields on
`CanvasDocument`.

### 2.2 Generation: lazy, never real-time

**Staleness is tolerated by design.** A summary is navigation metadata, not a
source of truth. A
slightly outdated summary is harmless, so regeneration is lazy:

- A chapter becomes *stale* when `hash(content) !== summaryContentHash`.
  Stale chapters are refreshed only on:
  - **edit-idle**: no edits to that chapter for ~60 s (NOT the 1 s save
    debounce — typing must never fan out into LLM calls);
  - **chapter switch**: summarize the chapter being left;
  - **send-time, non-blocking**: sending a chat message enqueues refreshes
    but uses existing summaries immediately; fresh ones serve the next turn.
  - Manual refresh in the sidebar; "Summarize all chapters" for imports.
- Only the actively edited chapter ever goes stale → steady-state cost is a
  few small calls per writing session, not book-sized.
- **Execution**: one background `streamLLM` call with a fixed prompt
  (*"Summarize this chapter in ~120 words, then list key
  characters/entities/facts as bullets. Plain text only."*), FIFO queue,
  concurrency 1, silent failure (stale summary kept). Never runs while a
  generation is streaming into that document.
- **Model**: a configurable **utility model** (Settings), defaulting to the
  cheapest model of the active provider (Flash/Haiku-class), independent of
  the chat model.
- **Fallback** when no summary exists: first ~300 chars of
  `htmlToPlainText(content)`. Chapters under ~1,000 chars never need a real
  summary — the truncated text *is* the summary.

---

## 3. Layer 0 — Always-On Chapter Index

Every chat request in a multi-chapter book includes a compact index, placed
in the **dynamic context block** (final user message, next to reference docs
— NOT the system prompt, whose byte-stability the prompt cache depends on;
the index churns whenever a summary regenerates).

```
CHAPTER INDEX (all chapters in this book; full text NOT included unless it
appears in REFERENCED DOCUMENT CONTEXTS or is the active document):
#1 "Chapter 1: Origins" — Riva discovers the buried archive; introduces …
#2 "Chapter 2: The Crossing" [ACTIVE — this is the document you can edit]
#3 "Chapter 3: Ashfall" — The siege begins; Kael betrays …
```

For very large books (>40 chapters), clamp per-chapter lines to one sentence.
Single-document books emit no index and no lookup instructions.

---

## 4. Layer 1 — Auto-Selection with Manual Adjustment

### 4.1 Scoring: a pure function, zero LLM cost

Selection must be instant and free — it runs (debounced) on keystrokes. New
module `src/utils/contextSelection.ts`:

```typescript
export function selectReferenceChapters(input: ChapterScoreInput, budget: SelectionBudget): SelectionResult
```

| Signal | Score |
|---|---|
| Pinned by user | ∞ (always include) |
| Blocked by user | −∞ (never include) |
| Title mentioned in prompt (reuses `detectReferencedDocIds` logic) | 100 |
| Title mentioned in the last 4 chat turns | 60 |
| Adjacent chapter (prev/next of active) | 40 |
| Keyword overlap between prompt and chapter summary/entities | 0–50 scaled |
| Attached in the previous turn (continuity) | 30 |

Include chapters scoring ≥ 40, greedy by score, under a total reference
budget (default 60k chars; per-doc cap stays 20k). Budget overflow drops
lowest-score chapters first (they degrade to their index line, not to
nothing). A wrong selection degrades to the chapter's index line, never a
hallucination.

### 4.2 Interaction workflow

The reference tag bar becomes a **live preview of what will be sent**,
recomputed (~300 ms debounce) as the user types:

- **Tag states** (all clickable, tooltip explains why):
  - `auto` — hollow/dashed amber + ✨: selected by the scorer for this
    prompt; ephemeral, recomputed per keystroke/send.
  - `pinned` — solid amber (existing `.reference-tag.active` style): sticky
    across turns until unpinned. **Behavior change**: today's selection
    clears after send; pins persist.
  - `neutral` — default gray: not attached; model sees only its index line.
  - `blocked` — struck-through/dimmed: never auto-attached for this active
    document until unblocked.
- **Click cycle**: `neutral/auto → pinned → blocked → neutral`.
- **Budget chip** next to the bar: estimated context size (`~32k / 60k`).
  Over-budget auto tags dim with a tooltip ("over budget — summary only").
- **Post-send transparency**: assistant replies keep the
  `[Attached Context: …]` labels, now marked `(auto)` vs pinned.

Store: `pinnedReferenceIds` + `blockedReferenceIds` replace the
send-and-clear `selectedReferenceIds`, persisted per document as today
(with a migration mapping old data: `selectedReferenceIds → pinned`).
Auto-selected IDs are computed at send time and never persisted.

---

## 5. Layer 2 — Agentic On-Demand Lookup — **REMOVED 2026-08-17**

Shipped, then deleted unused. The machinery reached the model on every
multi-chapter turn (the toggle defaulted ON, the `<lookup>` rules were in the
system prompt, and `lookup_chapters` was offered as a tool on the tool
protocol) — and the model never once used it.

Evidence that decided it: a search of stored transcripts found no `<lookup`
request from the model and no `📖` status bubble (what a lookup round
renders), while `Attached Context` labels (Layer 1 doing its job) were common.

So Layer 1 was carrying the whole load and Layer 2 was dead weight: a bounded
retry loop, a second parse path in `onDone`, a settings toggle, a tool
definition, and a block of system prompt, none of it ever exercised outside
tests.

Removed in full: `parseLookupRequest`, `resolveLookupTitles`,
`LookupLoopContext`, the `lookup_chapters` tool and its `allowLookup` gate, the
`CHAPTER_LOOKUP_RULES` prompt block, the `agenticLookupEnabled` setting and its
Settings checkbox, and the continuation loop in `useChatLLM`.

**If this is revisited**, the original design is in git history
(`git show <commit-before>:docs/features/smart_context_selection.md`) and the
questions worth answering first are:

1. Did the models simply not know the protocol, or did they never *need* it
   because Layer 1's picks were adequate? Those call for opposite fixes.
2. On the tool protocol the model had a real `lookup_chapters` function and
   still did not call it — which points at (b) rather than a syntax problem.
3. A cheaper probe than rebuilding the loop: log when the model's answer cites
   a chapter that was only in the index, never attached. If that never
   happens, Layer 2 has no job.

Note that the [cache-first context strategy](cache_first_context.md) changes
the economics either way: chapters now enter an append-only ledger, so a
mid-turn addition costs one appended block rather than a rebuilt prompt. A
future Layer 2 would be markedly cheaper than the one removed here.

## 6. Layer 3 — Whole-Book Tasks (Escalation Ladder)

Some tasks are inherently global — "write an outline of the whole book",
"check character consistency", "where does Kael appear?". Selection and
targeted lookup are the wrong tools: the task genuinely needs *all*
chapters. The answer is an escalation ladder spending the minimum the task
needs.

### 6.1 Entering whole-book mode

1. **Explicit**: an "All chapters" super-tag at the front of the tag bar;
   clicking pins the whole book (individual tags collapse into one chip).
2. **Heuristic**: the scorer recognizes global-intent phrases ("whole book",
   "all chapters", "全书", "所有章节", "outline", "大纲", "consistency"…)
   and pre-activates the super-tag as `auto` — visible before sending,
   rejectable like any auto tag.
3. **Model-driven**: `<lookup chapters="*">` routes here.

### 6.2 The ladder

**Rung 0 — Structural digest (free).** Per-chapter heading trees (`h1`–`h3`)
extracted deterministically from stored HTML at zero LLM cost. Whole-book
mode always includes `heading tree + summary` per chapter — for
outline-shaped tasks this alone is frequently sufficient and fits in a few
thousand tokens.

**Rung 1 — Everything fits: attach it all.** Compare total book size
(plain-texted) against a **model-aware context budget** (per-provider
defaults: Gemini-class 1M models hold most books; Claude-class 200k holds
mid-size ones; small local models may hold none). If it fits, attach every
chapter, single normal request. This is the most expensive *single call* in
the system (a 100k-word book ≈ 100–150k input tokens), so it has its own
guards:

- **One-shot by default.** The super-tag auto-deactivates after send —
  staying pinned would re-bill the whole book every turn. Keeping it is an
  explicit second click ("keep for this conversation"), which also switches
  the cache layout (next point).
- **Cache placement when sticky.** The standard layout puts volatile context
  in the final user message — correct for small changing attachments,
  pathological for a large *unchanging* book behind a growing history (never
  a cache hit). When sticky, the book text moves to the **stable prefix
  region** (right after the system prompt, before history) with an Anthropic
  `cacheHint` / OpenAI-Gemini implicit prefix caching, so turns 2+ pay
  cache-read prices. Trade-off: the book sits far from the request, but
  whole-book tasks are analytical rather than `<edit>`-targeted, so
  SEARCH-adjacency matters less; the *active* document still rides in the
  final message.
- **Cost visibility.** When estimated input exceeds ~50k tokens, show the
  same inline confirmation as Rung 2, with the estimate and a
  "Fast mode: summaries only" alternative.

**Rung 2 — Doesn't fit: client-orchestrated map-reduce.** A batched pass
reusing the Layer 2 round machinery (transient rounds, status bubbles,
abort, token accounting):

```
batches = chapters in book order, greedy-packed to the context budget
notes   = ""                        // running scratchpad
for each batch (round i of N):
    prompt = [task] + [RUNNING NOTES: notes] + [batch full text]
           + "Update and extend the notes. Do NOT answer yet."
    notes  = model output           // transient, not persisted
final round:
    prompt = [task] + [complete notes] + [active document]
    → normal response handling (chat / <canvas> / <edit> routing)
```

- **Cost consent required**: inline confirmation before starting —
  "Whole-book processing needs ~N calls (~X tokens). [Fast mode: summaries
  only] [Proceed] [Cancel]". Multi-call spend is never silent.
- **Progress**: one status bubble per round ("📚 Reading chapters 6–11
  (batch 2/4)…"), running notes inspectable on expand.
- **History hygiene**: only the task and final answer persist.
- **Guards**: max 12 rounds, abort cancels the pass, a failed round retries
  once then surfaces the standard error path.

### 6.3 Rung selection

Automatic: outline/structure prompts always get Rung 0 material; Rung 1
whenever the book fits (with the >50k confirmation); Rung 2 only with
explicit consent. The model can self-escalate: a Rung 0/1 attempt answering
`<lookup chapters="*">` triggers the Rung 2 confirmation.

---

## 7. Cost Model (summary of design discussion)

Ordered cheapest to most expensive:

1. **Layer 1 scoring** — free (pure local function; runs on debounced
   keystrokes, microseconds).
2. **Summary maintenance** — a few utility-model calls per writing session
   (only the edited chapter goes stale; lazy triggers; ~2k in / 200 out per
   call). "Summarize all" on import ≈ tens of k tokens once.
3. **Layer 0 index** — ~100–200 tokens/chapter, every turn, in the uncached
   dynamic tail. The first thing to clamp for very large books.
4. **Attached chapter full text** — ~5k tokens/chapter/turn, uncached; same
   price as today's manual attach. Over-attachment risk is bounded by the
   score threshold, budget cap, pre-send visibility, and `blocked`.
5. **Whole-book Rung 1** — one book-sized call; one-shot default + sticky
   cache placement + >50k confirmation.
6. **Whole-book Rung 2** — N book-sized calls; always behind explicit
   consent with a fast-mode alternative.

---

## 8. Implementation Phases

1. **Phase 1 — Summaries + Index**: schema migration (+test), lazy summary
   queue, Layer 0 index in `buildDynamicContext`.
2. **Phase 2 — Auto-selection UI**: `utils/contextSelection.ts` scorer
   (+tests), pinned/blocked store fields (+migration), four-state tag bar,
   budget chip.
3. **Phase 3 — Agentic lookup**: `<lookup>` parser (+tests), client loop
   with guards (+tests), settings toggle, status bubble.
4. **Phase 4 — Whole-book mode**: heading-tree digest (+tests), super-tag
   (one-shot default), model-aware budgets, Rung 1 guards, Rung 2 map-reduce
   loop with confirmation (+tests for batch packing and rung selection).

Each phase is independently shippable and useful.

## 9. Testing Plan

- `contextSelection.test.ts`: scoring signals, budget eviction order,
  pinned/blocked precedence, threshold behavior.
- `text.test.ts`: `<lookup>` extraction — clean tag, surrounding prose,
  malformed attributes, multiple titles, `*`, fuzzy title resolution.
- Persistence migrations: documents envelope (summary fields),
  pinned/blocked defaulting from legacy `selectedReferenceIds`.
- Loop guards: pure decision function (round count, dedupe, nothing-new
  fallback) with the LLM boundary mocked.
- Whole-book: heading-tree extraction from HTML fixtures, batch packing
  (order preserved, budget respected, oversized chapter truncated with
  notice), rung selection (fits / doesn't fit / fast mode).

## 10. Open Questions

- Embeddings for Layer 1 scoring? Deferred — keyword overlap against
  LLM-generated entity lists is free and likely sufficient; Layer 2 covers
  misses either way.

## 11. Implementation Notes

The v1 deviations originally listed here have been closed:

- **Summaries are server-synced.** The `documents` table gained
  `summary`/`summary_content_hash` columns (idempotent `ALTER TABLE`
  migration in `init_db`, tested in `scripts/test_api_server.py`); document
  PUT/POST accept the fields, book/document GET return them.
  `carryOverLocalSummaries` remains as a fallback merge for summaries
  generated while logged out — a server value wins over a local one.
- **Summaries use a configurable utility model.** `ProviderConfig.summaryModel`
  (Settings → per-provider "Summary Model" input); empty = the chat model.
- **Cost consent is an inline 3-option panel** (Proceed / Fast mode / Cancel)
  rendered in ChatPanel. Consent is awaited BEFORE the message enters the
  chat, so Cancel has zero side effects.
- **Sticky whole-book mode with stable-prefix cache placement.** The
  super-tag cycles off → once → sticky. Sticky keeps the book attached every
  turn as a `[user book-content (cacheHint) + assistant ack]` pair right
  after the system prompt — byte-stable across turns, so turns 2+ pay
  cache-read prices — while the volatile tail (index + active doc) stays in
  the final user message. Sticky consents once per activation.
- **Sidebar summary controls**: per-chapter regenerate (✨, amber when
  stale; force-bypasses the staleness check) and a header "summarize all
  stale chapters" action.
- **Lazy-loaded (server) chapters attach correctly.** Server books load
  chapter metadata only (`contentLoaded: false`); the scorer's `attachable()`
  drops empty docs, which originally made pinning an unopened chapter a
  silent no-op — and whole-book / Layer 2 lookup silently skipped unopened
  chapters too. The store's `ensureDocumentContents(ids)` (backed by
  `store/contentLoader.ts`, tested: dedup of concurrent fetches, tolerant of
  failures) now fetches missing content eagerly on pin, before Layer 1
  selection at send time, before whole-book planning, and per lookup round —
  fulfilling the "Layer 2 can fetch them later" degrade path.

Remaining known limitations:

- **Sticky + over-budget books degrade to 'once' semantics**: re-running the
  Rung 2 batched pass every turn would multiply cost, so an over-budget book
  triggers the batched consent per send even when the tag is sticky. A
  notes-caching scheme (reuse batch notes until the book content hash
  changes) is the natural follow-up.
- **Storage migration:** documents live in a versioned IndexedDB envelope
  (v2). v0 bare arrays and v1 envelopes migrate sequentially; legacy per-doc
  `selectedReferenceIds` becomes `pinnedReferenceIds`.

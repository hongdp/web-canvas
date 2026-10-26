# Cache-first context strategy

> **Note (2026-10-06):** pins and the sticky whole-book prefix mentioned
> below were retired ([agentic_chat_loop.md D7](agentic_chat_loop.md)).
> Chapters now enter the ledger only through the prefetch and through what
> the model reads.

Status: **implemented** for the layout and the ledger (§4–§7); the history
windowing changes (breakers 3–5) are **not done** — see §11.
Supersedes the layout half of the 2026-07-07 "Cache-Friendly Prompt Layout"
decision; the windowing/trimming helpers in `utils/llmContext.ts` stay.

## 1. Why now

A local run measured the cost directly. `api-server.log`, one ordinary chat
turn against a local Qwen3.8-Flash-Next endpoint:

```
Started generation job (provider=ollama)
  prompt processing, n_tokens = 4661, t = 42.56 s  (109 tok/s)
first token after 42.87s (7505 chars of input)
  … then 18.4 tok/s
```

Forty-three seconds before the first visible token, all of it prefill. The
same engine re-reads a prefix it has already seen in **0.2 s** when the token
sequence is byte-identical (measured with a 45k-token prompt).
So the prefill is not a hardware limit — it is a **prompt-layout** result.

The same arithmetic governs the paid providers: an unchanged prefix is billed
at cache-read rates (roughly a tenth) instead of full input rates. What is
slow locally is expensive remotely; one fix serves both.

## 2. The goal, stated as an invariant

> Between consecutive turns of a session, the token sequence sent to the model
> must share the longest possible identical prefix, and every change must
> happen as close to the end as possible.

Corollaries the design commits to:

- Context **grows by appending**. Nothing already sent moves or changes.
- What must change (the active document, the request) sits in a **volatile
  tail** that we accept re-prefilling every turn.
- Anything that would **shorten or reorder** the stable part is a user-visible
  event, not a silent optimization.

## 3. What the current layout does

`useChatLLM.ts:1190`:

```
apiMessages = [ systemPrompt, ...bookPrefixMessages, ...historyMessages, finalUserMessage ]
```

with `finalUserMessage.content = dynamicContext + "\n\nUSER REQUEST:\n" + prompt`,
and `dynamicContext` (`chat/dynamicContext.ts`) =
chapter index + **all attached reference chapters** + **the active document**.

The intent was right — volatile content last. But the *bulk* of the prompt is
inside that volatile tail, so the tail is where all the tokens are.

### The six prefix breakers

| # | Breaker | Where | Severity |
|---|---|---|---|
| 1 | Every attached reference chapter lives in the final message, so the whole reference block is re-prefilled each turn | `chat/dynamicContext.ts` | **critical** — this is the bulk |
| 2 | Attached chapters are ordered `[pinned in book order, then autos sorted by score desc]`. Scores are recomputed from the new prompt every turn, so the same set of chapters is emitted in a different order | `contextSelection.ts:191` | **critical** — reorders bytes even when nothing changed |
| 3 | History is trimmed from the **front** once it exceeds `MAX_HISTORY_CHARS` (80k) | `llmContext.ts:44`, `useChatLLM.ts:39` | high — one drop invalidates everything after it |
| 4 | Images are stripped from all but the last `keepImagesInLast` (4) messages, so a message's content silently changes as the conversation advances past it | `llmContext.ts:88` | high |
| 5 | Consecutive same-role messages are merged, so an earlier turn's bytes change when a neighbour is dropped | `llmContext.ts:70` | medium |
| 6 | The active document is re-serialized every turn even when unedited | `chat/dynamicContext.ts` | medium |

Breaker 2 deserves emphasis: a user who changes nothing, attaches nothing and
asks a second question still gets a different byte sequence, because the score
of each chapter moved with the new prompt text. That alone can cost a full
re-prefill of 60k characters.

Prior art in this repo already does the right thing for one case:
`buildStickyBookPrefix` (`chat/wholeBook.ts:186`) injects the book as a
`user`/`assistant` pair **before** the history and marks it `cacheHint: true`.
This design generalizes that shape to the normal path.

## 4. Proposed layout

```
[ system prompt ]                     stable for the session
[ CONTEXT LEDGER ]  ← new             append-only, user/assistant pair, cacheHint
     chapter index (stable order)
     chapter A  (attached turn 1)
     chapter B  (attached turn 1)
     chapter F  (attached turn 4)     ← appended, never inserted
[ history ]                           append-only
[ VOLATILE TAIL ]                     re-prefilled every turn, deliberately
     active document (current bytes)
     USER REQUEST: …
```

The tail is then the active document plus the request — for a book with six
attached chapters that is the difference between re-reading ~70k characters
and re-reading ~8k.

**The active document stays in the tail.** It is the most volatile object in
the app and the `<edit>` protocol needs it adjacent to the request so SEARCH
blocks are copied from current bytes (2026-07-07 decision, unchanged). We pay
one document of prefill per turn and stop paying for everything else.

## 5. The context ledger

A session-scoped, ordered record of what has already been sent:

```ts
interface ContextLedger {
  /** Chapter ids in the order they were first sent. Never reordered. */
  sentIds: string[]
  /** Bytes as sent, per id — so a re-send is detectably identical. */
  sentHash: Record<string, string>
  /** Set when the ledger was rebuilt, to explain a cold turn in the UI. */
  invalidatedAt?: number
  invalidationReason?: 'user-evicted' | 'chapter-edited' | 'budget' | 'session-start'
}
```

Rules:

1. **Append-only.** A newly selected chapter is appended to `sentIds`.
2. **Order is insertion order**, never score order, never book order. Score
   decides *admission*, never *position*.
3. **No silent eviction.** The selector may no longer drop a chapter it
   previously attached just because this turn's scores changed.
4. **An edited chapter invalidates from its position onward.** If chapter B's
   content changed, everything from B on must be re-sent. Prefer to re-append
   B at the end and mark the old copy stale in the same message… which is not
   possible without rewriting the block, so this is a genuine invalidation —
   see §7 open question 2.
5. The ledger resets on: switching books, switching the active chapter, a
   provider/model change, or an explicit user reset.

## 6. When the user's request would evict — proceed silently (revised 2026-08-30)

The original design stopped and asked here: an inline 3-option consent panel
("仍然移除 / 保留该章并继续 / 取消") blocked the send whenever removing a
chapter would invalidate part of the cached prefix. **Removed by user request**
— being interrupted about cache internals when deleting a chapter was worse
than the re-prefill it warned about. A user removal now takes effect
immediately; the plan pays the one-turn re-prefill without comment.

What remains of the mechanism:

- `planLedgerTurn` still *prices* every drop (`resendChars`, `resentIds`) —
  the arithmetic is tested and available should a non-blocking indicator ever
  want to show it.
- `planKeepingRemoved` (the "keep it anyway" branch) and the `requiresConsent`
  flag were deleted with the panel.
- The whole-book consent panel (a different gate, about attaching an entire
  book's cost) is unchanged.

## 7. Selection algorithm changes

`selectReferenceChapters` keeps its scoring but changes what it returns:

- Input gains `ledgerIds: string[]` (what has already been sent).
- Output gains `additions: string[]` (append these) and
  `evictions: string[]` (would need confirmation).
- `attachedIds` becomes `[...ledgerIds, ...additions]` — ledger order first.
- `SCORE_PREVIOUS_TURN` (+30) becomes redundant for ledger members and should
  apply only to *non*-ledger candidates, so it stops distorting admission.
- The budget check applies to additions only.

History windowing (`trimHistoryForContext`) changes:

- Trimming from the front is allowed only when it crosses a full invalidation
  we have already told the user about; otherwise raise the window.
- `keepImagesInLast` becomes a **fixed message index** captured when the
  message enters the window, not a rolling offset (kills breaker 4).
- Same-role merging happens at message-creation time, not at assembly time
  (kills breaker 5).

## 8. Acceptance criteria

Measured, not asserted:

1. Two consecutive turns with no chapter/document change produce token
   sequences whose common prefix covers everything before the volatile tail.
   Unit-testable without a model: assemble twice, compare strings.
2. Attaching one new chapter changes only the tail of the ledger block.
3. Reordering scores (different prompt wording, same chapter set) produces a
   byte-identical ledger block.
4. ~~An eviction cannot happen without the consent panel firing.~~ (panel
   removed 2026-08-30 — evictions proceed silently; the plan's pricing of
   them stays tested.)
5. On the local endpoint, turn 2 of a session reports a first-token time at
   least 5× lower than turn 1 for an unchanged chapter set (`api-server.log`
   already logs exactly this).

## 9. Open questions

1. **Where does the chapter index go?** It changes whenever any chapter's
   title or summary changes, and summaries are refreshed in the background.
   Putting it at the head of the ledger makes it a frequent invalidator.
   Options: (a) freeze the index in the ledger and accept staleness until the
   next reset, (b) move the index into the volatile tail (it is small), (c)
   split — frozen structure in the ledger, fresh summaries in the tail.
   Recommendation: **(b)**, it is a few hundred bytes against tens of
   thousands.
2. **An edited reference chapter.** Rule 4 above has no cheap answer: the
   chapter's bytes are in the middle of the ledger. Either invalidate from
   there (honest, expensive) or append a correction block ("chapter B has been
   revised; the authoritative text follows") and leave the stale copy in
   place, which costs tokens and risks the model reading the old version.
   Recommendation: invalidate. (Originally: surface it in the consent panel;
   the panel is gone, so surfacing would need a non-blocking indicator.)
3. **Does the sticky whole-book path merge into the ledger** or stay separate?
   It is the same mechanism with a different admission rule; merging them is
   tempting but the whole-book path also disables lookup and reshapes the
   budget.
4. **Per-provider cache markers.** `cacheHint` maps to Anthropic
   `cache_control` and is capped at 3 breakpoints (`llm.ts:549`,
   `server_generation.py:556`). With a ledger the natural breakpoints are:
   end of system, end of ledger, end of history. That is exactly 3 — no room
   for a fourth, so the design must not add another.

## 10. Not in scope

- Changing what the scorer *considers* (title/keyword/adjacency signals).
- The agentic `<lookup>` loop — removed entirely on 2026-08-17 as dead code,
  see smart_context_selection.md §5.
- Roleplay mode, which has its own volatile game-state block and should be
  revisited separately once this lands.

## 11. As built (2026-08-17)

Landed:

- `utils/contextLedger.ts` — the planner, pure and tested 18 ways. Append-only,
  insertion-ordered, longest-valid-prefix on any removal.
- `hooks/chat/dynamicContext.ts` — split into `buildLedgerMessages` (the stable
  block, injected before the history as a `cacheHint` user/assistant pair) and
  `buildVolatileTail` (chapter index + active document).
- `utils/contextSelection.ts` — takes `ledgerIds`; ledger members ride free
  (no budget, no re-scoring), and the continuity bonus applies only to
  candidates outside the ledger, where it can still change an outcome.
- `useChatLLM.ts` — assembles `[system][ledger][history][tail]`, plans each
  turn against `ledgerRef`, and resets the ledger when the book, provider or
  model changes (a different model is a different cache).
- ~~Eviction consent panel~~ — removed 2026-08-30 by user request; removals
  proceed silently and the planner still prices them.

Decisions taken during implementation:

1. **Chapter index → volatile tail**, as recommended. It moves with every
   background summary refresh and is a few hundred bytes.
2. **An edited chapter is dropped from its position and re-appended at the
   end**, so the prefix before it survives. Same treatment for the chapter that
   becomes the active document — with the added reason that a stale duplicate
   is how an `<edit>` SEARCH block silently stops matching.
3. **Whole-book stays separate.** It installs its own sticky prefix and now
   explicitly resets the ledger, since the block the ledger believed was cached
   is no longer in the prompt.
4. New admissions are ordered **farthest-first in book order**
   (`orderAdmissionsForSwitchCost`), because switching the active document
   truncates the ledger at that chapter's position and the writer's next
   chapter is usually the adjacent one.

Verified by `useChatLLM.test.ts` ("cache-first prompt layout"), which drives
the real hook over two turns and asserts the second request's messages start
with the first request's, that the shared prefix actually contains the chapter
text, and that a newly pinned chapter appends rather than reorders. Removing
the ledger block or the prefix-keeping logic turns those red (checked).

### Breaker 3, partly addressed: budget against the model, not a constant

`MAX_HISTORY_CHARS = 80_000` is gone. The window is now derived per turn in
`utils/contextWindow.ts`:

```
budget = contextTokens − maxOutputTokens − (system + ledger + active doc) − 10% safety
```

Two things made the flat number wrong in both directions at once. The local
endpoint runs at **262144** tokens, so history was being trimmed at roughly a
tenth of what fits; and a 32k model would have been handed a prompt it must
silently truncate — from the FRONT, which is precisely the cached prefix.

The window comes from the endpoint when it states one: llama.cpp reports
`meta.n_ctx` on `/v1/models`, and `/api/models` now forwards it as
`contextWindows` into `discoveredContextWindows` in the store. A reported value
always beats the table, because only the server knows what it was started with
— this endpoint went from 32K to 262144 within a day. The table in
`contextWindow.ts` is the fallback for providers that report nothing.

Token estimation is CJK-aware: `length / 4` is an English rule, and a Chinese
character is roughly one token, so it underestimates this app's actual content
four-fold. `estimateTokens` counts the scripts separately, and `tokensToChars`
converts back using the conversation's measured CJK ratio.

Trimming from the front still *happens* when a conversation genuinely outgrows
the window — it is just far rarer now, and it is no longer triggered by a
constant that has nothing to do with the model in use.

### Breakers 4 and 5, fixed

`trimHistoryForContext` now **normalizes the whole history first, then cuts
whole messages from the front**, instead of cutting and then reshaping what
survived.

- **Images (4).** History messages never carry images; the turn that attached
  one sends it, and afterwards the message keeps a fixed
  `[image sent earlier in this conversation]` placeholder. The old rolling
  "last 4 keep theirs" rule meant a message silently lost its images once the
  conversation moved past it — new bytes for a message the model had already
  seen. A message that was *only* an image keeps the placeholder rather than
  vanishing, which would delete a turn and merge the assistant turns around it.
- **Same-role merging (5).** Merging runs on the full history before the budget
  cut, so a merged block is atomic: sent whole or not at all. Merging after the
  cut let the window start *inside* a run and emit a fragment the model had
  never seen — the cached prefix ends exactly there. Mutation-checked: restoring
  the old order turns the fragment test red.

## 12. Ordering for the way this book is actually written

The first ordering heuristic — farthest-in-book-order first, on the theory that
the writer edits an adjacent chapter next — was wrong for this app's main
workflows:

| Workflow | What happens |
|---|---|
| A. Pinned outline + write the next chapter from the previous one | The outline is referenced every turn and revised after most sessions |
| B. Fold the new chapter back into the outline | The outline becomes the **active document** |
| C. Go back and finish an earlier chapter using later text (rarer) | A long-finished chapter becomes active |

A permanently pinned outline is usually chapter zero, so book distance sorted
it **first** — the most expensive position, since removing an entry re-sends
everything after it — while it is in fact the most frequently edited document
in the book. Workflow B then paid for the whole ledger every time.

`orderAdmissionsByStability` sorts by `updatedAt` instead, oldest first, with
book distance only as a tie-break. Finished chapters lead; the outline sinks to
the end, where invalidating it costs nothing but itself. Measured in the tests:
workflow B re-sends 500 characters instead of 18,000.

Workflow C remains the expensive case — the most stable chapter sits first, and
jumping back to edit it invalidates the rest. The design **self-heals**: that
chapter leaves the ledger while it is active, and when it returns its timestamp
is fresh, so it sorts last from then on. One expensive turn, not a recurring
one.

## 13. Edits append, they do not cut (2026-10-05)

**The measurement.** On a live turn (a "continue the chapter" request, grok
via the backend job), the writer had revised the outline, which sat fourth in
a six-entry ledger.
The planner treated that as §5's edit case: cut at the outline, re-send
everything after it, and append the outline at the end. Step 1 hit only 9% of
its prompt tokens in the cache, and the first token arrived after 20.4 s. The ledger order confirms the cut: the outline moved from 4th place to
last.

**The rule now** (`planLedgerTurn` with `options.render`):
- **Append, don't cut.** An edited chapter keeps its old copy where it is,
  byte for byte (`LedgerEntry.text` stores the exact block that was sent).
  Its new version is appended with the header "(UPDATED — this version
  replaces the earlier copy of this chapter above; disregard that one)". The
  old copy is marked `stale`; that is metadata only, and its bytes do not
  change. This is the "append, never mutate" rule Manus describes for
  KV-cache hits, and Codex uses the same rule for its context updates (see
  agentic_chat_loop.md, "Prior art").
- **Only when cheaper.** The append is taken when what sits after the old
  copy is larger than the chapter itself. Otherwise the cut is cheaper and
  stays.
- **A budget.** Outdated copies may total max(20k chars, 30% of the ledger).
  Past that, the ledger is cut at the first outdated copy and re-sent clean,
  as one consolidation.
- **Leaving removes every copy.** A chapter that leaves (opened,
  deselected, deleted) takes all its copies with it. A chapter re-sent after a
  cut, with an old copy still above it, carries the UPDATED header.

The model is told in two places: the header on the new copy, and the index
marker "[in context — CHANGED since you last saw it…]" (agentic_chat_loop.md
D8).

**A second cache-buster, found on the way.** Chapters were rendered from
their raw HTML:
- a pending review diff came out as plain text with the deleted and inserted
  words run together;
- accepting that diff changed the bytes, so the chapter counted as edited.

Ledger blocks are now rendered and hashed from the accepted reading
(`ledgerBlock`), the same reading `read_chapter` and the active document
give the model. Accepting the agent's own diff no longer costs a re-send;
rejecting it does, which is correct, because the text changed.

Not done: a chapter that becomes ACTIVE is still dropped from the ledger,
which cuts there. Keeping its copy as a stale duplicate of the tail would
follow the same rule, but the active document is edited by the user on every
keystroke. That trade-off has not been measured.

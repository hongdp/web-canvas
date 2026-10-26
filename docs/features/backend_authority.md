# Backend authority: server-side state and agent (Python)

Status: **phase 1 implemented** (2026-10-06): chapter revisions, conflict
handling and book events, as built in §4.1. Phases 2–4 are proposed.
Registered in the Decision Log of `docs/design.md`.

The decisions already taken (user, 2026-10-06):

- **The server is the single source of truth** for chapters, versions, chat
  messages and agent runs. The frontend is a UI over that state.
- **The agent runs on the server, in Python.** The loop, the tools, context
  assembly and the text logic they rely on are ported from TypeScript. The
  option of a Node service reusing the TypeScript was considered and not
  chosen.
- **Offline: only a temporary cache.** Edits made while disconnected are
  held locally and submitted on reconnect against the revision they were
  based on. A conflict is shown, never silently merged.
- **Paused until this lands:** keeping text written for a chapter that
  does not exist yet, and queueing requests. They are specified here (§6)
  and built server-side. Creating a chapter the moment its write arrives
  was paused too, and landed client-side on 2026-10-06 instead (§6.1).

## 1. Why

Today the browser owns the truth. The Zustand store holds the documents,
persists them to IndexedDB, and PUTs them to the server last-write-wins.
The agent loop (`src/agent`, `useChatLLM`) runs in the page. The server
stores files, authenticates, and relays model calls as resumable jobs
(`server_generation.py`).

Every recent failure traces back to that split:

| Observed (2026-10-06) | Cause |
|---|---|
| A long series run stopped partway | A page reload killed the loop; a rejoined stream can finish one step but not continue the run |
| A run's third step hung as "running" | A service restart killed the job; the bubble's state lives in the page |
| A selection rewrite was dropped | The write depended on the editor showing the right chapter at the right moment |
| Edits beside a selection could write one chapter over another | Editor, store and run each held a copy of "the chapter" |
| Requests cannot be queued across devices or reloads | The queue would live in one page's memory |

Each was patched where it surfaced. The structure behind them stays:
three copies of each chapter (editor, store, run) are kept in step from
inside a page that can disappear at any time.

## 2. Target architecture

```
 Browser (UI only)                         Server (truth + agent)
 ─────────────────                         ──────────────────────
 TipTap editor (local, fast typing) ──patch(baseRev)──▶ Document store (revisions)
 Chat panel, timeline, review UI   ◀──── events (SSE) ── Run engine (asyncio)
 Temporary offline cache                                  ├─ tool registry (Python)
                                                          ├─ context assembly
                                                          └─ LLM transports (existing)
```

### 2.1 Documents with revisions

- Every chapter has a monotonically increasing `revision`.
- **Every write carries the revision it was based on**: the user's typing
  (debounced patches), accept/reject, renames and reorders, and the
  agent's writes. The server applies it only if the base matches, else
  answers `409` with the current revision and content.
- The run engine writes through the same path, so the `RunState.known`
  rule (refuse a write to a chapter the user changed meanwhile) becomes the
  ordinary revision check.
- Version snapshots are taken by the server before the first agent write
  to a chapter in a run, as `commitDoc` does today.
- The client store becomes a cache. It renders server state and applies
  server events.

### 2.2 Events

One server-sent event stream per open book, carrying:

- `doc.updated {id, revision, content | patch, by}`: from any writer,
  any device;
- `run.*`: `started`, `step` (text deltas, tool timeline items),
  `preview` (partial rewrite text for a chapter), `finished`, `queued`;
- `lock {chapterIds}`: chapters the run is writing into (the edit lock of
  agentic_chat_loop.md §0.4, decided on the server).

A reconnecting client resumes from an event id. The resumable-job buffers
in `server_generation.py` already work this way for one generation; this
generalizes them to the book.

### 2.3 Run engine

- **One asyncio task per run, one run at a time per book.** Further
  requests wait in a per-book **queue** (§6.3).
- **Each step is persisted before it is executed:** the request messages
  (append-only, as today), the reply, the tool results. A server restart
  resumes the run at its last completed step, instead of losing it.
- **The LLM transports are the existing ones** in `server_generation.py`,
  called in-process instead of via `/api/generate`.
- **Tool execution reads and writes the document store**, never an editor:
  - the live preview is an event the client paints;
  - a selection rewrite is placed by text (`alignSelectionBlocks`, already
    built this way), from the request's captured selection text.

#### Runaway runs (user decision, 2026-10-08; built in phase 3, §4.3)

On the client, three things end a run that does no useful work: the user
presses Stop, a reload or a closed tab kills it, and `agentMaxSteps`. On the
server the first two are gone by design (a run outlives the tab), and the
user has set the step limit to 0 for grok. A 127-step run that never wrote
(2026-10-07) would burn tokens unattended. The server therefore **pauses,
never kills**, consistent with "analyze the cause, do not forbid":

- **Pause with a notice.** When the last N steps repeated the same calls
  with the same results and wrote nothing, or when a run's accumulated
  cost passes a budget, the run is suspended and a `run.paused` event names
  why, with the last steps' reasoning summaries (the step journal). The
  user resumes it, changes the instruction, or abandons it; the persisted
  steps make a resume exact.
- **Unattended runs are tighter.** While a tab is attached the user's
  settings apply, 0 steps included. Once every tab has gone, a run that
  passes the unattended step or cost ceiling pauses on its own.
- **Budget in money.** Usage is already recorded per step; the run keeps
  the running total and pauses at the configured amount.

### 2.4 Frontend

- **`useChatLLM` shrinks to a renderer**: send a request, cancel it, and
  apply `run.*` events to the bubble and the editor.
- **The editor keeps typing local and smooth.** It sends patches; on
  `409` it rebases or shows the conflict.
- **The `Editor.tsx` content-sync race simplifies.** Every external change
  arrives as a `doc.updated` with a revision, so "did this come from me?"
  becomes a revision comparison instead of the `contentFromEditorRef`
  string match.
- **IndexedDB keeps only the temporary offline queue.**

## 3. What is ported to Python

Measured on the feature branch (non-test lines):

| Area | Source | Lines |
|---|---|---|
| Loop, policy, registry, invocations, tools, freshness, polish | `src/agent/**` | ~2,500 |
| Request assembly, stream splitting, ledger planning, prompts | `src/hooks/useChatLLM.ts`, `src/hooks/chat/*` (the non-React parts) | ~1,200 of 2,660 |
| Text logic | `utils/text.ts` (edit matching, 6 levels), `diff.ts` (custom LCS, no library), `diffResolution.ts`, `paragraphs.ts`, `polish.ts`, `systemPrompt.ts`, `contextLedger.ts`, `chapterIndex.ts`, `contextSelection.ts`, `pendingChanges.ts`, `imagePreservation.ts`, `llmContext.ts` | ~3,300 |

**HTML parsing.** Several of these use the browser's `DOMParser`
(paragraphs, image handling, plain-text extraction). Python uses one parser
for all of them. As built (§4.2) it is the standard library's `html.parser`
driving a small tree builder, `scripts/wc_text/dom.py`, whose serializer
reproduces what a browser's `innerHTML` gives back — `html5lib` was
considered and not needed. TipTap's own normalization (what the editor
stores after a round trip) is NOT reproduced. The server stores what it is
sent, and the editor's normalized output arrives as an ordinary user patch.

**Parity before replacement.** The TypeScript suites are the
specification:

- every pure function gets its test cases ported as fixtures;
- a parity harness runs the same inputs through both implementations
  (Node in CI, as the provider-message parity tests do today) and must
  match byte for byte for `diffHtml`, edit application and prompt
  assembly;
- prompt bytes matter beyond correctness: grok's cache is exact-prefix, so
  a one-character drift in assembly costs every cached prefix.

## 4. Phases

Each phase ships on its own and leaves the app working.

| Phase | Scope | Exit criterion |
|---|---|---|
| 1. Document authority | Revisions on chapters; revision-checked writes (user, accept/reject, agent writes from the current client loop); `doc.updated` events; store as cache; offline queue | Two tabs editing different chapters stay in sync; a stale write gets 409 and a visible conflict, never a silent overwrite |
| 2. Pure logic in Python | Port §3's text logic and prompt/context assembly, with ported fixtures and the parity harness | Parity harness green on every fixture and on a replay of real stored turns |
| 3. Server run engine | Run API and `run.*` events; tools against the document store; persisted steps and resume after restart; per-book queue; client loop kept behind a per-provider switch | A run survives a page reload and an API restart; a request sent mid-run queues |
| 4. Retire the client loop | Remove `src/agent` from the bundle and shrink `useChatLLM` to the renderer; build the three paused features (§6) server-side | No agent code in the frontend; the paused features shipped |

### 4.1 Phase 1 as built

**Server** (`scripts/api_server.py`, `server_events.py`, `server_db.py`,
`server_content.py`; tests in `test_document_authority.py`):

- `documents.revision` (INTEGER, default 1; added to existing databases at
  startup). Every read returns it; create returns `revision: 1`.
- `PUT /api/books/{id}/documents/{doc}` takes an optional `baseRevision`.
  A text or title change is applied with a conditional `UPDATE … WHERE
  revision = ?`; a stale base gets **409** with the chapter as it is now
  (`revision`, `title`, `content`). A write without a base still wins, so
  an older client keeps working. A summary-only save neither bumps the
  revision nor publishes an event: a background summary would otherwise
  turn every open tab's next save into a conflict.
- Content files are replaced atomically (temp file + `os.replace`).
- **Events:** `GET /api/books/{id}/events` is a server-sent event stream
  from an in-process hub (one uvicorn process, so a dict of asyncio queues
  is the broker). Kinds: `document updated|deleted` and `documents
  created|replaced|reordered`. Each carries the writer's `X-Client-Id`, so a
  tab ignores its own echo. A 15s heartbeat comment keeps proxies from
  closing an idle stream; a subscriber that stops reading (256 queued) is
  dropped and reconnects. Measured through the Vite proxy: an event
  arrives about 20 ms after the write's response.

**Client** (`src/store/documentSync.ts`, `bookEvents.ts`; tests in
`src/store/__tests__/documentSync.test.ts`, `initialBookChoice.test.ts`):

- `CanvasDocument.revision` is the server revision the local text is based
  on; `unsynced` marks a text or title change the server has not
  confirmed. Both persist with the chapter in IndexedDB, so a reload cannot
  drop an edit made inside the save debounce. Optional fields: an older
  record reads as "synced, revision unknown", no migration needed.
- `serverCopies` records each chapter as the server last confirmed it, on
  **load as well as on save**. A save sends text only for a chapter that
  differs from it or is unsynced, with `baseRevision`. Re-sending an
  unchanged chapter after a reload would bump its revision and make
  another tab's next save a conflict.
- **A 409** keeps the local text as a version snapshot, adopts the server's
  copy and shows a banner ("Open history"). Two identical copies are
  adopted silently. The agent's writes take the same path: they mark the
  chapter unsynced like typing does.
- **Loading a book** (`initializeStoreFromServer`, `switchBook` on the same
  book) keeps every unsynced local chapter, text and base revision,
  instead of an empty stub (`mergeServerChapters`), and keeps an unsynced
  chapter only this device holds. `switchBook` saves unsynced chapters
  before it reloads.
- **The cache belongs to one book.** Startup may open another book than
  the cache holds (the account moved on, on another device). Then the
  cache's unsynced chapters are saved to **their** book first
  (`saveOtherBookEdits`; a 409 there becomes a version in that book) and
  are not merged into the one that opens.
- **Events** (`connectBookEvents`, wired in `App.tsx` per open book):
  - a chapter with an unsynced edit is never overwritten (its save will
    conflict instead);
  - a newer revision of a loaded chapter is fetched and adopted (unless a
    keystroke landed during the fetch);
  - an unloaded chapter only moves its revision;
  - structural events, and a reconnect after a drop, re-read the chapter
    list (`resyncBook`).
  
  During an agent run, an event that changes a chapter the run has read
  surfaces as a user edit, and the run refuses to write over it
  (agentic_chat_loop.md, the `RunState.known` rule).

**Not in phase 1**, by choice:

- event resume by id: a reconnect re-reads the list instead;
- patches: full chapter HTML per debounced save stays;
- `lock` and `run.*` events (phase 3);
- server-taken version snapshots;
- shrinking IndexedDB to an offline queue (it remains the full cache);
- the focus-time check still reloads the whole book through `switchBook`
  when another device wrote. It also brings chat and settings, which have
  no events yet.

### 4.2 Phase 2 as built

**The port** is the Python package `scripts/wc_text/`, one module per
TypeScript file, function for function, with the same names in snake case:

| Module | Source | What it holds |
|---|---|---|
| `text.py` | `utils/text.ts` | tag parsing (`<canvas>`, `<edit>`, `<selection_replace>`), the six-level edit matcher, local edit application with diff markup, doc-status parsing, word counts |
| `diff.py`, `diff_resolution.py` | `utils/diff.ts`, `utils/diffResolution.ts` | the block-then-token LCS diff with its cell cap, markup stripping, accept/reject on HTML |
| `paragraphs.py`, `pending_changes.py` | `utils/paragraphs.ts`, `utils/pendingChanges.ts` | numbered paragraphs, the was/now list of pending changes |
| `image_preservation.py` | `utils/imagePreservation.ts` | placeholders in and out, and the re-insertion of images a rewrite dropped |
| `llm_context.py`, `chapter_index.py`, `context_selection.py`, `context_ledger.py` | the matching `utils/*.ts` | plain text, the history budget, the chapter index, the prefetch scorer, the append-only ledger planner (FNV-1a hash included) |
| `dynamic_context.py` | `hooks/chat/dynamicContext.ts` | the ledger block and the volatile tail |
| `system_prompt.py`, `document_tools.py` | `utils/systemPrompt.ts`, `utils/documentTools.ts` | prompt assembly, the three document tools and the provider adapters |
| `polish.py` | `utils/polish.ts` | chunking, prompt, parsing, validation and reassembly of the polish pass |
| `dom.py` | — | the fragment parser and browser-style serializer the above share |

Not ported, by kind: browser-only helpers (blob URL and GIF conversion,
`getTimestampId`), `collectDiffRanges` (it walks a ProseMirror document, so
it belongs to the editor), and the React halves of the hooks.

**The harness.** `src/parity/__tests__/parity.test.ts` holds every case:
it runs each through the TypeScript and writes `{input, output}` to
`scripts/parity/fixtures/<module>.json` when `WRITE_FIXTURES=1`
(`npm run parity:fixtures`), and otherwise asserts that the committed
fixtures still equal what the TypeScript produces — so a change to a
ported function cannot pass `npm test` without regenerating them.
`scripts/test_parity.py` runs the same inputs through the Python and
compares byte for byte; it also fails when a fixture names a function the
Python has no port for. The prompt's fixed texts travel the same way:
`promptTexts()` writes them to `scripts/wc_text/data/prompt_texts.json`,
and `system_prompt.py` assembles from that file with the ported logic. One
source for the bytes, because grok's cache is exact-prefix. Where a
function takes a callback the JSON cannot carry (the ledger renderer, the
image preserver), both sides use the same fixed stand-in. Random diff ids
are renumbered in order of appearance on both sides before comparing.

Fourteen fixture files, 533 cases, all green on both sides.

**What the harness caught**, each now a comment at the place it matters:
JavaScript's `\w` and `\d` are ASCII-only while Python's are not;
`String.prototype.trim` strips U+FEFF and Python's `strip()` does not
(`_js_trim`); `DOMParser` decodes entities, drops `/>` and closes an open
`<p>` on the way back out, so slicing the source never matched
`outerHTML`; a document's leading whitespace never reaches the body;
`Math.round` rounds halves up, Python's `round` to even; `Date.parse`
reads a zone-less date as UTC, `fromisoformat` as local time; the ledger
hash walks UTF-16 code units; a string replacement interprets `$&` and
`$$`; and `undefined` vanishes from JSON while `None` does not.

**Known difference.** Lengths and slices count code points in Python and
UTF-16 units in JavaScript, so a truncation boundary can move by one next
to an astral character (an emoji). The hash is exact; the rest is
tolerated until a fixture shows it mattering.

**Exit criterion.** The fixtures are green. The second half — a replay of
real stored turns — waits for phase 3: the step journal records what each
step did, not the request it sent, and the server will only assemble a
real request once it owns the run.

### 4.3 Phase 3 as built

**The switch.** `ProviderConfig.serverRuns` (Settings, per provider; on unless the writer
turns it off — off by default until 2026-10-17, when a closed page stopped a
tab-run turn after its first step). With it on and the user logged in, a chat turn is posted to
`POST /api/books/{id}/runs` and the loop runs in the API process. Off, the
turn runs in the tab exactly as before. Roleplay (removed 2026-10-12) and
imports were never runs.

**The Polish button is a run too** (2026-10-17, phase 4): the request
carries `polish: {documentId, language}`, and `_execute_polish` runs the
chapter through the polish model with no agent loop — queued like a turn,
locked, snapshotted and written by the server, its record the one the tab
built (one tool line, the chapter in "changed this turn"). Stop sets the
run's `stop_event` and aborts its side jobs (`_ModelCall`), so a chunk in
flight keeps its draft — the agent's `polish_chapter` tool stops the same way.

**The history budget is calibrated** as the tab's is (`tokenCalibration.ts`):
a finished run stores measured ÷ estimated prompt tokens (`AgentRun.token_ratio`,
clamped to [1, 3]) in its provider|model `run_context` state, and the next
turn's budget divides the window by it (`server_context._token_ratio`).

**Server** (`scripts/server_runs.py`, `server_context.py`,
`server_documents.py`, the `wc_agent/` package):

- `wc_agent/` is the port of `src/agent`: the run controller as an async
  loop (`run.py`), the registry, and every tool — the three writes,
  `rename_chapter`, `read_chapter`, `grep`, `list_chapters`,
  `open_chapter`, `delete_chapter`, `polish_chapter`, `analyze_book` —
  over the ports of `wc_agent/types.py`. Two things differ from the client
  by construction: there is no editor, so a selection rewrite is always
  placed by its text in the stored chapter, and edits beside a selection
  read the stored chapter. `test_agent.py` runs the tools and the loop
  against an in-memory book.
- `server_context.py` assembles the request (the port of
  `assembleChatRequest`): the prefetch scorer, the system prompt, the
  history budget, the append-only ledger, the freshness markers and the
  volatile tail, all from `wc_text`. The ledger and the seen record live
  in the `run_context` table per book and provider|model — the context a
  browser tab used to hold in refs — so a reload or another device keeps
  the cached prefix.
- `server_documents.py` holds the revision-checked write, creation,
  deletion and snapshot the run's ports use; each publishes its book
  event, so every open tab (the sending one included) applies the run's
  writes as it applies another device's.
- Each step is a `GenerationJob` (`server_generation`), so the transports,
  the decrypt and effort retries and the step journal are the ones the
  client path uses. The job's events are forwarded live as `run.*` events;
  the markup preview is computed server-side (`split_streaming_response`,
  `preview_rewrite`) and sent as `run.preview {documentId, html}`.
- `run.*` events on the book's event stream: `queued`, `started`,
  `step_started`, `delta`, `reasoning`, `preview`, `preview_selection`,
  `progress`, `lock`, `open`, `corrective`, `step` (the record so far),
  `paused`, `finished` (the final content, record, reasoning items and the
  version snapshots taken). The hub's queue is sized for per-token deltas.
- **Persisted after every step** (`runs` table: request, the loop's
  snapshot, the record): `AgentRun.snapshot()` carries the append-only
  messages, what the model has seen (by content hash, as a reload does),
  the chapters created and touched, and whether a step follows. The
  lifespan hook re-launches every run that was running when the process
  died, from its last completed step — the step in flight is made again,
  which the exact-prefix cache makes cheap (`test_runs.py`).
- **Messages**: the server inserts the turn's two messages at submission
  (the record carries `run: {id, status}`) and writes the final content,
  record, reasoning items and usage at the end, so another device loads a
  finished turn without ever having seen its events.
- **One run per book; the rest queue** (§6.3). A request posted while a
  run is active is `queued` and starts when the current one finishes. Its
  history is the client's snapshot corrected on start: a bubble that was
  still a placeholder is read from the messages table, and turns that
  landed meanwhile are appended. **Stop holds the queue**: the step in
  flight is aborted, the run ends as `stopped`, and queued requests stay
  listed with "send now" and "remove" (`/start`, `DELETE`); a new request
  while nothing runs still starts at once.
- **Pause, never kill** ("Runaway runs"). Between steps the engine
  suspends a run, with a `paused` event naming why and the last three
  steps' calls, answers and reasoning heads: `repeating` — the last three
  steps made the same calls with the same arguments and wrote nothing
  (the calls, not the answers: `list_chapters` answers "identical to your
  previous result" from the second time, and the 13× loop of 2026-10-06
  is the same call either way); `unattended` — no tab was subscribed to the
  book for more than 12 steps; `token_budget` — prompt plus completion
  tokens passed `runTokenBudget` (Settings; absent = none). `/resume`
  continues from the next step; the guard then judges only steps after
  the resume. A budget in money waits for a price table (none exists in
  the app yet); tokens stand in for it.
- The sending tab reports the chapter it shows (`/view`) and follows the
  run's `open` events; other tabs only watch. The server locks the
  chapters the run writes into (`lock`: the selection turn's chapter, the
  previewed one, one being polished), and the client merges them into its
  edit lock.

**Client** (`src/services/serverRuns.ts`, `store/runEvents.ts`,
`hooks/chat/serverRunEvents.ts`, `useChatLLM.ts`, `RunControls.tsx`):

- `bookEvents` hands `run` events to an emitter; the chat hook renders
  them: the bubble through pure reducers (`applyRunEvent`: the server's
  record replaces the bubble's at every `step`, `paused` and `finished`;
  the step in flight is painted from the deltas with the same splitter as
  a local turn), the editor through the ports a local run uses (previews,
  the lock, opening a chapter).
- **A message during a run steers it** (agentic_chat_loop.md §0.8):
  `POST /api/books/{id}/runs/{run_id}/steer {text}` hands the text to the
  running run (`AgentRun.steer`), which appends it as a user message after
  the step in flight; 409 when the run is not running, and the client then
  queues the request as before. The pending text is in the run's snapshot,
  so a restart keeps it. The client sends it from `handleSendMessage` when a
  run is attached and streaming; the user message goes into the chat with no
  bubble of its own.
- **The analyze and polish ports** (`_AnalyzePort`, `_PolishPort`) keep the
  run as `self.owner`: as `self.run` it hid their `run()` method and every
  server-side `analyze_book`/`polish_chapter` failed (fixed 2026-10-09,
  agentic_chat_loop.md §0.11). A tool that raises is logged with its
  traceback (`wc_agent.run`).
- **Step failures** (agentic_chat_loop.md §0.10): a generation job retries
  a call that produced nothing yet (retryable statuses, connection errors;
  `Retry-After` honored) and records why it failed (`error_kind`:
  `transient`, `idle`, `repetition`, `context`, `fatal`) and whether output
  had started. The engine's step driver retries a step that broke mid-reply
  or looped once, and raises `StepUnavailable` when retries are spent or the
  stream went idle; the loop puts the step back and pauses the run
  (`step_failed` or `repeating_output`). Run steps on grok send the xAI
  repetition-check headers; a resume after `repeating_output` sends the
  next step without them.
- **The conversation summary** (agentic_chat_loop.md §0.9) lives per book in
  `run_context` under scope `chat-summary` (`{upToId, text}`); the run's
  request assembly plans the cut, calls the model through `_ModelCall`
  (`<book>:summary`, low reasoning) only when the stored summary no longer
  covers the dropped prefix, and stores the result.
- **A server run's preview never reaches the store.** A document preview
  is written with `emitUpdate:false`; a selection preview writes real
  transactions, so a server run flags them (`SILENT_PREVIEW_META`,
  `selectionReplace.ts`) and `Editor.tsx`'s `onUpdate` skips the store for
  them. Published, the preview marked the chapter unsynced, the run's
  document event was ignored (an unsynced chapter is never overwritten by
  an event) and the tab's next save sent the preview with a stale base
  revision: every selection rewrite ended in a conflict banner, with the
  run's text in version history (2026-10-08). The store keeps the chapter
  the server confirmed; the run's commit arrives through its document
  event, and the content sync replaces the preview. A tab-local run keeps
  publishing its selection preview: it commits what it previewed.
- **The send flushes the save debounce.** The server builds the request
  from the stored book, so an edit still inside the 3s debounce would be
  missing from the prompt, and the run's write on that stale chapter
  would race the tab's save for the revision. `startServerTurn` saves
  every chapter that `needsTextSync` before posting.
- On load and on every book switch the hook lists the book's runs: a
  queued, running or paused run gets its bubbles (created if another
  device sent it) and its live text; a run that finished while the tab was
  away settles its bubble from the result. The job-list reconcile skips
  bubbles that belong to a run.
- Stop keeps the draft on screen as one undo step, as before, and tells
  the server; the run's `finished` event then settles the bubble, and the
  note says the draft was kept.
- The chat input stays open while a server run streams (a request queues),
  and a bubble whose run is queued or paused shows its controls.
- Versions the run took are merged into the store from `finished` as
  metadata (their text loads on demand, as every server version does).

**Added 2026-10-08** (agentic_chat_loop.md §0.8): the engine's repeat guard
pauses at six identical steps, after the loop's own nudge at three; a
`paused` event with `reason: "question"` carries an `ask_user` question and
its options, and `POST …/runs/{id}/answer` continues the run with the
answer as the next message; before each step's results the engine checks
the chapters the model has seen for a newer revision, the chapter list for
a change, and the queue for waiting requests, and tells the model in
reminders; `longReasoningReminderTokens` in the request config enables the
long-reasoning reminder.

**Added 2026-10-10 — catching up on missed run events.** A phone
suspends the book's stream while the page is in the background, often
without an error, and a run that finished meanwhile left its bubble
"working" with the timer still counting minutes after the end. The tab now re-lists the book's runs when the stream reconnects
after a drop and whenever the page comes back to the foreground
(`emitRunCatchUp`, `store/runEvents`); a stream that gave up (`CLOSED`) is
reopened then. The run the tab follows is settled through the same handler
as the live `finished` / `paused` event, so the bubble, the streaming flag,
the reasoning timer and the edit locks end together.

**Added 2026-10-10 — API keys at rest.** A run's request is persisted for
restart recovery, and it carried the provider key in plain text — for
every run ever made, finished or not, and in every copy of
`metadata.db`. Now (`server_secrets.py`) a live run (queued, running,
paused) stores its key sealed (Fernet) with a key file beside the database
(`run_secret.key`, mode 0600), and a finished run (done, stopped, error)
stores none; the stored request is rewritten on every persist. Rows written
before this are scrubbed once at startup and the database is vacuumed, so
the old text does not survive in free pages. If the key file is lost, a
live run resumes without a key and its next call fails and pauses it.
Not covered: the settings sync (`user_settings.provider_configs`, one row
per user since 2026-10-10) keeps provider keys on purpose, so a new device gets them, and older
`metadata.db.bak-*` copies still hold what they held.

**Not in phase 3**, by choice: event resume by id (a reconnect lists the
runs again), a money budget, approval for destructive tools, retiring
`src/agent` (phase 4). The `PUT /documents/{id}` route keeps its own copy
of the revision check beside `server_documents.write_document`.

### 4.4 Phase 4 as built (2026-10-17)

The tab's loop is gone. `src/agent/` (the TypeScript loop, its tools and
13 test files) was deleted, and `useChatLLM` went from 2,650 lines to about
600: it posts every turn, and the Polish button, to `POST /api/books/{id}/runs`
and renders the run's `run.*` events. The editor side of those events (the
live chapter preview, the silent selection preview, the edit locks, Stop's
single undo step) lives in the hook itself, with no tool context behind it.

- **No switch.** `ProviderConfig.serverRuns` and its Settings checkbox are
  removed; a stored `serverRuns: false` is ignored. Signed out, the chat says
  to sign in (generation needed a session already).
- **Done before the cut** (part A, #123): the Polish button became a run
  (`polish: {documentId, language}`, `_execute_polish`), and the history
  budget's per-model token ratio (`tokenCalibration.ts`, now deleted) moved
  to the server's `run_context` state.
- **Old bubbles.** A bubble a closed tab-run left at "Thinking..." is retired
  to the interrupted notice once `/api/generate/active` confirms no job fills
  it (`retireOrphanedBubbles`); a tab-run's Delete/Keep question keeps its
  local delete (`confirmChapterDeletion`).
- **Parity.** Six fixtures had their TypeScript in `src/agent` (`freshness`,
  `policy`, `chapters`, `invocations`, `reminders`, `run_compaction`). They are
  now Python-owned golden files: `src/parity` checks they exist and no longer
  writes them; `python3 scripts/test_parity.py --rewrite-python-owned`
  recomputes their outputs after an intended Python change. The other
  fixtures are still written from `src/utils` and `src/hooks/chat`.
- **The rest of the dead TypeScript** (2026-10-18): 20 more modules nothing
  in the frontend ran — request assembly (`llmContext`, `dynamicContext`,
  `contextSelection`, `contextWindow`, `chapterIndex`, `providerProfile`),
  prompts (`systemPrompt`, `documentTools`), summaries and transcripts,
  `toolCallStream`, `imagePreservation`, `polish`, `retryPolicy`,
  `pendingChanges`, `plan`, `editHints`, `textReplace`, `webText`,
  `attachments` — were deleted with their unit tests (178), as were the
  tab's conversation-summary copy (`chatSummaryStore`), the attachment/web
  read ports and the job-rejoin functions of `remoteGeneration`. Their 20
  fixtures joined `PYTHON_OWNED`; `--rewrite-python-owned` now rewrites only
  outputs that changed, with JS-style integers, so a no-op run leaves every
  file byte-identical. `prompt_texts.json` is edited directly.
- **§6 is done:** the tab's "refuse while a turn streams" path went with the
  loop (6.3), and part C built 6.2 server-side.

## 5. Risks

- **Size.** About 7,000 lines and their tests are rewritten. Mitigation:
  phases 1–2 change no behavior, and the parity harness turns the port
  into a mechanical, checkable task.
- **Diff and edit-matching drift.** These are the most subtle code paths
  (six matching levels, span diffs, pending-markup rules). Mitigation:
  port them first and test them hardest.
- **Preview latency.** Preview text now travels model → server → browser,
  instead of model → server job → browser with the client parsing it. The
  hop count is the same; the parse moves to the server.
- **Two implementations during the transition.** The switch in phase 3
  keeps one path authoritative per provider at a time; the parity harness
  keeps them from diverging.

## 6. Paused features, as they will work server-side

### 6.1 Creating a chapter the moment its call arrives

**Landed client-side, 2026-10-06** (agentic_chat_loop.md §0, "creating IS
writing"): there is no `create_chapter` any more. A chapter is created by
the write that fills it — `<canvas new_chapter="title">` creates it as soon
as the opening tag has streamed, so the live preview runs in it; a
`new_chapter` write through `update_document` creates it when the call
completes. A chapter whose write never landed is removed at run end; on
Stop it is kept with its draft. The server run engine (phase 3) ports this
behaviour as is: the write's first bytes create the chapter and the
`preview` events follow.

### 6.2 Keeping text written for a chapter that does not exist

**Built server-side, 2026-10-17** (phase 4, part C), redefined for a book
with no `create_chapter`: a whole rewrite (`<canvas chapter="X">`,
`update_document`) naming a chapter that does not exist — "There is no
chapter #7", "No chapter is titled …" — is kept on the run
(`RunState.held_text`, `_hold_missing`) instead of being lost to the
refusal. The model is told its text is KEPT and not to send it again.

- **The confirmation** is an `update_document` with an empty `html`:
  `new_chapter="title"` writes the kept text into a new chapter, a chapter's
  number into that chapter — under every rule a rewrite has (the seen-text
  rule, the user-edit rule, a reviewable diff). A refused confirmation keeps
  the text.
- A cut-off rewrite (unclosed, arguments lost) is not kept: it is not the
  text the model meant.
- Text still kept when the run ends is discarded, with a note in the bubble.
- Partial edits (`<edit>`, `edit_paragraphs`) to a missing chapter have
  nothing to keep and are refused as before.
- Kept text lives in the run's memory: a server restart between the refusal
  and the confirmation loses it (the model is then refused an empty html and
  writes the text again).

### 6.3 Queueing requests

**Done** (phase 3 server-side; phase 4 removed the tab's refusal): a message
sent while a run works steers it, or — with images, or when the run will not
take it — is posted as a turn and queues.

A request sent while a run is active joins the book's queue and is shown
as queued (cancelable).

- **Bound to the chapter that was open when it was sent**, not the one
  open when it runs (user decision).
- **A selection made with it** is carried as its text, and placed by text
  when it runs.
- **Stop pauses the queue** (user decision): the current run stops, and
  queued requests stay listed, each with "send now" and "remove".
- **Server-side**, so the queue survives reloads and is the same on every
  device.

## 7. Open questions

- **Event transport:** SSE is enough for server→client. Do patches go over
  plain HTTP, or does a WebSocket become worthwhile for typing?
- **Patch format:** full chapter HTML per debounce (simple; chapters are
  tens of KB), or ProseMirror steps (smaller; ties the server to the
  editor's schema)? Start with full HTML plus revision.
- **Roleplay mode and the import pipeline** are client-driven too. They
  are out of scope here; they keep working against the new document API.

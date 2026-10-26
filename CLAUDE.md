# CLAUDE.md

Guidance for AI assistants (Claude Code and others) working in this repository.

## What This Project Is

**Web Canvas** (npm package name `web_canvas`) is an
LLM-powered document editing workspace inspired by OpenAI/Google Canvas. A
rich-text document editor ("the canvas") sits side-by-side with a chat
assistant, letting writers draft, structure, and refine multi-chapter
books/documentation collaboratively with an LLM. It also has an AI
image-generation pipeline. (The roleplay "game master" mode was removed on
2026-10-12.)

The authoritative product/engineering principles live in **`SKILL.md`** and the
architecture/decision record in **`docs/design.md`**. This file summarizes them
for day-to-day work — when they conflict, `SKILL.md` and `docs/design.md` win,
and you should update them alongside your changes (see Documentation Rules
below).

## Tech Stack

- **Frontend**: React 19 + TypeScript, Vite 8, Zustand 5 (state).
- **Editor engine**: TipTap 3 / ProseMirror (`@tiptap/react`, `starter-kit`).
- **Icons**: `lucide-react`. Styling is plain CSS (`src/index.css`) with CSS
  variables for theming (dark/light). There is no Tailwind dependency despite
  what the README's stack note implies — do not add Tailwind classes.
- **Backend**: a Python **FastAPI** server (`scripts/api_server.py`) for auth,
  per-user storage, and books/documents/versions CRUD, plus server-side URL
  scraping. It binds to `127.0.0.1:3000`; Vite proxies `/api/*` to it.
- **Tests**: Vitest (`jsdom`), see `vitest.config.ts`.
- **Lint/Types**: ESLint flat config (`eslint.config.js`), `tsc -b` for type
  checking.

## Repository Layout

```
src/
  App.tsx                 # Root component: layout modes, resizing, editor loop (~520 lines)
  main.tsx                # Entry point
  index.css               # CSS entry: ordered @imports of src/styles/* (order is load-bearing)
  styles/                 # Section stylesheets (tokens, base, editor, chat, responsive, ...)
  types/                  # Shared TypeScript types (barrel: types/index.ts)
    document.ts  llm.ts  chat.ts  import.ts  auth.ts
  store/
    useAppStore.ts        # Slice assembly + auto-save subscription + public re-exports
    types.ts              # AppState = intersection of slice interfaces
    slices/               # documentsSlice, booksSlice, settingsSlice, authSlice,
                          #   versionsSlice, chatSlice, uiSlice, attachmentsSlice
    defaults.ts           # Mock documents, default provider configs/prompts
    settingsPersistence.ts# Versioned cookie/localStorage settings + migrations
    serverSync.ts         # initializeStoreFromServer / performSync
    syncRuntime.ts        # Module-level isInitialized/saveTimeout (single owner)
    persistence.ts        # localStorage / IndexedDB wrappers + documents envelope
    documentSync.ts       # Chapter revisions: unsynced flag, server copies, 409 conflicts
    bookEvents.ts         # SSE subscription: other tabs'/devices' writes, live; run.* → runEvents
    runEvents.ts          # Emitter the chat hook subscribes to for a server run's events
    __tests__/            # persistence + storage API tests
  components/             # React UI components
    Editor.tsx            # TipTap editor host (fragile editor↔store sync — see SKILL.md)
    editorExtensions.ts   # TipTap extensions (DiffAddition/Deletion, CustomImage, ...)
    AppHeader.tsx  CanvasHeader.tsx  CanvasFooter.tsx  VersionHistorySidebar.tsx
    ChatPanel.tsx  ChaptersSidebar.tsx  SettingsModal.tsx  AuthForm.tsx
    ImageGenerationModal.tsx  imageGen/   # Image-gen modal + step components
    AttachmentsSection.tsx  # The book's reference files in the sidebar (A1, A2…; add / remove)
  hooks/
    useChatLLM.ts         # The chat as a renderer of server runs: posts each turn (and the
                          #   Polish button) to /api/books/{id}/runs, renders run.* events
    chat/                 # Extracted chat-flow modules: streamHandlers,
                          #   serverRunEvents (pure reducers), selectionReplace, types
    useDiffHandlers.ts  useModelFetcher.ts  useImageUpload.ts
  services/
    llm.ts                # streamLLM: every generation is a backend job (no in-browser transport)
    serverRuns.ts         # The run API client (start / list / stop / resume / start / remove / view)
    attachments.ts        # Attachment list / upload / remove (reading them is the server run's)
    imageGen.ts  imageGenModels.ts
  utils/
    convert.ts  diff.ts  text.ts  export.ts        # pure helpers (well tested)
    paragraphs.ts  contextLedger.ts  reasoningEffort.ts  protocolChoice.ts  titleSync.ts  # used + parity
    providers.ts          # The provider registry: one row per provider (defaults, labels, discovery)
    runDefaults.ts        # The Settings form's run defaults (the server's wc_text/policy.py applies them)
  i18n/                   # en.ts / zh.ts translation bundles + index.ts hook
  parity/__tests__/       # TS↔Python parity cases; checks fixtures are current, or writes them
scripts/
  start-server.js         # Orchestrator: spawns Python API + Vite (npm run dev)
  api_server.py           # FastAPI app entry: books/documents/versions routes
  server_config.py  server_db.py  server_content.py   # Backend helper modules
  server_auth.py  server_scrape.py  server_migration.py
  server_events.py      # Per-book event hub behind /api/books/{id}/events (SSE)
  server_runs.py        # Server-side agent runs: engine, ports, /api/books/{id}/runs* (phase 3)
  server_context.py     # Request assembly for a run; ledger + seen record per book (run_context)
  server_documents.py   # Revision-checked write / create / delete / snapshot + their events
  server_secrets.py     # Seals a live run's provider key at rest (run_secret.key beside the DB)
  server_attachments.py # A book's reference files: decode, store, /api/books/{id}/attachments*
  server_web.py         # Anonymous headless Chromium: /api/web/search, /api/web/read (no LAN)
  server_generation.py  # Generation jobs: registry, SSE readers, retries, the journal, /api/generate*
  wc_providers/         # The providers' wire formats, one module per API family: common (errors,
                        #   the HTTP seam tests patch, message helpers), openai_compat (Chat
                        #   Completions with a Dialect row per provider), responses, anthropic, gemini
  server_static.py      # --serve-dist: the built frontend from the API process (installable release)
  release.sh            # npm run release (see Releases below)
  wc_agent/             # The agent loop (the only one since phase 4): async run, registry, every tool
  test_api_server.py      # pytest — patch state on the OWNING module (see docstring)
  test_agent.py  test_runs.py   # pytest — the tools/loop on a fake book; the engine on a scripted provider
  wc_text/                # Python port of the pure text logic (backend_authority.md §4.2):
                          #   one module per src/utils file, dom.py for browser-style HTML
  test_parity.py          # pytest — every scripts/parity/fixtures/*.json case, byte-exact
  parity/fixtures/        # Written from the TypeScript by src/parity (npm run parity:fixtures)
docs/
  design.md               # Architecture + Decision Log (register new design docs here)
  features/               # Per-feature design specs
public/                   # PWA manifest, service worker (sw.js), icons
.githooks/pre-push        # Runs `npm test`, then the backend pytest suite, before every push
```

## Common Commands

```bash
npm install                 # install deps; `prepare` wires .githooks via core.hooksPath
npm run dev                 # start full stack (Python API + Vite) via scripts/start-server.js
npm run dev -- --storage-dir <path> --host   # custom storage dir, expose to LAN
npm run dev:debug           # Vite debug mode
npm run build               # tsc -b && vite build (always run before considering work done)
npm start                   # serve dist/ + the API from one process (api_server.py --serve-dist)
npm run release             # release/web_canvas-<version>.tar.gz from HEAD: clean export, no .env, key scan
npm run lint                # eslint .
npm test                    # vitest run (the pre-push gate, with pytest after it)
npm run test:watch          # vitest watch
npm run test:coverage       # coverage over utils, store/persistence
npm run parity:fixtures     # regenerate scripts/parity/fixtures from the TypeScript
(cd scripts && python3 -m pytest -q)   # backend tests incl. the Python parity suite
./start.sh [--daemon|--stop|--status|--logs]  # convenience wrapper around npm run dev
```

**Logs**: `app.log` holds the orchestrator + Vite output; the Python API
server's own output (including `web_canvas.*` log lines — job starts,
time-to-first-token, per-job summaries with prompt-cache hits (and `wrote N`,
the cache-write tokens OpenAI GPT-5.6+ and Anthropic bill above input), a
`prefix:` line saying where a request departs from the conversation's
previous one, and for Anthropic a `cache diagnostics:` line — the API's own
verdict on where the two requests diverged, threaded by `previous_message_id`
per conversation, which also fills the Console's Caching page) goes to
**`api-server.log`**. Grepping
`app.log` for API behavior finds nothing. Every finished step's reasoning,
visible text and tool calls go to **`.local_db/step-journal/<date>.jsonl`**
(kept 7 days): read it to learn WHY a run did something, instead of guessing.

**Vite does not watch `.claude/`** (`server.watch.ignored`): agent worktrees
live there, and their builds used to reload the open app mid-turn. Keep it so.

**Editing `scripts/*.py` requires restarting the API server** — Vite hot-reloads
the frontend, the Python process does not reload itself. A backend running code
from before your change looks exactly like a code defect, and cost three
debugging detours in one session. Check it first, before reading any code:

```bash
ps -o lstart= -p $(ss -ltnp | grep ':3000' | grep -o 'pid=[0-9]*' | cut -d= -f2)
```

If that start time predates your edit, restart before concluding anything.

`npm run dev` does **not** just start Vite — `scripts/start-server.js` also
spawns the Python API server (preferring `~/miniconda3/bin/python3`, falling
back to `python3`) with auto-restart, and loads `.env`/`.env.local` into the
Node process. If the Python API can't bind `:3000` (stale process), the whole
stack dies — see SKILL.md §5.6 for the kill-before-restart procedure.

## Environment & Configuration

- Copy `.env.example` → `.env` and set provider keys (`VITE_GEMINI_API_KEY`,
  `VITE_OPENAI_API_KEY`, `VITE_ANTHROPIC_API_KEY`, `VITE_GROK_API_KEY`, etc.).
- `VITE_STORAGE_DIR` sets where the API server persists books/users/sessions
  (overridable via `--storage-dir`). Defaults to `./storage`.
- `.env`, `.env.*` (except `.env.example`), `storage/`, `.local_db/`, and
  `public/sensitive_words.json` are git-ignored. Never commit secrets, storage
  data, or the SQLite DB.

## Architecture Notes (read before editing)

### State: Zustand single source of truth
The store is assembled in `src/store/useAppStore.ts` from slice creators
under `src/store/slices/` (documents, books, settings, auth, versions, chat,
ui) and holds documents, chat messages, LLM/provider configs, versions, auth,
multi-book state, and sync status. The public API is re-exported from
`useAppStore.ts` — import from there, not from slice files. Key rules:

- **Stale closures**: async stream callbacks must read fresh state via
  `useAppStore.getState()` rather than closing over snapshots.
- **Optimistic UI**: server-mutating operations (book create/update/delete)
  update local state *synchronously first*, then fire-and-forget to the server.
  On failure set `serverSaveStatus: 'failed'` — do **not** roll back local
  state. See `createNewBook`.

### Persistence: hybrid IndexedDB + localStorage
Heavy data (`documents`, `versions` with base64 images) goes to **IndexedDB**
(via `store/persistence.ts`) to dodge localStorage's ~5MB `QuotaExceededError`.
Lightweight keys (`theme`, layout, credentials) stay in synchronous
`localStorage` to avoid render flashes. Document saves are debounced (1s) and
flushed on `beforeunload`. Legacy localStorage keys are auto-migrated into
IndexedDB on init.

### Versioned persistence envelopes
Any persisted client structure that may evolve MUST use a versioned envelope
`{ version: number, data: T }` with sequential migrations (`v0→v1→v2`) and
default-field merging. Migrations run at store init and rewrite validated state
back to storage. **Every migration needs a test** that feeds a prior-version
payload and asserts the upgraded shape.

### Editor sync: the TipTap rollback race
TipTap reads content once on mount. Programmatic updates (LLM streams) use a
`useEffect` calling `editor.commands.setContent(content, { emitUpdate: false })`.
A `contentFromEditorRef` tracks editor-originated HTML; `setContent` is skipped
when incoming content matches, otherwise user edits "roll back" after a paste.
This is a performance-critical, fragile pattern — see `Editor.tsx` and
SKILL.md §2.2/§3.2 before touching the editor↔store loop.
TipTap's `setEditable()` emits a synthetic `update` by default; `Editor.tsx`
passes `emitUpdate=false`, or the live preview leaks into the store when
`isStreaming` flips (the Stop button). Stop keeps the half-streamed draft as
ONE undo step (`keepCanvasPreview` in `useChatLLM.ts`) and writes the same
HTML to the store — screen, persistence, and Undo must always agree.
During a chat turn only the chapters the run is writing into are read-only
(`isEditLocked`: the previewed one, a selection turn's chapter, one being
polished); the user may edit the rest, and a write to a chapter they changed
meanwhile is refused and redone (`RunState.known`, agentic_chat_loop.md §0.4).
A streamed selection rewrite touches the editor only through
`replaceSelectionWithHtml` (`hooks/chat/selectionReplace.ts`). The range it
rewrites is captured at the run's first step from the editor's live selection
ONLY when that selection's text is the store's `selectedText` (what the chat
showed); otherwise the text is located in the document, and a text that is not
there ends as "the selection is gone" — never an insert at the cursor. Never derive an
inserted range's end from `slice.size`: ProseMirror wraps an open slice
inserted at document level (Ctrl+A selects from position 0), so the real
insert is longer — read the end from `tr.mapping.map(to, 1)`. Its
transactions are flagged silent (`SILENT_PREVIEW_META`) and skipped by
`onUpdate`: the server commits the rewrite, and a preview that reached the
store would leave the chapter unsynced, the run's own write event ignored
and the tab's next save a 409.

### LLM integration
`services/llm.ts` exposes `streamLLM(messages, config, callbacks)`; every call
runs as a backend job (`/api/generate`, `scripts/server_generation.py`);
`scripts/wc_providers/` holds the ONE implementation of each provider's wire
format, one module per API family — grok and
OpenAI at api.openai.com on a **Responses API** (`uses_responses_api`; from
gpt-5.4 on, Chat Completions refuses tools with reasoning), Ollama/llama.cpp
and other OpenAI-compatible servers on Chat Completions, Gemini, Anthropic,
and Moonshot's Kimi (`moonshot`, Chat Completions at api.moonshot.ai or .cn):
kimi-k3 takes `reasoning_effort` low/high/max (this app sends low unless
chosen; xhigh is sent as max), kimi-k2.6 gets `thinking: {enabled, keep:
"all"}`, and every Kimi's `reasoning_content` is kept as the step's thinking
block and sent back on the assistant message (`moonshotThinking` /
`moonshot_thinking`, `_reasoning_content`): Kimi's docs call it required in
a tool-calling loop (measured 2026-10-11: accepted without it too — replayed
for the model's continuity, as the other providers' reasoning is). Kimi's
Responses API is not used: kimi-k3 only, `encrypted_content` always null,
`tool_choice` only `auto` (the design.md row has the measurements).
OpenAI's function tools are sent `strict: false` (strict, the default, makes
the model fill every optional argument with an empty value). There is no
in-browser transport: it was removed (2026-10-10) because login is required
and it was a second copy of every provider to keep in step. All responses
**stream**; never block the UI. The **Canvas Markup Protocol** wraps document
updates in `<canvas>...</canvas>` blocks so the frontend can route document
content to the editor and conversational text to chat. The static system
prompt (`wc_text/system_prompt.py`, texts in `wc_text/data/prompt_texts.json`) carries the protocol ONLY — channels, markup,
status line. With the agent tools on, a markup model gets ONE consolidated
text (`agentMarkupPrompt` in `prompt_texts.json`: what you edit / what you can do / how a turn goes /
format / examples); the legacy single-document rules stay byte-identical for
the tools-off configuration. grok also gets `update_document` natively beside
its tag (`nativeOnMarkup`): grok-4.7 delivers reference documents through a
tool call and would not write them as tags. Persona, task, and style guidance belong to the user's preset and
their message; do not add writing instructions to the system prompt. Every reply must also end with a
`<doc_status>updated|unchanged</doc_status>` declaration — **mandatory, on
every reply**, including ones that change nothing. The model decides whether an
edit was warranted; the client rejects a turn only on evidence from the reply
itself: no declaration at all (`undeclared`), a declared update with no markup
(`claimed`), markup the parser rejected (`malformed`), or a first-person claim
of having written next to a `unchanged` declaration. Never re-add local guessing
of user intent.
A provider safety block (403) triggers a retry with a user-supplied local
word list, then an interactive Prompt Editor UI (`status === 'prompt_edit'`).

**A turn is a run of steps** (`scripts/wc_agent/run.py`; the design as built is
§0 of `docs/features/agentic_chat_loop.md`). Each step is one model call; native
tool calls and markup blocks are normalized into the same tool invocations and
executed from one registry — add a tool as a `Tool` in `wc_agent/tools/` and
register it in `server_runs.py`'s `CHAT_TOOLS`. Tools reach the book only through
the ports in `wc_agent/types.py` (`ServerPorts` in `server_runs.py`). A run never rebuilds an earlier message: each
step appends, so follow-up steps hit grok's exact-prefix cache, and tool-call
arguments are replayed byte-for-byte (`argumentsText`). The same goes for
Anthropic thinking blocks (replayed where they streamed — one that came
after the text stays after it, `afterText`, or the API calls it modified),
Gemini thought signatures, and grok's output items:
grok runs on the xAI **Responses API** (not Chat Completions), and each step's
reasoning item, with its `encrypted_content`, goes back unchanged in the next
step (`LLMMessage.responseItems`), so the model keeps its plan — and the final
step's reasoning is kept on the chat message (`ChatMessage.reasoningItems`,
`messages.reasoning_items`) and replayed for the last 8 turns, so it keeps what
it worked out across turns too (grok and OpenAI: `usesResponsesApi`). The tools offered are
fixed at a run's first step. Writes reach any chapter through `chapter` (a
`chapter="…"` attribute on grok's markup). An edit on a chapter whose HTML the
model has not seen this run is refused, and a live preview must never paint
another chapter's text into the open editor (`preview_rewrite`). Per-provider
settings: `agentTools`, `agentMaxSteps` (0 = unlimited), `continueAfterWrites`
(default on: a write's result goes back to the model like any tool result,
and only a reply with no action ends the turn).

**Every turn runs on the server** (`docs/features/backend_authority.md` §4.3;
phase 4, 2026-10-17, removed the tab's loop and the `serverRuns` switch).
`useChatLLM` posts the request to `POST /api/books/{id}/runs` and the loop —
`scripts/wc_agent/` — runs in the API process against the document store,
with each step a generation job. The tab only renders the run's `run.*` events
from the book stream (`hooks/chat/serverRunEvents.ts` for the bubble, the
hook's preview for the editor and the locks); the Polish button is a run too
(`polish: {documentId}`, `_execute_polish`, no agent loop) — offered, as
`polish_chapter` is, only with a polish model other than the chat model
(`polish_offered`; with the same model the button is hidden); a reload or another device lists
the book's runs and rejoins — and so does a tab whose stream reconnected or
that came back to the foreground (`emitRunCatchUp`), or a run that finished
while a phone slept keeps its bubble "working". The run's state is persisted
after every step and a restart resumes it; its stored request holds the
provider key only sealed and only while the run can continue
(`server_secrets`); one run per book, later requests queue; Stop holds the
queue; a run that repeats itself, runs unattended or passes its token budget is
**paused, never killed**, and `/resume` continues it. The loop has one
implementation: edit `wc_agent` (and `server_runs.py`); there is nothing to
mirror in the frontend.

**Between steps the loop talks back** (agentic_chat_loop.md §0.8, after
Grok Build's harness): `<system-reminder>` blocks appended to a step's last
result message — never a message of their own. A failed SEARCH quotes the
nearest paragraph (`wc_text/edit_hints`); three identical steps without a write
get one nudge (server runs pause at six); a step that reasoned past
`longReasoningReminderTokens` without writing makes the step after next
say "act, don't think" (one step late on purpose, measured on grok-4.7);
the `plan` tool's checklist comes back after each step and an early
no-action reply is nudged twice (a write item may be marked done only once
something was written since it started); a plan the previous turn left
unfinished starts the next turn (`previousTurnPlan`, `carriedPlanReminder`); a no-action ending after an HTML
read that no edit followed is nudged once; `delete_chapter` on a chapter
with text asks with Delete / Keep buttons and only the button deletes (the
run on `/answer` with `confirm`);
`rename_chapter` carries a leading `<h1>` that read the old title along;
`ask_user` pauses the run with choices until `/answer`; a message sent while a turn
runs **steers** it (`AgentRun.steer`, `POST …/runs/{id}/steer`): it lands as
a user message after the step in flight, and a reply that would have ended
the turn continues instead — the timeline marks where it landed and the chat
shows the turn split there (`components/chatRows`, display only); the turn after a Stop carries a reminder that the
previous one was stopped; a no-action reply claiming a write that never
happened gets the editor's facts once (`claimsOwnWrite`); `wrapReminder`
escapes its own tag in quoted text. History past the budget is **summarized**, not
cut (`wc_text/conversation_summary`, agentic_chat_loop.md §0.9): the dropped
prefix becomes a fixed-section note placed after the ledger, refreshed only
when the cut moves past it; each transport keeps its own copy.
**Across turns the request only grows** (docs/features/cache_continuity.md,
agent turns): a finished turn's **transcript** (`AgentRun.transcript()`:
its final user message as sent, every step, the step that ended it) is
stored — `turn_transcripts` on the server, tab memory otherwise — and the
next turn replays it in place of the collapsed pair
(`wc_text/turn_transcripts`). The tail sends the chapter index, active
chapter and attachments list in full only when the copy the model has is
stale or not replayed (`diffTailParts`); the ledger is **frozen** between
summaries and pin changes go into the tail (`pinnedUpdates`); a summary is
asked for at the end of the live conversation with the turn's tools.
Never put anything per-turn ahead of the history, and never rebuild a
replayed message. Within a run,
old read results are elided in place before a step that would not fit the
window (`agent/runCompaction`, `promptTokenLimit`; the latest step's results
are never touched), sized by the last step's measured prompt tokens.
**A failing step** (agentic_chat_loop.md §0.10): a call that produced
nothing is retried (`wc_text/retry_policy`; 429/5xx/connection, `Retry-After`,
never a context-length error) by the backend job;
a server-run step that broke mid-reply or looped (xAI's repetition-check
headers, which END the generation on a trigger) is redone once; when that
is not enough the run **pauses** (`step_failed`, `repeating_output`) with
its snapshot instead of ending. grok's stream read timeout is 180 s (idle),
other providers 600 s.
**One way to look** (`docs/features/read_and_list.md`): `read` reads chapters
and attachments; what does not fit in one read comes back cut with the price
of the rest, and with a `task` it is read in batches outside the conversation
(notes back; past 200k estimated tokens only after the user agrees). `list`
shows what can be read: chapters, attachments, an attachment's sections with
their ¶ spans. Old tool names stay registry aliases (`aliases` on a tool).
**Fewer steps** (agentic_chat_loop.md §0.11): `read` takes `parts`
and `grep` takes `patterns`; a whole rewrite of a plain chapter (bare
`<p>`/`<h*>`) needs its current text seen (`RunState.textSeen`), not its HTML;
`edit_paragraphs` edits by ¶ number with a `starts_with` anchor
(`utils/paragraphs` `paragraphSpans`/`applyParagraphEdits`); writes take
`plan_done` (an attribute on tags); eight look-up-only steps in a row get one
reminder.
`replace_text` replaces a word or name across chapters in one call, in the
text only (`wc_text/text_replace`), each chapter a reviewable diff; `expect`
refuses on a different count, `preview` only lists.
`copy_paragraphs` moves or copies a ¶ range as stored (images and list
entries included) — into another chapter after a named ¶, within the same
chapter, or into a new chapter placed where it belongs (`after_chapter`,
`ServerPorts.move`) — so splitting, merging or moving a scene never re-types
text; `move_chapter` reorders chapters; `edit_paragraphs` deletes a whole range
with `through` + `through_starts_with` (`wc_agent/tools/paragraph_moves.py`).

**The CHAPTER INDEX lists chapters by position (`#n`) and title only** (with the
open-chapter and freshness markers). Chapter summaries are not used anywhere —
not in the index, `list`, the sidebar or the keyword scorer (2026-10-11): their
generation was removed on 2026-08-29, the stored ones went stale, and switching
books had copied three onto another book's chapter of the same id. The
`summary` columns stay in the database, unread.

**Context with the agent tools on is pinned, not chosen**
(`docs/features/pinned_context.md`): a turn carries the CHAPTER INDEX, the
active chapter, and only the chapters the writer pinned
(`CanvasDocument.pinned`, `documents.pinned`, ≤ 60,000 chars,
`pinnedContextIds`) in the ledger ahead of the history; the model reads the
rest with its tools, and the bubble carries no `[Attached Context]` label.
The keyword scorer (`select_reference_chapters`) runs only with the tools off.
Do not reintroduce automatic admissions on agent turns: measured, they went
unused and their churn cost the cache.

**Two document protocols, one per model.** The markup above is one of them;
the other is native tool calling (`wc_text/document_tools.py`). `ProviderConfig.
documentProtocol` picks per provider (`utils/protocolChoice.ts`), default
`auto`. Neither wins everywhere, and both halves are measured: a local
Qwen3-14B never emits `<canvas>` but calls `update_document` correctly, while
grok sends a whole tool call in ONE delta (113 chars) against 54 content
deltas — so tool calling has no live preview there. Two rules follow:
`build_chat_system_prompt` requires `protocol` and teaches exactly one (describing
tools to a request that sends none silently disables editing), and the
`<doc_status>` machinery above applies to the markup protocol only — a tool
call IS the declaration, so text-based failure detection is skipped when one is
present.

**Adding a provider** (2026-10-11): the id in `LLMProvider` (`types/llm.ts`),
a row in the registry (`utils/providers.ts`: defaults, labels, placeholders,
help text, how its models are discovered — the UI, persistence and the model
fetcher iterate it), a line in each measured table (`reasoningEffort` with its
`wc_text` port and parity cases; `wc_text/provider_profile.py`,
`context_window.py`, `chapter_index.py` batch sizes, Python only), and the wire format on the server: a
`Dialect` row in `wc_providers/openai_compat.py` for an OpenAI-compatible
API, a module of its own otherwise, named in `wc_providers.dispatch`.

**Reasoning effort**: `utils/reasoningEffort.ts` holds the per-provider
capability table (no provider exposes one over the API — xAI's
`/language-models` returns pricing and modalities only) plus the normalized
levels. The app default is **low**, not the provider's: grok-4.6 defaults to
`high` and was measured thinking for 127–199s before its first visible token.
`scripts/server_generation.py` applies it, and the remote start payload
must forward `reasoningEffort` (the payload copies config field by
field; a field left out is silently ignored, as `conversationId` was).
The client forwards the RAW setting; the backend resolves it against the
model (`wc_text/reasoning_effort.py`, the parity-tested port — for Anthropic,
OpenAI and Gemini; the tables were measured per model on 2026-10-10).
**Gemini** streams over SSE (`?alt=sse`, key in `x-goog-api-key`), takes
`thinkingLevel` on 3.x and a budget on 2.5 (`geminiThinking`), and always
gets `includeThoughts`: a `thought: true` part is reasoning, never document
text. 2.5 is closed to new keys, so the model list leaves it out. **Claude** takes `thinking: {type: "adaptive", display:
"summarized"}` + `output_config.effort` on Opus 4.6+ and every 5.x model,
which refuse `{type: "enabled", budget_tokens}`; 4.5 and earlier take only
the budget (`anthropicThinking`, measured per model 2026-10-10). Never send
`{type: "disabled"}` (several 5.x models refuse it) or temperature/top_p.
Anthropic counts thinking against `max_tokens`: with adaptive thinking the
configured limit is the visible output and the request adds 32k of headroom
(capped at 64k, `ADAPTIVE_THINKING_HEADROOM` in `wc_providers/anthropic.py`) —
a whole-chapter rewrite on sonnet-5-5 stopped at 16384 otherwise.

**Attachments and the web** (`docs/features/attachments_and_web.md`). A
book's reference files (a long text as `.txt`) live on the server, not in
IndexedDB, and are addressed as `A1`, `A2`… by the read tools
(`read_chapter` with `paragraphs` or `section`, `grep`, `analyze_book`). The
request carries only their index; never let a code path put an attachment's
text into the conversation whole — one read is capped at 20,000 characters
and one turn at 100,000 (`ATTACHMENT_RUN_READ_CAP`). `web_search` /
`web_read` run in a fresh, cookie-less headless Chromium on the API server
that refuses private addresses; a bot check is reported, never bypassed.
`web_images` brings a page's pictures into the book: fetched and shrunk on
the server, registered in the run's image registry, placed by the model as
`{{IMAGE_PLACEHOLDER_n}}` tokens. `view_images` shows a chapter's own pictures to a
vision model (by ¶ or number; 6 per call, 30 per run; inline or fetched like
`web_images`, carried on the tool result as `images`; each with its token).
Image tokens are numbered by the BOOK (`server_context.stable_image_registry`,
keyed by src): a token means the same picture in every turn and any chapter,
so writing one elsewhere copies the picture. `web_read` lists a page's same-site links (`L1 text — url`) and
takes up to 5 `urls`; visits to one host are ≥1 s apart; a bot check is
reported, never bypassed. `web_save` stores pages as ONE new attachment by
default (`_AttachmentsPort.save`, an `attachments` book event refreshes the
tabs), and `copy_paragraphs from_chapter="A3"` copies from it into chapters
(only an `A…` reference names an attachment there); `into="chapter"` writes
one page directly, its pictures inlined. A saved page's pictures are kept
beside the attachment (`<id>.images.json`, numbered through it) for
`copy_paragraphs` and `view_images chapter="A3"`; a page's decoration
(template icons, emoticons, small GIFs) is not listed; `web_images view=true`
shows web pictures to a vision model; a page whose address the USER wrote
(`RunState.user_urls`) is kept as an attachment by `web_read` itself, its
first 6 pictures shown — a hint to save it was measured ignored
(attachments_and_web.md §2d). `view_images` with a `task` looks at up to 150
pictures in batches of 8 as side calls and brings back a line each (§2e). `diffHtml` marks an `<img>` like a
word, so a placed or removed picture is reviewable and Reject undoes it. The Import-from-URL modal and its pipeline
are gone (2026-10-11): loading a page into a chapter is a chat request.

### Backend API (`scripts/api_server.py`)
FastAPI with cookie-based sessions (HttpOnly, SameSite=Lax) + CSRF
double-submit tokens. Per-user storage isolated as `state_<username>.json` /
per-book files; metadata indexed in a local SQLite DB at `.local_db/metadata.db`
(DELETE journal mode, kept off network mounts). Endpoints are under `/api/*`:
auth (`/api/auth/*`), books (`/api/books`, `/api/books/{id}`), nested documents
and versions. **Performance**: list endpoints extract metadata by regex
over the first few KB of large JSON files rather than full-parsing; the save
endpoint reorders JSON keys so `bookTitle`/`updatedAt` stay within that window.
**Chapter revisions** (`docs/features/backend_authority.md` §4.1): every
chapter has a server `revision`. A text save sends `baseRevision`, and a
stale one gets `409` with the current chapter; the client keeps its text as
a version and adopts the server's (`src/store/documentSync.ts`). Never
re-send an unchanged chapter's text, because it bumps the revision and
turns other tabs' saves into conflicts. `serverCopies` decides what is
unchanged, so record it wherever content loads. Writes send `X-Client-Id`.
`/api/books/{id}/events` streams every change to the book's open tabs
(`src/store/bookEvents.ts`).
Settings (provider configs with their keys, system prompts, theme) are the
user's, not a book's: one `user_settings` row per user, written by every
book save (`upsert_user_settings`). The old per-book `book_settings` table
is folded into it and dropped at startup (`migrate_user_settings`).
A book still remembers what it uses — the provider, that provider's model
and reasoning effort, the preset's id — in `book_preferences`, by reference:
every book save records them (`upsert_book_preferences`) and the book's GET
lays them over the user's settings (`apply_book_preferences`; a deleted
preset id falls back to the user's). Keys and preset texts are never per book.
A `user_state` table holds each account's **last active book**: every
book write records it, `/api/auth/session` returns it as
`lastActiveBookId`, and the client's init opens that book ahead of the
localStorage pointer — a new device must land in the book last worked in.

## Conventions

- **TypeScript everywhere**; avoid `any` except at API boundaries with explicit
  validation. `tsconfig` enables `noUnusedLocals`/`noUnusedParameters` — unused
  symbols fail the build.
- **English only** for code, comments, identifiers, docs, and commit messages.
  Non-English text is allowed only in data/i18n/translation files (`src/i18n/`)
  and user-facing localized defaults.
- **Shared types** live in `src/types/` and are re-exported through
  `types/index.ts`; the store re-exports many for backward compatibility.
- **Document performance fixes inline** with a Problem / Root Cause / Fix block
  comment (SKILL.md §3.6) so cleanup refactors don't silently revert them.
  Applies to backend code too.
- **Never write the user's test material into the repo.** What the user
  tries the app on — the pages and links they give it, site names and
  addresses, post titles and text, their books' chapters, characters and
  pictures — is private. It never goes into code, comments, tests,
  fixtures, docs, the Decision Log, commit messages or PR text. Describe a
  measurement generically ("a web page"), and write test data as
  invented text with `example.com` addresses. Live probes against real
  pages stay local (scratchpad), never committed.
- **Sensitive word lists** stay in git-ignored `public/sensitive_words.json`
  (template: `….example`), fetched at runtime — never hardcode them in source.

## Testing

- Vitest, `jsdom`, globals enabled. Shared setup in `src/test-setup.ts`.
- The runner only discovers `src/**/__tests__/**/*.test.ts` — tests outside that
  pattern are silently ignored. Co-locate tests in a `__tests__/` dir next to
  the code; name them `<module>.test.ts`.
- Priority: pure logic (transformations, parsing, diffing, serialization,
  migrations) → store actions → persistence/migrations → component behavior.
- **Hook flow tests**: `src/hooks/__tests__/useChatLLMServerRun.test.ts` drives
  the real hook against a mocked `fetch` and run events from the emitter, and
  asserts on the store, using a ~20-line `createRoot` + `act` harness — there is no
  `@testing-library/react` dependency, and adding one is not required. Keep
  such tests in `.ts` (no JSX) so the runner's `include` pattern picks them up.
- Tests must be deterministic and isolated; reset store/mocks/storage in
  `beforeEach`/`afterEach`; mock only at boundaries (`fetch`, LLM calls, timers).
- Coverage is collected over `src/utils/**` and
  `src/store/persistence.ts`. Extend the `include` list in `vitest.config.ts`
  when adding non-trivial pure-logic modules elsewhere.
- **Timeouts**: `testTimeout` (5s) catches a test awaiting a promise that never
  resolves. It does **not** catch a test that starves the event loop — an
  effect/render cascade never yields, so the runner's timers never fire and the
  suite wedges. The wall-clock bound in `.githooks/pre-push` is what covers
  that; keep it. A hook under test must receive **stable callback identities**
  (inline arrows in effect deps re-run the effect every render, which is exactly
  how that wedge happens).
- **New logic is not "done" until `npm test` passes and the logic is covered.**
- **Text logic the frontend still runs has two implementations** (`text`,
  `paragraphs`, `diff`, `diff_resolution`, `context_ledger`, `protocol_choice`,
  `title_sync`, `stream_handlers`, `reasoning_effort`): `scripts/wc_text/`
  mirrors `src/utils/*` byte for byte. Changing one of those TypeScript
  functions: port the change, run `npm run parity:fixtures`, then
  `pytest scripts/test_parity.py`. The vitest check fails on stale fixtures,
  so a one-sided change cannot pass `npm test`.
- **Everything else is Python only** (2026-10-17/18: the TypeScript nothing in
  the frontend ran was deleted — the agent loop, then the request assembly,
  prompts, summaries, transcripts, attachments, web text, polish, retries).
  Their fixtures are **Python-owned** golden files (`PYTHON_OWNED` in
  `test_parity.py`): after an intended change, `python3 scripts/test_parity.py
  --rewrite-python-owned` (it rewrites only outputs that changed) and review
  the diff. The prompt texts are data: edit `scripts/wc_text/data/prompt_texts.json`
  itself. The loop is tested in `scripts/test_agent.py` / `test_runs.py`.

## Git Workflow

- `master` is the default/deployable branch. Feature branches: `feat/<desc>`.
- **Conventional commits**: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`.
- A `pre-push` git hook (`.githooks/pre-push`, wired by `npm run prepare`) runs
  `npm test`, then the backend pytest suite (skipped with a warning where
  pytest/fastapi are missing), and blocks the push on failure. GitHub Actions
  (`.github/workflows/ci.yml`) runs the same two suites plus lint, type-check
  and build on every PR; `master` is protected by a ruleset (no force push or
  deletion, PRs only, the `frontend` and `backend` checks required). Bypass only in emergencies with
  `git push --no-verify` (discouraged).
- Before committing: run the unit suite (`npm test`), type-check/build
  (`npm run build` or `npx tsc --noEmit`), and a security scan of the diff for
  stray secrets/keys/local config.
- **Do not commit unless the user explicitly asks.** Do not open a PR unless the
  user explicitly asks.

## Releases

`npm run release` builds `release/web_canvas-<version>.tar.gz` (source + the
built `dist/`, so an install needs Python only) from a clean `git archive` of
`HEAD`, with no `.env` and an emptied environment: Vite bakes every `VITE_*`
it sees — including API keys — into the bundle, and the script refuses a
bundle with anything key-shaped. Bump `package.json`'s `version`, add a
`CHANGELOG.md` entry, merge, then build from the merged commit, tag it
`v<version>` and attach the archive and its `.sha256` to a GitHub release.
Install and operations docs: `README.md`, `docs/deployment.md`.

## Documentation Rules

- Every new design or feature spec must be registered in the **Decision Log**
  inside `docs/design.md` for traceability.
- Keep design docs, feature specs, and the Decision Log updated **before**
  committing or wrapping a phase of work.
- When you change behavior covered by `SKILL.md`, update `SKILL.md` too.

## Review Checklist (before merging a feature)

- [ ] Follows the design principles in `SKILL.md`.
- [ ] Document editing experience stays smooth; LLM edits remain reviewable
      (diff) and reversible (undo / version history).
- [ ] Works in both light and dark mode.
- [ ] Unit tests added for new functionality; no regressions (`npm test` green).
- [ ] `npm run build` / type-check passes; no secrets in the diff.

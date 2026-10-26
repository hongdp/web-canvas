---
name: web-canvas
description: >
  An LLM-powered web frontend for document editing, inspired by OpenAI/Google Canvas.
  Users interact with a chat interface alongside a live document canvas, using LLM
  capabilities to draft, edit, rewrite, and refine documents collaboratively.
---

# Web Canvas — Project Principles

## 1. Design Principles

### 1.1 The Document is the Product
The canvas (document) is the primary artifact. Chat is a means to shape the document,
not an end in itself. Every UI decision should prioritize the reading and editing
experience of the document over the chat experience.

### 1.2 Minimal Friction, Maximum Control
- **Direct manipulation first**: Users should be able to select text and act on it
  (rewrite, expand, shorten, translate) without leaving the canvas.
- **Chat as fallback**: Complex or ambiguous instructions go through chat; simple
  operations should be accessible via inline controls, keyboard shortcuts, or
  context menus.
- **Never destructive**: Every LLM edit must be reviewable (diff view) and
  reversible (undo stack / version history). The user always has the final say.

### 1.3 Progressive Disclosure
- New users see a clean chat + canvas split. Advanced features (version history,
  model settings, export options) are discoverable but never in the way.
- Quick-action buttons (e.g., "Fix grammar", "Make concise", "Change tone") are
  visible for common tasks; the chat input handles everything else.

### 1.4 Visual Clarity
- LLM-generated changes are visually distinct from user edits (e.g., highlighted
  diffs, animated insertions).
- The UI clearly communicates state: idle, thinking, streaming, awaiting review.
- Dark mode and light mode are first-class citizens, not afterthoughts.

### 1.5 Responsive & Adaptive Layout
- Desktop: side-by-side chat + canvas.
- Tablet: collapsible chat panel, full-width canvas.
- Mobile: stacked layout with swipe navigation between chat and canvas.

---

## 2. Implementation Principles

### 2.1 Architecture
- **Frontend-only MVP**: The initial version runs entirely in the browser, calling
  LLM provider APIs directly from the client (API key stored locally). No backend
  server is required for v1.
- **Component-based UI**: Use a modern component framework. Keep components small,
  focused, and composable.
- **State management**: A single source of truth for document content, chat messages, and streaming lifecycle. Note: Asynchronous stream callbacks must query the fresh store state dynamically (e.g., `useAppStore.getState().messages`) rather than closing over local variable snapshots to prevent stale closure bugs.

### 2.2 Document Engine
- Use a battle-tested rich-text editing library (e.g., TipTap / ProseMirror,
  Lexical, or CodeMirror for code documents) rather than building from scratch.
- The document model must support:
  - **Operational transforms or CRDTs** for future collaborative editing.
  - **Structured diffs** so LLM edits can be shown as additions/deletions.
  - **Serialization** to Markdown and plain text for LLM prompt construction.
- **TipTap Sync Hook**: TipTap only reads content once on mount. To synchronize programmatically updated content (e.g., LLM streams), implement a `useEffect` hook that checks for updates and calls `editor.commands.setContent(content, { emitUpdate: false })` to avoid loop recursion. **Critical**: Use a `contentFromEditorRef` to track content that originated from the editor's own `onUpdate` callback, and skip `setContent` when the incoming content matches — otherwise user edits will "roll back" due to a race condition in the `onUpdate → Zustand → re-render → useEffect` loop. See § 3.2 and `Editor.tsx`.

### 2.3 LLM Integration
- **Provider-agnostic**: Abstract the LLM API behind a provider interface.
  Support OpenAI, Google Gemini, Anthropic, and local models (Ollama) from day one.
- **Streaming responses**: All LLM calls stream tokens into the document or chat
  in real time. Never block the UI waiting for a full response.
- **Prompt engineering is internal**: Users write natural language; the system
  constructs structured prompts (system message, document context, selection
  context, instruction) behind the scenes.
- **Context windowing**: For long documents, send only relevant sections (around
  the selection or the full document if small enough), not the entire history.
  Chat history is trimmed to a character budget (most recent first), base64
  images are only re-sent for the last few messages, and read-only reference
  documents are truncated per-doc with an explicit notice (`src/utils/llmContext.ts`).
- **Protocol compliance is probabilistic — recover, don't just instruct**: the
  model sometimes replies with a bare acknowledgement ("好的。",
  ~13 output tokens) and no `<canvas>`/`<edit>` tags, so nothing reaches the
  document while the chat claims success. Measured against grok-4.5
  (2026-07-25): it happens with the preset disabled and with no chat history,
  and the success rate for an identical prompt drifted between 22% and 65%
  within the same hour — no prompt wording tested moved it beyond the noise
  (history elision correlates: ~32% with stripped assistant turns vs ~71% with
  none, but does not explain the failure). Treat tag compliance as a fallible
  input: detect "no document action" client-side, retry once, and surface a ⚠️
  rather than reporting success. Do not add prompt text on an n=1 result.
- **History hygiene**: What the model sees as its own past turns must be what it
  actually said. UI-appended artifacts (`[Attached Context: …]` labels, `⚠️`
  truncation/edit-skip/stream-error notes) are stripped before messages re-enter
  the prompt; empty messages are dropped and consecutive same-role turns merged
  so stricter providers never see an invalid sequence. Read-only context
  (reference docs, world lore, game state) is converted to structured plain text
  (`htmlToPlainText` — block boundaries kept, entities decoded); only the active
  document keeps verbatim HTML, which the `<edit>` SEARCH protocol requires.
- **Cache-friendly prompt layout**: Assemble requests as
  `[stable system prompt] + [windowed chat history] + [volatile document/game-state
  context merged into the FINAL user message]`. Volatile content must come last:
  it changes every turn, and placing it early invalidates provider prompt caches
  (OpenAI/Gemini prefix caching, Anthropic `cache_control` — hinted via
  `LLMMessage.cacheHint` on the last history message). It also keeps the current
  document adjacent to the request, which improves `<edit>` SEARCH fidelity.
- **Canvas Markup Protocol**: Restructure streaming data separating conversational response text from document updates using XML-like blocks. The LLM wraps document updates inside `<canvas>...</canvas>` blocks, which the frontend extracts to stream directly to the editor canvas while routing outer text to the chat.
- **The format protocol is always last**: the chat system prompt is assembled in
  a fixed order (`src/utils/systemPrompt.ts`) — protocol rules → chapter-lookup
  protocol → the user's writing preset → `FORMAT_PROTOCOL_REMINDER`. Presets are
  user-authored and routinely carry output-channel language ("output the prose
  directly", "add no explanations", "avoid non-`<language>` text"); appended last
  they outrank the protocol and the model answers in chat with no tags, which
  silently leaves the document untouched. Anything appended after the preset must
  keep the reminder in final position — recency is the whole mechanism.
- **Dynamic Model Discovery**: Fetch available models dynamically via Google's ListModels API or provider configuration endpoints when the API key is set, falling back to static offline model lists if configuration is missing.
- **Safety Self-Healing & Prompt Editor UI**: When an LLM request fails due to a safety threshold or guideline violation (e.g. 403 safety blocks), the app automatically triggers a self-healing retry by applying local sensitive word censorship to the user prompts. If this auto-retry fails, the system transitions to an interactive Prompt Editor UI (`status === 'prompt_edit'`), allowing users to inspect/edit the raw system/user prompts, retry manually, or save progress and exit.

### 2.4 Code Quality
- TypeScript everywhere. No `any` types except at API boundaries with explicit
  validation.
- Every component has a clear interface (props/events). No prop drilling beyond
  two levels — use context or state management.
- CSS variables for theming. No magic numbers in styles.
- All user-facing strings are externalizable (prepare for i18n even if not
  implemented in v1).

### 2.5 Testing Strategy
- **Mandatory Unit Tests**: All new functionalities and components MUST be accompanied by comprehensive unit tests. You must write tests alongside your code to ensure correctness and prevent future regressions.
- **Unit tests** for state management, prompt construction, and document
  transformations.
- **Integration tests** for the LLM provider abstraction (mock API responses).
- **E2E tests** for critical user flows: create document → chat edit → review
  diff → accept/reject.

#### 2.5.1 Unit Testing Guidelines
These rules govern how unit tests are written and run in this project. The stack is **Vitest** (`jsdom` environment, globals enabled) configured in [vitest.config.ts](/vitest.config.ts).

**Tooling & Commands**
- Run the full suite with `npm test` (`vitest run`). Use `npm run test:watch` while developing and `npm run test:coverage` to inspect coverage.
- Tests run under `jsdom`; browser globals (`window`, `document`, `localStorage`) are available. Shared setup lives in [src/test-setup.ts](/src/test-setup.ts) — register global mocks/polyfills there, not in individual specs.

**File Layout & Naming**
- Co-locate tests in a `__tests__/` directory next to the code under test (e.g. `src/utils/__tests__/text.test.ts` for `src/utils/text.ts`).
- Name files `<module>.test.ts`. The runner only discovers `src/**/__tests__/**/*.test.ts` — files outside this pattern will be silently ignored.
- Import the functions under test by relative path and the matchers explicitly: `import { describe, it, expect } from 'vitest'`.

**What to Test (priority order)**
1. **Pure logic first**: extract document transformations, prompt construction, parsing (e.g. the canvas markup protocol), diffing, serialization, and migrations into pure functions and test them exhaustively. These are the highest-value, lowest-cost tests.
2. **State management**: Zustand store actions and reducers — assert on the resulting state, including the stale-closure and optimistic-update behaviors described in §2.1 and §3.2.
3. **Persistence & migrations**: every versioned-envelope migration (§2.6) MUST have a test that feeds in a prior-version payload and asserts the upgraded shape, including default-field merging.
4. **Components**: test behavior (rendered output, event handlers, state transitions) rather than implementation details.

**Test Structure & Quality**
- Group related cases under a `describe` block per function/unit; write one `it` per behavior with a sentence-style name describing the expected outcome.
- Cover the **happy path, edge cases, and error/empty inputs** (empty strings, nulls, boundary sizes, malformed input). Follow the existing thoroughness in [text.test.ts](/src/utils/__tests__/text.test.ts).
- Each test must be **deterministic and isolated**: no reliance on real network, wall-clock ordering, or leftover state from other tests. Reset shared state (store, mocks, storage) in `beforeEach`/`afterEach`.
- **Mock at boundaries only**: stub LLM provider calls, `fetch`, and timers with `vi.mock` / `vi.useFakeTimers`. Never mock the unit under test itself.
- Assert on **observable outcomes**, not internal call sequencing, unless the interaction itself is the contract (e.g. a provider adapter must send a specific request shape).

**Coverage Expectations**
- Coverage is collected (v8) over `src/utils/**` and `src/store/persistence.ts`. New pure-logic modules in these areas are expected to keep coverage high — when adding a non-trivial module elsewhere that warrants coverage, extend the `include` list in [vitest.config.ts](/vitest.config.ts).
- A new feature is not "done" until `npm test` passes locally and the new logic is covered. This is part of the §4.1 pre-commit verification and the §4.2 review checklist.

### 2.6 Persistent Data Versioning
- **Versioned Envelopes**: Any persistent structures stored client-side (cookies, localStorage, or IndexedDB) that are subject to future updates MUST be stored in a versioned envelope `{ version: number, data: T }`.
- **Sequential Schema Migrations**: When updating a schema, increment the version number and write a safe, sequential migration pipeline (e.g., `v0 ➔ v1 ➔ v2`) to dynamically parse, map, and rewrite the stored state without destroying existing user settings.
- **Default Fallback Merging**: Migrated structures MUST always be merged with default configuration models to guarantee that newly introduced fields are correctly initialized for existing installations.
- **Self-Healing Persistent Layer**: Migrations must be triggered during store initialization, automatically rewriting the updated, validated, and versioned state back to client storage.

---

## 3. Operation Principles

### 3.1 Security & Privacy
- **No server, no data leakage (v1)**: API keys are stored in browser
  `localStorage` (or a more secure browser API if available). Document content
  never leaves the browser except to the user-configured LLM provider.
- **Key hygiene**: Warn users if they attempt to share/export a document that
  contains embedded API keys. Provide a "clear keys" action.
- **CSP headers**: If served from a static host, configure Content Security
  Policy to restrict outbound requests to known LLM API domains only.
- **Sensitive Word Isolation**: Local sensitive keyword lists must be stored in a git-ignored JSON file (`public/sensitive_words.json`), using a template (`public/sensitive_words.json.example`) for developers. The web application dynamically fetches this file at runtime, ensuring sensitive keywords are kept out of source control.

### 3.2 Performance
- **Time-to-interactive < 2s** on a modern connection. Lazy-load non-critical
  features (version history panel, export module, settings).
- **Streaming latency**: First token from the LLM should appear in the UI within
  the network round-trip time — no artificial buffering.
- **Large documents**: The editor must handle documents up to 50,000 words
  without jank. Virtualize the rendering if needed.
- **TipTap Content Sync — Avoiding the Rollback Race Condition**: When TipTap's `onUpdate` fires, the new HTML flows through `onChange → Zustand → React re-render → content prop`. The `useEffect` that synchronizes the `content` prop back into TipTap must **never** overwrite the editor with content that originated from its own `onUpdate`. Use a ref (`contentFromEditorRef`) to track editor-originated HTML and skip `setContent` when the incoming prop matches. Without this guard, user edits (especially large pastes) appear to "roll back" after a brief delay because the slightly-stale store HTML overwrites the live editor state. See `Editor.tsx`.
- **TipTap `setEditable` Emits an Update**: `editor.setEditable(editable, emitUpdate = true)` fires a synthetic `update` event, and `onUpdate` publishes `editor.getHTML()` to the store. Anything painted into the editor off the record (the live LLM preview uses `emitUpdate: false`) leaks into the store the moment read-only is toggled — which is exactly what the Stop button does. Always call `setEditable(x, false)`: toggling editability changes no content and must publish none. **Stop keeps the draft**: stopping a generation commits the half-streamed document as a single undo step (pre-stream HTML restored outside history, then the draft applied as one recorded transaction) and writes the same HTML to the store, so the screen, persistence, and Undo agree. See `Editor.tsx` and `keepCanvasPreview` in `useChatLLM.ts`.
- **Optimistic UI for Server-Dependent Operations**: Any operation that creates, updates, or deletes data on the server (e.g., book creation, book deletion) must update the local Zustand state **synchronously first** and perform the server round-trip in the background (fire-and-forget). The UI should never block waiting for a server response. If the server call fails, surface an error indicator (e.g., `serverSaveStatus: 'failed'`) but do not roll back the local state. See `createNewBook` in `useAppStore.ts`.
- **Lightweight Server Metadata Extraction**: When the server needs to list items whose full data is very large (e.g., book JSON files containing embedded base64 images, chat history, chapters), never fully parse the entire file just to extract metadata fields. Instead, read only the first few KB and use regex to extract the needed fields. Ensure the save endpoint reorders JSON keys to place metadata fields (`bookTitle`, `updatedAt`) at the top of the file so they are always within the read window. See `extract_book_metadata` and `save_storage` in `api_server.py`.

### 3.3 Persistence & Recovery
- **Auto-save to localStorage / Backend**: Auto-save updates asynchronously in the background. In local setups, changes are persisted via write-through caching.
- **Hybrid Storage Model (IndexedDB + LocalStorage)**: Heavy workspace datasets like `documents` and `versions` (containing large embedded Base64 image payloads) are stored asynchronously using browser-native **IndexedDB** to avoid browser `QuotaExceededError` (5MB limit). Lightweight keys (e.g., `theme`, layout states, credentials) are stored in synchronous `localStorage` to prevent visual flashes.
- **Client-Side GIF Image Processing (First-Frame JPEGs)**: To support uploading and pasting GIF images up to 15MB, the app dynamically converts GIFs to static JPEGs using HTML5 Canvas. The conversion extracts the first frame of the GIF and compresses it, ensuring the resulting data URL fits within the 2MB size limit and works seamlessly with LLM Vision APIs.
- **Automated LocalStorage Migration**: On initialization, if the store detects legacy document or version keys in `localStorage`, it automatically imports them into the IndexedDB database and purges them from `localStorage` to free up space immediately.
- **Export**: Support exporting to Markdown, plain text, HTML, and PDF.
- **Import**: Support importing Markdown, plain text and HTML files through the sidebar's file import. The Import-from-URL modal and its scrape/outline/generation pipeline were removed on 2026-10-11; loading a web page into a chapter is a chat request (`web_read`).
- **Crash recovery**: On reload, detect unsaved state and offer to restore.

### 3.4 Deployment
- Static site deployment (Vercel, Netlify, GitHub Pages, or any CDN).
- No server-side dependencies for v1.
- CI pipeline: lint → type-check → test → build → deploy.

### 3.5 Observability (Future)
- Optional, opt-in analytics for usage patterns (which quick actions are popular,
  average edit cycle length).
- Error reporting for failed LLM calls (rate limits, malformed responses) to
  surface issues to the user gracefully.

### 3.6 Performance Optimization Documentation
Performance fixes are uniquely fragile — a developer who doesn't understand **why** a pattern exists may inadvertently revert it during a "cleanup" refactor. To prevent this, all performance optimizations must be documented **inline in the code** with the following structure:

1. **Problem Statement**: What symptom was observed (e.g., "editor content rolls back after paste", "book creation takes 5+ seconds").
2. **Root Cause**: The technical explanation of why the problem occurs (e.g., "race condition in the TipTap ↔ Zustand sync loop", "server parses 50MB JSON per book for a list endpoint").
3. **Fix Rationale**: Why this specific solution was chosen and what constraints it satisfies (e.g., "ref-based tracking avoids the race without breaking LLM streaming sync", "regex on first 4KB avoids full parse while key reordering guarantees the fields are within the read window").

Use a structured block comment format for significant optimizations:
```typescript
// ── Performance-Critical: <Title> ──────────────────────────────
// Problem: <what the user sees>
// Root Cause: <why it happens technically>
// Fix: <what we do and why this approach>
// ───────────────────────────────────────────────────────────────
```

For smaller inline fixes, a one-liner comment explaining "why" (not "what") is sufficient:
```typescript
// Skip setContent when the content came from our own onUpdate to prevent rollback
```

**This rule applies equally to frontend and backend code.** Server-side performance patterns (e.g., lightweight metadata extraction, JSON key reordering) must also carry rationale comments.

---

## 4. Development Workflow

### 4.1 Branching & Commits
- `main` is always deployable.
- Feature branches named `feat/<short-description>`.
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`.
- **Pre-commit Verification**: Always execute comprehensive testing and compilation validation (e.g., run `npx tsc --noEmit` and build verification) **before** committing changes to ensure no broken code enters the branch history.
- **Pre-commit Unit Test Gate**: The full unit test suite (`npm test`) MUST pass **before** any commit. Never commit with failing or skipped tests. If a change touches tested logic, run the suite and confirm green; if it adds new logic, the accompanying tests (see §2.5) must be present and passing. A red suite is a hard block on committing — fix the code or the test, never bypass the gate.
- **User Approval**: Never perform git commits unless explicit instruction or approval is obtained from the user.
- **Pre-commit Security Check**: Always perform a careful security scan (e.g., check `git diff` or staged files) **before** committing to ensure no private information, API keys, or local deployment configurations are accidentally staged or checked in.

### 4.2 Review Checklist
Before merging any feature:
- [ ] Does it follow the design principles above?
- [ ] Is the document editing experience still smooth?
- [ ] Are LLM edits reviewable and reversible?
- [ ] Does it work in both light and dark mode?
- [ ] Have unit tests been added for the new functionalities?
- [ ] Are there no regressions in existing tests (Unit/E2E)?

### 4.3 Documentation Rules
- Every separate design or feature specification document created must be registered in the **Decision Log** inside the main design document ([design.md](/docs/design.md#L375)) to maintain clear traceability.
- Always ensure all related design documents, feature specifications, and Decision Log entries are fully updated **before** committing changes or concluding a phase of work.

### 4.4 Language Policy
To ensure the codebase remains universally accessible and maintainable:
- **English Only for Project Files**: All project source code, documentation, inline comments, variable names, function definitions, pull request descriptions, and Git commit messages **MUST** be written in English.
- **Exceptions (Data and Configurations)**: You are allowed to use other languages (e.g., Chinese, Spanish, etc.) **ONLY** within data files, localized asset bundles, translation files (i18n), or user-facing configuration defaults where multiple languages are supported as part of the app's functionality.

---

## 5. Local Deployment Workflow

This section outlines the guidelines and procedure to build and run the local development/deployment server for Web Canvas.

### 5.1 Prerequisites
- **Node.js**: Version 18 or higher.
- **npm**: Standard Node package manager.
- Install packages:
  ```bash
  npm install
  ```

### 5.2 Environment Configuration
Configure environment variables in `.env` (copying from `.env.example`). Configure API keys and storage parameters.
- `VITE_STORAGE_DIR`: Fallback storage path for books, users, and session databases (if not overridden via CLI).

### 5.3 Build Verification
Before deploying or checking in, compile TypeScript and bundle assets to verify build integrity:
```bash
npm run build
```

### 5.4 Launching the Server
Start the local server programmatically with custom configurations:
```bash
npm run dev -- --storage-dir <absolute-or-relative-path> [--host] [--mode debug]
```
- `--storage-dir`: Specify absolute/relative path where book states and database files are persisted.
- `--host`: Exposes the Vite dev server to external networks.
- `--mode debug`: Starts Vite in debug mode (enables debug logs).

Example command:
```bash
npm run dev -- --storage-dir /path/to/storage --host
```

### 5.5 Listening Port Verification
Ensure the server is running and listening on port `5173`:
```bash
ss -tulpn | grep 5173
# Or
lsof -i :5173
```

### 5.6 Server Restart Gotchas

#### Stale Port 3000 Causes Silent Failures
The Python API server binds to `127.0.0.1:3000`. If a previous Python process is still alive (e.g. from a diagnostic run or a failed start), the new API server will fail with `[Errno 98] address already in use`, exit with code 1, and `start-server.js` will immediately kill Node/Vite too — the whole stack silently dies within seconds of appearing to start.

**Diagnosis flow:**
```bash
# 1. Check if both ports are up
ss -tlnp | grep -E "3000|5173"

# 2. Check which PID owns port 3000
lsof -i :3000

# 3. Run Python directly to confirm it starts clean
python3 scripts/api_server.py \
  --storage-dir /path/to/storage \
  --host 127.0.0.1 --port 3000
# Should print: [Storage Server] Listening on http://127.0.0.1:3000
```

**Fix — always hard-kill before restarting:**
```bash
pkill -9 -f "api_server.py" || true
pkill -9 -f "start-server.js" || true
sleep 2   # wait for OS to release ports
```

#### `pkill` and Other `start-server` Processes
When writing restart scripts, do not use `pkill -f "start-server"` or similar broad patterns: other node processes can carry `--start-server` in their command line, and the pattern would kill them too. Match the specific script name (`api_server.py`, `start-server.js`) or use `fuser -k` on the port instead.

#### `start.sh -d` (daemon mode) May Not Survive a Non-Interactive Shell
The `nohup` + `disown` approach in `start.sh -d` may not keep processes alive when it is started from a non-interactive shell: the processes start but die within seconds because the parent shell exits before the children are fully detached.

**Preferred approach — run it under a service or a terminal multiplexer:**
```bash
npm run dev -- --storage-dir /path/to/storage --host
```
Run this in a terminal multiplexer (tmux, screen) or as a service (e.g. a systemd user unit), so it lives independently of the shell session.

#### Checking True Server Health
`start.sh --status` only checks by process name. Prefer port-level verification for ground truth:
```bash
ss -tlnp | grep -E "3000|5173"
# Both lines present = both servers running
# Only 3000 present = Python up, Vite dead (likely port conflict on prior start)
# Neither = everything stopped
```

Also verify Vite responds:
```bash
curl -k https://localhost:5173 -o /dev/null -w "%{http_code}"
# Should return 200
```

---


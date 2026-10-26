# Changelog

## 1.3.0 — 2026-10-26

The assistant can now explore the web for material and keep it in the book:
it follows a page's links, saves pages (with their pictures) as attachments,
and copies the parts you want into chapters without re-typing them. It can
look at many pictures at once and place pictures in the text when asked.
Download `web_canvas-1.3.0.tar.gz` from the GitHub release (it needs Python
only), or on Windows run `WebCanvas-Setup-1.3.0.exe`; see the README.

### Web pages and attachments

- `web_read` lists a page's links on the same site (the page's own links
  first, menus after) and reads up to five pages in one call. Visits to one
  host are at least a second apart; a page that answers with a bot check is
  reported, never bypassed.
- `web_save` stores pages as one attachment, pictures included; a page whose
  address you write in your message is kept automatically when it is read.
- `copy_paragraphs` copies a range of an attachment's paragraphs into a
  chapter, pictures included.
- Text written straight into a `<div>` (with `<br>` line breaks) is read as
  paragraphs; a site's decoration (icons, smilies, rank images) is not listed
  as pictures.

### Pictures

- `view_images` with a task looks at up to 150 pictures in batches outside
  the conversation and brings back one line per picture; `web_images` can
  show what it fetched; the assistant is told that a picture's token inserts
  that picture.
- Image tokens are keyed by the whole tag, so a thumbnail and the full-size
  picture of the same file stay distinct.

### Editing

- `copy_paragraphs` and `move_chapter` move or copy text the book already has
  instead of re-typing it; `edit_paragraphs` deletes a whole range.
- `grep` is capped by the size of its result rather than its pattern count.
- Polishing is offered only with a polish model other than the chat model.

### Fixes

- Reject All empties a chapter the assistant created, instead of doing
  nothing.
- A selection that went nowhere no longer locks its chapter.
- A question asked before an API restart can still be answered; a tab that
  missed a run's end settles it.

## 1.2.0 — 2026-10-18

Every chat turn now runs on the server, so closing the page, a phone going
to sleep or an API restart no longer stops one. The assistant can look at a
chapter's pictures and reuse them in other chapters. Moonshot's Kimi joins
the providers, and roleplay mode is removed. Download
`web_canvas-1.2.0.tar.gz` from the GitHub release (it needs Python only), or
on Windows run `WebCanvas-Setup-1.2.0.exe` (it needs nothing); see the README.

### Windows

- A Windows installer (`WebCanvas-Setup-<version>.exe`, built on GitHub Actions
  for every release): a per-user install with no administrator rights. It puts
  Web Canvas in the Start menu, opens it in the browser, and keeps the books in
  `%LOCALAPPDATA%\WebCanvas`, which uninstalling leaves in place. The web search
  and page-reading tools are not included (they need Playwright's Chromium).
- `WEB_CANVAS_DB_DIR` moves the metadata DB, its key and the step journal out
  of the program's folder (the installer uses it).

### The assistant

- **Server-only turns.** The browser's agent loop is gone. Every turn,
  including the Polish button, runs in the API process, and the tab only
  renders it. A reload or another device rejoins the turn in progress. The
  "Run turns on the server" setting is gone, because there is nothing else.
- **Pictures.**
  - `view_images` shows a vision model (Claude, OpenAI, grok, Gemini) a
    chapter's pictures, chosen by ¶ or number: inline images, or linked ones
    fetched through the server's browser.
  - Each picture comes with its token. Writing the token into another chapter
    copies the picture there.
  - Tokens are numbered per book, so a token means the same picture in every
    turn.
  - A placed or removed picture is part of the review diff, framed in the
    editor, and Reject undoes it.
- **New tools and behaviors.**
  - `replace_text` finds and replaces across chapters in one call.
  - Deleting a chapter that has text asks first.
  - A rename carries the chapter's heading.
  - A plan left unfinished carries into the next turn.
  - A whole rewrite aimed at a chapter that does not exist is kept, and
    placed once the model names where it goes.
  - A message sent mid-turn is shown splitting the turn.
- **Providers.**
  - Moonshot Kimi (`moonshot`) is added.
  - Claude gets 1M windows, headroom for adaptive thinking, and cache
    diagnostics on every request.
  - Gemini safety thresholds default to `BLOCK_NONE`, the provider's lowest
    filtering level; each category can be raised in Settings.
  - Prompt caching was measured on grok, OpenAI and Gemini, and the log now
    shows cache writes.
  - A provider quota that asks for hours is no longer retried in a tight
    loop.

### Fixes

- A selection rewrite lands on the selected text, never at the cursor, and
  drops its tag's line breaks.
- Chapters are named by position (`#n`) in the index and in listings.
- Accepting the review diff mid-turn, or the editor re-spelling a chapter,
  no longer counts as a user edit that refuses the next write.
- A paragraph anchor matches a list entry that starts with "•".
- The history budget is calibrated to each model's token count, and old
  results are elided only when that makes the prompt fit.
- Resize dividers track the cursor.

### Removed

- The roleplay ("game master") mode.
- The TypeScript copies of the agent loop and of the text logic only it
  used. Python is now the specification for those modules.

## 1.1.0 — 2026-10-11

The assistant can now bring a web page's pictures into the book, the
Import-from-URL modal is gone, chapter summaries are retired, settings are per
user with per-book preferences, and three live failures on Claude 5.x and
long turns are fixed. Download `web_canvas-1.1.0.tar.gz` from the GitHub
release; it needs Python only (see the README).

### Fixes

- A string cut in the middle of an emoji no longer loses a step (the server
  rejected the request: "surrogates not allowed"); cuts avoid the middle of
  a character, and the server replaces any stray half.
- Anthropic: a thinking block that came after the reply's text is replayed
  after it; replaying it ahead made the next step a 400 ("cannot be
  modified") on Claude 5.x.
- The footer's chapter length is the count the assistant's tools report:
  characters of the text, punctuation included (it used to leave punctuation
  out, so the two numbers differed by ~15% on Chinese prose).
- An image inside a text paragraph shows as `[image]` in reads and grep and
  survives a paragraph replace.

### Features

- `web_images`: the assistant fetches a page's pictures (numbered in
  `web_read`) and places them in a chapter as it writes. The Import-from-URL
  modal and its scrape pipeline are removed; loading a page into a chapter is
  a chat request. `Pillow` joins the requirements (image shrinking).
- Chapter summaries are no longer used; the chapter index is titles and
  markers. Settings are per user; each book remembers its provider, model,
  effort and preset.

### Project

- CI on GitHub Actions (lint, type-check, both test suites, build) on every
  PR; `master` is protected (PRs only, checks required). Legacy files and the
  per-machine dev certificate are out of the repository; a fresh clone builds.

## 1.0.0 — 2026-10-11

The first installable release. Download `web_canvas-1.0.0.tar.gz` from the
GitHub release; it needs Python only (see the README).

### The assistant

- Agentic chat turns: the model reads, greps and lists the book, plans,
  writes, renames and creates chapters through tools, asks questions, and can
  be steered mid-turn. Edits arrive as reviewable diffs; every turn is
  snapshotted first.
- Turns can run on the server, survive reloads, restarts and device changes,
  and pause rather than die when they loop or run long.
- Pinned context: the chapters you pin ride with every request; the model
  reads the rest itself.
- Each request extends the previous one (turn transcripts replayed, context
  sent as differences, a frozen ledger, summaries made from the live
  conversation), so prompt caches keep hitting across turns.
- Reference attachments (long `.txt` files read in sections) and web search /
  page reading (with Playwright).

### Providers

- Anthropic: adaptive thinking with an effort level on Claude Opus 4.6+ and
  every 5.x model (they refuse a token budget), a budget on 4.5 and earlier;
  thinking streams live; refusals and in-stream errors are reported.
- OpenAI: models at api.openai.com run on the Responses API (from gpt-5.4 on,
  Chat Completions refuses tools with reasoning); reasoning carried across
  steps and turns; non-strict tools. Compatible servers stay on Chat
  Completions.
- Gemini: Gemini 3 models (2.5 is closed to new keys), thinking levels,
  thoughts streamed as reasoning, SSE streaming, the key kept out of URLs.
- Grok on the xAI Responses API; Ollama, llama.cpp and RunPod endpoints.
- Model lists come from your key (through the server); reasoning effort is
  resolved per model on the server.
- Generation always runs on the server; the in-browser transport is gone.

### Workspace

- Settings belong to the user; each book remembers its provider, model,
  effort and system-prompt preset.
- Chapter summaries are no longer used: the chapter index lists chapters by
  number and title (stale summaries had been copied between books).

### Installing and running

- `python3 scripts/api_server.py --serve-dist` serves the built app and the
  API from one process, optionally over HTTPS (`--ssl-certfile`,
  `--ssl-keyfile`).
- `requirements.txt` / `requirements-dev.txt` for the server.
- `npm run release` builds the archive from a clean export with no `.env`
  and refuses a bundle containing anything shaped like an API key.
- A fresh clone builds without a local development certificate.

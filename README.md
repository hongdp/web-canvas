<div align="center">

<img src="public/icons/icon-192.png" width="96" alt="Web Canvas" />

# Web Canvas

**A self-hosted writing workspace where an AI agent works on your whole book — and every change is a diff you review.**

[![Latest release](https://img.shields.io/github/v/release/hongdp/web-canvas?label=release)](https://github.com/hongdp/web-canvas/releases)
[![CI](https://github.com/hongdp/web-canvas/actions/workflows/ci.yml/badge.svg)](https://github.com/hongdp/web-canvas/actions/workflows/ci.yml)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](#install-from-a-release)
[![Claude · OpenAI · Gemini · Grok · Kimi · local](https://img.shields.io/badge/models-Claude%20%7C%20OpenAI%20%7C%20Gemini%20%7C%20Grok%20%7C%20Kimi%20%7C%20local-6c47ff.svg)](#supported-models)

[Install](#install-from-a-release) · [How it works](#how-it-works) · [Features](#features) · [Develop](#develop-from-source) · [Docs](#documentation)

<img src="docs/images/workspace.png" alt="The Web Canvas workspace: chapters sidebar, chat with the assistant, and the editor" width="900" />

</div>

---

Chat assistants that "help you write" usually see one page at a time, rewrite
it wholesale, and forget everything between turns. Web Canvas is built for
long-form work — multi-chapter novels, documentation, anything with structure:

- **The agent reads and edits the whole book.** It greps for a scene, reads a
  chapter, consults the outline you pinned, then writes — by paragraph, by
  selection, or a full rewrite — across any chapter, in one turn.
- **Nothing lands without your review.** Every edit streams in as an inline
  diff you accept or reject, every turn is snapshotted first, and version
  history restores any earlier state.
- **Long sessions stay cheap and coherent.** Each request extends the previous
  one so the provider's prompt cache keeps hitting; history past the window is
  summarized, not cut; the model's reasoning is carried across steps and turns.
- **It is yours.** One Python process on your own machine, your own API keys,
  per-user accounts, no third-party service in the middle.

## Quick start

A release archive contains the built app, so it needs **Python 3.10+** only.

```bash
curl -LO https://github.com/hongdp/web-canvas/releases/download/v1.3.0/web_canvas-1.3.0.tar.gz
tar xzf web_canvas-1.3.0.tar.gz && cd web_canvas-1.3.0
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python3 scripts/api_server.py --serve-dist --port 8080 --storage-dir ./storage
```

Open <http://localhost:8080>, create an account, and add an API key under
**Settings**. That is the whole install — see [Install from a release](#install-from-a-release)
for HTTPS, other devices and the server options.

**On Windows**, download `WebCanvas-Setup-<version>.exe` from
[Releases](https://github.com/hongdp/web-canvas/releases) and run it —
no Python, no administrator rights. See [On Windows](#on-windows).

## How it works

```
 you                      the agent (one chat turn = a run of steps)                 your book
 ───                      ─────────────────────────────────────────                 ─────────
 "write chapter 7         ① plan        ② grep "the letter"      ③ read #3 ¶40–60   chapters,
  from the outline"  ──▶  ④ read the pinned outline  ⑤ write #7  ⑥ reply       ◀──  attachments,
                                                                                    the web
                          every write → an inline diff in the editor → you accept / reject / undo
```

- **Tools, not one giant prompt.** The model gets `read`, `grep`, `list`,
  four ways to write (whole chapter, search-and-replace edits, paragraph
  edits, selection rewrite), `rename`, `polish`, `plan`, `ask_user`,
  `web_search` and `web_read`. Reads come back as numbered paragraphs; edits are addressed
  by paragraph number with an anchor, so a stale edit is refused instead of
  landing in the wrong place.
- **Context you control.** Pin the outline and the character sheet; they ride
  ahead of the conversation in a cache-stable block. The model fetches the
  rest itself, so the prompt carries what the turn needs and nothing else.
- **Between steps, the loop talks back.** A failed search quotes the nearest
  paragraph; a plan comes back as a checklist; a reply that claims a write it
  never made gets the editor's facts; a turn that loops or runs long pauses
  instead of burning tokens.
- **Runs live on the server.** Close the tab, reload, open the book on your
  phone — the turn continues and the bubble catches up. A restart resumes a
  run from its last step.

## Features

| | |
|---|---|
| **Editing** | Rich-text editor (TipTap) · inline diffs per change · selection-aware rewrites and quick actions · a polish pass · images in chapters · undo that always matches what is on screen |
| **The agent** | Whole-book tools · paragraph-addressed edits with anchors · plans and questions · steerable mid-turn · pauses instead of dying · server-side runs that survive reloads |
| **Context** | Pinned chapters · cache-continuous requests (transcripts replayed, context as differences, frozen ledger) · conversation summaries from the live conversation · reasoning carried across turns (Grok, OpenAI) |
| **Sources** | Attach long `.txt` files (reference texts, notes) and read them in sections · web search, page reading and image fetching through a headless browser (optional) — "load this page into a chapter" is a chat request · import `.md`, `.html`, `.txt` files |
| **Books** | Many books per account · chapters sidebar with drag ordering · version history and snapshots · export to Markdown / HTML / text · each book remembers its model, effort and prompt preset |
| **Workspace** | Accounts with secure sessions · settings and keys per user · light and dark themes · desktop, tablet and phone layouts · installable PWA · image generation |

## Supported models

| Provider | Notes |
|---|---|
| **Anthropic Claude** | Adaptive thinking with an effort level (Opus 4.6+, all 5.x); thinking streamed live |
| **OpenAI** | Responses API at api.openai.com (GPT-5.x, GPT-6.x, o-series) with reasoning carried across steps and turns |
| **Google Gemini** | Gemini 3.x with thinking levels; thoughts streamed as reasoning |
| **xAI Grok** | Responses API with encrypted reasoning replayed; repetition checks |
| **Moonshot Kimi** | kimi-k3 (1M context, effort low/high/max) and the K2 line; reasoning carried through tool-calling loops; `.ai` and `.cn` hosts |
| **Local / rented** | Any OpenAI-compatible server: Ollama, llama.cpp, a RunPod pod |

Model lists come from your key; reasoning effort is set per model, in the app.
Generation always runs on the server, never from the browser.

## Install from a release

1. Download `web_canvas-<version>.tar.gz` and its `.sha256` from
   [Releases](https://github.com/hongdp/web-canvas/releases):
   ```bash
   sha256sum -c web_canvas-1.2.0.tar.gz.sha256
   tar xzf web_canvas-1.3.0.tar.gz && cd web_canvas-1.3.0
   ```
2. Install the server's dependencies (a virtual environment is recommended):
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Start it — one process serves the app and its API:
   ```bash
   python3 scripts/api_server.py --serve-dist --port 8080 --storage-dir ./storage
   ```
4. Open <http://localhost:8080>, create an account, and add your API keys
   under **Settings**. Keys are stored with your account on the server.

Optional, for the model's web search and page reading:
`pip install playwright && python -m playwright install chromium`.

### On Windows

1. Download `WebCanvas-Setup-<version>.exe` (and its `.sha256`, to check it
   with `Get-FileHash`) from
   [Releases](https://github.com/hongdp/web-canvas/releases) and run it.
   It installs for your user only, under `%LOCALAPPDATA%\Programs\WebCanvas`;
   no administrator rights are needed. Windows SmartScreen may warn that the
   installer is unsigned: choose **More info → Run anyway**.
2. Start **Web Canvas** from the Start menu. A console window opens (it is the
   server: close it to stop Web Canvas) and the app opens in your browser at
   `http://127.0.0.1:3000` (or the next free port).
3. Create an account and add your API keys under **Settings**.

Your books, accounts and settings live in `%LOCALAPPDATA%\WebCanvas`
(`storage\` and `db\`). Uninstalling leaves them there; delete the folder to
remove them too. The model's web search and page reading are not available in
the Windows build (they need Playwright's Chromium); everything else is.

### From other devices

Sessions use `Secure` cookies, which browsers accept over HTTPS or on
`localhost` only. To use the server from a phone or another computer, serve
HTTPS — with your own certificate, or a self-signed one:

```bash
# List every host name / IP you will browse to under [alt] in certs/san.cnf first.
openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/server-key.pem \
  -out certs/server-cert.pem -days 825 -config certs/san.cnf
python3 scripts/api_server.py --serve-dist --host 0.0.0.0 --port 8443 \
  --ssl-certfile certs/server-cert.pem --ssl-keyfile certs/server-key.pem
```

Or put it behind a reverse proxy that terminates TLS. Running as a service,
upgrading, logs and troubleshooting: [docs/deployment.md](docs/deployment.md).

### Server options

| Option | Default | |
|---|---|---|
| `--storage-dir DIR` | `$VITE_STORAGE_DIR`, else `./storage` | Books, accounts and sessions |
| `--host` / `--port` | `127.0.0.1` / `3000` | Where to listen |
| `--serve-dist [DIR]` | off (`dist/` when given bare) | Also serve the built app |
| `--ssl-certfile` / `--ssl-keyfile` | none | Serve HTTPS |

The metadata database lives in `.local_db/metadata.db` in the install
directory; keep it on a local disk, not a network mount.

## Develop from source

Needs **Node.js 20+** and **Python 3.10+**.

```bash
git clone https://github.com/hongdp/web-canvas.git
cd web-canvas
npm ci                                  # also wires the pre-push test hook
pip install -r requirements-dev.txt
cp .env.example .env                    # optional defaults (see below)
npm run dev                             # API on :3000 + Vite on :5173 (proxying /api)
```

`npm run dev -- --storage-dir /path/to/storage --host` uses another storage
directory and listens on the network. The dev server uses HTTPS when
`certs/dev-cert.pem` and `certs/dev-key.pem` exist, plain HTTP otherwise.

`.env` holds optional defaults for a new user's settings (`VITE_*_MODEL`,
`VITE_*_BASE_URL`, `VITE_STORAGE_DIR`). A `VITE_*_API_KEY` there is built into
the frontend bundle, so set keys in the app's Settings for anything that is
not your own machine — and never share a bundle built with keys in `.env`
(`npm run release` refuses to).

| Command | |
|---|---|
| `npm run dev` | Full stack with hot reload |
| `npm test` | Frontend tests (Vitest) |
| `cd scripts && python3 -m pytest -q` | Backend tests, incl. the TypeScript↔Python parity suite |
| `npm run lint` · `npm run build` | ESLint · type-check and build `dist/` |
| `npm start` | Serve `dist/` and the API from one process |
| `npm run release` | Build `release/web_canvas-<version>.tar.gz` from `HEAD` |

**Stack:** React 19 · TypeScript · Vite · Zustand · TipTap/ProseMirror on the
front; Python · FastAPI · Uvicorn · SQLite on the back. The agent loop runs
only on the server, in Python (`scripts/wc_agent`); the pure text logic the
frontend also runs exists in TypeScript and Python, and a parity suite keeps
the two byte-identical.

## Documentation

- [docs/design.md](docs/design.md) — architecture and the Decision Log: why
  each thing is the way it is, with the measurements behind it.
- [docs/features/](docs/features/) — one spec per feature: the
  [agentic loop](docs/features/agentic_chat_loop.md),
  [pinned context](docs/features/pinned_context.md),
  [cache continuity](docs/features/cache_continuity.md),
  [server-side runs](docs/features/backend_authority.md),
  [attachments and the web](docs/features/attachments_and_web.md),
  [read and list](docs/features/read_and_list.md).
- [docs/deployment.md](docs/deployment.md) — service, upgrade, HTTPS, logs.
- [CHANGELOG.md](CHANGELOG.md).
- Working with AI assistants on this codebase: [CLAUDE.md](CLAUDE.md) and
  [SKILL.md](SKILL.md).

## Contributing

Issues and pull requests are welcome. Before a PR: `npm test`, the backend
suite, `npm run lint`, `npm run build` — the pre-push hook runs the tests for
you. The agent loop lives only in `scripts/wc_agent` (it runs on the
server); a change to text logic that has both a TypeScript and a Python copy
goes into both, with the parity fixtures regenerated
(`npm run parity:fixtures`). Register a design change in the Decision Log in
`docs/design.md`.

## License

[GNU General Public License v3.0](LICENSE).

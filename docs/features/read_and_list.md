# read and list: one way to look at the book

Status: built 2026-10-10. Registered in the Decision Log of `docs/design.md`.
Replaces `read_chapter`, `analyze_book` and `list_chapters`
(agentic_chat_loop.md §0.11 and the D7 analyze_book row). `grep` stays a
tool of its own (it searches), and so does `web_read` (a browser, user
decision).

## 1. Why

Two tools read the book. `read_chapter` put text into the conversation,
capped; `analyze_book` read any amount in batches with separate model
calls and returned notes. The model had to choose between them before it
knew how much it was about to read, and in a live run it chose
`analyze_book` for a whole long attachment
when the task needed only chapters 10–20. And nothing listed what could be read:
`list_chapters` listed chapters only, and the request's ATTACHMENTS index
stops at 80 lines, so for an attachment of hundreds of chapters the model
grepped for headings three steps in a row before it could read anything.

## 2. `read`

Arguments: what to read — `chapters` (chapter numbers or titles, `A1` for
an attachment) or `parts` (up to 12 of `{chapter, paragraphs, section}`) —
with `paragraphs` / `section` / `format` as before, plus `task` and
`confirmed`. No chapters and a task: the whole book.

- **It fits** (every part within the per-chapter cap, the call within its
  cap, attachments within the turn's budget): the text comes back, as
  `read_chapter` returned it. With a `task` too: the text, since the model
  can do the task itself — no extra model call.
- **It does not fit, no `task`**: the first part with where to continue, as
  before, plus the price of the rest: "the rest is ≈N tokens; pass task=…
  to have it read in K batches and get notes". The model sees the cost
  before anything is spent.
- **It does not fit, with a `task`**: read in batches outside the
  conversation, notes back (what `analyze_book` did: sized under each
  price line, and past 200,000 estimated input tokens only with
  `confirmed: true` after asking the user). Chapter ranges are honored as
  well as attachment ranges.

## 3. `list`

- **No arguments**: every chapter with its number, size and summary (what
  `list_chapters` returned, with the same "nothing changed" notes), then
  each attachment: reference, name, size, number of sections.
- **`source: "A1"`**: the attachment's sections with their ¶ spans, 200 per
  call, continued with `from`; `section` ("第60–90章") or `paragraphs`
  narrow it. The model reads a range by those numbers next.
- **`source: "3"`**: a chapter's headings with their ¶ numbers.

## 4. Old names

`read_chapter`, `analyze_book` and `list_chapters` stay as aliases in the
registry: a call by an old name — from history, or a model's habit — runs
the new tool with the same arguments. Only the new names are offered.
Every prompt text, tool description and result note that named the old
tools names the new ones.

## 5. Both loops

`src/agent` and `scripts/wc_agent` change together; the list rendering is a
pure function in `utils/attachments` / `wc_text.attachments` with parity
cases.

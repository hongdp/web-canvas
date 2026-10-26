/**
 * A chapter as numbered paragraphs — prose's equivalent of a source file's
 * lines (agentic_chat_loop.md, "Paragraphs as lines").
 *
 * Coding agents read files with line numbers (Claude Code's Read prints
 * `cat -n`; Codex reads ranges with `sed -n`) so a model can cite a place,
 * grep to it and read only around it. A novel has no meaningful lines; its
 * unit is the paragraph, i.e. a top-level block of the chapter's HTML. The
 * numbering is 1-based and counts every non-empty block, headings and images
 * included, so a number means the same thing to grep, read_chapter and the
 * user.
 */
import { headChars } from './unicode'

export interface ChapterParagraph {
  /** 1-based position among the chapter's paragraphs. */
  number: number
  /** The block's own HTML, exactly as stored. */
  html: string
  /** Its plain text ('' for an image). */
  text: string
  /** `item`: one entry of a list — numbered on its own (see paragraphBlocks). */
  kind: 'paragraph' | 'heading' | 'image' | 'item' | 'other'
  /**
   * A text paragraph that also carries an image (inline, at its end). Shown
   * as a trailing "[image]" in the text read and searchable by that word: an
   * image inside a paragraph used to vanish from the text view, so a model
   * that grepped for it reported the image lost (2026-10-11).
   */
  image?: boolean
}

const decodeEntities = (s: string) => s
  .replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')

/** A block's plain text: tags dropped, list items and line breaks kept apart. */
export const blockText = (html: string) => decodeEntities(
  html
    .replace(/<(?:br|hr)\s*\/?>/gi, '\n')
    .replace(/<\/li>/gi, '\n')
    .replace(/<[^>]+>/g, '')
).replace(/[ \t]+\n/g, '\n').trim()

const IMAGE_RE = /<img\b|\{\{IMAGE_PLACEHOLDER_\d+\}\}/i

/** Top-level blocks of a chapter's HTML, in order; loose text becomes a `<p>`. */
export function topLevelBlocks(html: string): string[] {
  const doc = new DOMParser().parseFromString(`<body>${html}</body>`, 'text/html')
  return [...doc.body.childNodes].flatMap(node => {
    if (node.nodeType === Node.ELEMENT_NODE) return [(node as Element).outerHTML]
    const text = node.textContent?.trim()
    return text ? [`<p>${text}</p>`] : []
  })
}

/**
 * The blocks the numbering counts: the top-level blocks, except that a list
 * counts each of its items.
 *
 * Problem: a character card keeps each person's fields as one <ul>, which a
 *   text read showed as ONE paragraph; a model replaced that paragraph with
 *   one <p>, flattening every field into a line.
 * Fix: number each <li> of a top-level list on its own, so an entry is read,
 *   grepped and edited as itself (applyParagraphEdits keeps it an <li>).
 *   topLevelBlocks stays whole-list: polishing and pending changes cut the
 *   chapter into blocks and put them back.
 */
export function paragraphBlocks(html: string): string[] {
  const doc = new DOMParser().parseFromString(`<body>${html}</body>`, 'text/html')
  const out: string[] = []
  const add = (node: ChildNode) => {
    if (node.nodeType === Node.ELEMENT_NODE) { out.push((node as Element).outerHTML); return }
    const text = node.textContent?.trim()
    if (text) out.push(`<p>${text}</p>`)
  }
  for (const node of [...doc.body.childNodes]) {
    if (node.nodeType === Node.ELEMENT_NODE && /^(UL|OL)$/i.test((node as Element).tagName)) [...node.childNodes].forEach(add)
    else add(node)
  }
  return out
}

export function chapterParagraphs(html: string): ChapterParagraph[] {
  let number = 0
  return paragraphBlocks(html).flatMap(block => {
    // An image placeholder token is an image, not words.
    const text = blockText(block).replace(/\{\{IMAGE_PLACEHOLDER_\d+\}\}/g, '').trim()
    const image = IMAGE_RE.test(block)
    if (!text && !image) return []
    number++
    const kind: ChapterParagraph['kind'] = /^<h[1-6]\b/i.test(block) ? 'heading'
      : image && !text ? 'image'
      : /^<p\b/i.test(block) ? 'paragraph'
      : /^<li\b/i.test(block) ? 'item'
      : 'other'
    return [{ number, html: block, text, kind, ...(image && text ? { image: true } : {}) }]
  })
}

/**
 * A chapter's length the way the read tools state it: the characters of its
 * paragraphs' text. Write results use the same count, so a model can compare
 * what it wrote with what it read ("¶869–¶933 (3585 characters)").
 */
export const chapterChars = (html: string) =>
  chapterParagraphs(html).reduce((sum, p) => sum + p.text.length, 0)

/** One numbered line of a text-format read: "¶12 …", headings marked "#", list entries "•". */
export function numberedLine(p: ChapterParagraph): string {
  if (p.kind === 'image') return `¶${p.number} [image]`
  return `¶${p.number} ${p.kind === 'heading' ? '# ' : p.kind === 'item' ? '• ' : ''}${searchableText(p).replace(/\n+/g, ' / ')}`
}

/** A paragraph's text as grep searches it and the read shows it: with "[image]" when it carries one. */
export function searchableText(p: Pick<ChapterParagraph, 'text' | 'kind' | 'image'>): string {
  if (p.kind === 'image') return '[image]'
  return p.image ? `${p.text} [image]` : p.text
}

/** The image tags of a block, to keep when its text is replaced. */
export function blockImages(html: string): string[] {
  return html.match(/<img\b[^>]*>|\{\{IMAGE_PLACEHOLDER_\d+\}\}/gi) ?? []
}

// ── Paragraphs by position (agentic_chat_loop.md §0.11) ─────────────────────

/** A top-level block of a chapter's HTML, located in the string itself. */
export interface ParagraphSpan {
  /** Where the block starts and ends in the HTML (end exclusive). */
  start: number
  end: number
  /** The block's bytes, exactly as stored. */
  html: string
  /** Text outside any element (DOMParser makes it a paragraph of its own). */
  loose: boolean
}

const VOID_TAGS = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'])

/** The index just past the `>` that closes the tag opening at `from`, quotes respected; -1 when it never closes. */
function tagEnd(html: string, from: number): number {
  let quote: string | null = null
  for (let i = from + 1; i < html.length; i++) {
    const ch = html[i]
    if (quote) { if (ch === quote) quote = null; continue }
    if (ch === '"' || ch === "'") quote = ch
    else if (ch === '>') return i + 1
  }
  return -1
}

/**
 * The chapter's top-level blocks as positions in its HTML — no parsing and
 * re-serializing, so a block cut out and put back is byte for byte what was
 * stored. Comments and stray closing tags are skipped; an element that never
 * closes runs to the end. `numberedParagraphSpans` checks the result against
 * the DOM's reading before anything relies on it.
 */
export function paragraphSpans(html: string): ParagraphSpan[] {
  const out: ParagraphSpan[] = []
  let i = 0
  while (i < html.length) {
    const ch = html[i]
    if (ch !== '<') {
      const next = html.indexOf('<', i)
      const end = next === -1 ? html.length : next
      if (html.slice(i, end).trim()) out.push({ start: i, end, html: html.slice(i, end), loose: true })
      i = end
      continue
    }
    if (html.startsWith('<!--', i)) {
      const close = html.indexOf('-->', i + 4)
      i = close === -1 ? html.length : close + 3
      continue
    }
    const open = /^<([a-zA-Z][\w-]*)/.exec(html.slice(i, i + 40))
    const closeAt = tagEnd(html, i)
    if (!open || closeAt === -1) {
      // A stray `</x>` or a `<` that opens nothing: not a block.
      i = closeAt === -1 ? html.length : closeAt
      continue
    }
    const name = open[1].toLowerCase()
    if (VOID_TAGS.has(name) || html[closeAt - 2] === '/') {
      out.push({ start: i, end: closeAt, html: html.slice(i, closeAt), loose: false })
      i = closeAt
      continue
    }
    // Nesting of the same element counts; others cannot close it.
    const re = new RegExp(`<(/?)${name}(?=[\\s/>])`, 'gi')
    re.lastIndex = closeAt
    let depth = 1
    let end = html.length
    let m: RegExpExecArray | null
    while ((m = re.exec(html)) !== null) {
      const at = tagEnd(html, m.index)
      if (at === -1) break
      if (m[1]) depth--
      else if (html[at - 2] !== '/') depth++
      if (depth === 0) { end = at; break }
      re.lastIndex = at
    }
    if (name === 'ul' || name === 'ol') {
      // A list counts its items (paragraphBlocks): their spans, inside it.
      const closing = new RegExp(`</${name}\\s*>$`, 'i').exec(html.slice(i, end))
      const innerEnd = closing ? end - closing[0].length : end
      for (const s of paragraphSpans(html.slice(closeAt, innerEnd))) {
        out.push({ ...s, start: s.start + closeAt, end: s.end + closeAt })
      }
      i = end
      continue
    }
    out.push({ start: i, end, html: html.slice(i, end), loose: false })
    i = end
  }
  return out
}

export interface NumberedParagraphSpan extends ParagraphSpan {
  number: number
  text: string
  kind: ChapterParagraph['kind']
}

/**
 * The numbered paragraphs with their positions — or null when the position
 * reading and the DOM reading (`chapterParagraphs`, what read_chapter and
 * grep number) disagree, so nothing is ever cut at a wrong place.
 */
export function numberedParagraphSpans(html: string): NumberedParagraphSpan[] | null {
  const numbered = chapterParagraphs(html)
  const spans = paragraphSpans(html)
  const out: NumberedParagraphSpan[] = []
  for (const span of spans) {
    const block = span.loose ? `<p>${span.html.trim()}</p>` : span.html
    const text = blockText(block).replace(/\{\{IMAGE_PLACEHOLDER_\d+\}\}/g, '').trim()
    if (!text && !IMAGE_RE.test(block)) continue
    const p = numbered[out.length]
    if (!p || p.text !== text) return null
    out.push({ ...span, number: p.number, text, kind: p.kind })
  }
  return out.length === numbered.length ? out : null
}

const PLAIN_BLOCK_RE = /^<(p|h[1-6])>(?:[^<]|<br\s*\/?>)*<\/\1>$/i

/**
 * A chapter whose text view IS its HTML: every block a bare <p> or heading
 * holding only text and line breaks — nothing a text read would hide (an
 * image, a tag inside, an attribute, a list). Such a chapter may be rewritten
 * by a model that has seen its whole text (agentic_chat_loop.md §0.11).
 */
export function isPlainChapterHtml(html: string): boolean {
  const spans = paragraphSpans(html)
  if (numberedParagraphSpans(html) === null) return false
  return spans.every(s => !s.loose && PLAIN_BLOCK_RE.test(s.html.trim()))
}

/** A bare <p> holding images and nothing else (whitespace and line breaks aside). */
const IMAGE_ONLY_BLOCK_RE = /^<p>(?:\s|<br\s*\/?>)*(?:<img\b[^>]*>(?:\s|<br\s*\/?>)*)+<\/p>$/i

/**
 * A chapter a model may rewrite whole from its text read: plain, or plain but
 * for paragraphs that hold only images. The rewrite cannot carry those images
 * (the text read shows them as [image]), and does not need to: the write puts
 * back every image it dropped near where it stood (reinsertMissingImages).
 * Problem: a text read of a chapter ending in a paragraph of three pictures
 *   was refused as "not read yet" AFTER the model had written the whole
 *   chapter (the rewrite thrown away), then read its HTML and wrote it
 *   again (2026-10-12).
 */
export function isTextRewritableHtml(html: string): boolean {
  const spans = paragraphSpans(html)
  if (numberedParagraphSpans(html) === null) return false
  return spans.every(s => !s.loose && (PLAIN_BLOCK_RE.test(s.html.trim()) || IMAGE_ONLY_BLOCK_RE.test(s.html.trim())))
}

/** Paragraphs that are only the text read's [image] markers: what a rewrite from that read may copy, never text. */
export function stripImageMarkerParagraphs(html: string): string {
  return html.replace(/<p>(?:\s|\[image\])*\[image\](?:\s|\[image\])*<\/p>/g, '')
}

export type ParagraphAction = 'replace' | 'insert_before' | 'insert_after' | 'delete'

export interface ParagraphEdit {
  paragraph: number
  action: ParagraphAction
  /** New blocks for replace and inserts; bare text becomes paragraphs. */
  html?: string
  /** The first characters of the paragraph's text as the model saw it: the anchor. */
  startsWith: string
}

export type ParagraphEditOutcome =
  | { ok: true; html: string; paragraphsBefore: number; paragraphsAfter: number; firstChanged: number
      /** Edits applied, and the ones left out with why (an edit that no longer fits is skipped, not fatal). */
      applied: number; skipped: string[]; stale: Array<{ number: number; text: string }> }
  | { ok: false; error: string; stale: Array<{ number: number; text: string }> }

const ACTIONS: ParagraphAction[] = ['replace', 'insert_before', 'insert_after', 'delete']
const squash = (s: string) => s.replace(/\s+/g, ' ').trim()
/** The anchor as the model may copy it from a text read: "¶12 # Title" is "Title", "¶4 • 身份…" is "身份…". */
const anchorOf = (s: string) => squash(s).replace(/^¶\d+\s*/, '').replace(/^#\s+/, '').replace(/^•\s*/, '')

/** Edit distance between two short strings (anchors are a sentence at most). */
function levenshtein(a: string, b: string): number {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    const cur = [i]
    for (let j = 1; j <= b.length; j++) cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
    prev = cur
  }
  return prev[b.length]
}

/**
 * Whether a paragraph starts with the anchor the model gave — allowing a
 * slip or two in a long one. A model copying Chinese lines writes a rare
 * character as a look-alike now and then (four of six anchors in one call
 * had such a slip), and an anchor is there to catch a paragraph that MOVED,
 * which a one-character difference does not mean.
 */
export function anchorMatches(text: string, anchor: string): boolean {
  // The anchor loses a leading "•" (anchorOf: the text read's list marker), so
  // a paragraph whose own text starts with one is compared without it too.
  // Problem: an outline whose list entries begin with a typed "•" read as
  //   "¶33 • • 第八章 渡口…"; every anchor the model gave lost its "•" and none
  //   matched "• 第八章…" — four edits refused in a row (2026-10-13).
  const own = squash(text)
  const bare = own.replace(/^•\s*/, '')
  if (bare !== own && anchorMatches(bare, anchor)) return true
  const t = own
  if (t.startsWith(anchor)) return true
  const k = anchor.length >= 16 ? 2 : anchor.length >= 6 ? 1 : 0
  for (let len = anchor.length - k; k > 0 && len <= anchor.length + k; len++) {
    if (len > 0 && len <= t.length && levenshtein(anchor, t.slice(0, len)) <= k) return true
  }
  return false
}
const BLOCK_HTML_RE = /^\s*<(?:p|h[1-6]|blockquote|ul|ol|div|hr|figure|img|pre|table)\b/i

/** New content as blocks: HTML as it is, bare text as one paragraph per blank-line-separated part. */
export function asBlocks(html: string): string {
  if (BLOCK_HTML_RE.test(html)) return html.trim()
  return html.split(/\n\s*\n/).map(part => part.trim()).filter(Boolean)
    .map(part => `<p>${part.replace(/\n/g, '<br>')}</p>`).join('')
}

/** New content beside or in place of a list entry: still an entry (`<li>`), whatever was sent. */
export function asItems(html: string): string {
  return /^\s*<li\b/i.test(html) ? html.trim() : `<li>${asBlocks(html)}</li>`
}

/**
 * Apply edits addressed by paragraph number (edit_paragraphs). Each edit is
 * checked on its own: one whose anchor no longer matches (allowing a slip,
 * anchorMatches), whose number is past the end, that replaces an image, or
 * that replaces a paragraph twice is left out and reported; the rest apply.
 * Problem: all-or-nothing threw away two good edits for four mistyped
 *   anchors after a 3½-minute step.
 * Numbers are the chapter's before the call; splices go back to front so
 * none moves another, and everything else keeps its bytes. A list entry
 * stays a list entry (asItems).
 */
export function applyParagraphEdits(html: string, edits: ParagraphEdit[]): ParagraphEditOutcome {
  const spans = numberedParagraphSpans(html)
  if (spans === null) return { ok: false, error: 'this chapter\'s HTML could not be split into paragraphs reliably; use edit_document with SEARCH text from its HTML instead', stale: [] }
  if (edits.length === 0) return { ok: false, error: 'no edits were given', stale: [] }
  const stale: Array<{ number: number; text: string }> = []
  const errors: string[] = []
  const removed = new Set<number>()
  const valid: ParagraphEdit[] = []
  for (const e of edits) {
    const span = spans[e.paragraph - 1]
    if (!ACTIONS.includes(e.action)) { errors.push(`¶${e.paragraph}: unknown action "${e.action}"`); continue }
    if (!Number.isInteger(e.paragraph) || !span) { errors.push(`¶${e.paragraph}: the chapter has ${spans.length} paragraphs`); continue }
    if ((e.action === 'replace' || e.action.startsWith('insert')) && !(e.html ?? '').trim()) { errors.push(`¶${e.paragraph}: ${e.action} needs html`); continue }
    if (!anchorOf(e.startsWith)) { errors.push(`¶${e.paragraph}: starts_with is empty — give the first words of the paragraph as you read them`); continue }
    if (!anchorMatches(span.text, anchorOf(e.startsWith)) && !(span.kind === 'image' && /^\[?image\]?$/i.test(anchorOf(e.startsWith)))) {
      stale.push({ number: span.number, text: span.kind === 'image' ? '[image]' : span.text })
      continue
    }
    if (e.action === 'replace' && span.kind === 'image') { errors.push(`¶${e.paragraph} is an image: delete it, or insert next to it`); continue }
    if (e.action === 'replace' || e.action === 'delete') {
      if (removed.has(e.paragraph)) { errors.push(`¶${e.paragraph} is replaced or deleted twice`); continue }
      removed.add(e.paragraph)
    }
    valid.push(e)
  }
  const skipped = [
    ...errors,
    ...stale.map(s => `¶${s.number} does not start with what you gave; it now reads: ${s.text.length > 160 ? `${headChars(s.text, 160)}…` : s.text}`)
  ]
  if (valid.length === 0) return { ok: false, error: `nothing was applied:\n${skipped.join('\n')}`, stale }
  // Splices back to front. At one position a later paragraph's operation is
  // applied first, and within a paragraph its replacement before its
  // insert_before, so the result reads in paragraph order.
  const rank: Record<ParagraphAction, number> = { insert_after: 0, replace: 1, delete: 1, insert_before: 2 }
  const splices = valid.map((e, order) => {
    const span = spans[e.paragraph - 1]
    const at = e.action === 'insert_after' ? span.end : span.start
    const to = e.action === 'replace' || e.action === 'delete' ? span.end : at
    let text = e.action === 'delete' ? '' : span.kind === 'item' ? asItems(e.html ?? '') : asBlocks(e.html ?? '')
    // A replaced paragraph's inline image stays with the new text: the model
    // rewrites words, and the picture was never in the words it saw.
    const images = e.action === 'replace' ? blockImages(html.slice(span.start, span.end)) : []
    if (images.length > 0 && blockImages(text).length === 0) {
      const closing = /(<\/(?:p|h[1-6]|li|blockquote|div)>)\s*$/i
      text = closing.test(text) ? text.replace(closing, `${images.join('')}$1`) : `${text}<p>${images.join('')}</p>`
    }
    return { at, to, text, paragraph: e.paragraph, rank: rank[e.action], order }
  }).sort((a, b) => b.at - a.at || b.paragraph - a.paragraph || a.rank - b.rank || b.order - a.order)
  let out = html
  for (const s of splices) out = out.slice(0, s.at) + s.text + out.slice(s.to)
  return {
    ok: true,
    html: out,
    paragraphsBefore: spans.length,
    paragraphsAfter: chapterParagraphs(out).length,
    firstChanged: Math.min(...valid.map(e => e.paragraph)),
    applied: valid.length,
    skipped,
    stale
  }
}

/**
 * A whole rewrite keeps at least this share of a chapter's text unless the
 * model insists (a long timeline came back much shorter, with a range of
 * chapters folded into one line). Measured over earlier whole rewrites:
 * nearly all kept 95% or more; the ones below were a chapter split into
 * several, which a second send still allows.
 */
export const REWRITE_KEEP_RATIO = 0.85

export interface RewriteLoss { before: number; after: number; lostHeadings: number; lostItems: number }

/** What a whole rewrite would drop, or null when it keeps the chapter's text and structure. */
export function rewriteLoss(oldHtml: string, newHtml: string): RewriteLoss | null {
  const before = chapterChars(oldHtml)
  if (before < 200) return null
  const after = chapterChars(newHtml)
  const count = (html: string, re: RegExp) => (html.match(re) || []).length
  const lostHeadings = Math.max(0, count(oldHtml, /<h[1-6][\s>]/gi) - count(newHtml, /<h[1-6][\s>]/gi))
  const lostItems = Math.max(0, count(oldHtml, /<li[\s>]/gi) - count(newHtml, /<li[\s>]/gi))
  if (after >= before * REWRITE_KEEP_RATIO && lostHeadings === 0 && lostItems < 2) return null
  return { before, after, lostHeadings, lostItems }
}

/** What the model is told when a rewrite is held back for what it would drop. */
export function rewriteLossNote(cite: string, loss: RewriteLoss): string {
  const pct = Math.round((1 - loss.after / loss.before) * 100)
  const dropped = [
    loss.lostHeadings > 0 ? `${loss.lostHeadings} heading${loss.lostHeadings === 1 ? '' : 's'}` : '',
    loss.lostItems > 0 ? `${loss.lostItems} list item${loss.lostItems === 1 ? '' : 's'}` : ''
  ].filter(Boolean)
  return `The rewrite of ${cite} was NOT applied: it would take the chapter from ${loss.before} to ${loss.after} characters` +
    (pct > 0 ? ` (−${pct}%)` : '') + (dropped.length > 0 ? `, dropping ${dropped.join(' and ')}` : '') + '. ' +
    'A whole rewrite replaces everything, so whatever it leaves out is deleted. ' +
    'To add to the chapter or bring it up to date, keep its text and change only what changes: edit_paragraphs (by ¶ number) or edit_document / <edit> blocks. ' +
    'If the user asked for it to be shorter, or its text moved to other chapters, send the same rewrite again and it will be applied.'
}

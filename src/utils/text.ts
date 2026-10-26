/**
 * Text utility functions.
 * Pure helpers for text processing — no React dependencies.
 */
import { diffHtml } from './diff'

/**
 * Generate a timestamp-based unique ID with the given prefix.
 */
export function getTimestampId(prefix: string): string {
  return `${prefix}-${Date.now()}`
}

/**
 * Strip an incomplete `</selection_replace>` suffix from streamed LLM text.
 * This handles the case where the stream is interrupted mid-tag.
 */
export function stripIncompleteEndTag(text: string): string {
  const target = '</selection_replace>'
  for (let i = target.length; i > 0; i--) {
    const prefix = target.substring(0, i)
    if (text.endsWith(prefix)) {
      return text.substring(0, text.length - prefix.length)
    }
  }
  return text
}

/**
 * Result of extracting an XML-like tagged block (e.g. `<canvas>...</canvas>`)
 * from a raw LLM response.
 */
export interface TaggedBlock {
  /** An opening tag was found. */
  found: boolean
  /** A matching closing tag was found (false ⇒ the stream was likely truncated). */
  closed: boolean
  /** Content between the tags (partial if `closed` is false). */
  inner: string
  /** Text before the opening tag. */
  before: string
  /** Text after the closing tag (empty if `closed` is false). */
  after: string
  /** The `chapter="…"` attribute of the opening tag, if any (agentic loop, spec D2). */
  chapter?: string
  /** The `new_chapter="…"` attribute: the block creates a chapter of that title. */
  newChapter?: string
}

/**
 * The `chapter="…"` attribute of an opening tag, if present. Single or double
 * quotes; an empty value counts as absent (the active chapter).
 */
export function chapterAttribute(openingTag: string): string | undefined {
  const m = /\bchapter\s*=\s*(?:"([^"]*)"|'([^']*)')/i.exec(openingTag)
  const value = (m?.[1] ?? m?.[2] ?? '').trim()
  return value || undefined
}

/**
 * The `new_chapter="…"` attribute of a `<canvas>` opening tag: the title of a
 * chapter the block creates and fills. (`\bchapter` above never matches
 * inside it: `_c` is not a word boundary.)
 */
export function newChapterAttribute(openingTag: string): string | undefined {
  const m = /\bnew_chapter\s*=\s*(?:"([^"]*)"|'([^']*)')/i.exec(openingTag)
  const value = (m?.[1] ?? m?.[2] ?? '').trim()
  return value || undefined
}

/**
 * Robustly extract the first `<tag>...</tag>` block from an LLM response.
 *
 * Unlike a plain `indexOf('<tag>')`, this tolerates the real-world variations
 * models emit: case differences (`<Canvas>`), attributes (`<canvas foo="bar">`),
 * and whitespace in the closing tag (`</canvas >`). It also surrounds an
 * optional ```html / ``` markdown code fence so a fenced block still parses.
 *
 * Crucially it reports whether the closing tag was actually seen, so callers
 * can refuse to apply a destructive document replacement built from a
 * truncated (cut-off) response.
 */
export function extractTaggedBlock(text: string, tag: string): TaggedBlock {
  const openRe = new RegExp(`<${tag}(?:\\s[^>]*)?>`, 'i')
  const openMatch = openRe.exec(text)
  if (!openMatch) {
    return { found: false, closed: false, inner: '', before: text, after: '' }
  }

  const chapter = chapterAttribute(openMatch[0])
  const newChapter = newChapterAttribute(openMatch[0])
  const named = { ...(chapter ? { chapter } : {}), ...(newChapter ? { newChapter } : {}) }
  const before = text.substring(0, openMatch.index)
  const rest = text.substring(openMatch.index + openMatch[0].length)

  const closeRe = new RegExp(`</${tag}\\s*>`, 'i')
  const closeMatch = closeRe.exec(rest)
  if (!closeMatch) {
    return { found: true, closed: false, inner: rest, before, after: '', ...named }
  }

  const inner = rest.substring(0, closeMatch.index)
  const after = rest.substring(closeMatch.index + closeMatch[0].length)

  // Strip a wrapping markdown code fence around the inner HTML, if present.
  const fenced = inner.match(/^\s*```(?:html)?\s*([\s\S]*?)\s*```\s*$/i)
  return {
    found: true,
    closed: true,
    inner: fenced ? fenced[1] : inner,
    before,
    after,
    ...named
  }
}

/**
 * Detect explicit "elision" / lazy-omission markers in an LLM-produced document
 * replacement. When asked to re-emit a long document, models often abbreviate
 * unchanged regions with placeholders like `<!-- rest unchanged -->` or
 * `[content continues]`. Diffing the original against such output silently
 * deletes everything that was elided, so callers should refuse to apply it.
 *
 * Only flags EXPLICIT omission language (keywords inside comments / brackets /
 * parentheses) or a whole-paragraph ellipsis — never a bare "..." inside prose,
 * which is legitimate in fiction.
 */
export function hasElisionMarkers(html: string): boolean {
  const keyword = '(?:unchanged|omitted|omit|continues?|rest of (?:the |your )?(?:document|text|content|chapter)|remains? (?:the )?same|same as (?:before|above|previous)|as before|truncat\\w*|abbreviat\\w*|previous content|earlier content)'

  // 1. HTML comment containing omission language: <!-- ... rest unchanged -->
  if (new RegExp(`<!--[\\s\\S]*?${keyword}[\\s\\S]*?-->`, 'i').test(html)) {
    return true
  }
  // 2. Bracketed placeholder: [content continues], [unchanged], [...]
  if (new RegExp(`\\[[^\\]]{0,60}?${keyword}[^\\]]{0,40}?\\]`, 'i').test(html)) {
    return true
  }
  // 3. Parenthetical placeholder: (rest of the document remains the same)
  if (new RegExp(`\\([^)]{0,60}?${keyword}[^)]{0,40}?\\)`, 'i').test(html)) {
    return true
  }
  // 4. A paragraph whose entire content is just an ellipsis.
  if (/<p>\s*(?:\.\.\.|…)\s*<\/p>/i.test(html)) {
    return true
  }
  /*
   * 5–8. The same shortcuts in Chinese. A long timeline came back much
   *   shorter, with a range of chapters folded into one line such as
   *   "第1–9章：见原条目。", and the check let it through. Fiction says
   *   "保持不变" and "略微" too, so the short markers count only inside
   *   brackets, after a colon, or as a whole paragraph; the reference
   *   phrases ("见原条目", "此处省略") count anywhere.
   */
  if (ZH_ELISION_REFERENCE.test(html)) return true
  if (/[（(【[]\s*(?:略|同上|不变|保持不变|未变|省略|下同|内容不变|此处略)[^）)】\]]{0,20}[）)】\]]/.test(html)) return true
  if (/<p[^>]*>\s*(?:略|同上|下略|从略|……略|…略)[。.]?\s*<\/p>/.test(html)) return true
  if (/[:：]\s*(?:同上|略|不变|保持不变|见上|未变)[。.；;]?\s*(?=<|$)/.test(html)) return true
  return false
}

/** Chinese phrases that point at text instead of writing it (hasElisionMarkers 5). */
const ZH_ELISION_REFERENCE = /见原(?:条目|文|稿|版|内容)|见上文|见前文|见上一版|此处省略|以下省略|其余省略|余下省略|中间省略|与(?:前文|原文|上文|原稿|之前)(?:相同|一致)|其余(?:内容)?不变|其余同上/

/**
 * Validate a full-document (`<canvas>`) replacement before applying it as a diff.
 *
 * Returns a machine-readable reason string when the replacement looks unsafe to
 * apply (and would likely destroy content), or `null` when it is safe.
 *
 * - `'truncated'` — the closing tag never arrived; the response was cut off.
 * - `'elided'`    — the output abbreviates unchanged regions with placeholders.
 */
export function validateCanvasReplacement(
  newHtml: string,
  closingTagFound: boolean
): 'truncated' | 'elided' | null {
  if (!closingTagFound) return 'truncated'
  if (hasElisionMarkers(newHtml)) return 'elided'
  return null
}

/** A single localized search/replace edit emitted by the LLM. */
export interface EditBlock {
  /** Exact text from the current document to locate. */
  search: string
  /** Text to substitute in its place (may be empty for a deletion). */
  replace: string
  /**
   * The chapter named by the enclosing `<edit chapter="…">`, if any. Absent
   * = the active chapter, which is all the protocol could target before the
   * agentic loop.
   */
  chapter?: string
}

/** Result of parsing `<edit>` / conflict-marker blocks from an LLM response. */
export interface ParsedEdits {
  blocks: EditBlock[]
  /** Chat text before the edit region. */
  before: string
  /** Chat text after the edit region. */
  after: string
}

// Opening marker of one Aider-style conflict block.
const EDIT_SEARCH_RE = /<{5,}\s*SEARCH[^\n]*\n/gi
// Divider between the SEARCH and REPLACE halves.
const EDIT_DIVIDER_RE = /\n={3,}[^\n]*\n/
// Anything that legitimately ends a REPLACE half. Models finish a block in
// several ways: the canonical marker, a closing </edit> tag, or by starting
// the next block. Accepting only the canonical one meant a whole edit response
// was reclassified as plain chat — raw markup dumped into the chat while the
// document stayed untouched, with no warning (the reply was too long to look
// like an empty acknowledgement).
const EDIT_TERMINATOR_RE = /\n?>{5,}\s*REPLACE[^\n]*|\n?<\/edits?\s*>|\n<{5,}\s*SEARCH/i

/**
 * Parse localized edit blocks from an LLM response.
 *
 * This is the parser for Method A (search/replace edits): rather than re-emit
 * the whole document, the model emits only the changed regions as
 * SEARCH/REPLACE pairs. Parsing is lenient — conflict markers are matched
 * whether or not they are wrapped in `<edit>` tags, the block may end at
 * `>>>>>>> REPLACE`, at `</edit>`, or where the next block begins, and the
 * surrounding `<edit>`/`<edits>` sugar is stripped from the returned chat text.
 *
 * A block whose REPLACE half runs to the end of the response with NO
 * terminator is dropped: that is a cut-off stream, and applying half a
 * replacement would silently truncate the document.
 */
export function parseEditBlocks(text: string): ParsedEdits {
  const blocks: EditBlock[] = []
  let firstStart = -1
  let lastEnd = -1
  // The chapter of the <edit …> wrapper we are inside, tracked across the
  // gaps between blocks: one wrapper may hold several blocks.
  let currentChapter: string | undefined
  let scannedTo = 0

  EDIT_SEARCH_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = EDIT_SEARCH_RE.exec(text)) !== null) {
    const gap = text.slice(scannedTo, m.index)
    for (const tag of gap.match(/<\/?edits?\b[^>]*>/gi) ?? []) {
      currentChapter = tag.startsWith('</') ? undefined : chapterAttribute(tag)
    }
    const searchStart = m.index + m[0].length
    const rest = text.slice(searchStart)

    const divider = EDIT_DIVIDER_RE.exec(rest)
    if (!divider) continue

    const search = rest.slice(0, divider.index)
    const afterDividerStart = divider.index + divider[0].length
    const afterDivider = rest.slice(afterDividerStart)

    const terminator = EDIT_TERMINATOR_RE.exec(afterDivider)
    if (!terminator) continue

    if (search.trim()) {
      const block: EditBlock = { search, replace: afterDivider.slice(0, terminator.index) }
      if (currentChapter) block.chapter = currentChapter
      blocks.push(block)
    }
    if (firstStart === -1) firstStart = m.index
    // When the terminator IS the next block's SEARCH marker, stop short of it
    // so the following iteration still sees it.
    const startsNextBlock = /SEARCH/i.test(terminator[0])
    lastEnd = searchStart + afterDividerStart +
      (startsNextBlock ? terminator.index : terminator.index + terminator[0].length)
    if (/<\/edits?/i.test(terminator[0])) currentChapter = undefined
    scannedTo = lastEnd
    EDIT_SEARCH_RE.lastIndex = lastEnd
  }

  if (blocks.length === 0) {
    return { blocks, before: '', after: '' }
  }

  const stripSugar = (s: string) => s.replace(/<\/?edits?(?:\s[^>]*)?>/gi, '').trim()
  return {
    blocks,
    before: stripSugar(text.slice(0, firstStart)),
    after: stripSugar(text.slice(lastEnd))
  }
}

/**
 * A completed LLM response classified into the action the client must take.
 * Priority (mirrors the Canvas Markup Protocol): selection_replace >
 * localized edits > full-document canvas > plain chat.
 */
export interface ParsedAssistantResponse {
  kind: 'selection' | 'edits' | 'canvas' | 'chat'
  /** Conversational text outside the action tags (before + after joined). */
  chatText: string
  /** kind === 'selection': replacement text for the user's selection. */
  selectionText: string
  /** kind === 'edits': the parsed SEARCH/REPLACE blocks. */
  editBlocks: EditBlock[]
  /** kind === 'canvas': the full replacement document HTML. */
  canvasText: string
  /** kind === 'canvas': whether the closing tag arrived (guards truncation). */
  canvasClosed: boolean
  /** kind === 'canvas': the chapter its `chapter="…"` attribute names, if any. */
  canvasChapter?: string
  /** kind === 'canvas': the title its `new_chapter="…"` attribute creates, if any. */
  canvasNewChapter?: string
  /**
   * Further `<canvas chapter="…">` blocks — full rewrites of OTHER chapters
   * written in the same reply (agentic loop, spec D2) — and, when every
   * `<edit>` of the reply names another chapter, the attribute-less canvas
   * that rewrites the active one (`chapter` absent). A second attribute-less
   * canvas is still stray, as it always was.
   */
  extraCanvases: { text: string; closed: boolean; chapter?: string; newChapter?: string }[]
  /**
   * Document-markup regions removed from `chatText` because no channel took
   * them (a second channel's block, or markup too broken to parse). Never
   * shown raw — the bubble reports the count instead.
   */
  strayMarkup: number
}

/**
 * Classify a COMPLETE streamed response into the action to perform. Pure —
 * the caller decides how to apply the action (diffing, editor transactions,
 * warnings).
 */
// Document markup that must never reach the chat bubble. Order matters: an
// <edit> wrapper contains conflict markers, so wrappers are removed first.
const STRAY_MARKUP_PATTERNS: RegExp[] = [
  /<edits?(?:\s[^>]*)?>[\s\S]*?(?:<\/edits?>|$)/gi,
  /<{5,}\s*SEARCH[\s\S]*?(?:>{5,}\s*REPLACE[^\n]*|$)/gi,
  /<canvas(?:\s[^>]*)?>[\s\S]*?(?:<\/canvas>|$)/gi,
  /<selection_replace>[\s\S]*?(?:<\/selection_replace>|$)/gi
]
const LONE_MARKUP_TAG_RE = /<\/?(?:edits?|canvas|selection_replace)(?:\s[^>]*)?>/gi

/**
 * Remove document markup that no channel consumed, so it can never be shown
 * as chat. Returns the cleaned text and how many regions were removed.
 */
export function stripStrayDocumentMarkup(text: string): { text: string; removed: number } {
  let removed = 0
  let out = text
  for (const re of STRAY_MARKUP_PATTERNS) {
    out = out.replace(re, () => {
      removed++
      return '\n\n'
    })
  }
  out = out.replace(LONE_MARKUP_TAG_RE, '')
  return { text: out.replace(/\n{3,}/g, '\n\n').trim(), removed }
}

/**
 * Pull every `<canvas chapter="…">` and `<canvas new_chapter="…">` block out
 * of `text`. Attribute-less
 * canvases are left in place for the stray pass. A block with no closing tag
 * is taken as unclosed (truncated) and ends the scan.
 */
function takeChapterCanvases(text: string): {
  blocks: { text: string; closed: boolean; chapter?: string; newChapter?: string }[]
  rest: string
} {
  const blocks: { text: string; closed: boolean; chapter?: string; newChapter?: string }[] = []
  let rest = text
  const openRe = /<canvas\s[^>]*>/gi
  let m: RegExpExecArray | null
  openRe.lastIndex = 0
  while ((m = openRe.exec(rest)) !== null) {
    const chapter = chapterAttribute(m[0])
    const newChapter = newChapterAttribute(m[0])
    if (!chapter && !newChapter) continue
    const after = rest.slice(m.index + m[0].length)
    const close = /<\/canvas\s*>/i.exec(after)
    const inner = close ? after.slice(0, close.index) : after
    const fenced = inner.match(/^\s*```(?:html)?\s*([\s\S]*?)\s*```\s*$/i)
    blocks.push({
      text: fenced ? fenced[1] : inner,
      closed: !!close,
      ...(newChapter ? { newChapter } : { chapter })
    })
    const tail = close ? after.slice(close.index + close[0].length) : ''
    rest = (rest.slice(0, m.index).trim() + '\n\n' + tail.trim()).trim()
    openRe.lastIndex = 0
    if (!close) break
  }
  return { blocks, rest }
}

export function parseAssistantResponse(fullText: string): ParsedAssistantResponse {
  // The status trailer is protocol; it never reaches the bubble or the doc.
  fullText = stripDocStatus(fullText)
  const result: ParsedAssistantResponse = {
    kind: 'chat',
    chatText: fullText,
    selectionText: '',
    editBlocks: [],
    canvasText: '',
    canvasClosed: false,
    extraCanvases: [],
    strayMarkup: 0
  }

  const joinAround = (before: string, after: string): string => {
    let text = before.trim()
    if (after.trim()) {
      text += (text ? '\n\n' : '') + after.trim()
    }
    return text
  }

  const selectionBlock = extractTaggedBlock(fullText, 'selection_replace')
  const parsedEdits = parseEditBlocks(fullText)
  const canvasBlock = extractTaggedBlock(fullText, 'canvas')

  if (selectionBlock.found) {
    result.kind = 'selection'
    result.selectionText = selectionBlock.inner
    // Problem: a selection rewrite that came with an <edit> for text OUTSIDE
    //   the selection showed that edit raw in the chat bubble, unapplied.
    // Root Cause: the channels were exclusive — once <selection_replace> was
    //   found, everything around it was chat. The protocol forbids putting
    //   surrounding text in the tag, so a model that also smooths what follows
    //   has no other way to say it.
    // Fix: parse edits out of what surrounds the selection; the hook applies
    //   them after the selection (applyEditBlocksLocally).
    const rest = joinAround(selectionBlock.before, selectionBlock.after)
    const restEdits = parseEditBlocks(rest)
    result.editBlocks = restEdits.blocks
    result.chatText = restEdits.blocks.length > 0 ? joinAround(restEdits.before, restEdits.after) : rest
  } else if (parsedEdits.blocks.length > 0) {
    result.kind = 'edits'
    result.editBlocks = parsedEdits.blocks
    result.chatText = joinAround(parsedEdits.before, parsedEdits.after)
  } else if (canvasBlock.found) {
    result.kind = 'canvas'
    result.canvasText = canvasBlock.inner
    result.canvasClosed = canvasBlock.closed
    if (canvasBlock.newChapter) result.canvasNewChapter = canvasBlock.newChapter
    else if (canvasBlock.chapter) result.canvasChapter = canvasBlock.chapter
    result.chatText = joinAround(canvasBlock.before, canvasBlock.after)
  }

  // Rewrites of other chapters ride along with whichever channel led.
  const chapterCanvases = takeChapterCanvases(result.chatText)
  result.extraCanvases = chapterCanvases.blocks
  result.chatText = chapterCanvases.rest

  /*
   * Problem: edits won over a canvas, so a reply that rewrote the active
   *   chapter AND edited the outline (`<edit chapter="1">`) lost the rewrite
   *   as stray — silently: the model had to read the chapter to find its
   *   text missing, and wrote all 4k characters again (2026-10-06).
   * Fix: the channels only collide on the same chapter. When every edit
   *   names a chapter, the attribute-less canvas rewrites the active one
   *   alongside them. An unnamed edit may target the active chapter, so then
   *   the old precedence stands (and the run reports what was dropped).
   */
  if (result.kind === 'edits' && result.editBlocks.every(b => !!b.chapter)) {
    const own = extractTaggedBlock(result.chatText, 'canvas')
    if (own.found && !own.chapter && !own.newChapter) {
      result.extraCanvases.push({ text: own.inner, closed: own.closed })
      result.chatText = joinAround(own.before, own.after)
    }
  }

  // Whatever no channel took is dropped from the bubble and counted.
  const stray = stripStrayDocumentMarkup(result.chatText)
  result.chatText = stray.text
  result.strayMarkup = stray.removed
  return result
}

/** Result of applying a list of edit blocks to a document. */
export interface ApplyEditsResult {
  /** The document after all matched edits were applied. */
  html: string
  /** Edits whose SEARCH text could not be located (left unapplied). */
  failed: EditBlock[]
  /**
   * Of `failed`: edits that WERE located but change text still under review
   * in a way that cannot be applied (deleted text, across a pending change's
   * edge). Saying "not found" for these sends a model re-reading and
   * re-copying a SEARCH that was right all along.
   */
  underReview?: EditBlock[]
}

/**
 * Build a regex pattern from `search` where characters the LLM commonly
 * normalizes are matched as equivalence classes instead of literally:
 * whitespace runs ⇔ `&nbsp;`, straight ⇔ curly quotes, `&` ⇔ `&amp;`.
 * Everything else is escaped and matched exactly (tags included).
 */
function buildFuzzyPattern(search: string): string {
  let out = ''
  let i = 0
  const isWs = (idx: number) => /\s/.test(search[idx]) || search.startsWith('&nbsp;', idx)
  while (i < search.length) {
    if (isWs(i)) {
      out += '(?:\\s|&nbsp;)+'
      while (i < search.length && isWs(i)) i += search.startsWith('&nbsp;', i) ? 6 : 1
      continue
    }
    const ch = search[i]
    if (ch === "'" || ch === '‘' || ch === '’' || search.startsWith('&#39;', i) || search.startsWith('&apos;', i)) {
      out += "(?:'|‘|’|&#39;|&apos;)"
      i += search.startsWith('&#39;', i) ? 5 : search.startsWith('&apos;', i) ? 6 : 1
      continue
    }
    if (ch === '"' || ch === '“' || ch === '”' || search.startsWith('&quot;', i)) {
      out += '(?:"|“|”|&quot;)'
      i += search.startsWith('&quot;', i) ? 6 : 1
      continue
    }
    if (ch === '&') {
      out += '(?:&amp;|&)'
      i += search.startsWith('&amp;', i) ? 5 : 1
      continue
    }
    out += ch.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    i++
  }
  return out
}

/**
 * Reduce an HTML fragment to comparable plain text: tags stripped, common
 * entities decoded, quotes straightened, whitespace collapsed. Used for the
 * last-resort block-level text match.
 */
export function htmlToComparableText(html: string): string {
  return html
    .replace(/<[^>]*>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&#39;|&apos;|[‘’]/gi, "'")
    .replace(/&quot;|[“”]/gi, '"')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/\s+/g, ' ')
    .trim()
}

// Closing tags that terminate a top-level block, for the block-text fallback.
const EDIT_BLOCK_SPLIT_RE = /<\/(?:p|h[1-6]|blockquote|pre|ul|ol|table|figure|div)>/gi

/**
 * Every contiguous run of whole blocks whose text, as `normalize` reads it,
 * equals the SEARCH's — at most `limit` of them, in document order.
 */
function blockTextRuns(
  haystack: string,
  search: string,
  normalize: (html: string) => string,
  limit: number
): Array<{ start: number; end: number }> {
  const searchText = normalize(search)
  if (!searchText) return []

  // Split the haystack into block segments, each ending at a closing block tag.
  const segments: { start: number; end: number; text: string }[] = []
  EDIT_BLOCK_SPLIT_RE.lastIndex = 0
  let segStart = 0
  let m: RegExpExecArray | null
  while ((m = EDIT_BLOCK_SPLIT_RE.exec(haystack)) !== null) {
    const end = m.index + m[0].length
    segments.push({ start: segStart, end, text: normalize(haystack.slice(segStart, end)) })
    segStart = end
  }
  if (segStart < haystack.length) {
    segments.push({ start: segStart, end: haystack.length, text: normalize(haystack.slice(segStart)) })
  }

  const runs: Array<{ start: number; end: number }> = []
  for (let i = 0; i < segments.length && runs.length < limit; i++) {
    if (!segments[i].text) continue
    let acc = ''
    for (let j = i; j < segments.length; j++) {
      if (segments[j].text) acc = acc ? acc + ' ' + segments[j].text : segments[j].text
      if (acc === searchText) {
        runs.push({ start: segments[i].start, end: segments[j].end })
        break
      }
      if (acc.length > searchText.length) break
    }
  }
  return runs
}

/** Comparable text with every quote mark removed, not just straightened. */
export function quoteBlindText(html: string): string {
  return htmlToComparableText(html).replace(/["'“”‘’「」『』]/g, '').replace(/\s+/g, ' ').trim()
}

/**
 * Last-resort match: if the SEARCH's *plain text* equals the plain text of a
 * contiguous run of whole blocks (paragraphs/headings/lists), replace those
 * whole blocks. This survives the model dropping or altering inline tags
 * (<strong>, <em>, attributes) in its SEARCH copy, and can never produce
 * unbalanced HTML because only complete blocks are swapped.
 *
 * Problem: a 31-paragraph SEARCH that was verbatim except for four closing
 *   quotes (”) the model left out was skipped whole — "1 suggested change
 *   could not be located". The quotes were balanced in the document; the copy
 *   simply dropped them, near the end of each paragraph.
 * Root Cause: quotes were treated as an equivalence class (“ ” " are one
 *   character), which covers a SUBSTITUTED quote but not a MISSING one.
 * Fix: a second pass that ignores quote marks entirely. Still whole blocks
 *   only, and only when exactly ONE run matches — it is the more lenient pass,
 *   so an ambiguous hit is left unapplied rather than guessed.
 */
function replaceByBlockText(haystack: string, search: string, replace: string): string | null {
  const splice = (run: { start: number; end: number }) => haystack.slice(0, run.start) + replace + haystack.slice(run.end)
  const exact = blockTextRuns(haystack, search, htmlToComparableText, 1)
  if (exact.length > 0) return splice(exact[0])
  const quoteBlind = blockTextRuns(haystack, search, quoteBlindText, 2)
  return quoteBlind.length === 1 ? splice(quoteBlind[0]) : null
}

// Shortest excerpt, in comparable characters, the excerpt level will place.
// A short fragment recurs too easily for a heuristic to know which was meant.
const MIN_EXCERPT_CHARS = 8
// Window used to spot a REPLACE that re-states text left over in the block.
const DUPLICATION_WINDOW = 10
// Outer wrapper a model puts around SEARCH/REPLACE text.
const OUTER_OPEN_RE = /^<(p|h[1-6])(?:\s[^>]*)?>/i
const OUTER_CLOSE_RE = /<\/(p|h[1-6])>$/i
// Block boundaries, for measuring what is left of the block around a match.
const BLOCK_OPEN_RE = /<(?:p|h[1-6]|li|blockquote)(?:\s[^>]*)?>/gi
const BLOCK_CLOSE_RE = /<\/(?:p|h[1-6]|li|blockquote)>/i
const VOID_TAGS = new Set(['br', 'img', 'hr', 'input', 'meta', 'link', 'source', 'wbr', 'col', 'area', 'base', 'embed', 'param', 'track'])

/** True when every non-void tag in `html` is closed, in order. */
function isBalancedHtml(html: string): boolean {
  const stack: string[] = []
  const tagRe = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(\/?)>/g
  let m: RegExpExecArray | null
  while ((m = tagRe.exec(html)) !== null) {
    const name = m[2].toLowerCase()
    if (VOID_TAGS.has(name) || m[3] === '/') continue
    if (m[1]) {
      if (stack.pop() !== name) return false
    } else {
      stack.push(name)
    }
  }
  return stack.length === 0
}

/** True when `index` falls inside a tag (`<a title="…|…">`), not in text. */
function isInsideTag(html: string, index: number): boolean {
  if (index <= 0) return false
  return html.lastIndexOf('<', index - 1) > html.lastIndexOf('>', index - 1)
}

/** Every place `needle` occurs: exact occurrences, or failing that, fuzzy ones. */
function findAllMatches(haystack: string, needle: string): Array<{ start: number; end: number }> {
  const exact: Array<{ start: number; end: number }> = []
  for (let i = haystack.indexOf(needle); i !== -1; i = haystack.indexOf(needle, i + 1)) {
    exact.push({ start: i, end: i + needle.length })
  }
  if (exact.length > 0) return exact
  try {
    const re = new RegExp(buildFuzzyPattern(needle), 'g')
    const fuzzy: Array<{ start: number; end: number }> = []
    let m: RegExpExecArray | null
    while ((m = re.exec(haystack)) !== null) {
      if (m[0].length === 0) { re.lastIndex++; continue }
      fuzzy.push({ start: m.index, end: m.index + m[0].length })
    }
    return fuzzy
  } catch {
    return []
  }
}

/**
 * Excerpt match: SEARCH is wrapped in block tags (`<p>…</p>`) but quotes only
 * PART of the block — the model edited one sentence and put the paragraph's
 * tags around it.
 *
 * Problem: reported as "2 suggested changes could not be located". Replaying
 *   the turn showed both SEARCHes in the document VERBATIM — each was the tail
 *   of a paragraph, wrapped in `<p>…</p>`. Levels 1–4 look for the tags too,
 *   and `<p>tail` is not in the document; level 5 compares whole blocks, and
 *   the text is only part of one. The protocol asks SEARCH to start at a block
 *   boundary, which is likely what invites a `<p>` in front of a sentence.
 * Fix: drop the wrapper from both halves and place the excerpt in its own
 *   span, leaving the rest of the block where it was.
 *
 * Every guard below ends in "leave it unapplied", which is the old outcome:
 * - the excerpt must occur exactly ONCE (two hits: no telling which), and not
 *   inside a tag;
 * - REPLACE must be wrapped the same way, or not at all — anything else would
 *   change the block's type in mid-block;
 * - REPLACE must not re-state text left over in the block: if it does, the
 *   model rewrote the WHOLE block from a misremembered copy, and an in-place
 *   swap would duplicate the rest;
 * - the result must be balanced HTML (a multi-paragraph REPLACE cannot land
 *   inside <strong>).
 */
function replaceByUnwrappedExcerpt(haystack: string, search: string, replace: string): string | null {
  const open = OUTER_OPEN_RE.exec(search)
  const close = OUTER_CLOSE_RE.exec(search)
  if (!open && !close) return null
  const needle = search.slice(open ? open[0].length : 0, close ? close.index : search.length)
  if (htmlToComparableText(needle).length < MIN_EXCERPT_CHARS) return null

  let payload = replace.trim()
  const rOpen = OUTER_OPEN_RE.exec(payload)
  if (rOpen) {
    if (!open || rOpen[1].toLowerCase() !== open[1].toLowerCase()) return null
    payload = payload.slice(rOpen[0].length)
  }
  const rClose = OUTER_CLOSE_RE.exec(payload)
  if (rClose) {
    if (!close || rClose[1].toLowerCase() !== close[1].toLowerCase()) return null
    payload = payload.slice(0, rClose.index)
  }

  const hits = findAllMatches(haystack, needle)
  if (hits.length !== 1) return null
  const { start, end } = hits[0]
  if (isInsideTag(haystack, start)) return null

  // What stays of the enclosing block(s) once the excerpt is swapped out.
  let blockStart = 0
  BLOCK_OPEN_RE.lastIndex = 0
  let bo: RegExpExecArray | null
  while ((bo = BLOCK_OPEN_RE.exec(haystack)) !== null && bo.index < start) {
    blockStart = bo.index + bo[0].length
  }
  const closeAfter = BLOCK_CLOSE_RE.exec(haystack.slice(end))
  const blockEnd = closeAfter ? end + closeAfter.index : haystack.length
  const leftover = htmlToComparableText(haystack.slice(blockStart, start) + ' ' + haystack.slice(end, blockEnd))
  const payloadText = htmlToComparableText(payload)
  for (let i = 0; i + DUPLICATION_WINDOW <= leftover.length; i++) {
    if (payloadText.includes(leftover.slice(i, i + DUPLICATION_WINDOW))) return null
  }

  const result = haystack.slice(0, start) + payload + haystack.slice(end)
  return isBalancedHtml(result) ? result : null
}

/**
 * Locate `search` in `haystack` and return the string with it replaced by
 * `replace`, or `null` if it cannot be found. Tries progressively fuzzier
 * matches so a model that doesn't reproduce the document byte-for-byte still
 * applies: exact ⇒ trimmed ⇒ whitespace-insensitive ⇒ entity/quote-insensitive
 * ⇒ whole-block plain-text match ⇒ excerpt wrapped in tags it does not span.
 */
function applyOneEdit(haystack: string, search: string, replace: string): string | null {
  // 1. Exact substring.
  const exactIdx = haystack.indexOf(search)
  if (exactIdx !== -1) {
    return haystack.slice(0, exactIdx) + replace + haystack.slice(exactIdx + search.length)
  }

  const trimmed = search.trim()
  if (!trimmed) return null

  // 2. Trimmed exact substring.
  const trimmedIdx = haystack.indexOf(trimmed)
  if (trimmedIdx !== -1) {
    return haystack.slice(0, trimmedIdx) + replace + haystack.slice(trimmedIdx + trimmed.length)
  }

  // 3+4. Fuzzy regex: whitespace runs ⇔ &nbsp;, curly ⇔ straight quotes,
  // & ⇔ &amp; — the substitutions LLMs most often make when copying HTML.
  try {
    const re = new RegExp(buildFuzzyPattern(trimmed))
    const match = re.exec(haystack)
    if (match) {
      return haystack.slice(0, match.index) + replace + haystack.slice(match.index + match[0].length)
    }
  } catch {
    // Malformed pattern — fall through to the block-text match.
  }

  // 5. Whole-block plain-text match (tolerates dropped/altered inline tags).
  const byBlock = replaceByBlockText(haystack, trimmed, replace)
  if (byBlock !== null) return byBlock

  // 6. Part of a block, wrapped as if it were the whole block.
  return replaceByUnwrappedExcerpt(haystack, trimmed, replace)
}

/**
 * Apply a list of search/replace edits to a document, sequentially. Each edit
 * runs against the result of the previous one. Edits whose SEARCH text cannot
 * be located are collected in `failed` and skipped rather than applied
 * destructively, so unmatched content is never lost.
 */
export function applyEditBlocks(originalHtml: string, blocks: EditBlock[]): ApplyEditsResult {
  let html = originalHtml
  const failed: EditBlock[] = []
  for (const block of blocks) {
    const result = applyOneEdit(html, block.search, block.replace)
    if (result === null) {
      failed.push(block)
    } else {
      html = result
    }
  }
  return { html, failed }
}

const DIFF_MARKUP_RE = /class="[^"]*diff-(?:addition|deletion)/

/**
 * Split HTML into its top-level nodes, each balanced on its own — a
 * `<ul>…</ul>` stays whole instead of breaking at a `</p>` inside a `<li>`.
 * Concatenating the result gives back the input exactly.
 */
function splitTopLevelNodes(html: string): string[] {
  const nodes: string[] = []
  const tagRe = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(\/?)>/g
  let depth = 0
  let start = 0
  let m: RegExpExecArray | null
  while ((m = tagRe.exec(html)) !== null) {
    const name = m[2].toLowerCase()
    if (VOID_TAGS.has(name) || m[3] === '/') continue
    if (m[1]) {
      depth = Math.max(0, depth - 1)
      if (depth === 0) {
        nodes.push(html.slice(start, tagRe.lastIndex))
        start = tagRe.lastIndex
      }
    } else {
      depth++
    }
  }
  if (start < html.length) nodes.push(html.slice(start))
  return nodes
}

// A pending diff element's opening tag, or any closing </ins>/</del>.
const DIFF_ELEMENT_EDGE_RE = /<(?:ins|del)\b[^>]*class="[^"]*diff-(?:addition|deletion)[^"]*"[^>]*>|<\/(?:ins|del)>/gi

/** The pending diff element `index` lies inside — an insertion, a deletion — or null. */
function pendingDiffAt(html: string, index: number): 'ins' | 'del' | null {
  let inside: 'ins' | 'del' | null = null
  DIFF_ELEMENT_EDGE_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = DIFF_ELEMENT_EDGE_RE.exec(html)) !== null && m.index < index) {
    inside = m[0].startsWith('</') ? null : m[0].startsWith('<ins') ? 'ins' : 'del'
  }
  return inside
}

/**
 * Diff only the characters an edit changed inside `oldPart` → `newPart`. The
 * span is widened so it never cuts a word, a tag or an entity apart. Null when
 * the change overlaps pending diff markup, sits inside a pending deletion, or
 * is not balanced on its own (it splits or joins blocks).
 *
 * A change inside a pending INSERTION is applied to that inserted text as is.
 *   Problem: it used to be refused, to keep a diff from nesting inside a diff
 *     — and reported as "SEARCH not found". A selection rewrite that came out
 *     with a stray English word could not be corrected in the same turn: the
 *     model re-read, re-copied a SEARCH that was right, and failed again
 *     (2026-10-06).
 *   Fix: inserted text is a proposal nobody has accepted yet; correcting it
 *     changes the proposal. The review still shows one diff (confirmed text →
 *     corrected proposal) and reject-all still returns the confirmed text, so
 *     nothing nests.
 */
function diffChangedSpan(oldPart: string, newPart: string): string | null {
  const max = Math.min(oldPart.length, newPart.length)
  let start = 0
  while (start < max && oldPart[start] === newPart[start]) start++
  let tail = 0
  while (tail < max - start && oldPart[oldPart.length - 1 - tail] === newPart[newPart.length - 1 - tail]) tail++

  // Latin text is diffed in whole words, as diffHtml would; CJK has no word
  // boundaries, so a character is the unit there (\w excludes it).
  while (start > 0 && /\w/.test(oldPart[start - 1]) && (/\w/.test(oldPart[start] ?? '') || /\w/.test(newPart[start] ?? ''))) start--
  while (
    tail > 0 && /\w/.test(oldPart[oldPart.length - tail]) &&
    (/\w/.test(oldPart[oldPart.length - tail - 1] ?? '') || /\w/.test(newPart[newPart.length - tail - 1] ?? ''))
  ) tail--
  // Never start inside a tag or an entity: move back to its first character.
  if (oldPart.lastIndexOf('<', start - 1) > oldPart.lastIndexOf('>', start - 1)) start = oldPart.lastIndexOf('<', start - 1)
  const amp = oldPart.lastIndexOf('&', start - 1)
  if (amp !== -1 && /^&[a-zA-Z0-9#]*$/.test(oldPart.slice(amp, start))) start = amp
  // Never end inside one either: the shared suffix must start on a boundary.
  const suffix = oldPart.slice(oldPart.length - tail)
  const gt = suffix.indexOf('>')
  if (gt !== -1 && (suffix.indexOf('<') === -1 || gt < suffix.indexOf('<'))) tail -= gt + 1
  const semi = /^[a-zA-Z0-9#]*;/.exec(oldPart.slice(oldPart.length - tail))
  if (semi && /&[a-zA-Z0-9#]*$/.test(oldPart.slice(0, oldPart.length - tail))) tail -= semi[0].length

  const oldMid = oldPart.slice(start, oldPart.length - tail)
  const newMid = newPart.slice(start, newPart.length - tail)
  if (DIFF_MARKUP_RE.test(oldMid) || DIFF_MARKUP_RE.test(newMid)) return null
  if (!isBalancedHtml(oldMid) || !isBalancedHtml(newMid)) return null
  const inside = pendingDiffAt(oldPart, start)
  if (inside === 'del') return null
  // Balanced and free of diff markup, so it cannot cross the insertion's edge.
  const replacement = inside === 'ins' ? newMid : diffHtml(oldMid, newMid)
  return oldPart.slice(0, start) + replacement + oldPart.slice(oldPart.length - tail)
}

/**
 * Apply edit blocks to a document that already carries pending diff markup,
 * marking up ONLY the top-level blocks each edit changes.
 *
 * The edits path folds every pending diff into its base before diffing, which
 * is right for a NEW turn. Inside one turn it is not: after a selection rewrite
 * has just been placed, folding would mark the freshly rewritten passage as
 * already accepted. Here everything an edit does not touch stays
 * byte-identical — pending hunks and the selection's diff included.
 *
 * Each edit is diffed at the span it changed, not as a whole block.
 *
 * Problem: a block that already carried markup was refused outright, and two
 *   ordinary cases produce one — a selection that ends mid-paragraph leaves its
 *   fresh diff sharing a block with untouched text, and an earlier edit in the
 *   same batch marks its block. Worse, for Chinese a whole-block diff marks the
 *   ENTIRE paragraph (no word boundaries), so a second fix in that paragraph
 *   always landed inside the first one's diff. Both surfaced as "could not be
 *   located" (user-reported: 1 of several continuity edits after a selection
 *   rewrite).
 * Fix: diff only the changed span; refuse only when that span itself touches
 *   pending markup (see diffChangedSpan). A change that splits or joins blocks
 *   is not balanced as a span, and falls back to a whole-block diff where the
 *   block carries no markup.
 */
export function applyEditBlocksLocally(html: string, blocks: EditBlock[]): ApplyEditsResult {
  let current = html
  const failed: EditBlock[] = []
  const underReview: EditBlock[] = []
  for (const block of blocks) {
    const next = applyOneEdit(current, block.search, stripBlankParagraphs(block.replace))
    if (next === null) {
      failed.push(block)
      continue
    }
    const before = splitTopLevelNodes(current)
    const after = splitTopLevelNodes(next)
    let head = 0
    while (head < before.length && head < after.length && before[head] === after[head]) head++
    let tail = 0
    while (
      tail < before.length - head && tail < after.length - head &&
      before[before.length - 1 - tail] === after[after.length - 1 - tail]
    ) tail++
    const oldPart = before.slice(head, before.length - tail).join('')
    const newPart = after.slice(head, after.length - tail).join('')
    const replacement = diffChangedSpan(oldPart, newPart) ?? (DIFF_MARKUP_RE.test(oldPart) ? null : diffHtml(oldPart, newPart))
    if (replacement === null) {
      failed.push(block)
      underReview.push(block)
      continue
    }
    current = before.slice(0, head).join('') + replacement + before.slice(before.length - tail).join('')
  }
  return { html: current, failed, underReview }
}

/**
 * Clean up LLM-generated HTML:
 * 1. Remove blank `<p>` tags that contain only whitespace or &nbsp;.
 * 2. Collapse whitespace (including newlines) between block-level tags
 *    so that `</p>\n<p>` doesn't produce an extra blank line in TipTap.
 */
export function stripBlankParagraphs(html: string): string {
  return html
    .replace(/<p>\s*(<br\s*\/?>)?\s*<\/p>/gi, '')
    .replace(/<p>(\s|&nbsp;)+<\/p>/gi, '')
    .replace(/(<\/(p|h[1-6]|blockquote|ul|ol|li|div)>)\s+(<(p|h[1-6]|blockquote|ul|ol|li|div)[\s>])/gi, '$1$3')
}

/**
 * Convert a blob: URL to a data: URL via fetch + FileReader.
 */
export const convertBlobUrlToDataUrl = async (blobUrl: string): Promise<string> => {
  try {
    const res = await fetch(blobUrl)
    const blob = await res.blob()
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onloadend = () => resolve(reader.result as string)
      reader.onerror = reject
      reader.readAsDataURL(blob)
    })
  } catch (err) {
    console.error('Failed to convert blob URL to data URL:', err)
    return blobUrl
  }
}

/**
 * Convert a GIF data URL to JPEG by drawing the first frame on a canvas.
 * Returns the original URL unchanged if it's not a GIF.
 */
export const convertGifToJpegIfNeeded = (dataUrl: string): Promise<string> => {
  if (!dataUrl.startsWith('data:image/gif')) {
    return Promise.resolve(dataUrl)
  }
  return new Promise<string>((resolve) => {
    const img = new window.Image()
    img.onload = () => {
      try {
        const canvas = document.createElement('canvas')
        canvas.width = img.naturalWidth || img.width
        canvas.height = img.naturalHeight || img.height
        const ctx = canvas.getContext('2d')
        if (!ctx) {
          resolve(dataUrl)
          return
        }
        ctx.drawImage(img, 0, 0)
        const jpegDataUrl = canvas.toDataURL('image/jpeg', 0.9)
        resolve(jpegDataUrl)
      } catch (err) {
        console.error('Error drawing GIF to canvas:', err)
        resolve(dataUrl)
      }
    }
    img.onerror = () => {
      console.error('Error loading GIF image')
      resolve(dataUrl)
    }
    img.src = dataUrl
  })
}

/**
 * Detect a response that claims (or implies) a document change but produced
 * none — the "chat says it wrote the chapter, the document is untouched"
 * failure.
 *
 * Only meaningful when the response carried no action tags at all
 * (`parseAssistantResponse(...).kind === 'chat'`); the caller checks that.
 *
 * Problem / Root Cause / Fix:
 * - Problem: a write request comes back as a one-line acknowledgement with no
 *   <canvas>/<edit>/<selection_replace> tags, so nothing reaches the editor
 *   while the chat bubble reads like a success.
 * - Root cause: tag compliance is probabilistic. Measured against grok-4.5
 *   (2026-07-25, n=37 across conditions), the failure occurs with the system
 *   prompt preset disabled and with no chat history, and the success rate for
 *   an identical prompt drifted between 22% and 65% inside one hour — no
 *   prompt wording moved it beyond the noise.
 * - Fix: recover client-side instead of instructing harder. The caller retries
 *   the turn once with a corrective instruction and, if that also comes back
 *   empty-handed, warns the user rather than reporting success.
 *
 * Deliberately narrow, because a false positive costs an extra LLM call:
 * a genuine chat answer is usually longer, and a clarifying question (the
 * legitimate short reply) ends in a question mark.
 */
/**
 * Phrases where the model asserts it changed the document. Detecting the
 * CLAIM is the point: whether an edit was warranted is the model's call, but
 * "I updated it" with no markup is always a broken turn.
 */
/**
 * The model's own declaration of what it did to the document.
 *
 * Every reply ends with `<doc_status>updated|unchanged</doc_status>` (see
 * systemPrompt.ts). This is the ONLY reliable read on intent: the client
 * cannot know whether a request warranted an edit, and matching prose in two
 * languages only ever approximated it. A declaration that disagrees with the
 * emitted markup is the failure we actually want to catch.
 *
 * Returns null when the model omitted the trailer, which older/smaller models
 * do — callers fall back to the prose heuristic below.
 */
const DOC_STATUS_RE = /<doc_status>\s*(updated|unchanged)\s*<\/doc_status>/i

export type DocStatusDeclaration = 'updated' | 'unchanged'

export function parseDocStatus(fullText: string): DocStatusDeclaration | null {
  const m = DOC_STATUS_RE.exec(fullText || '')
  return m ? (m[1].toLowerCase() as DocStatusDeclaration) : null
}

/**
 * Remove the declaration from text headed for the chat bubble — it is
 * protocol, not something the user should read. Also swallows a trailer that
 * is still arriving, so it does not flicker through the live bubble one
 * character at a time.
 */
const TRAILER_OPEN = '<doc_status>'
const TRAILER_CLOSE = '</doc_status>'

/** Is this trailing fragment the beginning of a declaration and nothing else? */
function isPartialTrailer(tail: string): boolean {
  const t = tail.toLowerCase()
  if (TRAILER_OPEN.startsWith(t)) return true
  const m = /^<doc_status>\s*([a-z]*)(<\/?[a-z_]*)?$/.exec(t)
  if (!m) return false
  const [, word, close] = m
  if (word && !'updated'.startsWith(word) && !'unchanged'.startsWith(word)) return false
  if (close && !TRAILER_CLOSE.startsWith(close)) return false
  return true
}

export function stripDocStatus(text: string): string {
  let out = (text || '').replace(DOC_STATUS_RE, '')
  const lt = out.lastIndexOf('<')
  // A lone '<' is indistinguishable from prose, so it is left alone; it is
  // visible for at most one chunk.
  if (lt !== -1 && lt < out.length - 1 && isPartialTrailer(out.slice(lt))) {
    out = out.slice(0, lt)
  }
  return out.trimEnd()
}

/**
 * First-person claims of having written.
 *
 * Deliberately narrow. It is used ONLY to catch a reply that declares
 * "unchanged" while telling the user it changed something — a model
 * describing what the USER did ("你已经把这段改好了") must stay out of it.
 * The broader prose heuristic this replaced is gone: with the declaration
 * mandatory, an undeclared reply is a protocol failure on its own and no
 * amount of pattern-matching prose is needed to reach that verdict.
 */
const SELF_CLAIM_PATTERNS = [
  /\bi(?:'ve| have)?\s+(?:just\s+)?(?:updated|rewritten|rewrote|revised|edited|expanded|added|inserted|removed|deleted|replaced|continued|drafted)\b/i,
  /\bhere(?:'s| is)\s+the\s+(?:updated|revised|rewritten|new)\b/i,
  /我已(?:经)?[^。！？；\n]{0,10}?(?:更新|改写|重写|修改|润色|扩写|续写|写好|写完|写入|改好|补上|添加|删除|替换)/,
  // The lookbehind is load-bearing: "你已经把第二章改好了" is the model
  // describing the USER's edit, not claiming its own.
  /(?<![你您])已(?:经)?(?:帮你|为你|把|将)[^。！？；\n]{0,10}?(?:更新|改写|重写|修改|润色|扩写|续写|写好|写完|写入|改好|补上|添加|删除|替换)/
]


/**
 * Did this reply FAIL to deliver a document update it should have delivered?
 *
 * Two failure modes, both about the model's own output rather than about what
 * the user asked for:
 *  - 'malformed' — edit markup that the parser rejected. The model tried to
 *    edit and got the shape wrong.
 *  - 'claimed'   — the model states it changed the document while emitting no
 *    document markup at all.
 *
 * Deliberately NOT a guess at user intent. An earlier version retried any
 * short reply, so ordinary conversation ("does this read well?") burned three
 * extra generations and ended in a warning about a failure that never
 * happened. Whether an edit is warranted is the model's call; what the client
 * can judge is whether the model's own claim matches its own output.
 *
 * Callers pass the FULL response text and only ask when no document action
 * was parsed out of it.
 */
export type DocumentUpdateFailure = 'malformed' | 'claimed' | 'undeclared'

/** A first-person claim of having written or changed the book (the harness's claim audit, agentic_chat_loop.md §0.8). */
export function claimsOwnWrite(text: string): boolean {
  return SELF_CLAIM_PATTERNS.some(re => re.test(text || ''))
}

export function detectFailedDocumentUpdate(fullText: string): DocumentUpdateFailure | null {
  const text = (fullText || '').trim()
  if (!text) return null
  if (/<edits?\b|<{5,}\s*SEARCH/i.test(text)) return 'malformed'
  // An unclosed action tag: the model started the markup and never finished.
  if (/<(?:canvas|selection_replace)\b/i.test(text)) return 'malformed'

  const declared = parseDocStatus(text)

  // Declared an update and emitted nothing — the failure this exists for.
  if (declared === 'updated') return 'claimed'

  if (declared === 'unchanged') {
    // Normally authoritative, and deliberately so: it silences prose that only
    // DESCRIBES an edit ("你已经把这段改好了"). But a first-person claim of
    // having written, next to a declaration of having written nothing, is the
    // model contradicting itself — and that contradiction is exactly what the
    // user sees as "it said it wrote and it didn't".
    return SELF_CLAIM_PATTERNS.some(re => re.test(text)) ? 'claimed' : null
  }

  // No declaration at all. The protocol requires one on EVERY reply, including
  // replies that change nothing, precisely so that "no markup" is never
  // ambiguous: without it a model that silently skipped the work is
  // indistinguishable from one that deliberately answered in chat.
  return 'undeclared'
}

/**
 * Drop a trailing partial HTML tag or entity from a mid-stream fragment.
 *
 * Streamed `<canvas>` content is rendered into the editor as it arrives, so
 * the tail is routinely cut mid-token (`<p>The sleek ta`, `<h`, `&nbs`).
 * Feeding that to the DOM parser makes the last element flicker between
 * garbage states; trimming to the last complete construct keeps the live
 * preview stable. Unclosed *elements* are fine — the parser closes them.
 */
export function trimIncompleteHtmlTail(html: string): string {
  let out = html
  const lastLt = out.lastIndexOf('<')
  if (lastLt !== -1 && out.indexOf('>', lastLt) === -1) {
    out = out.slice(0, lastLt)
  }
  const lastAmp = out.lastIndexOf('&')
  if (lastAmp !== -1 && out.indexOf(';', lastAmp) === -1 && out.length - lastAmp <= 10) {
    out = out.slice(0, lastAmp)
  }
  return out
}

/**
 * "Nothing there" as the editor writes it: an empty string, or the empty
 * paragraph ProseMirror keeps because a document must contain one block.
 */
export function isBlankContent(html: string): boolean {
  // Media carries no text but is very much content — a chapter holding only a
  // generated illustration must not read as empty to the blanking guard.
  if (/<(img|video|audio|iframe)\b/i.test(html)) return false
  return !html.replace(/<[^>]+>/g, '').replace(/&nbsp;|\s/g, '')
}

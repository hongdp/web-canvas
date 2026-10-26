interface DiffItem {
  type: 'equal' | 'insert' | 'delete'
  value: string
}

/**
 * Checks if a token is an HTML tag.
 */
function isTag(token: string): boolean {
  return token.startsWith('<') && token.endsWith('>')
}

/**
 * An `<img>` is content, not structure: it is marked like text.
 *
 * Problem: an inserted image was emitted as a bare tag outside any `<ins>`,
 *   so "Reject" kept a picture the model had placed (an image copied from
 *   another chapter stayed after its paragraph was rejected); a deleted image
 *   was dropped like any tag, so it vanished without showing as a deletion and
 *   "Reject" could not bring it back (2026-10-18).
 * Fix: an image token goes inside the group's `<ins>` / `<del>` like a word.
 *   CustomImage is an inline node, so the editor's diff marks cover it and
 *   accept/reject resolve it with the text around it.
 */
function isImageTag(token: string): boolean {
  return /^<img\b/i.test(token)
}

/** A token the review markup carries: text, or an image. */
const isMarkedContent = (token: string): boolean => !isTag(token) || isImageTag(token)

/**
 * Runs the Longest Common Subsequence (LCS) algorithm to find the edits.
 * Since prefix/suffix optimization is applied first, the inputs here are typically small.
 */
function computeLcs(X: string[], Y: string[]): DiffItem[] {
  const m = X.length
  const n = Y.length
  
  // DP Table for LCS lengths
  const L: number[][] = Array(m + 1)
    .fill(0)
    .map(() => Array(n + 1).fill(0))

  for (let i = 0; i <= m; i++) {
    for (let j = 0; j <= n; j++) {
      if (i === 0 || j === 0) {
        L[i][j] = 0
      } else if (X[i - 1] === Y[j - 1]) {
        L[i][j] = L[i - 1][j - 1] + 1
      } else {
        L[i][j] = Math.max(L[i - 1][j], L[i][j - 1])
      }
    }
  }

  // Backtrack to find the diff path
  let i = m
  let j = n
  const result: DiffItem[] = []

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && X[i - 1] === Y[j - 1]) {
      result.push({ type: 'equal', value: X[i - 1] })
      i--
      j--
    } else if (j > 0 && (i === 0 || L[i][j - 1] >= L[i - 1][j])) {
      result.push({ type: 'insert', value: Y[j - 1] })
      j--
    } else {
      result.push({ type: 'delete', value: X[i - 1] })
      i--
    }
  }

  return result.reverse()
}

// Closing tags that end a logical block (paragraph, heading, list item, …).
// We split the token stream on these so the expensive token-level LCS only runs
// on the blocks that actually changed.
const BLOCK_CLOSE_RE = /^<\/(p|h[1-6]|li|blockquote|pre|ul|ol|div|td|th|tr|table|figure|figcaption|section|article|header|footer)>$/i

/**
 * Group a flat token list into block-level chunks (one chunk per paragraph /
 * heading / list item, etc.). Each chunk is the list of tokens up to and
 * including its closing block tag.
 */
function groupIntoBlocks(tokens: string[]): string[][] {
  const blocks: string[][] = []
  let current: string[] = []
  for (const tok of tokens) {
    current.push(tok)
    if (BLOCK_CLOSE_RE.test(tok)) {
      blocks.push(current)
      current = []
    }
  }
  if (current.length) blocks.push(current)
  return blocks
}

// Above this many DP cells the token-level LCS is too expensive to run inline,
// so fall back to a coarse delete-all-then-insert-all diff for that region.
const MAX_LCS_CELLS = 1_500_000

/**
 * Diff two token lists (a single changed region) with prefix/suffix trimming
 * and a capped LCS. Returns token-level DiffItems.
 */
function diffTokens(oldTokens: string[], newTokens: string[]): DiffItem[] {
  let prefix = 0
  while (
    prefix < oldTokens.length &&
    prefix < newTokens.length &&
    oldTokens[prefix] === newTokens[prefix]
  ) {
    prefix++
  }

  let suffix = 0
  while (
    suffix < oldTokens.length - prefix &&
    suffix < newTokens.length - prefix &&
    oldTokens[oldTokens.length - 1 - suffix] === newTokens[newTokens.length - 1 - suffix]
  ) {
    suffix++
  }

  const prefixTokens = oldTokens.slice(0, prefix)
  const suffixTokens = oldTokens.slice(oldTokens.length - suffix)
  const middleOld = oldTokens.slice(prefix, oldTokens.length - suffix)
  const middleNew = newTokens.slice(prefix, newTokens.length - suffix)

  const middleDiff: DiffItem[] =
    middleOld.length * middleNew.length > MAX_LCS_CELLS
      ? [
          ...middleOld.map(value => ({ type: 'delete' as const, value })),
          ...middleNew.map(value => ({ type: 'insert' as const, value }))
        ]
      : computeLcs(middleOld, middleNew)

  return [
    ...prefixTokens.map(value => ({ type: 'equal' as const, value })),
    ...middleDiff,
    ...suffixTokens.map(value => ({ type: 'equal' as const, value }))
  ]
}

/**
 * Diff two HTML strings and render a merged HTML containing <ins> and <del> tags.
 *
 * Two-level diff: an LCS over block-level chunks (cheap — there are far fewer
 * blocks than tokens) identifies which paragraphs/headings actually changed,
 * then a token-level LCS runs only on those. This keeps both the computation
 * and the resulting diff small even for large documents, where a single
 * whole-document token LCS would freeze the UI and produce a giant diff.
 */
export function diffHtml(oldHtml: string, newHtml: string): string {
  // Tokenize: Matches HTML tags (<p>, </h1>), words (Hello, text), or whitespaces (\n, spaces)
  const tokenRegex = /(<[^>]+>|[^\s<]+|\s+)/g
  const oldTokens = oldHtml.match(tokenRegex) || []
  const newTokens = newHtml.match(tokenRegex) || []

  // 1. Block-level LCS to locate the changed blocks.
  const oldBlocks = groupIntoBlocks(oldTokens)
  const newBlocks = groupIntoBlocks(newTokens)
  const blockDiff = computeLcs(
    oldBlocks.map(b => b.join('')),
    newBlocks.map(b => b.join(''))
  )

  // 2. Walk the block diff; emit unchanged blocks verbatim and token-diff each
  //    contiguous run of changed (deleted/inserted) blocks.
  const fullDiff: DiffItem[] = []
  let oi = 0
  let ni = 0
  let pendingOld: string[] = []
  let pendingNew: string[] = []

  const flushPending = () => {
    if (pendingOld.length && pendingNew.length) {
      for (const item of diffTokens(pendingOld, pendingNew)) fullDiff.push(item)
    } else if (pendingOld.length) {
      for (const value of pendingOld) fullDiff.push({ type: 'delete', value })
    } else if (pendingNew.length) {
      for (const value of pendingNew) fullDiff.push({ type: 'insert', value })
    }
    pendingOld = []
    pendingNew = []
  }

  for (const d of blockDiff) {
    if (d.type === 'equal') {
      flushPending()
      for (const value of oldBlocks[oi]) fullDiff.push({ type: 'equal', value })
      oi++
      ni++
    } else if (d.type === 'delete') {
      for (const value of oldBlocks[oi]) pendingOld.push(value)
      oi++
    } else {
      for (const value of newBlocks[ni]) pendingNew.push(value)
      ni++
    }
  }
  flushPending()

  // 5. Render to HTML with custom ins/del wrappers and grouped IDs
  let html = ''
  let idx = 0

  while (idx < fullDiff.length) {
    const item = fullDiff[idx]

    if (item.type === 'equal') {
      html += item.value
      idx++
    } else {
      // Transitioned into a change range -> generate a unique group ID
      const diffId = `diff-${Math.random().toString(36).substring(2, 9)}`
      
      const deletedTokens: string[] = []
      const insertedTokens: string[] = []

      // Collect contiguous deletes and inserts
      while (idx < fullDiff.length && fullDiff[idx].type !== 'equal') {
        const current = fullDiff[idx]
        if (current.type === 'delete') {
          deletedTokens.push(current.value)
        } else if (current.type === 'insert') {
          insertedTokens.push(current.value)
        }
        idx++
      }

      // Format deleted content: text and images only, ignore tags to prevent structural breakages
      const deletedText = deletedTokens
        .filter(isMarkedContent)
        .join('')

      // Format inserted content: output HTML tags as-is and wrap text nodes in <ins>
      let insertedHtml = ''
      let currentInsertText = ''

      const flushInsertText = () => {
        if (currentInsertText) {
          insertedHtml += `<ins class="diff-addition" data-diff-id="${diffId}">${currentInsertText}</ins>`
          currentInsertText = ''
        }
      }

      insertedTokens.forEach(token => {
        if (!isMarkedContent(token)) {
          flushInsertText()
          insertedHtml += token
        } else {
          currentInsertText += token
        }
      })
      flushInsertText()

      // Output deletions (<del>) then insertions (<ins>)
      if (deletedText) {
        html += `<del class="diff-deletion" data-diff-id="${diffId}">${deletedText}</del>`
      }
      if (insertedHtml) {
        html += insertedHtml
      }
    }
  }

  return html
}

/**
 * The document as it currently READS, with review markup removed.
 *
 * A pending diff leaves `<ins class="diff-addition">` and
 * `<del class="diff-deletion">` in the stored content, and that content is
 * what gets sent to the model. Measured from a real turn: the model copied
 * `<ins data-diff-id="diff-abc123" class="diff-addition">` into an
 * `edit_document` search string. The moment the user accepts or rejects that
 * diff the markup is gone, so the search can never match again — a silently
 * failed edit whose cause is invisible from the outside.
 *
 * Insertions are kept (they are what the draft now says) and deletions are
 * dropped, which is exactly the "accepted" reading the user is looking at.
 */
export function stripDiffMarkup(html: string): string {
  if (!html || (!html.includes('diff-addition') && !html.includes('diff-deletion'))) return html || ''
  return html
    .replace(/<del\b[^>]*class="[^"]*diff-deletion[^"]*"[^>]*>[\s\S]*?<\/del>/gi, '')
    .replace(/<ins\b[^>]*class="[^"]*diff-addition[^"]*"[^>]*>([\s\S]*?)<\/ins>/gi, '$1')
}

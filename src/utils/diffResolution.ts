import type { Node as ProseMirrorNode } from '@tiptap/pm/model'

export type DiffAction = 'accept' | 'reject'

export type DiffMarkName = 'diffAddition' | 'diffDeletion'

export interface DiffRange {
  from: number
  to: number
  /** `delete` removes the range outright; `unmark` keeps the text and strips `mark`. */
  op: 'delete' | 'unmark'
  mark: DiffMarkName
}

/**
 * Problem: rejecting a diff that ADDED a whole paragraph left a blank line
 * behind. The reject path only deleted the marked text range, so the `<p>`
 * wrapper the diff had introduced survived as an empty block — the document
 * never returned to its pre-diff bytes. Accepting a deletion had the mirror
 * bug: `diffHtml` drops the deleted block's tags and emits the removed text as
 * a bare `<del>`, which TipTap re-wraps in a paragraph of its own.
 *
 * Root cause: the resolution logic worked purely at text-node granularity and
 * had no notion of a block that exists *only* because of the diff.
 *
 * Fix: before collecting text ranges, find textblocks whose entire content is
 * text carrying the mark this action deletes, and remove those blocks whole —
 * expanding through ancestors (`li` inside `ul`, …) that the removal would
 * leave empty.
 */

/** The mark whose text this action erases; the other one is merely unmarked. */
function deletedMarkFor(action: DiffAction): DiffMarkName {
  return action === 'reject' ? 'diffAddition' : 'diffDeletion'
}

function hasDiffMark(node: ProseMirrorNode, mark: DiffMarkName, diffId?: string): boolean {
  return node.marks.some(
    m => m.type.name === mark && (diffId === undefined || m.attrs['data-diff-id'] === diffId)
  )
}

/**
 * Text, or an inline image: what diffHtml marks (an `<img>` goes inside the
 * `<ins>` / `<del>` like a word, so its node carries the diff mark).
 */
const isMarkable = (node: ProseMirrorNode): boolean => node.isText || (node.isInline && node.isLeaf && node.type.name === 'image')

function otherMark(mark: DiffMarkName): DiffMarkName {
  return mark === 'diffAddition' ? 'diffDeletion' : 'diffAddition'
}

/**
 * True when every bit of visible content in `block` is text carrying `mark`,
 * so erasing that text leaves nothing but an empty block node. Unmarked
 * whitespace between marked runs does not count as content — but text under
 * the opposite mark does, whatever its diff id: this action is about to
 * restore it, and whitespace-only restored text is still content the block
 * must survive to hold.
 */
function isBlockEntirelyMarked(
  block: ProseMirrorNode,
  mark: DiffMarkName,
  diffId?: string
): boolean {
  if (!block.isTextblock || block.content.size === 0) return false

  const kept = otherMark(mark)
  let sawMarked = false
  let onlyMarked = true

  block.forEach(child => {
    if (!onlyMarked) return
    if (!isMarkable(child)) {
      // Another inline node is real content the diff did not add.
      onlyMarked = false
      return
    }
    if (hasDiffMark(child, mark, diffId)) {
      sawMarked = true
    } else if (hasDiffMark(child, kept) || !child.isText || (child.text || '').trim().length > 0) {
      // An unmarked image is content the block must survive to hold.
      onlyMarked = false
    }
  })

  return sawMarked && onlyMarked
}

/**
 * Range to cut so that `block` (at `pos`) disappears without leaving an empty
 * ancestor behind. Returns null when the cut would empty the document itself —
 * `doc` requires `block+`, so the last block stays and is merely cleared.
 */
function removableBlockRange(
  doc: ProseMirrorNode,
  pos: number,
  block: ProseMirrorNode
): { from: number; to: number } | null {
  const $pos = doc.resolve(pos)
  let from = pos
  let to = pos + block.nodeSize

  for (let depth = $pos.depth; depth >= 0; depth--) {
    if ($pos.node(depth).childCount !== 1) break
    if (depth === 0) return null
    from = $pos.before(depth)
    to = $pos.after(depth)
  }

  return { from, to }
}

/**
 * Ranges to apply for accepting or rejecting diff markup, newest position
 * first so a caller can dispatch them in order without remapping.
 *
 * `diffId` restricts the walk to a single hunk (per-hunk accept/reject); omit
 * it to resolve every pending diff in the document.
 */
export function collectDiffRanges(
  doc: ProseMirrorNode,
  action: DiffAction,
  diffId?: string
): DiffRange[] {
  const deletedMark = deletedMarkFor(action)
  const blockRanges: DiffRange[] = []

  doc.descendants((node, pos) => {
    if (!node.isTextblock) return true
    if (!isBlockEntirelyMarked(node, deletedMark, diffId)) return false
    const range = removableBlockRange(doc, pos, node)
    if (range) blockRanges.push({ ...range, op: 'delete', mark: deletedMark })
    return false
  })

  const covered = (from: number, to: number) =>
    blockRanges.some(range => from >= range.from && to <= range.to)

  const textRanges: DiffRange[] = []

  doc.descendants((node, pos) => {
    if (!isMarkable(node)) return true
    const from = pos
    const to = pos + node.nodeSize
    if (covered(from, to)) return false
    for (const mark of node.marks) {
      const name = mark.type.name
      if (name !== 'diffAddition' && name !== 'diffDeletion') continue
      if (diffId !== undefined && mark.attrs['data-diff-id'] !== diffId) continue
      textRanges.push({
        from,
        to,
        op: name === deletedMark ? 'delete' : 'unmark',
        mark: name
      })
    }
    return false
  })

  return [...blockRanges, ...textRanges].sort((a, b) => b.from - a.from)
}

/**
 * String fallback used when no editor instance is mounted. Mirrors
 * `collectDiffRanges`: a block whose only content was the resolved-away markup
 * goes with it, instead of collapsing to an empty `<p>`.
 */
export function resolveDiffMarkupInHtml(html: string, action: DiffAction): string {
  const removedTag = action === 'reject' ? 'ins' : 'del'
  const keptTag = action === 'reject' ? 'del' : 'ins'
  // The element body is tempered rather than lazy (`[\s\S]*?`): a lazy body
  // BACKTRACKS across `</ins>` to satisfy the trailing `</p>`, so
  // `<p><ins>X </ins>A<ins> Y</ins></p>` — the shape `diffHtml` emits for a
  // rewrite that keeps text in the middle — matched as one run and took the
  // original "A" with it.
  const body = `(?:(?!<\\/${removedTag}\\b)[\\s\\S])*`
  const emptiedBlock = new RegExp(
    `<(p|h[1-6]|li|blockquote)\\b[^>]*>\\s*(?:<${removedTag}[^>]*data-diff-id="[^"]*"[^>]*>${body}<\\/${removedTag}>\\s*)+<\\/\\1>`,
    'gi'
  )
  let stripped = html.replace(emptiedBlock, '')
  if (stripped !== html) {
    // A removed `<p>` can leave its list item / quote wrapper hollow.
    let previous = ''
    while (previous !== stripped) {
      previous = stripped
      stripped = stripped.replace(/<(li|ul|ol|blockquote)\b[^>]*>\s*<\/\1>/gi, '')
    }
  }

  return stripped
    .replace(new RegExp(`<${removedTag}[^>]*data-diff-id="[^"]*"[^>]*>[\\s\\S]*?<\\/${removedTag}>`, 'gi'), '')
    .replace(
      new RegExp(`<${keptTag}[^>]*data-diff-id="[^"]*"[^>]*>([\\s\\S]*?)<\\/${keptTag}>`, 'gi'),
      '$1'
    )
}

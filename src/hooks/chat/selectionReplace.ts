/**
 * The one place a streamed selection rewrite touches the editor.
 *
 * Problem: an "Elaborate" on a Ctrl+A selection ended with hundreds of
 *   scrambled characters after the new text — a reversed, sparse sample of
 *   the chapter's own sentences with shards of `{{IMAGE_PLACEHOLDER_N}}`
 *   tokens mixed in. User-reported twice; reproduced from the 2026-09-13
 *   snapshots by replaying the stream through the real hook.
 * Root cause: every preview tick replaced [from, end] with the parsed partial
 *   and then set `end = from + slice.size`. parseSlice returns an OPEN slice,
 *   and ProseMirror inserts exactly slice.size positions only when both ends
 *   of the range sit inside a paragraph. Ctrl+A is an AllSelection whose
 *   `from` is 0 — document level — so the slice's open first paragraph gets
 *   closed with a fresh wrapper and the real insert is slice.size + 2. The
 *   next tick's range stopped two positions short of the previous insert,
 *   leaving its last character behind after the new text; every tick left
 *   one more, each ahead of the last — reversed, sparse, and carrying the
 *   letters of whatever placeholder token a partial had been cut through.
 * Fix: never derive the end from the slice. Ask the transaction where the old
 *   end went: `tr.mapping.map(to, 1)` is the position right after whatever
 *   was inserted, whatever fitting ProseMirror had to do.
 */
import type { Editor } from '@tiptap/core'
import { DOMParser as ProseMirrorDOMParser } from '@tiptap/pm/model'
import type { Transaction } from '@tiptap/pm/state'
import { clampSelectionRange } from './streamHandlers'

/**
 * Marks a preview transaction the store must not learn about.
 *
 * Problem: with a server-side run, every selection rewrite ended in
 *   "Another copy of <chapter> was saved first" and the run's text went to
 *   version history while the editor showed the half-streamed preview.
 * Root cause: the preview writes real transactions, so Editor.tsx's
 *   onUpdate published them to the store, which marked the chapter
 *   unsynced. The server then committed the rewrite and published its
 *   document event — ignored here, because an unsynced chapter is never
 *   overwritten by an event — and this tab's next save sent the preview
 *   with a stale base revision: 409, conflict banner. A tab-local run
 *   relies on exactly that store write (it commits what it previewed), so
 *   the behavior is right there and wrong only when the server writes.
 * Fix: a server run's preview carries this meta, and onUpdate skips the
 *   store for it. The store keeps the chapter the server confirmed, the
 *   server's write arrives through its document event like any other
 *   tab's, and Editor.tsx's content sync then replaces the preview.
 */
export const SILENT_PREVIEW_META = 'silentPreview'

/** Was this transaction a preview the store must ignore? (Editor.tsx's onUpdate.) */
export const isSilentPreview = (transaction: Transaction): boolean => transaction.getMeta(SILENT_PREVIEW_META) === true

/**
 * Replace `[from, to]` with the parsed `html` and return where the inserted
 * content now ends, or null when the range no longer fits the document
 * (chapter switched, document shortened) — in which case nothing is written.
 */
export function replaceSelectionWithHtml(editor: Editor, from: number, to: number, html: string, opts: { silent?: boolean } = {}): number | null {
  const range = clampSelectionRange(from, to, editor.state.doc.content.size)
  if (!range) return null

  const tempDiv = document.createElement('div')
  tempDiv.innerHTML = html
  const slice = ProseMirrorDOMParser.fromSchema(editor.state.schema).parseSlice(tempDiv)

  const tr = editor.state.tr.replace(range.from, range.to, slice)
  if (opts.silent) tr.setMeta(SILENT_PREVIEW_META, true)
  editor.view.dispatch(tr)
  return tr.mapping.map(range.to, 1)
}

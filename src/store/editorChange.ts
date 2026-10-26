import type { CanvasDocument } from '../types/document'
import { titleFollowingHeading } from '../utils/titleSync'
import { useAppStore } from './useAppStore'

/**
 * The editor's own HTML after a real transaction — a keystroke, a paste,
 * Undo, an accepted or rejected hunk — written to its chapter (App's
 * onChange for every mounted Editor).
 *
 * It may leave the chapter blank: that is the user emptying it, and the
 * blanking guard in documentsSlice exempts it (`allowBlank`). The HTML is
 * never a stale base: programmatic content reaches the editor with
 * emitUpdate:false, so it does not come back through here, and the editor is
 * mounted only once the chapter's content has loaded.
 */
export function commitEditorHtml(id: string, html: string): void {
  const { documents, updateDocument } = useAppStore.getState()
  const updates: Partial<CanvasDocument> = { content: html }
  // Title follows a leading <h1> — but only when the HEADING changed in
  // this edit. The unconditional version overwrote a chapter rename on the
  // next body keystroke whenever an h1 was present (any h1 ≠ title lost).
  // Decision logic and the original bounded-regex perf contract (no DOM,
  // no <img> materialization on very large imports) live in utils/titleSync.
  const prevDoc = documents.find(d => d.id === id)
  const followedTitle = titleFollowingHeading(prevDoc?.content ?? '', html, prevDoc?.title)
  if (followedTitle !== null) {
    updates.title = followedTitle
  }
  updateDocument(id, updates, { allowBlank: true })
}

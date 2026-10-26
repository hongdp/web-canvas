import { useCallback } from 'react'
import { Editor } from '@tiptap/react'
import type { CanvasDocument, DocumentWriteOptions } from '../store/useAppStore'
import { collectDiffRanges, resolveDiffMarkupInHtml, type DiffAction } from '../utils/diffResolution'

export function useDiffHandlers(
  activeEditor: Editor | null,
  activeDoc: Pick<CanvasDocument, 'content'>,
  updateActiveDocument: (updates: Partial<CanvasDocument>, options?: DocumentWriteOptions) => void,
  triggerUnsaved: () => void
) {
  const resolveAllDiffs = useCallback(
    (action: DiffAction) => {
      // The user's own review: rejecting a chapter that is all additions (one
      // the agent created) empties it, and that must reach the store
      // (documentsSlice's blanking guard explains the exemption).
      const userAction: DocumentWriteOptions = { allowBlank: true }
      if (activeEditor) {
        const { state, view } = activeEditor
        const tr = state.tr
        // Ranges come back last-position-first, so earlier ones stay valid.
        collectDiffRanges(state.doc, action).forEach(range => {
          if (range.op === 'delete') {
            tr.delete(range.from, range.to)
          } else {
            tr.removeMark(range.from, range.to, state.schema.marks[range.mark])
          }
        })
        view.dispatch(tr)
        updateActiveDocument({ content: activeEditor.getHTML() }, userAction)
      } else {
        updateActiveDocument({ content: resolveDiffMarkupInHtml(activeDoc.content, action) }, userAction)
      }
      triggerUnsaved()
    },
    [activeEditor, activeDoc.content, updateActiveDocument, triggerUnsaved]
  )

  // Accept all additions and finalize all deletions in active document
  const handleAcceptAllDiffs = useCallback(() => resolveAllDiffs('accept'), [resolveAllDiffs])

  // Reject all additions and restore all deleted text in active document
  const handleRejectAllDiffs = useCallback(() => resolveAllDiffs('reject'), [resolveAllDiffs])

  return {
    handleAcceptAllDiffs,
    handleRejectAllDiffs
  }
}

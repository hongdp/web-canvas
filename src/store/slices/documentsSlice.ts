import type { StateCreator } from 'zustand'
import type { CanvasDocument } from '../../types/document'
import type { AppState } from '../types'
import { localStorage, saveDocumentsToIndexedDB } from '../persistence'
import { idsNeedingContent, loadDocumentContents } from '../contentLoader'
import { MOCK_DOCUMENTS } from '../defaults'
import { loadSavedActiveDocId } from '../settingsPersistence'
// Benign import cycle: this module only references useAppStore inside action
// bodies, which run long after both modules have finished evaluating.
import { useAppStore } from '../useAppStore'

// Pure, so it lives with the other HTML helpers; re-exported for the guard's
// existing callers and tests.
import { isBlankContent } from '../../utils/text'
import { CLIENT_ID, CLIENT_ID_HEADER, markSaved, recordServerCopy } from '../documentSync'
export { isBlankContent }

/** How a write to a chapter is checked (see refusesBlanking below). */
export interface DocumentWriteOptions {
  /**
   * The user asked for this content: the editor's own HTML after a real
   * transaction, or Accept / Reject All. It may leave the chapter blank.
   * Programmatic writes (a kept preview, a heading rename) never set it.
   */
  allowBlank?: boolean
}

export interface DocumentsSlice {
  // Multi-document state
  documents: CanvasDocument[]
  activeDocumentId: string

  setActiveDocumentId: (id: string) => void
  /**
   * Append a chapter and return its id. `activate: false` leaves the open
   * chapter alone — the agent creates a chapter first and opens it only when
   * its first write starts (agentic_chat_loop.md D6).
   */
  addDocument: (title?: string, content?: string, options?: { activate?: boolean }) => string
  importAllDocuments: (docs: { title: string; content: string }[]) => void
  reorderDocuments: (newDocs: CanvasDocument[]) => void
  deleteDocument: (id: string) => void
  updateDocument: (id: string, updates: Partial<CanvasDocument>, options?: DocumentWriteOptions) => void
  updateActiveDocument: (updates: Partial<CanvasDocument>, options?: DocumentWriteOptions) => void
  setDocumentPinned: (id: string, pinned: boolean) => void
  /**
   * Ensure the given documents' content is loaded (server books lazy-load
   * metadata-only chapters). Resolves once every needed fetch settles; a
   * failed doc stays unloaded and degrades to its index line. Callers that
   * need chapter content (the read tools) MUST await this first, or an
   * unopened chapter reads as empty.
   */
  ensureDocumentContents: (ids: string[]) => Promise<void>
}

/** Does this update change the chapter itself (its text or title)? */
const changesText = (doc: CanvasDocument, updates: Partial<CanvasDocument>) =>
  (updates.content !== undefined && updates.content !== doc.content) ||
  (updates.title !== undefined && updates.title !== doc.title)

/**
 * Problem: a chapter of prose was replaced with an empty document and
 *   auto-saved over, with no version snapshot to go back to.
 * Root cause: a completion path wrote back its "leave it as it was" base,
 *   and that base had been captured before the chapter's lazy content
 *   finished loading, so it was ''. The write itself looked exactly like a
 *   legitimate one.
 * Fix: blanking a non-empty chapter is never something the app does on its
 *   own, so refuse the write here, where every path converges (both
 *   updateDocument — the agentic loop writes chapters that are not open —
 *   and updateActiveDocument), instead of auditing each caller for a stale
 *   base. This is a backstop, not the cure: the caller that captured an
 *   empty base is fixed too (see the rejoin in useChatLLM), but the next one
 *   like it must not cost a chapter.
 *
 * Problem (2026-10-25): Reject All on a chapter the agent had just created
 *   did nothing. Every block was a pending addition, so rejecting them left
 *   the editor's empty `<p></p>`; the guard refused that write, the store
 *   kept the text, the review banner stayed, and the chapter came back on
 *   the next load. Rejecting the last hunks one by one, or select-all +
 *   delete, was refused the same way.
 * Root cause: the guard assumed a user clearing a chapter "goes through the
 *   editor, which sends its own HTML rather than ''" — but the editor's
 *   empty HTML is blank too.
 * Fix: writes the user asked for pass `allowBlank` and are exempt: the
 *   editor's own HTML after a real transaction (commitEditorHtml — the
 *   editor sets programmatic content with emitUpdate:false and is mounted
 *   only once the chapter has loaded, so its HTML is never a stale base) and
 *   Accept / Reject All (useDiffHandlers). Everything else stays guarded.
 */
const refusesBlanking = (
  doc: CanvasDocument | undefined,
  updates: Partial<CanvasDocument>,
  options: DocumentWriteOptions | undefined
): boolean => {
  if (!doc || options?.allowBlank || updates.content === undefined) return false
  if (!isBlankContent(updates.content) || isBlankContent(doc.content)) return false
  console.error(
    '[documents] Refused to blank a non-empty chapter.',
    { documentId: doc.id, hadChars: doc.content.length }
  )
  return true
}

export const createDocumentsSlice: StateCreator<AppState, [], [], DocumentsSlice> = (set) => {
  const initialDocs = MOCK_DOCUMENTS
  const initialActiveId = loadSavedActiveDocId(initialDocs)

  return {
    // Multi-document state
    documents: initialDocs,
    activeDocumentId: initialActiveId,

    setActiveDocumentId: (id) => {
      localStorage.setItem('web_canvas_active_document_id', id)
      set({ activeDocumentId: id })

      // Lazy-load document content from server if not yet loaded
      void useAppStore.getState().ensureDocumentContents([id])
    },

    ensureDocumentContents: async (ids) => {
      const state = useAppStore.getState()
      if (!state.user || !state.activeBookId) return
      const needed = idsNeedingContent(ids, state.documents)
      if (needed.length === 0) return
      await loadDocumentContents(state.activeBookId, needed, {
        onLoaded: (id, content, revision) => {
          useAppStore.setState((s) => ({
            documents: s.documents.map(d =>
              d.id === id ? { ...d, content, contentLoaded: true, ...(revision !== undefined ? { revision } : {}) } : d
            )
          }))
          // The server's copy: not something to send back (documentSync).
          const loaded = useAppStore.getState().documents.find(d => d.id === id)
          if (loaded) recordServerCopy(id, loaded)
          // Update local cache
          saveDocumentsToIndexedDB(useAppStore.getState().documents, true)
        }
      })
    },

    addDocument: (title = 'New Chapter', content = '<p>Start writing...</p>', options) => {
      const activate = options?.activate !== false
      const newDoc: CanvasDocument = {
        // Suffixed: the agent can create two chapters in one millisecond.
        id: `doc-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        title,
        content,
        contentLoaded: true,
        // Not on the server until its POST lands; kept across a reload
        // until then (documentSync.mergeServerChapters).
        ...(useAppStore.getState().user ? { unsynced: true } : {}),
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      }
      let docId = ''

      set((state) => {
        const updatedDocs = [...state.documents, newDoc]
        saveDocumentsToIndexedDB(updatedDocs, true)
        docId = newDoc.id
        if (!activate) return { documents: updatedDocs }
        localStorage.setItem('web_canvas_active_document_id', newDoc.id)
        return {
          documents: updatedDocs,
          activeDocumentId: newDoc.id
        }
      })

      // Sync new document to server
      const state = useAppStore.getState()
      if (state.user && state.activeBookId) {
        fetch(`/api/books/${state.activeBookId}/documents`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': state.csrfToken || '',
            [CLIENT_ID_HEADER]: CLIENT_ID
          },
          body: JSON.stringify({
            documents: [{ id: newDoc.id, title: newDoc.title, content: newDoc.content, createdAt: newDoc.createdAt, updatedAt: newDoc.updatedAt }]
          })
        }).then(res => res.ok ? res.json() : null)
          .then(body => {
            useAppStore.getState().adoptServerUpdatedAt(body?.updatedAt)
            // Created: the server holds this text at revision 1.
            if (body) markSaved(newDoc.id, newDoc, body.revision ?? 1)
          })
          .catch(e => console.error('Failed to sync new document to server', e))
      }

      return docId
    },

    importAllDocuments: (newDocs) => {
      if (newDocs.length === 0) return

      const formattedDocs: CanvasDocument[] = newDocs.map((doc, idx) => ({
        id: `doc-${Date.now()}-${idx}`,
        title: doc.title || `Chapter ${idx + 1}`,
        content: doc.content || '<p></p>',
        contentLoaded: true,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      }))

      set(() => {
        saveDocumentsToIndexedDB(formattedDocs, true)
        localStorage.setItem('web_canvas_active_document_id', formattedDocs[0].id)
        return {
          documents: formattedDocs,
          activeDocumentId: formattedDocs[0].id
        }
      })
    },

    reorderDocuments: (newDocs) => {
      set(() => {
        saveDocumentsToIndexedDB(newDocs, true)
        return { documents: newDocs }
      })

      // Persist document order to server
      const state = useAppStore.getState()
      if (state.user && state.activeBookId) {
        const docIds = newDocs.map(d => d.id)
        fetch(`/api/books/${state.activeBookId}/documents/reorder`, {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': state.csrfToken || '',
            [CLIENT_ID_HEADER]: CLIENT_ID
          },
          body: JSON.stringify({ documentIds: docIds })
        }).then(res => res.ok ? res.json() : null)
          .then(body => useAppStore.getState().adoptServerUpdatedAt(body?.updatedAt))
          .catch(e => console.error('Failed to persist document order', e))
      }
    },

    deleteDocument: (id) => {
      set((state) => {
        const filteredDocs = state.documents.filter((d) => d.id !== id)

        let newActiveId = state.activeDocumentId
        if (state.activeDocumentId === id) {
          newActiveId = filteredDocs[0]?.id || ''
        }

        // If all docs are deleted, add a fresh fallback doc
        if (filteredDocs.length === 0) {
          const fallbackDoc: CanvasDocument = {
            id: `doc-${Date.now()}`,
            title: 'Chapter 1: Welcome',
            content: '<h1>Getting Started</h1><p>Start writing...</p>',
            contentLoaded: true,
            createdAt: new Date().toISOString(),
            updatedAt: new Date().toISOString(),
          }
          filteredDocs.push(fallbackDoc)
          newActiveId = fallbackDoc.id
        }

        saveDocumentsToIndexedDB(filteredDocs, true)
        localStorage.setItem('web_canvas_active_document_id', newActiveId)

        return {
          documents: filteredDocs,
          activeDocumentId: newActiveId
        }
      })

      // Sync deletion to server
      const state = useAppStore.getState()
      if (state.user && state.activeBookId) {
        fetch(`/api/books/${state.activeBookId}/documents/${id}`, {
          method: 'DELETE',
          headers: {
            'X-CSRF-Token': state.csrfToken || ''
          }
        }).then(res => res.ok ? res.json() : null)
          .then(body => useAppStore.getState().adoptServerUpdatedAt(body?.updatedAt))
          .catch(e => console.error('Failed to delete document on server', e))
      }
    },

    updateDocument: (id, updates, options) => {
      set((state) => {
        if (refusesBlanking(state.documents.find(d => d.id === id), updates, options)) return {}
        const updatedDocs = state.documents.map((d) => {
          if (d.id === id) {
            return {
              ...d,
              ...updates,
              ...(changesText(d, updates) ? { unsynced: true } : {}),
              updatedAt: new Date().toISOString(),
            }
          }
          return d
        })
        saveDocumentsToIndexedDB(updatedDocs, false)
        return { documents: updatedDocs }
      })
    },

    updateActiveDocument: (updates, options) => {
      set((state) => {
        if (refusesBlanking(state.documents.find(d => d.id === state.activeDocumentId), updates, options)) return {}
        const updatedDocs = state.documents.map((d) => {
          if (d.id === state.activeDocumentId) {
            return {
              ...d,
              ...updates,
              ...(changesText(d, updates) ? { unsynced: true } : {}),
              updatedAt: new Date().toISOString(),
            }
          }
          return d
        })
        saveDocumentsToIndexedDB(updatedDocs, false)
        return { documents: updatedDocs }
      })
    },

    setDocumentPinned: (id, pinned) => {
      // Metadata, like a summary: no updatedAt bump, no revision (pinned_context.md §2).
      set((state) => {
        const updatedDocs = state.documents.map((d) => (d.id === id ? { ...d, pinned } : d))
        saveDocumentsToIndexedDB(updatedDocs, false)
        return { documents: updatedDocs }
      })
      const state = useAppStore.getState()
      if (state.user && state.activeBookId) {
        fetch(`/api/books/${state.activeBookId}/documents/${id}`, {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': state.csrfToken || '',
            [CLIENT_ID_HEADER]: CLIENT_ID
          },
          body: JSON.stringify({ pinned })
        }).then(res => res.ok ? res.json() : null)
          .then(body => useAppStore.getState().adoptServerUpdatedAt(body?.updatedAt))
          .catch(e => console.error('Failed to sync the chapter pin to the server', e))
      }
    },


  }
}

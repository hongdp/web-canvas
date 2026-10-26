import type { StateCreator } from 'zustand'
import type { DocumentVersion } from '../../types/document'
import type { AppState } from '../types'
import { safeIndexedDBSet, saveDocumentsToIndexedDB } from '../persistence'
// Benign import cycle (see documentsSlice): useAppStore is only touched inside
// action bodies, which run long after both modules finish evaluating.
import { useAppStore } from '../useAppStore'

/**
 * Version history — the safety net behind every LLM edit.
 *
 * Problem: a chapter was replaced with an empty document and there was nothing
 *   to roll back to. The server held snapshots, none newer than the day
 *   the snapshot write path was dropped.
 * Root cause: three defects compounding. Snapshots were only ever written to
 *   IndexedDB — `POST /api/books/{id}/versions` existed on the server with no
 *   caller, so the per-book sync that replaced the legacy whole-state save in
 *   June silently dropped version persistence. Then the sync OVERWROTE local
 *   snapshots with the server's (empty) list, so each reload erased the local
 *   copy too. And the 50-snapshot cap was global, so several books competed
 *   for the same 50 slots.
 * Fix: write snapshots through to the server, merge on read instead of
 *   replacing, cap per book, and never treat an empty server list as proof
 *   that nothing exists.
 */

/** Snapshots kept per book. Was global, which let one book evict another's. */
const MAX_VERSIONS_PER_BOOK = 50

export interface VersionsSlice {
  // Version history state
  versions: DocumentVersion[]
  /** Snapshot the active document, or `documentId` when given (agentic writes to other chapters). */
  createVersionSnapshot: (title?: string, documentId?: string) => void
  /**
   * Async because a snapshot's content may live only on the server: the list
   * arrives as metadata with `content: ''` and is fetched on demand.
   */
  restoreVersion: (versionId: string) => Promise<void>
  deleteVersionSnapshot: (versionId: string) => void
}

/** Trim to the per-book cap, counting only snapshots of the same book. */
function capPerBook(versions: DocumentVersion[], bookId: string): DocumentVersion[] {
  let kept = 0
  return versions.filter(v => {
    // Snapshots predating the bookId field belong to whichever book was open
    // then; they are counted against the active book rather than kept forever.
    if ((v.bookId ?? bookId) !== bookId) return true
    kept += 1
    return kept <= MAX_VERSIONS_PER_BOOK
  })
}

function postVersion(version: DocumentVersion, bookId: string): void {
  const state = useAppStore.getState()
  if (!state.user || !bookId) return
  fetch(`/api/books/${bookId}/versions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': state.csrfToken || ''
    },
    body: JSON.stringify({
      id: version.id,
      documentId: version.documentId,
      title: version.title,
      timestamp: version.timestamp,
      content: version.content
    })
  }).catch(e => console.error('Failed to sync version snapshot to server', e))
}

export const createVersionsSlice: StateCreator<AppState, [], [], VersionsSlice> = (set) => ({
  // Version history state
  versions: [],

  createVersionSnapshot: (title = 'Manual Snapshot', documentId) => {
    // Computed before the update rather than inside it, so the server write can
    // happen outside the reducer — a fetch belongs nowhere near a state update.
    const state = useAppStore.getState()
    const targetId = documentId ?? state.activeDocumentId
    const activeDoc = state.documents.find(d => d.id === targetId)
    if (!activeDoc) return

    const bookId = state.activeBookId || 'default'
    const newVersion: DocumentVersion = {
      // Two snapshots in one millisecond (a turn writing two chapters) must
      // not share an id.
      id: `ver-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      documentId: targetId,
      bookId,
      timestamp: new Date().toISOString(),
      title,
      content: activeDoc.content,
    }

    set((s) => {
      const updatedVersions = capPerBook([newVersion, ...s.versions], bookId)
      safeIndexedDBSet('web_canvas_versions', updatedVersions)
      return { versions: updatedVersions }
    })

    postVersion(newVersion, bookId)
  },

  restoreVersion: async (versionId) => {
    const state = useAppStore.getState()
    const version = state.versions.find(v => v.id === versionId)
    if (!version) return

    // The list may be server metadata, whose content is fetched on demand.
    // Writing that empty string into the document is how a restore would have
    // BLANKED the chapter it was meant to rescue.
    let content = version.content
    if (!content) {
      const bookId = version.bookId || state.activeBookId || 'default'
      try {
        const res = await fetch(`/api/books/${bookId}/versions/${versionId}`)
        if (res.ok) content = (await res.json()).content || ''
      } catch (e) {
        console.error('Failed to load version content from server', e)
      }
    }
    if (!content) {
      console.error('[versions] Refusing to restore a snapshot with no content.', { versionId })
      return
    }

    // Undo snapshot of what is about to be replaced, so a restore is itself
    // reversible.
    const fresh = useAppStore.getState()
    const activeDoc = fresh.documents.find(d => d.id === fresh.activeDocumentId)
    const bookId = fresh.activeBookId || 'default'
    let preRestore: DocumentVersion | null = null
    let updatedVersions = fresh.versions

    if (activeDoc) {
      preRestore = {
        id: `ver-${Date.now()}`,
        documentId: fresh.activeDocumentId,
        bookId,
        timestamp: new Date().toISOString(),
        title: `Auto-save before restoring "${version.title}"`,
        content: activeDoc.content,
      }
      updatedVersions = capPerBook([preRestore, ...fresh.versions], bookId)
      safeIndexedDBSet('web_canvas_versions', updatedVersions)
    }

    const restoredContent = content
    const updatedDocs = fresh.documents.map((d) =>
      d.id === fresh.activeDocumentId
        ? { ...d, content: restoredContent, updatedAt: new Date().toISOString() }
        : d
    )
    saveDocumentsToIndexedDB(updatedDocs, true)

    // Cache content fetched just now, so a second restore needs no round trip.
    const finalVersions = version.content
      ? updatedVersions
      : updatedVersions.map(v => v.id === versionId ? { ...v, content: restoredContent } : v)

    set({ documents: updatedDocs, versions: finalVersions })

    if (preRestore) postVersion(preRestore, bookId)
  },

  deleteVersionSnapshot: (versionId) => {
    const state = useAppStore.getState()
    const version = state.versions.find(v => v.id === versionId)
    const bookId = version?.bookId || state.activeBookId || 'default'

    set((s) => {
      const filteredVersions = s.versions.filter(v => v.id !== versionId)
      safeIndexedDBSet('web_canvas_versions', filteredVersions)
      return { versions: filteredVersions }
    })

    // Without this the snapshot returns on the next sync, because the server
    // still has it and the merge trusts the server for anything it knows.
    if (state.user && bookId) {
      fetch(`/api/books/${bookId}/versions/${versionId}`, {
        method: 'DELETE',
        headers: { 'X-CSRF-Token': state.csrfToken || '' }
      }).catch(e => console.error('Failed to delete version snapshot on server', e))
    }
  },
})

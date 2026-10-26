/**
 * Chapter sync against the server's revisions (docs/features/
 * backend_authority.md phase 1).
 *
 * The server owns each chapter's revision. A local chapter remembers the
 * revision its text is based on (`CanvasDocument.revision`) and whether it
 * holds an edit the server has not confirmed (`unsynced`, persisted with the
 * chapter so a reload cannot drop it). A save names its base revision; if
 * another tab or device wrote first the server refuses it, and the local
 * text is kept in version history while the server's copy takes its place —
 * shown, never silently merged.
 */
import type { CanvasDocument } from '../types/document'
import { useAppStore } from './useAppStore'
import { saveDocumentsToIndexedDB } from './persistence'

/** This tab's id, sent with its writes; the server echoes it in their events. */
export const CLIENT_ID: string =
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `c-${Date.now()}-${Math.random().toString(36).slice(2)}`

export const CLIENT_ID_HEADER = 'X-Client-Id'

/** What a chapter looked like when the server last confirmed it. */
export type ServerCopy = Pick<CanvasDocument, 'title' | 'content' | 'summary' | 'summaryContentHash'>

/**
 * This page's record of the server's copy of each loaded chapter.
 *
 * A save sends a chapter only when it differs from this (or is flagged
 * unsynced). Recording it when content LOADS — not only after a save —
 * matters: an unchanged chapter re-sent after a reload would bump its
 * revision and turn another tab's next save into a conflict.
 */
export const serverCopies = new Map<string, ServerCopy>()

export const recordServerCopy = (id: string, copy: ServerCopy) => {
  serverCopies.set(id, {
    title: copy.title,
    content: copy.content,
    summary: copy.summary,
    summaryContentHash: copy.summaryContentHash
  })
}

/** Does this chapter's text or title still need to reach the server? */
export const needsTextSync = (doc: CanvasDocument): boolean => {
  if (doc.unsynced) return true
  const prev = serverCopies.get(doc.id)
  return !prev || prev.title !== doc.title || prev.content !== doc.content
}

const writeDocs = (update: (doc: CanvasDocument) => CanvasDocument | null) => {
  useAppStore.setState(s => ({
    documents: s.documents.map(d => update(d) ?? d)
  }))
  saveDocumentsToIndexedDB(useAppStore.getState().documents, true)
}

/**
 * A save landed: the server now holds `sent` at `revision`. The chapter is
 * synced unless it changed again while the request was in flight — then it
 * stays unsynced, now based on the revision just written.
 */
export const markSaved = (id: string, sent: ServerCopy, revision: number | undefined) => {
  recordServerCopy(id, sent)
  writeDocs(d => {
    if (d.id !== id) return null
    const unchanged = d.content === sent.content && d.title === sent.title
    return { ...d, ...(revision !== undefined ? { revision } : {}), unsynced: unchanged ? false : d.unsynced }
  })
}

/** The server's copy of a chapter, adopted as synced (it never marks the chapter unsynced). */
export const adoptServerChapter = (id: string, server: { title?: string; content: string; revision?: number }) => {
  writeDocs(d => d.id !== id ? null : {
    ...d,
    content: server.content,
    ...(server.title !== undefined ? { title: server.title } : {}),
    ...(server.revision !== undefined ? { revision: server.revision } : {}),
    contentLoaded: true,
    unsynced: false
  })
  const doc = useAppStore.getState().documents.find(d => d.id === id)
  if (doc) recordServerCopy(id, doc)
}

/**
 * The server refused a save: another write landed first (409). Keep the
 * local text as a version, take the server's copy, and say so.
 */
export const resolveConflict = (doc: CanvasDocument, server: { title?: string; content: string; revision: number }) => {
  const s = useAppStore.getState()
  const local = s.documents.find(d => d.id === doc.id) ?? doc
  // Both copies say the same thing: nothing was lost, nothing to tell.
  if (local.content === server.content && (server.title === undefined || local.title === server.title)) {
    adoptServerChapter(doc.id, server)
    return
  }
  s.createVersionSnapshot(`Your unsynced edit of "${doc.title}", kept when another copy was saved first`, doc.id)
  adoptServerChapter(doc.id, server)
  useAppStore.getState().setSyncNotice({ kind: 'conflict', documentTitle: doc.title })
}

/**
 * The book's chapters as the server lists them, keeping each local chapter
 * that holds an unsynced edit — its text, title and base revision — instead
 * of replacing it with an empty stub.
 *
 * Problem: every load rebuilt the list from server metadata with
 *   `content: ''`, so an edit typed in the last seconds before a tab closed
 *   (the server save is debounced; IndexedDB had it) was overwritten on the
 *   next load and lost.
 * Fix: an unsynced chapter survives the load and is saved with its base
 *   revision; if another copy was saved meanwhile, that is a conflict and
 *   the edit goes to version history rather than nowhere. A chapter only
 *   this device holds (created while the server was unreachable) is kept.
 */
export const mergeServerChapters = (serverDocs: CanvasDocument[], localDocs: CanvasDocument[]): CanvasDocument[] => {
  const localById = new Map(localDocs.map(d => [d.id, d]))
  const merged = serverDocs.map(doc => {
    const local = localById.get(doc.id)
    if (!local?.unsynced || local.contentLoaded === false) return doc
    return { ...doc, title: local.title, content: local.content, contentLoaded: true, unsynced: true, revision: local.revision ?? doc.revision }
  })
  const serverIds = new Set(serverDocs.map(d => d.id))
  const onlyHere = localDocs.filter(d => d.unsynced && d.contentLoaded !== false && !serverIds.has(d.id))
  return [...merged, ...onlyHere]
}

/**
 * Save the unsynced chapters of a book that is NOT the one being opened.
 *
 * The local cache holds the book this device last had open; the account may
 * have moved to another book on another device since, and startup opens
 * that one. Merging the cache into it would put one book's chapters in
 * another, and replacing the cache would lose them — so they go to their
 * own book first. One that lost the race is kept in that book's version
 * history, as a conflict in the open book is.
 */
export async function saveOtherBookEdits(bookId: string, docs: CanvasDocument[], fetchFn: typeof fetch = fetch): Promise<void> {
  const csrf = useAppStore.getState().csrfToken || ''
  const headers = { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, [CLIENT_ID_HEADER]: CLIENT_ID }
  const base = `/api/books/${encodeURIComponent(bookId)}`
  await Promise.all(docs.filter(d => d.unsynced && d.contentLoaded !== false).map(async d => {
    try {
      const res = await fetchFn(`${base}/documents/${d.id}`, {
        method: 'PUT',
        headers,
        body: JSON.stringify({ title: d.title, content: d.content, ...(d.revision !== undefined ? { baseRevision: d.revision } : {}) })
      })
      if (res.status === 404) {
        await fetchFn(`${base}/documents`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ documents: [{ id: d.id, title: d.title, content: d.content, createdAt: d.createdAt, updatedAt: d.updatedAt }] })
        })
      } else if (res.status === 409) {
        await fetchFn(`${base}/versions`, {
          method: 'POST',
          headers,
          body: JSON.stringify({
            id: `ver-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            documentId: d.id,
            title: `Your unsynced edit of "${d.title}", kept when another copy was saved first`,
            timestamp: new Date().toISOString(),
            content: d.content
          })
        })
      }
    } catch (e) {
      console.error(`[documentSync] Could not save ${d.id} to book ${bookId}`, e)
    }
  }))
}

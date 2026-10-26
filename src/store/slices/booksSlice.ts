import type { StateCreator } from 'zustand'
import type { CanvasDocument } from '../../types/document'
import type { ChatMessage } from '../../types/chat'
import type { AppState, ServerDocumentMeta, ServerVersionMeta } from '../types'
import { localStorage, safeIndexedDBSet, saveDocumentsToIndexedDB } from '../persistence'
import { loadSavedConfigs, mergeProviderConfigs, saveConfigsToCookie, saveSystemPromptsToCookie } from '../settingsPersistence'
import { clearPendingSave, getIsInitialized, setIsInitialized } from '../syncRuntime'
import { normalizeBrParagraphs } from '../../utils/convert'

// What each chapter looked like when the server last confirmed it now lives
// in documentSync (serverCopies), recorded on load as well as on save.
import { CLIENT_ID, CLIENT_ID_HEADER, markSaved, mergeServerChapters, needsTextSync, recordServerCopy, resolveConflict, serverCopies } from '../documentSync'
// Benign import cycle: this module only references useAppStore inside action
// bodies, which run long after both modules have finished evaluating.
import { useAppStore } from '../useAppStore'

export interface SyncNotice {
  kind: 'conflict'
  documentTitle: string
}

export interface BooksSlice {
  bookTitle: string
  setBookTitle: (title: string) => void

  // Storage & Sync state
  isStoreInitialized: boolean
  setIsStoreInitialized: (initialized: boolean) => void
  serverSaveStatus: 'saved' | 'saving' | 'failed' | 'local-only'
  setServerSaveStatus: (status: 'saved' | 'saving' | 'failed' | 'local-only') => void
  /**
   * Something the user must know about a save: another copy of a chapter
   * was saved first, so theirs went to version history (documentSync).
   */
  syncNotice: SyncNotice | null
  setSyncNotice: (notice: SyncNotice | null) => void
  syncToServer: () => Promise<void>
  lastSyncedAt: string | null
  /**
   * The book's `updatedAt` as the SERVER last reported it — either when we
   * loaded the book or as returned by our own successful save. Focus-time
   * change detection compares server value to server value; comparing a
   * server timestamp against the device clock is unreliable (phones drift
   * from the host by minutes) and a false positive forces a full book
   * reload, which resets the editor to the top of the chapter.
   */
  lastSeenServerUpdatedAt: string | null
  setLastSeenServerUpdatedAt: (value: string | null) => void
  /** Record a mutation response's book stamp as seen, keeping the max. */
  adoptServerUpdatedAt: (stamp: string | null | undefined) => void

  // Multi-book state
  activeBookId: string
  availableBooks: { id: string; title: string; updatedAt: string }[]
  isLoadingBooks: boolean
  fetchAvailableBooks: () => Promise<void>
  createNewBook: (title?: string) => Promise<void>
  switchBook: (id: string) => Promise<void>
  deleteBook: (id: string) => Promise<void>
}

export const createBooksSlice: StateCreator<AppState, [], [], BooksSlice> = (set) => ({
  bookTitle: localStorage.getItem('web_canvas_book_title') || 'Untitled Book',
  setBookTitle: (title) => {
    localStorage.setItem('web_canvas_book_title', title)
    set({ bookTitle: title })
  },

  // Multi-book implementation
  activeBookId: localStorage.getItem('web_canvas_active_book_id') || 'default',
  availableBooks: [],
  isLoadingBooks: false,
  fetchAvailableBooks: async () => {
    const state = useAppStore.getState()
    if (!state.user) return

    set({ isLoadingBooks: true })
    try {
      const res = await fetch('/api/books')
      if (res.ok) {
        const books: { id: string; title: string; updatedAt: string }[] = await res.json()
        set({ availableBooks: books })
      }
    } catch (e) {
      console.error('Failed to fetch available books from server', e)
    } finally {
      set({ isLoadingBooks: false })
    }
  },
  // ── Performance-Critical: Optimistic Book Creation ──────────────────────
  // Problem: Previously, createNewBook awaited the server POST and
  // fetchAvailableBooks() sequentially before returning. This blocked
  // the UI for the full network round-trip (POST save + GET list), making
  // book creation feel very slow — especially because the GET /api/books
  // endpoint had to fully parse every book's JSON file (large when it
  // embeds base64 images) just to extract title and updatedAt.
  //
  // Fix (three layers):
  //   1. Optimistic UI — Zustand state is updated synchronously so the new
  //      book and its chapters appear in the UI immediately.
  //   2. Fire-and-forget networking — The server POST and book list refresh
  //      run in a detached async IIFE; createNewBook returns instantly.
  //   3. Server-side lightweight metadata — The /api/books endpoint now
  //      reads only the first 4KB of each file via regex instead of fully
  //      parsing multi-MB JSON. The save endpoint reorders keys so
  //      bookTitle and updatedAt always appear at the top of the file.
  // ──────────────────────────────────────────────────────────────────────────
  createNewBook: async (title = 'New Book') => {
    const state = useAppStore.getState()
    const newBookId = `book-${Date.now()}`

    clearPendingSave()

    const defaultDocs: CanvasDocument[] = [
      {
        id: 'doc-1',
        title: 'Chapter 1: Welcome',
        content: `<h1>Getting Started</h1><p>Start writing your new book here...</p>`,
        contentLoaded: true,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString()
      }
    ]

    const welcomeMsg: ChatMessage = {
      id: 'welcome',
      role: 'assistant',
      content: `Welcome to your new book "${title}"! How can I help you write today?`,
      timestamp: new Date().toISOString(),
    }

    const updates: Partial<AppState> = {
      bookTitle: title,
      documents: defaultDocs,
      versions: [],
      activeDocumentId: 'doc-1',
      messages: [welcomeMsg],
      activeBookId: newBookId
    }

    const wasInitialized = getIsInitialized()
    setIsInitialized(false)

    localStorage.setItem('web_canvas_active_book_id', newBookId)
    localStorage.setItem('web_canvas_book_title', title)
    saveDocumentsToIndexedDB(defaultDocs, true)
    safeIndexedDBSet('web_canvas_versions', [])
    localStorage.setItem('web_canvas_active_document_id', 'doc-1')

    // Optimistically add the new book to availableBooks so it appears instantly
    const optimisticBook = { id: newBookId, title, updatedAt: new Date().toISOString() }
    set({
      ...updates,
      availableBooks: [optimisticBook, ...state.availableBooks],
    })

    setIsInitialized(wasInitialized)

    // Server sync happens in the background — UI is already updated
    if (state.user) {
      set({ serverSaveStatus: 'saving' })
      ;(async () => {
        try {
          const res = await fetch('/api/books', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': state.csrfToken || ''
            },
            body: JSON.stringify({
              id: newBookId,
              title,
              documents: defaultDocs.map(d => ({
                id: d.id,
                title: d.title,
                content: d.content,
                createdAt: d.createdAt,
                updatedAt: d.updatedAt
              })),
              activeDocumentId: 'doc-1',
              activeProvider: state.activeProvider,
              providerConfigs: state.providerConfigs,
              customSystemPrompts: state.customSystemPrompts,
              activeSystemPromptId: state.activeSystemPromptId,
              theme: state.theme,
              messages: [welcomeMsg],
              debugMode: state.debugMode
            })
          })

          if (res.ok) {
            useAppStore.setState({ serverSaveStatus: 'saved', lastSyncedAt: new Date().toISOString() })
          } else {
            useAppStore.setState({ serverSaveStatus: 'failed' })
          }
          await useAppStore.getState().fetchAvailableBooks()
        } catch (e) {
          console.error('Failed to save new book to server', e)
          useAppStore.setState({ serverSaveStatus: 'failed' })
        }
      })()
    }
  },
  switchBook: async (id) => {
    if (!useAppStore.getState().user) return

    // Problem: the pending (debounced) save was cancelled, so an edit made
    //   in the seconds before switching books — or before a focus-time pull
    //   reloaded this one — never reached the server.
    // Fix: save what is unsynced first. Reloading the SAME book then also
    //   keeps any chapter that is still unsynced (mergeServerChapters).
    if (useAppStore.getState().documents.some(d => d.unsynced)) {
      await useAppStore.getState().syncToServer()
    }
    const state = useAppStore.getState()
    const sameBook = state.activeBookId === id
    clearPendingSave()

    set({ serverSaveStatus: 'saving' })
    try {
      // Use the new API that returns metadata + doc list (no content)
      const res = await fetch(`/api/books/${id}`)
      if (res.ok) {
        const server = await res.json()
        if (server && typeof server === 'object' && Object.keys(server).length > 0) {
          const wasInitialized = getIsInitialized()
          setIsInitialized(false)

          const updates: Partial<AppState> = {
            activeBookId: id
          }
          localStorage.setItem('web_canvas_active_book_id', id)

          // Build document list from server metadata (content not loaded yet)
          if (server.documents) {
            const stubs: CanvasDocument[] =               server.documents.map((d: ServerDocumentMeta) => ({
                id: d.id,
                title: d.title,
                content: '', // Content will be lazy-loaded
                contentLoaded: false,
                createdAt: d.createdAt,
                updatedAt: d.updatedAt,
                summary: d.summary ?? undefined,
                summaryContentHash: d.summaryContentHash ?? undefined,
                ...(typeof d.revision === 'number' ? { revision: d.revision } : {}),
                ...(d.pinned ? { pinned: true } : {}),
              }))
            // Another book's chapters are not this one's to keep.
            const docs = sameBook ? mergeServerChapters(stubs, state.documents) : stubs
            updates.documents = docs
            saveDocumentsToIndexedDB(docs, true)
          }
          if (server.versions) {
            // Versions from new API don't include content (lazy-loaded)
            updates.versions = server.versions.map((v: ServerVersionMeta) => ({
              id: v.id,
              documentId: v.documentId,
              title: v.title,
              timestamp: v.timestamp,
              content: '', // Lazy-loaded on demand
            }))
            safeIndexedDBSet('web_canvas_versions', updates.versions)
          }
          if (server.bookTitle) {
            updates.bookTitle = server.bookTitle
            localStorage.setItem('web_canvas_book_title', server.bookTitle)
          }
          if (server.activeDocumentId) {
            updates.activeDocumentId = server.activeDocumentId
            localStorage.setItem('web_canvas_active_document_id', server.activeDocumentId)
          }
          if (server.activeProvider) {
            updates.activeProvider = server.activeProvider
            localStorage.setItem('web_canvas_active_provider', server.activeProvider)
          }
          if (server.providerConfigs) {
            const currentConfigs = state.providerConfigs || loadSavedConfigs()
            const mergedConfigs = mergeProviderConfigs(currentConfigs, server.providerConfigs)
            updates.providerConfigs = mergedConfigs
            saveConfigsToCookie(mergedConfigs)
          }
          if (server.customSystemPrompts) {
            updates.customSystemPrompts = server.customSystemPrompts
            saveSystemPromptsToCookie(server.customSystemPrompts, server.activeSystemPromptId || 'prompt-none')
          }
          if (server.activeSystemPromptId) {
            updates.activeSystemPromptId = server.activeSystemPromptId
          }
          if (server.theme) {
            updates.theme = server.theme
            localStorage.setItem('web_canvas_theme', server.theme)
          }
          if (server.messages) updates.messages = server.messages
          if (server.debugMode !== undefined) {
            updates.debugMode = server.debugMode
            localStorage.setItem('web_canvas_debug_mode', String(server.debugMode))
          }

          set(updates)
          setIsInitialized(wasInitialized)

          // Now lazy-load the active document's content
          const activeDocId = updates.activeDocumentId || server.activeDocumentId
          const keptLocal = useAppStore.getState().documents.find(d => d.id === activeDocId)?.unsynced
          if (activeDocId && !keptLocal) {
            try {
              const docRes = await fetch(`/api/books/${id}/documents/${activeDocId}`)
              if (docRes.ok) {
                const docData = await docRes.json()
                // Normalize on ingestion (see contentLoader): a chapter stored
                // as a <br> wall heals itself the first time it loads.
                const loadedContent = normalizeBrParagraphs(docData.content || '')
                useAppStore.setState((s) => ({
                  documents: s.documents.map(d =>
                    d.id === activeDocId
                      ? { ...d, content: loadedContent, contentLoaded: true, ...(typeof docData.revision === 'number' ? { revision: docData.revision } : {}) }
                      : d
                  )
                }))
                const loadedDoc = useAppStore.getState().documents.find(d => d.id === activeDocId)
                if (loadedDoc) recordServerCopy(activeDocId, loadedDoc)
                saveDocumentsToIndexedDB(useAppStore.getState().documents, true)
              }
            } catch (e) {
              console.error('Failed to load active document content', e)
            }
          }
        }
        set({ serverSaveStatus: 'saved', lastSyncedAt: new Date().toISOString(), lastSeenServerUpdatedAt: server.updatedAt || null })
      } else {
        set({ serverSaveStatus: 'failed' })
        if (res.status === 401) {
          useAppStore.setState({ user: null, serverSaveStatus: 'local-only' })
          alert('You have been logged out because another session has started on the server.')
          window.location.reload()
          return
        }
      }
    } catch (e) {
      console.error('Failed to switch book', e)
      set({ serverSaveStatus: 'failed' })
    }
  },
  deleteBook: async (id) => {
    const state = useAppStore.getState()
    if (!state.user) return
    if (id === state.activeBookId) {
      alert('Cannot delete the currently active book. Please switch to another book first.')
      return
    }

    // Optimistically remove from UI
    set({ availableBooks: state.availableBooks.filter(b => b.id !== id) })

    try {
      const res = await fetch(`/api/books/${id}`, {
        method: 'DELETE',
        headers: {
          'X-CSRF-Token': state.csrfToken || ''
        }
      })
      if (res.ok) {
        await useAppStore.getState().fetchAvailableBooks()
      }
    } catch (e) {
      console.error('Failed to delete book on server', e)
      // Restore on failure
      await useAppStore.getState().fetchAvailableBooks()
    }
  },

  // Storage & Sync implementation
  isStoreInitialized: false,
  setIsStoreInitialized: (initialized) => set({ isStoreInitialized: initialized }),
  serverSaveStatus: 'local-only',
  setServerSaveStatus: (status) => set({ serverSaveStatus: status }),
  syncNotice: null,
  setSyncNotice: (notice) => set({ syncNotice: notice }),
  lastSyncedAt: null,
  lastSeenServerUpdatedAt: null,
  setLastSeenServerUpdatedAt: (value) => set({ lastSeenServerUpdatedAt: value }),

  adoptServerUpdatedAt: (stamp) => {
    // Every document mutation (PUT/POST/DELETE/reorder) bumps the book's
    // server-side updated_at AFTER the metadata PUT whose response we record —
    // so without adopting these stamps the focus-time check compared a stale
    // baseline against our own write and reloaded the whole book, resetting
    // the reader to the top of the chapter (user-reported). Max, not last:
    // parallel doc PUTs resolve in any order, and ISO-8601 UTC strings order
    // lexicographically.
    if (!stamp) return
    set((state) => (!state.lastSeenServerUpdatedAt || stamp > state.lastSeenServerUpdatedAt)
      ? { lastSeenServerUpdatedAt: stamp }
      : {})
  },
  syncToServer: async () => {
    const state = useAppStore.getState()
    if (!state.user) return

    const bookId = state.activeBookId || 'default'
    set({ serverSaveStatus: 'saving' })
    try {
      // 1. Sync book metadata + settings + messages via PUT /api/books/{bookId}
      const metaRes = await fetch(`/api/books/${bookId}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': state.csrfToken || ''
        },
        body: JSON.stringify({
          bookTitle: state.bookTitle,
          activeDocumentId: state.activeDocumentId,
          activeProvider: state.activeProvider,
          providerConfigs: state.providerConfigs,
          customSystemPrompts: state.customSystemPrompts,
          activeSystemPromptId: state.activeSystemPromptId,
          theme: state.theme,
          messages: state.messages,
          debugMode: state.debugMode,
          documentOrder: state.documents.map(d => d.id)
        })
      })
      if (metaRes.status === 401) {
        set({ user: null, serverSaveStatus: 'local-only' })
        alert('You have been logged out because another session has started on the server.')
        window.location.reload()
        return
      }
      if (metaRes.status === 404) {
        // Book doesn't exist on server yet — create it with full data
        const createRes = await fetch('/api/books', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': state.csrfToken || ''
          },
          body: JSON.stringify({
            id: bookId,
            title: state.bookTitle,
            documents: state.documents
              .filter(d => d.contentLoaded !== false)
              .map(d => ({ id: d.id, title: d.title, content: d.content, createdAt: d.createdAt, updatedAt: d.updatedAt })),
            activeDocumentId: state.activeDocumentId,
            activeProvider: state.activeProvider,
            providerConfigs: state.providerConfigs,
            customSystemPrompts: state.customSystemPrompts,
            activeSystemPromptId: state.activeSystemPromptId,
            theme: state.theme,
            messages: state.messages,
            debugMode: state.debugMode
          })
        })
        if (createRes.ok) {
          set({ serverSaveStatus: 'saved', lastSyncedAt: new Date().toISOString() })
        } else {
          set({ serverSaveStatus: 'failed' })
        }
        return
      }

      // 2. Sync each loaded document's content + title individually.
      // Problem: every debounced save PUT the FULL content of every loaded
      //   chapter — a single edit re-serialized and re-uploaded the whole
      //   book (base64 images included), which stalled phones for seconds.
      // Fix: skip documents whose synced fields are reference-identical to
      //   what the last successful PUT sent (the store replaces a document
      //   object whenever it changes, so reference checks are sufficient).
      // Only what the server does not hold yet: a chapter whose text differs
      // from its server copy (or is flagged unsynced) sends text and the
      // revision it is based on; one whose summary alone changed sends just
      // the summary, which does not bump its revision (documentSync).
      const docsToSync = state.documents
        .filter(d => d.contentLoaded !== false)
        .filter(d => {
          const prev = serverCopies.get(d.id)
          return needsTextSync(d) || !prev ||
            prev.summary !== d.summary || prev.summaryContentHash !== d.summaryContentHash
        })
      const sentCopies = docsToSync.map(d => ({ title: d.title, content: d.content, summary: d.summary, summaryContentHash: d.summaryContentHash }))
      const docSyncPromises = docsToSync
        .map((d, i) => {
          const sendText = needsTextSync(d)
          return fetch(`/api/books/${bookId}/documents/${d.id}`, {
            method: 'PUT',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': state.csrfToken || '',
              [CLIENT_ID_HEADER]: CLIENT_ID
            },
            body: JSON.stringify({
              ...(sendText
                ? { title: d.title, content: d.content, ...(d.revision !== undefined ? { baseRevision: d.revision } : {}) }
                : {}),
              summary: d.summary ?? null,
              summaryContentHash: d.summaryContentHash ?? null
            })
          }).then(async res => {
            if (res.ok) {
              let body: { updatedAt?: string; revision?: number } | null = null
              try { body = await res.clone().json() } catch { /* older server: no body */ }
              useAppStore.getState().adoptServerUpdatedAt(body?.updatedAt)
              const prev = serverCopies.get(d.id)
              markSaved(d.id, sendText ? sentCopies[i] : { ...sentCopies[i], title: prev?.title ?? d.title, content: prev?.content ?? d.content },
                sendText ? body?.revision : undefined)
            } else if (res.status === 409) {
              // Another tab or device saved this chapter first.
              try {
                const server = await res.clone().json()
                if (typeof server?.content === 'string' && typeof server?.revision === 'number') resolveConflict(d, server)
              } catch (e) {
                console.error(`Conflict on document ${d.id} could not be resolved`, e)
              }
            }
            return res
          }).catch(e => {
            console.error(`Failed to sync document ${d.id}`, e)
            return null
          })
        })

      const docResults = await Promise.all(docSyncPromises)

      // Check if any document wasn't found (404) and needs to be created
      for (let i = 0; i < docResults.length; i++) {
        const res = docResults[i]
        if (res && res.status === 404) {
          const doc = docsToSync[i]
          if (doc) {
            await fetch(`/api/books/${bookId}/documents`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': state.csrfToken || '',
                [CLIENT_ID_HEADER]: CLIENT_ID
              },
              body: JSON.stringify({
                documents: [{ id: doc.id, title: doc.title, content: doc.content, createdAt: doc.createdAt, updatedAt: doc.updatedAt }]
              })
            }).then(res => res.ok ? res.json() : null)
              .then(body => {
                useAppStore.getState().adoptServerUpdatedAt(body?.updatedAt)
                if (body) markSaved(doc.id, sentCopies[i], body.revision ?? 1)
              })
              .catch(e => console.error(`Failed to create document ${doc.id}`, e))
          }
        }
      }

      if (metaRes.ok) {
        // Record the server's own timestamp for OUR write, so the focus-time
        // check does not mistake it for another device's change.
        let ourServerUpdatedAt: string | null = null
        try {
          const body = await metaRes.clone().json()
          if (body && typeof body.updatedAt === 'string') ourServerUpdatedAt = body.updatedAt
        } catch { /* older server: no updatedAt in the response */ }
        set({
          serverSaveStatus: 'saved',
          lastSyncedAt: new Date().toISOString()
        })
        // Through adopt (max), NOT a direct write: the doc PUTs above already
        // adopted their (newer) stamps, and this block runs after them — a
        // direct write here regressed the baseline to the meta PUT's older
        // stamp and resurrected the exact focus-reload the adoption exists to
        // prevent. Max semantics make the ordering irrelevant.
        useAppStore.getState().adoptServerUpdatedAt(ourServerUpdatedAt)
      } else {
        set({ serverSaveStatus: 'failed' })
      }
    } catch {
      set({ serverSaveStatus: 'failed' })
    }
  },
})

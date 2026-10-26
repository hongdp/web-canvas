/**
 * Server bootstrap and sync helpers: local IndexedDB bootstrap, background
 * session/book sync (initializeStoreFromServer).
 *
 * NOTE on the import cycle: this module imports useAppStore from
 * './useAppStore' while useAppStore.ts (indirectly, via the slices)
 * re-imports this module. The cycle is benign because useAppStore is only
 * referenced inside function bodies, which run long after both modules have
 * finished evaluating (ESM live bindings resolve by then).
 */
import type { CanvasDocument, DocumentVersion } from '../types/document'
import type { AppState, ServerDocumentMeta, ServerVersionMeta } from './types'
import { localStorage, db, safeIndexedDBSet, saveDocumentsToIndexedDB, loadDocumentsFromIndexedDB, clearRetiredSettings } from './persistence'
import { MOCK_DOCUMENTS } from './defaults'
import { loadSavedConfigs, mergeProviderConfigs, saveConfigsToCookie, saveSystemPromptsToCookie } from './settingsPersistence'
import { getIsInitialized, setIsInitialized } from './syncRuntime'
import { normalizeBrParagraphs } from '../utils/convert'
import { useAppStore } from './useAppStore'
import { mergeVersions, versionsMissingOnServer, backfillVersions } from './versionMerge'
import { mergeServerChapters, recordServerCopy, saveOtherBookEdits } from './documentSync'

export const initializeStoreFromServer = async (forceRemoteSync = false) => {
  if (getIsInitialized() && !forceRemoteSync) return

  // 1. Load local state from IndexedDB first (fast, zero network overhead)
  if (!getIsInitialized()) {
    clearRetiredSettings()
    // Perform LocalStorage to IndexedDB migration if not done yet
    const isMigrated = localStorage.getItem('web_canvas_indexeddb_migrated') === 'true'
    if (!isMigrated) {
      try {
        const oldDocs = localStorage.getItem('web_canvas_documents')
        if (oldDocs) {
          await db.set('web_canvas_documents', JSON.parse(oldDocs))
          localStorage.removeItem('web_canvas_documents')
        }
        const oldVersions = localStorage.getItem('web_canvas_versions')
        if (oldVersions) {
          await db.set('web_canvas_versions', JSON.parse(oldVersions))
          localStorage.removeItem('web_canvas_versions')
        }
        localStorage.setItem('web_canvas_indexeddb_migrated', 'true')
        console.log('[Storage Migration] Successfully migrated heavy keys to IndexedDB.')
      } catch (migrationErr) {
        console.error('[Storage Migration] Migration failed:', migrationErr)
      }
    }

    // Load documents and versions from IndexedDB
    let loadedDocs: CanvasDocument[] | null = null
    let loadedVersions: DocumentVersion[] | null = null
    try {
      loadedDocs = await loadDocumentsFromIndexedDB()
      loadedVersions = await db.get<DocumentVersion[]>('web_canvas_versions')
    } catch (dbErr) {
      console.error('[IndexedDB] Failed to load data:', dbErr)
    }

    const documentsToSet = (loadedDocs && loadedDocs.length > 0) ? loadedDocs : MOCK_DOCUMENTS
    const versionsToSet = loadedVersions || []

    useAppStore.setState({
      documents: documentsToSet,
      versions: versionsToSet
    })

    // Set isStoreInitialized immediately so the UI boots up instantly using offline/local cache
    useAppStore.setState({ isStoreInitialized: true })
    setIsInitialized(true)
  }

  const performSync = async () => {
    // 2. Fetch current session status in the background
    let loggedInUser: string | null = null
    // The account's last active book, as the server recorded it (see
    // server_db.record_last_active_book). Null for a brand-new account.
    let serverLastActiveBookId: string | null = null
    try {
      const sessionRes = await fetch('/api/auth/session')
      if (sessionRes.ok) {
        const sessionData = await sessionRes.json()
        const csrfToken: string | null = sessionData.csrfToken || null
        useAppStore.setState({ csrfToken })
        if (sessionData.loggedIn) {
          loggedInUser = sessionData.username
          useAppStore.setState({ user: { username: sessionData.username as string } })
          if (typeof sessionData.lastActiveBookId === 'string' && sessionData.lastActiveBookId) {
            serverLastActiveBookId = sessionData.lastActiveBookId
          }
        } else {
          useAppStore.setState({ user: null })
        }
      }
    } catch (e) {
      console.error('Session verification failed', e)
    }

    // 3. If not logged in, fetch available books list and stop here
    if (!loggedInUser) {
      await useAppStore.getState().fetchAvailableBooks()
      return
    }

    // 4. Continue initialization for logged-in user: fetch book state from server.
    //
    // Problem: a new device (or a browser whose cache was cleared, or an
    //   account switch, which clears the same keys) always opened the book
    //   with id 'default' — the choice came from localStorage alone, and the
    //   server kept no per-user pointer to the book last worked in.
    // Fix: the session reports the account's last active book, recorded by
    //   every book write on the server. It wins over the local pointer,
    //   because "the book I was last editing" belongs to the account, not to
    //   the browser; the local key stays the fallback when the server has
    //   nothing to say (a brand-new account), and 'default' the last resort.
    const activeBookId = serverLastActiveBookId
      || localStorage.getItem('web_canvas_active_book_id')
      || 'default'
    // The local cache holds the book this device had open last; read its id
    // before the pointer moves.
    const cachedBookId = localStorage.getItem('web_canvas_active_book_id')
    if (useAppStore.getState().activeBookId !== activeBookId) {
      useAppStore.setState({ activeBookId })
    }
    localStorage.setItem('web_canvas_active_book_id', activeBookId)
    const cacheIsThisBook = cachedBookId === activeBookId
    if (!cacheIsThisBook && cachedBookId) {
      await saveOtherBookEdits(cachedBookId, useAppStore.getState().documents)
    }

    useAppStore.setState({ serverSaveStatus: 'saving' })
    try {
      // Use new API that returns metadata + doc list (no content)
      const res = await fetch(`/api/books/${activeBookId}`)
      if (res.ok) {
        const serverData = await res.json()
        if (serverData && typeof serverData === 'object' && Object.keys(serverData).length > 0) {
          // Load server-side updates
          const updates: Partial<AppState> = {}

          // Build documents from metadata (without content — lazy-loaded)
          if (serverData.documents) {
            // Unsynced chapters survive only into their own book.
            const local = cacheIsThisBook ? useAppStore.getState().documents : []
            const docs: CanvasDocument[] = mergeServerChapters(
              serverData.documents.map((d: ServerDocumentMeta) => ({
                id: d.id,
                title: d.title,
                content: '', // Will be lazy-loaded for active doc
                contentLoaded: false,
                createdAt: d.createdAt,
                updatedAt: d.updatedAt,
                summary: d.summary ?? undefined,
                summaryContentHash: d.summaryContentHash ?? undefined,
                ...(typeof d.revision === 'number' ? { revision: d.revision } : {}),
                ...(d.pinned ? { pinned: true } : {}),
              })),
              local)
            updates.documents = docs
            saveDocumentsToIndexedDB(docs, true)
          }
          // MERGE, never replace. This assignment used to be
          // `updates.versions = serverData.versions.map(...)`, and since no
          // caller ever POSTed a snapshot the server list was always empty —
          // so every sync erased the local history, which is why an emptied
          // chapter had nothing to roll back to.
          {
            const serverVersions: ServerVersionMeta[] = serverData.versions || []
            const merged = mergeVersions(
              useAppStore.getState().versions,
              serverVersions,
              activeBookId
            )
            updates.versions = merged
            safeIndexedDBSet('web_canvas_versions', merged)

            // Push the backlog this browser accumulated while the write path
            // was missing, so history stops being per-device.
            const backlog = versionsMissingOnServer(merged, serverVersions, activeBookId)
            if (backlog.length > 0) {
              void backfillVersions(backlog, activeBookId)
            }
          }
          if (serverData.bookTitle) {
            updates.bookTitle = serverData.bookTitle
            localStorage.setItem('web_canvas_book_title', serverData.bookTitle)
          }
          if (serverData.activeDocumentId) {
            updates.activeDocumentId = serverData.activeDocumentId
            localStorage.setItem('web_canvas_active_document_id', serverData.activeDocumentId)
          }
          if (serverData.activeProvider) {
            updates.activeProvider = serverData.activeProvider
            localStorage.setItem('web_canvas_active_provider', serverData.activeProvider)
          }
          if (serverData.providerConfigs) {
            const currentConfigs = useAppStore.getState().providerConfigs || loadSavedConfigs()
            const mergedConfigs = mergeProviderConfigs(currentConfigs, serverData.providerConfigs)
            updates.providerConfigs = mergedConfigs
            saveConfigsToCookie(mergedConfigs)
          }
          if (serverData.customSystemPrompts) {
            updates.customSystemPrompts = serverData.customSystemPrompts
            saveSystemPromptsToCookie(serverData.customSystemPrompts, serverData.activeSystemPromptId || 'prompt-none')
          }
          if (serverData.activeSystemPromptId) {
            updates.activeSystemPromptId = serverData.activeSystemPromptId
          }
          if (serverData.theme) {
            updates.theme = serverData.theme
            localStorage.setItem('web_canvas_theme', serverData.theme)
          }
          if (serverData.messages) updates.messages = serverData.messages
          if (serverData.debugMode !== undefined) {
            updates.debugMode = serverData.debugMode
            localStorage.setItem('web_canvas_debug_mode', String(serverData.debugMode))
          }

          useAppStore.setState({ ...updates, serverSaveStatus: 'saved', lastSyncedAt: new Date().toISOString(), lastSeenServerUpdatedAt: serverData.updatedAt || null })

          // Lazy-load the active document's content
          const activeDocId = updates.activeDocumentId || serverData.activeDocumentId
          // An unsynced chapter keeps its local text (mergeServerChapters).
          const keptLocal = useAppStore.getState().documents.find(d => d.id === activeDocId)?.unsynced
          if (activeDocId && !keptLocal) {
            try {
              const docRes = await fetch(`/api/books/${activeBookId}/documents/${activeDocId}`)
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
              console.error('Failed to load active document content during init', e)
            }
          }
        } else {
          // Server is empty, initialize server with initial client/default state
          const state = useAppStore.getState()
          const postRes = await fetch('/api/books', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': state.csrfToken || ''
            },
            body: JSON.stringify({
              id: activeBookId,
              title: state.bookTitle,
              documents: state.documents.map(d => ({
                id: d.id,
                title: d.title,
                content: d.content,
                createdAt: d.createdAt,
                updatedAt: d.updatedAt,
              })),
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
          if (postRes.ok) {
            useAppStore.setState({ serverSaveStatus: 'saved', lastSyncedAt: new Date().toISOString() })
          } else {
            useAppStore.setState({ serverSaveStatus: 'failed' })
          }
        }
      } else {
        useAppStore.setState({ serverSaveStatus: 'failed' })
      }
    } catch (e) {
      console.error('Failed to load server data during initialization', e)
      useAppStore.setState({ serverSaveStatus: 'failed' })
    } finally {
      // Fetch available books list after sync is complete
      await useAppStore.getState().fetchAvailableBooks()
    }
  }

  if (forceRemoteSync) {
    await performSync()
  } else {
    // Execute asynchronously to allow render loop to run immediately
    performSync()
  }
}

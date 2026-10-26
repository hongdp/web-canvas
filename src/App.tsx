import { useState, useRef, useEffect, lazy, Suspense, useCallback, useMemo } from 'react'
import { Sparkles } from 'lucide-react'
import { ChatPanel } from './components/ChatPanel'
import { ChaptersSidebar } from './components/ChaptersSidebar'
import { AppHeader } from './components/AppHeader'
import { CanvasHeader } from './components/CanvasHeader'
import { CHAT_WIDTH, clampSize, loadPersistedSize, savePersistedSize } from './utils/layoutPrefs'
import { CanvasFooter } from './components/CanvasFooter'
import { VersionHistorySidebar } from './components/VersionHistorySidebar'
import { useAppStore, isEditLocked } from './store/useAppStore'
import { commitEditorHtml } from './store/editorChange'
import { DOMParser as ProseMirrorDOMParser } from '@tiptap/pm/model'

import { Editor } from './components/Editor'

// Lazy loaded heavy components to speed up initial mobile page shell load
const SettingsModal = lazy(() => import('./components/SettingsModal').then(m => ({ default: m.SettingsModal })))
const AuthForm = lazy(() => import('./components/AuthForm').then(m => ({ default: m.AuthForm })))
const ImageGenerationModal = lazy(() => import('./components/ImageGenerationModal').then(m => ({ default: m.ImageGenerationModal })))

import { useDiffHandlers } from './hooks/useDiffHandlers'
import { useModelFetcher } from './hooks/useModelFetcher'
import { useTranslation } from './i18n'
import { htmlToPlainText } from './utils/convert'
import { connectBookEvents } from './store/bookEvents'

// Stable no-op callbacks for the app-level model fetch (the Settings modal
// passes its own real error/loading setters). Must be module-scoped so their
// identity never changes between renders — otherwise useModelFetcher's effect
// deps change every render and refetch in a loop (429 rate-limit).
const noop = () => {}

function App() {
  const { t } = useTranslation()
  // Zustand store state
  const {
    theme,
    documents,
    activeDocumentId,
    isSidebarOpen,
    updateActiveDocument,
    toggleSidebar,
    activeEditor,
    isStoreInitialized,
    user,
    activeBookId,
    switchBook,
    lastSyncedAt,
    syncNotice,
    setSyncNotice
  } = useAppStore()

  // Local UI state
  // Persisted: a drag-set width that reset to 380 on every reload taught
  // people the handle was not worth using.
  const [chatWidth, setChatWidth] = useState(() =>
    loadPersistedSize(CHAT_WIDTH.key, CHAT_WIDTH.fallback, CHAT_WIDTH.bounds))
  const [isResizing, setIsResizing] = useState(false)
  const [isSettingsOpen, setIsSettingsOpen] = useState(false)
  const [isHistoryOpen, setIsHistoryOpen] = useState(false)
  const [imageGenInitialPrompt, setImageGenInitialPrompt] = useState('')
  const getLayoutMode = (width: number, height: number): 'desktop' | 'portrait' | 'landscape' | 'tablet-square' => {
    if (width >= 1024) return 'desktop'
    if (width > height && height < 500) return 'landscape'
    const ratio = width / height
    if (ratio >= 0.75 && ratio <= 1.35) return 'tablet-square'
    return 'portrait'
  }

  const [layoutMode, setLayoutMode] = useState<'desktop' | 'portrait' | 'landscape' | 'tablet-square'>(
    getLayoutMode(window.innerWidth, window.innerHeight)
  )

  // Save status state
  const [saveStatus, setSaveStatus] = useState<'saved' | 'unsaved'>('saved')
  const saveTimeoutRef = useRef<number | null>(null)

  const triggerUnsaved = useCallback(() => {
    setSaveStatus('unsaved')
    if (saveTimeoutRef.current) {
      clearTimeout(saveTimeoutRef.current)
    }
    saveTimeoutRef.current = window.setTimeout(() => {
      setSaveStatus('saved')
    }, 1500)
  }, [])

  const forceSave = useCallback(() => {
    if (saveTimeoutRef.current) {
      clearTimeout(saveTimeoutRef.current)
      saveTimeoutRef.current = null
    }
    setSaveStatus('saved')
  }, [])

  // Keep the live model lists (Gemini/Grok) populated app-wide so the top-bar
  // model dropdown matches Settings even before Settings is ever opened.
  useModelFetcher(true, noop, noop)

  // Image generation modal state
  const [isImageGenOpen, setIsImageGenOpen] = useState(false)

  // The book's reference files (attachments_and_web.md §1): the sidebar
  // lists them and the agent's index is built from them.
  useEffect(() => {
    if (isStoreInitialized && user) void useAppStore.getState().loadAttachments(activeBookId)
  }, [isStoreInitialized, user, activeBookId])

  // Clear timeout on unmount
  useEffect(() => {
    return () => {
      if (saveTimeoutRef.current) {
        clearTimeout(saveTimeoutRef.current)
      }
    }
  }, [])

  // Background prefetch heavy chunks as soon as the main shell is interactive
  useEffect(() => {
    const prefetch = () => {
      import('./components/SettingsModal').catch(err => console.warn('Failed to prefetch SettingsModal', err))
      import('./components/AuthForm').catch(err => console.warn('Failed to prefetch AuthForm', err))
      import('./components/ImageGenerationModal').catch(err => console.warn('Failed to prefetch ImageGenerationModal', err))
    }
    
    if (document.readyState === 'complete') {
      prefetch()
    } else {
      window.addEventListener('load', prefetch)
      return () => window.removeEventListener('load', prefetch)
    }
  }, [])


  // Clear timeout and reset to saved when active document changes
  useEffect(() => {
    if (saveTimeoutRef.current) {
      clearTimeout(saveTimeoutRef.current)
      saveTimeoutRef.current = null
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSaveStatus('saved')
  }, [activeDocumentId])

  // Portrait hands the document to the PAGE scroller instead of a nested
  // overflow div. Verified on-device (Firefox/Android): a pure-static control
  // page scrolled smoothly with root scroll and janked inside a nested
  // scroller with identical content — Gecko does not async-composite this
  // subscroller, so every tuning probe (will-change, contain, layerization)
  // failed. The class lives on <html> because html/body cannot be selected
  // from a descendant's layout class.
  useEffect(() => {
    document.documentElement.classList.toggle('root-scroll', layoutMode === 'portrait')
    return () => document.documentElement.classList.remove('root-scroll')
  }, [layoutMode])

  // Portrait hands the document to the PAGE scroller. The whole canvas column
  // is otherwise built as a fixed-height shell — every level sets height:100%
  // and overflow:hidden so that ONE inner div scrolls. Under a page scroller
  // each of those is a nested scroll container: it clips the grown content and
  // re-anchors `position: sticky` to itself. Rather than fight six inline
  // declarations with !important from CSS (which is how this was first tried,
  // and it leaked one layer at a time), the shell styles are simply NOT
  // EMITTED in portrait. See responsive.css for the CSS half.
  const rootScroll = layoutMode === 'portrait'
  const shellBox = rootScroll
    ? { height: 'auto' as const, overflow: 'visible' as const }
    : { height: '100%' as const, overflow: 'hidden' as const }

  // Track window size for mobile responsive layouts
  useEffect(() => {
    const handleResize = () => {
      setLayoutMode(getLayoutMode(window.innerWidth, window.innerHeight))
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // 2. Cross-Device Sync: Fetch server books and check for modifications on tab focus/visibility change
  useEffect(() => {
    const checkServerUpdates = async () => {
      const state = useAppStore.getState()
      if (!state.user || state.isStreaming) return

      // Do NOT pull-overwrite from the server while we have local changes that
      // haven't been flushed yet. The save is debounced (~3s), so right after a
      // generation the new content (including pending diffs) lives only in
      // memory. Reloading from the server here would clobber it — this is what
      // made AI diffs vanish when DevTools/window focus changed mid-debounce.
      if (state.serverSaveStatus === 'saving') return
      const activeDoc = state.documents.find(d => d.id === state.activeDocumentId)
      if (activeDoc?.content?.includes('data-diff-id')) return

      try {
        const res = await fetch('/api/books')
        if (res.ok) {
          const books = await res.json()

          const currentBook = (books as Array<{ id: string; updatedAt?: string }>).find(b => b.id === state.activeBookId)
          if (currentBook) {
            const serverUpdatedAt = currentBook.updatedAt
            const lastSeen = state.lastSeenServerUpdatedAt

            // Compare SERVER value to SERVER value.
            // Problem: this used to compare the server's timestamp against the
            //   client clock (`lastSyncedAt`), and treated "never synced this
            //   session" as "changed". Devices drift — a phone can run minutes
            //   ahead of the host — so returning to the tab reloaded
            //   the whole book almost every time, which re-sets the editor
            //   content and throws the reader back to the top of the chapter.
            // Fix: remember the server's own `updatedAt` (from the book load
            //   and from our own save's response) and reload only when the
            //   server reports a value we have not seen — i.e. another device
            //   really did write.
            if (serverUpdatedAt && lastSeen && serverUpdatedAt !== lastSeen) {
              console.log('Server changes detected. Syncing active book from server:', state.activeBookId)
              await state.switchBook(state.activeBookId)
            } else if (serverUpdatedAt && !lastSeen) {
              // First observation this session: adopt it as the baseline
              // instead of treating the unknown as a change.
              state.setLastSeenServerUpdatedAt(serverUpdatedAt)
            }
          }
        }
      } catch (err) {
        console.error('Failed to check server updates on focus', err)
      }
    }

    // `focus` and `visibilitychange` both fire when a tab comes back, which
    // ran the check (and its book reload) twice. Collapse them.
    let checkTimer: number | null = null
    const handleFocusOrVisible = () => {
      if (document.visibilityState !== 'visible') return
      if (checkTimer !== null) window.clearTimeout(checkTimer)
      checkTimer = window.setTimeout(() => {
        checkTimer = null
        void checkServerUpdates()
      }, 150)
    }

    window.addEventListener('focus', handleFocusOrVisible)
    document.addEventListener('visibilitychange', handleFocusOrVisible)

    return () => {
      if (checkTimer !== null) window.clearTimeout(checkTimer)
      window.removeEventListener('focus', handleFocusOrVisible)
      document.removeEventListener('visibilitychange', handleFocusOrVisible)
    }
  }, [activeBookId, switchBook, lastSyncedAt])

  // Live chapter changes from other tabs and devices (store/bookEvents).
  useEffect(() => {
    if (!user || !isStoreInitialized || !activeBookId) return
    return connectBookEvents(activeBookId)
  }, [user, isStoreInitialized, activeBookId])

  // Retrieve active document
  const activeDoc = documents.find(d => d.id === activeDocumentId) || documents[0] || {
    id: 'default',
    title: 'Untitled Chapter',
    content: '<p>Start writing...</p>'
  }

  const documentPlainTextContext = useMemo(() => htmlToPlainText(activeDoc.content), [activeDoc.content])
  const hasPendingDiffs = activeDoc.content.includes('data-diff-id')
  // Accept/reject rewrite the open chapter: not while the assistant is
  // writing into it (it would commit a half-painted preview).
  const reviewLocked = useAppStore(s => isEditLocked(s, s.activeDocumentId))

  // Handle theme changes
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  // Handle theme changes
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  // Horizontal resizing handlers
  /*
   * Problem: the chat panel jumped wider on the first mousemove and then ran
   *   ahead of the cursor — its width was set to `e.clientX`, the cursor's
   *   distance from the WINDOW's left edge, while the panel starts after the
   *   chapters sidebar (240px by default).
   * Root cause: absolute position used as a width.
   * Fix: the width moves by the cursor's delta from where the drag began,
   *   like the chapters sidebar's handle; pointer capture keeps the drag
   *   alive over the editor and outside the window.
   */
  const chatDrag = useRef<{ startX: number; startWidth: number } | null>(null)

  const endChatResize = () => {
    if (!chatDrag.current) return
    chatDrag.current = null
    setIsResizing(false)
    document.body.style.removeProperty('cursor')
    document.body.style.removeProperty('user-select')
    setChatWidth(current => {
      savePersistedSize(CHAT_WIDTH.key, current, CHAT_WIDTH.bounds)
      return current
    })
  }

  const chatResizeHandlers = {
    onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => {
      e.preventDefault()
      chatDrag.current = { startX: e.clientX, startWidth: chatWidth }
      setIsResizing(true)
      e.currentTarget.setPointerCapture(e.pointerId)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    },
    onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => {
      if (!chatDrag.current) return
      setChatWidth(clampSize(
        chatDrag.current.startWidth + (e.clientX - chatDrag.current.startX),
        CHAT_WIDTH.bounds
      ))
    },
    onPointerUp: endChatResize,
    onPointerCancel: endChatResize,
  }

  const { handleAcceptAllDiffs, handleRejectAllDiffs } = useDiffHandlers(
    activeEditor,
    activeDoc,
    updateActiveDocument,
    triggerUnsaved
  )

  // Route editor selection quick action toolbar commands to LLM
  // Open image generation modal with optional selected text as initial prompt
  const handleOpenImageGen = useCallback((selectedText: string) => {
    setImageGenInitialPrompt(selectedText)
    setIsImageGenOpen(true)
  }, [])

  useEffect(() => {
    const handleOpenImageEvent = (e: Event) => {
      const customEvent = e as CustomEvent<string>
      handleOpenImageGen(customEvent.detail)
    }
    window.addEventListener('open-image-gen', handleOpenImageEvent)
    return () => window.removeEventListener('open-image-gen', handleOpenImageEvent)
  }, [handleOpenImageGen])

  // Memoized editor onChange — avoids re-creating this on every render which
  // would cause the Editor to re-render on each App state change (e.g. chat input typing).
  // The write itself (title-follows-heading, and why it may blank the
  // chapter) lives in store/editorChange.
  const handleEditorChangeFor = useCallback((id: string, html: string) => {
    triggerUnsaved()
    commitEditorHtml(id, html)
  }, [triggerUnsaved])

  // Insert a generated image (base64 data URL) into the active editor at cursor position
  const handleInsertGeneratedImage = useCallback((dataUrl: string, altText: string) => {
    if (!activeEditor) return
    const imgHtml = `<img src="${dataUrl}" alt="${altText.replace(/"/g, '&quot;')}" style="max-width: 100%; height: auto; border-radius: 6px;" />`
    const tempDiv = document.createElement('div')
    tempDiv.innerHTML = imgHtml
    const slice = ProseMirrorDOMParser.fromSchema(activeEditor.state.schema).parseSlice(tempDiv)
    const tr = activeEditor.state.tr
    const { to } = activeEditor.state.selection
    tr.insert(to, slice.content)
    activeEditor.view.dispatch(tr)
    // Use getState() to avoid listing updateActiveDocument as a dependency (it's a new ref each render)
    useAppStore.getState().updateActiveDocument({ content: activeEditor.getHTML() })
    triggerUnsaved()
  }, [activeEditor, triggerUnsaved])

  if (!isStoreInitialized) {
    return (
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100%',
        width: '100%',
        backgroundColor: 'var(--bg-primary)',
        color: 'var(--text-primary)',
        gap: '1.5rem',
        fontFamily: 'system-ui, sans-serif'
      }}>
        <div style={{ position: 'relative', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div className="animate-spin" style={{
            width: '48px',
            height: '48px',
            borderRadius: '50%',
            border: '3px solid var(--border-color)',
            borderTopColor: 'var(--accent)'
          }} />
          <Sparkles size={20} style={{ position: 'absolute', color: 'var(--accent)' }} />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '0.25rem' }}>
          <h2 style={{ fontSize: '1.25rem', fontWeight: 600, margin: 0 }}>{t.app.materializingCanvas}</h2>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', margin: 0 }}>{t.app.checkingStorage}</p>
        </div>
      </div>
    )
  }

  if (!user) {
    return (
      <Suspense fallback={
        <div style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          width: '100%',
          backgroundColor: 'var(--bg-primary)',
          color: 'var(--text-primary)',
          gap: '1.5rem',
          fontFamily: 'system-ui, sans-serif'
        }}>
          <div style={{ position: 'relative', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <div className="animate-spin" style={{
              width: '48px',
              height: '48px',
              borderRadius: '50%',
              border: '3px solid var(--border-color)',
              borderTopColor: 'var(--accent)'
            }} />
            <Sparkles size={20} style={{ position: 'absolute', color: 'var(--accent)' }} />
          </div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', margin: 0 }}>{t.app.loadingAuth}</p>
        </div>
      }>
        <AuthForm />
      </Suspense>
    )
  }


  return (
    <div className={`app-container layout-${layoutMode}`}>
      {/* Top Application Bar */}
      <AppHeader layoutMode={layoutMode} onOpenSettings={() => setIsSettingsOpen(true)} />

      {/* Main split work area */}
      <main className="app-main">
        {/* Chapters Left Sidebar */}
        <ChaptersSidebar />

        {/* Sidebar Backdrop overlays for mobile drawer dismissal */}
        {isSidebarOpen && layoutMode !== 'desktop' && (
          <div 
            className="sidebar-backdrop" 
            onClick={toggleSidebar} 
          />
        )}
        {isHistoryOpen && layoutMode !== 'desktop' && (
          <div 
            className="sidebar-backdrop" 
            onClick={() => setIsHistoryOpen(false)} 
          />
        )}

        <ChatPanel
          chatWidth={chatWidth}
          layoutMode={layoutMode}
          isHistoryOpen={isHistoryOpen}
          forceSave={forceSave}
          setSaveStatus={setSaveStatus}
        />

        {/* Resizing Divider Gutter */}
        {layoutMode === 'desktop' && (
          <div 
            className={`resize-handle ${isResizing ? 'active' : ''}`}
            {...chatResizeHandlers}
          />
        )}

        {/* Right Side: Document Canvas Panel */}
        <section className="canvas-panel" style={{ display: 'flex', flexDirection: 'row', width: '100%', ...shellBox }}>
            {/* flex:1 in BOTH modes — .canvas-panel is a ROW flex container, so
                the main axis here is HORIZONTAL and `flex: none` would size
                this column to its max-content width (the toolbar's ~740px
                button row), dragging every descendant past a 450px viewport.
                Only height/overflow are mode-dependent. */}
            <div className="canvas-column" style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, ...shellBox }}>
              <CanvasHeader
                activeDoc={activeDoc}
                layoutMode={layoutMode}
                saveStatus={saveStatus}
                forceSave={forceSave}
                triggerUnsaved={triggerUnsaved}
                isHistoryOpen={isHistoryOpen}
                setIsHistoryOpen={setIsHistoryOpen}
                onOpenImageGen={handleOpenImageGen}
              />

              {syncNotice && (
                <div className="diff-review-banner sync-notice-banner" role="status">
                  <span className="diff-banner-text">
                    {t.app.syncConflict.message.replace('{title}', syncNotice.documentTitle)}
                  </span>
                  <div className="diff-banner-actions">
                    <button
                      onClick={() => { setIsHistoryOpen(true); setSyncNotice(null) }}
                      className="diff-banner-btn neutral"
                      type="button"
                    >
                      {t.app.syncConflict.openHistory}
                    </button>
                    <button onClick={() => setSyncNotice(null)} className="diff-banner-btn neutral" type="button">
                      {t.app.dismiss}
                    </button>
                  </div>
                </div>
              )}

              {hasPendingDiffs && (
                <div className="diff-review-banner">
                  <span className="diff-banner-text">{t.app.diffReview.title}</span>
                  <div className="diff-banner-actions">
                    <button 
                      onClick={handleAcceptAllDiffs} 
                      className="diff-banner-btn accept"
                      type="button"
                      disabled={reviewLocked}
                    >
                      {t.app.diffReview.acceptAll}
                    </button>
                    <button 
                      onClick={handleRejectAllDiffs} 
                      className="diff-banner-btn reject"
                      type="button"
                      disabled={reviewLocked}
                    >
                      {t.app.diffReview.rejectAll}
                    </button>
                  </div>
                </div>
              )}

              <div className="canvas-editor-container">
                {documents.map(doc => {
                  const isActive = doc.id === activeDocumentId
                  if (!isActive) return null
                  
                  // Show loading skeleton if content not yet loaded from server
                  if (doc.contentLoaded === false && doc.content === '') {
                    return (
                      <div key={doc.id} style={{ 
                        display: 'flex', 
                        flexDirection: 'column',
                        justifyContent: 'center', 
                        alignItems: 'center',
                        height: '100%',
                        flex: 1, /* fills the column in page-scroll mode, where 100% of auto is 0 */
                        gap: '1rem',
                        color: 'var(--text-muted)' 
                      }}>
                        <div className="loading-spinner" style={{ width: 32, height: 32 }} />
                        <span>Loading chapter content...</span>
                      </div>
                    )
                  }

                  return (
                    <div 
                      key={doc.id} 
                      style={{ height: rootScroll ? 'auto' : '100%', width: '100%' }}
                    >
                      <Editor 
                        isActive={true}
                        rootScroll={rootScroll}
                        documentId={doc.id}
                        content={doc.content} 
                        onChange={(html) => handleEditorChangeFor(doc.id, html)}
                      />
                    </div>
                  )
                })}
              </div>


              <CanvasFooter activeDoc={activeDoc} />
            </div>

            {/* Version History Sidebar Drawer */}
            <VersionHistorySidebar isHistoryOpen={isHistoryOpen} onClose={() => setIsHistoryOpen(false)} />
          </section>
      </main>

      {/* Settings Modal Overlay */}
      <Suspense fallback={null}>
        <SettingsModal 
          isOpen={isSettingsOpen} 
          onClose={() => setIsSettingsOpen(false)} 
        />
      </Suspense>

      {/* Image Generation Modal */}
      <Suspense fallback={null}>
        <ImageGenerationModal
          isOpen={isImageGenOpen}
          onClose={() => setIsImageGenOpen(false)}
          initialPrompt={imageGenInitialPrompt}
          documentContext={documentPlainTextContext}
          onInsertImage={handleInsertGeneratedImage}
        />
      </Suspense>
    </div>
  )
}

export default App

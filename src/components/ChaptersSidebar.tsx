import React, { useRef, useState } from 'react'
import { Plus, Trash2, BookOpen, ChevronLeft, Upload, ShieldAlert, Book, Library, RefreshCw, X, Check, GripVertical, ChevronUp, ChevronDown, Pin } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { markdownToHtml, txtToHtml, sanitizeHtml, splitHtmlToChapters, splitMarkdownToChapters, splitTxtToChapters } from '../utils/convert'
import { AttachmentsSection } from './AttachmentsSection'
import { useTranslation } from '../i18n'
import { SIDEBAR_WIDTH, clampSize, loadPersistedSize, savePersistedSize } from '../utils/layoutPrefs'

export const ChaptersSidebar: React.FC = () => {
  const { t } = useTranslation()

  // Desktop width, drag-adjustable and persisted. Chapter summaries live in
  // this column; a fixed 240px was the reason they were unreadable there.
  const [sidebarWidth, setSidebarWidth] = useState(() =>
    loadPersistedSize(SIDEBAR_WIDTH.key, SIDEBAR_WIDTH.fallback, SIDEBAR_WIDTH.bounds))
  const [isResizingSidebar, setIsResizingSidebar] = useState(false)
  const sidebarDrag = useRef<{ startX: number; startWidth: number } | null>(null)
  const {
    documents,
    activeDocumentId,
    isSidebarOpen,
    setActiveDocumentId,
    addDocument,
    importAllDocuments,
    reorderDocuments,
    deleteDocument,
    toggleSidebar,
    bookTitle,
    setBookTitle,
    user,
    activeBookId,
    availableBooks,
    isLoadingBooks,
    fetchAvailableBooks,
    createNewBook,
    switchBook,
    deleteBook,
    setDocumentPinned,
    isStreaming
  } = useAppStore()

  const [showReplaceConfirm, setShowReplaceConfirm] = useState(false)
  const [pendingChapters, setPendingChapters] = useState<{ title: string; content: string }[]>([])
  const [localBookTitle, setLocalBookTitle] = useState(bookTitle)
  // Keep the local editing buffer in sync with global store changes (e.g. if
  // updated via storage) using the "adjust state during render" pattern from
  // the React docs, which avoids a cascading setState-in-effect.
  const [prevBookTitle, setPrevBookTitle] = useState(bookTitle)
  if (bookTitle !== prevBookTitle) {
    setPrevBookTitle(bookTitle)
    setLocalBookTitle(bookTitle)
  }

  const [showBookManager, setShowBookManager] = useState(false)
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [newBookName, setNewBookName] = useState('')
  const [switchingBookId, setSwitchingBookId] = useState<string | null>(null)
  const [deletingBookId, setDeletingBookId] = useState<string | null>(null)
  const [isCreating, setIsCreating] = useState(false)
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null)
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null)

  React.useEffect(() => {
    if (showBookManager && user) {
      fetchAvailableBooks()
    }
  }, [showBookManager, user, fetchAvailableBooks])

  const handleCreateBook = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newBookName.trim()) return
    setIsCreating(true)
    try {
      await createNewBook(newBookName.trim())
      setNewBookName('')
      setShowCreateForm(false)
    } catch (err) {
      console.error('Failed to create book:', err)
    } finally {
      setIsCreating(false)
    }
  }

  const fileInputRef = useRef<HTMLInputElement>(null)
  const replaceFileInputRef = useRef<HTMLInputElement>(null)

  const handleDragStart = (e: React.DragEvent, index: number) => {
    setDraggedIndex(index)
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', String(index))
  }

  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault()
    if (dragOverIndex !== index) {
      setDragOverIndex(index)
    }
  }

  const handleDragEnd = () => {
    setDraggedIndex(null)
    setDragOverIndex(null)
  }

  const handleDrop = (e: React.DragEvent, targetIndex: number) => {
    e.preventDefault()
    if (draggedIndex === null || draggedIndex === targetIndex) {
      handleDragEnd()
      return
    }

    const reordered = [...documents]
    const [removed] = reordered.splice(draggedIndex, 1)
    reordered.splice(targetIndex, 0, removed)
    reorderDocuments(reordered)
    handleDragEnd()
  }

  const handleMoveUp = (index: number) => {
    if (index === 0) return
    const reordered = [...documents]
    const temp = reordered[index]
    reordered[index] = reordered[index - 1]
    reordered[index - 1] = temp
    reorderDocuments(reordered)
  }

  const handleMoveDown = (index: number) => {
    if (index === documents.length - 1) return
    const reordered = [...documents]
    const temp = reordered[index]
    reordered[index] = reordered[index + 1]
    reordered[index + 1] = temp
    reorderDocuments(reordered)
  }

  const handleAddDoc = () => {
    const chapterNum = documents.length + 1
    addDocument(`Chapter ${chapterNum}: Untitled`)
  }

  const handleImportClick = () => {
    fileInputRef.current?.click()
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files || files.length === 0) return

    const file = files[0]
    
    // TODO(security): Validate file size bounds before parsing to prevent browser lockups on extremely large documents
    if (file.size > 10 * 1024 * 1024) { // 10MB limit
      alert('File is too large. Please select a file smaller than 10MB.')
      return
    }

    const reader = new FileReader()

    reader.onload = (event) => {
      const text = event.target?.result as string
      if (typeof text !== 'string') return

      let htmlContent: string
      const extension = file.name.split('.').pop()?.toLowerCase() || ''
      const filenameWithoutExt = file.name.substring(0, file.name.lastIndexOf('.')) || file.name

      if (['md', 'markdown'].includes(extension)) {
        htmlContent = markdownToHtml(text)
      } else if (['html', 'htm'].includes(extension)) {
        htmlContent = sanitizeHtml(text)
      } else {
        // Plain text (txt or other)
        htmlContent = txtToHtml(text)
      }

      addDocument(filenameWithoutExt, htmlContent)

      // Reset the file input so the same file name can be imported consecutively if needed
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    }

    reader.readAsText(file)
  }

  const handleReplaceClick = () => {
    replaceFileInputRef.current?.click()
  }

  const handleReplaceFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files || files.length === 0) return

    const file = files[0]
    
    // TODO(security): Validate file size bounds before parsing to prevent browser lockups on extremely large documents
    if (file.size > 10 * 1024 * 1024) { // 10MB limit
      alert('File is too large. Please select a file smaller than 10MB.')
      return
    }

    const reader = new FileReader()

    reader.onload = (event) => {
      const text = event.target?.result as string
      if (typeof text !== 'string') return

      let parsedChapters: { title: string; content: string }[]
      const extension = file.name.split('.').pop()?.toLowerCase() || ''
      const filenameWithoutExt = file.name.substring(0, file.name.lastIndexOf('.')) || file.name

      if (['md', 'markdown'].includes(extension)) {
        parsedChapters = splitMarkdownToChapters(text)
      } else if (['html', 'htm'].includes(extension)) {
        parsedChapters = splitHtmlToChapters(text)
      } else {
        parsedChapters = splitTxtToChapters(text)
      }

      // Fallback: if splitting resulted in 0 chapters, treat whole file as a single chapter
      if (parsedChapters.length === 0) {
        let htmlContent: string
        if (['md', 'markdown'].includes(extension)) {
          htmlContent = markdownToHtml(text)
        } else if (['html', 'htm'].includes(extension)) {
          htmlContent = sanitizeHtml(text)
        } else {
          htmlContent = txtToHtml(text)
        }
        parsedChapters = [{ title: filenameWithoutExt, content: htmlContent }]
      }

      // Map empty titles to the filename
      parsedChapters = parsedChapters.map((ch, idx) => ({
        title: ch.title.trim() || `${filenameWithoutExt} - Part ${idx + 1}`,
        content: ch.content
      }))

      setPendingChapters(parsedChapters)
      setShowReplaceConfirm(true)

      // Reset the file input
      if (replaceFileInputRef.current) {
        replaceFileInputRef.current.value = ''
      }
    }

    reader.readAsText(file)
  }

  const endSidebarResize = () => {
    if (!sidebarDrag.current) return
    sidebarDrag.current = null
    setIsResizingSidebar(false)
    setSidebarWidth(current => {
      savePersistedSize(SIDEBAR_WIDTH.key, current, SIDEBAR_WIDTH.bounds)
      return current
    })
  }

  return (
    <aside
      className={`chapters-sidebar ${isSidebarOpen ? '' : 'collapsed'} ${isResizingSidebar ? 'resizing' : ''}`}
      style={{ ['--chapters-width' as string]: `${sidebarWidth}px` }}
    >
      <div
        className={`sidebar-resize-handle ${isResizingSidebar ? 'active' : ''}`}
        onPointerDown={(e) => {
          e.preventDefault()
          sidebarDrag.current = { startX: e.clientX, startWidth: sidebarWidth }
          setIsResizingSidebar(true)
          ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
        }}
        onPointerMove={(e) => {
          if (!sidebarDrag.current) return
          setSidebarWidth(clampSize(
            sidebarDrag.current.startWidth + (e.clientX - sidebarDrag.current.startX),
            SIDEBAR_WIDTH.bounds
          ))
        }}
        onPointerUp={endSidebarResize}
        onPointerCancel={endSidebarResize}
      />
      <div className="sidebar-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <BookOpen size={16} style={{ color: 'var(--text-secondary)' }} />
          <h2>Chapters</h2>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>          <button
            onClick={toggleSidebar}
            className="btn-icon"
            title={t.sidebar.collapse}
            type="button"
            style={{ padding: '0.25rem' }}
          >
            <ChevronLeft size={16} />
          </button>
        </div>
      </div>

      <div style={{ padding: '0.6rem 1rem', borderBottom: '1px solid var(--border-color)', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <div className="canvas-title-wrapper" style={{ flex: 1, opacity: isStreaming ? 0.6 : 1 }}>
          <Book size={16} style={{ color: 'var(--text-secondary)', flexShrink: 0 }} />
          <input
            type="text"
            value={localBookTitle}
            onChange={(e) => setLocalBookTitle(e.target.value)}
            onBlur={() => setBookTitle(localBookTitle)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                setBookTitle(localBookTitle)
                e.currentTarget.blur()
              }
            }}
            placeholder={t.sidebar.untitledBook}
            className="canvas-title-input"
            title={t.sidebar.bookTitlePlaceholder}
            disabled={isStreaming}
            style={{
              fontSize: '0.95rem',
              fontWeight: 600,
              padding: '0.2rem 0.4rem',
              height: 'auto',
              cursor: isStreaming ? 'not-allowed' : 'text'
            }}
          />
        </div>
        {user && (
          <button
            onClick={() => {
              if (isStreaming) return
              setShowBookManager(true)
            }}
            disabled={isStreaming}
            className="btn-icon"
            title={isStreaming ? t.sidebar.manageBooksLocked : t.sidebar.manageBooks}
            type="button"
            style={{ 
              padding: '0.25rem', 
              flexShrink: 0,
              opacity: isStreaming ? 0.5 : 1,
              cursor: isStreaming ? 'not-allowed' : 'pointer'
            }}
          >
            <Library size={16} />
          </button>
        )}
      </div>

      <div className="chapters-list">
        {documents.map((doc, idx) => {
          const isActive = doc.id === activeDocumentId
          const isDragging = idx === draggedIndex
          const isDragOver = idx === dragOverIndex && draggedIndex !== null && draggedIndex !== idx
          const dragOverClass = isDragOver ? (draggedIndex > idx ? 'drag-over-above' : 'drag-over-below') : ''
          
          return (
            <div
              key={doc.id}
              // Switching is allowed while the assistant writes: every write
              // targets its chapter by id, and the live preview follows the
              // chapter it belongs to (agentic_chat_loop.md D2). Only the
              // chapter it is writing into is read-only; the user may edit
              // the others, and a write to one they changed meanwhile is
              // refused and redone (§0.4).
              onClick={() => setActiveDocumentId(doc.id)}
              className={`chapter-item ${isActive ? 'active' : ''} ${isDragging ? 'dragging' : ''} ${dragOverClass}`}
              style={{ cursor: 'pointer' }}
              title={doc.title}
              // Reordering stays blocked: the model names chapters by their
              // number in the index it was sent, and a reorder mid-turn would
              // point those numbers at other chapters.
              draggable={!isStreaming}
              onDragStart={(e) => handleDragStart(e, idx)}
              onDragOver={(e) => handleDragOver(e, idx)}
              onDragEnd={handleDragEnd}
              onDrop={(e) => handleDrop(e, idx)}
            >
              <div 
                className="chapter-grip"
                title={isStreaming ? t.sidebar.dragLocked : t.sidebar.dragToReorder}
                draggable={false}
              >
                <GripVertical size={13} />
              </div>

              {doc.pinned && (
                <span className="chapter-pin-mark" title={t.sidebar.pinnedHint}><Pin size={12} /></span>
              )}
              <span className="chapter-title">{doc.title || 'Untitled Chapter'}</span>
              
              <div
                className="chapter-actions"
                onClick={(e) => e.stopPropagation()}
                onDragStart={(e) => e.stopPropagation()}
              >
                <button
                  onClick={() => setDocumentPinned(doc.id, !doc.pinned)}
                  className={`btn-icon chapter-action-btn${doc.pinned ? ' pinned' : ''}`}
                  title={doc.pinned ? t.sidebar.unpinChapter : t.sidebar.pinChapter}
                  aria-pressed={Boolean(doc.pinned)}
                  type="button"
                  style={{ padding: '0.15rem' }}
                >
                  <Pin size={13} />
                </button>
                <button
                  onClick={() => handleMoveUp(idx)}
                  disabled={idx === 0 || isStreaming}
                  className="btn-icon chapter-action-btn move-up"
                  title={t.sidebar.moveUp}
                  type="button"
                  style={{ padding: '0.15rem' }}
                >
                  <ChevronUp size={13} />
                </button>
                <button
                  onClick={() => handleMoveDown(idx)}
                  disabled={idx === documents.length - 1 || isStreaming}
                  className="btn-icon chapter-action-btn move-down"
                  title={t.sidebar.moveDown}
                  type="button"
                  style={{ padding: '0.15rem' }}
                >
                  <ChevronDown size={13} />
                </button>
                <button
                  onClick={() => {
                    if (confirm(t.sidebar.deleteChapterConfirm.replace('{title}', doc.title))) {
                      deleteDocument(doc.id)
                    }
                  }}
                  disabled={isStreaming}
                  className="btn-icon chapter-action-btn delete"
                  title={t.sidebar.deleteChapter}
                  type="button"
                  style={{ padding: '0.15rem' }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          )
        })}
      </div>

      <AttachmentsSection />

      <div style={{ padding: '1rem', borderTop: '1px solid var(--border-color)', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
        <button
          onClick={handleAddDoc}
          disabled={isStreaming}
          className="btn-primary"
          style={{
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '0.5rem',
            fontSize: '0.85rem',
            padding: '0.6rem',
            opacity: isStreaming ? 0.5 : 1,
            cursor: isStreaming ? 'not-allowed' : 'pointer'
          }}
          type="button"
        >
          <Plus size={16} /> {t.sidebar.newChapter}
        </button>
        <button
          onClick={handleImportClick}
          disabled={isStreaming}
          className="btn-secondary"
          style={{
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '0.5rem',
            fontSize: '0.85rem',
            padding: '0.6rem',
            opacity: isStreaming ? 0.5 : 1,
            cursor: isStreaming ? 'not-allowed' : 'pointer'
          }}
          type="button"
        >
          <Upload size={16} /> {t.sidebar.importChapter}
        </button>
        <button
          onClick={handleReplaceClick}
          disabled={isStreaming}
          className="btn-secondary"
          style={{
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '0.5rem',
            fontSize: '0.85rem',
            padding: '0.6rem',
            opacity: isStreaming ? 0.5 : 1,
            cursor: isStreaming ? 'not-allowed' : 'pointer'
          }}
          type="button"
        >
          <Upload size={16} /> {t.sidebar.replaceAllChapters}
        </button>
        <input
          type="file"
          ref={fileInputRef}
          onChange={handleFileChange}
          accept=".html,.htm,.md,.markdown,.txt"
          style={{ display: 'none' }}
        />
        <input
          type="file"
          ref={replaceFileInputRef}
          onChange={handleReplaceFileChange}
          accept=".html,.htm,.md,.markdown,.txt"
          style={{ display: 'none' }}
        />
      </div>

      {showReplaceConfirm && (
        <div className="modal-overlay" onClick={() => setShowReplaceConfirm(false)}>
          <div 
            className="modal-content glass-panel" 
            onClick={(e) => e.stopPropagation()}
            style={{
              border: '1px solid var(--border-color)',
              backgroundColor: 'var(--bg-secondary)',
              color: 'var(--text-primary)',
              maxWidth: '420px',
              padding: '1.5rem',
              textAlign: 'left'
            }}
          >
            <div className="modal-header" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.75rem' }}>
              <ShieldAlert size={20} style={{ color: 'var(--accent)' }} />
              <h3 style={{ margin: 0, fontSize: '1.1rem', fontWeight: 600 }}>{t.sidebar.replaceAllTitle}</h3>
            </div>
            <div className="modal-body" style={{ margin: '1rem 0', fontSize: '0.9rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              <p>This action will permanently delete all existing chapters in this project and replace them with the <strong>{pendingChapters.length}</strong> imported chapter(s).</p>
              <p style={{ marginTop: '0.5rem', color: 'var(--text-muted)', fontSize: '0.8rem' }}>{t.sidebar.replaceAllNotice}</p>
            </div>
            <div className="modal-footer" style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.75rem', paddingTop: '0.75rem', borderTop: '1px solid var(--border-color)' }}>
              <button 
                onClick={() => setShowReplaceConfirm(false)} 
                className="btn-secondary"
                style={{ padding: '0.4rem 1rem', fontSize: '0.85rem' }}
                type="button"
              >
                {t.app.dismiss}
              </button>
              <button 
                onClick={() => {
                  importAllDocuments(pendingChapters)
                  setShowReplaceConfirm(false)
                  setPendingChapters([])
                }} 
                className="btn-primary"
                style={{ padding: '0.4rem 1rem', fontSize: '0.85rem' }}
                type="button"
              >
                {t.sidebar.confirmReplace}
              </button>
            </div>
          </div>
        </div>
      )}

      {showBookManager && (
        <div className="modal-overlay" onClick={() => setShowBookManager(false)}>
          <div
            className="modal-content glass-panel"
            onClick={(e) => e.stopPropagation()}
            style={{
              border: '1px solid var(--border-color)',
              backgroundColor: 'var(--bg-secondary)',
              color: 'var(--text-primary)',
              maxWidth: '480px',
              padding: '1.5rem',
              textAlign: 'left'
            }}
          >
            <div className="modal-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.75rem' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <Library size={18} style={{ color: 'var(--accent)' }} />
                <h3 style={{ margin: 0, fontSize: '1.1rem', fontWeight: 600 }}>{t.sidebar.manageBooks}</h3>
              </div>
              <button
                onClick={() => setShowBookManager(false)}
                className="btn-icon"
                title={t.sidebar.close}
                type="button"
                style={{ padding: '0.25rem' }}
              >
                <X size={16} />
              </button>
            </div>

            <div className="modal-body" style={{ margin: '1rem 0', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
              {showCreateForm ? (
                <form onSubmit={handleCreateBook} style={{ display: 'flex', gap: '0.5rem' }}>
                  <input
                    type="text"
                    placeholder={t.sidebar.bookTitlePlaceholder}
                    value={newBookName}
                    onChange={(e) => setNewBookName(e.target.value)}
                    className="form-input"
                    style={{ flex: 1, padding: '0.4rem 0.6rem', fontSize: '0.85rem' }}
                    required
                    autoFocus
                  />
                  <button type="submit" className="btn-primary" disabled={isCreating} style={{ padding: '0.4rem 1rem', fontSize: '0.85rem' }}>
                    {isCreating ? <RefreshCw size={14} className="animate-spin" /> : 'Create'}
                  </button>
                  <button type="button" onClick={() => setShowCreateForm(false)} className="btn-secondary" style={{ padding: '0.4rem 0.6rem' }}>
                    <X size={14} />
                  </button>
                </form>
              ) : (
                <button
                  onClick={() => setShowCreateForm(true)}
                  className="btn-secondary"
                  style={{
                    width: '100%',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '0.5rem',
                    fontSize: '0.85rem',
                    padding: '0.5rem'
                  }}
                  type="button"
                >
                  <Plus size={14} /> {t.sidebar.createNewBook}
                </button>
              )}

              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', maxHeight: '300px', overflowY: 'auto', paddingRight: '4px' }}>
                {isLoadingBooks && availableBooks.length === 0 ? (
                  <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', padding: '2rem', color: 'var(--text-secondary)' }}>
                    <RefreshCw size={18} className="animate-spin" style={{ marginRight: '0.5rem' }} />
                    <span>{t.sidebar.loadingBooks}</span>
                  </div>
                ) : availableBooks.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                    No books found on the server.
                  </div>
                ) : (
                  availableBooks.map((book) => {
                    const isActive = book.id === activeBookId
                    const isSwitching = switchingBookId === book.id
                    const isDeleting = deletingBookId === book.id

                    return (
                      <div
                        key={book.id}
                        className="glass-panel"
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          padding: '0.75rem 1rem',
                          borderRadius: '8px',
                          border: isActive ? '1px solid var(--accent)' : '1px solid var(--border-color)',
                          backgroundColor: isActive ? 'var(--accent-glow)' : 'var(--bg-primary)',
                          transition: 'all 0.2s',
                          cursor: isActive ? 'default' : 'pointer'
                        }}
                        onClick={async () => {
                          if (isActive || isSwitching || isDeleting) return
                          setSwitchingBookId(book.id)
                          try {
                            await switchBook(book.id)
                          } catch (err) {
                            console.error(err)
                          } finally {
                            setSwitchingBookId(null)
                          }
                        }}
                      >
                        <div style={{ flex: 1, minWidth: 0, marginRight: '1rem' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                            <span style={{
                              fontWeight: isActive ? 600 : 500,
                              color: isActive ? 'var(--accent)' : 'var(--text-primary)',
                              fontSize: '0.85rem',
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis'
                            }}>
                              {book.title}
                            </span>
                            {isActive && (
                              <span style={{
                                fontSize: '0.65rem',
                                backgroundColor: 'var(--accent)',
                                color: 'var(--accent-text)',
                                padding: '1px 6px',
                                borderRadius: '10px',
                                fontWeight: 600,
                                whiteSpace: 'nowrap'
                              }}>
                                Active
                              </span>
                            )}
                          </div>
                          <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.2rem' }}>
                            Updated: {new Date(book.updatedAt).toLocaleString()}
                          </div>
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }} onClick={(e) => e.stopPropagation()}>
                          {isSwitching ? (
                            <RefreshCw size={14} className="animate-spin" style={{ color: 'var(--text-secondary)' }} />
                          ) : isActive ? (
                            <Check size={16} style={{ color: 'var(--accent)' }} />
                          ) : (
                            <button
                              onClick={async () => {
                                if (confirm(t.sidebar.deleteBookConfirm.replace('{title}', book.title))) {
                                  setDeletingBookId(book.id)
                                  try {
                                    await deleteBook(book.id)
                                  } catch (err) {
                                    console.error(err)
                                  } finally {
                                    setDeletingBookId(null)
                                  }
                                }
                              }}
                              disabled={isDeleting}
                              className="btn-icon"
                              title={t.sidebar.deleteBook}
                              style={{ padding: '0.25rem', color: 'var(--text-muted)' }}
                              type="button"
                            >
                              {isDeleting ? (
                                <RefreshCw size={14} className="animate-spin" />
                              ) : (
                                <Trash2 size={14} style={{ color: 'var(--diff-remove-text)' }} />
                              )}
                            </button>
                          )}
                        </div>
                      </div>
                    )
                  })
                )}
              </div>
            </div>

            <div className="modal-footer" style={{ display: 'flex', justifyContent: 'flex-end', paddingTop: '0.75rem', borderTop: '1px solid var(--border-color)' }}>
              <button
                onClick={() => setShowBookManager(false)}
                className="btn-secondary"
                style={{ padding: '0.4rem 1rem', fontSize: '0.85rem' }}
                type="button"
              >
                {t.sidebar.close}
              </button>
            </div>
          </div>
        </div>
      )}

    </aside>
  )
}

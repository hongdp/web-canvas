import { useState } from 'react'
import { History, Cloud, CloudOff, CloudUpload, Wand2, RefreshCw, Save, Download, BookOpen, Sparkles } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import type { CanvasDocument } from '../store/useAppStore'
import { useTranslation } from '../i18n'
import { exportDocument } from '../utils/export'
import { contentWithRenamedHeading } from '../utils/titleSync'
import { polishOffered } from '../utils/runDefaults'

interface CanvasHeaderProps {
  activeDoc: Pick<CanvasDocument, 'title' | 'content'>
  layoutMode: 'desktop' | 'portrait' | 'landscape' | 'tablet-square'
  saveStatus: 'saved' | 'unsaved'
  forceSave: () => void
  triggerUnsaved: () => void
  isHistoryOpen: boolean
  setIsHistoryOpen: (open: boolean) => void
  onOpenImageGen: (selectedText: string) => void
}

/**
 * Canvas panel header: document title input plus the actions row (local save
 * status, server sync status, version history toggle, image generation, and
 * the export dropdown). Extracted from App.tsx unchanged (same DOM/classes);
 * save-status state stays in App because the editor and chat flows drive it.
 */
export function CanvasHeader({
  activeDoc,
  layoutMode,
  saveStatus,
  forceSave,
  triggerUnsaved,
  isHistoryOpen,
  setIsHistoryOpen,
  onOpenImageGen
}: CanvasHeaderProps) {
  const { t } = useTranslation()
  const {
    documents,
    updateActiveDocument,
    isStreaming,
    selectedText,
    bookTitle,
    serverSaveStatus,
    syncToServer,
    activeDocumentId,
    requestPolish,
    activeProvider,
    providerConfigs
  } = useAppStore()

  const [isExportDropdownOpen, setIsExportDropdownOpen] = useState(false)

  // Export document handler
  const handleExport = (format: 'html' | 'markdown' | 'txt', exportAll: boolean) => {
    exportDocument(format, exportAll, documents, activeDoc, bookTitle)
  }

  return (
    <div className="canvas-header">
      <div className="canvas-title-wrapper">
        <BookOpen size={16} style={{ color: 'var(--text-secondary)' }} />
        <input
          type="text"
          value={activeDoc.title}
          onChange={e => {
            triggerUnsaved()
            updateActiveDocument({ title: e.target.value })
          }}
          onBlur={() => {
            // Rename → heading linkage, the other half of title-follows-h1
            // (utils/titleSync): committing a rename rewrites a leading <h1>
            // so the two never disagree. On commit, not per keystroke — a
            // content update re-parses the whole document in the editor.
            const renamed = contentWithRenamedHeading(activeDoc.content, activeDoc.title)
            if (renamed !== null) {
              updateActiveDocument({ content: renamed })
            }
          }}
          onKeyDown={e => {
            if (e.key === 'Enter') e.currentTarget.blur()
          }}
          className="canvas-title-input"
          placeholder="Untitled Document"
          title="Document Title"
          disabled={isStreaming}
          style={{
            cursor: isStreaming ? 'not-allowed' : 'text',
            opacity: isStreaming ? 0.6 : 1
          }}
        />
      </div>

      <div className="canvas-actions">
        {/* Local Save Status Button */}
        <button
          onClick={() => {
            if (saveStatus === 'unsaved') {
              forceSave()
            }
          }}
          className={`btn-icon ${saveStatus === 'unsaved' ? 'is-dirty' : ''}`}
          title={
            saveStatus === 'unsaved'
              ? 'Unsaved changes in browser (autosaving locally...)'
              : 'All edits saved to local browser storage'
          }
          type="button"
          style={{
            color: saveStatus === 'unsaved' ? 'var(--accent)' : 'var(--text-muted)',
            cursor: saveStatus === 'unsaved' ? 'pointer' : 'default',
          }}
        >
          {saveStatus === 'unsaved' ? (
            <RefreshCw size={18} className="animate-spin" />
          ) : (
            <Save size={18} />
          )}
        </button>

        {/* Server Sync Status Indicator */}
        <button
          onClick={() => {
            if (serverSaveStatus === 'failed') {
              syncToServer()
            }
          }}
          className={`btn-icon ${serverSaveStatus === 'saving' ? 'is-dirty' : ''} ${serverSaveStatus === 'failed' ? 'has-error' : ''}`}
          title={
            serverSaveStatus === 'saved'
              ? 'All changes synced to cloud server'
              : serverSaveStatus === 'saving'
              ? 'Syncing changes to cloud server...'
              : serverSaveStatus === 'failed'
              ? 'Sync failed (server offline). Click to retry.'
              : 'Local-only mode (log in to sync with cloud)'
          }
          type="button"
          style={{
            color:
              serverSaveStatus === 'saved'
                ? 'var(--text-muted)'
                : serverSaveStatus === 'saving'
                ? 'var(--accent)'
                : serverSaveStatus === 'failed'
                ? 'var(--diff-remove-text)'
                : 'var(--text-muted)',
            cursor: serverSaveStatus === 'failed' ? 'pointer' : 'default',
            opacity: serverSaveStatus === 'local-only' ? 0.4 : 1,
          }}
        >
          {serverSaveStatus === 'saving' ? (
            <CloudUpload size={18} className="animate-pulse" />
          ) : serverSaveStatus === 'failed' ? (
            <CloudOff size={18} />
          ) : serverSaveStatus === 'local-only' ? (
            <CloudOff size={18} />
          ) : (
            <Cloud size={18} />
          )}
        </button>

        <button
          onClick={() => setIsHistoryOpen(!isHistoryOpen)}
          className={`btn-icon ${isHistoryOpen ? 'active' : ''}`}
          title={t.app.historyToggle}
          type="button"
          style={{ color: isHistoryOpen ? 'var(--accent)' : 'inherit' }}
        >
          <History size={18} />
        </button>

        {/* Polish (agentic_chat_loop.md D9): only ever on the user's request, and only with a
            polish model other than the chat model (utils/runDefaults polishOffered). */}
        {polishOffered(activeProvider, providerConfigs[activeProvider]) && <button
          onClick={() => requestPolish(activeDocumentId)}
          className="btn-icon"
          title={t.app.polishHint}
          type="button"
          disabled={isStreaming || !activeDoc.content}
          style={{
            borderRadius: '7px',
            padding: '5px 10px',
            display: 'flex',
            alignItems: 'center',
            gap: '5px',
            fontSize: '0.78rem',
            fontWeight: 600,
            border: '1px solid var(--border-color)',
            opacity: isStreaming ? 0.5 : 1,
            cursor: isStreaming ? 'not-allowed' : 'pointer'
          }}
        >
          <Sparkles size={14} />
          {layoutMode !== 'portrait' && t.app.polish}
        </button>}

        {/* Generate Image Button */}
        <button
          onClick={() => onOpenImageGen(selectedText || '')}
          className="btn-icon"
          title="Generate image with AI (uses selected text as prompt)"
          type="button"
          style={{
            background: 'var(--accent)',
            color: 'var(--accent-text)',
            borderRadius: '7px',
            padding: '5px 10px',
            display: 'flex',
            alignItems: 'center',
            gap: '5px',
            fontSize: '0.78rem',
            fontWeight: 600,
            border: 'none',
            boxShadow: 'var(--shadow-sm)',
          }}
        >
          <Wand2 size={14} />
          {layoutMode !== 'portrait' && 'Gen Image'}
        </button>

        {/* Export Dropdown relative wrapper */}
        <div style={{ position: 'relative', display: 'inline-block' }}>
          <button
            onClick={() => setIsExportDropdownOpen(!isExportDropdownOpen)}
            className={`btn-icon ${isExportDropdownOpen ? 'active' : ''}`}
            title={t.app.exportDoc}
            type="button"
            id="export-dropdown-trigger"
          >
            <Download size={18} />
          </button>
          {isExportDropdownOpen && (
            <div
              className="glass-panel dropdown-menu"
              style={{
                position: 'absolute',
                right: 0,
                top: 'calc(100% + 6px)',
                display: 'flex',
                flexDirection: 'column',
                width: '200px',
                borderRadius: '8px',
                boxShadow: 'var(--shadow-lg)',
                zIndex: 30,
                padding: '6px'
              }}
            >
              <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', padding: '4px 8px', fontWeight: 600 }}>Active Chapter</div>
              <button
                onClick={() => {
                  handleExport('html', false)
                  setIsExportDropdownOpen(false)
                }}
                className="dropdown-item"
                type="button"
                style={{ paddingLeft: '12px' }}
              >
                HTML (.html)
              </button>
              <button
                onClick={() => {
                  handleExport('markdown', false)
                  setIsExportDropdownOpen(false)
                }}
                className="dropdown-item"
                type="button"
                style={{ paddingLeft: '12px' }}
              >
                Markdown (.md)
              </button>
              <button
                onClick={() => {
                  handleExport('txt', false)
                  setIsExportDropdownOpen(false)
                }}
                className="dropdown-item"
                type="button"
                style={{ paddingLeft: '12px' }}
              >
                Plain Text (.txt)
              </button>

              <div style={{ height: '1px', backgroundColor: 'var(--border-color)', margin: '4px 0' }} />

              <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', padding: '4px 8px', fontWeight: 600 }}>All Chapters (Combined)</div>
              <button
                onClick={() => {
                  handleExport('html', true)
                  setIsExportDropdownOpen(false)
                }}
                className="dropdown-item"
                type="button"
                style={{ paddingLeft: '12px' }}
              >
                HTML (.html)
              </button>
              <button
                onClick={() => {
                  handleExport('markdown', true)
                  setIsExportDropdownOpen(false)
                }}
                className="dropdown-item"
                type="button"
                style={{ paddingLeft: '12px' }}
              >
                Markdown (.md)
              </button>
              <button
                onClick={() => {
                  handleExport('txt', true)
                  setIsExportDropdownOpen(false)
                }}
                className="dropdown-item"
                type="button"
                style={{ paddingLeft: '12px' }}
              >
                Plain Text (.txt)
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

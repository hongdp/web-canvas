/**
 * The book's reference files in the sidebar (docs/features/attachments_and_web.md §1):
 * what the assistant can look up as A1, A2…, with add and remove. The text
 * stays on the server; the assistant reads it in bounded parts.
 */
import React, { useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Paperclip, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { useTranslation } from '../i18n'

const ACCEPT = '.txt,.md,.markdown,.text'

export const AttachmentsSection: React.FC = () => {
  const { t } = useTranslation()
  const user = useAppStore(s => s.user)
  const activeBookId = useAppStore(s => s.activeBookId)
  const attachmentsBookId = useAppStore(s => s.attachmentsBookId)
  const list = useAppStore(s => s.attachments)
  const busy = useAppStore(s => s.attachmentsBusy)
  const error = useAppStore(s => s.attachmentsError)
  const addAttachments = useAppStore(s => s.addAttachments)
  const removeAttachment = useAppStore(s => s.removeAttachment)
  const inputRef = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(true)
  const attachments = attachmentsBookId === activeBookId ? list : []

  return (
    <div className="attachments-section">
      <div className="attachments-header">
        <button type="button" className="attachments-toggle" onClick={() => setOpen(o => !o)} title={t.sidebar.attachmentsHint}>
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <Paperclip size={13} />
          <span>{t.sidebar.attachments}{attachments.length > 0 ? ` (${attachments.length})` : ''}</span>
        </button>
        <button
          type="button"
          className="btn-icon chapter-action-btn"
          title={user ? t.sidebar.addAttachment : t.sidebar.attachmentsLogin}
          disabled={!user || busy}
          onClick={() => inputRef.current?.click()}
          style={{ padding: '0.2rem' }}
        >
          {busy ? <RefreshCw size={13} className="animate-spin" /> : <Plus size={14} />}
        </button>
        <input
          type="file"
          ref={inputRef}
          accept={ACCEPT}
          multiple
          style={{ display: 'none' }}
          onChange={(e) => {
            const files = Array.from(e.target.files ?? [])
            e.target.value = ''
            if (files.length > 0) void addAttachments(files)
          }}
        />
      </div>
      {open && (
        <div className="attachments-list">
          {!user && <div className="attachments-note">{t.sidebar.attachmentsLogin}</div>}
          {user && attachments.length === 0 && !busy && <div className="attachments-note">{t.sidebar.attachmentsHint}</div>}
          {busy && <div className="attachments-note">{t.sidebar.attachmentsUploading}</div>}
          {error && <div className="attachments-note attachments-error">{error}</div>}
          {attachments.map(a => (
            <div key={a.id} className="attachment-item" title={a.name}>
              <span className="attachment-ref">{a.ref}</span>
              <div className="attachment-text">
                <span className="attachment-name">{a.name}</span>
                <span className="attachment-meta">{t.sidebar.attachmentSize(a.chars, a.sections.length)}</span>
              </div>
              <button
                type="button"
                className="btn-icon chapter-action-btn delete"
                title={t.sidebar.removeAttachment}
                disabled={busy}
                onClick={() => {
                  if (confirm(t.sidebar.removeAttachmentConfirm.replace('{title}', a.name))) void removeAttachment(a.id)
                }}
                style={{ padding: '0.15rem' }}
              >
                <Trash2 size={13} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

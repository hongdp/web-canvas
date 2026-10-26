import { Sparkles, X } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'

interface VersionHistorySidebarProps {
  isHistoryOpen: boolean
  onClose: () => void
}

/**
 * Version history drawer for the active chapter: manual snapshots plus
 * restore/delete per version. Extracted from App.tsx unchanged (same
 * DOM/classes); open state stays in App because backdrops and the canvas
 * header toggle also drive it.
 */
export function VersionHistorySidebar({ isHistoryOpen, onClose }: VersionHistorySidebarProps) {
  const {
    versions,
    activeDocumentId,
    createVersionSnapshot,
    restoreVersion,
    deleteVersionSnapshot
  } = useAppStore()

  return (
    <aside className={`history-sidebar ${isHistoryOpen ? 'open' : 'collapsed'}`}>
      <div className="history-header">
        <h3>Version History</h3>
        <div style={{ display: 'flex', gap: '4px' }}>
          <button
            onClick={() => createVersionSnapshot('Manual Snapshot')}
            className="btn-icon"
            title="Save manual snapshot"
            type="button"
          >
            <Sparkles size={16} style={{ color: 'var(--accent)' }} />
          </button>
          <button
            onClick={onClose}
            className="btn-icon"
            title="Close history"
            type="button"
          >
            <X size={16} />
          </button>
        </div>
      </div>
      <div className="history-list">
        {versions.filter(v => v.documentId === activeDocumentId).length === 0 ? (
          <div style={{ padding: '2rem 1rem', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
            No snapshots taken yet for this chapter.
          </div>
        ) : (
          versions
            .filter(v => v.documentId === activeDocumentId)
            .map((version) => (
              <div key={version.id} className="history-item">
                <span className="history-item-title">{version.title}</span>
                <span className="history-item-time">
                  {new Date(version.timestamp).toLocaleString([], {
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit'
                  })}
                </span>
                <div className="history-item-actions">
                  <button
                    // Async now: a snapshot's content may live only on the
                    // server, and restoring the empty stub would blank the
                    // chapter the user is trying to rescue.
                    onClick={() => { void restoreVersion(version.id) }}
                    className="history-item-btn restore"
                    type="button"
                  >
                    Restore
                  </button>
                  <button
                    onClick={() => deleteVersionSnapshot(version.id)}
                    className="history-item-btn delete"
                    type="button"
                  >
                    Delete
                  </button>
                </div>
              </div>
            ))
        )}
      </div>
    </aside>
  )
}

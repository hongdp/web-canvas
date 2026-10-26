import { FilePenLine } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { useTranslation } from '../i18n'
import type { AgentTouchedChapter, AgentTurnRecord } from '../types/chat'
import type { TimelineSegment } from './chatRows'

/** A chapter still carries review markup from some turn. */
const hasPendingDiff = (html: string) => html.includes('diff-addition') || html.includes('diff-deletion')

/**
 * Open a chapter and bring its first pending change into view. The content
 * may still be loading (server books lazy-load), so the scroll waits for it.
 */
async function viewChapter(id: string): Promise<void> {
  const s = useAppStore.getState()
  s.setActiveDocumentId(id)
  await s.ensureDocumentContents([id])
  window.setTimeout(() => {
    document
      .querySelector('.ProseMirror .diff-addition, .ProseMirror .diff-deletion')
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, 150)
}

/**
 * The reply of an agentic turn, in the order it happened: each step's text,
 * then the tool calls that step made, where it made them — the step in flight
 * last while the turn runs. Replaces the plain `content` rendering for turns
 * that have a timeline.
 */
/**
 * A turn's steps in order. `segment` shows one part of a turn the chat shows
 * split at a steer (components/chatRows): the first part carries the prefix,
 * the last the live state and the suffix.
 */
export function AgentTimeline({ record, segment }: { record: AgentTurnRecord; segment?: TimelineSegment }) {
  const { t } = useTranslation()
  const running = record.status === 'running'
  const first = segment?.first ?? true
  const last = segment?.last ?? true
  const items = segment?.items ?? (record.timeline ?? []).filter(i => i.type !== 'steer')
  return (
    <div className="agent-timeline">
      {first && record.prefix && <div className="agent-timeline-text agent-timeline-prefix">{record.prefix}</div>}
      {items.map((item, i) => item.type === 'text'
        ? <div key={i} className="agent-timeline-text">{item.text}</div>
        : item.type === 'tool'
        ? <div key={i} className={`agent-tool-line${item.ok ? '' : ' failed'}`}>{item.line}</div>
        : null
      )}
      {last && running && (record.live
        ? <div className="agent-timeline-text">{record.live}</div>
        : <div className="agent-tool-line pending">{t.agent.running}</div>)}
      {last && record.suffix && <div className="agent-timeline-text">{record.suffix}</div>}
    </div>
  )
}

/**
 * What an agentic turn changed, under its reply (agentic_chat_loop.md D2,
 * §5.6): one row per chapter — open it at its first diff. Turns recorded
 * before the timeline existed also get their steps as a collapsed list.
 */
export function AgentTurnSummary({ record }: { record: AgentTurnRecord }) {
  const { t } = useTranslation()
  const documents = useAppStore(state => state.documents)
  const activeDocumentId = useAppStore(state => state.activeDocumentId)

  const kindLabel = (row: AgentTouchedChapter) =>
    row.kind === 'created' ? t.agent.kindCreated
      : row.kind === 'rewrite' ? t.agent.kindRewrite
      : row.kind === 'selection' ? t.agent.kindSelection
      : row.kind === 'polished' ? t.agent.kindPolished(row.changes)
      : row.kind === 'renamed' ? t.agent.kindRenamed
      : t.agent.kindEdits(row.changes)

  const plan = record.plan ?? []
  if (record.touched.length === 0 && plan.length === 0 && (record.timeline || record.trace.length === 0)) return null
  return (
    <div className="agent-turn">
      {plan.length > 0 && (
        <ul className="agent-plan">
          {plan.map(item => (
            <li key={item.id} className={`agent-plan-item ${item.status}`}>
              <span className="agent-plan-mark">{item.status === 'done' ? '☑' : item.status === 'in_progress' ? '▶' : item.status === 'dropped' ? '✕' : '☐'}</span>
              <span className="agent-plan-title">{item.title}</span>
            </li>
          ))}
        </ul>
      )}
      {record.touched.length > 0 && (
        <div className="agent-touched">
          <div className="agent-touched-title">{t.agent.changedThisTurn}</div>
          {record.touched.map(row => {
            const doc = documents.find(d => d.id === row.documentId)
            // Read from the chapter as it is NOW, so an accept or reject shows.
            // A chapter whose content has not loaded cannot be judged.
            const status = !doc ? null
              : doc.contentLoaded === false && !doc.content ? null
              : hasPendingDiff(doc.content) ? 'pending' : 'resolved'
            return (
              // Title and what happened stack on the left (the title truncates);
              // status and View are one group that wraps below as a unit when
              // the bubble is narrow, instead of pushing past its edge.
              <div key={row.documentId} className={`agent-touched-row${doc ? '' : ' deleted'}`}>
                <FilePenLine size={13} className="agent-touched-icon" />
                <div className="agent-touched-main">
                  <span className="agent-touched-name" title={doc?.title ?? row.titleAtRun}>
                    {doc?.title ?? row.titleAtRun}
                  </span>
                  <span className="agent-touched-kind">
                    {kindLabel(row)}
                    {row.failed > 0 && (
                      <span className="agent-touched-failed">
                        {' · '}
                        {/* A polish chunk that failed its checks kept its draft; only an edit is "not located". */}
                        {row.kind === 'polished' ? t.agent.keptDraft(row.failed) : t.agent.failed(row.failed)}
                      </span>
                    )}
                  </span>
                </div>
                <div className="agent-touched-actions">
                {!doc ? (
                  <span className="agent-touched-status">{t.agent.deleted}</span>
                ) : (
                  <>
                    {status && (
                      <span className={`agent-touched-status ${status}`}>
                        {status === 'pending' ? t.agent.pending : t.agent.resolved}
                      </span>
                    )}
                    <button
                      type="button"
                      className="agent-touched-view"
                      onClick={() => { void viewChapter(row.documentId) }}
                      disabled={row.documentId === activeDocumentId && status !== 'pending'}
                    >
                      {t.agent.view}
                    </button>
                  </>
                )}
                </div>
              </div>
            )
          })}
        </div>
      )}
      {!record.timeline && record.trace.length > 0 && (
        <details className="agent-trace">
          <summary>
            {t.agent.trace(record.steps, record.trace.length)}
            {record.status === 'running' ? ` · ${t.agent.running}` : ''}
          </summary>
          <ol>
            {record.trace.map((line, i) => <li key={i}>{line}</li>)}
          </ol>
        </details>
      )}
    </div>
  )
}

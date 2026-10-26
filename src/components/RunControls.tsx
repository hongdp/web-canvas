import { useState } from 'react'
import { useAppStore } from '../store/useAppStore'
import { useTranslation } from '../i18n'
import type { AgentTurnRecord, ChapterDeletionConfirm } from '../types/chat'
import type { ServerRunAction } from '../services/serverRuns'

/**
 * Under a bubble: a server-side run's queue and pause controls
 * (backend_authority.md §6.3 and "Runaway runs"), and the question a run
 * put to the user with `ask_user` — as choices and a free answer. A paused
 * server run continues with the answer; a tab-run that ended asking gets the
 * answer as the next message.
 *
 * A question delete_chapter asked carries `confirm`: it shows Delete / Keep
 * and no free answer, because only the Delete button may delete.
 */
export function RunControls({ record, onAction, onAnswer, disabled }: {
  record: AgentTurnRecord
  onAction: (runId: string, action: ServerRunAction) => void
  onAnswer: (record: AgentTurnRecord, answer: string, confirm?: ChapterDeletionConfirm) => void
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const language = useAppStore(state => state.language)
  const [free, setFree] = useState('')
  const run = record.run
  const serverQuestion = run?.status === 'paused' && run.pause?.reason === 'question'
    ? { question: run.pause.question ?? run.pause.message, options: run.pause.options ?? [], confirm: run.pause.confirm }
    : null
  const question = serverQuestion ?? (record.question && !run ? record.question : null)

  if (question?.confirm) {
    const c = question.confirm
    return (
      <div className="run-controls">
        <div className="run-controls-text">❓ {t.agent.confirmDelete(c.number, c.title, c.chars)}</div>
        <div className="run-controls-actions">
          <button type="button" className="agent-touched-view" disabled={disabled} onClick={() => onAnswer(record, t.agent.deletedAnswer(c.number, c.title), c)}>{t.agent.deleteChapter}</button>
          <button type="button" className="agent-touched-view" disabled={disabled} onClick={() => onAnswer(record, t.agent.keptAnswer(c.number, c.title))}>{t.agent.keepChapter}</button>
        </div>
      </div>
    )
  }

  if (question) {
    return (
      <div className="run-controls">
        <div className="run-controls-text">❓ {question.question}</div>
        <div className="run-controls-actions">
          {question.options.map(option => (
            <button key={option} type="button" className="agent-touched-view" disabled={disabled} onClick={() => onAnswer(record, option)}>{option}</button>
          ))}
        </div>
        <form className="run-controls-actions" onSubmit={e => { e.preventDefault(); if (free.trim()) { onAnswer(record, free.trim()); setFree('') } }}>
          <input className="form-input" value={free} disabled={disabled} placeholder={t.agent.answerPlaceholder} onChange={e => setFree(e.target.value)} />
          <button type="submit" className="agent-touched-view" disabled={disabled || !free.trim()}>{t.agent.answer}</button>
        </form>
      </div>
    )
  }

  if (!run || (run.status !== 'queued' && run.status !== 'paused')) return null
  const pause = run.pause
  const reason = pause?.reason === 'repeating' ? t.agent.pausedRepeating
    : pause?.reason === 'unattended' ? t.agent.pausedUnattended
    : pause?.reason === 'token_budget' ? t.agent.pausedBudget
    : pause?.reason === 'step_failed' ? t.agent.pausedStepFailed(pause.message)
    : pause?.reason === 'repeating_output' ? t.agent.pausedRepeatingOutput
    : pause?.message ?? ''
  return (
    <div className="run-controls">
      <div className="run-controls-text">
        {run.status === 'queued' ? t.agent.queued(run.position ?? 0) : `${t.agent.paused} ${reason}`}
        {run.status === 'paused' && pause?.steps && pause.steps.length > 0 && (
          <details className="run-controls-steps">
            <summary>{t.agent.pausedSteps}</summary>
            <ol>
              {pause.steps.map((s, i) => (
                <li key={i}>
                  <div>{s.calls.join(', ') || (language === 'zh' ? '（无调用）' : '(no calls)')}</div>
                  {s.reasoning && <div className="run-controls-reasoning">{s.reasoning}</div>}
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
      <div className="run-controls-actions">
        {run.status === 'queued' ? (
          <>
            <button type="button" className="agent-touched-view" onClick={() => onAction(run.id, 'start')}>{t.agent.sendNow}</button>
            <button type="button" className="agent-touched-view" onClick={() => onAction(run.id, 'remove')}>{t.agent.remove}</button>
          </>
        ) : (
          <>
            <button type="button" className="agent-touched-view" onClick={() => onAction(run.id, 'resume')}>{t.agent.resume}</button>
            <button type="button" className="agent-touched-view" onClick={() => onAction(run.id, 'stop')}>{t.agent.abandon}</button>
          </>
        )}
      </div>
    </div>
  )
}

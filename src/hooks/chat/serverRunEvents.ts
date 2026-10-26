/**
 * How a server-side run's events change the chat messages (pure). The hook
 * applies these to the store and does the editor side (previews, locks,
 * opening chapters) itself.
 *
 * One rule throughout: the server's record is the truth for a run's bubble.
 * The bubble this tab painted mid-turn is replaced, never merged, by the
 * record the next `step`, `paused` or `finished` event carries.
 */
import type { AgentTurnRecord, ChatMessage } from '../../types/chat'
import type { ServerRunEvent, ServerRunResult, ServerRunSummary } from '../../services/serverRuns'
import { ASSISTANT_PLACEHOLDER, splitStreamingResponse } from './streamHandlers'

/** The step in flight, per run: its raw text so far and the progress line. */
export interface RunLive {
  text: string
  progress: string | null
  /** This tab kept the half-streamed draft when it pressed Stop. */
  keptDraft?: boolean
}

export const KEPT_DRAFT_NOTE = '⏹️ Stopped. The partial draft was kept in the document — Undo (Ctrl+Z) restores the previous version.'

const linkOf = (run: ServerRunSummary, position?: number): AgentTurnRecord['run'] => ({
  id: run.id,
  status: run.status,
  ...(position !== undefined ? { position } : {}),
  ...(run.pause ? { pause: run.pause } : {})
})

/** The bubble's record for a run that has not finished: the server's, with the run link. */
function runningRecord(run: ServerRunSummary, status: AgentTurnRecord['status'], position?: number): AgentTurnRecord {
  const base = run.record ?? { status: 'running', steps: 0, trace: [], touched: [], timeline: [] }
  return { ...base, status, live: undefined, run: linkOf(run, position) }
}

/**
 * The turn's two messages exist in this tab. A run started from another
 * device, or found again after a reload, may have neither.
 */
export function ensureRunMessages(messages: ChatMessage[], run: ServerRunSummary): ChatMessage[] {
  let out = messages
  const now = new Date().toISOString()
  if (run.userMessageId && !out.some(m => m.id === run.userMessageId)) {
    out = [...out, { id: run.userMessageId, role: 'user', content: run.prompt ?? '', timestamp: run.createdAt ?? now }]
  }
  if (run.assistantMessageId && !out.some(m => m.id === run.assistantMessageId)) {
    out = [...out, { id: run.assistantMessageId, role: 'assistant', content: ASSISTANT_PLACEHOLDER, timestamp: run.createdAt ?? now }]
  }
  return out
}

const update = (messages: ChatMessage[], id: string | null | undefined, fn: (m: ChatMessage) => ChatMessage): ChatMessage[] =>
  id ? messages.map(m => (m.id === id ? fn(m) : m)) : messages

/** The bubble while the run goes: finished steps from the record, the step in flight after them. */
function paint(m: ChatMessage, live: RunLive | undefined): ChatMessage {
  const chat = live ? splitStreamingResponse(live.text).chatText.trim() : ''
  const liveLine = [chat, live?.progress ?? ''].filter(Boolean).join('\n\n')
  const agent = m.agent ? { ...m.agent, live: liveLine || undefined } : m.agent
  const finished = (m.agent?.timeline ?? []).flatMap(item => (item.type === 'text' ? [item.text] : [])).join('\n\n')
  const body = [finished, liveLine].filter(Boolean).join('\n\n') || (m.content === ASSISTANT_PLACEHOLDER ? ASSISTANT_PLACEHOLDER : 'Updating document...')
  const prefix = m.agent?.prefix
  return { ...m, agent, content: prefix ? `${prefix}\n\n${body}` : body }
}

/**
 * Apply one event to the messages. `live` is the hook's per-run map of the
 * step in flight; it is updated here too (deltas, progress, step boundaries).
 */
export function applyRunEvent(messages: ChatMessage[], event: ServerRunEvent, live: Map<string, RunLive>): ChatMessage[] {
  const run = event.run
  switch (event.kind) {
    case 'queued': {
      if (!run) return messages
      const withBubbles = ensureRunMessages(messages, run)
      return update(withBubbles, run.assistantMessageId, m => ({ ...m, agent: runningRecord(run, 'queued', event.position) }))
    }
    case 'started': {
      if (!run) return messages
      live.set(run.id, { text: '', progress: null })
      const withBubbles = ensureRunMessages(messages, run)
      return update(withBubbles, run.assistantMessageId, m => paint({ ...m, agent: runningRecord(run, 'running') }, live.get(run.id)))
    }
    case 'step_started': {
      const entry = live.get(event.runId) ?? { text: '', progress: null }
      live.set(event.runId, { ...entry, text: '', progress: null })
      return messages
    }
    case 'delta': {
      const entry = live.get(event.runId) ?? { text: '', progress: null }
      entry.text += event.text ?? ''
      live.set(event.runId, entry)
      return update(messages, bubbleOf(messages, event.runId), m => paint(m, entry))
    }
    case 'progress': {
      const entry = live.get(event.runId) ?? { text: '', progress: null }
      entry.progress = event.line ?? null
      live.set(event.runId, entry)
      return update(messages, bubbleOf(messages, event.runId), m => paint(m, entry))
    }
    case 'corrective': {
      const why = event.failure === 'malformed'
        ? 'That reply used a document-edit format I could not apply'
        : event.failure === 'undeclared'
          ? 'That reply skipped the required status declaration'
          : 'That reply said the document was updated but sent no update'
      live.set(event.runId, { text: '', progress: null })
      return update(messages, bubbleOf(messages, event.runId), m => ({ ...m, content: `🔁 ${why} — retrying (${event.attempt}/${event.max})…` }))
    }
    case 'step': {
      live.set(event.runId, { text: '', progress: null })
      return update(messages, bubbleOf(messages, event.runId), m => paint(
        { ...m, agent: { ...(event.record as AgentTurnRecord), status: 'running', run: { ...(m.agent?.run ?? { id: event.runId }), id: event.runId, status: 'running' } } },
        live.get(event.runId)
      ))
    }
    case 'paused': {
      live.delete(event.runId)
      return update(messages, bubbleOf(messages, event.runId), m => {
        const record: AgentTurnRecord = { ...(event.record as AgentTurnRecord), status: 'paused', live: undefined,
          run: { id: event.runId, status: 'paused', ...(event.pause ? { pause: event.pause } : {}) } }
        const finished = (record.timeline ?? []).flatMap(item => (item.type === 'text' ? [item.text] : [])).join('\n\n')
        return { ...m, agent: record, content: [record.prefix, finished].filter(Boolean).join('\n\n') || m.content }
      })
    }
    case 'finished': {
      const result = event.result as ServerRunResult | undefined
      const kept = live.get(event.runId)?.keptDraft
      live.delete(event.runId)
      if (!result) return messages
      const withBubbles = run ? ensureRunMessages(messages, run) : messages
      return update(withBubbles, run?.assistantMessageId ?? bubbleOf(withBubbles, event.runId), m => {
        const content = kept && event.status === 'stopped' ? result.content.replace(/⏹️ Stopped\.$/, KEPT_DRAFT_NOTE) : result.content
        const record: AgentTurnRecord = { ...result.record, live: undefined, run: { id: event.runId, status: event.status ?? 'done' } }
        const hasRecord = record.trace.length > 0 || record.touched.length > 0 || (record.timeline ?? []).length > 0
        return {
          ...m, content,
          ...(hasRecord ? { agent: record } : { agent: undefined }),
          ...(result.reasoningItems?.length ? { reasoningItems: result.reasoningItems } : {})
        }
      })
    }
    default:
      return messages
  }
}

/** The assistant bubble a run streams into. */
export function bubbleOf(messages: ChatMessage[], runId: string): string | undefined {
  return messages.find(m => m.role === 'assistant' && m.agent?.run?.id === runId)?.id
}

/** A bubble that still waits on a run: the run finished without this tab seeing it. */
export function bubbleStillWaiting(m: ChatMessage): boolean {
  return m.role === 'assistant' && (m.content === ASSISTANT_PLACEHOLDER || m.agent?.status === 'running' || m.agent?.status === 'queued' || m.agent?.status === 'paused')
}

/** A run's token usage as its events report it: cumulative over the run. */
export interface RunUsage { promptTokens: number; completionTokens: number; cachedPromptTokens: number }

/**
 * What a run spent since the last report this tab counted: the footer's
 * session counter adds it (pinned_context.md §3). Server runs used to add
 * nothing, so the counter stayed at 0 with serverRuns on. Never negative: a
 * report older than the one counted (a replay) adds nothing.
 */
export function runUsageDelta(
  previous: RunUsage | undefined,
  current: { promptTokens?: number; completionTokens?: number; cachedPromptTokens?: number } | undefined
): { delta: RunUsage; total: RunUsage } | null {
  if (!current) return null
  const total: RunUsage = {
    promptTokens: current.promptTokens ?? 0,
    completionTokens: current.completionTokens ?? 0,
    cachedPromptTokens: current.cachedPromptTokens ?? 0
  }
  const before = previous ?? { promptTokens: 0, completionTokens: 0, cachedPromptTokens: 0 }
  const delta: RunUsage = {
    promptTokens: Math.max(0, total.promptTokens - before.promptTokens),
    completionTokens: Math.max(0, total.completionTokens - before.completionTokens),
    cachedPromptTokens: Math.max(0, total.cachedPromptTokens - before.cachedPromptTokens)
  }
  if (delta.promptTokens === 0 && delta.completionTokens === 0) return null
  return { delta, total }
}

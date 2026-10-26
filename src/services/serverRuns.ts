/**
 * The server-side run API (backend_authority.md §4.3, scripts/server_runs.py).
 *
 * With `ProviderConfig.serverRuns` on, a chat turn is posted here and the
 * loop runs in the API process; this tab renders the `run.*` events that
 * arrive on the book's event stream (store/bookEvents → store/runEvents).
 */
import type { AgentTurnRecord } from '../types/chat'
import type { ProviderConfig } from '../types/llm'
import { useAppStore } from '../store/useAppStore'
import { CLIENT_ID, CLIENT_ID_HEADER } from '../store/documentSync'

export type ServerRunStatus = 'queued' | 'running' | 'paused' | 'done' | 'stopped' | 'error'

export interface ServerRunSummary {
  id: string
  bookId: string
  status: ServerRunStatus
  createdAt: string
  updatedAt: string
  finishedAt?: string | null
  userMessageId?: string | null
  assistantMessageId?: string | null
  prompt?: string
  activeDocumentId?: string
  selectedText?: string
  clientId?: string | null
  record: AgentTurnRecord
  pause?: AgentTurnRecord['run'] extends infer R ? (R extends { pause?: infer P } ? P : never) : never
  error?: string | null
}

export interface ServerRunResult {
  content: string
  record: AgentTurnRecord
  reasoningItems?: unknown[]
  readIds?: string[]
  usage?: { promptTokens?: number; completionTokens?: number; cachedPromptTokens?: number }
  snapshots?: Array<{ id: string; documentId: string; title: string; timestamp: string }>
}

export interface ServerRunDetail extends ServerRunSummary {
  liveText?: string
  liveReasoning?: string
  result?: ServerRunResult | null
  snapshots?: ServerRunResult['snapshots']
}

/** What the server needs to run one turn: the request and the conversation before it. */
export interface ServerRunRequest {
  prompt: string
  images?: string[]
  provider: string
  config: Partial<ProviderConfig>
  activeDocumentId: string
  selectedText?: string
  history: Array<{ id: string; role: 'user' | 'assistant'; content: string; images?: string[]; agent?: AgentTurnRecord; reasoningItems?: unknown[] }>
  userMessageId: string
  assistantMessageId: string
  customInstructions?: string
  polishPrompt?: { system: string; template: string }
  contextWindowTokens?: number
  clientId?: string
  /** The Polish button's run: this chapter through the polish model, no agent loop. */
  polish?: { documentId: string; language: 'zh' | 'en' }
}

/** One `run.*` event as the book's event stream carries it. */
export interface ServerRunEvent {
  id?: number
  type: 'run'
  kind: 'started' | 'queued' | 'step_started' | 'delta' | 'reasoning' | 'preview' | 'preview_selection' | 'progress' | 'lock' | 'open' |
    'corrective' | 'step' | 'paused' | 'finished' | string
  runId: string
  run?: ServerRunSummary
  position?: number
  step?: number
  final?: boolean
  text?: string
  documentId?: string
  html?: string | null
  settled?: boolean
  line?: string | null
  documentIds?: string[]
  failure?: string
  attempt?: number
  max?: number
  record?: AgentTurnRecord
  pause?: NonNullable<AgentTurnRecord['run']>['pause']
  status?: ServerRunStatus
  result?: ServerRunResult
  resumed?: boolean
  /** On `step`: the run's usage so far (cumulative), for the footer's session counter. */
  usage?: ServerRunResult['usage']
}

function headers(): Record<string, string> {
  return { 'Content-Type': 'application/json', 'X-CSRF-Token': useAppStore.getState().csrfToken || '', [CLIENT_ID_HEADER]: CLIENT_ID }
}

async function expectOk(res: Response, what: string): Promise<unknown> {
  if (!res.ok) {
    let detail = ''
    try { detail = ((await res.json()) as { detail?: string }).detail ?? '' } catch { /* not JSON */ }
    throw new Error(`${what} failed (${res.status})${detail ? `: ${detail}` : ''}`)
  }
  return res.json()
}

export async function startServerRun(bookId: string, request: ServerRunRequest, fetchFn: typeof fetch = fetch): Promise<{ run: ServerRunSummary; position: number; queueHeld: boolean }> {
  const res = await fetchFn(`/api/books/${encodeURIComponent(bookId)}/runs`, { method: 'POST', headers: headers(), body: JSON.stringify(request) })
  return expectOk(res, 'Starting the run') as Promise<{ run: ServerRunSummary; position: number; queueHeld: boolean }>
}

export async function listServerRuns(bookId: string, fetchFn: typeof fetch = fetch): Promise<{ runs: ServerRunDetail[]; queueHeld: boolean }> {
  const res = await fetchFn(`/api/books/${encodeURIComponent(bookId)}/runs`)
  return expectOk(res, 'Listing runs') as Promise<{ runs: ServerRunDetail[]; queueHeld: boolean }>
}

export async function getServerRun(bookId: string, runId: string, fetchFn: typeof fetch = fetch): Promise<ServerRunDetail> {
  const res = await fetchFn(`/api/books/${encodeURIComponent(bookId)}/runs/${encodeURIComponent(runId)}`)
  return ((await expectOk(res, 'Reading the run')) as { run: ServerRunDetail }).run
}

export type ServerRunAction = 'stop' | 'resume' | 'start' | 'remove'

/** Stop (pauses the queue), resume a paused run, start a queued one now, or remove a queued/paused one. */
export async function serverRunAction(bookId: string, runId: string, action: ServerRunAction, fetchFn: typeof fetch = fetch): Promise<void> {
  const base = `/api/books/${encodeURIComponent(bookId)}/runs/${encodeURIComponent(runId)}`
  const res = action === 'remove'
    ? await fetchFn(base, { method: 'DELETE', headers: headers() })
    : await fetchFn(`${base}/${action}`, { method: 'POST', headers: headers() })
  await expectOk(res, `Run ${action}`)
}

/** A message sent while the run works: the run takes it as its next user message (409 when it is not running). */
export async function steerServerRun(bookId: string, runId: string, text: string, messageId?: string, fetchFn: typeof fetch = fetch): Promise<void> {
  const res = await fetchFn(`/api/books/${encodeURIComponent(bookId)}/runs/${encodeURIComponent(runId)}/steer`, {
    method: 'POST', headers: headers(), body: JSON.stringify(messageId ? { text, messageId } : { text })
  })
  await expectOk(res, 'Steering the run')
}

/**
 * The user's answer to a run paused on a question; the run continues with it.
 * `confirm` is set only by the Delete button of a delete_chapter question: the
 * server deletes the chapter before the run goes on.
 */
export async function answerServerRun(bookId: string, runId: string, answer: string, confirm = false, fetchFn: typeof fetch = fetch): Promise<void> {
  const res = await fetchFn(`/api/books/${encodeURIComponent(bookId)}/runs/${encodeURIComponent(runId)}/answer`, {
    method: 'POST', headers: headers(), body: JSON.stringify(confirm ? { answer, confirm: true } : { answer })
  })
  await expectOk(res, 'Answering the run')
}

/** The chapter this tab shows now; the run follows the user's view (agentic_chat_loop.md §0.4). */
export async function reportRunView(bookId: string, runId: string, documentId: string, fetchFn: typeof fetch = fetch): Promise<void> {
  try {
    await fetchFn(`/api/books/${encodeURIComponent(bookId)}/runs/${encodeURIComponent(runId)}/view`, {
      method: 'POST', headers: headers(), body: JSON.stringify({ documentId })
    })
  } catch (e) {
    console.warn('[serverRuns] could not report the view', e)
  }
}

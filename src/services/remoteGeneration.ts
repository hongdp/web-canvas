/**
 * Remote (server-side) generation transport —
 * see docs/features/resumable_generation.md.
 *
 * A browser tab is not a safe host for a long generation: mobile Firefox
 * discards a backgrounded tab and the provider connection dies with it, so
 * everything produced so far is lost. When the user is logged in the
 * generation therefore runs as a job inside the FastAPI backend and the tab
 * is only a reader that may detach and re-attach at any character offset.
 *
 * The job buffer on the server is the source of truth; this module keeps just
 * enough in localStorage (`{ jobId, meta, offset }`) to find its way back
 * after the tab is destroyed.
 */
import type { LLMMessage, ProviderConfig, StreamCallbacks, ThinkingBlock } from '../types/llm'
import { localStorage } from '../store/persistence'
import { useAppStore } from '../store/useAppStore'
import { readSSEDataLines } from './llm'

/** Opaque-to-the-server job description, echoed back by `/api/generate/active`. */
export interface RemoteJobMeta {
  bookId?: string
  documentId?: string
  /** Chat bubble this job streams into — the anchor a rejoin needs. */
  assistantMessageId?: string
  kind?: 'chat' | 'summary' | 'batch'
  /**
   * The text the user had selected when the turn started, for a
   * <selection_replace> edit. Positions cannot survive a reload — the range
   * lived in a ref — but TEXT can be found again, which is how <edit> blocks
   * have always located themselves. Without this a selection rewrite that
   * finished after a refresh had nowhere to go and was dropped in silence.
   */
  selectedText?: string
}

export interface PersistedGenerationJob {
  jobId: string
  meta: RemoteJobMeta
  /** Characters of the job buffer this client has already rendered. */
  offset: number
}

export interface RemoteJobInfo {
  jobId: string
  status: 'running' | 'done' | 'error' | 'aborted'
  meta?: RemoteJobMeta
  length?: number
  createdAt?: string
  updatedAt?: string
}

/** Namespaced like every other client-owned key (see store/persistence.ts). */
const ACTIVE_JOB_KEY = 'web_canvas_active_generation'

/**
 * Thrown only when the job could not be STARTED (backend down, 4xx/5xx, no
 * jobId). `streamLLM` reports it as the call's error; there is no other
 * transport to fall back to.
 */
export class RemoteStartError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'RemoteStartError'
  }
}

/**
 * The stream endpoint ANSWERED, and the answer was no (404: the job is gone,
 * 401: the session is). Kept apart from a transport failure because only the
 * latter is worth a re-attach — retrying a refusal just asks twice.
 */
class StreamRefusedError extends Error {
  constructor(status: number, statusText: string) {
    super(`Generation stream failed (${status}): ${statusText}`)
    this.name = 'StreamRefusedError'
  }
}

// The job this tab is currently reading. Module-level (single owner) so the
// stop button can abort it without threading the id through the UI.
let activeJobId: string | null = null

// ── Persisted job record ──────────────────────────────────────────────────────

export function readPersistedJob(): PersistedGenerationJob | null {
  const raw = localStorage.getItem(ACTIVE_JOB_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedGenerationJob>
    if (!parsed || typeof parsed.jobId !== 'string') return null
    return {
      jobId: parsed.jobId,
      meta: parsed.meta && typeof parsed.meta === 'object' ? parsed.meta : {},
      offset: typeof parsed.offset === 'number' && parsed.offset >= 0 ? parsed.offset : 0
    }
  } catch {
    // Corrupt record: treat it as absent rather than blocking generation.
    return null
  }
}

function writePersistedJob(job: PersistedGenerationJob): void {
  localStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(job))
}

export function clearPersistedJob(): void {
  localStorage.removeItem(ACTIVE_JOB_KEY)
}

// ── Requests ──────────────────────────────────────────────────────────────────

/** Same cookie session + double-submit CSRF header the store's writes send. */
function apiHeaders(): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    'X-CSRF-Token': useAppStore.getState().csrfToken || ''
  }
}

/**
 * True when generation should run on the backend: the endpoints are
 * session-scoped, so a logged-out client has nowhere to put the job.
 * Read lazily (never at module load) to keep the store graph out of the
 * import cycle.
 */
/**
 * Re-attach attempts after a stream ends without a terminal event. One is
 * enough to ride out a proxy hiccup; a job that no longer exists fails its
 * retry immediately (404), so this costs nothing when the server really died.
 */
const MAX_STREAM_RECONNECTS = 1
/** The pause before a re-attach. Mutable only so tests need not sleep through it. */
export const streamReconnect = { delayMs: 500 }

/**
 * A `thinking_block` event's payload, checked field by field: it is replayed
 * to Anthropic verbatim, and a malformed block there is a 400 for the whole
 * next step — better to drop it here and let the provider say what is missing.
 */
function parseThinkingBlock(raw: unknown): ThinkingBlock | null {
  if (!raw || typeof raw !== 'object') return null
  const block = raw as Record<string, unknown>
  const after = block.afterText === true ? { afterText: true as const } : {}
  if (block.type === 'thinking' && typeof block.thinking === 'string' && typeof block.signature === 'string') {
    return { type: 'thinking', thinking: block.thinking, signature: block.signature, ...after }
  }
  if (block.type === 'redacted_thinking' && typeof block.data === 'string') {
    return { type: 'redacted_thinking', data: block.data, ...after }
  }
  return null
}

export function isRemoteGenerationAvailable(): boolean {
  return !!useAppStore.getState().user
}

/**
 * Attach to a job's SSE stream from `fromOffset` and map its events onto the
 * shared StreamCallbacks contract. Never throws: transport problems are
 * reported through `onError`, exactly like the direct provider paths.
 */
async function attachToJob(
  jobId: string,
  fromOffset: number,
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
  reconnectsLeft: number = MAX_STREAM_RECONNECTS,
  carryText: string = '',
  carryThinkingBlocks: number = 0,
  carryResponseItems: number = 0
): Promise<void> {
  let offset = fromOffset
  // Thinking blocks already handed to onThinkingBlock by THIS logical attach.
  // Every attach replays the job's blocks from the first (they have no text
  // offset), so a mid-turn reconnect would otherwise deliver them twice — and
  // a duplicated block makes Anthropic reject the replay. Carried forward
  // like fullText; a fresh resume starts at 0 and gets them all.
  let thinkingBlocksSeen = carryThinkingBlocks
  // grok's output items: the same replay-from-the-first rule, the same guard.
  let responseItemsSeen = carryResponseItems
  // Text THIS reader rendered. On a resume it deliberately excludes the
  // replayed-before-the-offset prefix — onDone reports what was streamed here.
  // A mid-turn reconnect carries it forward: one logical attach must report
  // one whole answer, or the caller would parse only the tail.
  let fullText = carryText
  let terminal = false

  try {
    const response = await fetch(
      `/api/generate/${encodeURIComponent(jobId)}/stream?from=${offset}`,
      { signal }
    )
    if (!response.ok) {
      throw new StreamRefusedError(response.status, response.statusText)
    }

    await readSSEDataLines(response, (dataString) => {
      if (!dataString || dataString === '[DONE]') return
      let event: {
        type?: string
        text?: string
        index?: number
        id?: string
        name?: string
        replay?: boolean
        signature?: string
        block?: unknown
        item?: unknown
        offset?: number
        message?: string
        usage?: { promptTokens: number; completionTokens: number; cachedPromptTokens?: number; reasoningTokens?: number }
      }
      try {
        event = JSON.parse(dataString)
      } catch (e) {
        console.warn('[RemoteGeneration] Failed to parse SSE payload', e, dataString)
        return
      }

      if (event.type === 'attached') {
        // The stream is live but the model has not spoken yet. Sent so the UI
        // can tell "connected, waiting" from "nothing is happening" — see the
        // header-flush note in server_generation._job_event_stream.
        callbacks.onAttached?.()
      } else if (event.type === 'tool_call') {
        // Live tool-call arguments: the document preview reads a partially
        // written `html` value out of them. Never part of fullText and never
        // an offset — the same rules reasoning follows.
        callbacks.onToolCallDelta?.({
          index: typeof event.index === 'number' ? event.index : 0,
          id: event.id,
          name: event.name,
          argumentsText: event.text || '',
          // A replay carries the whole call; appending it would duplicate.
          replace: event.replay === true,
          // Gemini thoughtSignature; only added when sent, so deltas for
          // every other provider keep their exact shape.
          ...(typeof event.signature === 'string' && event.signature ? { signature: event.signature } : {})
        })
      } else if (event.type === 'thinking_block') {
        // A completed Anthropic reasoning block, for the replay. `index` is
        // its position in the job; anything below what was already delivered
        // is a reconnect's replay of a block this caller has.
        const position = typeof event.index === 'number' ? event.index : thinkingBlocksSeen
        if (position < thinkingBlocksSeen) return
        const block = parseThinkingBlock(event.block)
        if (!block) return
        thinkingBlocksSeen = position + 1
        callbacks.onThinkingBlock?.(block)
      } else if (event.type === 'response_item') {
        // A completed grok output item (reasoning ciphertext above all), for
        // the next step. Skipped when a reconnect replays one already given.
        const position = typeof event.index === 'number' ? event.index : responseItemsSeen
        if (position < responseItemsSeen) return
        if (!event.item || typeof event.item !== 'object') return
        responseItemsSeen = position + 1
        callbacks.onResponseItem?.(event.item)
      } else if (event.type === 'reasoning') {
        // Thinking, not text: it never joins fullText and never advances the
        // offset, so a reconnect simply misses what was thought while away.
        if (event.text) callbacks.onReasoning?.(event.text)
      } else if (event.type === 'delta') {
        const text = event.text || ''
        if (!text) return
        fullText += text
        callbacks.onChunk(text)
        // The server sends the buffer length AFTER this event; advancing the
        // persisted offset only once the chunk is rendered is what makes a
        // reconnect gap-free and duplicate-free.
        offset = typeof event.offset === 'number' ? event.offset : offset + text.length
        persistOffset(jobId, offset)
      } else if (event.type === 'done') {
        terminal = true
        clearActiveJob(jobId)
        callbacks.onDone(fullText, event.usage)
      } else if (event.type === 'error') {
        terminal = true
        clearActiveJob(jobId)
        callbacks.onError(new Error(event.message || 'Remote generation failed'))
      }
    }, signal)

    if (!terminal) {
      // Body ended without a terminal event: the connection dropped, or the
      // server went away. The job may well still be generating, so re-attach
      // once from what was rendered — a transient drop then costs nothing but
      // a pause, and only a job that is really gone becomes an error.
      if (reconnectsLeft > 0 && !signal?.aborted) {
        await new Promise(resolve => setTimeout(resolve, streamReconnect.delayMs))
        return attachToJob(jobId, offset, callbacks, signal, reconnectsLeft - 1, fullText, thinkingBlocksSeen, responseItemsSeen)
      }
      // Keep the persisted record: a later page load can still pick the job up
      // if it survived. Reporting beats pretending a truncated answer is whole.
      callbacks.onError(new Error('Generation stream disconnected before completion'))
    }
  } catch (error) {
    if (terminal) return
    // Problem: a connection that died by THROWING (a thawed mobile tab finds
    //   its socket gone: "network error") went straight to onError, while one
    //   that died by ENDING got a re-attach. Same event, opposite outcomes —
    //   and the throwing kind reverted the document and showed an error for a
    //   job that was still generating on the server.
    // Fix: both take the re-attach. Not when the caller aborted (Stop must
    //   stop), and not when the server refused (a 404 will 404 again).
    const retryable = !signal?.aborted && !(error instanceof StreamRefusedError)
    if (retryable && reconnectsLeft > 0) {
      await new Promise(resolve => setTimeout(resolve, streamReconnect.delayMs))
      return attachToJob(jobId, offset, callbacks, signal, reconnectsLeft - 1, fullText, thinkingBlocksSeen, responseItemsSeen)
    }
    callbacks.onError(error instanceof Error ? error : new Error(String(error)))
  }
}

function persistOffset(jobId: string, offset: number): void {
  const persisted = readPersistedJob()
  if (!persisted || persisted.jobId !== jobId) return
  writePersistedJob({ ...persisted, offset })
}

function clearActiveJob(jobId: string): void {
  if (activeJobId === jobId) activeJobId = null
  const persisted = readPersistedJob()
  if (!persisted || persisted.jobId === jobId) clearPersistedJob()
}

/**
 * Start a backend generation job and stream it into `callbacks`.
 *
 * Throws {@link RemoteStartError} when the job could never be created. Everything
 * after a successful start is reported through the callbacks.
 */
export async function startRemoteGeneration(
  messages: LLMMessage[],
  config: ProviderConfig & { provider: string; conversationId?: string; tools?: unknown[] },
  meta: RemoteJobMeta,
  callbacks: StreamCallbacks,
  signal?: AbortSignal
): Promise<void> {
  let jobId: string
  try {
    const response = await fetch('/api/generate', {
      method: 'POST',
      headers: apiHeaders(),
      body: JSON.stringify({
        provider: config.provider,
        config: {
          apiKey: config.apiKey,
          model: config.model,
          baseUrl: config.baseUrl,
          maxOutputTokens: config.maxOutputTokens,
          geminiSafetySettings: config.geminiSafetySettings,
          // Forwarded, not dropped: the backend turns this into xAI's
          // x-grok-conv-id, which routes the turn to the same prompt-cache
          // shard. Omitting it silently made every turn a full-price,
          // full-latency prefill once generation moved server-side.
          conversationId: config.conversationId,
          // Same lesson: the backend cannot apply an effort it never receives.
          reasoningEffort: config.reasoningEffort,
          tools: config.tools,
          toolChoice: config.toolChoice
        },
        messages,
        meta
      }),
      signal
    })
    if (!response.ok) {
      throw new Error(`(${response.status}) ${response.statusText}`)
    }
    const data = await response.json()
    if (!data || typeof data.jobId !== 'string') {
      throw new Error('response contained no jobId')
    }
    jobId = data.jobId
  } catch (error) {
    throw new RemoteStartError(
      `Failed to start remote generation: ${error instanceof Error ? error.message : String(error)}`
    )
  }

  activeJobId = jobId
  writePersistedJob({ jobId, meta, offset: 0 })
  await attachToJob(jobId, 0, callbacks, signal)
}

/** Jobs the backend still knows about for this session (running or retained). */
export async function fetchActiveGenerations(): Promise<RemoteJobInfo[]> {
  const response = await fetch('/api/generate/active')
  if (!response.ok) {
    throw new Error(`Failed to list active generations (${response.status})`)
  }
  const data = await response.json()
  return Array.isArray(data) ? (data as RemoteJobInfo[]) : []
}

/**
 * What the server knows about the jobs behind a set of chat bubbles.
 * `known: false` means the question could not be asked — NOT that the answer
 * was "nothing": callers must not retire a bubble on it.
 */
export type BubbleJobLookup =
  | { known: false }
  | { known: true; jobs: Map<string, RemoteJobInfo> }

/**
 * Find the job that can still fill each of these assistant bubbles, keyed by
 * message id.
 *
 * The localStorage record is a hint, not the truth: it lives in ONE browser on
 * ONE origin, in a single slot any other generation overwrites. A turn sent
 * from the desktop and opened on the phone — or over the LAN address instead
 * of localhost — has no record at all, and used to be declared "Interrupted"
 * while its job was running. The server echoes `meta.assistantMessageId` for
 * every job it holds, which is enough to find the way back without one.
 *
 * Any status qualifies: a finished job replays its answer, an errored one its
 * real error — both better than a guess. The no-action retry re-issues a turn
 * into the SAME bubble, so the newest job per bubble wins.
 */
export async function findJobsForBubbles(messageIds: string[], bookId?: string | null): Promise<BubbleJobLookup> {
  if (messageIds.length === 0) return { known: true, jobs: new Map() }

  let listed: RemoteJobInfo[]
  try {
    const response = await fetch('/api/generate/active')
    if (response.status === 401 || response.status === 403) {
      // Logged out: nothing can be running remotely for this browser, so the
      // answer is a definite "no jobs" and dead placeholders may be retired.
      return { known: true, jobs: new Map() }
    }
    if (!response.ok) return { known: false }
    const data = await response.json()
    listed = Array.isArray(data) ? (data as RemoteJobInfo[]) : []
  } catch {
    return { known: false }
  }

  const wanted = new Set(messageIds)
  const jobs = new Map<string, RemoteJobInfo>()
  for (const job of listed) {
    const meta = job.meta || {}
    const messageId = meta.assistantMessageId
    if (!messageId || !wanted.has(messageId)) continue
    if (meta.kind && meta.kind !== 'chat') continue
    // Message ids are timestamps, unique per account in practice — but a job
    // that names another book is not this bubble's job.
    if (bookId && meta.bookId && meta.bookId !== bookId) continue
    const current = jobs.get(messageId)
    if (!current || (job.createdAt || '') > (current.createdAt || '')) jobs.set(messageId, job)
  }
  return { known: true, jobs }
}

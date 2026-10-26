/**
 * Tests for the resumable (server-side) generation transport.
 *
 * The contract under test is docs/features/resumable_generation.md §3/§5: SSE
 * events map onto the existing StreamCallbacks, the persisted offset is what
 * makes a reconnect duplicate- and gap-free, a start failure is the call's
 * error (there is no in-browser transport), and stopping reaches the backend. fetch is mocked at
 * the boundary; nothing else is stubbed.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  startRemoteGeneration,
  findJobsForBubbles,
  readPersistedJob,
  clearPersistedJob,
  RemoteStartError,
  streamReconnect
} from '../remoteGeneration'
import { streamLLM } from '../llm'
import { useAppStore } from '../../store/useAppStore'
import type { LLMMessage, StreamCallbacks, ThinkingBlock } from '../../types/llm'

const STORAGE_KEY = 'web_canvas_active_generation'

// ── Fetch boundary ───────────────────────────────────────────────────────────

/** A Response whose body streams `parts` one read at a time. */
function streamingResponse(parts: string[], ok = true, status = 200): Response {
  let i = 0
  const encoder = new TextEncoder()
  return {
    ok,
    status,
    statusText: ok ? 'OK' : 'Server Error',
    body: {
      getReader: () => ({
        read: async () =>
          i < parts.length
            ? { value: encoder.encode(parts[i++]), done: false }
            : { value: undefined, done: true }
      })
    },
    text: async () => parts.join(''),
    json: async () => JSON.parse(parts.join(''))
  } as unknown as Response
}

/** Serializes job events the way the backend writes them onto the wire. */
const sse = (events: unknown[]) => events.map(e => `data: ${JSON.stringify(e)}\n\n`)

function jsonResponse(payload: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    statusText: ok ? 'OK' : 'Error',
    json: async () => payload,
    text: async () => JSON.stringify(payload)
  } as unknown as Response
}

type Route = (url: string, init?: RequestInit) => Response | undefined
let routes: Route[] = []
const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input)
  for (const route of routes) {
    const res = route(url, init)
    if (res) return res
  }
  throw new Error(`Unrouted fetch: ${url}`)
})

const calls = () => fetchMock.mock.calls.map(c => String(c[0]))

// ── Callback recorder ────────────────────────────────────────────────────────

function recorder() {
  const chunks: string[] = []
  const done: Array<{ text: string; usage?: unknown }> = []
  const errors: string[] = []
  const callbacks: StreamCallbacks = {
    onChunk: c => chunks.push(c),
    onDone: (text, usage) => done.push({ text, usage }),
    onError: e => errors.push(e.message)
  }
  return { chunks, done, errors, callbacks }
}

const messages: LLMMessage[] = [{ role: 'user', content: 'hi' }]
const config = { provider: 'openai', apiKey: 'sk-test', model: 'gpt-x', baseUrl: 'https://provider.test/v1' }

beforeEach(() => {
  // The re-attach pause is real time; nothing here measures it (test audit, 2026-10-10: 4.5 s of sleeping).
  streamReconnect.delayMs = 0
  routes = []
  fetchMock.mockClear()
  vi.stubGlobal('fetch', fetchMock)
  clearPersistedJob()
  useAppStore.setState({ user: { username: 'alice' }, csrfToken: 'csrf-123' })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  useAppStore.setState({ user: null, csrfToken: null })
})

describe('startRemoteGeneration', () => {

  // Regression: streamLLM wraps the caller's callbacks to add debug logging,
  // and rebuilt the object field by field — so every OPTIONAL callback was
  // silently dropped on the way to the transport. The backend streamed the
  // model's thinking for minutes and the UI never saw a byte of it.
  it('passes optional callbacks through streamLLM to the transport', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-opt' }) : undefined,
      url => url.includes('/stream')
        ? streamingResponse(sse([
            { type: 'attached', offset: 0, status: 'running' },
            { type: 'reasoning', text: 'weighing options' },
            { type: 'delta', text: 'Answer', offset: 6 },
            { type: 'done', offset: 6 }
          ]))
        : undefined
    ]
    const seen: string[] = []
    const rec = recorder()

    await streamLLM(messages, { ...config, provider: 'grok' }, {
      ...rec.callbacks,
      onAttached: () => seen.push('attached'),
      onReasoning: t => seen.push('reasoning:' + t)
    })

    expect(seen).toEqual(['attached', 'reasoning:weighing options'])
    expect(rec.done[0].text).toBe('Answer')
  })

  // Regression: the payload copies config field by field, and conversationId
  // was left out when generation moved server-side. The backend turns it into
  // xAI's x-grok-conv-id (same prompt-cache shard), so dropping it made every
  // turn a full-price, full-latency prefill — visible in the UI as ~0 cache
  // hits on a book that had been cheap to continue.
  it('forwards conversationId so the backend can route the prompt cache', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-cache' }) : undefined,
      url => url.includes('/stream') ? streamingResponse(sse([{ type: 'done', offset: 0 }])) : undefined
    ]

    await startRemoteGeneration(
      messages,
      { ...config, provider: 'grok', conversationId: 'book-42' },
      {},
      recorder().callbacks
    )

    const startInit = fetchMock.mock.calls[0][1] as RequestInit
    expect(JSON.parse(String(startInit.body)).config.conversationId).toBe('book-42')
  })

  // Same class of bug as conversationId above: the backend applies the effort,
  // so a payload that omits it silently leaves every turn on the provider's
  // default (three minutes of thinking, in the case that motivated this).
  it('forwards reasoningEffort so the backend can apply it', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-effort' }) : undefined,
      url => url.includes('/stream') ? streamingResponse(sse([{ type: 'done', offset: 0 }])) : undefined
    ]

    await startRemoteGeneration(
      messages,
      { ...config, provider: 'grok', reasoningEffort: 'low' },
      {},
      recorder().callbacks
    )

    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))
    expect(body.config.reasoningEffort).toBe('low')
  })

  // The stream announces itself before the model speaks: response headers
  // only flush with the first body byte, so a slow first token left the client
  // unable to tell a live stream from a stalled one (15s of it, measured).
  it('reports the attach before any token arrives', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-att' }) : undefined,
      url => url.includes('/stream')
        ? streamingResponse(sse([
            { type: 'attached', offset: 0, status: 'running' },
            { type: 'delta', text: 'hi', offset: 2 },
            { type: 'done', offset: 2 }
          ]))
        : undefined
    ]
    const rec = recorder()
    const order: string[] = []
    const callbacks = {
      ...rec.callbacks,
      onAttached: () => order.push('attached'),
      onChunk: (c: string) => order.push('chunk:' + c)
    }

    await startRemoteGeneration(messages, config, {}, callbacks)

    expect(order).toEqual(['attached', 'chunk:hi'])
    // It is not text: nothing lands in the message and no offset is consumed.
    expect(rec.done[0]?.text ?? '').toBe('hi')
  })

  // Reasoning is the model's thinking, not its reply: reported live so a long
  // wait is visible, but never part of the message and never an offset.
  it('reports reasoning without letting it reach the message text', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-think' }) : undefined,
      url => url.includes('/stream')
        ? streamingResponse(sse([
            { type: 'reasoning', text: 'weighing options' },
            { type: 'delta', text: 'Answer', offset: 6 },
            { type: 'done', offset: 6 }
          ]))
        : undefined
    ]
    const rec = recorder()
    const thoughts: string[] = []

    await startRemoteGeneration(messages, config, {}, { ...rec.callbacks, onReasoning: t => thoughts.push(t) })

    expect(thoughts).toEqual(['weighing options'])
    expect(rec.chunks).toEqual(['Answer'])
    expect(rec.done[0].text).toBe('Answer')
    // Reasoning advanced no offset; the finished job is cleared as usual.
    expect(readPersistedJob()).toBeNull()
  })

  // A stream that ends without a terminal event usually means the connection
  // dropped, not that the job died — the backend keeps generating. Re-attach
  // once from what was rendered so a hiccup costs a pause, not the turn.
  it('re-attaches once from the rendered offset when the stream drops', async () => {
    const streamCalls: string[] = []
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-drop' }) : undefined,
      url => {
        if (!url.includes('/stream')) return undefined
        streamCalls.push(url)
        return streamCalls.length === 1
          ? streamingResponse(sse([{ type: 'delta', text: 'Hel', offset: 3 }]))   // cut off
          : streamingResponse(sse([{ type: 'delta', text: 'lo', offset: 5 }, { type: 'done', offset: 5 }]))
      }
    ]
    const rec = recorder()

    await startRemoteGeneration(messages, config, {}, rec.callbacks)

    // The retry resumes at the offset already rendered — no gap, no duplicate.
    expect(streamCalls[1]).toContain('from=3')
    expect(rec.chunks).toEqual(['Hel', 'lo'])
    expect(rec.errors).toEqual([])
    expect(rec.done[0].text).toBe('Hello')
  })

  it('re-attaches when the connection dies by throwing, not only by ending', async () => {
    // A thawed mobile tab finds its socket gone: read() rejects. That used to
    // go straight to onError — reverting the document and showing an error
    // for a job that was still generating.
    const streamCalls: string[] = []
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-thaw' }) : undefined,
      url => {
        if (!url.includes('/stream')) return undefined
        streamCalls.push(url)
        if (streamCalls.length > 1) {
          return streamingResponse(sse([{ type: 'delta', text: 'lo', offset: 5 }, { type: 'done', offset: 5 }]))
        }
        let reads = 0
        const encoder = new TextEncoder()
        return {
          ok: true, status: 200, statusText: 'OK',
          body: { getReader: () => ({
            read: async () => {
              if (reads++ === 0) return { value: encoder.encode(sse([{ type: 'delta', text: 'Hel', offset: 3 }])[0]), done: false }
              throw new TypeError('network error')
            }
          }) }
        } as unknown as Response
      }
    ]
    const rec = recorder()

    await startRemoteGeneration(messages, config, {}, rec.callbacks)

    expect(streamCalls[1]).toContain('from=3')
    expect(rec.errors).toEqual([])
    expect(rec.done[0].text).toBe('Hello')
  })

  it('does not re-ask a stream the server refused', async () => {
    // 404 means the job is gone; asking again only asks twice.
    let streamAttempts = 0
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-404' }) : undefined,
      url => {
        if (!url.includes('/stream')) return undefined
        streamAttempts++
        return jsonResponse({ detail: 'Generation job not found.' }, false, 404)
      }
    ]
    const rec = recorder()

    await startRemoteGeneration(messages, config, {}, rec.callbacks)

    expect(streamAttempts).toBe(1)
    expect(rec.errors[0]).toContain('404')
  })

  it('does not re-attach a stream the caller aborted', async () => {
    // Stop must stop: the re-attach is for connections that died on their own.
    let streamAttempts = 0
    const controller = new AbortController()
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-stop' }) : undefined,
      url => {
        if (!url.includes('/stream')) return undefined
        streamAttempts++
        controller.abort()
        const err = new Error('The operation was aborted.')
        err.name = 'AbortError'
        throw err
      }
    ]
    const rec = recorder()

    await startRemoteGeneration(messages, config, {}, rec.callbacks, controller.signal)

    expect(streamAttempts).toBe(1)
    expect(rec.errors).toEqual(['The operation was aborted.'])
  })

  it('gives up after the single re-attach when the job is really gone', async () => {
    let streamAttempts = 0
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-gone' }) : undefined,
      url => {
        if (!url.includes('/stream')) return undefined
        streamAttempts++
        return streamingResponse(sse([{ type: 'delta', text: 'partial', offset: 7 }]))
      }
    ]
    const rec = recorder()

    await startRemoteGeneration(messages, config, {}, rec.callbacks)

    expect(streamAttempts).toBe(2)
    expect(rec.errors).toEqual(['Generation stream disconnected before completion'])
    // The record survives so a later page load can still pick the job up.
    expect(readPersistedJob()?.jobId).toBe('gen-gone')
  })

  it('maps SSE events onto the callback contract in order', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-1', createdAt: 'now' }) : undefined,
      url => url.includes('/api/generate/gen-1/stream')
        ? streamingResponse(sse([
            { type: 'delta', text: 'Hel', offset: 3 },
            { type: 'delta', text: 'lo', offset: 5 },
            { type: 'done', offset: 5, usage: { promptTokens: 7, completionTokens: 2 } }
          ]))
        : undefined
    ]
    const { chunks, done, errors, callbacks } = recorder()

    await startRemoteGeneration(messages, config, { assistantMessageId: 'a-1', kind: 'chat' }, callbacks)

    expect(chunks).toEqual(['Hel', 'lo'])
    expect(done).toEqual([{ text: 'Hello', usage: { promptTokens: 7, completionTokens: 2 } }])
    expect(errors).toEqual([])
    expect(calls()[1]).toContain('from=0')
  })

  it('sends the CSRF header and the provider payload on start', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-2' }) : undefined,
      () => streamingResponse(sse([{ type: 'done', offset: 0 }]))
    ]
    await startRemoteGeneration(messages, config, { kind: 'chat' }, recorder().callbacks)

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-123')
    const body = JSON.parse(init.body as string)
    expect(body.provider).toBe('openai')
    expect(body.config).toMatchObject({ apiKey: 'sk-test', model: 'gpt-x', baseUrl: 'https://provider.test/v1' })
    expect(body.messages).toEqual(messages)
    expect(body.meta).toEqual({ kind: 'chat' })
  })

  it('reports a mid-stream error event without clearing more than its own job', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-3' }) : undefined,
      () => streamingResponse(sse([
        { type: 'delta', text: 'partial', offset: 7 },
        { type: 'error', message: 'provider exploded', offset: 7 }
      ]))
    ]
    const { chunks, errors, callbacks } = recorder()

    await startRemoteGeneration(messages, config, {}, callbacks)

    expect(chunks).toEqual(['partial'])
    expect(errors).toEqual(['provider exploded'])
    expect(readPersistedJob()).toBeNull()
  })

  it('throws RemoteStartError when the job cannot be created', async () => {
    routes = [url => url.endsWith('/api/generate') ? jsonResponse({ detail: 'boom' }, false, 500) : undefined]

    await expect(
      startRemoteGeneration(messages, config, {}, recorder().callbacks)
    ).rejects.toBeInstanceOf(RemoteStartError)
    expect(readPersistedJob()).toBeNull()
  })
})

describe('persisted job record', () => {
  it('advances the offset as chunks render and clears it on completion', async () => {
    const seen: Array<number | null> = []
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-4' }) : undefined,
      () => streamingResponse(sse([
        { type: 'delta', text: 'abc', offset: 3 },
        { type: 'delta', text: 'de', offset: 5 },
        { type: 'done', offset: 5 }
      ]))
    ]
    const callbacks: StreamCallbacks = {
      // Sampling inside onChunk proves the offset is persisted as the text is
      // rendered, not only at the end.
      onChunk: () => seen.push(readPersistedJob()?.offset ?? null),
      onDone: () => {},
      onError: () => {}
    }

    await startRemoteGeneration(messages, config, { assistantMessageId: 'a-4' }, callbacks)

    // The first sample is taken before the first chunk's offset is written.
    expect(seen).toEqual([0, 3])
    expect(readPersistedJob()).toBeNull()
  })

  it('ignores a corrupt record instead of blocking generation', () => {
    window.localStorage.setItem(STORAGE_KEY, '{not json')
    expect(readPersistedJob()).toBeNull()
  })
})

describe('findJobsForBubbles', () => {
  const active = (jobs: unknown[]): Route => url => url.endsWith('/api/generate/active') ? jsonResponse(jobs) : undefined

  it('finds the job behind a bubble with no local record at all', async () => {
    // The turn was sent from another device (or another origin): localStorage
    // here is empty, and the server is the only one who knows.
    routes = [active([
      { jobId: 'gen-a', status: 'running', length: 0, createdAt: '2026-09-21T06:09:57Z',
        meta: { assistantMessageId: 'a-1', bookId: 'book-1', kind: 'chat' } }
    ])]

    const found = await findJobsForBubbles(['a-1'], 'book-1')

    expect(found.known).toBe(true)
    expect(found.known && found.jobs.get('a-1')?.jobId).toBe('gen-a')
    expect(readPersistedJob()).toBeNull()   // looking is not adopting
  })

  it('takes the newest job when a retry re-issued the turn into the same bubble', async () => {
    routes = [active([
      { jobId: 'gen-old', status: 'done', createdAt: '2026-09-21T06:00:00Z', meta: { assistantMessageId: 'a-1' } },
      { jobId: 'gen-new', status: 'running', createdAt: '2026-09-21T06:05:00Z', meta: { assistantMessageId: 'a-1' } }
    ])]

    const found = await findJobsForBubbles(['a-1'])
    expect(found.known && found.jobs.get('a-1')?.jobId).toBe('gen-new')
  })

  it('ignores jobs for other bubbles, other books, and non-chat kinds', async () => {
    routes = [active([
      { jobId: 'gen-other-bubble', status: 'running', meta: { assistantMessageId: 'a-2' } },
      { jobId: 'gen-other-book', status: 'running', meta: { assistantMessageId: 'a-1', bookId: 'book-2' } },
      { jobId: 'gen-summary', status: 'running', meta: { assistantMessageId: 'a-1', kind: 'summary' } }
    ])]

    const found = await findJobsForBubbles(['a-1'], 'book-1')
    expect(found.known && found.jobs.size).toBe(0)
  })

  it('offers a finished or failed job too — its replay beats a guess', async () => {
    routes = [active([
      { jobId: 'gen-done', status: 'done', length: 9406, meta: { assistantMessageId: 'a-1' } },
      { jobId: 'gen-err', status: 'error', length: 0, meta: { assistantMessageId: 'a-2' } }
    ])]

    const found = await findJobsForBubbles(['a-1', 'a-2'])
    expect(found.known && [...found.jobs.keys()].sort()).toEqual(['a-1', 'a-2'])
  })

  it('says "unknown", not "none", when the server cannot be asked', async () => {
    routes = [url => url.endsWith('/api/generate/active') ? jsonResponse({}, false, 503) : undefined]
    expect(await findJobsForBubbles(['a-1'])).toEqual({ known: false })

    routes = []   // fetch itself rejects: the network is down
    expect(await findJobsForBubbles(['a-1'])).toEqual({ known: false })
  })

  it('treats a logged-out session as a definite "no jobs"', async () => {
    // Nothing can be running remotely for this browser, so a dead placeholder
    // from the direct transport may still be retired.
    routes = [url => url.endsWith('/api/generate/active') ? jsonResponse({}, false, 401) : undefined]

    const found = await findJobsForBubbles(['a-1'])
    expect(found.known && found.jobs.size).toBe(0)
  })

  it('makes no request when there is nothing to look up', async () => {
    const found = await findJobsForBubbles([])
    expect(found.known).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('streamLLM transport selection', () => {
  it('uses the remote transport when logged in', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-9' }) : undefined,
      url => url.includes('/stream') ? streamingResponse(sse([
        { type: 'delta', text: 'remote', offset: 6 },
        { type: 'done', offset: 6 }
      ])) : undefined
    ]
    const { chunks, done, callbacks } = recorder()

    await streamLLM(messages, config, callbacks)

    expect(chunks).toEqual(['remote'])
    expect(done[0].text).toBe('remote')
    expect(calls().some(u => u.includes('provider.test'))).toBe(false)
  })

  it('reports a failed start as an error and never calls the provider from the browser', async () => {
    routes = [url => url.endsWith('/api/generate') ? jsonResponse({ detail: 'down' }, false, 500) : undefined]
    const { done, errors, callbacks } = recorder()

    await streamLLM(messages, config, callbacks)

    expect(done).toEqual([])
    expect(errors[0]).toMatch(/Failed to start remote generation/)
    expect(calls()).toEqual(['/api/generate'])
  })

  it('does not fall back once the job is running (no double generation)', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-10' }) : undefined,
      url => url.includes('/stream') ? streamingResponse(sse([{ type: 'error', message: 'provider 429', offset: 0 }])) : undefined
    ]
    const { errors, callbacks } = recorder()

    await streamLLM(messages, config, callbacks)

    expect(errors).toEqual(['provider 429'])
    expect(calls().some(u => u.includes('provider.test'))).toBe(false)
  })

  it('refuses to generate when logged out (there is no in-browser transport)', async () => {
    useAppStore.setState({ user: null })
    const { done, errors, callbacks } = recorder()
    await streamLLM(messages, config, callbacks)
    expect(done).toEqual([])
    expect(errors[0]).toMatch(/Not signed in/)
    expect(calls()).toEqual([])
  })
})

// Reasoning artifacts the next tool-loop step must replay (Anthropic thinking
// blocks, Gemini thoughtSignature). The backend publishes them as informational
// events and replays them to every reader that attaches.
describe('reasoning artifacts over the backend transport', () => {
  const THINKING: ThinkingBlock = { type: 'thinking', thinking: 'Need chapter 3.', signature: 'EqQB+/==' }
  const REDACTED: ThinkingBlock = { type: 'redacted_thinking', data: 'EmwKAhgB' }

  function artifactRecorder() {
    const rec = recorder()
    const blocks: ThinkingBlock[] = []
    const deltas: Array<Record<string, unknown>> = []
    rec.callbacks.onThinkingBlock = b => blocks.push(b)
    rec.callbacks.onToolCallDelta = d => deltas.push(d as unknown as Record<string, unknown>)
    return { ...rec, blocks, deltas }
  }

  it('maps thinking_block events onto onThinkingBlock, in order, outside the text', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-think' }) : undefined,
      url => url.includes('/stream')
        ? streamingResponse(sse([
            { type: 'attached', offset: 0, status: 'running' },
            { type: 'thinking_block', index: 0, block: THINKING },
            { type: 'thinking_block', index: 1, block: REDACTED },
            { type: 'tool_call', index: 2, id: 'toolu_1', name: 'read_chapter', text: '{}' },
            { type: 'delta', text: 'Reading.', offset: 8 },
            { type: 'done', offset: 8 }
          ]))
        : undefined
    ]
    const rec = artifactRecorder()

    await streamLLM(messages, { ...config, provider: 'anthropic' }, rec.callbacks)

    // Through streamLLM's wrapper: an optional callback it rebuilt instead of
    // spreading would silently drop these.
    expect(rec.blocks).toEqual([THINKING, REDACTED])
    expect(rec.done[0].text).toBe('Reading.')
    // A tool_call without a signature keeps its exact old shape.
    expect(rec.deltas).toEqual([{ index: 2, id: 'toolu_1', name: 'read_chapter', argumentsText: '{}', replace: false }])
    expect('signature' in rec.deltas[0]).toBe(false)
  })

  it('keeps the afterText mark on a block that streamed after the text, and adds nothing to the others', async () => {
    // The server replays such a block after the text; moved ahead of it,
    // Anthropic calls the block modified (400, 2026-10-11).
    const LATER: ThinkingBlock = { type: 'thinking', thinking: 'Again.', signature: 'S2', afterText: true }
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-after' }) : undefined,
      url => url.includes('/stream')
        ? streamingResponse(sse([
            { type: 'thinking_block', index: 0, block: THINKING },
            { type: 'delta', text: 'Draft.', offset: 6 },
            { type: 'thinking_block', index: 1, block: { ...LATER, afterText: true, stray: 'dropped' } },
            { type: 'done', offset: 6 }
          ]))
        : undefined
    ]
    const rec = artifactRecorder()

    await streamLLM(messages, { ...config, provider: 'anthropic' }, rec.callbacks)

    expect(rec.blocks).toEqual([THINKING, LATER])
    expect(Object.keys(rec.blocks[1])).toEqual(['type', 'thinking', 'signature', 'afterText'])
  })

  it("carries a tool_call event's signature onto the delta, live and on replay", async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-sig' }) : undefined,
      url => url.includes('/stream')
        ? streamingResponse(sse([
            { type: 'tool_call', index: 0, id: null, name: 'read_chapter', text: '{"chapter":"三"}', signature: 'CiQB+/sig==', replay: true },
            { type: 'tool_call', index: 1, id: null, name: 'read_chapter', text: '{"chapter":"四"}' },
            { type: 'done', offset: 0 }
          ]))
        : undefined
    ]
    const rec = artifactRecorder()

    await startRemoteGeneration(messages, { ...config, provider: 'gemini' }, {}, rec.callbacks)

    expect(rec.deltas[0]).toMatchObject({ index: 0, signature: 'CiQB+/sig==', replace: true })
    expect('signature' in rec.deltas[1]).toBe(false)
  })

  it('does not deliver a block twice when a mid-turn reconnect replays them all', async () => {
    // Every attach replays the job's blocks from the first. A duplicated
    // block in the replay is a 400 from Anthropic for the whole next step.
    const streamCalls: string[] = []
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-rethink' }) : undefined,
      url => {
        if (!url.includes('/stream')) return undefined
        streamCalls.push(url)
        return streamCalls.length === 1
          ? streamingResponse(sse([{ type: 'thinking_block', index: 0, block: THINKING }]))   // cut off
          : streamingResponse(sse([
              { type: 'thinking_block', index: 0, block: THINKING },   // replay
              { type: 'thinking_block', index: 1, block: REDACTED },   // new
              { type: 'done', offset: 0 }
            ]))
      }
    ]
    const rec = artifactRecorder()

    await startRemoteGeneration(messages, { ...config, provider: 'anthropic' }, {}, rec.callbacks)

    expect(streamCalls).toHaveLength(2)
    expect(rec.errors).toEqual([])
    expect(rec.blocks).toEqual([THINKING, REDACTED])
  })

  it('hands grok\'s output items to onResponseItem, once each across a mid-turn reconnect', async () => {
    const R1 = { type: 'reasoning', id: 'rs_1', encrypted_content: 'CIPHER==' }
    const R2 = { type: 'function_call', call_id: 'c', name: 'grep', arguments: '{}' }
    const streamCalls: string[] = []
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-items' }) : undefined,
      url => {
        if (!url.includes('/stream')) return undefined
        streamCalls.push(url)
        return streamCalls.length === 1
          ? streamingResponse(sse([{ type: 'response_item', index: 0, item: R1 }]))   // cut off
          : streamingResponse(sse([
              { type: 'response_item', index: 0, item: R1 },   // replay
              { type: 'response_item', index: 1, item: R2 },   // new
              { type: 'done', offset: 0 }
            ]))
      }
    ]
    const rec = artifactRecorder()
    const items: unknown[] = []
    rec.callbacks.onResponseItem = item => items.push(item)

    await startRemoteGeneration(messages, { ...config, provider: 'grok' }, {}, rec.callbacks)

    expect(streamCalls).toHaveLength(2)
    expect(rec.errors).toEqual([])
    expect(items).toEqual([R1, R2])
  })

  it('drops a malformed block instead of handing it to the replay', async () => {
    routes = [
      url => url.endsWith('/api/generate') ? jsonResponse({ jobId: 'gen-bad' }) : undefined,
      url => url.includes('/stream')
        ? streamingResponse(sse([
            { type: 'thinking_block', index: 0, block: { type: 'thinking', thinking: 'no signature' } },
            { type: 'thinking_block', index: 1, block: { type: 'redacted_thinking' } },
            { type: 'thinking_block', index: 2, block: { ...THINKING, extra: 'stray' } },
            { type: 'done', offset: 0 }
          ]))
        : undefined
    ]
    const rec = artifactRecorder()

    await startRemoteGeneration(messages, { ...config, provider: 'anthropic' }, {}, rec.callbacks)

    expect(rec.blocks).toEqual([THINKING])
    expect(Object.keys(rec.blocks[0])).toEqual(['type', 'thinking', 'signature'])
  })
})

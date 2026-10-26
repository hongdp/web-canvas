/**
 * How a server run's events change the chat messages (chat/serverRunEvents),
 * and that the book event stream hands `run` events to the run listeners
 * instead of resyncing the book.
 */
import { describe, it, expect, vi } from 'vitest'
import type { ChatMessage } from '../../types/chat'
import type { ServerRunSummary } from '../../services/serverRuns'
import { applyRunEvent, ensureRunMessages, bubbleOf, bubbleStillWaiting, KEPT_DRAFT_NOTE, runUsageDelta, type RunLive } from '../chat/serverRunEvents'
import { applyBookEvent, connectBookEvents } from '../../store/bookEvents'
import { onRunCatchUp, onRunEvent } from '../../store/runEvents'
import { ASSISTANT_PLACEHOLDER } from '../chat/streamHandlers'
import { useAppStore } from '../../store/useAppStore'

const run = (over: Partial<ServerRunSummary> = {}): ServerRunSummary => ({
  id: 'run-1', bookId: 'book-1', status: 'running', createdAt: '2026-10-08T00:00:00.000Z', updatedAt: '2026-10-08T00:00:00.000Z',
  userMessageId: 'u1', assistantMessageId: 'a1', prompt: '写第一章', activeDocumentId: 'doc-1', clientId: 'tab',
  record: { status: 'running', steps: 0, trace: [], touched: [], timeline: [], prefix: '（前言）' },
  ...over
})
const base: ChatMessage[] = [
  { id: 'u1', role: 'user', content: '写第一章', timestamp: 't' },
  { id: 'a1', role: 'assistant', content: ASSISTANT_PLACEHOLDER, timestamp: 't' }
]

describe('applyRunEvent', () => {
  it('creates the turn\'s messages for a run another device sent, and marks a queued one', () => {
    const live = new Map<string, RunLive>()
    const out = applyRunEvent([], { type: 'run', kind: 'queued', runId: 'run-1', run: run({ status: 'queued' }), position: 2 }, live)
    expect(out.map(m => m.id)).toEqual(['u1', 'a1'])
    expect(out[0].content).toBe('写第一章')
    expect(out[1].agent?.status).toBe('queued')
    expect(out[1].agent?.run).toEqual({ id: 'run-1', status: 'queued', position: 2 })
    expect(ensureRunMessages(out, run()).length).toBe(2)
  })

  it('shows the step in flight: chat text live, markup hidden, the progress line below', () => {
    const live = new Map<string, RunLive>()
    let out = applyRunEvent(base, { type: 'run', kind: 'started', runId: 'run-1', run: run() }, live)
    out = applyRunEvent(out, { type: 'run', kind: 'delta', runId: 'run-1', text: '好的，开始写。\n<canvas><p>正' }, live)
    out = applyRunEvent(out, { type: 'run', kind: 'progress', runId: 'run-1', line: '✍️ #2 "第二章" … 120 chars' }, live)
    const bubble = out[1]
    expect(bubbleOf(out, 'run-1')).toBe('a1')
    expect(bubble.agent?.live).toBe('好的，开始写。\n\n✍️ #2 "第二章" … 120 chars')
    expect(bubble.content).toBe('（前言）\n\n好的，开始写。\n\n✍️ #2 "第二章" … 120 chars')
    expect(bubble.content).not.toContain('<canvas>')
  })

  it('replaces the bubble\'s record at each step boundary and clears the live text', () => {
    const live = new Map<string, RunLive>()
    let out = applyRunEvent(base, { type: 'run', kind: 'started', runId: 'run-1', run: run() }, live)
    out = applyRunEvent(out, { type: 'run', kind: 'delta', runId: 'run-1', text: 'Reading.' }, live)
    out = applyRunEvent(out, { type: 'run', kind: 'step', runId: 'run-1', record: { status: 'running', steps: 1, trace: ['📖 read #2'], touched: [], timeline: [{ type: 'text', text: 'Reading.' }, { type: 'tool', line: '📖 read #2', ok: true }] } }, live)
    expect(out[1].agent?.steps).toBe(1)
    expect(out[1].agent?.live).toBeUndefined()
    expect(out[1].agent?.run).toEqual({ id: 'run-1', status: 'running' })
    expect(out[1].content).toContain('Reading.')
    expect(live.get('run-1')?.text).toBe('')
  })

  it('marks a paused run with why, and finishes with the server\'s content, record and reasoning', () => {
    const live = new Map<string, RunLive>()
    let out = applyRunEvent(base, { type: 'run', kind: 'started', runId: 'run-1', run: run() }, live)
    out = applyRunEvent(out, { type: 'run', kind: 'paused', runId: 'run-1', record: { status: 'running', steps: 3, trace: ['a', 'a', 'a'], touched: [], timeline: [] },
      pause: { reason: 'repeating', message: 'same calls', steps: [] } }, live)
    expect(out[1].agent?.status).toBe('paused')
    expect(out[1].agent?.run?.pause?.reason).toBe('repeating')
    expect(bubbleStillWaiting(out[1])).toBe(true)
    out = applyRunEvent(out, { type: 'run', kind: 'finished', runId: 'run-1', status: 'done', run: run({ status: 'done' }),
      result: { content: '写完了。', record: { status: 'done', steps: 4, trace: ['a', 'a', 'a', '✏️ rewrote #1'], touched: [{ documentId: 'doc-1', titleAtRun: 'C1', kind: 'rewrite', changes: 1, failed: 0 }], timeline: [] }, reasoningItems: [{ type: 'reasoning', encrypted_content: 'x' }] } }, live)
    expect(out[1].content).toBe('写完了。')
    expect(out[1].agent?.status).toBe('done')
    expect(out[1].agent?.run).toEqual({ id: 'run-1', status: 'done' })
    expect(out[1].reasoningItems).toHaveLength(1)
    expect(bubbleStillWaiting(out[1])).toBe(false)
    expect(live.has('run-1')).toBe(false)
  })

  it('says the draft was kept when this tab stopped the run and kept it', () => {
    const live = new Map<string, RunLive>([['run-1', { text: '', progress: null, keptDraft: true }]])
    let out = applyRunEvent(base, { type: 'run', kind: 'started', runId: 'run-1', run: run() }, live)
    live.get('run-1')!.keptDraft = true
    out = applyRunEvent(out, { type: 'run', kind: 'finished', runId: 'run-1', status: 'stopped', run: run({ status: 'stopped' }),
      result: { content: 'Half.\n\n⏹️ Stopped.', record: { status: 'stopped', steps: 1, trace: [], touched: [], timeline: [] } } }, live)
    expect(out[1].content).toBe(`Half.\n\n${KEPT_DRAFT_NOTE}`)
    expect(out[1].agent).toBeUndefined()
  })
})

describe('the book event stream', () => {
  it('hands run events to the run listeners and never resyncs the book for them', async () => {
    const seen: unknown[] = []
    const off = onRunEvent(e => seen.push(e))
    const fetchFn = vi.fn()
    await applyBookEvent({ id: 1, type: 'run', kind: 'delta', documentId: undefined } as never, { bookId: 'book-1', fetchFn: fetchFn as never })
    off()
    expect(seen).toHaveLength(1)
    expect(fetchFn).not.toHaveBeenCalled()
  })

  // A phone suspends the stream in the background (2026-10-10): coming back
  // must reopen a stream that gave up and let the runs catch up.
  it('reopens a closed stream and asks the runs to catch up when the page comes back', async () => {
    vi.useFakeTimers()
    const sources: Array<{ url: string; readyState: number; close: () => void; onopen?: () => void; onerror?: () => void }> = []
    class FakeEventSource {
      static CLOSED = 2
      readyState = 1
      onopen?: () => void
      onerror?: () => void
      onmessage?: (m: unknown) => void
      url: string
      constructor(url: string) { this.url = url; sources.push(this) }
      close() { this.readyState = 2 }
    }
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })))
    let catchUps = 0
    const off = onRunCatchUp(() => { catchUps++ })
    const disconnect = connectBookEvents('book-1')
    expect(sources).toHaveLength(1)

    // Back in the foreground with the stream still open: catch up only.
    document.dispatchEvent(new Event('visibilitychange'))
    window.dispatchEvent(new Event('focus'))
    vi.advanceTimersByTime(400)
    expect(catchUps).toBe(1)
    expect(sources).toHaveLength(1)

    // The stream gave up while hidden: reopened, and caught up.
    sources[0].readyState = 2
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(400)
    expect(sources).toHaveLength(2)
    expect(catchUps).toBe(2)

    // A reconnect after a drop catches up too.
    sources[1].onerror?.()
    sources[1].onopen?.()
    expect(catchUps).toBe(3)

    disconnect()
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(400)
    expect(catchUps).toBe(3)
    expect(sources[1].readyState).toBe(2)
    off()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })
})

describe('runUsageDelta', () => {
  it('counts what a run spent since the last report, never less than nothing', () => {
    expect(runUsageDelta(undefined, undefined)).toBeNull()
    const first = runUsageDelta(undefined, { promptTokens: 100, completionTokens: 10 })
    expect(first?.delta).toEqual({ promptTokens: 100, completionTokens: 10, cachedPromptTokens: 0 })
    const second = runUsageDelta(first!.total, { promptTokens: 250, completionTokens: 30, cachedPromptTokens: 90 })
    expect(second?.delta).toEqual({ promptTokens: 150, completionTokens: 20, cachedPromptTokens: 90 })
    // The same report again, or an older one: nothing to add.
    expect(runUsageDelta(second!.total, { promptTokens: 250, completionTokens: 30, cachedPromptTokens: 90 })).toBeNull()
    expect(runUsageDelta(second!.total, { promptTokens: 100, completionTokens: 10 })).toBeNull()
  })
})

describe('a pin set in another tab', () => {
  it('arrives with the chapter list, without touching the chapter text', async () => {
    useAppStore.setState({
      activeBookId: 'book-1', activeDocumentId: 'd1',
      documents: [
        { id: 'd1', title: '第一章', content: '<p>正文</p>', contentLoaded: true, createdAt: 't', updatedAt: 't', revision: 3 },
        { id: 'd2', title: '人物卡', content: '<p>卡</p>', contentLoaded: true, createdAt: 't', updatedAt: 't', revision: 1 }
      ]
    })
    const fetchFn = vi.fn(async () => ({ ok: true, json: async () => ({ documents: [
      { id: 'd1', title: '第一章', createdAt: 't', updatedAt: 't', revision: 3, pinned: false },
      { id: 'd2', title: '人物卡', createdAt: 't', updatedAt: 't', revision: 1, pinned: true }
    ] }) }))
    await applyBookEvent({ id: 2, type: 'document', kind: 'pinned', documentId: 'd2', revision: 1, clientId: 'other-tab' } as never, { bookId: 'book-1', fetchFn: fetchFn as never })
    const docs = useAppStore.getState().documents
    expect(docs.find(d => d.id === 'd2')?.pinned).toBe(true)
    expect(docs.find(d => d.id === 'd2')?.content).toBe('<p>卡</p>')
    expect(docs.find(d => d.id === 'd1')?.pinned ?? false).toBe(false)
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })
})

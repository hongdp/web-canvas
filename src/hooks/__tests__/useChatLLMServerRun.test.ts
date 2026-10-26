/**
 * The chat hook: every turn is posted to the server and the bubble and editor
 * follow the run's events (backend_authority.md §4.3, phase 4).
 * The API is a mocked fetch; the events come straight from the emitter.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { createElement, act } from 'react'
import { createRoot, type Root } from 'react-dom/client'

vi.mock('../../services/llm', () => ({ streamLLM: vi.fn() }))
vi.mock('../../services/chapterSummaries', () => ({ enqueueStaleSummaryRefreshes: vi.fn() }))

import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import { useChatLLM } from '../useChatLLM'
import { useAppStore, isEditLocked } from '../../store/useAppStore'
import { emitRunCatchUp, emitRunEvent } from '../../store/runEvents'
import { CLIENT_ID, recordServerCopy, serverCopies } from '../../store/documentSync'
import { isSilentPreview } from '../chat/selectionReplace'
import type { ServerRunSummary } from '../../services/serverRuns'

interface Harness { current: ReturnType<typeof useChatLLM>; unmount: () => void }

function renderChatHook(editor: unknown = null): Harness {
  const harness = { current: null as unknown as ReturnType<typeof useChatLLM> } as Harness
  const Probe = () => {
    harness.current = useChatLLM({
      activeEditor: editor as never, selectedText: '', uploadedImages: images, setUploadedImages: vi.fn(),
      layoutMode: 'landscape', setIsChatExpanded: vi.fn(), forceSave: vi.fn(), setSaveStatus: vi.fn()
    })
    return null
  }
  let root: Root
  act(() => { root = createRoot(document.createElement('div')); root.render(createElement(Probe)) })
  harness.unmount = () => act(() => root.unmount())
  return harness
}

function stubEditor(html: string) {
  const writes: string[] = []
  const chain = { setMeta: () => chain, setContent: (c: string) => { writes.push(c); html = c; return chain }, run: () => true }
  return { writes, editor: { chain: () => chain, getHTML: () => html, commands: { setContent: (c: string) => { writes.push(c); html = c } }, state: { selection: { from: 0, to: 0 } } } }
}

const doc = (id: string, title: string, content: string) => ({ id, title, content, contentLoaded: true, createdAt: 't', updatedAt: 't' })
const bubble = () => useAppStore.getState().messages.filter(m => m.role === 'assistant').at(-1)!
const flush = async () => { for (let i = 0; i < 6; i++) await act(async () => { await Promise.resolve() }) }

const summary = (over: Partial<ServerRunSummary> = {}): ServerRunSummary => ({
  id: 'run-1', bookId: 'book-1', status: 'running', createdAt: 't', updatedAt: 't', activeDocumentId: 'doc-1', clientId: 'x',
  record: { status: 'running', steps: 0, trace: [], touched: [], timeline: [] }, ...over
})

const posted: Array<{ url: string; body: unknown }> = []
let steerRefused = false
/** What /api/generate/active answers: the jobs running, or 'down' when the server cannot be asked. */
let activeJobs: unknown[] | 'down' = []
/** What GET /runs/run-1 answers: the run, or null for 404. */
let single: unknown = null
/** The images attached to the next send (the hook's uploadedImages prop). */
const images: string[] = []
/** Every request, in order: "<METHOD> <url>". */
const calls: string[] = []
let listed: { runs: unknown[]; queueHeld: boolean } = { runs: [], queueHeld: false }
let nextRun: (body: { assistantMessageId: string; userMessageId: string }) => ServerRunSummary = body =>
  summary({ assistantMessageId: body.assistantMessageId, userMessageId: body.userMessageId })

let savedProvider: string
beforeEach(() => {
  posted.length = 0
  calls.length = 0
  steerRefused = false
  activeJobs = []
  single = null
  images.length = 0
  serverCopies.clear()
  listed = { runs: [], queueHeld: false }
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const json = (data: unknown, status = 200) => ({ ok: status < 300, status, statusText: 'OK', json: async () => data }) as Response
    calls.push(`${init?.method ?? 'GET'} ${url}`)
    if (url === '/api/generate/active') return activeJobs === 'down' ? json({}, 503) : json(activeJobs)
    if (url === '/api/books/book-1/runs' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      posted.push({ url, body })
      const run = nextRun(body)
      return json({ run, position: run.status === 'queued' ? 1 : 0, queueHeld: false })
    }
    if (url === '/api/books/book-1/runs') return json(listed)
    if (url === '/api/books/book-1/runs/run-1' && (init?.method ?? 'GET') === 'GET') return single ? json({ run: single }) : json({ detail: 'Run not found.' }, 404)
    if (url.startsWith('/api/books/book-1/runs/')) {
      posted.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null })
      if (url.endsWith('/steer') && steerRefused) return json({ detail: 'This run is not running; send the request as a new turn.' }, 409)
      return json({ success: true })
    }
    return json({})
  }))
  savedProvider = useAppStore.getState().activeProvider
  const s = useAppStore.getState()
  useAppStore.setState({
    user: { username: 'alice' }, activeBookId: 'book-1', csrfToken: 'tok',
    documents: [doc('doc-1', '第一章', '<p>原文。</p>'), doc('doc-2', '第二章', '<p>二。</p>')],
    activeDocumentId: 'doc-1',
    messages: [{ id: 'welcome', role: 'assistant', content: 'hi', timestamp: 't' }],
    isStreaming: false, editLockedIds: null, versions: [],
    providerConfigs: { ...s.providerConfigs, grok: { ...s.providerConfigs.grok, apiKey: 'k', model: 'grok-4.6' } },
    activeProvider: 'grok'
  })
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  useAppStore.setState({ user: null, activeProvider: savedProvider as never, isStreaming: false, editLockedIds: null })
})

describe('a bubble a closed tab-run left waiting (before phase 4)', () => {
  const waiting = () => useAppStore.setState({
    messages: [{ id: 'u-1', role: 'user', content: '写第一章', timestamp: 't' }, { id: 'a-1', role: 'assistant', content: 'Thinking...', timestamp: 't' }]
  })

  it('is retired once the server confirms no job fills it', async () => {
    // 2026-10-16: the page closed after the turn's first step; the bubble said "Thinking..." for good.
    waiting()
    const h = renderChatHook()
    await flush()
    expect(useAppStore.getState().messages.find(m => m.id === 'a-1')?.content).toContain('Interrupted')
    h.unmount()
  })

  it('is left alone when the server cannot be asked', async () => {
    waiting()
    activeJobs = 'down'
    const h = renderChatHook()
    await flush()
    expect(useAppStore.getState().messages.find(m => m.id === 'a-1')?.content).toBe('Thinking...')
    h.unmount()
  })
})

describe('signed out', () => {
  it('says to sign in instead of sending', async () => {
    useAppStore.setState({ user: null })
    const h = renderChatHook()
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    expect(posted).toHaveLength(0)
    expect(bubble().content).toContain('Sign in')
    expect(useAppStore.getState().isStreaming).toBe(false)
    h.unmount()
  })
})

describe('a turn', () => {
  it('posts the Polish button as a server run of its own', async () => {
    const h = renderChatHook(stubEditor('<p>原文。</p>').editor)
    await act(async () => { useAppStore.getState().requestPolish('doc-2') })
    await flush()
    expect(posted).toHaveLength(1)
    const body = posted[0].body as { prompt: string; activeDocumentId: string; history: unknown[]; polish?: { documentId: string } }
    expect(body.polish?.documentId).toBe('doc-2')
    expect(body.activeDocumentId).toBe('doc-2')
    expect(body.history).toEqual([])
    // The server snapshots and writes; the tab only renders the run.
    expect(useAppStore.getState().versions).toEqual([])
    expect(bubble().agent?.run).toEqual({ id: 'run-1', status: 'running' })
    h.unmount()
  })

  it('posts the request, then renders the run: preview, record, finish', async () => {
    const stub = stubEditor('<p>原文。</p>')
    const h = renderChatHook(stub.editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    expect(posted).toHaveLength(1)
    const body = posted[0].body as { prompt: string; activeDocumentId: string; history: unknown[]; config: { serverRuns?: boolean; apiKey: string }; provider: string }
    expect(body.prompt).toBe('写第一章')
    expect(body.activeDocumentId).toBe('doc-1')
    expect(body.history).toEqual([])
    expect(body.provider).toBe('grok')
    expect(body.config.apiKey).toBe('k')
    expect(useAppStore.getState().isStreaming).toBe(true)
    expect(bubble().agent?.run).toEqual({ id: 'run-1', status: 'running' })

    const runId = 'run-1'
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId, run: summary({ assistantMessageId: bubble().id }) }) })
    act(() => { emitRunEvent({ type: 'run', kind: 'delta', runId, text: '好的。\n<canvas><p>新' }) })
    expect(bubble().agent?.live).toBe('好的。')
    act(() => { emitRunEvent({ type: 'run', kind: 'preview', runId, documentId: 'doc-1', html: '<p>新</p>' }) })
    expect(stub.writes.at(-1)).toBe('<p>新</p>')
    act(() => { emitRunEvent({ type: 'run', kind: 'lock', runId, documentIds: ['doc-1'] }) })
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(true)
    expect(isEditLocked(useAppStore.getState(), 'doc-2')).toBe(false)
    act(() => { emitRunEvent({ type: 'run', kind: 'step', runId, record: { status: 'running', steps: 1, trace: ['✏️ rewrote #1'], touched: [{ documentId: 'doc-1', titleAtRun: '第一章', kind: 'rewrite', changes: 1, failed: 0 }], timeline: [{ type: 'text', text: '好的。' }, { type: 'tool', line: '✏️ rewrote #1', ok: true }] } }) })
    expect(bubble().agent?.steps).toBe(1)
    act(() => {
      emitRunEvent({ type: 'run', kind: 'finished', runId, status: 'done', run: summary({ status: 'done', assistantMessageId: bubble().id }),
        result: { content: '好的。\n\n写完了。', record: { status: 'done', steps: 2, trace: ['✏️ rewrote #1'], touched: [{ documentId: 'doc-1', titleAtRun: '第一章', kind: 'rewrite', changes: 1, failed: 0 }], timeline: [] },
          snapshots: [{ id: 'ver-1', documentId: 'doc-1', title: 'Auto-save', timestamp: 't' }] } })
    })
    expect(useAppStore.getState().isStreaming).toBe(false)
    expect(bubble().content).toBe('好的。\n\n写完了。')
    expect(bubble().agent?.status).toBe('done')
    expect(useAppStore.getState().versions.map(v => v.id)).toContain('ver-1')
    h.unmount()
  })

  // The footer's Session Tokens stayed at 0 with serverRuns on (2026-10-10).
  it('counts a server run\'s usage into the session totals, step by step', async () => {
    useAppStore.setState({ sessionInputTokens: 0, sessionOutputTokens: 0, sessionCacheHitTokens: 0, sessionCacheMissTokens: 0, lastTurnCache: null })
    const h = renderChatHook(stubEditor('<p>原文。</p>').editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    const runId = 'run-1'
    const record = { status: 'running' as const, steps: 1, trace: [], touched: [], timeline: [] }
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId, run: summary({ assistantMessageId: bubble().id }) }) })
    act(() => { emitRunEvent({ type: 'run', kind: 'step', runId, record, usage: { promptTokens: 1000, completionTokens: 100, cachedPromptTokens: 0 } }) })
    act(() => { emitRunEvent({ type: 'run', kind: 'step', runId, record: { ...record, steps: 2 }, usage: { promptTokens: 2200, completionTokens: 150, cachedPromptTokens: 1000 } }) })
    let st = useAppStore.getState()
    expect(st.sessionInputTokens).toBe(2200)
    expect(st.sessionOutputTokens).toBe(150)
    expect(st.sessionCacheHitTokens).toBe(1000)
    expect(st.lastTurnCache).toMatchObject({ promptTokens: 1200, cachedTokens: 1000 })
    // The finish reports the same total again (or one more step): counted once.
    act(() => {
      emitRunEvent({ type: 'run', kind: 'finished', runId, status: 'done', run: summary({ status: 'done', assistantMessageId: bubble().id }),
        result: { content: '好了。', record: { ...record, status: 'done', steps: 3 }, usage: { promptTokens: 3500, completionTokens: 170, cachedPromptTokens: 2100 } } })
    })
    st = useAppStore.getState()
    expect(st.sessionInputTokens).toBe(3500)
    expect(st.sessionOutputTokens).toBe(170)
    expect(st.sessionCacheHitTokens).toBe(2100)
    h.unmount()
  })

  // Seen 2026-10-10: the phone's stream was suspended while the run
  // finished; the bubble stayed "working" with the timer counting.
  it('settles a run whose finish was missed once the tab catches up', async () => {
    const stub = stubEditor('<p>原文。</p>')
    const h = renderChatHook(stub.editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    const runId = 'run-1'
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId, run: summary({ assistantMessageId: bubble().id }) }) })
    act(() => { emitRunEvent({ type: 'run', kind: 'lock', runId, documentIds: ['doc-1'] }) })
    expect(useAppStore.getState().isStreaming).toBe(true)

    // The finished event never arrives; the server lists the run as done.
    listed = { runs: [{ ...summary({ status: 'done', assistantMessageId: bubble().id, userMessageId: 'u' }),
      result: { content: '写完了。', record: { status: 'done', steps: 1, trace: [], touched: [], timeline: [{ type: 'text', text: '写完了。' }] } } }], queueHeld: false }
    await act(async () => { emitRunCatchUp() })
    await flush()
    expect(useAppStore.getState().isStreaming).toBe(false)
    expect(bubble().content).toBe('写完了。')
    expect(bubble().agent?.status).toBe('done')
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(false)
    expect(useAppStore.getState().streamingReasoning).toBe('')
    h.unmount()
  })

  it('settles a followed run that has left the list, from the run alone', async () => {
    // 2026-10-18: finished runs leave the list (memory TTL, a restart); the tab kept the chapter locked.
    const h = renderChatHook(stubEditor('<p>原文。</p>').editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId: 'run-1', run: summary({ assistantMessageId: bubble().id }) }) })
    act(() => { emitRunEvent({ type: 'run', kind: 'lock', runId: 'run-1', documentIds: ['doc-1'] }) })
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(true)
    listed = { runs: [], queueHeld: false }
    single = { ...summary({ status: 'done', assistantMessageId: bubble().id, userMessageId: 'u' }),
      result: { content: '写完了。', record: { status: 'done', steps: 1, trace: [], touched: [], timeline: [{ type: 'text', text: '写完了。' }] } } }
    await act(async () => { emitRunCatchUp() })
    await flush()
    expect(useAppStore.getState().isStreaming).toBe(false)
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(false)
    expect(bubble().content).toBe('写完了。')
    h.unmount()
  })

  it('unlocks the tab when the followed run is gone from the server too', async () => {
    const h = renderChatHook(stubEditor('<p>原文。</p>').editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId: 'run-1', run: summary({ assistantMessageId: bubble().id }) }) })
    act(() => { emitRunEvent({ type: 'run', kind: 'lock', runId: 'run-1', documentIds: ['doc-1'] }) })
    listed = { runs: [], queueHeld: false }
    await act(async () => { emitRunCatchUp() })
    await flush()
    expect(useAppStore.getState().isStreaming).toBe(false)
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(false)
    h.unmount()
  })

  it('shows a run that paused while its events were missed as paused', async () => {
    const h = renderChatHook(stubEditor('<p>原文。</p>').editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId: 'run-1', run: summary({ assistantMessageId: bubble().id }) }) })
    listed = { runs: [summary({ status: 'paused', assistantMessageId: bubble().id, pause: { reason: 'step_failed', message: 'x' } as never })], queueHeld: false }
    await act(async () => { emitRunCatchUp() })
    await flush()
    expect(useAppStore.getState().isStreaming).toBe(false)
    expect(bubble().agent?.run?.status).toBe('paused')
    h.unmount()
  })

  // A text message mid-turn steers the run (below); one with images has no
  // steer path and queues as a turn of its own.
  it('queues a request with images sent mid-turn instead of refusing it', async () => {
    const h = renderChatHook()
    await act(async () => { await h.current.handleSendMessage(undefined, '第一条') })
    await flush()
    expect(useAppStore.getState().isStreaming).toBe(true)
    nextRun = body => summary({ id: 'run-2', status: 'queued', assistantMessageId: body.assistantMessageId, userMessageId: body.userMessageId })
    images.push('data:image/png;base64,AAAA')
    await act(async () => { await h.current.handleSendMessage(undefined, '第二条') })
    await flush()
    expect(posted.filter(p => p.url === '/api/books/book-1/runs')).toHaveLength(2)
    expect(bubble().agent?.status).toBe('queued')
    expect(bubble().agent?.run?.position).toBe(1)
    // Still streaming the first; a "send now" goes to the queued run.
    expect(useAppStore.getState().isStreaming).toBe(true)
    await act(async () => { await h.current.handleRunAction('run-2', 'start') })
    expect(posted.at(-1)?.url).toBe('/api/books/book-1/runs/run-2/start')
    h.unmount()
  })

  it('stops a server run: keeps the draft on screen, tells the server, settles on its finish', async () => {
    const stub = stubEditor('<p>原文。</p>')
    const h = renderChatHook(stub.editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写') })
    await flush()
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId: 'run-1', run: summary({ assistantMessageId: bubble().id }) }) })
    act(() => { emitRunEvent({ type: 'run', kind: 'preview', runId: 'run-1', documentId: 'doc-1', html: '<p>半截</p>' }) })
    act(() => { h.current.handleStopGeneration() })
    expect(posted.at(-1)?.url).toBe('/api/books/book-1/runs/run-1/stop')
    expect(useAppStore.getState().documents.find(d => d.id === 'doc-1')?.content).toBe('<p>半截</p>')
    expect(useAppStore.getState().isStreaming).toBe(false)
    act(() => {
      emitRunEvent({ type: 'run', kind: 'finished', runId: 'run-1', status: 'stopped', run: summary({ status: 'stopped', assistantMessageId: bubble().id }),
        result: { content: '⏹️ Stopped.', record: { status: 'stopped', steps: 1, trace: [], touched: [], timeline: [] } } })
    })
    expect(bubble().content).toContain('partial draft was kept')
    h.unmount()
  })

  // The server commits a selection rewrite itself. Its preview is painted
  // through real transactions, which Editor.tsx publishes to the store —
  // unless flagged: published, the chapter went unsynced, the server's
  // document event was ignored, and the tab's next save hit a 409.
  it('paints a selection preview with silent transactions, leaving the store alone', async () => {
    const flags: boolean[] = []
    const editor = new Editor({
      element: document.createElement('div'), extensions: [StarterKit], content: '<p>原文。</p>',
      onUpdate: ({ transaction }) => { flags.push(isSilentPreview(transaction)) }
    })
    nextRun = body => summary({ assistantMessageId: body.assistantMessageId, userMessageId: body.userMessageId, selectedText: '原文' })
    const h = renderChatHook(editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '改写选中') })
    await flush()
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId: 'run-1', run: summary({ assistantMessageId: bubble().id, selectedText: '原文' }) }) })
    act(() => { emitRunEvent({ type: 'run', kind: 'preview_selection', runId: 'run-1', documentId: 'doc-1', html: '<p>新文</p>' }) })
    expect(editor.getHTML()).toBe('<p>新文。</p>')
    expect(flags).toEqual([true])
    const stored = useAppStore.getState().documents.find(d => d.id === 'doc-1')!
    expect(stored.content).toBe('<p>原文。</p>')
    expect(stored.unsynced).toBeFalsy()
    editor.destroy()
    h.unmount()
  })

  it('spends the selection a finished turn rewrote, so the next message does not send it again', async () => {
    // 2026-10-25: the stale selection came back with the next message; that turn's rewrite went nowhere.
    useAppStore.getState().setSelectedText('原文')
    const run = summary({ selectedText: '原文', clientId: CLIENT_ID })
    nextRun = body => ({ ...run, assistantMessageId: body.assistantMessageId, userMessageId: body.userMessageId })
    const h = renderChatHook(stubEditor('<p>原文。</p>').editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '改写选中') })
    await flush()
    act(() => { emitRunEvent({ type: 'run', kind: 'finished', runId: 'run-1', status: 'done', run: { ...run, status: 'done', assistantMessageId: bubble().id },
      result: { content: '改好了。', record: { status: 'done', steps: 1, trace: [], touched: [], timeline: [] } } }) })
    expect(useAppStore.getState().selectedText).toBe('')
    h.unmount()
  })

  // The run reads the stored book; an edit still inside the save debounce
  // would be missing from it, and the run's write would race the save.
  it('saves an unsynced chapter before posting the turn', async () => {
    const s = useAppStore.getState()
    for (const d of s.documents) recordServerCopy(d.id, d)
    useAppStore.setState({ documents: s.documents.map(d => d.id === 'doc-1' ? { ...d, content: '<p>改过。</p>', unsynced: true } : d) })
    const h = renderChatHook(stubEditor('<p>改过。</p>').editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '继续') })
    await flush()
    const save = calls.indexOf('PUT /api/books/book-1/documents/doc-1')
    const post = calls.indexOf('POST /api/books/book-1/runs')
    expect(save).toBeGreaterThanOrEqual(0)
    expect(post).toBeGreaterThan(save)
    expect(calls.filter(c => c.startsWith('PUT /api/books/book-1/documents/'))).toEqual(['PUT /api/books/book-1/documents/doc-1'])
    h.unmount()
  })

  it('posts straight away when every chapter is synced', async () => {
    const s = useAppStore.getState()
    for (const d of s.documents) recordServerCopy(d.id, d)
    const h = renderChatHook(stubEditor('<p>原文。</p>').editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '继续') })
    await flush()
    expect(calls.some(c => c.startsWith('PUT /api/books/'))).toBe(false)
    expect(calls).toContain('POST /api/books/book-1/runs')
    h.unmount()
  })

  // A message typed while the run works steers it: the run takes it, the
  // chat shows it, and no bubble waits on a turn of its own.
  it('steers the running run with a message sent mid-turn', async () => {
    const stub = stubEditor('<p>原文。</p>')
    const h = renderChatHook(stub.editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId: 'run-1', run: summary({ assistantMessageId: bubble().id }) }) })
    const before = useAppStore.getState().messages.length
    await act(async () => { await h.current.handleSendMessage(undefined, '把第二段删掉') })
    await flush()
    // With the chat message's id: the run marks where it landed, for the chat to split the bubble there.
    const steered = useAppStore.getState().messages.find(m => m.role === 'user' && m.content === '把第二段删掉')
    expect(posted.at(-1)).toEqual({ url: '/api/books/book-1/runs/run-1/steer', body: { text: '把第二段删掉', messageId: steered?.id } })
    const messages = useAppStore.getState().messages
    expect(messages).toHaveLength(before + 1)
    expect(messages.at(-1)).toMatchObject({ role: 'user', content: '把第二段删掉' })
    expect(posted.filter(p => p.url === '/api/books/book-1/runs')).toHaveLength(1)
    h.unmount()
  })

  it('queues the message as a turn when the server refuses the steer', async () => {
    const stub = stubEditor('<p>原文。</p>')
    const h = renderChatHook(stub.editor)
    await act(async () => { await h.current.handleSendMessage(undefined, '写第一章') })
    await flush()
    act(() => { emitRunEvent({ type: 'run', kind: 'started', runId: 'run-1', run: summary({ assistantMessageId: bubble().id }) }) })
    steerRefused = true
    nextRun = body => summary({ id: 'run-2', status: 'queued', assistantMessageId: body.assistantMessageId, userMessageId: body.userMessageId })
    await act(async () => { await h.current.handleSendMessage(undefined, '再来一章') })
    await flush()
    expect(posted.filter(p => p.url === '/api/books/book-1/runs')).toHaveLength(2)
    expect(useAppStore.getState().messages.at(-1)?.role).toBe('assistant')
    expect(useAppStore.getState().messages.at(-2)).toMatchObject({ role: 'user', content: '再来一章' })
    h.unmount()
  })

  it('finds the book\'s running run on load and renders it into new bubbles', async () => {
    listed = { runs: [{ ...summary({ userMessageId: 'u-far', assistantMessageId: 'a-far', prompt: '远端发的' }), liveText: '正在写…', liveReasoning: '' }], queueHeld: false }
    const h = renderChatHook()
    await flush()
    await flush()
    const ids = useAppStore.getState().messages.map(m => m.id)
    expect(ids).toContain('u-far')
    expect(ids).toContain('a-far')
    expect(bubble().agent?.run).toEqual({ id: 'run-1', status: 'running' })
    expect(bubble().agent?.live).toBe('正在写…')
    expect(useAppStore.getState().isStreaming).toBe(true)
    h.unmount()
  })
})

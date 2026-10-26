import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useAppStore } from '../useAppStore'

/**
 * Version history is the safety net behind every LLM edit, and all three of
 * its parts were broken at once: snapshots never reached the server, the sync
 * erased the local copy with the server's empty list, and the 50-snapshot cap
 * was global so one book evicted another's history.
 *
 * Restoring had a fourth defect of the same family as the chapter-emptying bug:
 * a snapshot from the server carries `content: ''` until fetched, and restore
 * wrote that straight into the document.
 */
const calls: Array<{ url: string; method: string; body: unknown }> = []

const doc = (content: string) => ([{
  id: 'doc-1', title: 'Chapter 1', content, contentLoaded: true, createdAt: '', updatedAt: ''
}])

beforeEach(() => {
  calls.length = 0
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.spyOn(console, 'info').mockImplementation(() => {})
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method || 'GET', body: init?.body ? JSON.parse(String(init.body)) : undefined })
    return { ok: true, status: 200, json: async () => ({ content: '<p>from the server</p>' }) } as Response
  }))
  useAppStore.setState({
    documents: doc('<p>current text</p>') as never,
    activeDocumentId: 'doc-1',
    activeBookId: 'book-1',
    versions: [],
    user: { username: 'alice' } as never,
    csrfToken: 'tok'
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  useAppStore.setState({ user: null, versions: [] })
})

describe('createVersionSnapshot', () => {
  it('writes the snapshot through to the server', () => {
    // The whole cause of the 7-week gap: the endpoint existed, nothing called it.
    useAppStore.getState().createVersionSnapshot('Auto-save before: "扩写"')

    const post = calls.find(c => c.method === 'POST')
    expect(post?.url).toBe('/api/books/book-1/versions')
    expect(post?.body).toMatchObject({
      documentId: 'doc-1',
      title: 'Auto-save before: "扩写"',
      content: '<p>current text</p>'
    })
  })

  it('stamps the book on the snapshot', () => {
    useAppStore.getState().createVersionSnapshot()
    expect(useAppStore.getState().versions[0].bookId).toBe('book-1')
  })

  it('caps per book, not globally', () => {
    // 60 snapshots belonging to another book must not evict this book's.
    const others = Array.from({ length: 60 }, (_, i) => ({
      id: `other-${i}`, documentId: 'doc-9', bookId: 'book-other',
      timestamp: `2026-01-01T00:${String(i).padStart(2, '0')}:00Z`, title: 'x', content: '<p>y</p>'
    }))
    useAppStore.setState({ versions: others })

    useAppStore.getState().createVersionSnapshot('mine')
    const after = useAppStore.getState().versions
    expect(after.filter(v => v.bookId === 'book-other')).toHaveLength(60)
    expect(after.filter(v => v.bookId === 'book-1')).toHaveLength(1)
  })

  it('keeps at most 50 for the active book', () => {
    const mine = Array.from({ length: 50 }, (_, i) => ({
      id: `mine-${i}`, documentId: 'doc-1', bookId: 'book-1',
      timestamp: `2026-01-01T00:${String(i).padStart(2, '0')}:00Z`, title: 'x', content: '<p>y</p>'
    }))
    useAppStore.setState({ versions: mine })

    useAppStore.getState().createVersionSnapshot('newest')
    const mineAfter = useAppStore.getState().versions.filter(v => v.bookId === 'book-1')
    expect(mineAfter).toHaveLength(50)
    expect(mineAfter[0].title).toBe('newest')          // the new one survived
    expect(mineAfter.some(v => v.id === 'mine-49')).toBe(false)  // the oldest went
  })

  it('does nothing when there is no active document', () => {
    useAppStore.setState({ documents: [] as never })
    useAppStore.getState().createVersionSnapshot()
    expect(useAppStore.getState().versions).toEqual([])
    expect(calls.filter(c => c.method === 'POST')).toHaveLength(0)
  })
})

describe('restoreVersion', () => {
  it('fetches the content of a server-side snapshot instead of restoring nothing', async () => {
    useAppStore.setState({
      versions: [{ id: 'ver-1', documentId: 'doc-1', bookId: 'book-1', timestamp: '2026-08-16T00:00:00Z', title: 'server snap', content: '' }]
    })

    await useAppStore.getState().restoreVersion('ver-1')

    expect(calls.some(c => c.url === '/api/books/book-1/versions/ver-1' && c.method === 'GET')).toBe(true)
    expect(useAppStore.getState().documents[0].content).toBe('<p>from the server</p>')
  })

  it('refuses to restore when no content can be had', async () => {
    // Writing the empty string here is precisely how a "restore" would have
    // destroyed the chapter it was asked to rescue.
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) } as Response)))
    useAppStore.setState({
      versions: [{ id: 'ver-1', documentId: 'doc-1', bookId: 'book-1', timestamp: '2026-08-16T00:00:00Z', title: 'gone', content: '' }]
    })

    await useAppStore.getState().restoreVersion('ver-1')
    expect(useAppStore.getState().documents[0].content).toBe('<p>current text</p>')
  })

  it('restores local content without a round trip', async () => {
    useAppStore.setState({
      versions: [{ id: 'ver-1', documentId: 'doc-1', bookId: 'book-1', timestamp: '2026-08-16T00:00:00Z', title: 'local', content: '<p>older draft</p>' }]
    })

    await useAppStore.getState().restoreVersion('ver-1')

    expect(useAppStore.getState().documents[0].content).toBe('<p>older draft</p>')
    expect(calls.some(c => c.method === 'GET')).toBe(false)
  })

  it('snapshots what it replaces, and syncs that too', async () => {
    useAppStore.setState({
      versions: [{ id: 'ver-1', documentId: 'doc-1', bookId: 'book-1', timestamp: '2026-08-16T00:00:00Z', title: 'older', content: '<p>older draft</p>' }]
    })

    await useAppStore.getState().restoreVersion('ver-1')

    const titles = useAppStore.getState().versions.map(v => v.title)
    expect(titles.some(t => t.startsWith('Auto-save before restoring'))).toBe(true)
    const post = calls.find(c => c.method === 'POST')
    expect(post?.body).toMatchObject({ content: '<p>current text</p>' })
  })
})

describe('deleteVersionSnapshot', () => {
  it('deletes on the server too, or the snapshot returns on the next sync', () => {
    useAppStore.setState({
      versions: [{ id: 'ver-1', documentId: 'doc-1', bookId: 'book-1', timestamp: '2026-08-16T00:00:00Z', title: 'x', content: '<p>y</p>' }]
    })

    useAppStore.getState().deleteVersionSnapshot('ver-1')

    expect(useAppStore.getState().versions).toEqual([])
    expect(calls.some(c => c.url === '/api/books/book-1/versions/ver-1' && c.method === 'DELETE')).toBe(true)
  })
})

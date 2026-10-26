import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useAppStore } from '../useAppStore'
import type { CanvasDocument } from '../../types/document'
import {
  CLIENT_ID, markSaved, mergeServerChapters, needsTextSync, recordServerCopy, saveOtherBookEdits, serverCopies
} from '../documentSync'
import { applyBookEvent } from '../bookEvents'

// Chapter sync against server revisions (backend_authority.md phase 1): an
// unsynced edit survives a reload, a save names its base revision, and a
// save that lost the race is kept in version history instead of vanishing.

const T = '2026-10-06T00:00:00Z'
const chapter = (id: string, extra: Partial<CanvasDocument> = {}): CanvasDocument => ({
  id, title: `Ch ${id}`, content: `<p>${id}</p>`, contentLoaded: true, createdAt: T, updatedAt: T, revision: 1, ...extra
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

beforeEach(() => {
  serverCopies.clear()
  useAppStore.setState({
    user: { username: 'alice' },
    activeBookId: 'book-1',
    csrfToken: 'tok',
    documents: [],
    versions: [],
    syncNotice: null,
  })
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('mergeServerChapters', () => {
  it('keeps a local chapter with an unsynced edit instead of an empty stub', () => {
    const server = [chapter('a', { content: '', contentLoaded: false, revision: 3 })]
    const local = [chapter('a', { content: '<p>typed before the tab closed</p>', unsynced: true, revision: 2 })]
    const [merged] = mergeServerChapters(server, local)
    expect(merged.content).toBe('<p>typed before the tab closed</p>')
    expect(merged.unsynced).toBe(true)
    // Its save must name the revision the edit was based on, not the server's.
    expect(merged.revision).toBe(2)
  })

  it('takes the server stub for a synced chapter', () => {
    const server = [chapter('a', { content: '', contentLoaded: false, revision: 3 })]
    const [merged] = mergeServerChapters(server, [chapter('a', { content: '<p>old</p>' })])
    expect(merged.content).toBe('')
    expect(merged.revision).toBe(3)
  })

  it('keeps an unsynced chapter only this device has', () => {
    const merged = mergeServerChapters([chapter('a')], [chapter('a'), chapter('offline', { unsynced: true })])
    expect(merged.map(d => d.id)).toEqual(['a', 'offline'])
  })

  it('drops a synced chapter the server no longer lists', () => {
    expect(mergeServerChapters([chapter('a')], [chapter('a'), chapter('gone')]).map(d => d.id)).toEqual(['a'])
  })
})

describe('needsTextSync and markSaved', () => {
  it('sends a chapter whose text differs from the server copy, or that is flagged', () => {
    const doc = chapter('a')
    expect(needsTextSync(doc)).toBe(true) // never confirmed
    recordServerCopy('a', doc)
    expect(needsTextSync(doc)).toBe(false)
    expect(needsTextSync({ ...doc, unsynced: true })).toBe(true)
    expect(needsTextSync({ ...doc, content: '<p>new</p>' })).toBe(true)
  })

  it('clears the flag and takes the new revision when the chapter did not change meanwhile', () => {
    const doc = chapter('a', { unsynced: true })
    useAppStore.setState({ documents: [doc] })
    markSaved('a', doc, 2)
    const saved = useAppStore.getState().documents[0]
    expect(saved.unsynced).toBe(false)
    expect(saved.revision).toBe(2)
  })

  it('stays unsynced, now on the new base, when the user typed during the save', () => {
    const sent = chapter('a', { unsynced: true })
    useAppStore.setState({ documents: [{ ...sent, content: '<p>typed during the save</p>' }] })
    markSaved('a', sent, 2)
    const doc = useAppStore.getState().documents[0]
    expect(doc.unsynced).toBe(true)
    expect(doc.revision).toBe(2)
  })
})

describe('editing marks a chapter unsynced', () => {
  it('on a text change, not on a summary change', () => {
    useAppStore.setState({ documents: [chapter('a')], activeDocumentId: 'a' })
    useAppStore.getState().updateDocument('a', { summary: 'S' })
    expect(useAppStore.getState().documents[0].unsynced).toBeFalsy()
    useAppStore.getState().updateDocument('a', { content: '<p>edited</p>' })
    expect(useAppStore.getState().documents[0].unsynced).toBe(true)
  })
})

describe('syncToServer with revisions', () => {
  type Call = { url: string; method: string; body: Record<string, unknown> | null; headers: Record<string, string> }
  let calls: Call[]
  const stubServer = (docPut: (body: Record<string, unknown>) => Response) => {
    calls = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null
      calls.push({ url: String(url), method, body, headers: (init?.headers ?? {}) as Record<string, string> })
      if (method === 'PUT' && String(url).includes('/documents/')) return docPut(body)
      return json({ success: true, updatedAt: T })
    }))
  }
  const docPuts = () => calls.filter(c => c.method === 'PUT' && c.url.includes('/documents/'))

  it('sends an edited chapter with its base revision and this tab id', async () => {
    useAppStore.setState({ documents: [chapter('a', { content: '<p>edited</p>', unsynced: true, revision: 4 })] })
    stubServer(() => json({ success: true, revision: 5, updatedAt: T }))
    await useAppStore.getState().syncToServer()
    const [put] = docPuts()
    expect(put.body).toMatchObject({ content: '<p>edited</p>', baseRevision: 4 })
    expect(put.headers['X-Client-Id']).toBe(CLIENT_ID)
    const doc = useAppStore.getState().documents[0]
    expect(doc.revision).toBe(5)
    expect(doc.unsynced).toBe(false)
  })

  it('sends only the summary when the text is what the server has', async () => {
    const doc = chapter('a')
    recordServerCopy('a', doc)
    useAppStore.setState({ documents: [{ ...doc, summary: 'new summary' }] })
    stubServer(() => json({ success: true, revision: 1, updatedAt: T }))
    await useAppStore.getState().syncToServer()
    const [put] = docPuts()
    expect(put.body).toEqual({ summary: 'new summary', summaryContentHash: null })
  })

  it('skips a chapter the server already holds', async () => {
    const doc = chapter('a')
    recordServerCopy('a', doc)
    useAppStore.setState({ documents: [doc] })
    stubServer(() => json({ success: true }))
    await useAppStore.getState().syncToServer()
    expect(docPuts()).toHaveLength(0)
  })

  it('on a conflict keeps the local text in version history and shows the server copy', async () => {
    useAppStore.setState({ documents: [chapter('a', { content: '<p>mine</p>', unsynced: true, revision: 1 })] })
    stubServer(() => json({ error: 'conflict', revision: 2, title: 'Ch a', content: '<p>theirs</p>' }, 409))
    await useAppStore.getState().syncToServer()

    const s = useAppStore.getState()
    expect(s.documents[0]).toMatchObject({ content: '<p>theirs</p>', revision: 2, unsynced: false })
    expect(s.versions).toHaveLength(1)
    expect(s.versions[0]).toMatchObject({ documentId: 'a', content: '<p>mine</p>' })
    expect(s.syncNotice).toEqual({ kind: 'conflict', documentTitle: 'Ch a' })
  })

  it('a conflict between identical copies is adopted silently', async () => {
    useAppStore.setState({ documents: [chapter('a', { content: '<p>same</p>', unsynced: true, revision: 1 })] })
    stubServer(() => json({ error: 'conflict', revision: 2, title: 'Ch a', content: '<p>same</p>' }, 409))
    await useAppStore.getState().syncToServer()
    const s = useAppStore.getState()
    expect(s.documents[0]).toMatchObject({ revision: 2, unsynced: false })
    expect(s.versions).toHaveLength(0)
    expect(s.syncNotice).toBeNull()
  })
})

describe('applyBookEvent', () => {
  const fetchChapter = (content: string, revision: number) =>
    vi.fn(async () => json({ id: 'a', title: 'Ch a', content, revision }))

  it('reloads the attachment list when a run saved one (web_save)', async () => {
    const loadAttachments = vi.fn(async () => {})
    const original = useAppStore.getState().loadAttachments
    useAppStore.setState({ loadAttachments })
    try {
      await applyBookEvent({ id: 1, type: 'attachments', kind: 'created', clientId: null }, { bookId: 'book-1' })
      expect(loadAttachments).toHaveBeenCalledWith('book-1')
    } finally {
      useAppStore.setState({ loadAttachments: original })
    }
  })

  it('ignores the echo of this tab’s own write', async () => {
    useAppStore.setState({ documents: [chapter('a')] })
    const fetchFn = fetchChapter('<p>x</p>', 2)
    await applyBookEvent({ id: 1, type: 'document', kind: 'updated', documentId: 'a', revision: 2, clientId: CLIENT_ID }, { bookId: 'book-1', fetchFn })
    expect(fetchFn).not.toHaveBeenCalled()
  })

  it('loads another tab’s newer copy of an open chapter', async () => {
    useAppStore.setState({ documents: [chapter('a')] })
    const fetchFn = fetchChapter('<p>from the phone</p>', 2)
    await applyBookEvent({ id: 1, type: 'document', kind: 'updated', documentId: 'a', revision: 2, clientId: 'other' }, { bookId: 'book-1', fetchFn })
    expect(useAppStore.getState().documents[0]).toMatchObject({ content: '<p>from the phone</p>', revision: 2, unsynced: false })
    // Recorded as the server's copy: it is not sent back.
    expect(needsTextSync(useAppStore.getState().documents[0])).toBe(false)
  })

  it('never overwrites an unsynced edit; that save will conflict instead', async () => {
    useAppStore.setState({ documents: [chapter('a', { content: '<p>typing</p>', unsynced: true })] })
    const fetchFn = fetchChapter('<p>from the phone</p>', 2)
    await applyBookEvent({ id: 1, type: 'document', kind: 'updated', documentId: 'a', revision: 2, clientId: 'other' }, { bookId: 'book-1', fetchFn })
    expect(fetchFn).not.toHaveBeenCalled()
    expect(useAppStore.getState().documents[0].content).toBe('<p>typing</p>')
  })

  it('does not adopt a fetch that raced a keystroke', async () => {
    useAppStore.setState({ documents: [chapter('a')] })
    const fetchFn = vi.fn(async () => {
      useAppStore.getState().updateDocument('a', { content: '<p>typed meanwhile</p>' })
      return json({ id: 'a', title: 'Ch a', content: '<p>from the phone</p>', revision: 2 })
    })
    await applyBookEvent({ id: 1, type: 'document', kind: 'updated', documentId: 'a', revision: 2, clientId: 'other' }, { bookId: 'book-1', fetchFn })
    expect(useAppStore.getState().documents[0].content).toBe('<p>typed meanwhile</p>')
  })

  it('skips an event no newer than what this tab holds', async () => {
    useAppStore.setState({ documents: [chapter('a', { revision: 3 })] })
    const fetchFn = fetchChapter('<p>x</p>', 2)
    await applyBookEvent({ id: 1, type: 'document', kind: 'updated', documentId: 'a', revision: 2, clientId: 'other' }, { bookId: 'book-1', fetchFn })
    expect(fetchFn).not.toHaveBeenCalled()
  })

  it('only moves the revision of a chapter not loaded here', async () => {
    useAppStore.setState({ documents: [chapter('a', { content: '', contentLoaded: false })] })
    const fetchFn = fetchChapter('<p>x</p>', 2)
    await applyBookEvent({ id: 1, type: 'document', kind: 'updated', documentId: 'a', revision: 2, clientId: 'other' }, { bookId: 'book-1', fetchFn })
    expect(fetchFn).not.toHaveBeenCalled()
    expect(useAppStore.getState().documents[0]).toMatchObject({ revision: 2, contentLoaded: false })
  })

  it('a deletion elsewhere drops the chapter, keeping one with an unsynced edit', async () => {
    useAppStore.setState({
      documents: [chapter('a'), chapter('b'), chapter('c', { unsynced: true })],
      activeDocumentId: 'b'
    })
    const fetchFn = vi.fn(async () => json({ documents: [{ id: 'a', title: 'Ch a', createdAt: T, updatedAt: T, revision: 1 }] }))
    await applyBookEvent({ id: 1, type: 'document', kind: 'deleted', documentId: 'b', clientId: 'other' }, { bookId: 'book-1', fetchFn })
    const s = useAppStore.getState()
    expect(s.documents.map(d => d.id)).toEqual(['a', 'c'])
    // The open chapter is gone: open one that exists.
    expect(s.activeDocumentId).toBe('a')
  })

  it('a chapter created elsewhere appears, unloaded', async () => {
    useAppStore.setState({ documents: [chapter('a')], activeDocumentId: 'a' })
    const fetchFn = vi.fn(async () => json({ documents: [
      { id: 'a', title: 'Ch a', createdAt: T, updatedAt: T, revision: 1 },
      { id: 'n', title: 'New', createdAt: T, updatedAt: T, revision: 1 }
    ] }))
    await applyBookEvent({ id: 1, type: 'documents', kind: 'created', documentIds: ['n'], clientId: 'other' }, { bookId: 'book-1', fetchFn })
    const docs = useAppStore.getState().documents
    expect(docs.map(d => d.id)).toEqual(['a', 'n'])
    expect(docs[0].content).toBe('<p>a</p>')
    expect(docs[1]).toMatchObject({ contentLoaded: false, content: '' })
  })
})

describe('saveOtherBookEdits', () => {
  const run = async (status: number) => {
    const calls: Array<{ url: string; method: string; body: Record<string, unknown> }> = []
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), method: init?.method ?? 'GET', body: JSON.parse(String(init?.body)) })
      return calls.length === 1 ? json({}, status) : json({ success: true })
    }) as unknown as typeof fetch
    await saveOtherBookEdits('book-x', [chapter('a', { content: '<p>mine</p>', unsynced: true, revision: 3 }), chapter('clean')], fetchFn)
    return calls
  }

  it('sends only unsynced chapters, with their base revision, to their own book', async () => {
    const calls = await run(200)
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ url: '/api/books/book-x/documents/a', method: 'PUT', body: { content: '<p>mine</p>', baseRevision: 3 } })
  })

  it('creates a chapter that book does not have', async () => {
    const calls = await run(404)
    expect(calls[1]).toMatchObject({ url: '/api/books/book-x/documents', method: 'POST' })
  })

  it('keeps a chapter that lost the race in that book’s version history', async () => {
    const calls = await run(409)
    expect(calls[1]).toMatchObject({ url: '/api/books/book-x/versions', method: 'POST', body: { documentId: 'a', content: '<p>mine</p>' } })
  })
})

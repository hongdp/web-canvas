/**
 * Which book opens on a device that has no local pointer — or a stale one.
 *
 * User-reported: logging in on a new device opened a book other than the one
 * last edited. The choice came from localStorage alone, so a fresh browser
 * always got 'default'. The server now reports the account's last active book
 * with the session; it must win, with the local key and then 'default' as
 * fallbacks. Drives the real initializeStoreFromServer against a stubbed API.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useAppStore, initializeStoreFromServer } from '../useAppStore'
import { setIsInitialized } from '../syncRuntime'
import { localStorage as ls } from '../persistence'

const bookPayload = (id: string) => ({
  bookTitle: `Title of ${id}`,
  activeDocumentId: `${id}-doc`,
  updatedAt: '2026-09-06T00:00:00Z',
  documents: [{
    id: `${id}-doc`, title: 'Chapter 1', sortOrder: 0,
    createdAt: '2026-09-01T00:00:00Z', updatedAt: '2026-09-01T00:00:00Z'
  }],
  versions: [],
  messages: []
})

/** Stub the API; returns the request log so a test can see which book was fetched. */
function stubServer(session: Record<string, unknown>): string[] {
  const requested: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    const method = init?.method ?? 'GET'
    requested.push(`${method} ${u}`)
    if (u === '/api/auth/session') return new Response(JSON.stringify(session), { status: 200 })
    const book = u.match(/^\/api\/books\/([^/]+)$/)
    if (book && method === 'GET') return new Response(JSON.stringify(bookPayload(book[1])), { status: 200 })
    if (u.includes('/documents/')) return new Response(JSON.stringify({ content: '<p>text</p>' }), { status: 200 })
    if (u === '/api/books') return new Response(JSON.stringify([]), { status: 200 })
    return new Response(JSON.stringify({ success: true }), { status: 200 })
  }))
  return requested
}

beforeEach(() => {
  // The auto-save the init triggers is a debounced timer; keep it from firing.
  vi.useFakeTimers()
  // jsdom has no IndexedDB; the persistence wrappers log and move on.
  vi.spyOn(console, 'error').mockImplementation(() => {})
  window.localStorage.clear()
  // Skip the IndexedDB bootstrap: this is about the remote half of init.
  setIsInitialized(true)
  useAppStore.setState({ user: null, activeBookId: 'default', bookTitle: 'Untitled Book', serverSaveStatus: 'saved' })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('initial book choice', () => {
  it('opens the book the server says was last active, over a stale local pointer', async () => {
    ls.setItem('web_canvas_active_book_id', 'book-old')
    useAppStore.setState({ activeBookId: 'book-old' })
    const requested = stubServer({ loggedIn: true, username: 'alice', csrfToken: 't', lastActiveBookId: 'book-new' })

    await initializeStoreFromServer(true)

    expect(requested).toContain('GET /api/books/book-new')
    expect(requested).not.toContain('GET /api/books/book-old')
    expect(useAppStore.getState().activeBookId).toBe('book-new')
    expect(useAppStore.getState().bookTitle).toBe('Title of book-new')
    // Later saves and the next cold start must agree with what was opened.
    expect(ls.getItem('web_canvas_active_book_id')).toBe('book-new')
  })

  it('falls back to the local pointer when the server has none (brand-new account)', async () => {
    ls.setItem('web_canvas_active_book_id', 'book-local')
    useAppStore.setState({ activeBookId: 'book-local' })
    const requested = stubServer({ loggedIn: true, username: 'alice', csrfToken: 't', lastActiveBookId: null })

    await initializeStoreFromServer(true)

    expect(requested).toContain('GET /api/books/book-local')
    expect(useAppStore.getState().activeBookId).toBe('book-local')
  })

  it("falls back to 'default' on a device with nothing at all", async () => {
    const requested = stubServer({ loggedIn: true, username: 'alice', csrfToken: 't' })

    await initializeStoreFromServer(true)

    expect(requested).toContain('GET /api/books/default')
    expect(useAppStore.getState().activeBookId).toBe('default')
  })
})

// The local cache holds the book this device had open last. An edit in it
// the server never got (the tab closed inside the save debounce) must reach
// ITS book — kept when that book opens, saved back when another one does.
describe('unsynced chapters in the local cache', () => {
  const unsynced = (id: string) => ({
    id, title: 'Typed offline', content: '<p>not on the server yet</p>', contentLoaded: true,
    createdAt: '2026-09-01T00:00:00Z', updatedAt: '2026-09-01T00:00:00Z', revision: 1, unsynced: true
  })

  it('are kept when their own book opens', async () => {
    ls.setItem('web_canvas_active_book_id', 'book-a')
    useAppStore.setState({ activeBookId: 'book-a', documents: [unsynced('book-a-doc')] })
    const requested = stubServer({ loggedIn: true, username: 'alice', csrfToken: 't', lastActiveBookId: 'book-a' })

    await initializeStoreFromServer(true)

    const doc = useAppStore.getState().documents.find(d => d.id === 'book-a-doc')
    expect(doc).toMatchObject({ content: '<p>not on the server yet</p>', unsynced: true })
    // The server's copy is not loaded over it.
    expect(requested).not.toContain('GET /api/books/book-a/documents/book-a-doc')
  })

  it('are saved to their own book, and kept out of the one that opens', async () => {
    ls.setItem('web_canvas_active_book_id', 'book-a')
    useAppStore.setState({ activeBookId: 'book-a', documents: [unsynced('a-only')] })
    const requested = stubServer({ loggedIn: true, username: 'alice', csrfToken: 't', lastActiveBookId: 'book-b' })

    await initializeStoreFromServer(true)

    expect(requested).toContain('PUT /api/books/book-a/documents/a-only')
    expect(useAppStore.getState().activeBookId).toBe('book-b')
    expect(useAppStore.getState().documents.map(d => d.id)).toEqual(['book-b-doc'])
  })
})

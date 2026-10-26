/**
 * Cross-account workspace isolation.
 *
 * Observed on a live server: signing in as a brand-new account showed another
 * account's book, and the auto-save then wrote that content into the new
 * account's server storage. The document/version caches live in the BROWSER,
 * so switching accounts must drop them before anything syncs.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  getCachedWorkspaceOwner,
  setCachedWorkspaceOwner,
  clearWorkspaceCache,
  db,
  localStorage as ls
} from '../persistence'

// jsdom has no IndexedDB, so the wrapper is spied on directly — what matters
// here is WHICH keys the cleanup touches, not the storage engine.
beforeEach(() => {
  window.localStorage.clear()
  vi.restoreAllMocks()
})

describe('workspace cache ownership', () => {
  it('starts with no owner', () => {
    expect(getCachedWorkspaceOwner()).toBeNull()
  })

  it('remembers and forgets the owner', () => {
    setCachedWorkspaceOwner('alice')
    expect(getCachedWorkspaceOwner()).toBe('alice')
    setCachedWorkspaceOwner(null)
    expect(getCachedWorkspaceOwner()).toBeNull()
  })
})

describe('clearWorkspaceCache', () => {
  it('removes every cached chapter, not just the index', async () => {
    // Documents are one record PER CHAPTER since the v3 layout; dropping only
    // the index would leave every chapter's text in IndexedDB after a logout
    // or an account switch.
    vi.spyOn(db, 'get').mockResolvedValue({ version: 3, ids: ['d1', 'd2'] } as never)
    const removed: string[] = []
    vi.spyOn(db, 'remove').mockImplementation(async (key: string) => { removed.push(key) })

    await clearWorkspaceCache()

    expect(removed).toContain('web_canvas_doc:d1')
    expect(removed).toContain('web_canvas_doc:d2')
    expect(removed).toContain('web_canvas_documents')
    expect(removed).toContain('web_canvas_versions')
  })

  it('still clears the index and versions when no chapters are cached', async () => {
    vi.spyOn(db, 'get').mockResolvedValue(null as never)
    const removed: string[] = []
    vi.spyOn(db, 'remove').mockImplementation(async (key: string) => { removed.push(key) })

    await clearWorkspaceCache()

    expect(removed).toEqual(['web_canvas_documents', 'web_canvas_versions'])
  })

  it('drops the pointers that would resurrect the previous book', async () => {
    vi.spyOn(db, 'get').mockResolvedValue(null as never)
    vi.spyOn(db, 'remove').mockResolvedValue(undefined as never)
    ls.setItem('web_canvas_active_book_id', 'book-of-previous-user')
    ls.setItem('web_canvas_active_document_id', 'doc-of-previous-user')
    ls.setItem('web_canvas_book_title', 'Previous User Book')

    await clearWorkspaceCache()

    expect(ls.getItem('web_canvas_active_book_id')).toBeNull()
    expect(ls.getItem('web_canvas_active_document_id')).toBeNull()
    expect(ls.getItem('web_canvas_book_title')).toBeNull()
  })

  it('survives a storage failure instead of blocking sign-in', async () => {
    vi.spyOn(db, 'get').mockRejectedValue(new Error('IDB unavailable'))
    vi.spyOn(db, 'remove').mockResolvedValue(undefined as never)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    await expect(clearWorkspaceCache()).resolves.toBeUndefined()
  })
})

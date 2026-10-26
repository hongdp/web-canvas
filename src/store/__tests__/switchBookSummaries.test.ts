/**
 * Chapter summaries do not travel between books.
 *
 * Chapter ids repeat across books (most books' first chapter is "doc-1").
 * switchBook carried a summary over from the chapters on screen by id, so
 * switching from one book to another put the first book's chapter summary on
 * the second book's chapter of the same id; it was then saved as that
 * chapter's own and shown in the chapter index (seen on a multi-book
 * account, 2026-10-11). Summaries are no longer used and the carry-over is gone.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useAppStore } from '../useAppStore'
import type { CanvasDocument } from '../../types/document'

const chapter = (over: Partial<CanvasDocument>): CanvasDocument => ({
  id: 'doc-1', title: 'Chapter 1', content: '<p>text</p>', contentLoaded: true,
  createdAt: '2026-10-01T00:00:00Z', updatedAt: '2026-10-01T00:00:00Z', ...over
})

function serveBook(bookId: string, documents: Array<Record<string, unknown>>) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === `/api/books/${bookId}`) {
      return new Response(JSON.stringify({ bookTitle: bookId, documents, versions: [], messages: [] }), { status: 200 })
    }
    return new Response(JSON.stringify({ content: '<p>loaded</p>' }), { status: 200 })
  })
}

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  useAppStore.setState({
    user: { username: 'alice' },
    activeBookId: 'book-a',
    documents: [chapter({ summary: "Book A's first chapter: a camping trip.", summaryContentHash: 'h-a' })]
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('switchBook', () => {
  it("does not give another book's chapter summary to a chapter of the same id", async () => {
    serveBook('book-b', [{ id: 'doc-1', title: 'Chapter 1 of B', createdAt: 't', updatedAt: 't' }])

    await useAppStore.getState().switchBook('book-b')

    const doc = useAppStore.getState().documents.find(d => d.id === 'doc-1')
    expect(useAppStore.getState().activeBookId).toBe('book-b')
    expect(doc?.summary).toBeUndefined()
    expect(doc?.summaryContentHash).toBeUndefined()
  })
})

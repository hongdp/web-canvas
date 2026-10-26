import { describe, it, expect, vi } from 'vitest'
import { idsNeedingContent, loadDocumentContents, type LoadableDoc } from '../contentLoader'

const docs: LoadableDoc[] = [
  { id: 'loaded-true', contentLoaded: true },
  { id: 'loaded-undef' },
  { id: 'unloaded-a', contentLoaded: false },
  { id: 'unloaded-b', contentLoaded: false }
]

// The server's revision of the chapter rides along (documentSync).
const okResponse = (content: string) =>
  ({ ok: true, json: async () => ({ content, revision: 4 }) }) as Response

describe('idsNeedingContent', () => {
  it('keeps only docs explicitly marked contentLoaded: false', () => {
    expect(idsNeedingContent(['loaded-true', 'loaded-undef', 'unloaded-a'], docs))
      .toEqual(['unloaded-a'])
  })

  it('ignores unknown ids and dedupes', () => {
    expect(idsNeedingContent(['ghost', 'unloaded-a', 'unloaded-a', 'unloaded-b'], docs))
      .toEqual(['unloaded-a', 'unloaded-b'])
  })

  it('returns empty for empty input', () => {
    expect(idsNeedingContent([], docs)).toEqual([])
  })
})

describe('loadDocumentContents', () => {
  it('fetches each doc and reports loaded content', async () => {
    const fetchFn = vi.fn(async (url: string | URL | Request) =>
      okResponse(`content-for ${String(url)}`))
    const loaded: Record<string, string> = {}

    await loadDocumentContents('book-1', ['unloaded-a', 'unloaded-b'], {
      fetchFn: fetchFn as unknown as typeof fetch,
      onLoaded: (id, content) => { loaded[id] = content }
    })

    expect(fetchFn).toHaveBeenCalledTimes(2)
    expect(fetchFn).toHaveBeenCalledWith('/api/books/book-1/documents/unloaded-a')
    expect(loaded['unloaded-a']).toBe('content-for /api/books/book-1/documents/unloaded-a')
    expect(loaded['unloaded-b']).toBe('content-for /api/books/book-1/documents/unloaded-b')
  })

  it('dedupes concurrent requests for the same doc', async () => {
    let release!: (r: Response) => void
    const gate = new Promise<Response>(resolve => { release = resolve })
    const fetchFn = vi.fn(() => gate)
    const onLoaded = vi.fn()

    const first = loadDocumentContents('book-1', ['unloaded-a'], {
      fetchFn: fetchFn as unknown as typeof fetch, onLoaded
    })
    const second = loadDocumentContents('book-1', ['unloaded-a'], {
      fetchFn: fetchFn as unknown as typeof fetch, onLoaded
    })

    release(okResponse('<p>once</p>'))
    await Promise.all([first, second])

    expect(fetchFn).toHaveBeenCalledTimes(1)
    expect(onLoaded).toHaveBeenCalledTimes(1)
    expect(onLoaded).toHaveBeenCalledWith('unloaded-a', '<p>once</p>', 4)
  })

  it('does not dedupe across different books', async () => {
    const fetchFn = vi.fn(async () => okResponse('x'))
    const onLoaded = vi.fn()
    await Promise.all([
      loadDocumentContents('book-1', ['unloaded-a'], { fetchFn: fetchFn as unknown as typeof fetch, onLoaded }),
      loadDocumentContents('book-2', ['unloaded-a'], { fetchFn: fetchFn as unknown as typeof fetch, onLoaded })
    ])
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('settles without rejecting when a fetch fails, and skips onLoaded', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const fetchFn = vi.fn(async (url: string | URL | Request) => {
      if (String(url).includes('unloaded-a')) throw new Error('network down')
      return okResponse('<p>ok</p>')
    })
    const onLoaded = vi.fn()

    await expect(loadDocumentContents('book-1', ['unloaded-a', 'unloaded-b'], {
      fetchFn: fetchFn as unknown as typeof fetch, onLoaded
    })).resolves.toBeUndefined()

    expect(onLoaded).toHaveBeenCalledTimes(1)
    expect(onLoaded).toHaveBeenCalledWith('unloaded-b', '<p>ok</p>', 4)
    consoleError.mockRestore()
  })

  it('skips onLoaded on non-ok responses and malformed payloads', async () => {
    const fetchFn = vi.fn(async (url: string | URL | Request) => {
      if (String(url).includes('unloaded-a')) return { ok: false } as Response
      return { ok: true, json: async () => ({ notContent: 1 }) } as Response
    })
    const onLoaded = vi.fn()
    await loadDocumentContents('book-1', ['unloaded-a', 'unloaded-b'], {
      fetchFn: fetchFn as unknown as typeof fetch, onLoaded
    })
    expect(onLoaded).not.toHaveBeenCalled()
  })

  it('allows a retry after a failed fetch (in-flight entry is cleared)', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    let calls = 0
    const fetchFn = vi.fn(async () => {
      calls++
      if (calls === 1) throw new Error('flaky')
      return okResponse('<p>second try</p>')
    })
    const onLoaded = vi.fn()

    await loadDocumentContents('book-1', ['unloaded-a'], { fetchFn: fetchFn as unknown as typeof fetch, onLoaded })
    await loadDocumentContents('book-1', ['unloaded-a'], { fetchFn: fetchFn as unknown as typeof fetch, onLoaded })

    expect(fetchFn).toHaveBeenCalledTimes(2)
    expect(onLoaded).toHaveBeenCalledWith('unloaded-a', '<p>second try</p>', 4)
    consoleError.mockRestore()
  })
})

// ── paragraph normalization on ingestion ──────────────────────────────────────
describe('loadDocumentContents paragraph normalization', () => {
  const fetchOk = (content: string) =>
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ content }) }) as unknown as typeof fetch

  it('splits <br> walls into paragraphs before the content reaches the store', async () => {
    const seen: string[] = []
    await loadDocumentContents('book-1', ['d1'], {
      fetchFn: fetchOk('<p>一<br>二<br>三</p>'),
      onLoaded: (_id, content) => seen.push(content)
    })
    expect(seen).toEqual(['<p>一</p><p>二</p><p>三</p>'])
  })

  it('passes already-clean content through untouched', async () => {
    const html = '<h1>T</h1><p>a</p><p>b</p>'
    const seen: string[] = []
    await loadDocumentContents('book-2', ['d2'], {
      fetchFn: fetchOk(html),
      onLoaded: (_id, content) => seen.push(content)
    })
    expect(seen).toEqual([html])
  })
})

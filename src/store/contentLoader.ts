/**
 * Lazy document-content loader for server-backed books.
 *
 * Server book loads only fetch chapter METADATA (contentLoaded: false); full
 * content arrives on demand. Before this module existed, the only fetch point
 * was opening a chapter — so pinning an unopened chapter, whole-book mode, and
 * the whole-book paths all silently skipped chapters the user never opened
 * (the context selector's `attachable()` drops empty docs). The store's
 * `ensureDocumentContents` action wraps these helpers to close that hole.
 */
import type { CanvasDocument } from '../types/document'
import { normalizeBrParagraphs } from '../utils/convert'

/** The subset of CanvasDocument the loader needs to inspect. */
export type LoadableDoc = Pick<CanvasDocument, 'id' | 'contentLoaded'>

/**
 * Ids (deduped, input order) whose docs exist but are metadata-only.
 * `contentLoaded === false` matches the selector's `attachable()` semantics:
 * `true`/`undefined` mean the content is already local.
 */
export function idsNeedingContent(ids: string[], documents: LoadableDoc[]): string[] {
  return [...new Set(ids)].filter(
    id => documents.find(d => d.id === id)?.contentLoaded === false
  )
}

// In-flight fetches keyed by bookId/docId — concurrent ensure calls (a pin's
// eager load racing a send) share one request instead of double-fetching.
const inFlight = new Map<string, Promise<void>>()

export interface ContentLoaderDeps {
  /** Injectable for tests; defaults to the global fetch. */
  fetchFn?: typeof fetch
  /** Apply one successfully loaded doc's content (and its server revision) to the store. */
  onLoaded: (id: string, content: string, revision?: number) => void
}

/**
 * Fetch content for the given doc ids. Resolves when every fetch settles and
 * never rejects: a failed doc is logged and stays unloaded, degrading to its
 * index line exactly as before (the caller's selection logic handles it).
 */
export async function loadDocumentContents(
  bookId: string,
  ids: string[],
  deps: ContentLoaderDeps
): Promise<void> {
  const fetchFn = deps.fetchFn ?? fetch
  await Promise.all(ids.map(id => {
    const key = `${bookId}/${id}`
    const existing = inFlight.get(key)
    if (existing) return existing
    const load = (async () => {
      try {
        const res = await fetchFn(`/api/books/${bookId}/documents/${id}`)
        if (!res.ok) return
        const data = await res.json()
        if (data && typeof data.content === 'string') {
          // Paragraph shape is normalized on the way IN, so a chapter stored
          // as a <br> wall (older imports, other clients) heals itself the
          // first time it loads. normalizeBrParagraphs early-returns on
          // content without <br>, so the common path costs one regex test.
          deps.onLoaded(id, normalizeBrParagraphs(data.content), typeof data.revision === 'number' ? data.revision : undefined)
        }
      } catch (e) {
        console.error('[ContentLoader] Failed to load document content', id, e)
      } finally {
        inFlight.delete(key)
      }
    })()
    inFlight.set(key, load)
    return load
  }))
}

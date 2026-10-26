import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useAppStore } from '../useAppStore'

// The focus-time check ("did another device write?") compares the book's
// server updated_at against the last stamp WE recorded. Document mutations
// bump the server stamp after the metadata PUT whose response used to be the
// only thing recorded — a guaranteed-stale baseline, so returning to the tab
// after any sync reloaded the whole book and threw the reader to the top of
// the chapter (user-reported). Every mutation response now carries its stamp
// and this action adopts it.
describe('adoptServerUpdatedAt', () => {
  beforeEach(() => {
    useAppStore.setState({ lastSeenServerUpdatedAt: null })
  })

  it('adopts a stamp onto an empty baseline', () => {
    useAppStore.getState().adoptServerUpdatedAt('2026-08-31T01:00:00Z')
    expect(useAppStore.getState().lastSeenServerUpdatedAt).toBe('2026-08-31T01:00:00Z')
  })

  it('keeps the max across out-of-order parallel responses', () => {
    // Doc PUTs run in parallel; responses resolve in any order. Adopting the
    // LAST one instead of the max would regress the baseline.
    const s = useAppStore.getState()
    s.adoptServerUpdatedAt('2026-08-31T01:00:05Z')
    s.adoptServerUpdatedAt('2026-08-31T01:00:02Z')
    expect(useAppStore.getState().lastSeenServerUpdatedAt).toBe('2026-08-31T01:00:05Z')
  })

  it('ignores a missing stamp (older server without the field)', () => {
    const s = useAppStore.getState()
    s.adoptServerUpdatedAt('2026-08-31T01:00:00Z')
    s.adoptServerUpdatedAt(undefined)
    s.adoptServerUpdatedAt(null)
    expect(useAppStore.getState().lastSeenServerUpdatedAt).toBe('2026-08-31T01:00:00Z')
  })
})

// The isolated max-semantics above were mutation-tested and correct — and the
// first shipped version still regressed, because syncToServer's FINAL set
// wrote the meta PUT's (older) stamp directly, clobbering what the doc PUTs
// had adopted moments earlier. Correct in isolation, zeroed in composition:
// this drives the real syncToServer end to end.
describe('syncToServer keeps the newest stamp across the whole flow', () => {
  const T1 = '2026-08-31T02:00:00Z' // meta PUT — written first, so older
  const T2 = '2026-08-31T02:00:01Z' // doc PUT — bumps the book stamp after

  beforeEach(() => {
    useAppStore.setState({
      user: { username: 'alice' },
      activeBookId: 'book-t',
      csrfToken: 'tok',
      lastSeenServerUpdatedAt: null,
      documents: [{
        // Fresh id per run so the module-level serverCopies record (documentSync)
        // never marks it unchanged.
        id: `doc-${Date.now()}`,
        title: 'Ch', content: '<p>x</p>', contentLoaded: true,
        createdAt: '2026-08-30T00:00:00Z', updatedAt: '2026-08-30T00:00:00Z'
      }],
    })
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url)
      const method = init?.method ?? 'GET'
      if (method === 'PUT' && /\/api\/books\/book-t$/.test(u)) {
        return new Response(JSON.stringify({ success: true, updatedAt: T1 }), { status: 200 })
      }
      if (method === 'PUT' && u.includes('/documents/')) {
        return new Response(JSON.stringify({ success: true, updatedAt: T2 }), { status: 200 })
      }
      return new Response(JSON.stringify({ success: true }), { status: 200 })
    }))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('ends on the doc PUT stamp, not the older meta stamp written last', async () => {
    await useAppStore.getState().syncToServer()

    expect(useAppStore.getState().serverSaveStatus).toBe('saved')
    // The regression: this read T1 — the final set clobbered the adoption.
    expect(useAppStore.getState().lastSeenServerUpdatedAt).toBe(T2)
  })
})

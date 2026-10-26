import { describe, it, expect, beforeEach, vi } from 'vitest'
import { getCookie, localStorage as ls, migrateDocumentsPayload, DOCUMENTS_ENVELOPE_VERSION, DOCUMENTS_INDEX_VERSION, clearRetiredSettings, diffDocumentsForWrite, loadDocumentsFromIndexedDB, db } from '../persistence'
import type { CanvasDocument } from '../../types/document'

// ── getCookie ─────────────────────────────────────────────────────────────────
describe('getCookie', () => {
  beforeEach(() => {
    document.cookie.split(';').forEach(cookie => {
      const name = cookie.split('=')[0].trim()
      if (name) document.cookie = `${name}=; max-age=0; path=/`
    })
  })

  it('returns null when the cookie does not exist', () => {
    expect(getCookie('nonexistent_cookie_xyz')).toBeNull()
  })

  it('returns the value when the cookie is set', () => {
    document.cookie = 'test_cookie=hello123; path=/'
    expect(getCookie('test_cookie')).toBe('hello123')
  })

  it('returns the correct value among multiple cookies', () => {
    document.cookie = 'cookie_a=valueA; path=/'
    document.cookie = 'cookie_b=valueB; path=/'
    expect(getCookie('cookie_a')).toBe('valueA')
    expect(getCookie('cookie_b')).toBe('valueB')
  })

  it('handles URL-encoded cookie values', () => {
    const encoded = encodeURIComponent('hello world / special&chars')
    document.cookie = `encoded_cookie=${encoded}; path=/`
    expect(getCookie('encoded_cookie')).toBe('hello world / special&chars')
  })
})

// ── safeLocalStorage wrapper ──────────────────────────────────────────────────
// The ls wrapper captures window.localStorage at module import time.
// test-setup.ts installs a full in-memory polyfill so all Storage methods work.
describe('localStorage wrapper', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('does not throw on QuotaExceededError — logs a warning instead', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    // Temporarily patch setItem on the polyfill to throw QuotaExceededError
    const originalSetItem = window.localStorage.setItem
    window.localStorage.setItem = () => {
      const err = new DOMException('QuotaExceededError')
      Object.defineProperty(err, 'name', { value: 'QuotaExceededError' })
      Object.defineProperty(err, 'code', { value: 22 })
      throw err
    }

    expect(() => ls.setItem('key', 'value')).not.toThrow()
    expect(warnSpy).toHaveBeenCalled()

    window.localStorage.setItem = originalSetItem
    warnSpy.mockRestore()
  })
})

// ── migrateDocumentsPayload (versioned documents envelope) ────────────────────
describe('migrateDocumentsPayload', () => {
  const legacyDoc: CanvasDocument = {
    id: 'doc-1',
    title: 'Chapter 1',
    content: '<p>Hello</p>',
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T00:00:00Z'
  }

  it('migrates a legacy v0 bare array, preserving all fields', () => {
    const result = migrateDocumentsPayload([legacyDoc])
    expect(result).toEqual([legacyDoc])
    // New optional summary fields default to absent — that is a valid v1 doc.
    expect(result![0].summary).toBeUndefined()
    expect(result![0].summaryContentHash).toBeUndefined()
  })

  it('reads a current-version envelope', () => {
    const doc: CanvasDocument = { ...legacyDoc, summary: 'A summary', summaryContentHash: 'abc123' }
    const result = migrateDocumentsPayload({ version: DOCUMENTS_ENVELOPE_VERSION, data: [doc] })
    expect(result).toEqual([doc])
    expect(result![0].summary).toBe('A summary')
  })

  it('returns null for null/undefined (first run, nothing stored)', () => {
    expect(migrateDocumentsPayload(null)).toBeNull()
    expect(migrateDocumentsPayload(undefined)).toBeNull()
  })

  it('refuses an unknown future envelope version', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(migrateDocumentsPayload({ version: DOCUMENTS_ENVELOPE_VERSION + 1, data: [legacyDoc] })).toBeNull()
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
  })

  it('refuses corrupt payload shapes', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(migrateDocumentsPayload('garbage')).toBeNull()
    expect(migrateDocumentsPayload({ version: 1 })).toBeNull()
    expect(migrateDocumentsPayload(42)).toBeNull()
    warnSpy.mockRestore()
  })

  it('accepts an empty legacy array (user deleted everything)', () => {
    expect(migrateDocumentsPayload([])).toEqual([])
  })

  // v4 (agentic_chat_loop.md D7): manual reference selection is retired, so
  // every legacy shape loses its reference fields — and nothing else.
  it('drops v0 selectedReferenceIds', () => {
    const result = migrateDocumentsPayload([{ ...legacyDoc, selectedReferenceIds: ['doc-2', 'doc-3'] }])
    expect(result).toEqual([legacyDoc])
  })

  it('drops a v1 selection and keeps the v1 fields', () => {
    const v1Doc = { ...legacyDoc, summary: 'S', summaryContentHash: 'h', selectedReferenceIds: ['doc-9'] }
    const result = migrateDocumentsPayload({ version: 1, data: [v1Doc] })
    expect(result).toEqual([{ ...legacyDoc, summary: 'S', summaryContentHash: 'h' }])
  })

  it('drops v2 pins and blocks', () => {
    const v2Doc = { ...legacyDoc, pinnedReferenceIds: ['a'], blockedReferenceIds: ['b'] }
    const result = migrateDocumentsPayload({ version: DOCUMENTS_ENVELOPE_VERSION, data: [v2Doc] })
    expect(result).toEqual([legacyDoc])
  })
})

// ── v3 → v4 per-document records (D7) ─────────────────────────────────────────
describe('loadDocumentsFromIndexedDB — v3 → v4', () => {
  it('rewrites v3 records without the retired reference fields, under a v4 index', async () => {
    const store = new Map<string, unknown>([
      ['web_canvas_documents', { version: 3, ids: ['d1', 'd2'] }],
      ['web_canvas_doc:d1', { id: 'd1', title: 'A', content: '<p>a</p>', createdAt: 't', updatedAt: 't', pinnedReferenceIds: ['d2'], blockedReferenceIds: [] }],
      ['web_canvas_doc:d2', { id: 'd2', title: 'B', content: '<p>b</p>', createdAt: 't', updatedAt: 't', summary: 'S' }]
    ])
    const get = vi.spyOn(db, 'get').mockImplementation(async <T,>(key: string) => (store.get(key) ?? null) as T | null)
    const set = vi.spyOn(db, 'set').mockImplementation(async (key: string, value: unknown) => { store.set(key, value) })

    const docs = await loadDocumentsFromIndexedDB()

    expect(docs).toEqual([
      { id: 'd1', title: 'A', content: '<p>a</p>', createdAt: 't', updatedAt: 't' },
      { id: 'd2', title: 'B', content: '<p>b</p>', createdAt: 't', updatedAt: 't', summary: 'S' }
    ])
    expect(store.get('web_canvas_doc:d1')).not.toHaveProperty('pinnedReferenceIds')
    expect(store.get('web_canvas_documents')).toEqual({ version: DOCUMENTS_INDEX_VERSION, ids: ['d1', 'd2'] })
    get.mockRestore(); set.mockRestore()
  })
})

// ── retired settings ──────────────────────────────────────────────────────────
describe('clearRetiredSettings', () => {
  it('forgets the retired whole-book mode', () => {
    ls.setItem('web_canvas_whole_book_mode', 'sticky')
    clearRetiredSettings()
    expect(ls.getItem('web_canvas_whole_book_mode')).toBeNull()
  })
})

// ── diffDocumentsForWrite (v3 per-document persistence) ───────────────────────
describe('diffDocumentsForWrite', () => {
  const doc = (id: string, content = 'x'): CanvasDocument => ({
    id, title: id, content, createdAt: 't', updatedAt: 't'
  })

  it('first write (no snapshot) writes everything and the index', () => {
    const d = diffDocumentsForWrite(null, [doc('a'), doc('b')])
    expect(d.changed.map(x => x.id)).toEqual(['a', 'b'])
    expect(d.removedIds).toEqual([])
    expect(d.indexChanged).toBe(true)
  })

  it('writes only reference-changed documents on subsequent saves', () => {
    const a = doc('a'), b = doc('b')
    const prev = new Map([['a', a], ['b', b]])
    const b2 = { ...b, content: 'edited' }
    const d = diffDocumentsForWrite(prev, [a, b2])
    expect(d.changed.map(x => x.id)).toEqual(['b'])
    expect(d.removedIds).toEqual([])
    expect(d.indexChanged).toBe(false)
  })

  it('nothing changed → nothing written', () => {
    const a = doc('a')
    const d = diffDocumentsForWrite(new Map([['a', a]]), [a])
    expect(d.changed).toEqual([])
    expect(d.indexChanged).toBe(false)
  })

  it('detects removals and index membership changes', () => {
    const a = doc('a'), b = doc('b')
    const d = diffDocumentsForWrite(new Map([['a', a], ['b', b]]), [a])
    expect(d.removedIds).toEqual(['b'])
    expect(d.indexChanged).toBe(true)
  })

  it('detects pure reorders as index changes without doc writes', () => {
    const a = doc('a'), b = doc('b')
    const d = diffDocumentsForWrite(new Map([['a', a], ['b', b]]), [b, a])
    expect(d.changed).toEqual([])
    expect(d.indexChanged).toBe(true)
  })

  it('additions write the new doc and the index', () => {
    const a = doc('a'), c = doc('c')
    const d = diffDocumentsForWrite(new Map([['a', a]]), [a, c])
    expect(d.changed.map(x => x.id)).toEqual(['c'])
    expect(d.indexChanged).toBe(true)
  })
})

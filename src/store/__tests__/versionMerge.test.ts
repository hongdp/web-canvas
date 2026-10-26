import { describe, it, expect } from 'vitest'
import { mergeVersions, versionsMissingOnServer } from '../versionMerge'
import type { DocumentVersion } from '../../types/document'
import type { ServerVersionMeta } from '../types'

/**
 * The bug these exist for: version history was written only to IndexedDB (the
 * server's POST endpoint had no caller), and the sync then did
 * `versions = serverVersions` with an always-empty server list — so each reload
 * erased the local history as well. When a chapter was later emptied by
 * another bug, there was nothing at all to roll back to.
 *
 * The invariant: an empty server list is the absence of evidence, never
 * evidence of absence.
 */

const local = (id: string, ts: string, content = '<p>text</p>', bookId?: string): DocumentVersion =>
  ({ id, documentId: 'doc-1', bookId, timestamp: ts, title: `snap ${id}`, content })

const remote = (id: string, ts: string): ServerVersionMeta =>
  ({ id, documentId: 'doc-1', title: `snap ${id}`, timestamp: ts })

describe('mergeVersions', () => {
  it('keeps local snapshots when the server has none', () => {
    const merged = mergeVersions([local('a', '2026-08-16T01:00:00Z')], [], 'book-1')
    expect(merged).toHaveLength(1)
    expect(merged[0].content).toBe('<p>text</p>')
  })

  it('adds server snapshots this browser has never seen', () => {
    const merged = mergeVersions([], [remote('s1', '2026-08-16T02:00:00Z')], 'book-1')
    expect(merged).toEqual([
      { id: 's1', documentId: 'doc-1', bookId: 'book-1', title: 'snap s1', timestamp: '2026-08-16T02:00:00Z', content: '' }
    ])
  })

  it('unions both sides without losing either', () => {
    const merged = mergeVersions(
      [local('a', '2026-08-16T01:00:00Z')],
      [remote('s1', '2026-08-16T02:00:00Z')],
      'book-1'
    )
    expect(merged.map(v => v.id)).toEqual(['s1', 'a'])   // newest first
  })

  it('prefers the local content for a snapshot both sides know', () => {
    // The server row carries metadata only; dropping the content we already
    // have would turn a restorable snapshot into a round trip — or worse, into
    // an empty restore.
    const merged = mergeVersions(
      [local('a', '2026-08-16T01:00:00Z', '<p>the real text</p>')],
      [remote('a', '2026-08-16T01:00:00Z')],
      'book-1'
    )
    expect(merged).toHaveLength(1)
    expect(merged[0].content).toBe('<p>the real text</p>')
  })

  it('stamps the book onto snapshots that predate the field', () => {
    expect(mergeVersions([local('a', '2026-08-16T01:00:00Z')], [], 'book-9')[0].bookId).toBe('book-9')
  })

  it('sorts newest first regardless of input order', () => {
    const merged = mergeVersions(
      [local('old', '2026-01-01T00:00:00Z'), local('new', '2026-08-16T00:00:00Z')],
      [remote('mid', '2026-05-01T00:00:00Z')],
      'book-1'
    )
    expect(merged.map(v => v.id)).toEqual(['new', 'mid', 'old'])
  })
})

describe('versionsMissingOnServer', () => {
  it('finds the backlog to upload', () => {
    const backlog = versionsMissingOnServer(
      [local('a', '2026-08-16T01:00:00Z'), local('b', '2026-08-16T02:00:00Z')],
      [remote('a', '2026-08-16T01:00:00Z')],
      'book-1'
    )
    expect(backlog.map(v => v.id)).toEqual(['b'])
  })

  it('never uploads a metadata-only stub', () => {
    // An empty snapshot on the server looks restorable and is not — strictly
    // worse than having no snapshot.
    const backlog = versionsMissingOnServer([local('a', '2026-08-16T01:00:00Z', '')], [], 'book-1')
    expect(backlog).toEqual([])
  })

  it('leaves another book\'s snapshots alone', () => {
    const backlog = versionsMissingOnServer(
      [local('a', '2026-08-16T01:00:00Z', '<p>x</p>', 'book-other')],
      [],
      'book-1'
    )
    expect(backlog).toEqual([])
  })

  it('claims snapshots that predate the bookId field', () => {
    // They belong to whichever book was open then, and the active book is the
    // only reasonable guess — better uploaded than lost.
    const backlog = versionsMissingOnServer([local('a', '2026-08-16T01:00:00Z')], [], 'book-1')
    expect(backlog.map(v => v.id)).toEqual(['a'])
  })
})

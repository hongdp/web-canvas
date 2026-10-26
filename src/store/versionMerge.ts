import type { DocumentVersion } from '../types/document'
import type { ServerVersionMeta } from './types'

/**
 * Reconciling version snapshots between the browser and the server.
 *
 * The sync used to do `versions = serverVersions` — and since nothing ever
 * POSTed a snapshot, the server's list was always empty, so every sync wiped
 * the local history. An empty server list is not evidence that no snapshots
 * exist; it is the absence of evidence.
 */

/**
 * Union by id, newest first. Server rows contribute metadata (their content is
 * fetched on demand); local rows keep whatever content they already hold, so a
 * snapshot taken this session stays restorable without a round trip.
 */
export function mergeVersions(
  local: DocumentVersion[],
  server: ServerVersionMeta[],
  bookId: string
): DocumentVersion[] {
  const byId = new Map<string, DocumentVersion>()

  for (const v of server) {
    byId.set(v.id, {
      id: v.id,
      documentId: v.documentId,
      bookId,
      title: v.title,
      timestamp: v.timestamp,
      content: '',           // lazy: fetched when restored
    })
  }

  for (const v of local) {
    const existing = byId.get(v.id)
    if (existing) {
      // Keep the local content if we have it; the server row adds nothing else.
      if (v.content) byId.set(v.id, { ...existing, content: v.content })
      continue
    }
    byId.set(v.id, { ...v, bookId: v.bookId ?? bookId })
  }

  return [...byId.values()].sort((a, b) => b.timestamp.localeCompare(a.timestamp))
}

/**
 * Snapshots this browser holds that the server has never seen — the backlog to
 * push after a sync. Only ones with content are sendable: a metadata-only stub
 * would upload an empty snapshot, which is worse than none at all (it looks
 * restorable and is not).
 */
export function versionsMissingOnServer(
  local: DocumentVersion[],
  server: ServerVersionMeta[],
  bookId: string
): DocumentVersion[] {
  const known = new Set(server.map(v => v.id))
  return local.filter(v =>
    !known.has(v.id) &&
    !!v.content &&
    (v.bookId ?? bookId) === bookId
  )
}

/**
 * Upload a backlog of snapshots the server has never seen, oldest first so the
 * server's ordering matches the browser's. Sequential on purpose: this runs on
 * every sync and a browser holding fifty snapshots should not open fifty
 * connections at once.
 */
export async function backfillVersions(versions: DocumentVersion[], bookId: string): Promise<void> {
  const { useAppStore } = await import('./useAppStore')
  const state = useAppStore.getState()
  if (!state.user || !bookId) return

  const ordered = [...versions].sort((a, b) => a.timestamp.localeCompare(b.timestamp))
  let sent = 0
  for (const v of ordered) {
    try {
      const res = await fetch(`/api/books/${bookId}/versions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': useAppStore.getState().csrfToken || ''
        },
        body: JSON.stringify({
          id: v.id,
          documentId: v.documentId,
          title: v.title,
          timestamp: v.timestamp,
          content: v.content
        })
      })
      if (res.ok) sent += 1
    } catch (e) {
      // A failed backfill is retried on the next sync — the local copy stays.
      console.error('Failed to backfill a version snapshot', e)
      break
    }
  }
  if (sent > 0) console.info(`[versions] Backfilled ${sent} snapshot(s) to the server.`)
}

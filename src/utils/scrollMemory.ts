/**
 * Per-chapter scroll memory.
 *
 * Problem: returning to the app threw the reader back to the top of the
 *   chapter. Two independent causes — mobile Firefox discards a backgrounded
 *   tab and reloads it on return, and the app itself used to reload the book
 *   on focus. The second is fixed at the source; the first is the browser's
 *   call, so the position has to survive a full reload.
 * Fix: remember where each chapter was scrolled to and restore it when the
 *   chapter mounts. Stored as ONE capped map so the keyspace cannot grow
 *   without bound.
 */
const STORAGE_KEY = 'web_canvas_scroll_positions'
/** Keep the most recently touched chapters only. */
const MAX_ENTRIES = 60
/** Below this, restoring is pointless — the chapter is effectively at the top. */
const MIN_MEANINGFUL_OFFSET = 40

interface ScrollEntry {
  top: number
  /** Monotonic counter, not a clock: used only for eviction ordering. */
  seq: number
}

type ScrollMap = Record<string, ScrollEntry>

let seqCounter = 0

function read(): ScrollMap {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? (parsed as ScrollMap) : {}
  } catch {
    return {}
  }
}

function write(map: ScrollMap): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
  } catch {
    // Quota or private mode — scroll memory is a convenience, never a failure.
  }
}

/** Evict the least recently written entries, keeping the map bounded. */
export function capEntries(map: ScrollMap, max: number = MAX_ENTRIES): ScrollMap {
  const ids = Object.keys(map)
  if (ids.length <= max) return map
  const keep = ids
    .sort((a, b) => (map[b]?.seq ?? 0) - (map[a]?.seq ?? 0))
    .slice(0, max)
  const next: ScrollMap = {}
  for (const id of keep) next[id] = map[id]
  return next
}

export function saveScrollPosition(documentId: string, top: number): void {
  if (!documentId) return
  const map = read()
  if (top < MIN_MEANINGFUL_OFFSET) {
    // Back at the top: forget the entry rather than pin the reader there.
    if (map[documentId]) {
      delete map[documentId]
      write(map)
    }
    return
  }
  map[documentId] = { top: Math.round(top), seq: ++seqCounter }
  write(capEntries(map))
}

/** Saved offset for a chapter, or 0 when there is nothing worth restoring. */
export function loadScrollPosition(documentId: string): number {
  if (!documentId) return 0
  const entry = read()[documentId]
  return entry && typeof entry.top === 'number' ? entry.top : 0
}

/**
 * Module-level mutable sync-runtime flags, kept in exactly ONE module so
 * every user sees the same value. They are shared by the books/auth slices
 * (bulk state swaps must pause auto-save), the server bootstrap
 * (serverSync.ts), and the auto-save subscription (useAppStore.ts).
 * ESM imports are read-only views, so a plain `export let` could not be
 * reassigned by importers — hence the accessor functions.
 */

// Guards the auto-save subscription: while false, store updates only refresh
// the change-tracking baseline instead of scheduling a server sync. Toggled
// off during bulk state swaps (book create/switch) and set once the local
// bootstrap in initializeStoreFromServer completes.
let isInitialized = false

export const getIsInitialized = (): boolean => isInitialized

export const setIsInitialized = (value: boolean) => {
  isInitialized = value
}

// Debounce handle for the auto-save subscription's syncToServer timer.
let saveTimeout: ReturnType<typeof setTimeout> | null = null

/**
 * How long the FIRST pending save may be starved before it runs regardless.
 * A pure debounce resets on every call, and a stream calls it on every chunk.
 */
const MAX_SAVE_STARVATION_MS = 10_000
let firstScheduledAt: number | null = null

/** Cancel a pending debounced save, if any (book switch/create, logout). */
export const clearPendingSave = () => {
  if (saveTimeout) {
    clearTimeout(saveTimeout)
    saveTimeout = null
  }
  // Also forget how long the pending save had been waiting: a cancelled save
  // must not leave the next one already counted as starved.
  firstScheduledAt = null
}

/** (Re)arm the debounced save timer, replacing any pending one. */
/**
 * When the FIRST scheduled save must run no matter how much keeps arriving.
 *
 * Problem: a pure debounce resets on every call, and a stream calls this on
 *   every chunk — tens of times a second. The timer never expired, so nothing
 *   was saved for the whole generation, and a refresh mid-stream found no
 *   reply on the server at all.
 * Fix: keep the debounce (it is what stops a save per keystroke) but cap how
 *   long it can be starved. Past the cap the pending save runs and the
 *   debounce starts over.
 */
export const schedulePendingSave = (callback: () => void, delayMs: number) => {
  const now = Date.now()
  if (firstScheduledAt === null) firstScheduledAt = now

  const starvedFor = now - firstScheduledAt
  if (starvedFor >= MAX_SAVE_STARVATION_MS) {
    if (saveTimeout) clearTimeout(saveTimeout)
    saveTimeout = null
    firstScheduledAt = null
    callback()
    return
  }

  if (saveTimeout) clearTimeout(saveTimeout)
  saveTimeout = setTimeout(() => {
    saveTimeout = null
    firstScheduledAt = null
    callback()
  }, Math.min(delayMs, MAX_SAVE_STARVATION_MS - starvedFor))
}

/**
 * `run.*` events from the book's event stream (store/bookEvents), handed to
 * whoever renders runs — the chat hook. A tiny emitter rather than store
 * state: deltas arrive per token, and the hook paints them with its own
 * throttles instead of re-rendering the app for each.
 */
import type { ServerRunEvent } from '../services/serverRuns'

type Listener = (event: ServerRunEvent) => void
const listeners = new Set<Listener>()

export function onRunEvent(listener: Listener): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function emitRunEvent(event: ServerRunEvent): void {
  for (const listener of [...listeners]) {
    try {
      listener(event)
    } catch (e) {
      console.error('[runEvents] listener failed', e)
    }
  }
}

/**
 * "Events may have been missed": the book's stream came back after a drop,
 * or the page came back to the foreground (a phone suspends the stream in
 * the background, often without an error). Whoever renders runs re-reads
 * their state then — a run that finished meanwhile would otherwise keep its
 * bubble "working" with the timer counting (a run that finished while the
 * phone slept still showed "thinking").
 */
type CatchUpListener = () => void
const catchUpListeners = new Set<CatchUpListener>()

export function onRunCatchUp(listener: CatchUpListener): () => void {
  catchUpListeners.add(listener)
  return () => { catchUpListeners.delete(listener) }
}

export function emitRunCatchUp(): void {
  for (const listener of [...catchUpListeners]) {
    try {
      listener()
    } catch (e) {
      console.error('[runEvents] catch-up listener failed', e)
    }
  }
}

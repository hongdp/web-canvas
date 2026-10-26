import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { schedulePendingSave, clearPendingSave } from '../syncRuntime'

// A pure debounce resets on every call, and a stream calls this on every
// chunk — tens of times a second. The timer never expired, so nothing was
// saved for the whole generation and a refresh mid-stream found no reply on
// the server. The debounce must survive; the starvation must not.
beforeEach(() => {
  vi.useFakeTimers()
  clearPendingSave?.()
})
afterEach(() => vi.useRealTimers())

describe('schedulePendingSave', () => {
  it('still debounces a burst of edits into one save', () => {
    const save = vi.fn()
    for (let i = 0; i < 5; i++) {
      schedulePendingSave(save, 3000)
      vi.advanceTimersByTime(100)
    }
    expect(save).not.toHaveBeenCalled()
    vi.advanceTimersByTime(3000)
    expect(save).toHaveBeenCalledTimes(1)
  })

  it('saves anyway when a stream keeps resetting the timer', () => {
    const save = vi.fn()
    // 30 seconds of chunks arriving every 50ms: a pure debounce saves NEVER.
    for (let i = 0; i < 600; i++) {
      schedulePendingSave(save, 3000)
      vi.advanceTimersByTime(50)
    }
    expect(save.mock.calls.length).toBeGreaterThan(0)
  })

  it('forgets accumulated starvation when the save is cancelled', () => {
    // A book switch cancels the pending save; the next edit must get its full
    // debounce, not be treated as already overdue.
    const save = vi.fn()
    for (let i = 0; i < 100; i++) {
      schedulePendingSave(save, 3000)
      vi.advanceTimersByTime(50)
    }
    clearPendingSave()
    save.mockClear()

    schedulePendingSave(save, 3000)
    expect(save).not.toHaveBeenCalled()
    vi.advanceTimersByTime(3000)
    expect(save).toHaveBeenCalledTimes(1)
  })

  it('resumes debouncing after a starvation save', () => {
    const save = vi.fn()
    for (let i = 0; i < 400; i++) {
      schedulePendingSave(save, 3000)
      vi.advanceTimersByTime(50)
    }
    const duringStream = save.mock.calls.length
    expect(duringStream).toBeGreaterThan(0)

    // Stream ends; one final edit must still be saved on the normal delay.
    schedulePendingSave(save, 3000)
    vi.advanceTimersByTime(3000)
    expect(save.mock.calls.length).toBe(duringStream + 1)
  })
})

import { describe, it, expect, beforeEach } from 'vitest'
import { clampSize, loadPersistedSize, savePersistedSize, CHAT_WIDTH, SIDEBAR_WIDTH } from '../layoutPrefs'

beforeEach(() => window.localStorage.clear())

describe('layoutPrefs', () => {
  it('round-trips a size', () => {
    savePersistedSize('k', 333, { min: 200, max: 400 })
    expect(loadPersistedSize('k', 240, { min: 200, max: 400 })).toBe(333)
  })

  it('clamps on load, not only on save', () => {
    // A stored value from a bigger monitor (or corruption) must not produce a
    // panel wider than the current bounds allow.
    window.localStorage.setItem('k', '9999')
    expect(loadPersistedSize('k', 240, { min: 200, max: 400 })).toBe(400)
    window.localStorage.setItem('k', '-5')
    expect(loadPersistedSize('k', 240, { min: 200, max: 400 })).toBe(200)
    window.localStorage.setItem('k', 'garbage')
    expect(loadPersistedSize('k', 240, { min: 200, max: 400 })).toBe(200)
  })

  it('falls back when nothing is stored', () => {
    expect(loadPersistedSize('missing', 380, { min: 280, max: 600 })).toBe(380)
  })

  it('keeps the shipped bounds sane', () => {
    for (const { fallback, bounds } of [CHAT_WIDTH, SIDEBAR_WIDTH]) {
      expect(clampSize(fallback, bounds)).toBe(fallback) // default is within bounds
      expect(bounds.min).toBeLessThan(bounds.max)
    }
  })
})

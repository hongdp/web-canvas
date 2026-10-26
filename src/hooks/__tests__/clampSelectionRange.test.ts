import { describe, it, expect } from 'vitest'
import { clampSelectionRange } from '../chat/streamHandlers'

// A selection replacement dispatches positions captured BEFORE the stream. If
// the document moved on, ProseMirror throws "Position N out of range" — which
// reached the user as "⚠️ Error during stream" and threw the whole generation
// away. Reported live with "Position 10 out of range": a nearly empty document
// with a range from a much longer one.
describe('clampSelectionRange', () => {
  it('passes a range that still fits', () => {
    expect(clampSelectionRange(4, 12, 100)).toEqual({ from: 4, to: 12 })
  })

  it('trims an end that now runs past the document', () => {
    expect(clampSelectionRange(4, 999, 40)).toEqual({ from: 4, to: 40 })
  })

  it('gives up when the start itself is gone', () => {
    // Writing at a guessed position is worse than not writing.
    expect(clampSelectionRange(120, 200, 10)).toBeNull()
    expect(clampSelectionRange(11, 11, 10)).toBeNull()
  })

  it('accepts a range that ends exactly at the document end', () => {
    expect(clampSelectionRange(0, 10, 10)).toEqual({ from: 0, to: 10 })
  })

  it('never returns an inverted range', () => {
    const r = clampSelectionRange(8, 3, 100)
    expect(r).toEqual({ from: 8, to: 8 })
  })

  it('rejects nonsense rather than passing it to ProseMirror', () => {
    expect(clampSelectionRange(NaN, 5, 100)).toBeNull()
    expect(clampSelectionRange(2, NaN, 100)).toBeNull()
    expect(clampSelectionRange(-1, 5, 100)).toBeNull()
    expect(clampSelectionRange(2, 5, -1)).toBeNull()
  })
})

import { describe, it, expect, beforeEach } from 'vitest'
import {
  saveScrollPosition,
  loadScrollPosition,
  capEntries
} from '../scrollMemory'

beforeEach(() => {
  window.localStorage.clear()
})

describe('scrollMemory', () => {
  it('round-trips a chapter offset', () => {
    saveScrollPosition('doc-1', 1234)
    expect(loadScrollPosition('doc-1')).toBe(1234)
  })

  it('keeps chapters independent', () => {
    saveScrollPosition('doc-1', 500)
    saveScrollPosition('doc-2', 900)
    expect(loadScrollPosition('doc-1')).toBe(500)
    expect(loadScrollPosition('doc-2')).toBe(900)
  })

  it('returns 0 for a chapter that was never scrolled', () => {
    expect(loadScrollPosition('unknown')).toBe(0)
  })

  it('forgets the entry once the reader is back near the top', () => {
    saveScrollPosition('doc-1', 800)
    saveScrollPosition('doc-1', 5)
    expect(loadScrollPosition('doc-1')).toBe(0)
  })

  it('survives corrupt storage instead of throwing', () => {
    window.localStorage.setItem('web_canvas_scroll_positions', '{not json')
    expect(loadScrollPosition('doc-1')).toBe(0)
    expect(() => saveScrollPosition('doc-1', 400)).not.toThrow()
    expect(loadScrollPosition('doc-1')).toBe(400)
  })

  it('caps the map, evicting the least recently written chapters', () => {
    const map = {
      a: { top: 1, seq: 1 },
      b: { top: 2, seq: 2 },
      c: { top: 3, seq: 3 }
    }
    const capped = capEntries(map, 2)
    expect(Object.keys(capped).sort()).toEqual(['b', 'c'])
  })

  it('keeps the stored map bounded across many chapters', () => {
    for (let i = 0; i < 80; i++) saveScrollPosition(`doc-${i}`, 100 + i)
    const raw = window.localStorage.getItem('web_canvas_scroll_positions') || '{}'
    expect(Object.keys(JSON.parse(raw)).length).toBeLessThanOrEqual(60)
    // The most recent chapter is always retained.
    expect(loadScrollPosition('doc-79')).toBe(179)
  })

  it('ignores an empty document id', () => {
    expect(() => saveScrollPosition('', 500)).not.toThrow()
    expect(loadScrollPosition('')).toBe(0)
  })
})

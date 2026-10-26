import { describe, it, expect } from 'vitest'
import { sliceChars, headChars, tailChars } from '../unicode'

describe('sliceChars', () => {
  // "a😀b": the emoji is two code units (1 and 2).
  const s = 'a😀b'
  it('never cuts a surrogate pair in half', () => {
    expect(headChars(s, 2)).toBe('a')
    expect(headChars(s, 3)).toBe('a😀')
    expect(sliceChars(s, 2)).toBe('b')
    expect(tailChars(s, 2)).toBe('b')
    expect(tailChars(s, 3)).toBe('😀b')
    for (const out of [headChars(s, 2), sliceChars(s, 2), tailChars(s, 2)]) expect(out).not.toMatch(/[\ud800-\udfff]/)
  })
  it('is plain slice on whole characters', () => {
    expect(sliceChars('hello', 1, 3)).toBe('el')
    expect(headChars('hello', 10)).toBe('hello')
    expect(sliceChars('hello', 4, 2)).toBe('')
  })
})

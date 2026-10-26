/**
 * Cutting JavaScript strings without breaking characters.
 *
 * A JS string is UTF-16: an emoji is two code units, and `slice` at a count
 * can land between them. The half left behind is a lone surrogate, which
 * survives JSON (as "\ud83d") into the API server and fails there when the
 * request is encoded as UTF-8 — "'utf-8' codec can't encode character
 * '\ud83d' … surrogates not allowed", a whole step lost (2026-10-11).
 */

const isHigh = (code: number) => code >= 0xd800 && code <= 0xdbff
const isLow = (code: number) => code >= 0xdc00 && code <= 0xdfff

/** `s.slice(start, end)` moved off the middle of a surrogate pair: the start forward, the end back. */
export function sliceChars(s: string, start: number, end?: number): string {
  let from = Math.max(0, start)
  let to = Math.min(end ?? s.length, s.length)
  if (from > 0 && from < s.length && isLow(s.charCodeAt(from)) && isHigh(s.charCodeAt(from - 1))) from++
  if (to > 0 && to < s.length && isLow(s.charCodeAt(to)) && isHigh(s.charCodeAt(to - 1))) to--
  return from < to ? s.slice(from, to) : ''
}

/** The first `max` code units of `s`, never ending on half a character. */
export const headChars = (s: string, max: number): string => sliceChars(s, 0, max)

/** The last `max` code units of `s`, never starting on half a character. */
export const tailChars = (s: string, max: number): string => sliceChars(s, Math.max(0, s.length - max))

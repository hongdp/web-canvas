/**
 * Persisted, clamped panel sizes.
 *
 * Problem: the chat panel was drag-resizable but reset to its default on every
 *   reload (plain useState), and the chapters sidebar was not adjustable at
 *   all — while chapter summaries, which people actually read, were squeezed
 *   into it at 0.72rem.
 * Fix: one tiny helper both panels (and the overview drawer) share. Clamping
 *   on LOAD matters as much as on save: a stored size from a larger monitor,
 *   or a corrupted value, must not produce a panel wider than the window.
 */

export interface SizeBounds {
  min: number
  max: number
}

export function clampSize(value: number, bounds: SizeBounds): number {
  if (!Number.isFinite(value)) return bounds.min
  return Math.max(bounds.min, Math.min(bounds.max, Math.round(value)))
}

export function loadPersistedSize(key: string, fallback: number, bounds: SizeBounds): number {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return clampSize(fallback, bounds)
    return clampSize(parseFloat(raw), bounds)
  } catch {
    return clampSize(fallback, bounds)
  }
}

export function savePersistedSize(key: string, value: number, bounds: SizeBounds): void {
  try {
    localStorage.setItem(key, String(clampSize(value, bounds)))
  } catch {
    // Quota/private mode: sizing is a convenience, never a failure.
  }
}

export const CHAT_WIDTH = { key: 'web_canvas_chat_width', fallback: 380, bounds: { min: 280, max: 600 } }
export const SIDEBAR_WIDTH = { key: 'web_canvas_chapters_width', fallback: 240, bounds: { min: 200, max: 420 } }

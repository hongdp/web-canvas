/**
 * Vitest global setup: polyfill localStorage for jsdom environments
 * that don't implement the full Storage API (e.g., when --localstorage-file is absent).
 */

// Build a simple in-memory localStorage polyfill
const store: Record<string, string> = {}

const localStoragePolyfill: Storage = {
  getItem: (key: string) => store[key] ?? null,
  setItem: (key: string, value: string) => { store[key] = String(value) },
  removeItem: (key: string) => { delete store[key] },
  clear: () => { Object.keys(store).forEach(k => delete store[k]) },
  key: (index: number) => Object.keys(store)[index] ?? null,
  get length() { return Object.keys(store).length }
}

Object.defineProperty(window, 'localStorage', {
  value: localStoragePolyfill,
  writable: true,
  configurable: true
})

import { describe, it, expect, beforeEach } from 'vitest'
import { migrateProvidersConfig, loadSavedProvider } from '../settingsPersistence'
import { DEFAULT_CONFIGS } from '../defaults'

// Adding a provider changes a PERSISTED shape: everyone already has a stored
// envelope that predates `runpod`. Nothing bumps the version for it, because
// the migration's closing step already merges DEFAULT_CONFIGS over whatever
// was stored — but "already handled" is a claim, and this is the test that
// makes it one. Without it, a later refactor of that merge would silently
// leave upgraders with no RunPod config and an unusable tab.
describe('provider config migration: adding the runpod provider', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('gives a stored v2 envelope the runpod defaults it never had', () => {
    const stored = JSON.stringify({
      version: 2,
      data: {
        gemini: { apiKey: 'g-key', model: 'gemini-2.5-pro', baseUrl: 'https://g/v1beta', maxOutputTokens: 16384 },
        ollama: { apiKey: 'ollama-no-key', model: 'qwen3.8-27b', baseUrl: 'http://127.0.0.1:8090/v1', maxOutputTokens: 16384 },
      },
    })

    const merged = migrateProvidersConfig(stored)

    expect(merged.runpod).toBeDefined()
    expect(merged.runpod.baseUrl).toBe(DEFAULT_CONFIGS.runpod.baseUrl)
    expect(merged.runpod.apiKey).toBe(DEFAULT_CONFIGS.runpod.apiKey)
  })

  it('does not disturb the local ollama endpoint while doing so', () => {
    // The entire point of a separate tab: the local model stays configured.
    const stored = JSON.stringify({
      version: 2,
      data: {
        ollama: { apiKey: 'ollama-no-key', model: 'qwen3.8-27b', baseUrl: 'http://127.0.0.1:8090/v1', maxOutputTokens: 16384 },
      },
    })

    const merged = migrateProvidersConfig(stored)

    expect(merged.ollama.baseUrl).toBe('http://127.0.0.1:8090/v1')
    expect(merged.ollama.model).toBe('qwen3.8-27b')
    expect(merged.runpod.baseUrl).not.toBe(merged.ollama.baseUrl)
  })

  it('keeps a runpod config the user has already customised', () => {
    const stored = JSON.stringify({
      version: 2,
      data: {
        runpod: { apiKey: 'ollama-no-key', model: 'custom-model-q4', baseUrl: 'https://podid-8000.proxy.runpod.net/v1', maxOutputTokens: 32768 },
      },
    })

    const merged = migrateProvidersConfig(stored)

    expect(merged.runpod.baseUrl).toBe('https://podid-8000.proxy.runpod.net/v1')
    expect(merged.runpod.model).toBe('custom-model-q4')
    expect(merged.runpod.maxOutputTokens).toBe(32768)
  })

  it('survives a legacy unversioned payload', () => {
    const merged = migrateProvidersConfig(JSON.stringify({
      gemini: { apiKey: 'k', model: 'm', baseUrl: 'b', maxOutputTokens: 1 },
    }))
    expect(merged.runpod.baseUrl).toBe(DEFAULT_CONFIGS.runpod.baseUrl)
  })
})

// A provider that can be SELECTED but not RELOADED is worse than one missing:
// the choice appears to work, then silently reverts on the next visit. This
// is the enumerated-in-N-places hazard of adding a provider — the AppHeader
// dropdown and these validation lists were both missed on the first pass.
describe('loadSavedProvider accepts the runpod provider', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('round-trips runpod from localStorage', () => {
    localStorage.setItem('web_canvas_active_provider', 'runpod')
    expect(loadSavedProvider()).toBe('runpod')
  })

  it('still rejects garbage', () => {
    localStorage.setItem('web_canvas_active_provider', 'not-a-provider')
    expect(loadSavedProvider()).not.toBe('not-a-provider')
  })
})

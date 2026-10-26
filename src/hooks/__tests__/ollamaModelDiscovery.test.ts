import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { createRoot } from 'react-dom/client'
import { act } from 'react'
import { createElement } from 'react'
import { useModelFetcher } from '../useModelFetcher'
import { useAppStore } from '../../store/useAppStore'

// A local endpoint serves whatever the user is running, so the shipped model
// list can never contain it — and the model field is a fixed dropdown, which
// made a local model literally unselectable. These tests pin the discovery.

const flush = async () => { await act(async () => { await Promise.resolve() }) }

// Stable identities. Inline arrows here would be new on every render, and the
// hook lists these callbacks in its effect deps — the effect would re-run,
// set state, re-render, and spin forever.
const noop = () => {}

function renderFetcher() {
  const container = document.createElement('div')
  const root = createRoot(container)
  const Probe = () => {
    useModelFetcher(true, noop, noop)
    return null
  }
  act(() => { root.render(createElement(Probe)) })
  return () => act(() => { root.unmount() })
}

const originalFetch = globalThis.fetch

beforeEach(() => {
  useAppStore.setState({
    availableModels: {},
    providerConfigs: {
      ...useAppStore.getState().providerConfigs,
      ollama: {
        apiKey: 'ollama-no-key',
        model: 'llama3',
        baseUrl: 'http://127.0.0.1:8090/v1',
        maxOutputTokens: 4096
      }
    }
  })
})

afterEach(() => {
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

describe('useModelFetcher — local endpoint discovery', () => {
  it("reads Ollama's own shape: {models:[{name}]}", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) =>
      String(url).endsWith('/v1/models')
        ? ({ ok: true, json: async () => ({ models: [{ name: 'qwen3.8-27b-instruct' }, { name: 'llama3' }] }) } as Response)
        : ({ ok: false, json: async () => ({}) } as Response)
    ) as typeof fetch

    const unmount = renderFetcher()
    await flush()

    expect(useAppStore.getState().availableModels.ollama).toEqual(['qwen3.8-27b-instruct', 'llama3'])
    unmount()
  })

  it("reads OpenAI's shape: {data:[{id}]}", async () => {
    globalThis.fetch = vi.fn(async () =>
      ({ ok: true, json: async () => ({ data: [{ id: 'local-model-a' }] }) } as Response)
    ) as typeof fetch

    const unmount = renderFetcher()
    await flush()

    expect(useAppStore.getState().availableModels.ollama).toEqual(['local-model-a'])
    unmount()
  })

  // Regression: the backend normalizes to plain strings, and the client parser
  // read `.name` off each entry — undefined for a string, so the list came back
  // empty and the dropdown silently kept its hardcoded contents. Each side was
  // tested against its own fixture; nothing covered the seam.
  it("reads the backend's already-normalized {models:[string]}", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) =>
      String(url).includes('/api/models')
        ? ({ ok: true, json: async () => ({ models: ['qwen3.8-27b-instruct'] }) } as Response)
        : ({ ok: false, json: async () => ({}) } as Response)
    ) as typeof fetch

    const unmount = renderFetcher()
    await flush()

    expect(useAppStore.getState().availableModels.ollama).toEqual(['qwen3.8-27b-instruct'])
    unmount()
  })

  it('replaces a configured model the endpoint does not serve', async () => {
    // Otherwise every send 404s against a name left over from another endpoint.
    globalThis.fetch = vi.fn(async () =>
      ({ ok: true, json: async () => ({ models: [{ name: 'qwen3.8-27b-instruct' }] }) } as Response)
    ) as typeof fetch

    const unmount = renderFetcher()
    await flush()

    expect(useAppStore.getState().providerConfigs.ollama.model).toBe('qwen3.8-27b-instruct')
    unmount()
  })

  it('keeps a configured model the endpoint does serve', async () => {
    useAppStore.setState({
      providerConfigs: {
        ...useAppStore.getState().providerConfigs,
        ollama: { ...useAppStore.getState().providerConfigs.ollama, model: 'llama3' }
      }
    })
    globalThis.fetch = vi.fn(async () =>
      ({ ok: true, json: async () => ({ models: [{ name: 'qwen3.8-27b-instruct' }, { name: 'llama3' }] }) } as Response)
    ) as typeof fetch

    const unmount = renderFetcher()
    await flush()

    expect(useAppStore.getState().providerConfigs.ollama.model).toBe('llama3')
    unmount()
  })

  it('stays quiet when no local server is running', async () => {
    // The common case, not an error: no dropdown change, no error banner.
    globalThis.fetch = vi.fn(async () => { throw new Error('ECONNREFUSED') }) as typeof fetch

    const unmount = renderFetcher()
    await flush()

    expect(useAppStore.getState().availableModels.ollama ?? []).toEqual([])
    expect(useAppStore.getState().providerConfigs.ollama.model).toBe('llama3')
    unmount()
  })
})

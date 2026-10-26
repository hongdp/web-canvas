import { describe, it, expect } from 'vitest'
import { resolveDocumentProtocol, autoProtocolFor, DOCUMENT_PROTOCOLS } from '../protocolChoice'

// Why this setting exists at all: measured on grok, the SAME request sent tool
// arguments in ONE 113-character delta but ordinary content in 54. A single
// chunk leaves nothing to render mid-stream, so the live preview dies. A local
// llama.cpp sends 205 argument deltas and streams fine — but cannot follow the
// tag protocol at all. Neither protocol wins everywhere.
describe('resolveDocumentProtocol', () => {
  it('honours an explicit choice over any measurement', () => {
    // The table describes other people's streaming behaviour and will go
    // stale; the user must be able to overrule it without a code change.
    expect(resolveDocumentProtocol('grok', 'tools')).toBe('tools')
    expect(resolveDocumentProtocol('ollama', 'markup')).toBe('markup')
  })

  it('keeps coarse-streaming providers on markup under auto', () => {
    for (const provider of ['grok', 'gemini']) {
      expect(resolveDocumentProtocol(provider, 'auto'), provider).toBe('markup')
      expect(resolveDocumentProtocol(provider, undefined), provider).toBe('markup')
    }
  })

  it('puts local models on tools under auto', () => {
    // A local Qwen3-14B never emitted <canvas> and always produced a valid
    // tool call for the same request.
    expect(resolveDocumentProtocol('ollama', 'auto')).toBe('tools')
  })

  it('puts runpod on tools too — it is the same llama.cpp, just rented', () => {
    // The provider is separate so a local model and a pod can be configured
    // at once; the protocol need is identical, and defaulting it to markup
    // would silently disable document editing on the pod.
    expect(resolveDocumentProtocol('runpod', 'auto')).toBe('tools')
    expect(resolveDocumentProtocol('runpod', undefined)).toBe('tools')
  })

  it('defaults an unknown provider to markup, the shipped path', () => {
    expect(resolveDocumentProtocol('some-new-provider', 'auto')).toBe('markup')
  })

  it('always resolves to something the prompt builder accepts', () => {
    for (const p of DOCUMENT_PROTOCOLS) {
      for (const provider of ['grok', 'ollama', 'openai']) {
        expect(['tools', 'markup']).toContain(resolveDocumentProtocol(provider, p))
      }
    }
  })
})

describe('autoProtocolFor', () => {
  it('reports what auto would pick, for labelling the setting', () => {
    expect(autoProtocolFor('ollama')).toBe('tools')
    expect(autoProtocolFor('grok')).toBe('markup')
  })
})

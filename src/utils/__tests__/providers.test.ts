import { describe, it, expect } from 'vitest'
import { PROVIDERS, PROVIDER_IDS, PROVIDER_MODELS, DEFAULT_CONFIGS, isProviderId, modelsFor, providerDisplayName } from '../providers'

describe('the provider registry', () => {
  it('has one row per provider, in the order the UI lists them', () => {
    expect(PROVIDER_IDS).toEqual(['gemini', 'openai', 'anthropic', 'ollama', 'runpod', 'grok', 'moonshot'])
    for (const id of PROVIDER_IDS) expect(PROVIDERS[id].id).toBe(id)
  })

  it('is where the shipped lists and the defaults the store starts from come from', () => {
    // (A VITE_*_MODEL in .env may name a model off the shipped list; discovery replaces it at runtime.)
    for (const id of PROVIDER_IDS) {
      expect(PROVIDER_MODELS[id]).toBe(PROVIDERS[id].fallbackModels)
      expect(PROVIDERS[id].fallbackModels.length).toBeGreaterThan(0)
      expect(DEFAULT_CONFIGS[id]).toBe(PROVIDERS[id].defaults)
    }
    expect(DEFAULT_CONFIGS.moonshot.maxOutputTokens).toBe(32768)
  })

  it('offers the discovered list when there is one, else the shipped one', () => {
    expect(modelsFor('grok', {})).toEqual(PROVIDERS.grok.fallbackModels)
    expect(modelsFor('grok', { grok: [] })).toEqual(PROVIDERS.grok.fallbackModels)
    expect(modelsFor('grok', { grok: ['grok-9'] })).toEqual(['grok-9'])
  })

  it('names providers for the chat and recognises ids', () => {
    expect(providerDisplayName('moonshot')).toBe('Kimi')
    expect(providerDisplayName('grok')).toBe('Grok')
    expect(providerDisplayName('openai')).toBe('OpenAI')
    expect(providerDisplayName('nope')).toBe('Nope')
    expect(isProviderId('runpod')).toBe(true)
    expect(isProviderId('toString')).toBe(false)
    expect(isProviderId(undefined)).toBe(false)
  })

  it('points each hosted provider at its own host for model discovery', () => {
    const hosts = (id: 'openai' | 'moonshot' | 'gemini') => { const d = PROVIDERS[id].discovery; return d.kind === 'official' ? d.hosts : null }
    expect(hosts('openai')?.test('https://api.openai.com/v1/')).toBe(true)
    expect(hosts('openai')?.test('http://localhost:8080/v1/')).toBe(false)
    expect(hosts('moonshot')?.test('https://api.moonshot.cn/v1/')).toBe(true)
    expect(hosts('gemini')?.test('https://generativelanguage.googleapis.com/v1beta/')).toBe(true)
  })
})

/**
 * The provider registry: everything the app knows about an LLM provider that
 * is not a measurement, in one row per provider.
 *
 * Problem: adding Kimi (2026-10-11) touched 31 files. Ten of them held the
 *   same row copied by hand — the type union, the defaults, two persistence
 *   whitelists, seven `availableXxxModels` fields with their setters, the
 *   Settings tabs / labels / placeholders / help text, the header's dropdown,
 *   the chat's label, and a model-discovery effect per provider.
 * Fix: one row here; everything else iterates `PROVIDER_IDS` or reads
 *   `PROVIDERS[id]`. The measured tables stay where they are (reasoning
 *   effort, cache profile, context window, protocol choice: each a line per
 *   provider, mirrored in scripts/wc_text and parity-tested) — those are facts
 *   about the provider, not boilerplate. Adding a provider is: the union in
 *   types/llm.ts, a row here, a line in each table, and the wire format on
 *   the server.
 */
import type { LLMProvider, ProviderConfig } from '../types/llm'

/** Where a provider's model list comes from (hooks/useModelFetcher). */
export type ModelDiscovery =
  /** The key's own list, through the backend (/api/models), from the provider's host(s) only. */
  | { kind: 'official'; hosts: RegExp }
  /** Whatever the configured endpoint serves: its /models, direct and then through the backend. */
  | { kind: 'local' }
  | { kind: 'none' }

export interface ProviderSpec {
  id: LLMProvider
  /** "Grok (xAI)": the Settings heading and the header's provider dropdown. */
  label: string
  /** "Grok": the Settings tab and the chat's "{provider} is thinking". */
  shortLabel: string
  defaults: ProviderConfig
  /** Shown until a discovered list arrives; the whole list for a provider with none. */
  fallbackModels: string[]
  /** Chosen when the discovered list lacks the configured model; else the list's first. */
  recommendedModel?: string
  discovery: ModelDiscovery
  /** A local server: the key field is optional and `ollama-no-key` means none. */
  keyOptional?: boolean
  keyPlaceholder: string
  baseUrlPlaceholder: string
  /** The help box: an intro, a console link, then "The default base URL is …" and an optional extra sentence. */
  help: { intro: string; link?: { href: string; label: string }; extra?: string }
}

const env = import.meta.env

export const PROVIDERS: Record<LLMProvider, ProviderSpec> = {
  gemini: {
    id: 'gemini',
    label: 'Gemini',
    shortLabel: 'Gemini',
    defaults: {
      apiKey: env.VITE_GEMINI_API_KEY || '',
      model: env.VITE_GEMINI_MODEL || 'gemini-3.8-flash',
      baseUrl: env.VITE_GEMINI_BASE_URL || 'https://generativelanguage.googleapis.com/v1beta',
      // The lowest threshold Gemini offers. Each category stays adjustable in
      // Settings; a saved config keeps whatever the user set there.
      geminiSafetySettings: [
        { category: 'HARM_CATEGORY_HARASSMENT', threshold: 'BLOCK_NONE' },
        { category: 'HARM_CATEGORY_HATE_SPEECH', threshold: 'BLOCK_NONE' },
        { category: 'HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold: 'BLOCK_NONE' },
        { category: 'HARM_CATEGORY_DANGEROUS_CONTENT', threshold: 'BLOCK_NONE' }
      ],
      maxOutputTokens: 16384
    },
    // 2.5 is closed to new keys.
    fallbackModels: ['gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.5-flash', 'gemini-3.1-pro-preview', 'gemini-flash-latest', 'gemini-pro-latest'],
    recommendedModel: 'gemini-3.8-flash',
    discovery: { kind: 'official', hosts: /^https:\/\/generativelanguage\.googleapis\.com\// },
    keyPlaceholder: 'Enter Google AI Studio API Key...',
    baseUrlPlaceholder: 'e.g. https://generativelanguage.googleapis.com/v1beta',
    help: { intro: 'To get a free Gemini API key, visit the', link: { href: 'https://aistudio.google.com/', label: 'Google AI Studio' } }
  },
  openai: {
    id: 'openai',
    label: 'OpenAI',
    shortLabel: 'OpenAI',
    defaults: {
      apiKey: env.VITE_OPENAI_API_KEY || '',
      model: env.VITE_OPENAI_MODEL || 'gpt-5.5',
      baseUrl: env.VITE_OPENAI_BASE_URL || 'https://api.openai.com/v1',
      maxOutputTokens: 16384
    },
    fallbackModels: ['gpt-5.5', 'gpt-6.1-sol', 'gpt-5.4-mini', 'gpt-5.1', 'gpt-4.1', 'o3'],
    recommendedModel: 'gpt-5.5',
    // Only OpenAI's own host: a compatible server under this provider keeps the shipped list.
    discovery: { kind: 'official', hosts: /^https:\/\/api\.openai\.com\// },
    keyPlaceholder: 'Enter OpenAI API Key (sk-...)...',
    baseUrlPlaceholder: 'e.g. https://api.openai.com/v1',
    help: { intro: 'To get an OpenAI API key, visit the', link: { href: 'https://platform.openai.com/', label: 'OpenAI Platform' } }
  },
  anthropic: {
    id: 'anthropic',
    label: 'Anthropic',
    shortLabel: 'Anthropic',
    defaults: {
      apiKey: env.VITE_ANTHROPIC_API_KEY || '',
      model: env.VITE_ANTHROPIC_MODEL || 'claude-sonnet-5-5',
      baseUrl: env.VITE_ANTHROPIC_BASE_URL || 'https://api.anthropic.com/v1',
      maxOutputTokens: 16384
    },
    fallbackModels: ['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5', 'claude-fable-5-1', 'claude-sonnet-4-6', 'claude-opus-4-6'],
    recommendedModel: 'claude-sonnet-5-5',
    discovery: { kind: 'official', hosts: /^https:\/\/api\.anthropic\.com\// },
    keyPlaceholder: 'Enter Anthropic API Key (sk-ant-...)...',
    baseUrlPlaceholder: 'e.g. https://api.anthropic.com/v1',
    help: { intro: 'To get an Anthropic API key, visit the', link: { href: 'https://console.anthropic.com/', label: 'Anthropic Console' } }
  },
  ollama: {
    id: 'ollama',
    label: 'Ollama (Local)',
    shortLabel: 'Ollama',
    defaults: {
      apiKey: env.VITE_OLLAMA_API_KEY || 'ollama-no-key',
      model: env.VITE_OLLAMA_MODEL || 'llama3',
      baseUrl: env.VITE_OLLAMA_BASE_URL || 'http://localhost:11434/v1',
      maxOutputTokens: 16384
    },
    fallbackModels: ['llama3', 'mistral', 'gemma2', 'codegemma', 'phi3'],
    discovery: { kind: 'local' },
    keyOptional: true,
    keyPlaceholder: 'Optional token for authenticated local proxies...',
    baseUrlPlaceholder: 'e.g. http://localhost:11434/v1',
    help: { intro: 'Ollama runs locally on your machine. Ensure Ollama is running (run `ollama serve`) and the base URL is reachable.' }
  },
  // A remote llama.cpp on a rented GPU. Separate from `ollama` on purpose:
  // both are OpenAI-compatible servers with no auth, and keeping one slot for
  // each is what lets the local model and the pod stay configured at once.
  // The default points at an SSH tunnel (`ssh -L 8092:localhost:<port>`)
  // rather than a pod address, because a tunnel's port survives a pod restart
  // while RunPod's own port mapping does not.
  runpod: {
    id: 'runpod',
    label: 'RunPod (Cloud GPU)',
    shortLabel: 'RunPod',
    defaults: {
      apiKey: env.VITE_RUNPOD_API_KEY || 'ollama-no-key',
      model: env.VITE_RUNPOD_MODEL || 'your-model',
      baseUrl: env.VITE_RUNPOD_BASE_URL || 'http://127.0.0.1:8100/v1',
      maxOutputTokens: 16384
    },
    // A placeholder: llama.cpp's router names each model after its directory
    // on the server, and a live listing replaces this.
    fallbackModels: ['your-model'],
    discovery: { kind: 'local' },
    keyOptional: true,
    keyPlaceholder: 'Leave as ollama-no-key unless the endpoint sets --api-key...',
    baseUrlPlaceholder: 'e.g. http://127.0.0.1:8100/v1 (tunnel) or https://<pod-id>-8000.proxy.runpod.net/v1',
    help: { intro: 'A llama.cpp server on a rented GPU, reached through an SSH tunnel or the pod\'s *.proxy.runpod.net address.' }
  },
  grok: {
    id: 'grok',
    label: 'Grok (xAI)',
    shortLabel: 'Grok',
    defaults: {
      apiKey: env.VITE_GROK_API_KEY || '',
      model: env.VITE_GROK_MODEL || 'grok-3',
      baseUrl: env.VITE_GROK_BASE_URL || 'https://api.x.ai/v1',
      maxOutputTokens: 16384
    },
    fallbackModels: ['grok-4.3', 'grok-build-0.1', 'grok-3', 'grok-2', 'grok-2-vision', 'grok-beta'],
    discovery: { kind: 'official', hosts: /^https:\/\/api\.x\.ai\// },
    keyPlaceholder: 'Enter Grok API Key (xai-...)...',
    baseUrlPlaceholder: 'e.g. https://api.x.ai/v1',
    help: { intro: 'To get a Grok API key, visit the', link: { href: 'https://console.x.ai/', label: 'xAI Console' } }
  },
  // Moonshot's Kimi, on Chat Completions. api.moonshot.cn/v1 for a China
  // account (a separate platform; keys do not cross). Kimi's max_tokens
  // covers the reasoning AND the reply, and its docs ask for 16k or more in a
  // tool-calling loop: 32k, not 16k.
  moonshot: {
    id: 'moonshot',
    label: 'Kimi (Moonshot)',
    shortLabel: 'Kimi',
    defaults: {
      apiKey: env.VITE_MOONSHOT_API_KEY || '',
      model: env.VITE_MOONSHOT_MODEL || 'kimi-k2.6',
      baseUrl: env.VITE_MOONSHOT_BASE_URL || 'https://api.moonshot.ai/v1',
      maxOutputTokens: 32768
    },
    // platform.kimi.ai, 2026-10-11.
    fallbackModels: ['kimi-k2.6', 'kimi-k3', 'kimi-k2.7-code', 'kimi-k2.7-code-highspeed'],
    // 262k window, thinking on, a quarter of kimi-k3's price.
    recommendedModel: 'kimi-k2.6',
    discovery: { kind: 'official', hosts: /^https:\/\/api\.moonshot\.(?:ai|cn)\// },
    keyPlaceholder: 'Enter Moonshot API Key (sk-...)...',
    baseUrlPlaceholder: 'e.g. https://api.moonshot.ai/v1 (or https://api.moonshot.cn/v1)',
    help: { intro: 'To get a Kimi API key, visit the', link: { href: 'https://platform.kimi.ai/', label: 'Kimi Open Platform' }, extra: 'A China account uses https://api.moonshot.cn/v1.' }
  }
}

/** Every provider, in the order the UI lists them. */
export const PROVIDER_IDS = Object.keys(PROVIDERS) as LLMProvider[]

export const isProviderId = (value: unknown): value is LLMProvider =>
  typeof value === 'string' && Object.prototype.hasOwnProperty.call(PROVIDERS, value)

/** The shipped model lists by provider (shown until discovery answers). */
export const PROVIDER_MODELS: Record<LLMProvider, string[]> = Object.fromEntries(
  PROVIDER_IDS.map(id => [id, PROVIDERS[id].fallbackModels])
) as Record<LLMProvider, string[]>

/** Every provider's default config, as the store starts and as migrations fill gaps. */
export const DEFAULT_CONFIGS: Record<LLMProvider, ProviderConfig> = Object.fromEntries(
  PROVIDER_IDS.map(id => [id, PROVIDERS[id].defaults])
) as Record<LLMProvider, ProviderConfig>

/** The models to offer: the discovered list when there is one, else the shipped list. */
export function modelsFor(provider: LLMProvider, discovered: Partial<Record<LLMProvider, string[]>>): string[] {
  const list = discovered[provider]
  return list && list.length > 0 ? list : PROVIDERS[provider].fallbackModels
}

/** The chat's name for a provider ("Kimi is thinking…"). */
export const providerDisplayName = (provider: string): string =>
  isProviderId(provider) ? PROVIDERS[provider].shortLabel : provider.charAt(0).toUpperCase() + provider.slice(1)

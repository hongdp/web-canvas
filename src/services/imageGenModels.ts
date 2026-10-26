/**
 * Image generation provider metadata and model discovery.
 * Pure (non-React) logic extracted from ImageGenerationModal.tsx:
 * aspect-ratio presets, provider labels/model catalogs, and the
 * per-provider ListModels discovery calls.
 */

import type { ImageGenProvider } from './imageGen'
import type { LLMProvider } from '../types/llm'

export type AspectRatio = '1:1' | '16:9' | '9:16' | '4:3' | '3:4'

export const ASPECT_RATIOS: { label: string; value: AspectRatio; w: number; h: number }[] = [
  { label: '1:1', value: '1:1', w: 1024, h: 1024 },
  { label: '16:9', value: '16:9', w: 1792, h: 1024 },
  { label: '9:16', value: '9:16', w: 1024, h: 1792 },
  { label: '4:3', value: '4:3', w: 1024, h: 768 },
  { label: '3:4', value: '3:4', w: 768, h: 1024 },
]

export const PROVIDER_LABELS: Record<ImageGenProvider, string> = {
  openai: 'DALL·E',
  gemini: 'Imagen',
  stabilityai: 'Stability',
  grok: 'Grok Image',
}

export const PROVIDER_FULL_LABELS: Record<ImageGenProvider, string> = {
  openai: 'OpenAI DALL·E',
  gemini: 'Google Imagen',
  stabilityai: 'Stability AI',
  grok: 'Grok Image (xAI)',
}

// Which LLM provider config shares an API key with each image provider
// e.g. Grok image generation → use providerConfigs.grok.apiKey
export const PROVIDER_TO_LLM: Partial<Record<ImageGenProvider, LLMProvider>> = {
  openai: 'openai',
  gemini: 'gemini',
  grok: 'grok',
  // stabilityai has no LLM counterpart — uses its own dedicated key
}

export const ALL_MODELS: Record<ImageGenProvider, string[]> = {
  openai: ['dall-e-3', 'dall-e-2', 'gpt-image-1'],
  gemini: ['imagen-3.0-generate-001', 'imagen-3.0-fast-generate-001'],
  stabilityai: ['stable-diffusion-xl-1024-v1-0', 'stable-diffusion-v1-6', 'stable-image-core', 'stable-image-ultra'],
  grok: ['grok-2-image', 'grok-imagine-image-quality'],
}

// ── Model discovery ──────────────────────────────────────────────────────────
// Known image-generation model name patterns per provider (for filtering /v1/models)
const IMAGE_MODEL_PATTERNS: Record<ImageGenProvider, RegExp> = {
  openai:     /dall-e|gpt-image|imagen/i,
  gemini:     /imagen/i,
  stabilityai:/stable/i,
  grok:       /grok.*image|aurora|imagine/i,
}

export async function fetchImageModels(
  provider: ImageGenProvider,
  apiKey: string,
  baseUrl?: string
): Promise<string[]> {
  const fallback = ALL_MODELS[provider]

  try {
    if (provider === 'gemini') {
      // Gemini uses its own models API
      const base = (baseUrl || 'https://generativelanguage.googleapis.com/v1beta').replace(/\/$/, '')
      const res = await fetch(`${base}/models?key=${apiKey}`)
      if (!res.ok) return fallback
      const data = await res.json() as { models?: Array<{ name: string; supportedGenerationMethods?: string[] }> }
      const models = (data.models || [])
        .filter(m =>
          (m.supportedGenerationMethods || []).some((method: string) =>
            method.toLowerCase().includes('generate') || method.toLowerCase().includes('predict')
          ) && IMAGE_MODEL_PATTERNS.gemini.test(m.name)
        )
        .map(m => m.name.startsWith('models/') ? m.name.slice(7) : m.name)
      return models.length ? models : fallback
    }

    if (provider === 'stabilityai') {
      // Stability AI has an engines endpoint
      const base = (baseUrl || 'https://api.stability.ai').replace(/\/$/, '')
      const res = await fetch(`${base}/v1/engines/list`, {
        headers: { Authorization: `Bearer ${apiKey}`, Accept: 'application/json' },
      })
      if (!res.ok) return fallback
      const data: unknown = await res.json()
      const ids = (Array.isArray(data) ? data as Array<{ id?: string }> : [])
        .map(e => e.id as string)
        .filter(Boolean)
      return ids.length ? ids : fallback
    }

    // OpenAI-compatible endpoint (openai + grok)
    const base = (
      baseUrl ||
      (provider === 'grok' ? 'https://api.x.ai/v1' : 'https://api.openai.com/v1')
    ).replace(/\/$/, '')
    const res = await fetch(`${base}/models`, {
      headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    })
    if (!res.ok) return fallback
    const data = await res.json() as { data?: Array<{ id: string }> }
    const pattern = IMAGE_MODEL_PATTERNS[provider]
    const models = (data.data || [])
      .map(m => m.id)
      .filter((id: string) => pattern.test(id))
      .sort()
    return models.length ? models : fallback
  } catch {
    return fallback
  }
}

/**
 * Image Generation Service
 * Supports OpenAI DALL·E, Google Gemini Imagen, Stability AI, and Grok Aurora (xAI).
 *
 * Also provides LLM-powered prompt enhancement to rewrite user prompts into rich,
 * detailed image generation prompts using the currently configured chat LLM.
 */

import { streamLLM } from './llm'
import type { ProviderConfig } from '../types/llm'

export type ImageGenProvider = 'openai' | 'gemini' | 'stabilityai' | 'grok'

export interface ImageGenConfig {
  provider: ImageGenProvider
  apiKey: string
  model?: string
  baseUrl?: string
  width?: number
  height?: number
  /** The user's preset as a ratio string ("16:9") — what xAI's API takes. */
  aspectRatio?: string
  steps?: number
  style?: string
  styleSystemPrompt?: string
  llmEnhancementEnabled?: boolean
}

export interface ImageGenResult {
  dataUrl: string // base64 Data URL: "data:image/png;base64,..."
  revisedPrompt?: string
}

export const DEFAULT_IMAGE_STYLE_SYSTEM_PROMPT = `You are an expert prompt engineer specializing in AI image generation. Your task is to rewrite and enrich a basic image prompt into a highly detailed, vivid, and evocative prompt optimized for diffusion models.

Guidelines:
- Expand on the subject, setting, mood, lighting, color palette, and artistic style
- Incorporate cinematic or photographic language (e.g., "golden hour lighting", "shallow depth of field", "hyperrealistic", "8K detail")
- Preserve the core intent of the original prompt
- Incorporate any additional context provided to make the image contextually relevant
- Keep the final prompt concise (2-4 sentences max), dense with descriptive terms
- Output ONLY the enhanced prompt text — no explanation, no labels, no quotes`

/**
 * Generate an image from a text prompt.
 */
export async function generateImage(
  prompt: string,
  config: ImageGenConfig
): Promise<ImageGenResult> {
  if (!config.apiKey) {
    throw new Error(`API key is required for image generation with ${config.provider}.`)
  }

  switch (config.provider) {
    case 'openai':
      return generateWithOpenAI(prompt, config)
    case 'gemini':
      return generateWithGemini(prompt, config)
    case 'stabilityai':
      return generateWithStabilityAI(prompt, config)
    case 'grok':
      return generateWithGrok(prompt, config)
    default:
      throw new Error(`Unsupported image generation provider: ${config.provider}`)
  }
}

/**
 * Enhance a prompt using the currently configured LLM before image generation.
 * Streams the response and calls onChunk for each token so the UI can show live output.
 *
 * @param rawPrompt - The user's base prompt (often from selected text)
 * @param additionalContext - Extra context from the document (optional)
 * @param styleSystemPrompt - System instruction that controls the style/enhancement logic
 * @param llmConfig - The active LLM provider config (apiKey, model, baseUrl, etc.)
 * @param onChunk - Called with each streamed text chunk (for live preview)
 * @returns The full enhanced prompt string
 */
export function enhancePromptWithLLM(
  rawPrompt: string,
  additionalContext: string,
  styleSystemPrompt: string,
  llmConfig: ProviderConfig & { provider: string },
  onChunk: (chunk: string) => void,
  signal?: AbortSignal
): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const contextSection = additionalContext.trim()
      ? `\n\n<additional_context>\n${additionalContext.trim()}\n</additional_context>`
      : ''

    const userMessage = `Rewrite and enhance the following image prompt${contextSection ? ' using the provided context' : ''}:\n\n<prompt>${rawPrompt.trim()}</prompt>${contextSection}`

    let fullText = ''

    streamLLM(
      [
        { role: 'system', content: styleSystemPrompt || DEFAULT_IMAGE_STYLE_SYSTEM_PROMPT },
        { role: 'user', content: userMessage },
      ],
      { ...llmConfig, signal },
      {
        onChunk: (chunk) => {
          fullText += chunk
          onChunk(chunk)
        },
        onDone: (text) => resolve(text || fullText),
        onError: reject,
      }
    )
  })
}

/**
 * OpenAI DALL·E image generation
 */
async function generateWithOpenAI(prompt: string, config: ImageGenConfig): Promise<ImageGenResult> {
  const model = config.model || 'dall-e-3'
  const baseUrl = config.baseUrl || 'https://api.openai.com/v1'

  const sizeMap: Record<string, string> = {
    '1024x1024': '1024x1024',
    '1792x1024': '1792x1024',
    '1024x1792': '1024x1792',
  }
  const w = config.width || 1024
  const h = config.height || 1024
  const sizeKey = `${w}x${h}`
  const size = sizeMap[sizeKey] || '1024x1024'

  const body: Record<string, unknown> = {
    model,
    prompt,
    n: 1,
    size,
    response_format: 'b64_json',
  }

  if (model === 'dall-e-3' && config.style) {
    body.style = config.style // 'vivid' | 'natural'
  }

  const response = await fetch(`${baseUrl}/images/generations`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${config.apiKey}`,
    },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const errText = await response.text()
    throw new Error(`OpenAI image generation error (${response.status}): ${errText || response.statusText}`)
  }

  const data = await response.json()
  const imageData = data.data?.[0]
  if (!imageData) throw new Error('No image data returned from OpenAI.')

  return {
    dataUrl: `data:image/png;base64,${imageData.b64_json}`,
    revisedPrompt: imageData.revised_prompt,
  }
}

/**
 * Grok (xAI) image generation — OpenAI-compatible endpoint.
 * Model: grok-2-image (Aurora architecture)
 */
async function generateWithGrok(prompt: string, config: ImageGenConfig): Promise<ImageGenResult> {
  const model = config.model || 'grok-2-image'
  const baseUrl = config.baseUrl || 'https://api.x.ai/v1'

  const body: Record<string, unknown> = {
    model,
    prompt,
    n: 1,
    response_format: 'b64_json',
  }

  // xAI's images/generations has NO width/height parameters — this used to
  // send them anyway (behind a comment claiming Aurora accepts them), and the
  // API silently ignored unknown fields, so the aspect-ratio picker did
  // nothing (user-reported). The documented control is `aspect_ratio` with
  // ratio strings; checked against docs.x.ai (model-capabilities/images) on
  // 2026-08-30. Legacy grok-2-image predates the parameter and may still
  // ignore it — that is the model's limitation, not a request-shape bug.
  // No derivation fallback from width/height: "1344:768" is not a member of
  // the API's ratio enum, and an invalid value is worse than omitting the
  // field (which just gets the API default).
  if (config.aspectRatio) body.aspect_ratio = config.aspectRatio

  const response = await fetch(`${baseUrl}/images/generations`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${config.apiKey}`,
    },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const errText = await response.text()
    throw new Error(`Grok (xAI) image generation error (${response.status}): ${errText || response.statusText}`)
  }

  const data = await response.json()
  // Try b64_json first, then url
  const imageData = data.data?.[0]
  if (!imageData) throw new Error('No image data returned from Grok.')

  if (imageData.b64_json) {
    return { dataUrl: `data:image/png;base64,${imageData.b64_json}` }
  }

  if (imageData.url) {
    // Fetch the URL and convert to base64
    const imgRes = await fetch(imageData.url)
    if (!imgRes.ok) throw new Error('Failed to fetch generated image from Grok URL.')
    const blob = await imgRes.blob()
    const b64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve((reader.result as string))
      reader.onerror = reject
      reader.readAsDataURL(blob)
    })
    return { dataUrl: b64 }
  }

  throw new Error('Grok returned image data in an unrecognised format.')
}

/**
 * Google Gemini Imagen generation
 */
async function generateWithGemini(prompt: string, config: ImageGenConfig): Promise<ImageGenResult> {
  const model = config.model || 'imagen-3.0-generate-001'
  const baseUrl = config.baseUrl || 'https://generativelanguage.googleapis.com/v1beta'

  const body: { instances: Array<{ prompt: string }>; parameters: { sampleCount: number; aspectRatio?: string } } = {
    instances: [{ prompt }],
    parameters: { sampleCount: 1 },
  }

  if (config.width && config.height) {
    body.parameters.aspectRatio = config.width >= config.height
      ? (config.width / config.height > 1.4 ? '16:9' : '1:1')
      : '9:16'
  }

  const url = `${baseUrl}/models/${model}:predict?key=${config.apiKey}`
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const errText = await response.text()
    throw new Error(`Gemini Imagen error (${response.status}): ${errText || response.statusText}`)
  }

  const data = await response.json()
  const prediction = data.predictions?.[0]
  if (!prediction) throw new Error('No image prediction returned from Gemini Imagen.')

  const b64 = prediction.bytesBase64Encoded
  const mimeType = prediction.mimeType || 'image/png'

  return { dataUrl: `data:${mimeType};base64,${b64}` }
}

/**
 * Stability AI / generic Stable Diffusion image generation (SDXL API)
 */
async function generateWithStabilityAI(
  prompt: string,
  config: ImageGenConfig
): Promise<ImageGenResult> {
  const model = config.model || 'stable-diffusion-xl-1024-v1-0'
  const baseUrl = config.baseUrl || 'https://api.stability.ai/v1'

  const width = config.width || 1024
  const height = config.height || 1024
  const steps = config.steps || 30

  const body = {
    text_prompts: [{ text: prompt, weight: 1 }],
    cfg_scale: 7,
    height,
    width,
    steps,
    samples: 1,
  }

  const response = await fetch(`${baseUrl}/generation/${model}/text-to-image`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      Authorization: `Bearer ${config.apiKey}`,
    },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const errText = await response.text()
    throw new Error(`Stability AI error (${response.status}): ${errText || response.statusText}`)
  }

  const data = await response.json()
  const artifact = data.artifacts?.[0]
  if (!artifact) throw new Error('No image artifact returned from Stability AI.')

  return { dataUrl: `data:image/png;base64,${artifact.base64}` }
}

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { generateImage } from '../imageGen'

// The aspect-ratio picker did nothing on Grok (user-reported): the request
// carried width/height, which xAI's images/generations does not define and
// silently ignored. The documented control is `aspect_ratio` with ratio
// strings (docs.x.ai, checked 2026-08-30). These tests pin the request SHAPE,
// which is the part that regressed invisibly.
describe('generateImage — grok request shape', () => {
  const captured: { url: string; body: Record<string, unknown> }[] = []

  beforeEach(() => {
    captured.length = 0
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      captured.push({ url: String(url), body: JSON.parse(String(init?.body)) })
      return new Response(JSON.stringify({ data: [{ b64_json: 'aW1n' }] }), { status: 200 })
    }))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends aspect_ratio, never the undefined width/height fields', async () => {
    const result = await generateImage('a lighthouse at dusk', {
      provider: 'grok',
      apiKey: 'xai-test',
      model: 'grok-imagine-image-quality',
      width: 1792,
      height: 1024,
      aspectRatio: '16:9',
    })

    expect(captured).toHaveLength(1)
    expect(captured[0].url).toBe('https://api.x.ai/v1/images/generations')
    expect(captured[0].body.aspect_ratio).toBe('16:9')
    // The old shape: silently ignored by the API, so the picker did nothing.
    expect(captured[0].body).not.toHaveProperty('width')
    expect(captured[0].body).not.toHaveProperty('height')
    expect(result.dataUrl).toBe('data:image/png;base64,aW1n')
  })

  it('omits aspect_ratio entirely when no preset is given', async () => {
    // Deriving "1792:1024" from width/height would be OUTSIDE the API's ratio
    // enum (it reduces to 7:4, and raw pixel pairs are invalid) — worse than
    // the API default. Absence is the correct encoding of "no preference".
    await generateImage('a lighthouse', {
      provider: 'grok',
      apiKey: 'xai-test',
      width: 1792,
      height: 1024,
    })

    expect(captured[0].body).not.toHaveProperty('aspect_ratio')
    expect(captured[0].body).not.toHaveProperty('width')
  })
})

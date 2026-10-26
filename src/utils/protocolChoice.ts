/**
 * Which document protocol a model gets: native tool calls, or the tag markup.
 *
 * Neither wins outright — the migration to tools traded one failure for
 * another, and both halves are measured:
 *
 *  - MARKUP fails on small models. A local Qwen3-14B, asked to expand a
 *    paragraph, emitted bare <p> with no <canvas> wrapper every time, so
 *    nothing reached the document; the same request as a tool call succeeded on
 *    the first attempt. The tag protocol is a private language no model was
 *    trained on.
 *
 *  - TOOLS destroy the live preview on providers that do not stream arguments
 *    incrementally. Measured on grok, same request, same document: tool
 *    arguments arrived in ONE 113-character delta, while ordinary content
 *    arrived in 54. With a single chunk there is no intermediate state to
 *    render, so the document only changes when the turn ends.
 *
 * Hence a per-model setting rather than a global choice. 'auto' encodes the
 * measurements above so the default is right without anyone configuring
 * anything; the explicit values exist because this table describes other
 * people's streaming behaviour and will go stale — if xAI starts sending tool
 * arguments in fragments, 'tools' should become the better pick for grok
 * before this file catches up.
 */

export const DOCUMENT_PROTOCOLS = ['auto', 'tools', 'markup'] as const
export type DocumentProtocol = (typeof DOCUMENT_PROTOCOLS)[number]

/** What 'auto' resolves to, and why. */
type Resolved = 'tools' | 'markup'

/**
 * Providers measured to deliver tool-call arguments in one (or very few)
 * chunks. They keep the markup protocol under 'auto' so the live preview
 * survives; both have always followed the tags correctly.
 */
const COARSE_TOOL_STREAMING = new Set(['grok', 'gemini'])

/**
 * Providers whose models are too small to follow a private tag format, and
 * which do stream tool arguments finely (205 deltas measured on llama.cpp
 * behind the ollama setting).
 */
const NEEDS_TOOLS = new Set(['ollama', 'runpod'])

export function resolveDocumentProtocol(
  provider: string,
  protocol: DocumentProtocol | undefined
): Resolved {
  if (protocol === 'tools' || protocol === 'markup') return protocol
  if (NEEDS_TOOLS.has(provider)) return 'tools'
  if (COARSE_TOOL_STREAMING.has(provider)) return 'markup'
  // Unknown or hosted frontier providers: the markup protocol is what this app
  // has always shipped and what its failure handling is built around. Tools are
  // opt-in until their streaming has been measured for that provider.
  return 'markup'
}

/** Does 'auto' pick tools here? Used by the settings UI to label the option. */
export function autoProtocolFor(provider: string): Resolved {
  return resolveDocumentProtocol(provider, 'auto')
}

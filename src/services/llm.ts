import type { ProviderConfig, LLMMessage, StreamCallbacks } from '../types/llm'
import { startRemoteGeneration, isRemoteGenerationAvailable, type RemoteJobMeta } from './remoteGeneration'

// Re-export for backward compatibility
export type { LLMMessage, StreamCallbacks }

/**
 * Streams one generation. Every call runs as a backend job
 * (scripts/server_generation.py, resumable_generation.md §5): the API server
 * holds the one implementation of each provider's wire format, and a job
 * outlives the tab. There is no in-browser transport any more — it was
 * reached only when a logged-in tab failed to start a job, and it was a
 * second copy of every provider to keep in step (removed 2026-10-10).
 */
export async function streamLLM(
  messages: LLMMessage[],
  config: ProviderConfig & {
    provider: string
    debug?: boolean
    signal?: AbortSignal
    conversationId?: string
    /** Job description echoed back by the backend, used to rejoin after a reload. */
    remoteMeta?: RemoteJobMeta
  },
  callbacks: StreamCallbacks
): Promise<void> {
  const { provider, apiKey, debug } = config

  const debugCallbacks: StreamCallbacks = {
    // Spread FIRST so optional callbacks pass through. Rebuilding this object
    // field by field silently dropped onReasoning and onAttached: the backend
    // streamed the model's thinking and the UI never saw a byte of it.
    ...callbacks,
    onChunk: (chunk: string) => {
      if (debug) console.log('[DEBUG] Incoming LLM Chunk:', chunk)
      callbacks.onChunk(chunk)
    },
    onDone: (fullText: string, usage?: { promptTokens: number; completionTokens: number; cachedPromptTokens?: number }) => {
      if (debug) console.log('[DEBUG] LLM Stream Completed. Full Response Text:', fullText, 'Usage:', usage)
      callbacks.onDone(fullText, usage)
    },
    onError: (err: Error) => {
      if (debug) console.error('[DEBUG] LLM Stream Error:', err)
      callbacks.onError(err)
    }
  }

  try {
    if (!apiKey && provider !== 'ollama') {
      throw new Error(`API key is missing for ${provider}. Please configure it in Settings.`)
    }
    if (!isRemoteGenerationAvailable()) {
      throw new Error('Not signed in: generation runs on the server. Please sign in again.')
    }
    await startRemoteGeneration(messages, config, config.remoteMeta ?? {}, debugCallbacks, config.signal)
  } catch (error) {
    callbacks.onError(error instanceof Error ? error : new Error(String(error) || 'Unknown network error'))
  }
}

/**
 * Splits an SSE response body into its `data:` payloads, keeping partial
 * lines across reads.
 *
 * Exported so the remote-generation transport (services/remoteGeneration.ts)
 * parses the wire format with the SAME code as the direct provider paths —
 * one SSE parser in the codebase, not one per transport. Throws on abort or
 * transport failure; the caller owns error reporting.
 */
export async function readSSEDataLines(
  response: Response,
  onData: (data: string, event?: string) => void,
  signal?: AbortSignal
): Promise<void> {
  const reader = response.body?.getReader()
  if (!reader) throw new Error('Response body is not readable')

  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = ''

  while (true) {
    if (signal?.aborted) {
      throw new Error('Stream aborted by user')
    }
    const { value, done } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')

    // Keep the last incomplete line in the buffer
    buffer = lines.pop() || ''

    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) continue

      if (trimmed.startsWith('event:')) {
        currentEvent = trimmed.slice(6).trim()
      } else if (trimmed.startsWith('data:')) {
        onData(trimmed.slice(5).trim(), currentEvent)
      }
    }
  }

  // Process any remaining buffer content
  if (buffer && buffer.startsWith('data:')) {
    onData(buffer.slice(5).trim(), currentEvent)
  }
}


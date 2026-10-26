/**
 * Shared interfaces for the chat orchestration hook (`useChatLLM`) and its
 * extracted helper modules under `src/hooks/chat/`.
 */

import type { AgentTurnRecord } from '../../types/chat'

/** Minimal chat-message shape needed to build history for the LLM. */
export interface HistorySourceMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  images?: string[]
  /** What an agentic turn did; summarized into one history line. */
  agent?: AgentTurnRecord
  /** grok / OpenAI (Responses API): the turn's final reasoning items, replayed ahead of its text (ChatMessage.reasoningItems). */
  reasoningItems?: unknown[]
}


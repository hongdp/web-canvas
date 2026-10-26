import type { StateCreator } from 'zustand'
import type { ChatMessage } from '../../types/chat'
import type { AppState } from '../types'

export interface ChatSlice {
  // Chat state
  messages: ChatMessage[]
  isStreaming: boolean
  /**
   * While streaming: the chapters the user may not edit — the ones the
   * assistant is writing into right now. null = all of them, which is what
   * any streamer that does not say otherwise gets (whole-book
   * batches). Cleared when streaming stops. Read it through isEditLocked.
   */
  editLockedIds: string[] | null
  setEditLockedIds: (ids: string[] | null) => void
  addMessage: (message: ChatMessage) => void
  clearChat: () => void
  setStreaming: (isStreaming: boolean) => void
  setMessages: (messages: ChatMessage[]) => void

  // Session stats & local storage tracking
  sessionInputTokens: number
  sessionOutputTokens: number
  sessionCacheHitTokens: number
  /**
   * The LAST turn's cache result. Session totals hide a single collapsed turn:
   * a prompt that lost its prefix and re-read 60k characters still leaves the
   * cumulative hit rate looking healthy. Every cache regression in this app so
   * far was invisible for exactly that reason.
   */
  lastTurnCache: {
    provider: string
    promptTokens: number
    cachedTokens: number | null
    firstTokenMs: number | null
  } | null
  setLastTurnCache: (record: {
    provider: string
    promptTokens: number
    cachedTokens: number | null
    firstTokenMs: number | null
  }) => void
  sessionCacheMissTokens: number
  addSessionTokens: (input: number, output: number, cacheHit?: number) => void
  resetSessionTokens: () => void
}

/**
 * May the user NOT edit this chapter right now? Everything is editable when
 * nothing streams; while something does, only the chapters it is writing
 * into are locked (agentic_chat_loop.md §0.4) — all of them when the
 * streamer did not say which.
 */
export function isEditLocked(
  state: Pick<ChatSlice, 'isStreaming' | 'editLockedIds'>,
  documentId: string | null | undefined
): boolean {
  if (!state.isStreaming) return false
  return state.editLockedIds === null || (!!documentId && state.editLockedIds.includes(documentId))
}

export const createChatSlice: StateCreator<AppState, [], [], ChatSlice> = (set) => ({
  // Chat state
  messages: [
    {
      id: 'welcome',
      role: 'assistant',
      content: "Hello! I'm your Web Canvas assistant. You can write your document directly in the right panel, or tell me what you want to write and I can draft it for you. How can I help you today?",
      timestamp: new Date().toISOString(),
    },
  ],
  isStreaming: false,
  editLockedIds: null,
  setEditLockedIds: (ids) => set((state) =>
    state.editLockedIds !== null && ids !== null &&
    state.editLockedIds.length === ids.length && state.editLockedIds.every((id, i) => id === ids[i])
      ? {}
      : { editLockedIds: ids }),
  addMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
  clearChat: () =>
    set(() => ({
      messages: [
        {
          id: `welcome-${Date.now()}`,
          role: 'assistant',
          content: "Chat history cleared. How can I help you with your document?",
          timestamp: new Date().toISOString(),
        },
      ],
    })),
  setStreaming: (isStreaming) => set(isStreaming ? { isStreaming } : { isStreaming, editLockedIds: null }),
  setMessages: (messages) => set({ messages }),

  // Session stats & local storage implementation
  sessionInputTokens: 0,
  sessionOutputTokens: 0,
  sessionCacheHitTokens: 0,
  lastTurnCache: null,
  setLastTurnCache: (record) => set({ lastTurnCache: record }),
  sessionCacheMissTokens: 0,
  addSessionTokens: (input, output, cacheHit = 0) => set((state) => {
    // Clamp defensively: providers report input/cached tokens in different
    // shapes, and a mis-mapped extractor must never corrupt the counters
    // with a negative miss.
    const hit = Math.min(cacheHit, input)
    const miss = Math.max(0, input - hit)
    return {
      sessionInputTokens: state.sessionInputTokens + input,
      sessionOutputTokens: state.sessionOutputTokens + output,
      sessionCacheHitTokens: state.sessionCacheHitTokens + hit,
      sessionCacheMissTokens: state.sessionCacheMissTokens + miss,
    }
  }),
  resetSessionTokens: () => set({
    sessionInputTokens: 0,
    sessionOutputTokens: 0,
    sessionCacheHitTokens: 0,
    sessionCacheMissTokens: 0
  }),
})

import type { StateCreator } from 'zustand'
import type { Editor } from '@tiptap/react'
import type { AppState } from '../types'
import { localStorage } from '../persistence'
import { setCookie, loadSavedSidebarOpen } from '../settingsPersistence'

export interface UiSlice {
  isSidebarOpen: boolean
  toggleSidebar: () => void


  /**
   * Tail of the reasoning the model is streaming right now, or ''.
   * Transient and throttled by the writer: a reasoning model can spend a
   * minute thinking before its first visible token, and showing that beats
   * showing a spinner. Never persisted, never part of the message.
   */
  streamingReasoning: string
  setStreamingReasoning: (text: string) => void

  // Selection & editor integration for inline diff review
  selectedText: string
  setSelectedText: (text: string) => void
  activeEditor: Editor | null
  setActiveEditor: (editor: Editor | null) => void

  /**
   * The Polish button's request (agentic_chat_loop.md D9). The button lives
   * in the canvas header and the turn machinery in the chat panel's hook, so
   * the request crosses through the store; the hook clears it when it runs.
   */
  polishRequest: { documentId: string; nonce: number } | null
  requestPolish: (documentId: string) => void
  clearPolishRequest: () => void
}

export const createUiSlice: StateCreator<AppState, [], [], UiSlice> = (set) => ({
  isSidebarOpen: loadSavedSidebarOpen(),

  streamingReasoning: '',
  setStreamingReasoning: (text) => set({ streamingReasoning: text }),

  polishRequest: null,
  requestPolish: (documentId) => set({ polishRequest: { documentId, nonce: Date.now() } }),
  clearPolishRequest: () => set({ polishRequest: null }),

  toggleSidebar: () => {
    set((state) => {
      const isOpen = !state.isSidebarOpen
      localStorage.setItem('web_canvas_sidebar_open', String(isOpen))
      setCookie('__Secure-web_canvas_sidebar_open', String(isOpen))
      return { isSidebarOpen: isOpen }
    })
  },


  // Selection & editor state implementation
  selectedText: '',
  setSelectedText: (text) => set({ selectedText: text }),
  activeEditor: null,
  setActiveEditor: (editor) => set({ activeEditor: editor }),
})

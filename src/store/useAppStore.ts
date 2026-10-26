/**
 * Combined app store. The state lives in slice creators under ./slices/*;
 * this module combines them into the single Zustand store, wires the
 * beforeunload flush and the debounced auto-save subscription, and
 * re-exports the store's historical public API so import sites outside
 * src/store/ stay unchanged.
 */
import { create } from 'zustand'

// Re-export shared types for backward compatibility
export type { LLMProvider, ImageGenProvider, ImageGenConfig, GeminiSafetySetting, ProviderConfig, SystemPromptTemplate } from '../types/llm'
export { PROVIDER_MODELS } from '../types/llm'
export type { ChatMessage } from '../types/chat'
export type { DocumentVersion, CanvasDocument } from '../types/document'

// Re-export constants and the server bootstrap so existing import sites
// (e.g. main.tsx, SettingsModal) keep working unchanged.
export { DEFAULT_SYSTEM_PROMPTS } from './defaults'
export { initializeStoreFromServer } from './serverSync'
export { isEditLocked } from './slices/chatSlice'
export type { DocumentWriteOptions } from './slices/documentsSlice'

import type { LLMProvider, ProviderConfig, SystemPromptTemplate } from '../types/llm'
import type { ChatMessage } from '../types/chat'
import type { DocumentVersion, CanvasDocument } from '../types/document'

import { flushPendingDocumentSave } from './persistence'
import type { AppState } from './types'
import { createDocumentsSlice } from './slices/documentsSlice'
import { createVersionsSlice } from './slices/versionsSlice'
import { createChatSlice } from './slices/chatSlice'
import { createSettingsSlice } from './slices/settingsSlice'
import { createUiSlice } from './slices/uiSlice'
import { createBooksSlice } from './slices/booksSlice'
import { createAuthSlice } from './slices/authSlice'
import { createAttachmentsSlice } from './slices/attachmentsSlice'
import { getIsInitialized, schedulePendingSave } from './syncRuntime'

if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => {
    const state = useAppStore.getState()
    flushPendingDocumentSave(state.documents)
  })
}

export const useAppStore = create<AppState>()((...a) => ({
  ...createDocumentsSlice(...a),
  ...createVersionsSlice(...a),
  ...createChatSlice(...a),
  ...createSettingsSlice(...a),
  ...createUiSlice(...a),
  ...createBooksSlice(...a),
  ...createAuthSlice(...a),
  ...createAttachmentsSlice(...a),
}))

interface SyncableState {
  documents: CanvasDocument[]
  versions: DocumentVersion[]
  bookTitle: string
  activeDocumentId: string
  activeProvider: LLMProvider
  providerConfigs: Record<LLMProvider, ProviderConfig>
  customSystemPrompts: SystemPromptTemplate[]
  activeSystemPromptId: string
  theme: 'dark' | 'light'
  messages: ChatMessage[]
  debugMode: boolean
}

let lastSyncableState: SyncableState = {
  documents: useAppStore.getState().documents,
  versions: useAppStore.getState().versions,
  bookTitle: useAppStore.getState().bookTitle,
  activeDocumentId: useAppStore.getState().activeDocumentId,
  activeProvider: useAppStore.getState().activeProvider,
  providerConfigs: useAppStore.getState().providerConfigs,
  customSystemPrompts: useAppStore.getState().customSystemPrompts,
  activeSystemPromptId: useAppStore.getState().activeSystemPromptId,
  theme: useAppStore.getState().theme,
  messages: useAppStore.getState().messages,
  debugMode: useAppStore.getState().debugMode,
}

const hasSyncableChanges = (state: AppState): boolean => {
  return (
    state.documents !== lastSyncableState.documents ||
    state.versions !== lastSyncableState.versions ||
    state.bookTitle !== lastSyncableState.bookTitle ||
    state.activeDocumentId !== lastSyncableState.activeDocumentId ||
    state.activeProvider !== lastSyncableState.activeProvider ||
    state.providerConfigs !== lastSyncableState.providerConfigs ||
    state.customSystemPrompts !== lastSyncableState.customSystemPrompts ||
    state.activeSystemPromptId !== lastSyncableState.activeSystemPromptId ||
    state.theme !== lastSyncableState.theme ||
    state.messages !== lastSyncableState.messages ||
    state.debugMode !== lastSyncableState.debugMode
  )
}

useAppStore.subscribe((state) => {
  if (!getIsInitialized()) {
    lastSyncableState = {
      documents: state.documents,
      versions: state.versions,
      bookTitle: state.bookTitle,
      activeDocumentId: state.activeDocumentId,
      activeProvider: state.activeProvider,
      providerConfigs: state.providerConfigs,
      customSystemPrompts: state.customSystemPrompts,
      activeSystemPromptId: state.activeSystemPromptId,
      theme: state.theme,
      messages: state.messages,
      debugMode: state.debugMode,
    }
    return
  }
  if (!state.user) {
    if (useAppStore.getState().serverSaveStatus !== 'local-only') {
      useAppStore.setState({ serverSaveStatus: 'local-only' })
    }
    return // Don't auto-save if user is not logged in
  }

  if (!hasSyncableChanges(state)) {
    return // No syncable data changed (e.g. only serverSaveStatus, activeEditor, or selectedText changed)
  }

  lastSyncableState = {
    documents: state.documents,
    versions: state.versions,
    bookTitle: state.bookTitle,
    activeDocumentId: state.activeDocumentId,
    activeProvider: state.activeProvider,
    providerConfigs: state.providerConfigs,
    customSystemPrompts: state.customSystemPrompts,
    activeSystemPromptId: state.activeSystemPromptId,
    theme: state.theme,
    messages: state.messages,
    debugMode: state.debugMode,
  }

  if (useAppStore.getState().serverSaveStatus !== 'saving') {
    useAppStore.setState({ serverSaveStatus: 'saving' })
  }

  schedulePendingSave(async () => {
    await useAppStore.getState().syncToServer()
  }, 3000) // 3-second debounce to accumulate edits and reduce server communication
})

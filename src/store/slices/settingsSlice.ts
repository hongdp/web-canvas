import type { StateCreator } from 'zustand'
import type { PolishPrompt } from '../../types/llm'
import type { LLMProvider, ImageGenConfig, ProviderConfig, SystemPromptTemplate } from '../../types/llm'
import type { AppState } from '../types'
import { localStorage } from '../persistence'
import {
  getCookie,
  setCookie,
  saveSystemPromptsToCookie,
  loadSavedSystemPromptsData,
  saveConfigsToCookie,
  loadSavedConfigs,
  loadSavedProvider,
  loadSavedTheme,
  loadSavedLanguage,
  loadSavedDebugMode,
  loadSavedPolishPrompt,
  savePolishPrompt,
  loadSavedImageGenConfig,
} from '../settingsPersistence'

export interface SettingsSlice {
  // Theme & Language state
  theme: 'dark' | 'light'
  setTheme: (theme: 'dark' | 'light') => void
  language: 'en' | 'zh'
  setLanguage: (lang: 'en' | 'zh') => void

  // LLM configurations
  activeProvider: LLMProvider
  providerConfigs: Record<LLMProvider, ProviderConfig>
  setProvider: (provider: LLMProvider) => void
  updateProviderConfig: (provider: LLMProvider, config: Partial<ProviderConfig>) => void
  /**
   * Discovered model lists by provider (hooks/useModelFetcher): the key's own
   * list for a hosted provider, the endpoint's /models for a local one.
   * Absent or empty = offer the shipped list (utils/providers modelsFor).
   */
  availableModels: Partial<Record<LLMProvider, string[]>>
  setAvailableModels: (provider: LLMProvider, models: string[]) => void
  /**
   * Context window per model id, as reported by the endpoint itself
   * (llama.cpp states `n_ctx` on /v1/models). Only the server knows what it
   * was started with — this endpoint went from 32K to 262144 in one day — so
   * a reported value always beats the table in utils/contextWindow.ts.
   */
  discoveredContextWindows: Record<string, number>
  setDiscoveredContextWindows: (windows: Record<string, number>) => void
  debugMode: boolean
  setDebugMode: (enabled: boolean) => void

  /** The polish pass's prompt (D9), user-editable; persisted locally. */
  polishPrompt: PolishPrompt
  setPolishPrompt: (prompt: PolishPrompt) => void

  // Image generation config
  imageGenConfig: ImageGenConfig
  updateImageGenConfig: (updates: Partial<ImageGenConfig>) => void

  // System prompts list
  customSystemPrompts: SystemPromptTemplate[]
  activeSystemPromptId: string
  setActiveSystemPromptId: (id: string) => void
  addSystemPrompt: (name?: string, content?: string) => string
  updateSystemPrompt: (id: string, updates: Partial<SystemPromptTemplate>) => void
  deleteSystemPrompt: (id: string) => void
}

export const createSettingsSlice: StateCreator<AppState, [], [], SettingsSlice> = (set) => {
  const initialPromptsData = loadSavedSystemPromptsData()
  if (!localStorage.getItem('web_canvas_system_prompts_backup') && !getCookie('__Secure-web_canvas_system_prompts')) {
    saveSystemPromptsToCookie(initialPromptsData.prompts, initialPromptsData.activePromptId)
  }
  const isEnvDebug = import.meta.env.VITE_DEBUG === 'true' || import.meta.env.MODE === 'debug'
  const initialDebugMode = isEnvDebug || loadSavedDebugMode() || (localStorage.getItem('web_canvas_debug_mode') === null && getCookie('__Secure-web_canvas_debug_mode') === '' && import.meta.env.DEV)

  return {
    // Theme & Language state
    theme: loadSavedTheme(),
    setTheme: (theme) => {
      localStorage.setItem('web_canvas_theme', theme)
      setCookie('__Secure-web_canvas_theme', theme)
      set({ theme })
    },
    language: loadSavedLanguage(),
    setLanguage: (language) => {
      localStorage.setItem('web_canvas_language', language)
      setCookie('__Secure-web_canvas_language', language)
      set({ language })
    },

    // LLM configurations
    activeProvider: loadSavedProvider(),
    providerConfigs: loadSavedConfigs(),
    availableModels: {},
    setAvailableModels: (provider, models) => {
      set(state => ({ availableModels: { ...state.availableModels, [provider]: models } }))
    },
    setProvider: (provider) => {
      localStorage.setItem('web_canvas_active_provider', provider)
      setCookie('__Secure-web_canvas_active_provider', provider)
      set({ activeProvider: provider })
    },

    discoveredContextWindows: {},
    setDiscoveredContextWindows: (windows) => {
      set(state => ({ discoveredContextWindows: { ...state.discoveredContextWindows, ...windows } }))
    },
    updateProviderConfig: (provider, newConfig) => {
      set((state) => {
        const updatedConfigs = {
          ...state.providerConfigs,
          [provider]: {
            ...state.providerConfigs[provider],
            ...newConfig,
          },
        }
        saveConfigsToCookie(updatedConfigs)
        return { providerConfigs: updatedConfigs }
      })
    },
    debugMode: initialDebugMode,
    setDebugMode: (enabled) => {
      localStorage.setItem('web_canvas_debug_mode', String(enabled))
      setCookie('__Secure-web_canvas_debug_mode', String(enabled))
      set({ debugMode: enabled })
    },

    polishPrompt: loadSavedPolishPrompt(),
    setPolishPrompt: (prompt) => {
      savePolishPrompt(prompt)
      set({ polishPrompt: prompt })
    },

    // Image generation config
    imageGenConfig: loadSavedImageGenConfig(),
    updateImageGenConfig: (updates) => {
      set((state) => {
        const newConfig = { ...state.imageGenConfig, ...updates }
        localStorage.setItem('web_canvas_image_gen_config', JSON.stringify(newConfig))
        return { imageGenConfig: newConfig }
      })
    },

    // System prompts state
    customSystemPrompts: initialPromptsData.prompts,
    activeSystemPromptId: initialPromptsData.activePromptId,
    setActiveSystemPromptId: (id) => {
      set((state) => {
        saveSystemPromptsToCookie(state.customSystemPrompts, id)
        return { activeSystemPromptId: id }
      })
    },
    addSystemPrompt: (name = 'New Preset', content = '') => {
      const newPrompt: SystemPromptTemplate = {
        id: `prompt-${Date.now()}`,
        name,
        content
      }
      const promptId = newPrompt.id
      set((state) => {
        const updated = [...state.customSystemPrompts, newPrompt]
        saveSystemPromptsToCookie(updated, state.activeSystemPromptId)
        return { customSystemPrompts: updated }
      })
      return promptId
    },
    updateSystemPrompt: (id, updates) => {
      set((state) => {
        const updated = state.customSystemPrompts.map((p) => {
          if (p.id === id) {
            return { ...p, ...updates }
          }
          return p
        })
        saveSystemPromptsToCookie(updated, state.activeSystemPromptId)
        return { customSystemPrompts: updated }
      })
    },
    deleteSystemPrompt: (id) => {
      set((state) => {
        const updated = state.customSystemPrompts.filter((p) => p.id !== id)
        let activeId = state.activeSystemPromptId
        if (state.activeSystemPromptId === id) {
          activeId = updated[0]?.id || 'prompt-none'
        }
        saveSystemPromptsToCookie(updated, activeId)
        return {
          customSystemPrompts: updated,
          activeSystemPromptId: activeId
        }
      })
    },
  }
}

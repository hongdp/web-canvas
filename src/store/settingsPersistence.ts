/**
 * Versioned settings persistence: cookie/localStorage load-save helpers and
 * the migration machinery for provider configs and system prompt presets.
 * These are pure-ish module functions (they only touch document.cookie and
 * the localStorage wrapper) shared by the store slices and server sync.
 */
import type { LLMProvider, ImageGenConfig, ProviderConfig, SystemPromptTemplate } from '../types/llm'
import type { CanvasDocument } from '../types/document'
import { localStorage } from './persistence'
import { DEFAULT_CONFIGS, DEFAULT_SYSTEM_PROMPTS, DEFAULT_IMAGE_GEN_CONFIG, DEFAULT_POLISH_PROMPT } from './defaults'
import { isProviderId } from '../utils/providers'
import type { PolishPrompt } from '../types/llm'

// TODO(security): Implement a Backend-for-Frontend (BFF) layer to store API keys
// in server-side HttpOnly cookies instead of exposing them to client-side JS.
export const getCookie = (name: string): string => {
  if (typeof document === 'undefined') return ''
  const nameEQ = name + '='
  const ca = document.cookie.split(';')
  for (let i = 0; i < ca.length; i++) {
    let c = ca[i]
    while (c.charAt(0) === ' ') c = c.substring(1, c.length)
    if (c.indexOf(nameEQ) === 0) return decodeURIComponent(c.substring(nameEQ.length, c.length))
  }
  return ''
}

export const setCookie = (name: string, value: string, days = 365) => {
  if (typeof document === 'undefined') return
  let expires = ''
  if (days) {
    const date = new Date()
    date.setTime(date.getTime() + days * 24 * 60 * 60 * 1000)
    expires = '; expires=' + date.toUTCString()
  }
  document.cookie = name + '=' + encodeURIComponent(value || '') + expires + '; path=/; SameSite=Lax; Secure'
}

const CURRENT_SETTINGS_VERSION = 2

interface VersionedCookieData<T> {
  version: number
  data: T
}

const CURRENT_PROMPTS_VERSION = 1

export const saveSystemPromptsToCookie = (prompts: SystemPromptTemplate[], activePromptId: string) => {
  try {
    localStorage.setItem('web_canvas_system_prompts_backup', JSON.stringify({
      version: CURRENT_PROMPTS_VERSION,
      prompts,
      activePromptId
    }))
  } catch (e) {
    console.error('Failed to save prompts backup to localStorage', e)
  }

  // Clear the old cookie if it exists to clean up bloated request headers
  if (getCookie('__Secure-web_canvas_system_prompts')) {
    setCookie('__Secure-web_canvas_system_prompts', '', -1)
  }
}

interface SavedPromptsEnvelope {
  version?: number
  prompts?: SystemPromptTemplate[]
  activePromptId?: string
}

export const loadSavedSystemPromptsData = (): { prompts: SystemPromptTemplate[]; activePromptId: string } => {
  // 1. Try loading from localStorage backup first (prioritized)
  const backup = localStorage.getItem('web_canvas_system_prompts_backup')
  if (backup) {
    try {
      const parsed = JSON.parse(backup) as SavedPromptsEnvelope
      if (parsed && typeof parsed === 'object' && parsed.version === CURRENT_PROMPTS_VERSION) {
        return {
          prompts: parsed.prompts || DEFAULT_SYSTEM_PROMPTS,
          activePromptId: parsed.activePromptId || 'prompt-none'
        }
      }
    } catch (e) {
      console.error('Failed to parse system prompts backup from localStorage', e)
    }
  }

  // 2. Fallback to cookie
  const saved = getCookie('__Secure-web_canvas_system_prompts')
  if (saved) {
    try {
      const parsed = JSON.parse(saved) as SavedPromptsEnvelope
      // Clean up the cookie since we've migrated
      setCookie('__Secure-web_canvas_system_prompts', '', -1)
      if (parsed && typeof parsed === 'object' && parsed.version === CURRENT_PROMPTS_VERSION) {
        const savedPrompts = parsed.prompts || []
        const mergedPrompts = [...DEFAULT_SYSTEM_PROMPTS]
        savedPrompts.forEach((sp: SystemPromptTemplate) => {
          const defaultIndex = mergedPrompts.findIndex(d => d.id === sp.id)
          if (defaultIndex !== -1) {
            mergedPrompts[defaultIndex] = sp
          } else {
            mergedPrompts.push(sp)
          }
        })
        const activePromptId = parsed.activePromptId || 'prompt-none'
        // Save to localStorage immediately so it's backed up
        try {
          localStorage.setItem('web_canvas_system_prompts_backup', JSON.stringify({
            version: CURRENT_PROMPTS_VERSION,
            prompts: mergedPrompts,
            activePromptId
          }))
        } catch (e) {
          console.error('Failed to save prompts backup to localStorage', e)
        }
        return {
          prompts: mergedPrompts,
          activePromptId
        }
      }
    } catch (e) {
      console.error('Failed to parse saved system prompts from cookie', e)
    }
  }

  return {
    prompts: DEFAULT_SYSTEM_PROMPTS,
    activePromptId: 'prompt-none'
  }
}

export const saveConfigsToCookie = (configs: Record<LLMProvider, ProviderConfig>) => {
  const envelope: VersionedCookieData<Record<LLMProvider, ProviderConfig>> = {
    version: CURRENT_SETTINGS_VERSION,
    data: configs,
  }
  const jsonStr = JSON.stringify(envelope)

  // 1. Save to localStorage
  try {
    localStorage.setItem('web_canvas_providers_backup', jsonStr)
  } catch (e) {
    console.error('Failed to save configs backup to localStorage', e)
  }

  // 2. Clear old cookie
  if (getCookie('__Secure-web_canvas_providers')) {
    setCookie('__Secure-web_canvas_providers', '', -1)
  }
}

export const migrateProvidersConfig = (savedString: string): Record<LLMProvider, ProviderConfig> => {
  try {
    const parsed = JSON.parse(savedString)

    // Case 1: Legacy unversioned structure (direct Record<LLMProvider, ProviderConfig>)
    if (!parsed || typeof parsed !== 'object' || !('version' in parsed)) {
      console.log('Migrating legacy (v0) LLM configs to v2')
      const migratedData = { ...DEFAULT_CONFIGS }
      const rawData = parsed as Partial<Record<LLMProvider, ProviderConfig & { systemPrompt?: string }>> | null
      let legacyPromptText = ''
      for (const p of Object.keys(DEFAULT_CONFIGS) as LLMProvider[]) {
        const rawConfig = rawData?.[p]
        if (rawConfig) {
          migratedData[p] = {
            ...DEFAULT_CONFIGS[p],
            ...rawConfig,
          }
          if (rawConfig.systemPrompt && !legacyPromptText) {
            legacyPromptText = rawConfig.systemPrompt
          }
          // Clean up legacy prompt field
          delete (migratedData[p] as ProviderConfig & { systemPrompt?: string }).systemPrompt
        }
      }

      if (legacyPromptText && legacyPromptText.trim()) {
        const importedId = `prompt-imported-${Date.now()}`
        const importedPrompt: SystemPromptTemplate = {
          id: importedId,
          name: 'Imported Preset',
          content: legacyPromptText,
        }
        saveSystemPromptsToCookie([...DEFAULT_SYSTEM_PROMPTS, importedPrompt], importedId)
      }

      // Save versioned localStorage immediately
      saveConfigsToCookie(migratedData)
      return migratedData
    }

    // Case 2: Versioned structure
    const versioned = parsed as VersionedCookieData<Record<LLMProvider, ProviderConfig>>
    const currentData = versioned.data
    let version = versioned.version

    if (version === 1) {
      console.log('Migrating version 1 LLM configs to v2')
      let legacyPromptText = ''
      for (const p of Object.keys(DEFAULT_CONFIGS) as LLMProvider[]) {
        const config = currentData[p] as (ProviderConfig & { systemPrompt?: string }) | undefined
        if (config && config.systemPrompt) {
          legacyPromptText = config.systemPrompt
          // Clean up legacy prompt field
          delete config.systemPrompt
        }
      }
      if (legacyPromptText && legacyPromptText.trim()) {
        const importedId = `prompt-imported-${Date.now()}`
        const importedPrompt: SystemPromptTemplate = {
          id: importedId,
          name: 'Imported Preset',
          content: legacyPromptText,
        }
        saveSystemPromptsToCookie([...DEFAULT_SYSTEM_PROMPTS, importedPrompt], importedId)
      }
      version = 2
    }

    // Save back if version was updated during migration
    if (version !== versioned.version) {
      saveConfigsToCookie(currentData)
    }

    // Ensure all required properties for all providers exist in returning data
    const merged = { ...DEFAULT_CONFIGS }
    for (const p of Object.keys(DEFAULT_CONFIGS) as LLMProvider[]) {
      if (currentData[p]) {
        merged[p] = {
          ...DEFAULT_CONFIGS[p],
          ...currentData[p],
        }
      }
    }
    return merged
  } catch (e) {
    console.error('Failed to parse and migrate saved configs, resetting to default', e)
    return DEFAULT_CONFIGS
  }
}

// Load initial configs and documents from cookies/localStorage
export const loadSavedConfigs = (): Record<LLMProvider, ProviderConfig> => {
  const backup = localStorage.getItem('web_canvas_providers_backup')
  if (backup) {
    return migrateProvidersConfig(backup)
  }
  const saved = getCookie('__Secure-web_canvas_providers')
  if (saved) {
    const configs = migrateProvidersConfig(saved)
    setCookie('__Secure-web_canvas_providers', '', -1)
    // Save to localStorage immediately so it's backed up
    try {
      localStorage.setItem('web_canvas_providers_backup', JSON.stringify({
        version: CURRENT_SETTINGS_VERSION,
        data: configs
      }))
    } catch (e) {
      console.error('Failed to save migrated configs backup to localStorage', e)
    }
    return configs
  }
  return DEFAULT_CONFIGS
}

export const mergeProviderConfigs = (
  current: Record<LLMProvider, ProviderConfig>,
  incoming: Record<LLMProvider, ProviderConfig>
): Record<LLMProvider, ProviderConfig> => {
  const merged = { ...current }
  for (const provider of Object.keys(current) as LLMProvider[]) {
    const currentConfig = current[provider]
    const incomingConfig = incoming[provider]

    const apiKey = (incomingConfig && incomingConfig.apiKey && incomingConfig.apiKey.trim() !== '')
      ? incomingConfig.apiKey
      : ((currentConfig && currentConfig.apiKey) || '')

    merged[provider] = {
      ...DEFAULT_CONFIGS[provider],
      ...currentConfig,
      ...incomingConfig,
      apiKey
    }
  }
  return merged
}

export const loadSavedProvider = (): LLMProvider => {
  const saved = localStorage.getItem('web_canvas_active_provider')
  if (isProviderId(saved)) return saved
  const cookieSaved = getCookie('__Secure-web_canvas_active_provider')
  if (isProviderId(cookieSaved)) {
    localStorage.setItem('web_canvas_active_provider', cookieSaved)
    return cookieSaved
  }
  return 'gemini'
}

export const loadSavedTheme = (): 'dark' | 'light' => {
  const saved = localStorage.getItem('web_canvas_theme')
  if (saved === 'dark' || saved === 'light') return saved
  const cookieSaved = getCookie('__Secure-web_canvas_theme')
  if (cookieSaved === 'dark' || cookieSaved === 'light') {
    localStorage.setItem('web_canvas_theme', cookieSaved)
    return cookieSaved
  }
  return 'dark'
}

export const loadSavedLanguage = (): 'en' | 'zh' => {
  const saved = localStorage.getItem('web_canvas_language')
  if (saved === 'en' || saved === 'zh') return saved
  const cookieSaved = getCookie('__Secure-web_canvas_language')
  if (cookieSaved === 'en' || cookieSaved === 'zh') {
    localStorage.setItem('web_canvas_language', cookieSaved)
    return cookieSaved
  }
  // Auto-detect browser language if possible
  if (typeof navigator !== 'undefined' && navigator.language.startsWith('zh')) {
    return 'zh'
  }
  return 'en'
}

export const loadSavedSidebarOpen = (): boolean => {
  const saved = localStorage.getItem('web_canvas_sidebar_open')
  if (saved !== null) return saved !== 'false'
  const cookieSaved = getCookie('__Secure-web_canvas_sidebar_open')
  if (cookieSaved !== '') {
    localStorage.setItem('web_canvas_sidebar_open', cookieSaved)
    return cookieSaved !== 'false'
  }
  return true
}

export const loadSavedDebugMode = (): boolean => {
  const saved = localStorage.getItem('web_canvas_debug_mode')
  if (saved !== null) return saved === 'true'
  const cookieSaved = getCookie('__Secure-web_canvas_debug_mode')
  if (cookieSaved !== '') {
    localStorage.setItem('web_canvas_debug_mode', cookieSaved)
    return cookieSaved === 'true'
  }
  return false
}

export const loadSavedImageGenConfig = (): ImageGenConfig => {
  const saved = localStorage.getItem('web_canvas_image_gen_config')
  if (saved) {
    try {
      return { ...DEFAULT_IMAGE_GEN_CONFIG, ...JSON.parse(saved) }
    } catch (e) {
      console.error('Failed to parse saved image gen config', e)
    }
  }
  return DEFAULT_IMAGE_GEN_CONFIG
}



export const loadSavedActiveDocId = (docs: CanvasDocument[]): string => {
  const saved = localStorage.getItem('web_canvas_active_document_id')
  if (saved && docs.some((d) => d.id === saved)) {
    return saved
  }
  return docs[0]?.id || ''
}

/**
 * The polish prompt (agentic_chat_loop.md D9), user-editable. Stored as a
 * versioned envelope so its shape can evolve: v1 = { system, template }.
 * Anything unreadable falls back to the default rather than to nothing — a
 * blank polish prompt would rewrite chunks with no instructions at all.
 */
export const POLISH_PROMPT_KEY = 'web_canvas_polish_prompt'
export const POLISH_PROMPT_VERSION = 1

export const loadSavedPolishPrompt = (): PolishPrompt => {
  try {
    const raw = localStorage.getItem(POLISH_PROMPT_KEY)
    if (!raw) return { ...DEFAULT_POLISH_PROMPT }
    const parsed = JSON.parse(raw) as { version?: number; data?: Partial<PolishPrompt> }
    const data = parsed && parsed.version === POLISH_PROMPT_VERSION ? parsed.data : undefined
    return {
      system: typeof data?.system === 'string' ? data.system : DEFAULT_POLISH_PROMPT.system,
      template: typeof data?.template === 'string' && data.template.includes('{part}') ? data.template : DEFAULT_POLISH_PROMPT.template
    }
  } catch {
    return { ...DEFAULT_POLISH_PROMPT }
  }
}

export const savePolishPrompt = (prompt: PolishPrompt): void => {
  try {
    localStorage.setItem(POLISH_PROMPT_KEY, JSON.stringify({ version: POLISH_PROMPT_VERSION, data: prompt }))
  } catch (e) {
    console.error('Failed to save the polish prompt', e)
  }
}

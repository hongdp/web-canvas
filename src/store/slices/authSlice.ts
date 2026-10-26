import type { StateCreator } from 'zustand'
import type { AppState } from '../types'
import { localStorage, clearWorkspaceCache, getCachedWorkspaceOwner, setCachedWorkspaceOwner } from '../persistence'
import { setCookie } from '../settingsPersistence'
import { DEFAULT_CONFIGS, DEFAULT_SYSTEM_PROMPTS, MOCK_DOCUMENTS } from '../defaults'
import { clearPendingSave } from '../syncRuntime'
// Benign import cycle: serverSync only references useAppStore inside function
// bodies, and this slice only calls initializeStoreFromServer at login time.
import { initializeStoreFromServer } from '../serverSync'
// Benign import cycle: this module only references useAppStore inside action
// bodies, which run long after both modules have finished evaluating.
import { useAppStore } from '../useAppStore'

export interface AuthSlice {
  // Auth state
  user: { username: string } | null
  csrfToken: string | null
  setUser: (user: { username: string } | null) => void
  setCsrfToken: (token: string | null) => void
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  checkSession: () => Promise<void>
  /**
   * Drop the browser-local workspace when the signed-in account changes.
   * The document/version/chat caches belong to the BROWSER, so without this
   * the next account inherits the previous one's workspace — and the
   * auto-save writes that content into the new account.
   */
  resetWorkspaceForUser: (username: string | null) => Promise<void>
}

export const createAuthSlice: StateCreator<AppState, [], [], AuthSlice> = (set) => ({
  // Auth implementation
  user: null,
  csrfToken: null,
  setUser: (user) => set({ user }),

  resetWorkspaceForUser: async (username) => {
    if (getCachedWorkspaceOwner() === username) return // same account: keep the cache
    clearPendingSave() // a queued save must not land in the new account
    await clearWorkspaceCache()
    set({
      documents: MOCK_DOCUMENTS,
      activeDocumentId: MOCK_DOCUMENTS[0].id,
      versions: [],
      messages: [],
      bookTitle: 'Untitled Book',
      activeBookId: 'default',
      availableBooks: [],
      lastSyncedAt: null,
      lastSeenServerUpdatedAt: null,
      serverSaveStatus: 'saved'
    })
    setCachedWorkspaceOwner(username)
  },
  setCsrfToken: (token) => set({ csrfToken: token }),
  login: async (username, password) => {
    const state = useAppStore.getState()
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': state.csrfToken || ''
      },
      body: JSON.stringify({ username, password })
    })
    if (!res.ok) {
      let errMsg = 'Login failed.'
      try {
        const contentType = res.headers.get('content-type')
        if (contentType && contentType.includes('application/json')) {
          const errData = await res.json()
          errMsg = errData.error || errData.detail || errMsg
        } else {
          const text = await res.text()
          if (text && text.trim().length > 0 && text.length < 200) {
            errMsg = text.trim()
          } else {
            errMsg = `Server error: HTTP ${res.status}`
          }
        }
      } catch {
        errMsg = `Server error: HTTP ${res.status}`
      }
      throw new Error(errMsg)
    }
    const data = await res.json()
    set({ user: { username: data.username }, csrfToken: data.csrfToken || state.csrfToken })

    // Before ANY sync: if this browser last held another account's workspace,
    // drop it. Otherwise the in-memory documents (and the localStorage active
    // book id) belong to the previous user and get written into this account.
    await useAppStore.getState().resetWorkspaceForUser(data.username)

    // Perform sync verification for this logged in user
    await initializeStoreFromServer(true)
    // Fetch available books
    await useAppStore.getState().fetchAvailableBooks()
  },
  register: async (username, password) => {
    const state = useAppStore.getState()
    const res = await fetch('/api/auth/register', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': state.csrfToken || ''
      },
      body: JSON.stringify({ username, password })
    })
    if (!res.ok) {
      let errMsg = 'Registration failed.'
      try {
        const contentType = res.headers.get('content-type')
        if (contentType && contentType.includes('application/json')) {
          const errData = await res.json()
          errMsg = errData.error || errData.detail || errMsg
        } else {
          const text = await res.text()
          if (text && text.trim().length > 0 && text.length < 200) {
            errMsg = text.trim()
          } else {
            errMsg = `Server error: HTTP ${res.status}`
          }
        }
      } catch {
        errMsg = `Server error: HTTP ${res.status}`
      }
      throw new Error(errMsg)
    }
  },
  logout: async () => {
    const state = useAppStore.getState()

    // Flush any pending saves to prevent data loss
    clearPendingSave()
    if (state.serverSaveStatus === 'saving') {
      try {
        await state.syncToServer()
      } catch (e) {
        console.error('Failed to flush save before logout', e)
      }
    }

    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        headers: {
          'X-CSRF-Token': state.csrfToken || ''
        }
      })
    } catch (e) {
      console.error('Logout request failed', e)
    }
    // Clear the IndexedDB cache to avoid leaving PII behind.
    // clearWorkspaceCache (not two db.remove calls) because documents are
    // stored as one record PER CHAPTER since the v3 layout — removing only the
    // index left every chapter's text sitting in IndexedDB after logout.
    await clearWorkspaceCache()
    setCachedWorkspaceOwner(null)
    localStorage.removeItem('web_canvas_system_prompts_backup')
    localStorage.removeItem('web_canvas_providers_backup')
    localStorage.removeItem('web_canvas_theme')
    localStorage.removeItem('web_canvas_sidebar_open')
    localStorage.removeItem('web_canvas_debug_mode')
    localStorage.removeItem('web_canvas_active_provider')

    // Clear cookies that might have been set
    setCookie('__Secure-web_canvas_theme', '', -1)
    setCookie('__Secure-web_canvas_sidebar_open', '', -1)
    setCookie('__Secure-web_canvas_debug_mode', '', -1)
    setCookie('__Secure-web_canvas_active_provider', '', -1)
    setCookie('__Secure-web_canvas_system_prompts', '', -1)
    setCookie('__Secure-web_canvas_providers', '', -1)

    set({
      user: null,
      activeBookId: 'default',
      availableBooks: [],
      lastSyncedAt: null,
      customSystemPrompts: DEFAULT_SYSTEM_PROMPTS,
      activeSystemPromptId: 'prompt-none',
      providerConfigs: DEFAULT_CONFIGS,
      activeProvider: 'gemini',
      theme: 'dark',
      isSidebarOpen: true,
      debugMode: false,
      documents: MOCK_DOCUMENTS,
      activeDocumentId: 'doc-1',
      versions: [],
      messages: [
        {
          id: 'welcome',
          role: 'assistant',
          content: "Hello! I'm your Web Canvas assistant. You can write your document directly in the right panel, or tell me what you want to write and I can draft it for you. How can I help you today?",
          timestamp: new Date().toISOString(),
        },
      ]
    })
  },
  checkSession: async () => {
    try {
      const res = await fetch('/api/auth/session')
      if (res.ok) {
        const data = await res.json()
        set({ csrfToken: data.csrfToken })
        if (data.loggedIn) {
          set({ user: { username: data.username } })
          // A restored session can belong to a different account than the one
          // whose workspace this browser has cached (another tab logged in, or
          // the session outlived a logout).
          await useAppStore.getState().resetWorkspaceForUser(data.username)
        } else {
          set({ user: null })
        }
      }
    } catch (e) {
      console.error('Failed to check session', e)
    }
  },
})

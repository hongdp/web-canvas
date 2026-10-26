import { useEffect } from 'react'
import { useAppStore } from '../store/useAppStore'
import type { LLMProvider } from '../types/llm'
import { PROVIDERS, PROVIDER_IDS } from '../utils/providers'

/**
 * Model discovery for every provider, driven by the registry
 * (utils/providers `discovery`):
 *
 *  - `official`: the key's own list, through the backend (/api/models) —
 *    Anthropic's API refuses calls from a page (CORS), the key stays out of
 *    URLs, and the backend trims each list to text models. Only from the
 *    provider's own host: a compatible server under the OpenAI setting keeps
 *    the shipped list. A configured model the list lacks (an old default that
 *    is no longer a valid id) is replaced by the recommended one.
 *  - `local`: whatever the endpoint serves. The shipped list (llama3,
 *    mistral, …) can never contain what someone runs locally, and the model
 *    field is a fixed dropdown — so a local model not on that list was simply
 *    unselectable. Direct first; the page is served over HTTPS, so a plain-http
 *    endpoint is blocked as mixed content and the backend (same host as the
 *    model server) answers instead. llama.cpp also states each model's `n_ctx`,
 *    which the context budgeter should believe over any table.
 *
 * One effect per discovery kind, keyed on the fields that matter (key, base
 * URL, model) rather than one effect per provider: adding a provider is a
 * registry row, not a hook.
 */

/**
 * The backend lists a hosted provider's models with the user's key, from the
 * provider's host only — with the windows it states (Anthropic's Models API
 * gives `max_input_tokens`), which beat the table in utils/contextWindow.ts.
 */
async function listOfficialModels(provider: LLMProvider, apiKey: string, baseUrl: string): Promise<Listed> {
  try {
    const res = await fetch('/api/models', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': useAppStore.getState().csrfToken || ''
      },
      body: JSON.stringify({ provider, apiKey, baseUrl })
    })
    if (!res.ok) return { names: [], windows: {} }
    const data = await res.json()
    return { names: normalizeModelList(data), windows: normalizeContextWindows(data) }
  } catch (err) {
    console.error(`Failed to fetch ${provider} models`, err)
    return { names: [], windows: {} }
  }
}

/**
 * Shapes a model listing into plain names.
 *
 * Three shapes reach this: OpenAI's `{data:[{id}]}`, Ollama's
 * `{models:[{name}]}`, and our own backend's already-normalized
 * `{models:["name"]}`. The last one is why the entry check is per-item rather
 * than per-shape — reading `.name` off a string yields undefined and silently
 * empties the list, which is exactly how this first shipped broken.
 */
function normalizeModelList(data: unknown): string[] {
  const payload = (data ?? {}) as {
    data?: Array<{ id?: string } | string>
    models?: Array<{ name?: string; model?: string } | string>
  }
  const nameOf = (entry: { id?: string; name?: string; model?: string } | string): string | undefined =>
    typeof entry === 'string' ? entry : entry?.id || entry?.name || entry?.model

  for (const list of [payload.data, payload.models]) {
    if (!Array.isArray(list)) continue
    const names = list.map(nameOf).filter((v): v is string => !!v)
    if (names.length > 0) return names
  }
  return []
}

/**
 * Context windows the endpoint stated, keyed by model id. llama.cpp puts
 * `n_ctx` under each model's `meta`; our own backend proxy forwards it as
 * `contextWindows`. Anything else simply has none, and the table in
 * utils/contextWindow.ts answers instead.
 */
function normalizeContextWindows(data: unknown): Record<string, number> {
  const payload = (data ?? {}) as {
    data?: Array<{ id?: string; meta?: { n_ctx?: number } }>
    contextWindows?: Record<string, number>
  }
  if (payload.contextWindows && typeof payload.contextWindows === 'object') {
    return payload.contextWindows
  }
  const out: Record<string, number> = {}
  for (const entry of payload.data ?? []) {
    const n = entry?.meta?.n_ctx
    if (entry?.id && typeof n === 'number' && n > 0) out[entry.id] = n
  }
  return out
}

type Listed = { names: string[]; windows: Record<string, number> }

async function listLocalModelsDirect(baseUrl: string): Promise<Listed> {
  try {
    const res = await fetch(`${baseUrl.replace(/\/$/, '')}/models`)
    if (!res.ok) return { names: [], windows: {} }
    const data = await res.json()
    return { names: normalizeModelList(data), windows: normalizeContextWindows(data) }
  } catch {
    return { names: [], windows: {} }
  }
}

async function listLocalModelsViaBackend(baseUrl: string): Promise<Listed> {
  try {
    const res = await fetch('/api/models', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': useAppStore.getState().csrfToken || ''
      },
      body: JSON.stringify({ baseUrl })
    })
    if (!res.ok) return { names: [], windows: {} }
    const data = await res.json()
    return { names: normalizeModelList(data), windows: normalizeContextWindows(data) }
  } catch {
    return { names: [], windows: {} }
  }
}

const OFFICIAL = PROVIDER_IDS.filter(id => PROVIDERS[id].discovery.kind === 'official')
const LOCAL = PROVIDER_IDS.filter(id => PROVIDERS[id].discovery.kind === 'local')

/** Does this provider's configuration point at the host its key lists models from? */
function listsFromKey(id: LLMProvider, apiKey: string, baseUrl: string): boolean {
  const discovery = PROVIDERS[id].discovery
  return discovery.kind === 'official' && !!apiKey && discovery.hosts.test(`${baseUrl}/`)
}

export function useModelFetcher(
  // When true the hook fetches the live model lists. Passed `isSettingsOpen`
  // from the Settings modal (so it refreshes + surfaces errors there), and
  // `true` from the app root so the top-bar model dropdown is populated even
  // before Settings is ever opened.
  enabled: boolean,
  setErrorMsg: (msg: string | null) => void,
  setIsLoadingModels: (loading: boolean) => void
) {
  const { providerConfigs, setAvailableModels, updateProviderConfig } = useAppStore()

  // The effects re-run on the fields that decide a listing, not on every
  // config edit (a max-tokens change must not refetch anything).
  const officialDeps = OFFICIAL.map(id => `${providerConfigs[id].apiKey}\u0000${providerConfigs[id].baseUrl}\u0000${providerConfigs[id].model}`).join('\u0001')
  const localDeps = LOCAL.map(id => `${providerConfigs[id].baseUrl}\u0000${providerConfigs[id].model}`).join('\u0001')

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const configs = useAppStore.getState().providerConfigs
    const pending = OFFICIAL.filter(id => listsFromKey(id, configs[id].apiKey, configs[id].baseUrl))
    // No key, or a foreign host: the shipped list.
    for (const id of OFFICIAL) if (!pending.includes(id)) setAvailableModels(id, [])
    if (pending.length === 0) return
    setIsLoadingModels(true)
    void Promise.all(pending.map(async id => {
      const spec = PROVIDERS[id]
      const config = configs[id]
      const listed = await listOfficialModels(id, config.apiKey, config.baseUrl)
      const list = listed.names
      if (cancelled) return true
      if (Object.keys(listed.windows).length > 0) useAppStore.getState().setDiscoveredContextWindows(listed.windows)
      if (list.length === 0) {
        setAvailableModels(id, [])
        setErrorMsg(`Could not load the ${spec.label} models for this key (check the key and base URL). Using the built-in list.`)
        return false
      }
      setAvailableModels(id, list)
      if (!list.includes(config.model)) {
        updateProviderConfig(id, { model: spec.recommendedModel && list.includes(spec.recommendedModel) ? spec.recommendedModel : list[0] })
      }
      return true
    })).then(results => {
      if (cancelled) return
      setIsLoadingModels(false)
      if (results.every(Boolean)) setErrorMsg(null)
    })
    return () => { cancelled = true }
  }, [enabled, officialDeps, setAvailableModels, updateProviderConfig, setErrorMsg, setIsLoadingModels])

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const configs = useAppStore.getState().providerConfigs
    for (const id of LOCAL) {
      void (async () => {
        try {
          const { baseUrl, model } = configs[id]
          let listed = await listLocalModelsDirect(baseUrl)
          if (listed.names.length === 0) listed = await listLocalModelsViaBackend(baseUrl)
          if (cancelled || listed.names.length === 0) return
          setAvailableModels(id, listed.names)
          if (Object.keys(listed.windows).length > 0) {
            useAppStore.getState().setDiscoveredContextWindows(listed.windows)
          }
          // A stale name from a previous endpoint would 404 on every send.
          if (!listed.names.includes(model)) updateProviderConfig(id, { model: listed.names[0] })
        } catch {
          // No server running (a stopped pod) is the normal case, not an
          // error worth showing: the dropdown falls back to the shipped list.
        }
      })()
    }
    return () => { cancelled = true }
  }, [enabled, localDeps, setAvailableModels, updateProviderConfig])
}

import type { ChangeEvent } from 'react'
import { Sun, Moon, Sparkles, Settings, Menu, LogOut } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { PROVIDERS, PROVIDER_IDS, modelsFor } from '../utils/providers'
import type { LLMProvider } from '../store/useAppStore'
import { useTranslation } from '../i18n'

interface AppHeaderProps {
  layoutMode: 'desktop' | 'portrait' | 'landscape' | 'tablet-square'
  onOpenSettings: () => void
}

/**
 * Top application bar: sidebar toggle, logo, provider/model/system-prompt
 * dropdowns, settings, theme + language switchers, and the user menu.
 * Extracted from App.tsx unchanged (same DOM/classes); all state comes from
 * the store, only layout mode and the settings opener are passed down.
 */
export function AppHeader({ layoutMode, onOpenSettings }: AppHeaderProps) {
  const { t } = useTranslation()
  const {
    theme,
    setTheme,
    language,
    setLanguage,
    isSidebarOpen,
    toggleSidebar,
    activeProvider,
    setProvider,
    providerConfigs,
    updateProviderConfig,
    availableModels,
    customSystemPrompts,
    activeSystemPromptId,
    setActiveSystemPromptId,
    user,
    logout
  } = useAppStore()

  const activeConfig = providerConfigs[activeProvider]

  // Toggle theme helper
  const toggleTheme = () => {
    setTheme(theme === 'dark' ? 'light' : 'dark')
  }

  const handleModelChange = (e: ChangeEvent<HTMLSelectElement>) => {
    updateProviderConfig(activeProvider, { model: e.target.value })
  }

  // Discovered lists (the key's own, or what a local endpoint serves) beat the shipped one.
  const getAvailableModels = () => modelsFor(activeProvider, availableModels)


  return (
    <header className="app-header">
      <div className="app-header-left">
        {/* Sidebar Toggle Button if collapsed */}
        {!isSidebarOpen && (
          <button
            onClick={toggleSidebar}
            className="btn-icon"
            title={t.app.sidebarToggle}
            type="button"
            style={{ marginRight: '0.5rem' }}
          >
            <Menu size={18} />
          </button>
        )}

        <div className="app-logo">
          <Sparkles size={20} style={{ color: 'var(--accent)' }} />
          Web <span>Canvas</span>
        </div>
      </div>

      <div className="app-header-right">
        {/* Provider Selector Dropdown */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{t.app.provider}</span>
          <select
            className="select-styled"
            value={activeProvider}
            onChange={(e) => setProvider(e.target.value as LLMProvider)}
            title="Select LLM Provider"
          >
            {PROVIDER_IDS.map(id => <option key={id} value={id}>{PROVIDERS[id].label}</option>)}
          </select>
        </div>

        {/* Dynamic Model Selector Dropdown */}
        {layoutMode !== 'portrait' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{t.app.activeModel}</span>
            <select
              className="select-styled"
              value={activeConfig.model}
              onChange={handleModelChange}
              title={`Select ${activeProvider} Model`}
            >
              {getAvailableModels().map(model => (
                <option key={model} value={model}>
                  {model}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* System Prompt Selector Dropdown */}
        {layoutMode !== 'portrait' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{t.app.systemPrompt}</span>
            <select
              className="select-styled"
              value={activeSystemPromptId}
              onChange={(e) => setActiveSystemPromptId(e.target.value)}
              title="Select System Prompt Preset"
            >
              {customSystemPrompts.map(prompt => (
                <option key={prompt.id} value={prompt.id}>
                  {prompt.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* Settings Button */}
        <button
          onClick={onOpenSettings}
          className="btn-icon"
          title={t.app.settingsTitle}
          type="button"
        >
          <Settings size={18} />
        </button>

        {/* Theme Switcher */}
        <button
          onClick={toggleTheme}
          className="btn-icon"
          title={theme === 'dark' ? t.app.switchToLight : t.app.switchToDark}
          type="button"
        >
          {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
        </button>

        {/* Language Switcher */}
        <button
          onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')}
          className="btn-icon font-medium text-xs"
          title={language === 'en' ? 'Switch to Chinese' : 'Switch to English'}
          type="button"
          style={{ width: '32px', height: '32px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
        >
          {language === 'en' ? '中' : 'EN'}
        </button>

        {/* User Profile & Logout */}
        {user && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginLeft: '0.25rem', borderLeft: '1px solid var(--border-color)', paddingLeft: '0.75rem' }}>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', fontWeight: 500 }}>
              {t.app.hi} <strong style={{ color: 'var(--text-primary)' }}>{user.username}</strong>
            </span>
            <button
              onClick={async () => {
                if (window.confirm(t.app.logoutConfirm)) {
                  await logout()
                  window.location.reload()
                }
              }}
              className="btn-icon"
              title={t.app.logout}
              type="button"
              style={{ color: 'var(--text-secondary)' }}
            >
              <LogOut size={16} />
            </button>
          </div>
        )}
      </div>
    </header>
  )
}

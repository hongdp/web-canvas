import React, { useState } from 'react'
import { X, Key, Shield, HelpCircle, Save, Plus, Trash2, Edit, AlertCircle, ShieldAlert } from 'lucide-react'
import { useAppStore, type LLMProvider } from '../store/useAppStore'
import { PROVIDERS, PROVIDER_IDS, modelsFor } from '../utils/providers'
import { useTranslation } from '../i18n'
import { defaultMaxSteps, defaultContinueAfterWrites, defaultPolishModel } from '../utils/runDefaults'
import { DEFAULT_POLISH_PROMPT } from '../store/defaults'
import { useModelFetcher } from '../hooks/useModelFetcher'
import { supportsReasoningEffort, supportedReasoningEfforts, DEFAULT_REASONING_EFFORT } from '../utils/reasoningEffort'
import { autoProtocolFor } from '../utils/protocolChoice'

interface SettingsModalProps {
  isOpen: boolean
  onClose: () => void
}

export const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose }) => {
  const { 
    activeProvider,
    setProvider,
    providerConfigs, 
    updateProviderConfig,
    polishPrompt,
    setPolishPrompt,
    availableModels,
    customSystemPrompts,
    addSystemPrompt,
    updateSystemPrompt,
    deleteSystemPrompt,
    activeSystemPromptId,
    setActiveSystemPromptId,
    debugMode,
    setDebugMode
  } = useAppStore()

  const { t } = useTranslation()

  const [activeTab, setActiveTab] = React.useState<LLMProvider>('gemini')
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [, setIsLoadingModels] = useState(false)

  useModelFetcher(isOpen, setErrorMsg, setIsLoadingModels)

  // Set the default tab to the active provider when the modal opens (and keep
  // it following external active-provider changes while open). Implemented as
  // the "adjust state during render" pattern to avoid setState-in-effect.
  const [prevTabSync, setPrevTabSync] = React.useState<{ isOpen: boolean; provider: LLMProvider }>({ isOpen: false, provider: activeProvider })
  if (prevTabSync.isOpen !== isOpen || prevTabSync.provider !== activeProvider) {
    setPrevTabSync({ isOpen, provider: activeProvider })
    if (isOpen) {
      setActiveTab(activeProvider)
    }
  }

  if (!isOpen) return null

  const currentConfig = providerConfigs[activeTab] || providerConfigs.gemini
  const geminiConfig = providerConfigs.gemini

  const handleConfigChange = (field: 'apiKey' | 'baseUrl' | 'maxOutputTokens' | 'model' | 'reasoningEffort' | 'documentProtocol', value: string | number) => {
    updateProviderConfig(activeTab, { [field]: value })
  }

  const spec = PROVIDERS[activeTab]


  return (
    <div className="modal-overlay" onClick={onClose}>
      <div 
        className="modal-content glass-panel" 
        onClick={(e) => e.stopPropagation()}
        style={{
          border: '1px solid var(--border-color)',
          backgroundColor: 'var(--bg-secondary)',
          color: 'var(--text-primary)',
          maxWidth: '560px'
        }}
      >
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <Shield size={18} style={{ color: 'var(--accent)' }} />
            <h3>{`${spec.label} ${t.settings.title}`}</h3>
          </div>
          <button onClick={onClose} className="btn-icon" title="Close" type="button">
            <X size={18} />
          </button>
        </div>

        {/* Selection Tabs */}
        <div className="settings-tabs" style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
          {PROVIDER_IDS.map((prov) => (
            <button
              key={prov}
              onClick={() => setActiveTab(prov)}
              type="button"
              className={`settings-tab-btn ${activeTab === prov ? 'active' : ''}`}
            >
              {PROVIDERS[prov].shortLabel}
            </button>
          ))}
        </div>

        <div className="modal-body" style={{ maxHeight: '48vh', overflowY: 'auto', paddingRight: '0.5rem', display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
          {/* Error Alert Display */}
          {errorMsg && (
            <div style={{
              padding: '0.75rem',
              borderRadius: '8px',
              backgroundColor: 'rgba(239, 68, 68, 0.15)',
              border: '1px solid rgba(239, 68, 68, 0.3)',
              color: '#f87171',
              fontSize: '0.85rem',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: '0.5rem'
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <AlertCircle size={16} style={{ flexShrink: 0 }} />
                <span>{errorMsg}</span>
              </div>
              <button 
                onClick={() => setErrorMsg(null)} 
                className="btn-icon" 
                title="Dismiss error"
                type="button"
                style={{ padding: '2px', color: '#f87171' }}
              >
                <X size={14} />
              </button>
            </div>
          )}

            <>
              {/* Active Provider Toggle */}
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0.75rem', borderRadius: '8px', backgroundColor: 'var(--bg-tertiary)', border: '1px solid var(--border-color)' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.2rem' }}>
                  <span style={{ fontSize: '0.85rem', fontWeight: 600 }}>Active LLM Provider</span>
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    {activeProvider === activeTab ? 'This provider is currently active.' : 'Configure and set as current provider.'}
                  </span>
                </div>
                {activeProvider === activeTab ? (
                  <span style={{ 
                    fontSize: '0.75rem', 
                    fontWeight: 600, 
                    color: 'var(--accent)', 
                    backgroundColor: 'rgba(var(--accent-rgb), 0.1)', 
                    padding: '0.25rem 0.5rem', 
                    borderRadius: '4px',
                    border: '1px solid var(--accent)'
                  }}>
                    Active
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => setProvider(activeTab as LLMProvider)}
                    className="btn-primary"
                    style={{ fontSize: '0.75rem', padding: '0.35rem 0.6rem', borderRadius: '6px' }}
                  >
                    Set Active
                  </button>
                )}
              </div>

              {/* Model Selector Dropdown */}
              <div className="form-group">
                <label htmlFor="model-select">{t.settings.modelName}</label>
                <select
                  id="model-select"
                  value={currentConfig.model}
                  onChange={(e) => handleConfigChange('model', e.target.value)}
                  className="select-styled"
                  style={{ width: '100%' }}
                >
                  {modelsFor(activeTab, availableModels).map(model => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </div>

              {/* API Key Input */}
              <div className="form-group">
                <label htmlFor="api-key-input" style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
                  <Key size={14} /> {spec.keyOptional ? 'API Key (Optional)' : `${spec.label} ${t.settings.apiKey}`}
                </label>
                <input
                  id="api-key-input"
                  type="password"
                  value={currentConfig.apiKey}
                  onChange={(e) => handleConfigChange('apiKey', e.target.value)}
                  placeholder={
                    spec.keyPlaceholder
                  }
                  className="form-input"
                />
              </div>

              {/* Base URL Input */}
              <div className="form-group">
                <label htmlFor="base-url-input">{t.settings.baseUrl}</label>
                <input
                  id="base-url-input"
                  type="text"
                  value={currentConfig.baseUrl}
                  onChange={(e) => handleConfigChange('baseUrl', e.target.value)}
                  placeholder={
                    spec.baseUrlPlaceholder
                  }
                  className="form-input"
                />
              </div>

              {/* Max Output Tokens Input */}
              <div className="form-group">
                <label htmlFor="max-tokens-input">Max Output Tokens (up to 65536)</label>
                <input
                  id="max-tokens-input"
                  type="number"
                  value={currentConfig.maxOutputTokens || 16384}
                  onChange={(e) => handleConfigChange('maxOutputTokens', parseInt(e.target.value) || 16384)}
                  placeholder="e.g. 16384"
                  min={1024}
                  max={65536}
                  step={1024}
                  className="form-input"
                />
              </div>

              {/* Reasoning effort — only for models that actually take one. */}
              {supportsReasoningEffort(activeTab, currentConfig.model) && (
                <div className="form-group">
                  <label htmlFor="reasoning-effort-select">{t.settings.reasoningEffort}</label>
                  <select
                    id="reasoning-effort-select"
                    value={currentConfig.reasoningEffort ?? DEFAULT_REASONING_EFFORT}
                    onChange={(e) => handleConfigChange('reasoningEffort', e.target.value)}
                    className="form-input"
                  >
                    {supportedReasoningEfforts(activeTab, currentConfig.model).map(level => (
                      <option key={level} value={level}>
                        {level === 'default' ? t.settings.reasoningProviderDefault : level}
                      </option>
                    ))}
                  </select>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                    {t.settings.reasoningEffortHint}
                  </p>
                </div>
              )}

              {/* How this model is asked to edit the document. Per provider,
                  because the right answer depends on how the provider streams
                  tool arguments — see utils/protocolChoice. */}
              <div className="form-group">
                <label htmlFor="document-protocol-select">{t.settings.documentProtocol}</label>
                <select
                  id="document-protocol-select"
                  value={currentConfig.documentProtocol ?? 'auto'}
                  onChange={(e) => handleConfigChange('documentProtocol', e.target.value)}
                  className="form-input"
                >
                  <option value="auto">
                    {t.settings.protocolAuto(
                      autoProtocolFor(activeTab) === 'tools' ? t.settings.protocolTools : t.settings.protocolMarkup
                    )}
                  </option>
                  <option value="tools">{t.settings.protocolTools}</option>
                  <option value="markup">{t.settings.protocolMarkup}</option>
                </select>
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                  {t.settings.documentProtocolHint}
                </p>
              </div>

              {/* The agentic loop (docs/features/agentic_chat_loop.md), per
                  provider: what a turn may read and how many steps it takes. */}
              <div className="form-group">
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={currentConfig.agentTools !== false}
                    onChange={(e) => updateProviderConfig(activeTab, { agentTools: e.target.checked })}
                  />
                  {t.settings.agentTools}
                </label>
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                  {t.settings.agentToolsHint}
                </p>
              </div>
              <div className="form-group">
                <label htmlFor="polish-model-input">{t.settings.polishModel}</label>
                <input
                  id="polish-model-input"
                  type="text"
                  className="form-input"
                  placeholder={defaultPolishModel(activeTab, currentConfig.model)}
                  value={currentConfig.polishModel ?? ''}
                  onChange={(e) => updateProviderConfig(activeTab, { polishModel: e.target.value || undefined })}
                />
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                  {t.settings.polishModelHint(defaultPolishModel(activeTab, currentConfig.model))}
                </p>
                <details style={{ marginTop: '0.5rem' }}>
                  <summary style={{ cursor: 'pointer', fontSize: '0.85rem' }}>{t.settings.polishPrompt}</summary>
                  <label htmlFor="polish-system-input" style={{ display: 'block', marginTop: '0.5rem', fontSize: '0.8rem' }}>{t.settings.polishSystem}</label>
                  <textarea
                    id="polish-system-input"
                    className="form-input"
                    rows={2}
                    value={polishPrompt.system}
                    onChange={(e) => setPolishPrompt({ ...polishPrompt, system: e.target.value })}
                  />
                  <label htmlFor="polish-template-input" style={{ display: 'block', marginTop: '0.5rem', fontSize: '0.8rem' }}>{t.settings.polishTemplate}</label>
                  <textarea
                    id="polish-template-input"
                    className="form-input"
                    rows={10}
                    value={polishPrompt.template}
                    onChange={(e) => setPolishPrompt({ ...polishPrompt, template: e.target.value })}
                  />
                  <button
                    type="button"
                    className="btn-secondary"
                    style={{ marginTop: '0.4rem' }}
                    onClick={() => setPolishPrompt({ ...DEFAULT_POLISH_PROMPT })}
                  >
                    {t.settings.polishReset}
                  </button>
                </details>
              </div>

              {currentConfig.agentTools !== false && (
                <>
                  <div className="form-group">
                    <label htmlFor="agent-max-steps-input">{t.settings.agentMaxSteps}</label>
                    <input
                      id="agent-max-steps-input"
                      type="number"
                      min={0}
                      step={1}
                      className="form-input"
                      placeholder={String(defaultMaxSteps(activeTab))}
                      value={currentConfig.agentMaxSteps ?? ''}
                      onChange={(e) => {
                        const raw = e.target.value
                        const n = Number(raw)
                        updateProviderConfig(activeTab, {
                          agentMaxSteps: raw === '' || !Number.isFinite(n) || n < 0 ? undefined : Math.floor(n)
                        })
                      }}
                    />
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                      {t.settings.agentMaxStepsHint(defaultMaxSteps(activeTab))}
                    </p>
                  </div>
                  <div className="form-group">
                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                      <input
                        type="checkbox"
                        checked={currentConfig.continueAfterWrites ?? defaultContinueAfterWrites(activeTab)}
                        onChange={(e) => updateProviderConfig(activeTab, { continueAfterWrites: e.target.checked })}
                      />
                      {t.settings.continueAfterWrites}
                    </label>
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                      {t.settings.continueAfterWritesHint}
                    </p>
                  </div>
                  <div className="form-group">
                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                      <input
                        type="checkbox"
                        checked={currentConfig.webAccess !== false}
                        onChange={(e) => updateProviderConfig(activeTab, { webAccess: e.target.checked })}
                      />
                      {t.settings.webAccess}
                    </label>
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                      {t.settings.webAccessHint}
                    </p>
                  </div>
                </>
              )}

              {supportsReasoningEffort(activeTab, currentConfig.model) && (
                <div className="form-group">
                  <label htmlFor="long-reasoning-input">{t.settings.longReasoningReminder}</label>
                  <input
                    id="long-reasoning-input"
                    type="number"
                    min={0}
                    step={500}
                    className="form-input"
                    value={currentConfig.longReasoningReminderTokens ?? ''}
                    onChange={(e) => {
                      const raw = e.target.value
                      const n = Number(raw)
                      updateProviderConfig(activeTab, { longReasoningReminderTokens: raw === '' || !Number.isFinite(n) || n <= 0 ? undefined : Math.floor(n) })
                    }}
                  />
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                    {t.settings.longReasoningReminderHint}
                  </p>
                </div>
              )}

              {/* Every turn runs on the server (backend_authority.md phase 4): its token budget. */}
              <div className="form-group">
                <label htmlFor="run-token-budget-input">{t.settings.runTokenBudget}</label>
                <input
                  id="run-token-budget-input"
                  type="number"
                  min={0}
                  step={1000}
                  className="form-input"
                  value={currentConfig.runTokenBudget ?? ''}
                  onChange={(e) => {
                    const raw = e.target.value
                    const n = Number(raw)
                    updateProviderConfig(activeTab, { runTokenBudget: raw === '' || !Number.isFinite(n) || n <= 0 ? undefined : Math.floor(n) })
                  }}
                />
                <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.35rem 0 0' }}>
                  {t.settings.runTokenBudgetHint}
                </p>
              </div>

              {/* Debug Mode Checkbox */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginTop: '0.25rem' }}>
                <input 
                  id="debug-mode-checkbox"
                  type="checkbox"
                  checked={debugMode}
                  onChange={(e) => setDebugMode(e.target.checked)}
                  style={{
                    width: '16px',
                    height: '16px',
                    accentColor: 'var(--accent)',
                    cursor: 'pointer'
                  }}
                />
                <label 
                  htmlFor="debug-mode-checkbox"
                  style={{
                    fontSize: '0.85rem',
                    fontWeight: 600,
                    color: 'var(--text-secondary)',
                    cursor: 'pointer',
                    userSelect: 'none'
                  }}
                >
                  Enable Debug Mode (logs request/response in console)
                </label>
              </div>


              {/* Help Info Box */}
              <div 
                className="form-help" 
                style={{
                  display: 'flex',
                  gap: '0.5rem',
                  padding: '0.75rem',
                  borderRadius: '8px',
                  backgroundColor: 'var(--bg-tertiary)',
                  border: '1px solid var(--border-color)',
                }}
              >
                <HelpCircle size={16} style={{ flexShrink: 0, color: 'var(--text-secondary)', marginTop: '2px' }} />
                <span style={{ fontSize: '0.85rem', lineHeight: '1.4' }}>
                  {spec.help.intro}
                  {spec.help.link && (
                    <>
                      {' '}
                      <a href={spec.help.link.href} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)', textDecoration: 'underline' }}>
                        {spec.help.link.label}
                      </a>
                    </>
                  )}
                  . The default base URL is <code>{spec.defaults.baseUrl}</code>.
                  {spec.help.extra && ` ${spec.help.extra}`}
                </span>
              </div>

              {/* Gemini Safety Settings Section */}
              {activeTab === 'gemini' && (
                <div style={{ marginTop: '0.5rem', borderTop: '1px solid var(--border-color)', paddingTop: '1rem' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', marginBottom: '0.75rem' }}>
                    <ShieldAlert size={16} style={{ color: 'var(--accent)' }} />
                    <span style={{ fontSize: '0.95rem', fontWeight: 600, color: 'var(--text-secondary)' }}>Gemini Safety Thresholds</span>
                  </div>
                  
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '0.75rem' }}>
                    {(() => {
                      const safetyCategories = [
                        { id: 'HARM_CATEGORY_HARASSMENT', label: 'Harassment' },
                        { id: 'HARM_CATEGORY_HATE_SPEECH', label: 'Hate Speech' },
                        { id: 'HARM_CATEGORY_SEXUALLY_EXPLICIT', label: 'Sexually Explicit' },
                        { id: 'HARM_CATEGORY_DANGEROUS_CONTENT', label: 'Dangerous Content' },
                      ]
                      
                      const thresholds = [
                        { value: 'HARM_BLOCK_THRESHOLD_UNSPECIFIED', label: 'Default / Unspecified' },
                        { value: 'BLOCK_NONE', label: 'Block None (Show All)' },
                        { value: 'BLOCK_ONLY_HIGH', label: 'Block High Risk' },
                        { value: 'BLOCK_MEDIUM_AND_ABOVE', label: 'Block Medium & Above' },
                        { value: 'BLOCK_LOW_AND_ABOVE', label: 'Block Low & Above' },
                      ]
                      
                      const currentSettings = geminiConfig.geminiSafetySettings || []
                      
                      return safetyCategories.map(cat => {
                        const currentSetting = currentSettings.find(s => s.category === cat.id)
                        const currentValue = currentSetting?.threshold || 'BLOCK_NONE'
                        
                        return (
                          <div key={cat.id} style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                            <span style={{ fontSize: '0.8rem', fontWeight: 500, color: 'var(--text-secondary)' }}>
                              {cat.label}
                            </span>
                            <select
                              value={currentValue}
                              onChange={(e) => {
                                const updated = currentSettings.map(s => 
                                  s.category === cat.id ? { ...s, threshold: e.target.value } : s
                                )
                                // In case the list was empty or missing this category
                                if (!currentSettings.some(s => s.category === cat.id)) {
                                  updated.push({ category: cat.id, threshold: e.target.value })
                                }
                                updateProviderConfig('gemini', { geminiSafetySettings: updated })
                              }}
                              className="select-styled"
                              style={{ width: '100%', fontSize: '0.8rem', padding: '0.35rem 0.5rem' }}
                            >
                              {thresholds.map(t => (
                                <option key={t.value} value={t.value}>
                                  {t.label}
                                </option>
                              ))}
                            </select>
                          </div>
                        )
                      })
                    })()}
                  </div>
                </div>
              )}

              {/* Active System Prompt Selector */}
              <div className="form-group" style={{ marginTop: '0.5rem', borderTop: '1px solid var(--border-color)', paddingTop: '1rem' }}>
                <label htmlFor="active-prompt-select" style={{ fontSize: '0.95rem', fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: '0.35rem' }}>
                  Active Prompt Preset
                </label>
                <select
                  id="active-prompt-select"
                  value={activeSystemPromptId}
                  onChange={(e) => setActiveSystemPromptId(e.target.value)}
                  className="select-styled"
                  style={{ width: '100%' }}
                >
                  {customSystemPrompts.map(prompt => (
                    <option key={prompt.id} value={prompt.id}>
                      {prompt.name}
                    </option>
                  ))}
                </select>
              </div>

              {/* Presets Management Header */}
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: '0.5rem', borderTop: '1px solid var(--border-color)', paddingTop: '1rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                  <Edit size={16} style={{ color: 'var(--accent)' }} />
                  <span style={{ fontSize: '0.95rem', fontWeight: 600, color: 'var(--text-secondary)' }}>System Prompt Presets</span>
                </div>
                <button 
                  onClick={() => addSystemPrompt('New Preset', '')}
                  className="btn-icon" 
                  title="Add New Preset"
                  type="button"
                  style={{ padding: '4px', borderRadius: '4px', backgroundColor: 'var(--bg-tertiary)', color: 'var(--accent)' }}
                >
                  <Plus size={16} />
                </button>
              </div>

              {/* Presets Management List */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                {customSystemPrompts.map((prompt) => (
                  <div 
                    key={prompt.id} 
                    style={{
                      padding: '0.75rem',
                      borderRadius: '8px',
                      backgroundColor: 'var(--bg-tertiary)',
                      border: '1px solid var(--border-color)',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '0.5rem'
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                      <input
                        type="text"
                        value={prompt.name}
                        onChange={(e) => updateSystemPrompt(prompt.id, { name: e.target.value })}
                        placeholder="Preset Name..."
                        className="form-input"
                        style={{ flex: 1, fontWeight: 600, padding: '0.35rem 0.6rem', fontSize: '0.85rem' }}
                      />
                      
                      <button 
                        onClick={() => deleteSystemPrompt(prompt.id)}
                        className="btn-icon" 
                        title="Delete Preset"
                        type="button"
                        disabled={customSystemPrompts.length <= 1}
                        style={{ 
                          color: customSystemPrompts.length <= 1 ? 'var(--text-muted)' : 'var(--text-secondary)',
                          cursor: customSystemPrompts.length <= 1 ? 'default' : 'pointer'
                        }}
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                    
                    <textarea
                      value={prompt.content}
                      onChange={(e) => updateSystemPrompt(prompt.id, { content: e.target.value })}
                      placeholder="System instruction contents..."
                      className="form-input"
                      rows={3}
                      style={{ resize: 'vertical', width: '100%', fontSize: '0.85rem', padding: '0.4rem 0.6rem', fontFamily: 'inherit' }}
                    />
                  </div>
                ))}
              </div>

            </>
        </div>

        <div className="modal-footer">
          <button onClick={onClose} className="btn-primary" style={{ display: 'flex', alignItems: 'center', gap: '0.35rem' }} type="button">
            <Save size={16} /> {t.settings.save}
          </button>
        </div>
      </div>
    </div>
  )
}

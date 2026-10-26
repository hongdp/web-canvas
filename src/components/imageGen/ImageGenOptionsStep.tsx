/**
 * STEP 3 – Image Options & Provider panel of the image generation modal
 * (aspect ratio, provider, advanced settings, prompt preview, generate
 * button). Presentational only; discovery/generation state lives in
 * ImageGenerationModal.
 */

import React from 'react'
import { Settings2, ChevronDown, ChevronRight, RefreshCw, AlertCircle, Check, FileText } from 'lucide-react'
import type { ImageGenProvider } from '../../services/imageGen'
import {
  ASPECT_RATIOS,
  ALL_MODELS,
  PROVIDER_LABELS,
  PROVIDER_FULL_LABELS,
} from '../../services/imageGenModels'
import type { AspectRatio } from '../../services/imageGenModels'
import type { LLMProvider, ImageGenConfig } from '../../types/llm'
import { textareaStyle, sectionLabel, pillBtn, accentBtnStyle } from './ui'

interface ImageGenOptionsStepProps {
  aspectRatio: AspectRatio
  setAspectRatio: (value: AspectRatio) => void
  provider: ImageGenProvider
  imageGenConfig: ImageGenConfig
  updateImageGenConfig: (updates: Partial<ImageGenConfig>) => void
  showAdvanced: boolean
  setShowAdvanced: React.Dispatch<React.SetStateAction<boolean>>
  availableModels: string[]
  discoveredModels: string[] | null
  isDiscovering: boolean
  discoverError: string | null
  onDiscoverModels: () => void
  effectiveApiKey: string
  usingSharedKey: boolean
  sharedLlmProvider: LLMProvider | undefined
  style: 'vivid' | 'natural'
  setStyle: (value: 'vivid' | 'natural') => void
  negativePrompt: string
  setNegativePrompt: (value: string) => void
  finalPrompt: string
  enhancedPrompt: string
  genError: string | null
  isGenerating: boolean
  isEnhancing: boolean
  onGenerate: () => void
}

export const ImageGenOptionsStep: React.FC<ImageGenOptionsStepProps> = ({
  aspectRatio,
  setAspectRatio,
  provider,
  imageGenConfig,
  updateImageGenConfig,
  showAdvanced,
  setShowAdvanced,
  availableModels,
  discoveredModels,
  isDiscovering,
  discoverError,
  onDiscoverModels,
  effectiveApiKey,
  usingSharedKey,
  sharedLlmProvider,
  style,
  setStyle,
  negativePrompt,
  setNegativePrompt,
  finalPrompt,
  enhancedPrompt,
  genError,
  isGenerating,
  isEnhancing,
  onGenerate,
}) => {
  return (
    <div style={{
      borderRadius: '10px',
      border: '1px solid var(--border-color)',
      overflow: 'hidden',
      marginBottom: '0.875rem',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: '0.5rem',
        padding: '0.6rem 0.85rem',
        backgroundColor: 'var(--bg-tertiary)',
        borderBottom: '1px solid var(--border-color)',
      }}>
        <div style={{
          width: 20, height: 20, borderRadius: '50%',
          background: 'var(--bg-secondary)',
          border: '1.5px solid var(--border-color)',
          color: 'var(--text-muted)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '0.7rem', fontWeight: 700, flexShrink: 0,
        }}>3</div>
        <span style={{ fontSize: '0.82rem', fontWeight: 600 }}>Image Options &amp; Provider</span>
      </div>
      <div style={{ padding: '0.85rem', display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>

        {/* Aspect Ratio */}
        <div>
          {sectionLabel('Aspect Ratio')}
          <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
            {ASPECT_RATIOS.map(r => pillBtn(aspectRatio === r.value, () => setAspectRatio(r.value), r.label))}
          </div>
        </div>

        {/* Provider */}
        <div>
          {sectionLabel('Image Provider')}
          <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
            {(Object.keys(PROVIDER_LABELS) as ImageGenProvider[]).map(p =>
              pillBtn(provider === p, () => updateImageGenConfig({ provider: p, model: ALL_MODELS[p][0] }), PROVIDER_LABELS[p])
            )}
          </div>
        </div>

        {/* Advanced Settings Toggle */}
        <div>
          <button
            type="button"
            onClick={() => setShowAdvanced(v => !v)}
            style={{
              display: 'flex', alignItems: 'center', gap: '5px',
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--text-secondary)', fontSize: '0.78rem', padding: '0',
            }}
          >
            <Settings2 size={13} />
            Advanced Options
            <ChevronDown size={12} style={{ transform: showAdvanced ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }} />
          </button>

          {showAdvanced && (
            <div style={{
              marginTop: '0.6rem', padding: '0.75rem',
              borderRadius: '8px', border: '1px solid var(--border-color)',
              backgroundColor: 'var(--bg-tertiary)',
              display: 'flex', flexDirection: 'column', gap: '0.65rem',
            }}>
              {/* Model */}
              <div>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.35rem' }}>
                  <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                    <span style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>Model</span>
                    {discoveredModels && (
                      <span style={{ fontSize: '0.68rem', color: '#10b981', background: 'rgba(16,185,129,0.1)', borderRadius: '4px', padding: '1px 5px' }}>
                        {discoveredModels.length} discovered
                      </span>
                    )}
                  </label>
                  <button
                    type="button"
                    title="Discover available models from API"
                    onClick={onDiscoverModels}
                    disabled={isDiscovering || !effectiveApiKey}
                    style={{
                      display: 'flex', alignItems: 'center', gap: '3px',
                      background: 'none', border: '1px solid var(--border-color)',
                      borderRadius: '5px', cursor: effectiveApiKey ? 'pointer' : 'not-allowed',
                      color: 'var(--text-secondary)', fontSize: '0.72rem',
                      padding: '2px 7px', opacity: effectiveApiKey ? 1 : 0.45,
                    }}
                  >
                    <RefreshCw size={10} style={{ animation: isDiscovering ? 'spin 1s linear infinite' : 'none' }} />
                    {isDiscovering ? 'Loading…' : 'Refresh'}
                  </button>
                </div>
                <select
                  className="select-styled"
                  value={imageGenConfig.model || availableModels[0]}
                  onChange={e => updateImageGenConfig({ model: e.target.value })}
                  style={{ width: '100%', fontSize: '0.82rem' }}
                >
                  {availableModels.map(m => <option key={m} value={m}>{m}</option>)}
                </select>
                {discoverError && (
                  <div style={{ fontSize: '0.72rem', color: '#f87171', marginTop: '0.25rem', display: 'flex', alignItems: 'center', gap: '4px' }}>
                    <AlertCircle size={11} /> {discoverError}
                  </div>
                )}
              </div>


              {/* API Key */}
              <div>
                {sectionLabel('API Key', `(for ${PROVIDER_FULL_LABELS[provider]})`)}
                {usingSharedKey ? (
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: '0.5rem',
                    padding: '0.45rem 0.7rem', borderRadius: '6px',
                    border: '1px solid rgba(16, 185, 129, 0.3)',
                    backgroundColor: 'rgba(16, 185, 129, 0.08)',
                    fontSize: '0.8rem', color: '#10b981',
                  }}>
                    <Check size={13} />
                    <span>Using <strong>{sharedLlmProvider}</strong> API key from Settings — no separate key needed</span>
                  </div>
                ) : (
                  <input
                    type="password"
                    value={imageGenConfig.apiKey}
                    onChange={e => updateImageGenConfig({ apiKey: e.target.value })}
                    placeholder={`Enter ${PROVIDER_FULL_LABELS[provider]} API key`}
                    style={{
                      width: '100%', padding: '0.42rem 0.65rem', borderRadius: '6px',
                      border: '1px solid var(--border-color)', outline: 'none',
                      backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)',
                      fontSize: '0.82rem', fontFamily: 'monospace', boxSizing: 'border-box',
                    }}
                  />
                )}
              </div>

              {/* Base URL */}
              <div>
                {sectionLabel('Custom Base URL', '(optional)')}
                <input
                  type="text"
                  value={imageGenConfig.baseUrl || ''}
                  onChange={e => updateImageGenConfig({ baseUrl: e.target.value })}
                  placeholder={
                    provider === 'grok' ? 'https://api.x.ai/v1' :
                    provider === 'openai' ? 'https://api.openai.com/v1' :
                    'Default endpoint'
                  }
                  style={{
                    width: '100%', padding: '0.42rem 0.65rem', borderRadius: '6px',
                    border: '1px solid var(--border-color)', outline: 'none',
                    backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)',
                    fontSize: '0.82rem', fontFamily: 'monospace', boxSizing: 'border-box',
                  }}
                />
              </div>

              {/* Style (DALL·E 3 only) */}
              {provider === 'openai' && (
                <div>
                  {sectionLabel('Style', '(DALL·E 3 only)')}
                  <div style={{ display: 'flex', gap: '5px' }}>
                    {(['vivid', 'natural'] as const).map(s =>
                      pillBtn(style === s, () => setStyle(s), s.charAt(0).toUpperCase() + s.slice(1))
                    )}
                  </div>
                </div>
              )}

              {/* Negative Prompt */}
              <div>
                {sectionLabel('Negative Prompt', '(what to avoid)')}
                <textarea
                  value={negativePrompt}
                  onChange={e => setNegativePrompt(e.target.value)}
                  placeholder="blurry, low quality, distorted, watermark…"
                  rows={2}
                  style={{ ...textareaStyle(), fontSize: '0.82rem' }}
                />
              </div>
            </div>
          )}
        </div>

        {/* Active prompt preview */}
        {finalPrompt && (
          <div style={{
            padding: '0.55rem 0.7rem', borderRadius: '7px',
            backgroundColor: 'var(--bg-tertiary)',
            border: '1px solid var(--border-color)',
            fontSize: '0.78rem', color: 'var(--text-secondary)',
          }}>
            <div style={{ fontWeight: 600, marginBottom: '0.2rem', display: 'flex', alignItems: 'center', gap: '4px' }}>
              <FileText size={12} />
              {enhancedPrompt ? 'Enhanced prompt' : 'Raw prompt'} will be sent to {PROVIDER_FULL_LABELS[provider]}
            </div>
            <div style={{ color: 'var(--text-primary)', fontSize: '0.8rem', lineHeight: 1.45 }}>
              {finalPrompt.length > 200 ? finalPrompt.slice(0, 200) + '…' : finalPrompt}
            </div>
          </div>
        )}

        {/* Gen error */}
        {genError && (
          <div style={{
            display: 'flex', gap: '0.4rem', alignItems: 'flex-start',
            padding: '0.55rem 0.7rem', borderRadius: '7px',
            backgroundColor: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.25)',
            color: '#f87171', fontSize: '0.8rem',
          }}>
            <AlertCircle size={13} style={{ flexShrink: 0, marginTop: '2px' }} />
            <span>{genError}</span>
          </div>
        )}

        {/* Generate button */}
        <button
          type="button"
          onClick={onGenerate}
          disabled={isGenerating || !finalPrompt || isEnhancing}
          style={{
            ...accentBtnStyle(isGenerating || !finalPrompt || isEnhancing),
            fontSize: '0.9rem', padding: '0.65rem 1.25rem', width: '100%',
          }}
        >
          {isGenerating ? (
            <><RefreshCw size={15} className="animate-spin" /> Generating image…</>
          ) : (
            <><ChevronRight size={15} /> Generate Image <span style={{ fontSize: '0.72rem', opacity: 0.75 }}>(Ctrl+Enter)</span></>
          )}
        </button>
      </div>
    </div>
  )
}

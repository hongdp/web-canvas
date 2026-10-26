/**
 * STEP 2 – LLM Prompt Enhancement panel of the image generation modal.
 * Presentational only; enhancement state and the abort controller live in
 * ImageGenerationModal.
 */

import React from 'react'
import { AlertCircle, Check, Sparkles, StopCircle, RotateCcw } from 'lucide-react'
import type { LLMProvider, ImageGenConfig } from '../../types/llm'
import { textareaStyle, accentBtnStyle, secondaryBtnStyle } from './ui'

interface ImageGenEnhanceStepProps {
  enhancedPrompt: string
  setEnhancedPrompt: (value: string) => void
  llmEnhancementEnabled: boolean
  updateImageGenConfig: (updates: Partial<ImageGenConfig>) => void
  activeProvider: LLMProvider
  activeModel: string
  isEnhancing: boolean
  enhFocused: boolean
  setEnhFocused: (value: boolean) => void
  enhanceError: string | null
  rawPrompt: string
  onEnhance: () => void
  onStopEnhance: () => void
}

export const ImageGenEnhanceStep: React.FC<ImageGenEnhanceStepProps> = ({
  enhancedPrompt,
  setEnhancedPrompt,
  llmEnhancementEnabled,
  updateImageGenConfig,
  activeProvider,
  activeModel,
  isEnhancing,
  enhFocused,
  setEnhFocused,
  enhanceError,
  rawPrompt,
  onEnhance,
  onStopEnhance,
}) => {
  return (
    <div style={{
      borderRadius: '10px',
      border: `1px solid ${enhancedPrompt ? 'var(--accent)' : 'var(--border-color)'}`,
      overflow: 'hidden',
      marginBottom: '0.875rem',
      transition: 'border-color 0.2s',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0.6rem 0.85rem',
        backgroundColor: enhancedPrompt ? 'rgba(128, 128, 128, 0.07)' : 'var(--bg-tertiary)',
        borderBottom: '1px solid var(--border-color)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <div style={{
            width: 20, height: 20, borderRadius: '50%',
            background: enhancedPrompt ? 'var(--accent)' : 'var(--bg-secondary)',
            border: enhancedPrompt ? 'none' : '1.5px solid var(--border-color)',
            color: enhancedPrompt ? 'white' : 'var(--text-muted)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: '0.7rem', fontWeight: 700, flexShrink: 0, transition: 'all 0.2s',
          }}>2</div>
          <span style={{ fontSize: '0.82rem', fontWeight: 600 }}>LLM Prompt Enhancement</span>
          <span style={{
            fontSize: '0.7rem', padding: '1px 6px', borderRadius: '4px',
            backgroundColor: llmEnhancementEnabled ? 'rgba(16,185,129,0.15)' : 'var(--bg-secondary)',
            color: llmEnhancementEnabled ? '#10b981' : 'var(--text-muted)',
            border: `1px solid ${llmEnhancementEnabled ? 'rgba(16,185,129,0.3)' : 'var(--border-color)'}`,
            cursor: 'pointer', userSelect: 'none',
          }}
            onClick={() => updateImageGenConfig({ llmEnhancementEnabled: !llmEnhancementEnabled })}
          >
            {llmEnhancementEnabled ? 'ON' : 'OFF'}
          </span>
        </div>
        <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
          via {activeProvider} / {activeModel}
        </span>
      </div>

      <div style={{ padding: '0.85rem', display: 'flex', flexDirection: 'column', gap: '0.65rem' }}>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-start' }}>
          <div style={{ flex: 1 }}>
            <textarea
              value={enhancedPrompt}
              onChange={e => setEnhancedPrompt(e.target.value)}
              placeholder={llmEnhancementEnabled
                ? `Click "✦ Enhance" to rewrite your prompt with ${activeProvider}…`
                : 'LLM enhancement is disabled. The raw prompt will be used directly.'}
              rows={4}
              disabled={!llmEnhancementEnabled || isEnhancing}
              style={{
                ...textareaStyle(enhFocused),
                opacity: !llmEnhancementEnabled ? 0.5 : 1,
                fontStyle: isEnhancing && !enhancedPrompt ? 'italic' : 'normal',
              }}
              onFocus={() => setEnhFocused(true)}
              onBlur={() => setEnhFocused(false)}
            />
            {enhancedPrompt && !isEnhancing && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginTop: '0.3rem' }}>
                <Check size={12} style={{ color: '#10b981' }} />
                <span style={{ fontSize: '0.72rem', color: '#10b981' }}>Enhanced prompt ready — you can edit it</span>
              </div>
            )}
          </div>
        </div>

        {enhanceError && (
          <div style={{
            display: 'flex', gap: '0.4rem', alignItems: 'flex-start',
            padding: '0.5rem 0.65rem', borderRadius: '7px',
            backgroundColor: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.25)',
            color: '#f87171', fontSize: '0.79rem',
          }}>
            <AlertCircle size={13} style={{ flexShrink: 0, marginTop: '2px' }} />
            <span>{enhanceError}</span>
          </div>
        )}

        <div style={{ display: 'flex', gap: '0.5rem' }}>
          {llmEnhancementEnabled && (
            isEnhancing ? (
              <button
                type="button"
                onClick={onStopEnhance}
                style={secondaryBtnStyle()}
              >
                <StopCircle size={14} style={{ color: '#ef4444' }} /> Stop
              </button>
            ) : (
              <button
                type="button"
                onClick={onEnhance}
                disabled={!rawPrompt.trim()}
                style={accentBtnStyle(!rawPrompt.trim())}
              >
                <Sparkles size={14} /> Enhance Prompt
              </button>
            )
          )}
          {enhancedPrompt && (
            <button
              type="button"
              onClick={() => setEnhancedPrompt('')}
              style={secondaryBtnStyle()}
            >
              <RotateCcw size={13} /> Use raw
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

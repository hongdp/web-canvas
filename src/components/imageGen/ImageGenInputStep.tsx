/**
 * STEP 1 – Input & Context panel of the image generation modal.
 * Presentational only; all state lives in ImageGenerationModal.
 */

import React from 'react'
import { Settings2, ChevronDown, RotateCcw } from 'lucide-react'
import { DEFAULT_IMAGE_STYLE_SYSTEM_PROMPT } from '../../services/imageGen'
import type { ImageGenConfig } from '../../types/llm'
import { textareaStyle, sectionLabel, secondaryBtnStyle } from './ui'

interface ImageGenInputStepProps {
  rawPrompt: string
  setRawPrompt: (value: string) => void
  context: string
  setContext: (value: string) => void
  promptRef: React.RefObject<HTMLTextAreaElement | null>
  promptFocused: boolean
  setPromptFocused: (value: boolean) => void
  contextFocused: boolean
  setContextFocused: (value: boolean) => void
  showStylePrompt: boolean
  setShowStylePrompt: React.Dispatch<React.SetStateAction<boolean>>
  styleSystemPrompt: string | undefined
  updateImageGenConfig: (updates: Partial<ImageGenConfig>) => void
}

export const ImageGenInputStep: React.FC<ImageGenInputStepProps> = ({
  rawPrompt,
  setRawPrompt,
  context,
  setContext,
  promptRef,
  promptFocused,
  setPromptFocused,
  contextFocused,
  setContextFocused,
  showStylePrompt,
  setShowStylePrompt,
  styleSystemPrompt,
  updateImageGenConfig,
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
          background: 'var(--accent)', color: 'var(--accent-text)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '0.7rem', fontWeight: 700, flexShrink: 0,
        }}>1</div>
        <span style={{ fontSize: '0.82rem', fontWeight: 600 }}>Input &amp; Context</span>
      </div>
      <div style={{ padding: '0.85rem', display: 'flex', flexDirection: 'column', gap: '0.65rem' }}>

        {/* Raw prompt */}
        <div>
          {sectionLabel('Image Prompt')}
          <textarea
            ref={promptRef}
            value={rawPrompt}
            onChange={e => setRawPrompt(e.target.value)}
            placeholder="Describe the image you want to generate…"
            rows={3}
            style={textareaStyle(promptFocused)}
            onFocus={() => setPromptFocused(true)}
            onBlur={() => setPromptFocused(false)}
          />
        </div>

        {/* Additional context */}
        <div>
          {sectionLabel('Additional Context', '(from document — helps LLM enrich the prompt)')}
          <textarea
            value={context}
            onChange={e => setContext(e.target.value)}
            placeholder="Paste relevant document excerpt or describe the scene context…"
            rows={2}
            style={textareaStyle(contextFocused)}
            onFocus={() => setContextFocused(true)}
            onBlur={() => setContextFocused(false)}
          />
        </div>

        {/* Style system prompt toggle */}
        <div>
          <button
            type="button"
            onClick={() => setShowStylePrompt(v => !v)}
            style={{
              display: 'flex', alignItems: 'center', gap: '5px',
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--text-secondary)', fontSize: '0.78rem', padding: '0',
            }}
          >
            <Settings2 size={13} />
            LLM Enhancement Style Prompt
            <ChevronDown size={12} style={{ transform: showStylePrompt ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }} />
          </button>
          {showStylePrompt && (
            <div style={{ marginTop: '0.5rem' }}>
              <textarea
                value={styleSystemPrompt ?? ''}
                onChange={e => updateImageGenConfig({ styleSystemPrompt: e.target.value })}
                placeholder={DEFAULT_IMAGE_STYLE_SYSTEM_PROMPT}
                rows={5}
                style={{ ...textareaStyle(), fontSize: '0.78rem', fontFamily: 'monospace' }}
              />
              <button
                type="button"
                onClick={() => updateImageGenConfig({ styleSystemPrompt: '' })}
                style={{ ...secondaryBtnStyle(), marginTop: '0.35rem', fontSize: '0.74rem', padding: '3px 8px' }}
              >
                <RotateCcw size={11} /> Reset to default
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

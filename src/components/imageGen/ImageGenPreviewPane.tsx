/**
 * Right column of the image generation modal: generated image preview,
 * zoom controls, revised-prompt badge, insert/regenerate actions.
 * Presentational only; state lives in ImageGenerationModal.
 */

import React from 'react'
import { ZoomIn, ZoomOut, Check, ImagePlus, RefreshCw } from 'lucide-react'
import { secondaryBtnStyle } from './ui'

interface ImageGenPreviewPaneProps {
  generatedImage: string
  zoom: number
  setZoom: React.Dispatch<React.SetStateAction<number>>
  revisedPrompt: string | null
  insertSuccess: boolean
  isGenerating: boolean
  onInsert: () => void
  onRegenerate: () => void
}

export const ImageGenPreviewPane: React.FC<ImageGenPreviewPaneProps> = ({
  generatedImage,
  zoom,
  setZoom,
  revisedPrompt,
  insertSuccess,
  isGenerating,
  onInsert,
  onRegenerate,
}) => {
  return (
    <div style={{
      flex: '0 0 320px',
      display: 'flex', flexDirection: 'column', gap: '0.75rem',
      padding: '1rem 1.25rem 1rem 0',
      borderLeft: '1px solid var(--border-color)',
      paddingLeft: '1.25rem',
      overflowY: 'auto',
    }}>
      <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>Generated Image</div>

      <div style={{
        borderRadius: '10px', overflow: 'hidden',
        border: '1px solid var(--border-color)',
        backgroundColor: 'var(--bg-tertiary)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <img
          src={generatedImage}
          alt="Generated"
          style={{
            width: '100%', height: 'auto', maxHeight: '360px',
            objectFit: 'contain',
            transform: `scale(${zoom})`, transformOrigin: 'center',
            transition: 'transform 0.2s', display: 'block',
          }}
        />
      </div>

      {/* Zoom Controls */}
      <div style={{ display: 'flex', gap: '5px', alignItems: 'center' }}>
        <button type="button" onClick={() => setZoom(z => Math.max(0.5, z - 0.1))} className="btn-icon" style={{ padding: '4px' }}>
          <ZoomOut size={13} />
        </button>
        <span style={{ fontSize: '0.77rem', color: 'var(--text-secondary)', minWidth: '2.8rem', textAlign: 'center' }}>
          {Math.round(zoom * 100)}%
        </span>
        <button type="button" onClick={() => setZoom(z => Math.min(2.5, z + 0.1))} className="btn-icon" style={{ padding: '4px' }}>
          <ZoomIn size={13} />
        </button>
        <button type="button" onClick={() => setZoom(1)} className="btn-icon" style={{ padding: '3px 6px', fontSize: '0.72rem' }}>1:1</button>
      </div>

      {/* Revised Prompt Badge */}
      {revisedPrompt && (
        <div style={{
          padding: '0.55rem 0.7rem', borderRadius: '7px',
          backgroundColor: 'rgba(128, 128, 128, 0.08)',
          border: '1px solid rgba(128, 128, 128, 0.25)',
          fontSize: '0.77rem', color: 'var(--text-secondary)',
        }}>
          <span style={{ fontWeight: 600, color: 'var(--accent)', display: 'block', marginBottom: '0.2rem' }}>
            Provider-revised prompt:
          </span>
          {revisedPrompt}
        </div>
      )}

      {/* Insert Button */}
      <button
        type="button"
        onClick={onInsert}
        disabled={insertSuccess}
        style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '0.5rem',
          padding: '0.6rem 1.2rem', borderRadius: '9px', border: 'none', width: '100%',
          background: '#10b981', color: 'white',
          fontSize: '0.88rem', fontWeight: 600,
          cursor: insertSuccess ? 'default' : 'pointer',
          transition: 'all 0.2s',
          boxShadow: '0 3px 10px rgba(16, 185, 129, 0.3)',
        }}
      >
        {insertSuccess ? <><Check size={15} /> Inserted!</> : <><ImagePlus size={15} /> Insert into Editor</>}
      </button>

      {/* Regenerate hint */}
      <button
        type="button"
        onClick={onRegenerate}
        disabled={isGenerating}
        style={secondaryBtnStyle(isGenerating)}
      >
        <RefreshCw size={13} className={isGenerating ? 'animate-spin' : ''} />
        {isGenerating ? 'Regenerating…' : 'Regenerate'}
      </button>

      <p style={{ fontSize: '0.73rem', color: 'var(--text-muted)', textAlign: 'center', margin: 0 }}>
        Edit the prompt above and regenerate, or enhance again.
      </p>
    </div>
  )
}

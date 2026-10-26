/**
 * Shared style factories and micro render helpers for the image generation
 * modal — extracted from ImageGenerationModal.tsx.
 */

import React from 'react'

// ─── Helpers ────────────────────────────────────────────────────────────────

export function textareaStyle(focused: boolean = false): React.CSSProperties {
  return {
    width: '100%',
    padding: '0.6rem 0.75rem',
    borderRadius: '8px',
    border: `1px solid ${focused ? 'var(--accent)' : 'var(--border-color)'}`,
    backgroundColor: 'var(--bg-tertiary)',
    color: 'var(--text-primary)',
    fontSize: '0.855rem',
    resize: 'vertical' as const,
    outline: 'none',
    fontFamily: 'inherit',
    lineHeight: 1.55,
    boxSizing: 'border-box' as const,
    transition: 'border-color 0.18s',
  }
}

export function sectionLabel(text: string, sub?: string) {
  return (
    <label style={{ display: 'block', marginBottom: '0.35rem' }}>
      <span style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>{text}</span>
      {sub && <span style={{ fontSize: '0.73rem', color: 'var(--text-muted)', marginLeft: '0.4rem' }}>{sub}</span>}
    </label>
  )
}

export function pillBtn(active: boolean, onClick: () => void, label: string) {
  return (
    <button
      key={label}
      type="button"
      onClick={onClick}
      style={{
        padding: '3px 10px',
        borderRadius: '6px',
        fontSize: '0.775rem',
        border: active ? '1px solid var(--accent)' : '1px solid var(--border-color)',
        backgroundColor: active ? 'rgba(128, 128, 128, 0.15)' : 'var(--bg-tertiary)',
        color: active ? 'var(--accent)' : 'var(--text-secondary)',
        cursor: 'pointer',
        transition: 'all 0.15s',
        fontWeight: active ? 600 : 400,
        whiteSpace: 'nowrap' as const,
      }}
    >
      {label}
    </button>
  )
}

// ── Render helpers ──

export const accentBtnStyle = (disabled: boolean): React.CSSProperties => ({
  display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '0.45rem',
  padding: '0.55rem 1.1rem', borderRadius: '8px', border: 'none',
  background: disabled ? 'var(--bg-tertiary)' : 'var(--accent)',
  color: disabled ? 'var(--text-muted)' : 'var(--accent-text)',
  fontSize: '0.875rem', fontWeight: 600,
  cursor: disabled ? 'not-allowed' : 'pointer',
  transition: 'all 0.18s',
  boxShadow: disabled ? 'none' : '0 3px 12px rgba(128, 128, 128, 0.35)',
  flex: '0 0 auto',
})

export const secondaryBtnStyle = (disabled = false): React.CSSProperties => ({
  display: 'flex', alignItems: 'center', gap: '0.4rem',
  padding: '0.5rem 0.9rem', borderRadius: '8px',
  border: '1px solid var(--border-color)',
  backgroundColor: 'var(--bg-tertiary)',
  color: disabled ? 'var(--text-muted)' : 'var(--text-secondary)',
  fontSize: '0.82rem', fontWeight: 500,
  cursor: disabled ? 'not-allowed' : 'pointer',
  transition: 'all 0.15s',
  flex: '0 0 auto',
})

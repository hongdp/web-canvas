/**
 * Editor.tsx ↔ store contract around the read-only toggle.
 *
 * TipTap's `setEditable(editable, emitUpdate = true)` emits a synthetic
 * `update` by default, and Editor.tsx's onUpdate publishes editor.getHTML()
 * to the store. The live LLM preview is written with emitUpdate:false so it
 * never reaches the store — flipping `isStreaming` (which toggles read-only)
 * must not undo that. Drives the real component against a real TipTap editor
 * in jsdom, since the leak lives in the wiring between the two.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { createElement, act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { Editor } from '../Editor'
import { useAppStore } from '../../store/useAppStore'

// jsdom has no ResizeObserver; the bubble menu's positioning wants one only
// when it shows, but a no-op keeps any code path from throwing on lookup.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

let root: Root | null = null
let container: HTMLDivElement | null = null

const renderEditor = (onChange: (html: string) => void) => {
  container = document.createElement('div')
  document.body.appendChild(container)
  act(() => {
    root = createRoot(container!)
    root.render(createElement(Editor, {
      content: '<p>old text</p>',
      onChange,
      isActive: true,
      documentId: 'doc-1'
    }))
  })
  const editor = useAppStore.getState().activeEditor
  if (!editor) throw new Error('Editor did not register itself as the active editor')
  return editor
}

beforeEach(() => {
  useAppStore.setState({ isStreaming: false, activeEditor: null, selectedText: '' })
})

afterEach(() => {
  act(() => { root?.unmount() })
  container?.remove()
  root = null
  container = null
})

describe('Editor — read-only toggling while streaming', () => {
  it('publishes nothing to the store when isStreaming flips around a live preview', () => {
    const onChange = vi.fn()
    const editor = renderEditor(onChange)

    // A send: the editor goes read-only.
    act(() => { useAppStore.getState().setStreaming(true) })
    // The hook's live preview: a write that must stay out of the store.
    editor.chain().setMeta('addToHistory', false).setContent('<p>half-streamed draft</p>', { emitUpdate: false }).run()
    expect(editor.getHTML()).toBe('<p>half-streamed draft</p>')
    // Stop: streaming ends while the preview is still on screen.
    act(() => { useAppStore.getState().setStreaming(false) })

    expect(editor.isEditable).toBe(true)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('still publishes real content changes', () => {
    const onChange = vi.fn()
    const editor = renderEditor(onChange)

    act(() => { editor.commands.setContent('<p>typed</p>') })

    expect(onChange).toHaveBeenCalledWith('<p>typed</p>')
  })
})

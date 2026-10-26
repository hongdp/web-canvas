/**
 * replaceSelectionWithHtml against a real TipTap editor.
 *
 * The end it returns must be where the insert really ends, not where the
 * slice's own size says it should. ProseMirror wraps an open slice inserted
 * at document level (a Ctrl+A selection starts at 0) in a fresh paragraph,
 * so the two differ by two — and a live preview that trusted the slice left
 * a residue of the previous partial behind on every tick.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import { CustomImage } from '../../components/editorExtensions'
import { replaceSelectionWithHtml, isSilentPreview } from '../chat/selectionReplace'

let editor: Editor | null = null
const make = (content: string) => {
  editor = new Editor({ element: document.createElement('div'), extensions: [StarterKit, CustomImage], content })
  return editor
}
afterEach(() => {
  editor?.destroy()
  editor = null
})

/** The live preview's loop: each partial replaces [from, end of the previous one]. */
function stream(e: Editor, from: number, to: number, partials: string[]): number {
  let end = to
  for (const html of partials) {
    const next = replaceSelectionWithHtml(e, from, end, html)
    if (next === null) throw new Error(`range lost at ${html}`)
    end = next
  }
  return end
}

const PARTIALS = [
  '<p>X</p>', '<p>XY</p>', '<p>XY</p><p>Z</p>', '<p>XY</p><p>ZW</p>',
  '<p>XY</p><p>ZW</p><p>Q</p>', '<p>XY</p><p>ZW</p><p>QR</p>'
]

describe('replaceSelectionWithHtml', () => {
  it('leaves no residue when the selection is the whole document (Ctrl+A, from = 0)', () => {
    const e = make('<p>AAA</p><p>BBB</p>')
    stream(e, 0, e.state.doc.content.size, PARTIALS)
    expect(e.getHTML()).toBe('<p>XY</p><p>ZW</p><p>QR</p>')
  })

  it('reports the end of the insert, not the size of the open slice', () => {
    const e = make('<p>AAA</p><p>BBB</p>')
    const end = replaceSelectionWithHtml(e, 0, e.state.doc.content.size, '<p>X</p>')
    // doc(paragraph("X")) spans 3 positions; the open slice alone was 1.
    expect(end).toBe(3)
    expect(end).toBe(e.state.doc.content.size)
  })

  it('keeps the text around an in-paragraph selection', () => {
    const e = make('<p>AAA</p><p>BBB</p><p>CCC</p>')
    stream(e, 6, 9, PARTIALS)
    expect(e.getHTML()).toBe('<p>AAA</p><p>XY</p><p>ZW</p><p>QR</p><p>CCC</p>')
  })

  it('handles image paragraphs at position 0', () => {
    const e = make('<p>AAA</p><p>BBB</p>')
    stream(e, 0, e.state.doc.content.size, [
      '<p><img src="a.png"></p>',
      '<p><img src="a.png"></p><p>Z</p>',
      '<p><img src="a.png"></p><p>ZW</p>'
    ])
    expect(e.getHTML()).toMatch(/^<p><img[^>]*src="a\.png"[^>]*><\/p><p>ZW<\/p>$/)
  })

  it('returns null and writes nothing when the range no longer fits', () => {
    const e = make('<p>AAA</p>')
    expect(replaceSelectionWithHtml(e, 50, 60, '<p>X</p>')).toBeNull()
    expect(e.getHTML()).toBe('<p>AAA</p>')
  })

  // A server run's preview must reach the screen but not the store: the
  // flag rides on the transaction, where Editor.tsx's onUpdate reads it.
  it('flags a silent preview on its transaction, and nothing else', () => {
    const seen: boolean[] = []
    editor = new Editor({
      element: document.createElement('div'), extensions: [StarterKit, CustomImage], content: '<p>AAA</p>',
      onUpdate: ({ transaction }) => { seen.push(isSilentPreview(transaction)) }
    })
    expect(replaceSelectionWithHtml(editor, 1, 4, '<p>X</p>', { silent: true })).toBe(2)
    expect(editor.getHTML()).toBe('<p>X</p>')
    replaceSelectionWithHtml(editor, 1, 2, '<p>Y</p>')
    editor.commands.insertContentAt(2, 'Z')
    expect(editor.getHTML()).toBe('<p>YZ</p>')
    expect(seen).toEqual([true, false, false])
  })
})

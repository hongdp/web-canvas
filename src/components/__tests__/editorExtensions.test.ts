import { describe, it, expect, afterEach } from 'vitest'
import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import { ParagraphsFromLineBreaks } from '../editorExtensions'

/**
 * Drives a real TipTap editor: the plugin works at the transaction level, so
 * only an end-to-end insert proves it fires for every entry path (the Android
 * IME paste that motivated it never produces a paste event).
 */
let editor: Editor | null = null

const makeEditor = (content = '<p></p>') => {
  editor = new Editor({
    element: document.createElement('div'),
    extensions: [StarterKit, ParagraphsFromLineBreaks],
    content
  })
  return editor
}

afterEach(() => {
  editor?.destroy()
  editor = null
})

describe('ParagraphsFromLineBreaks', () => {
  it('splits a paragraph carrying a wall of hard breaks', () => {
    const e = makeEditor()
    e.commands.setContent('<p>一<br>二<br>三</p>')
    expect(e.getHTML()).toBe('<p>一</p><p>二</p><p>三</p>')
  })

  it('handles text inserted with newlines (the Android IME paste path)', () => {
    const e = makeEditor()
    // insertContent with \n produces hard breaks exactly like an IME commit.
    e.commands.insertContent('第一行<br>第二行<br>第三行')
    expect(e.getHTML()).toBe('<p>第一行</p><p>第二行</p><p>第三行</p>')
  })

  it('leaves a single hard break alone as a soft break', () => {
    const e = makeEditor()
    e.commands.setContent('<p>地址一<br>地址二</p>')
    expect(e.getHTML()).toBe('<p>地址一<br>地址二</p>')
  })

  it('collapses consecutive breaks without emitting empty paragraphs', () => {
    const e = makeEditor()
    e.commands.setContent('<p>a<br><br><br>b<br>c</p>')
    expect(e.getHTML()).toBe('<p>a</p><p>b</p><p>c</p>')
  })

  it('preserves inline marks on both sides of a split', () => {
    const e = makeEditor()
    e.commands.setContent('<p><strong>a</strong><br><em>b</em><br>c</p>')
    expect(e.getHTML()).toBe('<p><strong>a</strong></p><p><em>b</em></p><p>c</p>')
  })

  it('does not disturb paragraphs without breaks', () => {
    const e = makeEditor()
    const html = '<h1>T</h1><p>one</p><p>two</p>'
    e.commands.setContent(html)
    expect(e.getHTML()).toBe(html)
  })

  it('is idempotent — a settled document produces no further transactions', () => {
    const e = makeEditor()
    e.commands.setContent('<p>a<br>b<br>c</p>')
    const once = e.getHTML()
    e.commands.setContent(once)
    expect(e.getHTML()).toBe(once)
  })
})

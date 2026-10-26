/**
 * Reviewing a chapter the agent created: every block is a pending addition,
 * so rejecting it leaves the editor's empty `<p></p>`. The store's blanking
 * guard used to refuse that write — the editor went blank, the store kept
 * the text, the review banner stayed, and the chapter came back on reload.
 *
 * A real TipTap editor (the diff ranges are ProseMirror positions) and the
 * real store; the editor's onUpdate is App's onChange (commitEditorHtml).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { createElement, act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import { CustomImage, DiffAddition, DiffDeletion } from '../../components/editorExtensions'
import { diffHtml } from '../../utils/diff'
import { collectDiffRanges } from '../../utils/diffResolution'
import { useDiffHandlers } from '../useDiffHandlers'
import { useAppStore } from '../../store/useAppStore'
import { commitEditorHtml } from '../../store/editorChange'

const CHAPTER = '<h1>雨夜</h1><p>雨下了一整夜。</p><p>天亮时，街上的积水映着灰白的云。</p>'

let editor: Editor | null = null
let root: Root | null = null
/** The blanking guard's refusals (React's act() warnings land on console.error too). */
const refusals = () => vi.mocked(console.error).mock.calls.filter(c => String(c[0]).includes('Refused to blank'))

/** Mirrors Editor.tsx: the extensions that matter here, and onUpdate → App's onChange. */
function makeEditor(content: string, { wired = true } = {}) {
  editor = new Editor({
    element: document.createElement('div'),
    extensions: [StarterKit.configure({ strike: false }), DiffAddition, DiffDeletion, CustomImage],
    content,
    ...(wired ? { onUpdate: ({ editor: e }) => commitEditorHtml('doc-1', e.getHTML()) } : {})
  })
  return editor
}

/** A chapter the agent wrote from nothing, open in the editor and the store. */
function openAgentChapter(opts?: { wired?: boolean }) {
  const e = makeEditor(diffHtml('<p></p>', CHAPTER), opts)
  const markup = e.getHTML()
  expect(markup).toContain('data-diff-id')
  expect(markup).not.toContain('<del')
  useAppStore.setState({
    documents: [{ id: 'doc-1', title: '雨夜', content: markup, contentLoaded: true, createdAt: '', updatedAt: '' }] as never,
    activeDocumentId: 'doc-1'
  })
  return e
}

function renderDiffHandlers(e: Editor | null) {
  const harness = { current: null as unknown as ReturnType<typeof useDiffHandlers> }
  const Probe = () => {
    const s = useAppStore()
    const doc = s.documents.find(d => d.id === s.activeDocumentId)!
    harness.current = useDiffHandlers(e as never, doc, s.updateActiveDocument, noop)
    return null
  }
  act(() => { root = createRoot(document.createElement('div')); root.render(createElement(Probe)) })
  return harness
}
const noop = () => {}

/** Mirrors Editor.tsx handleResolveDiff: one hunk, then onChange with the editor's HTML. */
function resolveHunk(e: Editor, diffId: string) {
  const { state, view } = e
  const tr = state.tr
  collectDiffRanges(state.doc, 'reject', diffId).forEach(range => {
    if (range.op === 'delete') tr.delete(range.from, range.to)
    else tr.removeMark(range.from, range.to, state.schema.marks[range.mark])
  })
  view.dispatch(tr)
  commitEditorHtml('doc-1', e.getHTML())
}

const stored = () => useAppStore.getState().documents[0]
/** App's review banner shows while the chapter holds diff markup. */
const bannerShown = () => stored().content.includes('data-diff-id')

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => {
  act(() => root?.unmount())
  root = null
  editor?.destroy()
  editor = null
  vi.restoreAllMocks()
})

describe('Reject All on a chapter the agent created', () => {
  it('empties the chapter in the store and clears the review banner', () => {
    const e = openAgentChapter()
    const handlers = renderDiffHandlers(e)
    act(() => handlers.current.handleRejectAllDiffs())

    expect(e.getHTML()).toBe('<p></p>')
    expect(stored().content).toBe('<p></p>')
    expect(bannerShown()).toBe(false)
    expect(stored().unsynced).toBe(true)
    // The heading went with the text; the title is never cleared by it.
    expect(stored().title).toBe('雨夜')
    expect(refusals()).toEqual([])
  })

  it('writes the empty chapter itself, not only through the editor', () => {
    // The hook's own write, with the editor's onUpdate not reaching the store.
    const e = openAgentChapter({ wired: false })
    const handlers = renderDiffHandlers(e)
    act(() => handlers.current.handleRejectAllDiffs())

    expect(stored().content).toBe('<p></p>')
    expect(bannerShown()).toBe(false)
  })

  it('empties it from the stored markup when no editor is open', () => {
    const e = openAgentChapter({ wired: false })
    const handlers = renderDiffHandlers(null)
    expect(e.getHTML()).toContain('data-diff-id')
    act(() => handlers.current.handleRejectAllDiffs())

    expect(stored().content).not.toContain('雨')
    expect(bannerShown()).toBe(false)
  })
})

describe('rejecting the hunks of such a chapter one by one', () => {
  it('lets the last rejection empty the chapter, the store following the editor', () => {
    const e = openAgentChapter()
    const ids = [...new Set([...e.getHTML().matchAll(/data-diff-id="([^"]+)"/g)].map(m => m[1]))]
    expect(ids.length).toBeGreaterThan(1)

    for (const id of ids) {
      resolveHunk(e, id)
      expect(stored().content).toBe(e.getHTML())
    }
    expect(stored().content).toBe('<p></p>')
    expect(bannerShown()).toBe(false)
    expect(refusals()).toEqual([])
  })
})

describe('the editor emptying a chapter by hand', () => {
  it('reaches the store (select all + delete)', () => {
    const e = makeEditor('<p>雨下了一整夜。</p>')
    useAppStore.setState({
      documents: [{ id: 'doc-1', title: '雨', content: e.getHTML(), contentLoaded: true, createdAt: '', updatedAt: '' }] as never,
      activeDocumentId: 'doc-1'
    })
    e.chain().selectAll().deleteSelection().run()

    expect(e.getHTML()).toBe('<p></p>')
    expect(stored().content).toBe('<p></p>')
  })
})

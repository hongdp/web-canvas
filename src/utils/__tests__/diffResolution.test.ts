import { describe, it, expect, afterEach } from 'vitest'
import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import { CustomImage, DiffAddition, DiffDeletion } from '../../components/editorExtensions'
import { diffHtml } from '../diff'
import { collectDiffRanges, resolveDiffMarkupInHtml, type DiffAction } from '../diffResolution'

/**
 * Drives a real TipTap editor: the ranges are positions into a ProseMirror
 * document, so only round-tripping through the actual schema proves that a
 * rejected addition takes its wrapper block with it.
 */
let editor: Editor | null = null

const makeEditor = (content: string) => {
  editor = new Editor({
    element: document.createElement('div'),
    // Mirrors Editor.tsx: StarterKit's strike also claims <del>, so it is off there.
    extensions: [StarterKit.configure({ strike: false }), DiffAddition, DiffDeletion, CustomImage],
    content
  })
  return editor
}

/** Mirrors the apply loop in Editor.tsx / useDiffHandlers.ts. */
const resolve = (e: Editor, action: DiffAction, diffId?: string) => {
  const { state, view } = e
  const tr = state.tr
  collectDiffRanges(state.doc, action, diffId).forEach(range => {
    if (range.op === 'delete') {
      tr.delete(range.from, range.to)
    } else {
      tr.removeMark(range.from, range.to, state.schema.marks[range.mark])
    }
  })
  view.dispatch(tr)
  return e.getHTML()
}

afterEach(() => {
  editor?.destroy()
  editor = null
})

describe('collectDiffRanges — reject', () => {
  it('restores the original bytes when the diff introduced a new paragraph', () => {
    const original = '<p>Chapter one.</p>'
    const updated = '<p>Chapter one.</p><p>Brand new paragraph.</p>'
    const e = makeEditor(diffHtml(original, updated))

    expect(resolve(e, 'reject')).toBe(original)
  })

  it('leaves no blank paragraph between surviving blocks', () => {
    const original = '<p>First.</p><p>Last.</p>'
    const updated = '<p>First.</p><p>Inserted.</p><p>Last.</p>'
    const e = makeEditor(diffHtml(original, updated))

    const html = resolve(e, 'reject')
    expect(html).toBe(original)
    expect(html).not.toContain('<p></p>')
  })

  it('keeps the paragraph when only part of its text was added', () => {
    const original = '<p>Hello</p>'
    const updated = '<p>Hello world</p>'
    const e = makeEditor(diffHtml(original, updated))

    expect(resolve(e, 'reject')).toBe(original)
  })

  it('restores deleted text and drops the added block in one pass', () => {
    const original = '<p>Keep this.</p><p>Doomed sentence.</p>'
    const updated = '<p>Keep this.</p><p>Doomed sentence.</p><p>Extra.</p>'
    const e = makeEditor(
      '<p>Keep this.</p><p><del class="diff-deletion" data-diff-id="d1">Doomed sentence.</del></p>' +
        '<p><ins class="diff-addition" data-diff-id="d2">Extra.</ins></p>'
    )
    expect(updated).toContain('Extra.')

    expect(resolve(e, 'reject')).toBe(original)
  })

  it('removes an added list item together with the list it created', () => {
    const e = makeEditor(
      '<p>Intro</p><ul><li><p><ins class="diff-addition" data-diff-id="d1">New bullet</ins></p></li></ul>'
    )

    expect(resolve(e, 'reject')).toBe('<p>Intro</p>')
  })

  it('keeps an existing list when only one of its items was added', () => {
    // Trailing paragraph on purpose: StarterKit appends one after any
    // transaction that leaves the document ending in a list.
    const e = makeEditor(
      '<ul><li><p>Old bullet</p></li><li><p><ins class="diff-addition" data-diff-id="d1">New bullet</ins></p></li></ul><p>After</p>'
    )

    expect(resolve(e, 'reject')).toBe('<ul><li><p>Old bullet</p></li></ul><p>After</p>')
  })

  it('keeps a block whose only surviving text is a deleted-marked nbsp', () => {
    // An intentional blank line (`<p>&nbsp;</p>`) that the model filled in: the
    // nbsp carries diffDeletion, so reject must restore it, and the block has to
    // survive to hold it even though JS trims a nbsp to nothing.
    const e = makeEditor(
      '<p>Keep.</p><p><del class="diff-deletion" data-diff-id="d1">&nbsp;</del><ins class="diff-addition" data-diff-id="d1">added</ins></p>'
    )

    const html = resolve(e, 'reject')
    expect(html).not.toContain('added')
    expect(html).toBe('<p>Keep.</p><p>&nbsp;</p>')
  })

  it('keeps the block when it also holds an inline image the diff did not add', () => {
    const e = makeEditor(
      '<p><img src="data:image/png;base64,AAA"><ins class="diff-addition" data-diff-id="d1">caption</ins></p>'
    )

    const html = resolve(e, 'reject')
    expect(html).toContain('<img')
    expect(html).not.toContain('caption')
  })

  it('leaves the last block in place rather than emptying the document', () => {
    const e = makeEditor('<p><ins class="diff-addition" data-diff-id="d1">Everything</ins></p>')

    const html = resolve(e, 'reject')
    expect(html).not.toContain('Everything')
    expect(html).toBe('<p></p>')
  })

  it('resolves only the requested hunk when a diff id is given', () => {
    const e = makeEditor(
      '<p>Base.</p><p><ins class="diff-addition" data-diff-id="d1">One.</ins></p>' +
        '<p><ins class="diff-addition" data-diff-id="d2">Two.</ins></p>'
    )

    const html = resolve(e, 'reject', 'd1')
    expect(html).toBe('<p>Base.</p><p><ins data-diff-id="d2" class="diff-addition">Two.</ins></p>')
  })
})

describe('collectDiffRanges — accept', () => {
  it('drops the block whose entire text was marked deleted', () => {
    const e = makeEditor(
      '<p>Kept.</p><p><del class="diff-deletion" data-diff-id="d1">Removed paragraph.</del></p>'
    )

    expect(resolve(e, 'accept')).toBe('<p>Kept.</p>')
  })

  it('unwraps additions and keeps their new paragraph', () => {
    const original = '<p>Chapter one.</p>'
    const updated = '<p>Chapter one.</p><p>Brand new paragraph.</p>'
    const e = makeEditor(diffHtml(original, updated))

    expect(resolve(e, 'accept')).toBe(updated)
  })

  it('keeps a partially deleted paragraph', () => {
    const e = makeEditor('<p>Keep <del class="diff-deletion" data-diff-id="d1">drop</del>this</p>')

    expect(resolve(e, 'accept')).toBe('<p>Keep this</p>')
  })
})

describe('resolveDiffMarkupInHtml (no editor mounted)', () => {
  it('rejects an added paragraph without leaving an empty block', () => {
    const html =
      '<p>Chapter one.</p><p><ins class="diff-addition" data-diff-id="d1">Brand new.</ins></p>'

    expect(resolveDiffMarkupInHtml(html, 'reject')).toBe('<p>Chapter one.</p>')
  })

  it('rejects an inline addition and restores deleted text', () => {
    const html =
      '<p>Hello <del class="diff-deletion" data-diff-id="d1">old</del><ins class="diff-addition" data-diff-id="d1">new</ins></p>'

    expect(resolveDiffMarkupInHtml(html, 'reject')).toBe('<p>Hello old</p>')
  })

  it('accepts a deleted paragraph without leaving an empty block', () => {
    const html =
      '<p>Kept.</p><p><del class="diff-deletion" data-diff-id="d1">Removed.</del></p>'

    expect(resolveDiffMarkupInHtml(html, 'accept')).toBe('<p>Kept.</p>')
  })

  it('accepts an added paragraph by unwrapping it in place', () => {
    const html =
      '<p>Chapter one.</p><p><ins class="diff-addition" data-diff-id="d1">Brand new.</ins></p>'

    expect(resolveDiffMarkupInHtml(html, 'accept')).toBe('<p>Chapter one.</p><p>Brand new.</p>')
  })

  it('collapses a list item emptied by the rejection', () => {
    const html =
      '<p>Intro</p><ul><li><p><ins class="diff-addition" data-diff-id="d1">New bullet</ins></p></li></ul>'

    expect(resolveDiffMarkupInHtml(html, 'reject')).toBe('<p>Intro</p>')
  })

  it('keeps text that sits between two insertions in the same block', () => {
    // Exactly what diffHtml emits for '<p>A</p>' -> '<p>X A Y</p>'. The block is
    // not empty afterwards — "A" is the original and must survive.
    const html = diffHtml('<p>A</p>', '<p>X A Y</p>')
    expect(html).toMatch(/^<p><ins[^>]*>X <\/ins>A<ins[^>]*> Y<\/ins><\/p>$/)

    expect(resolveDiffMarkupInHtml(html, 'reject')).toBe('<p>A</p>')
  })

  it('leaves content without diff markup untouched', () => {
    const html = '<p>Nothing pending</p><p></p>'

    expect(resolveDiffMarkupInHtml(html, 'reject')).toBe(html)
  })
})

describe('pictures under review (2026-10-18)', () => {
  const PIC = '<img src="https://pics.example/1.jpg">'

  it('marks a placed picture so Reject takes it out and Accept keeps it', () => {
    const original = '<p>Chapter one.</p>'
    const updated = `<p>Chapter one.</p><p>${PIC}</p><p>New text.</p>`
    const diffed = diffHtml(original, updated)
    expect(diffed).toMatch(/<ins class="diff-addition" data-diff-id="[^"]+"><img src="https:\/\/pics\.example\/1\.jpg"><\/ins>/)
    expect(resolveDiffMarkupInHtml(diffed, 'reject')).toBe(original)
    expect(resolveDiffMarkupInHtml(diffed, 'accept')).toBe(updated)
    expect(resolve(makeEditor(diffed), 'reject')).toBe(original)
  })

  it('keeps a picture placed by the editor until Accept', () => {
    const updated = `<p>Chapter one.</p><p>${PIC}</p>`
    const html = resolve(makeEditor(diffHtml('<p>Chapter one.</p>', updated)), 'accept')
    expect(html).toBe(updated)
  })

  it('shows a removed picture as a deletion that Reject brings back', () => {
    const original = `<p>She opened the door. ${PIC}</p>`
    const updated = '<p>She opened the door.</p>'
    const diffed = diffHtml(original, updated)
    expect(diffed).toContain(`<del class="diff-deletion"`)
    expect(diffed).toContain(PIC)
    expect(resolveDiffMarkupInHtml(diffed, 'accept')).not.toContain('<img')
    expect(resolveDiffMarkupInHtml(diffed, 'reject')).toContain(PIC)
    expect(resolve(makeEditor(diffed), 'reject')).toContain('<img src="https://pics.example/1.jpg">')
    // One editor per test: a second makeEditor would leave the first undestroyed, and its timers
    // firing after the environment is torn down fail the run ("document is not defined").
    editor?.destroy()
    editor = null
    expect(resolve(makeEditor(diffed), 'accept')).not.toContain('<img')
  })
})

import { describe, it, expect } from 'vitest'
import { findTextRangeInSpans, collectTextSpans, selectionNeedleFromHtml, type TextNodeSpan } from '../chat/streamHandlers'

// A reload destroys the selection range (it lived in a ref), so a selection
// rewrite that finishes afterwards has to find its passage again. The first
// version of this did string arithmetic on HTML and was wrong: a plain-text
// index is NOT a ProseMirror position — every block boundary adds one — so the
// error grew with each preceding paragraph and the rewrite landed off-target.
describe('findTextRangeInSpans', () => {
  // Positions as ProseMirror assigns them: <p>keep</p><p>选中的原文</p>
  const spans: TextNodeSpan[] = [
    { text: 'keep', from: 1 },
    { text: '选中的原文', from: 7 }
  ]

  it('returns real document positions, not string offsets', () => {
    const r = findTextRangeInSpans(spans, '选中的原文')
    // Off-by-block arithmetic would have said 6; the node tree says 7.
    expect(r).toEqual({ from: 7, to: 12 })
  })

  it('finds a passage that spans two text nodes', () => {
    // Marks split text: <p>拧开<strong>生锈</strong>的接头</p>
    const split: TextNodeSpan[] = [
      { text: '拧开', from: 1 },
      { text: '生锈', from: 3 },
      { text: '的接头', from: 5 }
    ]
    expect(findTextRangeInSpans(split, '生锈的接头')).toEqual({ from: 3, to: 8 })
  })

  it('refuses an ambiguous passage', () => {
    const twice: TextNodeSpan[] = [
      { text: '重复的句子', from: 1 },
      { text: '别的', from: 7 },
      { text: '重复的句子', from: 11 }
    ]
    expect(findTextRangeInSpans(twice, '重复的句子')).toBeNull()
  })

  it('refuses a passage that is gone', () => {
    expect(findTextRangeInSpans(spans, '早已删除的文字')).toBeNull()
  })

  it('handles empty input', () => {
    expect(findTextRangeInSpans([], 'x')).toBeNull()
    expect(findTextRangeInSpans(spans, '')).toBeNull()
    expect(findTextRangeInSpans(spans, '   ')).toBeNull()
  })
})

describe('collectTextSpans', () => {
  it('keeps only text nodes, with their positions', () => {
    const doc = {
      descendants(fn: (node: { isText?: boolean; text?: string }, pos: number) => void) {
        fn({ isText: false }, 0)
        fn({ isText: true, text: 'hello' }, 1)
        fn({ isText: false }, 6)
        fn({ isText: true, text: 'world' }, 8)
      }
    }
    expect(collectTextSpans(doc)).toEqual([
      { text: 'hello', from: 1 },
      { text: 'world', from: 8 }
    ])
  })
})

/**
 * Why a resumed rewrite of TWO paragraphs used to lose its preview and its
 * diff while ONE paragraph was fine — measured in the running app, not
 * guessed. Editor.tsx publishes the selection as serialized HTML:
 *
 *   selection inside one block  -> "船靠了岸。缆绳轻轻一响…"      (no tags)
 *   selection across two blocks -> "<p>一</p><p>二</p>"            (block tags)
 *
 * The haystack is the document flattened to text with no tags, so the first
 * matched by luck and the second could never match. That asymmetry — not
 * model randomness — is what made the bug look like it depended on how much
 * the user selected.
 */
describe('selectionNeedleFromHtml', () => {
  it('passes a tagless selection through unchanged', () => {
    expect(selectionNeedleFromHtml('船靠了岸。缆绳轻轻一响')).toBe('船靠了岸。缆绳轻轻一响')
  })

  it('reduces a multi-block selection to what the haystack holds', () => {
    // No separator between blocks: collectTextSpans concatenates text nodes,
    // so a space or newline here would break the very case this fixes.
    expect(selectionNeedleFromHtml('<p>第一段</p><p>第二段</p>')).toBe('第一段第二段')
  })

  it('drops inline markup that the flattened text does not carry', () => {
    expect(selectionNeedleFromHtml('<p>a <strong>bold</strong> b</p>')).toBe('a bold b')
  })

  it('decodes entities, which the document stores as characters', () => {
    expect(selectionNeedleFromHtml('<p>a &amp; b</p>')).toBe('a & b')
  })
})

describe('relocating a selection that spans blocks', () => {
  // Two paragraphs, as ProseMirror positions them: <p>第一段</p><p>第二段</p>
  const spans = [{ text: '第一段', from: 1 }, { text: '第二段', from: 6 }]

  it('finds a two-block selection from its serialized HTML', () => {
    expect(findTextRangeInSpans(spans, '<p>第一段</p><p>第二段</p>')).toEqual({ from: 1, to: 9 })
  })
})

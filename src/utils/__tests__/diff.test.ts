import { describe, it, expect } from 'vitest'
import { stripDiffMarkup, diffHtml } from '../diff'

describe('diffHtml', () => {
  it('returns the original HTML unchanged when inputs are identical', () => {
    const html = '<p>Hello world</p>'
    const result = diffHtml(html, html)
    expect(result).toBe(html)
  })

  it('wraps new text in <ins> when content is purely added', () => {
    const result = diffHtml('<p>Hello</p>', '<p>Hello world</p>')
    expect(result).toContain('<ins')
    expect(result).toContain('world')
    expect(result).not.toContain('<del')
  })

  it('wraps removed text in <del> when content is purely deleted', () => {
    const result = diffHtml('<p>Hello world</p>', '<p>Hello</p>')
    expect(result).toContain('<del')
    expect(result).toContain('world')
    expect(result).not.toContain('<ins')
  })

  it('produces both <ins> and <del> for mixed changes', () => {
    const result = diffHtml('<p>foo bar</p>', '<p>foo baz</p>')
    expect(result).toContain('<del')
    expect(result).toContain('<ins')
    expect(result).toContain('baz')
  })

  it('handles completely empty old HTML (pure insertion)', () => {
    const result = diffHtml('', '<p>Hello</p>')
    expect(result).toContain('<ins')
    expect(result).not.toContain('<del')
  })

  it('handles completely empty new HTML (pure deletion)', () => {
    const result = diffHtml('<p>Hello</p>', '')
    expect(result).toContain('<del')
    expect(result).not.toContain('<ins')
  })

  it('handles both inputs empty', () => {
    const result = diffHtml('', '')
    expect(result).toBe('')
  })

  it('preserves HTML tags as structural tokens (does not wrap tags in del/ins)', () => {
    const result = diffHtml('<p>old</p>', '<p>new</p>')
    // The <p> and </p> tags themselves should not appear inside del/ins
    // The del/ins elements should contain only text content
    const delMatches = result.match(/<del[^>]*>(.*?)<\/del>/gs) || []
    delMatches.forEach(del => {
      expect(del).not.toContain('<p>')
      expect(del).not.toContain('</p>')
    })
  })

  it('includes a data-diff-id attribute on ins and del for UI grouping', () => {
    const result = diffHtml('<p>Hello world</p>', '<p>Hello earth</p>')
    expect(result).toMatch(/data-diff-id="diff-/)
  })

  it('handles whitespace-only changes', () => {
    // Adding extra spaces between words
    const result = diffHtml('<p>a b</p>', '<p>a  b</p>')
    expect(typeof result).toBe('string')
    // Should not throw
  })

  it('handles Chinese text diff', () => {
    const result = diffHtml('<p>你好世界</p>', '<p>你好中国</p>')
    expect(result).toContain('<del')
    expect(result).toContain('<ins')
  })

  it('preserves common prefix and suffix without marking them as changes', () => {
    const result = diffHtml('<p>AAA BBB CCC</p>', '<p>AAA ZZZ CCC</p>')
    // AAA and CCC should appear outside ins/del
    expect(result).toContain('AAA')
    expect(result).toContain('CCC')
    // The changed part should be marked
    expect(result).toContain('<del')
    expect(result).toContain('<ins')
  })

  it('only marks the changed block in a multi-paragraph document', () => {
    const oldHtml = '<p>First paragraph stays.</p><p>Second changes here.</p><p>Third stays too.</p>'
    const newHtml = '<p>First paragraph stays.</p><p>Second is rewritten now.</p><p>Third stays too.</p>'
    const result = diffHtml(oldHtml, newHtml)
    // Unchanged paragraphs must appear verbatim, with no diff markup around them.
    expect(result).toContain('<p>First paragraph stays.</p>')
    expect(result).toContain('<p>Third stays too.</p>')
    // Only the middle paragraph carries ins/del.
    expect(result).toContain('<del')
    expect(result).toContain('<ins')
    // The untouched words are not wrapped in ins/del.
    expect(result).not.toMatch(/<(ins|del)[^>]*>[^<]*First/)
    expect(result).not.toMatch(/<(ins|del)[^>]*>[^<]*Third/)
  })

  it('handles inserted and deleted whole paragraphs', () => {
    const oldHtml = '<p>Keep one.</p><p>Delete me.</p>'
    const newHtml = '<p>Keep one.</p><p>Brand new.</p>'
    const result = diffHtml(oldHtml, newHtml)
    expect(result).toContain('<p>Keep one.</p>')
    expect(result).toContain('<del')
    expect(result).toContain('<ins')
  })

  it('diffs a small change inside a large document quickly and minimally', () => {
    // 800-paragraph document; only one paragraph changes. The block-level pass
    // should keep this fast and produce a tiny diff (no whole-doc token LCS).
    const paras = (changed: boolean) =>
      Array.from({ length: 800 }, (_, i) =>
        i === 400
          ? `<p>paragraph ${i} ${changed ? 'edited' : 'original'} text here</p>`
          : `<p>paragraph ${i} body text that stays the same across versions</p>`
      ).join('')
    const start = performance.now()
    const result = diffHtml(paras(false), paras(true))
    const elapsed = performance.now() - start
    expect(elapsed).toBeLessThan(300)
    // Exactly one change region.
    const delCount = (result.match(/<del/g) || []).length
    const insCount = (result.match(/<ins/g) || []).length
    expect(delCount).toBe(1)
    expect(insCount).toBe(1)
    expect(result).toContain('original')
    expect(result).toContain('edited')
  })

  it('diffs a large full rewrite quickly via the coarse fallback (no O(n^2) freeze)', () => {
    // Two large, almost-entirely-different documents with no shared prefix/suffix
    // would otherwise build a multi-million-cell LCS matrix and block for seconds.
    const oldHtml = '<p>' + Array.from({ length: 4000 }, (_, i) => `old${i}`).join(' ') + '</p>'
    const newHtml = '<p>' + Array.from({ length: 4000 }, (_, i) => `new${i}`).join(' ') + '</p>'
    const start = performance.now()
    const result = diffHtml(oldHtml, newHtml)
    const elapsed = performance.now() - start
    // Coarse path is effectively instant; assert it stays well under a second.
    expect(elapsed).toBeLessThan(500)
    expect(result).toContain('<del')
    expect(result).toContain('<ins')
    // All old words removed, all new words inserted.
    expect(result).toContain('old0')
    expect(result).toContain('new0')
  })
})

// ── stripDiffMarkup ──────────────────────────────────────────────────────────
// Review markup lives in the stored content, and that content is what the
// model receives. Observed on a real turn: it copied
// `<ins data-diff-id="diff-abc123" class="diff-addition">` into an edit's
// search string, which stops matching the moment that diff is resolved.
describe('stripDiffMarkup', () => {
  it('keeps insertions, which are what the draft now says', () => {
    const html = '<p>before <ins class="diff-addition" data-diff-id="d1">added</ins> after</p>'
    expect(stripDiffMarkup(html)).toBe('<p>before added after</p>')
  })

  it('drops deletions, which the draft no longer says', () => {
    const html = '<p>kept <del class="diff-deletion" data-diff-id="d1">removed</del>text</p>'
    expect(stripDiffMarkup(html)).toBe('<p>kept text</p>')
  })

  it('handles a document with both, across attribute orders', () => {
    const html = '<p><del data-diff-id="d1" class="diff-deletion">old</del><ins data-diff-id="d2" class="diff-addition">new</ins></p>'
    expect(stripDiffMarkup(html)).toBe('<p>new</p>')
  })

  it('leaves ordinary markup — including unrelated ins/del — alone', () => {
    const html = '<p>a <ins>manual insert</ins> and <strong>bold</strong></p>'
    expect(stripDiffMarkup(html)).toBe(html)
  })

  it('returns clean input untouched and survives empty input', () => {
    expect(stripDiffMarkup('<p>plain</p>')).toBe('<p>plain</p>')
    expect(stripDiffMarkup('')).toBe('')
  })
})

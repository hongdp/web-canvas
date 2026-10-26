import { describe, it, expect } from 'vitest'
import { getTimestampId, chapterAttribute, newChapterAttribute, stripIncompleteEndTag, extractTaggedBlock, hasElisionMarkers, validateCanvasReplacement, parseEditBlocks, applyEditBlocks, applyEditBlocksLocally, parseAssistantResponse, stripStrayDocumentMarkup, detectFailedDocumentUpdate, parseDocStatus, stripDocStatus, trimIncompleteHtmlTail } from '../text'
import { stripDiffMarkup } from '../diff'

// ── getTimestampId ────────────────────────────────────────────────────────────
describe('getTimestampId', () => {
  it('returns a string with the given prefix', () => {
    const id = getTimestampId('doc')
    expect(id).toMatch(/^doc-\d+$/)
  })

  it('includes a numeric timestamp after the prefix', () => {
    const before = Date.now()
    const id = getTimestampId('x')
    const after = Date.now()
    const ts = parseInt(id.split('-')[1], 10)
    expect(ts).toBeGreaterThanOrEqual(before)
    expect(ts).toBeLessThanOrEqual(after)
  })

  it('handles an empty prefix', () => {
    const id = getTimestampId('')
    expect(id).toMatch(/^-\d+$/)
  })
})

// ── stripIncompleteEndTag ─────────────────────────────────────────────────────
describe('stripIncompleteEndTag', () => {
  const fullTag = '</selection_replace>'

  it('returns the text unchanged if there is no partial tag', () => {
    expect(stripIncompleteEndTag('hello world')).toBe('hello world')
  })

  it('strips the full tag when it appears at the end', () => {
    const input = `some text${fullTag}`
    expect(stripIncompleteEndTag(input)).toBe('some text')
  })

  it('strips a partial tag suffix (first half)', () => {
    const partial = '</selec'
    const input = `content${partial}`
    expect(stripIncompleteEndTag(input)).toBe('content')
  })

  it('does NOT strip a suffix that is not part of the tag (e.g. "e>" alone)', () => {
    // "e>" is not a prefix of </selection_replace>, so input is unchanged
    const input = 'texte>'
    expect(stripIncompleteEndTag(input)).toBe('texte>')
  })

  it('does NOT strip if the tag appears in the middle, not at end', () => {
    const input = `${fullTag} more text`
    expect(stripIncompleteEndTag(input)).toBe(input)
  })

  it('handles an empty string', () => {
    expect(stripIncompleteEndTag('')).toBe('')
  })

  it('handles a string that is exactly the full tag', () => {
    expect(stripIncompleteEndTag(fullTag)).toBe('')
  })
})

// \u2500\u2500 extractTaggedBlock \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
describe('extractTaggedBlock', () => {
  it('reports not found when the tag is absent', () => {
    const r = extractTaggedBlock('just a chat reply', 'canvas')
    expect(r.found).toBe(false)
    expect(r.closed).toBe(false)
    expect(r.before).toBe('just a chat reply')
  })

  it('extracts a complete block and surrounding chat text', () => {
    const r = extractTaggedBlock('Sure!<canvas><p>Hi</p></canvas>Done', 'canvas')
    expect(r.found).toBe(true)
    expect(r.closed).toBe(true)
    expect(r.inner).toBe('<p>Hi</p>')
    expect(r.before).toBe('Sure!')
    expect(r.after).toBe('Done')
  })

  it('reports closed=false when the closing tag never arrived (truncation)', () => {
    const r = extractTaggedBlock('Here:<canvas><p>partial content', 'canvas')
    expect(r.found).toBe(true)
    expect(r.closed).toBe(false)
    expect(r.inner).toBe('<p>partial content')
  })

  it('is case-insensitive and tolerates attributes on the open tag', () => {
    const r = extractTaggedBlock('<Canvas data-x="1"><p>Body</p></CANVAS>', 'canvas')
    expect(r.found).toBe(true)
    expect(r.closed).toBe(true)
    expect(r.inner).toBe('<p>Body</p>')
  })

  it('tolerates whitespace inside the closing tag', () => {
    const r = extractTaggedBlock('<canvas><p>X</p></canvas >', 'canvas')
    expect(r.closed).toBe(true)
    expect(r.inner).toBe('<p>X</p>')
  })

  it('strips a wrapping markdown code fence from the inner HTML', () => {
    const r = extractTaggedBlock('<canvas>\n```html\n<p>Fenced</p>\n```\n</canvas>', 'canvas')
    expect(r.inner).toBe('<p>Fenced</p>')
  })

  it('works for the selection_replace tag too', () => {
    const r = extractTaggedBlock('ok<selection_replace>new text</selection_replace>', 'selection_replace')
    expect(r.found).toBe(true)
    expect(r.inner).toBe('new text')
  })
})

// \u2500\u2500 hasElisionMarkers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
describe('hasElisionMarkers', () => {
  it('flags an HTML comment that says the rest is unchanged', () => {
    expect(hasElisionMarkers('<p>Intro</p><!-- rest of the document unchanged -->')).toBe(true)
  })

  it('flags a bracketed continuation placeholder', () => {
    expect(hasElisionMarkers('<p>Start</p>[content continues]')).toBe(true)
  })

  it('flags a parenthetical "remains the same" placeholder', () => {
    expect(hasElisionMarkers('<p>A</p>(rest of the chapter remains the same)')).toBe(true)
  })

  it('flags a whole-paragraph ellipsis', () => {
    expect(hasElisionMarkers('<p>A</p><p>...</p><p>B</p>')).toBe(true)
    expect(hasElisionMarkers('<p>A</p><p>\u2026</p><p>B</p>')).toBe(true)
  })

  it('does NOT flag a bare ellipsis inside prose (legitimate in fiction)', () => {
    expect(hasElisionMarkers('<p>"Wait...," she whispered.</p>')).toBe(false)
  })

  it('does NOT flag ordinary content', () => {
    expect(hasElisionMarkers('<h1>Title</h1><p>A full paragraph of real content.</p>')).toBe(false)
  })

  it('does NOT flag the word "continues" used naturally in prose', () => {
    // Keyword must appear inside a comment/bracket/paren placeholder, not free prose.
    expect(hasElisionMarkers('<p>The road continues for miles.</p>')).toBe(false)
  })
})

// \u2500\u2500 validateCanvasReplacement \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
describe('validateCanvasReplacement', () => {
  it('returns "truncated" when the closing tag was not found', () => {
    expect(validateCanvasReplacement('<p>partial', false)).toBe('truncated')
  })

  it('returns "elided" when the closed output abbreviates content', () => {
    expect(validateCanvasReplacement('<p>A</p><!-- rest unchanged -->', true)).toBe('elided')
  })

  it('returns null for a complete, full replacement', () => {
    expect(validateCanvasReplacement('<h1>Title</h1><p>Full content here.</p>', true)).toBeNull()
  })

  it('prioritizes truncation over elision', () => {
    expect(validateCanvasReplacement('<p>A</p><!-- rest unchanged -->', false)).toBe('truncated')
  })
})

// ── parseEditBlocks ───────────────────────────────────────────────────────────
describe('parseEditBlocks', () => {
  const block = (search: string, replace: string) =>
    `<edit>\n<<<<<<< SEARCH\n${search}\n=======\n${replace}\n>>>>>>> REPLACE\n</edit>`

  it('returns no blocks when there are no markers', () => {
    const r = parseEditBlocks('just a normal chat reply')
    expect(r.blocks).toHaveLength(0)
  })

  it('parses a single edit block with search and replace', () => {
    const r = parseEditBlocks(block('<p>old</p>', '<p>new</p>'))
    expect(r.blocks).toHaveLength(1)
    expect(r.blocks[0].search).toBe('<p>old</p>')
    expect(r.blocks[0].replace).toBe('<p>new</p>')
  })

  it('parses multiple edit blocks', () => {
    const text = block('<p>a</p>', '<p>A</p>') + '\n' + block('<p>b</p>', '<p>B</p>')
    const r = parseEditBlocks(text)
    expect(r.blocks).toHaveLength(2)
    expect(r.blocks[1].search).toBe('<p>b</p>')
    expect(r.blocks[1].replace).toBe('<p>B</p>')
  })

  it('captures chat text before and after the edit region, stripping <edit> sugar', () => {
    const text = `Here is the change.\n${block('<p>x</p>', '<p>y</p>')}\nLet me know!`
    const r = parseEditBlocks(text)
    expect(r.before).toBe('Here is the change.')
    expect(r.after).toBe('Let me know!')
  })

  it('parses an empty REPLACE as a deletion', () => {
    const text = `<edit>\n<<<<<<< SEARCH\n<p>remove me</p>\n=======\n\n>>>>>>> REPLACE\n</edit>`
    const r = parseEditBlocks(text)
    expect(r.blocks).toHaveLength(1)
    expect(r.blocks[0].search).toBe('<p>remove me</p>')
    expect(r.blocks[0].replace.trim()).toBe('')
  })

  it('parses conflict markers even without <edit> wrapper tags', () => {
    const text = `<<<<<<< SEARCH\n<p>raw</p>\n=======\n<p>wrapped</p>\n>>>>>>> REPLACE`
    const r = parseEditBlocks(text)
    expect(r.blocks).toHaveLength(1)
    expect(r.blocks[0].search).toBe('<p>raw</p>')
  })

  it('ignores a block whose SEARCH is blank', () => {
    const text = `<<<<<<< SEARCH\n   \n=======\n<p>new</p>\n>>>>>>> REPLACE`
    const r = parseEditBlocks(text)
    expect(r.blocks).toHaveLength(0)
  })
})

// ── applyEditBlocks ───────────────────────────────────────────────────────────
describe('applyEditBlocks', () => {
  it('applies an exact-match edit', () => {
    const r = applyEditBlocks('<p>a</p><p>b</p>', [{ search: '<p>a</p>', replace: '<p>A</p>' }])
    expect(r.html).toBe('<p>A</p><p>b</p>')
    expect(r.failed).toHaveLength(0)
  })

  it('reports an edit whose SEARCH is not found, leaving the doc unchanged', () => {
    const r = applyEditBlocks('<p>a</p>', [{ search: '<p>missing</p>', replace: '<p>X</p>' }])
    expect(r.html).toBe('<p>a</p>')
    expect(r.failed).toHaveLength(1)
  })

  it('matches despite whitespace differences (newlines / indentation)', () => {
    const original = '<p>hello</p>\n<p>world</p>'
    // Model emits the search with different internal whitespace.
    const r = applyEditBlocks(original, [{ search: '<p>hello</p> <p>world</p>', replace: '<p>done</p>' }])
    expect(r.html).toBe('<p>done</p>')
    expect(r.failed).toHaveLength(0)
  })

  it('trims surrounding whitespace on the search text', () => {
    const r = applyEditBlocks('<p>keep</p>', [{ search: '\n  <p>keep</p>  \n', replace: '<p>kept</p>' }])
    expect(r.html).toBe('<p>kept</p>')
  })

  it('applies multiple edits sequentially', () => {
    const r = applyEditBlocks('<p>a</p><p>b</p><p>c</p>', [
      { search: '<p>a</p>', replace: '<p>A</p>' },
      { search: '<p>c</p>', replace: '<p>C</p>' }
    ])
    expect(r.html).toBe('<p>A</p><p>b</p><p>C</p>')
    expect(r.failed).toHaveLength(0)
  })

  it('applies matched edits and skips unmatched ones in the same batch', () => {
    const r = applyEditBlocks('<p>a</p><p>b</p>', [
      { search: '<p>a</p>', replace: '<p>A</p>' },
      { search: '<p>zzz</p>', replace: '<p>Z</p>' }
    ])
    expect(r.html).toBe('<p>A</p><p>b</p>')
    expect(r.failed).toHaveLength(1)
  })

  it('handles a deletion (empty replace)', () => {
    const r = applyEditBlocks('<p>a</p><p>b</p>', [{ search: '<p>a</p>', replace: '' }])
    expect(r.html).toBe('<p>b</p>')
  })

  it('does not treat $ in replacement as a special token', () => {
    const r = applyEditBlocks('<p>price</p>', [{ search: '<p>price</p>', replace: '<p>$5 & $10</p>' }])
    expect(r.html).toBe('<p>$5 & $10</p>')
  })

  // ── fuzzy level 4: entity / quote equivalence ──────────────────────────────
  it('matches when the doc has &nbsp; but the search has a plain space', () => {
    const r = applyEditBlocks('<p>hello&nbsp;world</p>', [{ search: '<p>hello world</p>', replace: '<p>hi</p>' }])
    expect(r.html).toBe('<p>hi</p>')
    expect(r.failed).toHaveLength(0)
  })

  it('matches when the doc has curly quotes but the search has straight quotes', () => {
    const r = applyEditBlocks('<p>she said “hi” and it’s fine</p>', [
      { search: `<p>she said "hi" and it's fine</p>`, replace: '<p>ok</p>' }
    ])
    expect(r.html).toBe('<p>ok</p>')
    expect(r.failed).toHaveLength(0)
  })

  it('matches when the doc has &amp; but the search has a bare &', () => {
    const r = applyEditBlocks('<p>salt &amp; pepper</p>', [{ search: '<p>salt & pepper</p>', replace: '<p>spices</p>' }])
    expect(r.html).toBe('<p>spices</p>')
    expect(r.failed).toHaveLength(0)
  })

  it('matches when doc has &#39; but the search has a curly apostrophe', () => {
    const r = applyEditBlocks('<p>it&#39;s here</p>', [{ search: '<p>it’s here</p>', replace: '<p>found</p>' }])
    expect(r.html).toBe('<p>found</p>')
    expect(r.failed).toHaveLength(0)
  })

  // ── fuzzy level 5: whole-block plain-text match ────────────────────────────
  it('matches a whole block even when the search dropped inline tags', () => {
    const doc = '<p>keep</p><p>The <strong>bold</strong> truth stays.</p><p>tail</p>'
    // Model copied the paragraph text but lost the <strong> markup.
    const r = applyEditBlocks(doc, [{ search: '<p>The bold truth stays.</p>', replace: '<p>Rewritten.</p>' }])
    expect(r.html).toBe('<p>keep</p><p>Rewritten.</p><p>tail</p>')
    expect(r.failed).toHaveLength(0)
  })

  it('matches a run of blocks by text when attributes differ', () => {
    const doc = '<h2 id="x">Title</h2><p class="lead">First para.</p><p>after</p>'
    // Model re-emitted the tags without the attributes.
    const r = applyEditBlocks(doc, [
      { search: '<h2>Title</h2><p>First para.</p>', replace: '<h2>New</h2><p>Changed.</p>' }
    ])
    expect(r.html).toBe('<h2>New</h2><p>Changed.</p><p>after</p>')
    expect(r.failed).toHaveLength(0)
  })

  // Reported: a 31-paragraph SEARCH, verbatim except for four closing quotes
  // the model left out, was skipped whole. A quote is an equivalence class for
  // SUBSTITUTION; a MISSING one needs the quote-blind pass.
  it('matches a run of whole blocks when the SEARCH dropped closing quotes', () => {
    const doc = '<p>开头。</p><p>她说：“先别挂。”然后停了一下。</p><p>他答：“好。”</p><p>结尾。</p>'
    const r = applyEditBlocks(doc, [{
      search: '<p>她说：“先别挂。然后停了一下。</p><p>他答：“好。</p>',
      replace: '<p>改写后的两段合成了一段。</p>'
    }])
    expect(r.failed).toHaveLength(0)
    expect(r.html).toBe('<p>开头。</p><p>改写后的两段合成了一段。</p><p>结尾。</p>')
  })

  it('leaves a quote-blind match unapplied when it is not unique', () => {
    const doc = '<p>“同一句话。”</p><p>中间。</p><p>同一句话。“”</p>'
    const r = applyEditBlocks(doc, [{ search: '<p>同一句话。</p>', replace: '<p>X</p>' }])
    expect(r.failed).toHaveLength(1)
    expect(r.html).toBe(doc)
  })

  it('still requires every other character to match', () => {
    const doc = '<p>她说：“先别挂。”然后停了一下。</p>'
    const r = applyEditBlocks(doc, [{ search: '<p>她说：“先别挂。然后停了两下。</p>', replace: '<p>X</p>' }])
    expect(r.failed).toHaveLength(1)
    expect(r.html).toBe(doc)
  })

  it('block-text match replaces whole blocks only — partial-paragraph text does not match', () => {
    const doc = '<p>alpha beta gamma</p>'
    const r = applyEditBlocks(doc, [{ search: 'beta', replace: 'BETA' }])
    // 'beta' matches as a plain substring (level 1), so it applies directly…
    expect(r.html).toBe('<p>alpha BETA gamma</p>')
    // …and a *paragraph-wrapped* partial text must never swap the whole block,
    // which would delete "gamma". It is placed in its own span instead (the
    // excerpt level, tested below).
    const r2 = applyEditBlocks(doc, [{ search: '<p>alpha beta</p>', replace: '<p>X</p>' }])
    expect(r2.failed).toHaveLength(0)
    expect(r2.html).toBe('<p>X gamma</p>')
  })

  it('still fails cleanly when the text genuinely is not in the document', () => {
    const r = applyEditBlocks('<p>real content</p>', [
      { search: '<p>hallucinated content</p>', replace: '<p>X</p>' }
    ])
    expect(r.failed).toHaveLength(1)
    expect(r.html).toBe('<p>real content</p>')
  })

  // ── level 6: an excerpt wrapped in block tags it does not span ─────────────
  // Reported as "2 suggested changes could not be located". Replaying the turn
  // showed both SEARCHes in the document verbatim: each was the TAIL of a
  // paragraph, wrapped in <p>…</p>. No earlier level could see it — the text
  // was in the document, the tags were not.
  describe('excerpt wrapped in tags it does not span', () => {
    it('applies a paragraph tail the model wrapped in <p> tags', () => {
      const doc = '<p>one</p><p>Lead-in clause, then the tail sentence to change.</p><p>three</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>then the tail sentence to change.</p>', replace: '<p>then a better tail.</p>' }])
      expect(r.failed).toHaveLength(0)
      expect(r.html).toBe('<p>one</p><p>Lead-in clause, then a better tail.</p><p>three</p>')
    })

    it('applies a paragraph head, and a middle, the same way', () => {
      const doc = '<p>Opening words here, and the rest of it stays.</p>'
      const head = applyEditBlocks(doc, [{ search: '<p>Opening words here,</p>', replace: '<p>New opening,</p>' }])
      expect(head.html).toBe('<p>New opening, and the rest of it stays.</p>')
      const middle = applyEditBlocks(doc, [{ search: '<p>and the rest of it</p>', replace: '<p>while the remainder</p>' }])
      expect(middle.html).toBe('<p>Opening words here, while the remainder stays.</p>')
    })

    it('splits the block when the excerpt becomes several paragraphs — balanced', () => {
      const doc = '<p>Keep this sentence. Rewrite this part please.</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>Rewrite this part please.</p>', replace: '<p>First new.</p><p>Second new.</p>' }])
      expect(r.html).toBe('<p>Keep this sentence. First new.</p><p>Second new.</p>')
    })

    it('deletes just the excerpt when REPLACE is empty', () => {
      const doc = '<p>Keep this sentence. Remove this one please.</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>Remove this one please.</p>', replace: '' }])
      expect(r.html).toBe('<p>Keep this sentence. </p>')
    })

    it('handles an excerpt that spans a paragraph boundary', () => {
      const doc = '<p>Alpha one. Alpha two.</p><p>Beta one. Beta two.</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>Alpha two.</p><p>Beta one.</p>', replace: '<p>Joined text.</p>' }])
      expect(r.html).toBe('<p>Alpha one. Joined text. Beta two.</p>')
    })

    it('works inside an inline element for a single-paragraph REPLACE', () => {
      const doc = '<p>Lead <strong>bold excerpt text</strong> end.</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>bold excerpt text</p>', replace: '<p>new excerpt words</p>' }])
      expect(r.html).toBe('<p>Lead <strong>new excerpt words</strong> end.</p>')
    })

    // ── every guard falls back to the old outcome: left unapplied ────────────
    it('refuses an excerpt that occurs more than once', () => {
      const doc = '<p>First: same phrase here.</p><p>Second: same phrase here.</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>same phrase here.</p>', replace: '<p>X</p>' }])
      expect(r.failed).toHaveLength(1)
      expect(r.html).toBe(doc)
    })

    it('refuses a REPLACE that re-states the rest of the block (a whole-block rewrite)', () => {
      // The model rewrote the WHOLE paragraph from a copy it remembered short.
      // An in-place swap would print the opening clause twice.
      const doc = '<p>The opening clause stays here. The closing clause.</p>'
      const r = applyEditBlocks(doc, [{
        search: '<p>The closing clause.</p>',
        replace: '<p>The opening clause stays here, joined with the closing clause.</p>'
      }])
      expect(r.failed).toHaveLength(1)
      expect(r.html).toBe(doc)
    })

    it('refuses a multi-paragraph REPLACE that would land inside an inline element', () => {
      const doc = '<p>Lead <strong>bold excerpt text</strong> end.</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>bold excerpt text</p>', replace: '<p>A.</p><p>B.</p>' }])
      expect(r.failed).toHaveLength(1)
      expect(r.html).toBe(doc)
    })

    it('refuses a REPLACE wrapped in a different block type', () => {
      const doc = '<p>Some lead text, then the excerpt to retitle.</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>then the excerpt to retitle.</p>', replace: '<h2>A heading</h2>' }])
      expect(r.failed).toHaveLength(1)
      expect(r.html).toBe(doc)
    })

    it('refuses an excerpt too short to place with confidence', () => {
      const doc = '<p>a big dog ran far</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>dog</p>', replace: '<p>cat</p>' }])
      expect(r.failed).toHaveLength(1)
      expect(r.html).toBe(doc)
    })

    it('refuses a match that lies inside a tag attribute, not in text', () => {
      const doc = '<p><a title="hidden tooltip words">link</a> rest</p>'
      const r = applyEditBlocks(doc, [{ search: '<p>hidden tooltip words</p>', replace: '<p>X</p>' }])
      expect(r.failed).toHaveLength(1)
      expect(r.html).toBe(doc)
    })
  })
})

// ── parseAssistantResponse ────────────────────────────────────────────────────
// ── a reply that uses more than one document channel ─────────────────────────
// Reported: an <edit> written beside a <selection_replace> appeared raw in the
// chat bubble, unapplied — the channels were exclusive, so everything around
// the selection was treated as chat.
describe('parseAssistantResponse — a selection rewrite with edits beside it', () => {
  const SEL = '<selection_replace><p>new selection text</p></selection_replace>'
  const EDIT = '<edit>\n<<<<<<< SEARCH\n<p>a later sentence</p>\n=======\n<p>a later sentence, smoothed</p>\n>>>>>>> REPLACE\n</edit>'

  it('keeps the selection and parses the edit instead of leaving it in the chat', () => {
    const r = parseAssistantResponse(`Done.\n${SEL}\n${EDIT}\n<doc_status>updated</doc_status>`)
    expect(r.kind).toBe('selection')
    expect(r.selectionText).toBe('<p>new selection text</p>')
    expect(r.editBlocks).toEqual([{ search: '<p>a later sentence</p>', replace: '<p>a later sentence, smoothed</p>' }])
    expect(r.chatText).toBe('Done.')
    expect(r.strayMarkup).toBe(0)
  })

  it('also finds an edit placed before the selection block', () => {
    const r = parseAssistantResponse(`Done.\n${EDIT}\n${SEL}`)
    expect(r.kind).toBe('selection')
    expect(r.editBlocks).toHaveLength(1)
    expect(r.chatText).toBe('Done.')
  })
})

describe('parseAssistantResponse — a rewrite of the active chapter beside edits of another (2026-10-06)', () => {
  const edit = (chapter: string) => `<edit chapter="${chapter}">\n<<<<<<< SEARCH\n<p>大纲旧</p>\n=======\n<p>大纲新</p>\n>>>>>>> REPLACE\n</edit>`

  it('keeps both when every edit names a chapter', () => {
    const r = parseAssistantResponse(`写好了。\n<canvas><p>第五章新稿</p></canvas>\n${edit('1')}`)
    expect(r.kind).toBe('edits')
    expect(r.extraCanvases).toEqual([{ text: '<p>第五章新稿</p>', closed: true }])
    expect(r.strayMarkup).toBe(0)
    expect(r.chatText).toBe('写好了。')
  })

  it('still drops the canvas when an unnamed edit may target the same chapter', () => {
    const r = parseAssistantResponse(`<canvas><p>第五章新稿</p></canvas>\n<edit>\n<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>b</p>\n>>>>>>> REPLACE\n</edit>`)
    expect(r.extraCanvases).toEqual([])
    expect(r.strayMarkup).toBe(1)
  })
})

describe('parseAssistantResponse — markup no channel took never reaches the chat', () => {
  it('drops a canvas written beside a selection, and counts it', () => {
    const r = parseAssistantResponse('Done.\n<selection_replace><p>x</p></selection_replace>\n<canvas><p>whole doc</p></canvas>')
    expect(r.kind).toBe('selection')
    expect(r.chatText).toBe('Done.')
    expect(r.strayMarkup).toBe(1)
  })

  it('drops a canvas written beside edits, and counts it', () => {
    const r = parseAssistantResponse('Ok.\n<edit>\n<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>b</p>\n>>>>>>> REPLACE\n</edit>\n<canvas><p>whole doc</p></canvas>')
    expect(r.kind).toBe('edits')
    expect(r.chatText).toBe('Ok.')
    expect(r.strayMarkup).toBe(1)
  })

  it('drops an unterminated SEARCH block from a chat reply', () => {
    const r = parseAssistantResponse('Here:\n<<<<<<< SEARCH\n<p>x</p>\n=======\n<p>y</p>')
    expect(r.kind).toBe('chat')
    expect(r.chatText).toBe('Here:')
    expect(r.strayMarkup).toBe(1)
  })

  it('leaves an ordinary chat reply untouched', () => {
    const r = parseAssistantResponse('Just talking about the plot, with 3 < 5 and a >>> arrow.')
    expect(r.chatText).toBe('Just talking about the plot, with 3 < 5 and a >>> arrow.')
    expect(r.strayMarkup).toBe(0)
  })
})

describe('stripStrayDocumentMarkup', () => {
  it('removes wrapped and bare edit blocks, canvas, a second selection, and lone tags', () => {
    const text = [
      'Before.',
      '<edit>\n<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>b</p>\n>>>>>>> REPLACE\n</edit>',
      '<<<<<<< SEARCH\n<p>c</p>\n=======\n<p>d</p>\n>>>>>>> REPLACE',
      '<canvas><p>doc</p></canvas>',
      '<selection_replace><p>sel</p></selection_replace>',
      'After. </edit>'
    ].join('\n')
    const r = stripStrayDocumentMarkup(text)
    expect(r.text).toBe('Before.\n\nAfter.')
    expect(r.removed).toBe(4)
  })

  it('removes an unclosed canvas through the end of the text', () => {
    expect(stripStrayDocumentMarkup('Intro\n<canvas><p>cut off')).toEqual({ text: 'Intro', removed: 1 })
  })
})

// ── edits applied on top of a document that already carries pending diffs ───
describe('applyEditBlocksLocally', () => {
  const PENDING = '<p>first <ins class="diff-addition">added earlier</ins></p>'

  it('marks only the block the edit changes and leaves pending diffs byte-identical', () => {
    const head = PENDING + '<p>middle stays</p>'
    const doc = head + '<p>the target sentence here</p><p>last</p>'
    const r = applyEditBlocksLocally(doc, [{ search: '<p>the target sentence here</p>', replace: '<p>the target sentence rewritten</p>' }])
    expect(r.failed).toHaveLength(0)
    expect(r.html.startsWith(head)).toBe(true)
    expect(r.html.endsWith('<p>last</p>')).toBe(true)
    expect(r.html.slice(head.length, r.html.length - '<p>last</p>'.length)).toMatch(/diff-(?:addition|deletion)/)
    expect(stripDiffMarkup(r.html)).toBe('<p>first added earlier</p><p>middle stays</p><p>the target sentence rewritten</p><p>last</p>')
  })

  // A block that already carries a pending diff is diffed at the changed span.
  // Refusing it outright skipped two ordinary cases: a continuity fix right
  // after a selection that ended mid-paragraph, and a second fix in the same
  // paragraph (user-reported: "1 suggested change could not be located").
  it('applies an edit to unmarked text in a block that already carries a pending diff', () => {
    const PENDING_INS = '<ins class="diff-addition">beta</ins>'
    const doc = `<p>alpha ${PENDING_INS} gamma delta epsilon</p>`
    const r = applyEditBlocksLocally(doc, [{ search: 'gamma delta', replace: 'GAMMA DELTA' }])
    expect(r.failed).toHaveLength(0)
    expect(r.html.startsWith(`<p>alpha ${PENDING_INS} `)).toBe(true)   // pending diff byte-identical
    expect(stripDiffMarkup(r.html)).toBe('<p>alpha beta GAMMA DELTA epsilon</p>')
  })

  it('applies two edits to the same paragraph', () => {
    const doc = '<p>first spot here, the middle stays, second spot here.</p>'
    const r = applyEditBlocksLocally(doc, [
      { search: 'first spot here', replace: 'first spot changed' },
      { search: 'second spot here', replace: 'second spot changed' }
    ])
    expect(r.failed).toHaveLength(0)
    expect(stripDiffMarkup(r.html)).toBe('<p>first spot changed, the middle stays, second spot changed.</p>')
  })

  it('applies two edits to the same Chinese paragraph', () => {
    // A whole-block diff marks the ENTIRE paragraph in Chinese (no word
    // boundaries), so the second edit used to land inside the first's diff.
    const doc = '<p>第一处还写着旧的，中间这句不动，第二处也还是旧的。</p>'
    const r = applyEditBlocksLocally(doc, [
      { search: '第一处还写着旧的', replace: '第一处改成新的' },
      { search: '第二处也还是旧的', replace: '第二处也改好了' }
    ])
    expect(r.failed).toHaveLength(0)
    expect(stripDiffMarkup(r.html)).toBe('<p>第一处改成新的，中间这句不动，第二处也改好了。</p>')
    expect(r.html).toContain('中间这句不动')   // the untouched middle carries no markup
  })

  it('diffs Latin text in whole words, not mid-word', () => {
    const r = applyEditBlocksLocally('<p>keep the word here please</p>', [{ search: 'here', replace: 'hear' }])
    expect(stripDiffMarkup(r.html)).toBe('<p>keep the word hear please</p>')
    expect(r.html).toMatch(/>here<\/del>/)
    expect(r.html).toMatch(/>hear<\/ins>/)
  })

  it('refuses a change that overlaps pending markup', () => {
    const doc = '<p>alpha <ins class="diff-addition">beta</ins> gamma</p>'
    const r = applyEditBlocksLocally(doc, [{ search: 'alpha <ins class="diff-addition">beta</ins> gamma', replace: 'alpha BETA gamma' }])
    expect(r.failed).toHaveLength(1)
    expect(r.html).toBe(doc)
  })

  it('corrects text inside a pending insertion in place — the proposal changes, no diff nests', () => {
    // Reported 2026-10-06: a selection rewrite came out with a stray English
    // word, and every edit removing it was refused (and reported "not found").
    const doc = '<p>keep <del class="diff-deletion">old words</del><ins class="diff-addition">brand new entrained words here</ins> end</p>'
    const r = applyEditBlocksLocally(doc, [{ search: 'new entrained words', replace: 'new words' }])
    expect(r.failed).toHaveLength(0)
    expect(r.html).toBe('<p>keep <del class="diff-deletion">old words</del><ins class="diff-addition">brand new words here</ins> end</p>')
    // Still one diff (confirmed text → corrected proposal); the reject side is
    // checked against a real editor in selectionWithEdits.test.ts.
    expect(stripDiffMarkup(r.html)).toBe('<p>keep brand new words here end</p>')
  })

  it('refuses a change inside a pending deletion, and says it was found, not missing', () => {
    const doc = '<p>keep <del class="diff-deletion">old words</del><ins class="diff-addition">new</ins> end</p>'
    const r = applyEditBlocksLocally(doc, [{ search: 'old words', replace: 'older words' }])
    expect(r.failed).toHaveLength(1)
    expect(r.underReview).toHaveLength(1)
    expect(r.html).toBe(doc)
  })

  it('never cuts a tag apart', () => {
    const doc = '<p><ins class="diff-addition">x</ins> keep <em>word</em> end</p>'
    const r = applyEditBlocksLocally(doc, [{ search: '<em>word</em>', replace: '<strong>word</strong>' }])
    expect(r.failed).toHaveLength(0)
    expect(stripDiffMarkup(r.html)).toBe('<p>x keep <strong>word</strong> end</p>')
    expect(r.html).not.toMatch(/<(?:ins|del)[^>]*>[a-z]+>/)   // no "<del>em>" style fragments
  })

  it('never cuts an entity apart', () => {
    const doc = '<p><ins class="diff-addition">x</ins> salt &amp; pepper</p>'
    const r = applyEditBlocksLocally(doc, [{ search: 'salt &amp; pepper', replace: 'salt &lt; pepper' }])
    expect(r.failed).toHaveLength(0)
    expect(stripDiffMarkup(r.html)).toBe('<p>x salt &lt; pepper</p>')
    expect(r.html).not.toMatch(/&(?:<|[a-z]*<)/)   // no "&<del>amp" or "&am<ins>"
  })

  it('refuses a change that would split a paragraph carrying pending markup', () => {
    const doc = '<p><ins class="diff-addition">x</ins> keep this. change this one.</p>'
    const r = applyEditBlocksLocally(doc, [{ search: 'change this one.', replace: 'one.</p><p>two.' }])
    expect(r.failed).toHaveLength(1)
    expect(r.html).toBe(doc)
  })

  it('reports an edit it cannot locate and changes nothing', () => {
    const doc = PENDING + '<p>real text</p>'
    const r = applyEditBlocksLocally(doc, [{ search: '<p>not in the document</p>', replace: '<p>x</p>' }])
    expect(r.failed).toHaveLength(1)
    expect(r.html).toBe(doc)
  })

  it('applies several edits, each as its own local diff', () => {
    const doc = '<p>one alpha</p><p>two</p><p>three gamma</p>'
    const r = applyEditBlocksLocally(doc, [
      { search: '<p>one alpha</p>', replace: '<p>one ALPHA</p>' },
      { search: '<p>three gamma</p>', replace: '<p>three GAMMA</p>' }
    ])
    expect(r.failed).toHaveLength(0)
    expect(r.html).toContain('<p>two</p>')
    expect(stripDiffMarkup(r.html)).toBe('<p>one ALPHA</p><p>two</p><p>three GAMMA</p>')
  })

  it('handles a pure insertion and a deletion', () => {
    const ins = applyEditBlocksLocally('<p>a</p><p>b</p>', [{ search: '<p>a</p>', replace: '<p>a</p><p>inserted</p>' }])
    expect(stripDiffMarkup(ins.html)).toBe('<p>a</p><p>inserted</p><p>b</p>')
    const del = applyEditBlocksLocally('<p>a</p><p>gone</p><p>b</p>', [{ search: '<p>gone</p>', replace: '' }])
    expect(stripDiffMarkup(del.html)).toBe('<p>a</p><p>b</p>')
  })

  it('treats a list as one node, so a change inside it stays balanced', () => {
    const doc = '<ul><li><p>one</p></li><li><p>two item</p></li></ul><p>after</p>'
    const r = applyEditBlocksLocally(doc, [{ search: '<p>two item</p>', replace: '<p>two items</p>' }])
    expect(r.failed).toHaveLength(0)
    expect(r.html.endsWith('<p>after</p>')).toBe(true)
    expect(stripDiffMarkup(r.html)).toBe('<ul><li><p>one</p></li><li><p>two items</p></li></ul><p>after</p>')
  })
})

describe('parseAssistantResponse', () => {
  it('classifies a plain chat response', () => {
    const r = parseAssistantResponse('Sure, here is my advice about pacing.')
    expect(r.kind).toBe('chat')
    expect(r.chatText).toBe('Sure, here is my advice about pacing.')
  })

  it('classifies a selection replacement with surrounding chat', () => {
    const r = parseAssistantResponse('Done!\n<selection_replace>The fluffy cat</selection_replace>\nAnything else?')
    expect(r.kind).toBe('selection')
    expect(r.selectionText).toBe('The fluffy cat')
    expect(r.chatText).toBe('Done!\n\nAnything else?')
  })

  it('classifies edit blocks and keeps surrounding chat', () => {
    const text = 'I made the change.\n<edit>\n<<<<<<< SEARCH\n<p>old</p>\n=======\n<p>new</p>\n>>>>>>> REPLACE\n</edit>'
    const r = parseAssistantResponse(text)
    expect(r.kind).toBe('edits')
    expect(r.editBlocks).toEqual([{ search: '<p>old</p>', replace: '<p>new</p>' }])
    expect(r.chatText).toBe('I made the change.')
  })

  it('classifies a closed canvas rewrite', () => {
    const r = parseAssistantResponse('Rewrote it.\n<canvas><h1>Doc</h1></canvas>')
    expect(r.kind).toBe('canvas')
    expect(r.canvasText).toBe('<h1>Doc</h1>')
    expect(r.canvasClosed).toBe(true)
    expect(r.chatText).toBe('Rewrote it.')
  })

  it('reports an unclosed canvas so truncation guards can refuse it', () => {
    const r = parseAssistantResponse('<canvas><h1>Doc</h1><p>cut off')
    expect(r.kind).toBe('canvas')
    expect(r.canvasClosed).toBe(false)
  })

  it('prefers selection over edits over canvas when several appear', () => {
    const both = '<selection_replace>x</selection_replace>\n<canvas><p>y</p></canvas>'
    expect(parseAssistantResponse(both).kind).toBe('selection')
    const editsAndCanvas = '<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>b</p>\n>>>>>>> REPLACE\n<canvas><p>y</p></canvas>'
    expect(parseAssistantResponse(editsAndCanvas).kind).toBe('edits')
  })
})

describe('trimIncompleteHtmlTail', () => {
  it('drops a partially streamed tag', () => {
    expect(trimIncompleteHtmlTail('<p>done</p><h')).toBe('<p>done</p>')
    expect(trimIncompleteHtmlTail('<p>done</p><h2 class="x')).toBe('<p>done</p>')
  })

  it('drops a partially streamed entity', () => {
    expect(trimIncompleteHtmlTail('<p>a&nbs')).toBe('<p>a')
    expect(trimIncompleteHtmlTail('<p>a&amp;b')).toBe('<p>a&amp;b')
  })

  it('keeps complete markup, including unclosed elements', () => {
    // Unclosed <p> is fine — the DOM parser closes it; only partial tokens hurt.
    expect(trimIncompleteHtmlTail('<p>half a sentence')).toBe('<p>half a sentence')
    expect(trimIncompleteHtmlTail('<h1>T</h1><p>x</p>')).toBe('<h1>T</h1><p>x</p>')
  })

  it('leaves a lone & in prose alone when it is far from the end', () => {
    expect(trimIncompleteHtmlTail('<p>Tom & Jerry went to the park today</p>'))
      .toBe('<p>Tom & Jerry went to the park today</p>')
  })

  it('handles empty input', () => {
    expect(trimIncompleteHtmlTail('')).toBe('')
  })
})

describe('parseEditBlocks — terminators models actually emit', () => {
  // The shape of a grok-4.5 reply (2026-07-26): no ">>>>>>> REPLACE",
  // the block just ends at </edit>. This used to parse as zero blocks, so the
  // whole response was displayed as chat and the document never changed.
  it('accepts </edit> as the block terminator', () => {
    const text = [
      '已在大纲补上「语气须轻松口语」。',
      '<edit>',
      '<<<<<<< SEARCH',
      '<p>语气口语。',
      '=======',
      '<p>语气口语，多用语助词。',
      '</edit>'
    ].join('\n')
    const r = parseEditBlocks(text)
    expect(r.blocks).toHaveLength(1)
    expect(r.blocks[0].search).toContain('语气口语。')
    expect(r.blocks[0].replace).toContain('多用语助词')
    expect(r.blocks[0].replace).not.toContain('</edit>')
    expect(r.before).toBe('已在大纲补上「语气须轻松口语」。')
  })

  it('parses consecutive </edit>-terminated blocks', () => {
    const block = (a: string, b: string) =>
      `<edit>\n<<<<<<< SEARCH\n${a}\n=======\n${b}\n</edit>`
    const r = parseEditBlocks(`${block('<p>one</p>', '<p>ONE</p>')}\n${block('<p>two</p>', '<p>TWO</p>')}`)
    expect(r.blocks).toHaveLength(2)
    expect(r.blocks[1].replace).toContain('<p>TWO</p>')
  })

  it('still parses the canonical >>>>>>> REPLACE form', () => {
    const text = '<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>b</p>\n>>>>>>> REPLACE'
    const r = parseEditBlocks(text)
    expect(r.blocks).toEqual([{ search: '<p>a</p>', replace: '<p>b</p>' }])
  })

  it('ends a terminator-less block where the next SEARCH marker begins', () => {
    const text = [
      '<<<<<<< SEARCH', '<p>one</p>', '=======', '<p>ONE</p>',
      '<<<<<<< SEARCH', '<p>two</p>', '=======', '<p>TWO</p>', '>>>>>>> REPLACE'
    ].join('\n')
    const r = parseEditBlocks(text)
    expect(r.blocks).toHaveLength(2)
    expect(r.blocks[0].replace.trim()).toBe('<p>ONE</p>')
    expect(r.blocks[1].replace.trim()).toBe('<p>TWO</p>')
  })

  it('drops a block cut off mid-REPLACE rather than applying half of it', () => {
    const text = '<<<<<<< SEARCH\n<p>whole paragraph</p>\n=======\n<p>replacement that never fin'
    expect(parseEditBlocks(text).blocks).toHaveLength(0)
  })

  it('keeps chat text on both sides of the edit region', () => {
    const text = 'Here you go.\n<edit>\n<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>b</p>\n</edit>\nAnything else?'
    const r = parseEditBlocks(text)
    expect(r.before).toBe('Here you go.')
    expect(r.after).toBe('Anything else?')
  })
})

// ── doc_status declaration ───────────────────────────────────────────────────
// The model declares what it did; the client checks the declaration against
// the markup. This replaces guessing intent from the user's prompt, and
// outranks the prose heuristic in both directions.
describe('parseDocStatus / stripDocStatus', () => {
  it('reads the declaration the model appended', () => {
    expect(parseDocStatus('Done.\n<canvas><p>x</p></canvas>\n<doc_status>updated</doc_status>')).toBe('updated')
    expect(parseDocStatus('Reads fine to me.\n<doc_status>unchanged</doc_status>')).toBe('unchanged')
    expect(parseDocStatus('<DOC_STATUS> Updated </DOC_STATUS>')).toBe('updated')
  })

  it('returns null when the model omitted it', () => {
    expect(parseDocStatus('好了。')).toBeNull()
    expect(parseDocStatus('')).toBeNull()
  })

  it('keeps the declaration out of the chat bubble', () => {
    expect(stripDocStatus('好了。\n<doc_status>updated</doc_status>')).toBe('好了。')
    expect(parseAssistantResponse('Done.\n<doc_status>updated</doc_status>').chatText).toBe('Done.')
    const parsed = parseAssistantResponse('好了。\n<canvas><p>x</p></canvas>\n<doc_status>updated</doc_status>')
    expect(parsed.kind).toBe('canvas')
    expect(parsed.chatText).toBe('好了。')
    expect(parsed.canvasText).toContain('<p>x</p>')
  })

  it('hides a declaration that is still arriving mid-stream', () => {
    // A lone trailing '<' is left alone: nothing distinguishes it from prose,
    // and it is visible for one chunk at most.
    for (const partial of ['<d', '<doc', '<doc_status', '<doc_status>', '<doc_status>unch']) {
      expect(stripDocStatus('好了。\n' + partial), partial).toBe('好了。')
    }
  })

  it('leaves ordinary trailing text alone', () => {
    expect(stripDocStatus('第三章写完了。')).toBe('第三章写完了。')
    expect(stripDocStatus('a < b')).toBe('a < b')
  })
})

// ── detectFailedDocumentUpdate ───────────────────────────────────────────────
// The declaration is MANDATORY on every reply. Without it a model that
// silently skipped the work is indistinguishable from one that deliberately
// answered in chat — which is how "it said it wrote and it didn't" kept
// coming back after the intent-guessing heuristic was removed.
describe('detectFailedDocumentUpdate', () => {
  it('fails a reply that never declared its status', () => {
    expect(detectFailedDocumentUpdate('这段读起来没问题。')).toBe('undeclared')
    expect(detectFailedDocumentUpdate('Which chapter did you mean?')).toBe('undeclared')
    expect(detectFailedDocumentUpdate('已经帮你改好了')).toBe('undeclared')
  })

  it('accepts a declared non-edit as a complete answer', () => {
    expect(detectFailedDocumentUpdate('流式测试正常，本次无需改文档。\n<doc_status>unchanged</doc_status>')).toBeNull()
    expect(detectFailedDocumentUpdate('The pacing reads well.\n<doc_status>unchanged</doc_status>')).toBeNull()
    expect(detectFailedDocumentUpdate('你想让第三章从哪里开始写？\n<doc_status>unchanged</doc_status>')).toBeNull()
  })

  it('flags a declared update that emitted nothing', () => {
    // Neutral prose no pattern would ever catch — only the declaration shows it.
    expect(detectFailedDocumentUpdate('嗯。\n<doc_status>updated</doc_status>')).toBe('claimed')
    expect(detectFailedDocumentUpdate('Ok.\n<doc_status>updated</doc_status>')).toBe('claimed')
  })

  it('flags a reply that declares "unchanged" while claiming it wrote', () => {
    // Self-contradiction is precisely what the user experiences as the bug,
    // so the declaration stops being authoritative here.
    for (const t of [
      '我已经把第二章改写了一遍。\n<doc_status>unchanged</doc_status>',
      "I've rewritten the second paragraph.\n<doc_status>unchanged</doc_status>",
      '已经帮你把这段续写完了。\n<doc_status>unchanged</doc_status>'
    ]) expect(detectFailedDocumentUpdate(t), t).toBe('claimed')
  })

  it('still lets the model describe what the USER changed', () => {
    // Not first person: no contradiction, no retry.
    expect(detectFailedDocumentUpdate(
      '你已经把第二章改好了，这版读起来顺多了。\n<doc_status>unchanged</doc_status>'
    )).toBeNull()
  })

  it('flags broken markup whatever the declaration says', () => {
    expect(detectFailedDocumentUpdate('<edit>\n<<<<<<< SEARCH\nx\n<doc_status>unchanged</doc_status>')).toBe('malformed')
    expect(detectFailedDocumentUpdate('Sure! <canvas><h1>Draft')).toBe('malformed')
    const long = '已在大纲补上要求。\n<edit>\n<<<<<<< SEARCH\n<p>原句</p>\n' + '补充说明。'.repeat(60)
    expect(detectFailedDocumentUpdate(long)).toBe('malformed')
  })

  it('returns null for empty input', () => {
    expect(detectFailedDocumentUpdate('')).toBeNull()
    expect(detectFailedDocumentUpdate('   ')).toBeNull()
  })
})


// ── new_chapter: a canvas that creates its chapter ──────────────────────────
describe('new_chapter', () => {
  it('is its own attribute: never read as `chapter`', () => {
    expect(newChapterAttribute('<canvas new_chapter="第二章 进城">')).toBe('第二章 进城')
    expect(chapterAttribute('<canvas new_chapter="第二章 进城">')).toBeUndefined()
    expect(newChapterAttribute("<canvas new_chapter=' 尾声 '>")).toBe('尾声')
    expect(newChapterAttribute('<canvas new_chapter="">')).toBeUndefined()
    expect(newChapterAttribute('<canvas chapter="3">')).toBeUndefined()
  })

  it('leads the canvas channel, with the title', () => {
    const r = parseAssistantResponse('写第二章。\n<canvas new_chapter="第二章"><p>x</p></canvas>')
    expect(r).toMatchObject({ kind: 'canvas', canvasText: '<p>x</p>', canvasClosed: true, canvasNewChapter: '第二章', chatText: '写第二章。' })
    expect(r.canvasChapter).toBeUndefined()
  })

  it('rides along with another channel like a named chapter does', () => {
    const r = parseAssistantResponse('<canvas><p>active</p></canvas>\n<canvas new_chapter="第二章"><p>two</p></canvas>')
    expect(r.canvasText).toBe('<p>active</p>')
    expect(r.extraCanvases).toEqual([{ text: '<p>two</p>', closed: true, newChapter: '第二章' }])
  })

  it('reports a cut-off new chapter as unclosed', () => {
    expect(parseAssistantResponse('<canvas new_chapter="第二章"><p>半').canvasClosed).toBe(false)
  })
})

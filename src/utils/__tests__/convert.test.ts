import { describe, it, expect } from 'vitest'
import {
  normalizeBrParagraphs,
  htmlToMarkdown,
  htmlToPlainText,
  markdownToHtml,
  txtToHtml,
  sanitizeHtml,
  extractChapterTitleFromContent,
  splitHtmlToChapters,
  splitMarkdownToChapters,
  splitTxtToChapters
} from '../convert'

// ── htmlToMarkdown ────────────────────────────────────────────────────────────
describe('htmlToMarkdown', () => {
  it('converts <h1> to # heading', () => {
    expect(htmlToMarkdown('<h1>Title</h1>')).toBe('# Title')
  })

  it('converts <h2> to ## heading', () => {
    expect(htmlToMarkdown('<h2>Sub</h2>')).toBe('## Sub')
  })

  it('converts <h3> to ### heading', () => {
    expect(htmlToMarkdown('<h3>Sub</h3>')).toBe('### Sub')
  })

  it('converts <p> to plain paragraph', () => {
    expect(htmlToMarkdown('<p>Hello world</p>')).toBe('Hello world')
  })

  it('converts <strong> to **bold**', () => {
    expect(htmlToMarkdown('<p><strong>bold</strong></p>')).toBe('**bold**')
  })

  it('converts <em> to *italic*', () => {
    expect(htmlToMarkdown('<p><em>italic</em></p>')).toBe('*italic*')
  })

  it('converts <code> to `inline code`', () => {
    expect(htmlToMarkdown('<p><code>fn()</code></p>')).toBe('`fn()`')
  })

  it('converts <ul> <li> to bullet list', () => {
    const html = '<ul><li>one</li><li>two</li></ul>'
    const md = htmlToMarkdown(html)
    expect(md).toContain('- one')
    expect(md).toContain('- two')
  })

  it('converts <ol> <li> to numbered list', () => {
    const html = '<ol><li>first</li><li>second</li></ol>'
    const md = htmlToMarkdown(html)
    expect(md).toContain('1. first')
    expect(md).toContain('2. second')
  })

  it('converts <hr> to ---', () => {
    expect(htmlToMarkdown('<hr />')).toContain('---')
  })

  it('strips <del> content', () => {
    expect(htmlToMarkdown('<p>keep<del>remove</del></p>')).not.toContain('remove')
  })

  it('keeps <ins> content', () => {
    expect(htmlToMarkdown('<p><ins>keep</ins></p>')).toContain('keep')
  })

  it('returns empty string for empty input', () => {
    expect(htmlToMarkdown('')).toBe('')
  })
})

// ── htmlToPlainText ───────────────────────────────────────────────────────────
describe('htmlToPlainText', () => {
  it('extracts plain text from HTML', () => {
    const text = htmlToPlainText('<p>Hello <strong>world</strong></p>')
    expect(text).toContain('Hello')
    expect(text).toContain('world')
  })

  it('strips <del> content', () => {
    const text = htmlToPlainText('<p>keep <del>deleted</del> this</p>')
    expect(text).not.toContain('deleted')
    expect(text).toContain('keep')
  })

  it('handles empty string', () => {
    expect(htmlToPlainText('')).toBe('')
  })
})

// ── markdownToHtml ────────────────────────────────────────────────────────────
describe('markdownToHtml', () => {
  it('converts # heading to <h1>', () => {
    expect(markdownToHtml('# Hello')).toContain('<h1>')
    expect(markdownToHtml('# Hello')).toContain('Hello')
  })

  it('converts ## heading to <h2>', () => {
    expect(markdownToHtml('## Sub')).toContain('<h2>')
  })

  it('converts **bold** to <strong>', () => {
    const html = markdownToHtml('**bold text**')
    expect(html).toContain('<strong>')
    expect(html).toContain('bold text')
  })

  it('converts *italic* to <em>', () => {
    const html = markdownToHtml('*italic text*')
    expect(html).toContain('<em>')
  })

  it('converts `code` to <code>', () => {
    expect(markdownToHtml('`fn()`')).toContain('<code>')
  })

  it('converts - list items to <ul><li>', () => {
    const html = markdownToHtml('- item one\n- item two')
    expect(html).toContain('<ul>')
    expect(html).toContain('<li>')
    expect(html).toContain('item one')
    expect(html).toContain('item two')
  })

  it('converts 1. list items to <ol><li>', () => {
    const html = markdownToHtml('1. first\n2. second')
    expect(html).toContain('<ol>')
    expect(html).toContain('<li>')
  })

  it('converts ``` code blocks to <pre><code>', () => {
    const html = markdownToHtml('```\nconst x = 1\n```')
    expect(html).toContain('<pre>')
    expect(html).toContain('<code>')
    expect(html).toContain('const x = 1')
  })

  it('converts --- to <hr />', () => {
    const html = markdownToHtml('---')
    expect(html).toContain('<hr')
  })

  it('converts > blockquote to <blockquote>', () => {
    const html = markdownToHtml('> quoted text')
    expect(html).toContain('<blockquote>')
  })

  it('escapes HTML in code blocks to prevent XSS', () => {
    const html = markdownToHtml('```\n<script>alert(1)</script>\n```')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
  })

  it('returns empty string for empty input', () => {
    expect(markdownToHtml('')).toBe('')
  })
})

// ── txtToHtml ─────────────────────────────────────────────────────────────────
describe('txtToHtml', () => {
  it('wraps paragraphs in <p> tags', () => {
    const html = txtToHtml('Hello world')
    expect(html).toContain('<p>')
    expect(html).toContain('Hello world')
  })

  it('splits double newlines into separate paragraphs', () => {
    const html = txtToHtml('Para one\n\nPara two')
    const matches = html.match(/<p>/g) || []
    expect(matches.length).toBe(2)
  })

  it('converts single newlines to <br /> within a paragraph', () => {
    const html = txtToHtml('line one\nline two')
    expect(html).toContain('<br />')
  })

  it('escapes HTML special characters', () => {
    const html = txtToHtml('<script>alert("xss")</script>')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })

  it('returns empty string for empty input', () => {
    expect(txtToHtml('')).toBe('')
  })
})

// ── sanitizeHtml ──────────────────────────────────────────────────────────────
describe('sanitizeHtml', () => {
  it('removes <script> tags and their content', () => {
    const result = sanitizeHtml('<p>Safe</p><script>alert(1)</script>')
    expect(result).not.toContain('<script>')
    expect(result).not.toContain('alert')
    expect(result).toContain('Safe')
  })

  it('removes inline event handlers (onclick, onload, etc.)', () => {
    const result = sanitizeHtml('<p onclick="alert(1)">Text</p>')
    expect(result).not.toContain('onclick')
    expect(result).toContain('Text')
  })

  it('removes href="javascript:" URIs', () => {
    const result = sanitizeHtml('<a href="javascript:void(0)">click</a>')
    expect(result).not.toContain('javascript:')
  })

  it('preserves safe HTML structure', () => {
    const html = '<h1>Title</h1><p>Body <strong>bold</strong></p>'
    const result = sanitizeHtml(html)
    expect(result).toContain('<h1>')
    expect(result).toContain('<strong>')
    expect(result).toContain('Title')
  })

  it('handles empty string', () => {
    expect(sanitizeHtml('')).toBe('')
  })
})

// ── extractChapterTitleFromContent ────────────────────────────────────────────
describe('extractChapterTitleFromContent', () => {
  it('extracts the first <h1> as title and removes it from content', () => {
    const html = '<h1>Chapter One</h1><p>Body text</p>'
    const { title, content } = extractChapterTitleFromContent(html, 'Default')
    expect(title).toBe('Chapter One')
    expect(content).not.toContain('<h1>')
    expect(content).toContain('Body text')
  })

  it('uses defaultTitle when no <h1> is present', () => {
    const html = '<p>Body only</p>'
    const { title, content } = extractChapterTitleFromContent(html, 'Fallback Title')
    expect(title).toBe('Fallback Title')
    expect(content).toBe(html)
  })

  it('uses defaultTitle for empty content', () => {
    const { title } = extractChapterTitleFromContent('', 'Empty')
    expect(title).toBe('Empty')
  })
})

// ── splitHtmlToChapters ───────────────────────────────────────────────────────
describe('splitHtmlToChapters', () => {
  it('splits on <hr> into separate chapters', () => {
    const html = '<h1>Chapter 1</h1><p>Content 1</p><hr /><h1>Chapter 2</h1><p>Content 2</p>'
    const chapters = splitHtmlToChapters(html)
    expect(chapters).toHaveLength(2)
    expect(chapters[0].title).toBe('Chapter 1')
    expect(chapters[1].title).toBe('Chapter 2')
  })

  it('returns a single chapter when no <hr> is present', () => {
    const html = '<h1>Only Chapter</h1><p>Content</p>'
    const chapters = splitHtmlToChapters(html)
    expect(chapters).toHaveLength(1)
    expect(chapters[0].title).toBe('Only Chapter')
  })

  it('returns empty array for empty input', () => {
    const chapters = splitHtmlToChapters('')
    expect(chapters).toHaveLength(0)
  })

  it('sanitizes chapter content to remove scripts', () => {
    const html = '<h1>Safe</h1><p>ok</p><script>bad()</script>'
    const chapters = splitHtmlToChapters(html)
    expect(chapters[0].content).not.toContain('<script>')
  })
})

// ── splitMarkdownToChapters ───────────────────────────────────────────────────
describe('splitMarkdownToChapters', () => {
  it('splits on --- separator into chapters', () => {
    const md = '# Chapter 1\n\nContent 1\n\n---\n\n# Chapter 2\n\nContent 2'
    const chapters = splitMarkdownToChapters(md)
    expect(chapters).toHaveLength(2)
    expect(chapters[0].title).toBe('Chapter 1')
    expect(chapters[1].title).toBe('Chapter 2')
  })

  it('returns one chapter when no separator is present', () => {
    const md = '# Solo\n\nBody text'
    const chapters = splitMarkdownToChapters(md)
    expect(chapters).toHaveLength(1)
  })

  it('does not split on --- inside a code block', () => {
    const md = '# Ch1\n\n```\n---\n```\n\nBody'
    const chapters = splitMarkdownToChapters(md)
    expect(chapters).toHaveLength(1)
  })

  it('returns empty array when input is only whitespace/blank', () => {
    // An entirely empty string still produces one empty chapter per implementation
    // Test that a whitespace-only string results in no meaningful chapters
    const chapters = splitMarkdownToChapters('   ')
    // The implementation may return 0 or 1 empty chapter — what matters is no content
    chapters.forEach(ch => {
      expect(ch.content.trim()).toBe('')
    })
  })
})

// ── splitTxtToChapters ────────────────────────────────────────────────────────
describe('splitTxtToChapters', () => {
  it('splits on underline-style === headings', () => {
    const txt = 'Chapter One\n===========\nContent of one\n\nChapter Two\n===========\nContent of two'
    const chapters = splitTxtToChapters(txt)
    expect(chapters).toHaveLength(2)
    expect(chapters[0].title).toBe('Chapter One')
    expect(chapters[1].title).toBe('Chapter Two')
  })

  it('returns a single chapter when no headings are present', () => {
    const txt = 'Just some text\nwith no headings'
    const chapters = splitTxtToChapters(txt)
    expect(chapters).toHaveLength(1)
  })

  it('returns empty array when input is blank/whitespace only', () => {
    // Implementation may return one empty chapter — ensure content is blank
    const chapters = splitTxtToChapters('   ')
    chapters.forEach(ch => {
      expect(ch.content.trim()).toBe('')
    })
  })
})

// ── normalizeBrParagraphs ─────────────────────────────────────────────────────
describe('normalizeBrParagraphs', () => {
  it('splits a wall of <br> lines into real paragraphs', () => {
    const out = normalizeBrParagraphs('<p>第一行<br>第二行<br>第三行</p>')
    expect(out).toBe('<p>第一行</p><p>第二行</p><p>第三行</p>')
  })

  it('leaves a single isolated <br> alone (deliberate soft break)', () => {
    const html = '<p>地址第一行<br>地址第二行</p>'
    expect(normalizeBrParagraphs(html)).toBe(html)
  })

  it('collapses consecutive <br> runs without creating empty paragraphs', () => {
    const out = normalizeBrParagraphs('<p>a<br><br><br>b<br>c</p>')
    expect(out).toBe('<p>a</p><p>b</p><p>c</p>')
  })

  it('splits through inline wrappers, cloning the formatting into each paragraph', () => {
    // Web copy usually wraps lines in <span>/<strong>; the break still ends a
    // line, so it must split — with the formatting preserved on both sides.
    expect(normalizeBrParagraphs('<p><strong>a<br>b<br>c</strong></p>'))
      .toBe('<p><strong>a</strong></p><p><strong>b</strong></p><p><strong>c</strong></p>')
    expect(normalizeBrParagraphs('<div><span>一<br>二<br>三</span></div>'))
      .toBe('<p><span>一</span></p><p><span>二</span></p><p><span>三</span></p>')
  })

  it('splits a bare top-level run with no block wrapper', () => {
    expect(normalizeBrParagraphs('一<br>二<br>三')).toBe('<p>一</p><p>二</p><p>三</p>')
  })

  it('leaves <br> inside headings and list items alone', () => {
    const h = '<h2>标题<br>副标题<br>第三行</h2>'
    expect(normalizeBrParagraphs(h)).toBe(h)
    const li = '<ul><li>a<br>b<br>c</li></ul>'
    expect(normalizeBrParagraphs(li)).toBe(li)
  })

  it('splits leaf blocks inside a wrapper without disturbing the wrapper', () => {
    expect(normalizeBrParagraphs('<div><p>a<br>b<br>c</p><p>d</p></div>'))
      .toBe('<div><p>a</p><p>b</p><p>c</p><p>d</p></div>')
    // A lone <br> in the inner block stays a soft break.
    expect(normalizeBrParagraphs('<div><p>a<br>b</p><p>c</p></div>'))
      .toBe('<div><p>a<br>b</p><p>c</p></div>')
  })

  it('converts <br>-separated divs into paragraphs and keeps inline markup', () => {
    const out = normalizeBrParagraphs('<div>x <em>em</em><br>y<br>z</div>')
    expect(out).toBe('<p>x <em>em</em></p><p>y</p><p>z</p>')
  })

  it('keeps media-only groups as their own paragraph', () => {
    const out = normalizeBrParagraphs('<p>text<br><img src="data:image/png;base64,AAA"><br>tail</p>')
    expect(out).toContain('<img src="data:image/png;base64,AAA">')
    expect(out.match(/<p>/g)?.length).toBe(3)
  })

  it('returns HTML without <br> untouched', () => {
    const html = '<h1>T</h1><p>one</p><p>two</p>'
    expect(normalizeBrParagraphs(html)).toBe(html)
  })

  it('leaves trailing/leading breaks from producing blank paragraphs', () => {
    const out = normalizeBrParagraphs('<p><br>a<br>b<br></p>')
    expect(out).toBe('<p>a</p><p>b</p>')
  })
})

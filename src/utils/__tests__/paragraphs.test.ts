import { describe, it, expect } from 'vitest'
import { chapterParagraphs, numberedLine, searchableText, applyParagraphEdits, chapterChars } from '../paragraphs'

describe('chapterParagraphs', () => {
  it('numbers every non-empty top-level block, headings and images included', () => {
    const paras = chapterParagraphs('<h1>标题</h1><p>一。</p><p></p><p><img src="x"></p><ul><li>a</li><li>b</li></ul>loose')
    expect(paras.map(p => [p.number, p.kind, p.text])).toEqual([
      [1, 'heading', '标题'],
      [2, 'paragraph', '一。'],
      [3, 'image', ''],
      // Each list entry on its own (a card's fields were once one ¶).
      [4, 'item', 'a'],
      [5, 'item', 'b'],
      [6, 'paragraph', 'loose']
    ])
  })

  it('treats an image placeholder token as an image', () => {
    expect(chapterParagraphs('<p>{{IMAGE_PLACEHOLDER_3}}</p>')[0].kind).toBe('image')
  })

  it('renders one line per paragraph for a text read', () => {
    const [h, p, img, x, y] = chapterParagraphs('<h2>T</h2><p>a &amp; b</p><p><img src="x"></p><ul><li>x</li><li>y</li></ul>')
    expect([h, p, img, x, y].map(numberedLine)).toEqual(['¶1 # T', '¶2 a & b', '¶3 [image]', '¶4 • x', '¶5 • y'])
  })

  // An image at the end of a text paragraph used to vanish from the text
  // view, so a model that grepped for it reported it lost (2026-10-11).
  it('marks a text paragraph that carries an image, and grep finds it by that word', () => {
    const [p, mixed] = chapterParagraphs('<p>Plain.</p><p>She smiled at the camera. <img src="pic"></p>')
    expect(p.image).toBeUndefined()
    expect(mixed).toMatchObject({ kind: 'paragraph', text: 'She smiled at the camera.', image: true })
    expect(numberedLine(mixed)).toBe('¶2 She smiled at the camera. [image]')
    expect(searchableText(mixed)).toBe('She smiled at the camera. [image]')
    expect(searchableText(p)).toBe('Plain.')
  })
})

describe('chapterChars', () => {
  // The one length the footer and the write tools both state (CanvasFooter).
  it('counts every character of the paragraphs\' text, punctuation included, not the markup or the gaps between blocks', () => {
    expect(chapterChars('<h1>第三章</h1>\n<p>“你来了。”她说……</p>\n<p>Hello, world!</p><p><img src="x"></p>')).toBe(3 + 10 + 13)
  })
})

describe('applyParagraphEdits keeps an inline image', () => {
  const html = '<p>Intro.</p><p>She smiled. <img src="pic"></p><p>End.</p>'

  it('carries the image into the replacement when the model rewrote only the words', () => {
    const out = applyParagraphEdits(html, [{ paragraph: 2, action: 'replace', html: '<p>She grinned.</p>', startsWith: 'She smiled' }])
    expect(out.ok && out.html).toBe('<p>Intro.</p><p>She grinned.<img src="pic"></p><p>End.</p>')
  })

  it('leaves a replacement that brings its own image alone', () => {
    const out = applyParagraphEdits(html, [{ paragraph: 2, action: 'replace', html: '<p>Own <img src="other"></p>', startsWith: 'She smiled' }])
    expect(out.ok && out.html).toBe('<p>Intro.</p><p>Own <img src="other"></p><p>End.</p>')
  })

  it('still deletes the paragraph, image and all, on delete', () => {
    const out = applyParagraphEdits(html, [{ paragraph: 2, action: 'delete', startsWith: 'She smiled' }])
    expect(out.ok && out.html).toBe('<p>Intro.</p><p>End.</p>')
  })
})

describe('anchors on list entries whose own text starts with "•"', () => {
  it('apply with the anchor as the model copies it from "¶33 • • 第八章…"', () => {
    // 2026-10-13: four edits of an outline entry refused in a row.
    const html = '<ul><li><p>• 第七章 出发：…</p></li><li><p>• 第八章 渡口：阿林替朋友买了礼物。</p></li></ul>'
    for (const startsWith of ['• 第八章 渡口', '• • 第八章 渡口', '第八章 渡口']) {
      const out = applyParagraphEdits(html, [{ paragraph: 2, action: 'replace', startsWith, html: '<p>• 第八章 渡口（已写）</p>' }])
      expect(out.ok).toBe(true)
    }
    expect(applyParagraphEdits(html, [{ paragraph: 2, action: 'replace', startsWith: '第九章', html: '<p>x</p>' }]).ok).toBe(false)
  })
})

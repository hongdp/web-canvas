import { describe, it, expect } from 'vitest'
import { leadingH1Text, titleFollowingHeading, contentWithRenamedHeading } from '../titleSync'

describe('leadingH1Text', () => {
  it('reads a leading h1', () => {
    expect(leadingH1Text('<h1>第一章：雪夜</h1><p>正文</p>')).toBe('第一章：雪夜')
  })
  it('ignores an h1 that is not first', () => {
    expect(leadingH1Text('<p>intro</p><h1>Later</h1>')).toBeNull()
  })
  it('returns null for empty headings and no heading', () => {
    expect(leadingH1Text('<h1>   </h1><p>x</p>')).toBeNull()
    expect(leadingH1Text('<p>x</p>')).toBeNull()
  })
})

describe('titleFollowingHeading', () => {
  const doc = (h1: string) => `<h1>${h1}</h1><p>正文内容</p>`

  it('follows the heading when the heading itself changed', () => {
    expect(titleFollowingHeading(doc('旧标题'), doc('新标题'), '旧标题')).toBe('新标题')
  })

  it('follows a heading typed into a document that had none', () => {
    expect(titleFollowingHeading('<p>x</p>', doc('First Heading'), 'Chapter 1')).toBe('First Heading')
  })

  it('does NOT revert a renamed title when the heading is untouched', () => {
    // The reported bug: rename the chapter to 新章节名, then type in the body.
    // The h1 still says 旧标题 on both sides of the edit — the title must
    // survive. The old unconditional sync returned '旧标题' here.
    const before = doc('旧标题')
    const after = doc('旧标题') + '<p>再补一句</p>'
    expect(titleFollowingHeading(before, after, '新章节名')).toBeNull()
  })

  it('leaves the title alone when heading and title already agree', () => {
    expect(titleFollowingHeading(doc('同名'), doc('同名'), '同名')).toBeNull()
  })

  it('never clears a title when the h1 is deleted', () => {
    expect(titleFollowingHeading(doc('标题'), '<p>只剩正文</p>', '标题')).toBeNull()
  })

  it('lets a later h1 edit win over an earlier rename', () => {
    // After a rename sticks, deliberately editing the h1 re-couples them.
    expect(titleFollowingHeading(doc('旧标题'), doc('最终标题'), '新章节名')).toBe('最终标题')
  })
})

describe('contentWithRenamedHeading (rename → h1 linkage)', () => {
  it('rewrites a leading h1 to the committed title', () => {
    expect(contentWithRenamedHeading('<h1>旧标题</h1><p>正文</p>', '新章节名'))
      .toBe('<h1>新章节名</h1><p>正文</p>')
  })
  it('preserves h1 attributes', () => {
    expect(contentWithRenamedHeading('<h1 id="t">Old</h1><p>x</p>', 'New'))
      .toBe('<h1 id="t">New</h1><p>x</p>')
  })
  it('escapes markup-significant characters in the title', () => {
    expect(contentWithRenamedHeading('<h1>Old</h1><p>x</p>', 'A <b> & B'))
      .toBe('<h1>A &lt;b&gt; &amp; B</h1><p>x</p>')
  })
  it('does not insert a heading into a document that has none', () => {
    expect(contentWithRenamedHeading('<p>只有正文</p>', '新章节名')).toBeNull()
  })
  it('is a no-op when the heading already matches', () => {
    expect(contentWithRenamedHeading('<h1>同名</h1><p>x</p>', '同名')).toBeNull()
  })
  it('refuses to rewrite a heading holding pending diff markup', () => {
    const c = '<h1><span data-diff-id="d1" class="diff-addition">Old</span></h1><p>x</p>'
    expect(contentWithRenamedHeading(c, 'New')).toBeNull()
  })
  it('ignores empty titles', () => {
    expect(contentWithRenamedHeading('<h1>Old</h1><p>x</p>', '   ')).toBeNull()
  })

  it('round-trips with titleFollowingHeading without fighting', () => {
    // Rename commits → h1 rewritten; the editor then reports that content.
    // The follow logic must see heading==title and leave everything alone.
    const before = '<h1>旧标题</h1><p>正文</p>'
    const after = contentWithRenamedHeading(before, '新章节名')!
    expect(titleFollowingHeading(before, after, '新章节名')).toBeNull()
  })
})

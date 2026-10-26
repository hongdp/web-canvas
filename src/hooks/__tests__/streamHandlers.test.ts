/**
 * The streamed bubble and the completion notes: document markup that no
 * channel takes must never be shown, during the stream or after it.
 */
import { describe, it, expect } from 'vitest'
import { splitStreamingResponse, buildCompletionWarnings } from '../chat/streamHandlers'

const EDIT = '<edit>\n<<<<<<< SEARCH\n<p>a later sentence</p>\n=======\n<p>a smoothed sentence</p>\n>>>>>>> REPLACE\n</edit>'

describe('splitStreamingResponse — markup beside a selection stays out of the bubble', () => {
  it('hides an edit that follows the selection block', () => {
    const r = splitStreamingResponse(`Done.\n<selection_replace><p>x</p></selection_replace>\n${EDIT}`)
    expect(r.isSelectionEdit).toBe(true)
    expect(r.selectionReplaceText).toBe('<p>x</p>')
    expect(r.chatText.trim()).toBe('Done.')
  })

  it('hides an edit that precedes the selection block', () => {
    const r = splitStreamingResponse(`Done.\n${EDIT}\n<selection_replace><p>x</p>`)
    expect(r.isSelectionEdit).toBe(true)
    expect(r.chatText.trim()).toBe('Done.')
  })

  it('hides an edit that follows a closed canvas', () => {
    const r = splitStreamingResponse(`Done.\n<canvas><p>doc</p></canvas>\n${EDIT}`)
    expect(r.canvasText).toBe('<p>doc</p>')
    expect(r.chatText.trim()).toBe('Done.')
  })

  it('still shows chat that follows the selection block', () => {
    const r = splitStreamingResponse('Done.\n<selection_replace><p>x</p></selection_replace>\nAnything else?')
    expect(r.chatText).toBe('Done.\n\nAnything else?')
  })
})

describe('buildCompletionWarnings — markup no channel could take', () => {
  const base = { canvasIssue: null, editFailedCount: 0, exhaustedNoActionRetries: false, reinsertedImages: 0 }

  it('says such changes existed, without showing them', () => {
    expect(buildCompletionWarnings({ ...base, strayMarkup: 2 })).toContain('This reply also contained 2 document changes')
    expect(buildCompletionWarnings({ ...base, strayMarkup: 1 })).toContain('1 document change that could not be applied')
  })

  it('stays quiet when a louder note already says the update failed as a whole', () => {
    expect(buildCompletionWarnings({ ...base, strayMarkup: 1, exhaustedNoActionRetries: true })).not.toContain('This reply also contained')
    expect(buildCompletionWarnings({ ...base, strayMarkup: 1, unretriableFailedUpdate: true })).not.toContain('This reply also contained')
  })

  it('adds nothing when there was none', () => {
    expect(buildCompletionWarnings({ ...base, strayMarkup: 0 })).toBe('')
  })
})

describe('splitStreamingResponse — a canvas that names a chapter', () => {
  it('reports the target and streams the body', () => {
    const split = splitStreamingResponse('Writing it.\n<canvas chapter="4"><p>第一章正')
    expect(split.canvasChapter).toBe('4')
    expect(split.canvasText).toBe('<p>第一章正')
    expect(split.chatText).toBe('Writing it.')
  })

  it('keeps a half-arrived opening tag out of the bubble', () => {
    expect(splitStreamingResponse('Writing it.\n<canvas chap').chatText).toBe('Writing it.')
  })

  it('has no target for a plain canvas', () => {
    expect(splitStreamingResponse('<canvas><p>x').canvasChapter).toBeUndefined()
  })

  it('reports the title of a chapter the canvas creates, not a chapter it names', () => {
    const split = splitStreamingResponse('写第二章。\n<canvas new_chapter="第二章 进城"><p>城门')
    expect(split.canvasNewChapter).toBe('第二章 进城')
    expect(split.canvasChapter).toBeUndefined()
    expect(split.canvasText).toBe('<p>城门')
    expect(split.chatText).toBe('写第二章。')
  })

  it('creates nothing while the opening tag is still arriving', () => {
    const split = splitStreamingResponse('<canvas new_chapter="第二')
    expect(split.canvasNewChapter).toBeUndefined()
    expect(split.chatText).toBe('')
  })
})

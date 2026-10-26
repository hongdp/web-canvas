/**
 * Pure pieces of the chat streaming pipeline: the incremental tag splitter
 * run on every chunk, and the no-action-retry constants + completion-warning
 * builder used by onDone. Nothing here touches the editor, hook refs, or the
 * store — the ref-coupled parts (live previews, editor transactions, the
 * retry re-dispatch) stay in useChatLLM.
 */
import { stripDocStatus, chapterAttribute, newChapterAttribute } from '../../utils/text'

// Recovery rounds per turn when the model answers a write request without any
// action tag (nothing reaches the document). Measured against grok-4.5 on a
// real chapter-rewrite request (n=8, 2026-07-25): 0 retries → 2/8 turns
// produced content, 1 → 4/8, 2 → 7/8, 3 → 8/8. Failures are independent
// re-rolls, not a stuck state, and a failed round costs ~25 output tokens, so
// three is the point where the curve flattens.
/**
 * Bubble text between "the turn started" and the first token. Shared so the
 * chat panel can recognise the wait and show it as such, rather than showing a
 * spinner that looks identical to a stalled connection.
 */
export const ASSISTANT_PLACEHOLDER = 'Thinking...'

/** Written over a placeholder once the server confirms no job can fill it. */
export const INTERRUPTED_NOTICE =
  '⚠️ Interrupted before the model replied (the page reloaded). Send again to retry.'

/**
 * Written when a rejoin could not even attach. Deliberately not "Stopped":
 * nobody stopped anything, and the job is very likely still generating.
 */
export const RECONNECT_FAILED_NOTICE =
  '⚠️ Could not reconnect to this generation — it may still be running on the server. Reload the page to try again.'

/**
 * A bubble that never received its reply: still the placeholder, or a notice
 * this client wrote in its place. Notices count because they are verdicts
 * reached without the server's say (or before it could be asked) — if a job
 * for the bubble turns up after all, the reply must still be able to land.
 * A user's "Stopped" is NOT here: that turn was ended on purpose.
 */
export const isUnfinishedBubble = (content: string): boolean =>
  content === ASSISTANT_PLACEHOLDER || content === INTERRUPTED_NOTICE || content === RECONNECT_FAILED_NOTICE

/** How much of the model's thinking to keep on screen. */
export const REASONING_TAIL_CHARS = 240
/** Minimum gap between reasoning repaints — deltas arrive far faster. */
export const REASONING_PAINT_MS = 250

export const MAX_NO_ACTION_RETRIES = 3
export const NO_ACTION_RETRY_INSTRUCTION = `Your previous reply did not follow the output protocol, so NOTHING reached the document — the user saw only your message.

Redo this turn. Exactly one of these two shapes is acceptable:
- You are changing the document: emit the change inside <canvas>, <edit> or <selection_replace>, with the full content (no summaries, no "as above"), and end with <doc_status>updated</doc_status>.
- You are NOT changing the document: answer normally, say what you need from the user, and end with <doc_status>unchanged</doc_status>. This is a perfectly good answer — but you may not say or imply that you edited anything.

The <doc_status> line is required either way, and it must match what you actually emitted.`

/** How a partially-streamed response splits into chat text vs. document markup. */
export interface StreamingSplit {
  /** Conversational text destined for the chat bubble. */
  chatText: string
  /** Body of a (possibly still-open) <canvas> block; empty when absent. */
  canvasText: string
  /**
   * The chapter the canvas's `chapter="…"` attribute names. The live preview
   * is routed by it: another chapter's text must never paint over the one
   * that is open (spec D2).
   */
  canvasChapter?: string
  /** The title the canvas's `new_chapter="…"` attribute creates: the preview creates it. */
  canvasNewChapter?: string
  /** Body of a (possibly still-open) <selection_replace> block; empty when absent. */
  selectionReplaceText: string
  isSelectionEdit: boolean
}

/**
 * Locate a passage by its TEXT in a live ProseMirror document.
 *
 * Used when a reload destroyed the selection range: `<edit>` blocks have
 * always relocated themselves by content, and a selection rewrite can do the
 * same instead of being dropped.
 *
 * It walks the real node tree rather than doing string arithmetic on HTML. A
 * plain-text index is NOT a ProseMirror position — every block boundary adds
 * one — so an offset computed from a string lands further off with each
 * preceding paragraph, which is worse than not relocating at all.
 *
 * Returns null when the passage is missing or ambiguous: rewriting the wrong
 * paragraph is a worse outcome than reporting the selection gone.
 */
export interface TextNodeSpan {
  text: string
  from: number
}

/**
 * The selection arrives as an HTML fragment — Editor.tsx serialises the
 * ProseMirror slice, so one paragraph is `<p>text</p>` and two are
 * `<p>a</p><p>b</p>`. The haystack here is the document flattened to text with
 * no tags and no separators between blocks, so the needle has to be reduced
 * the same way or it can never match. It never did: relocation returned null
 * for EVERY resumed selection, single-block ones included.
 *
 * Block tags collapse to nothing rather than to a space, which is what makes a
 * multi-block selection line up with the concatenated text spans.
 */
export function selectionNeedleFromHtml(selectedText: string): string {
  if (!selectedText || selectedText.indexOf('<') === -1) return (selectedText || '').trim()
  const doc = document.implementation.createHTMLDocument('')
  const div = doc.createElement('div')
  // Inert document: no browsing context, so embedded <img src="data:..."> in a
  // selection is never fetched or decoded (same reason as in Editor.tsx).
  div.innerHTML = selectedText
  return (div.textContent || '').trim()
}

export function findTextRangeInSpans(spans: TextNodeSpan[], selectedText: string): { from: number; to: number } | null {
  const needle = selectionNeedleFromHtml(selectedText)
  if (!needle) return null

  // One flat string, with a map back to document positions per character.
  let flat = ''
  const positions: number[] = []
  for (const span of spans) {
    for (let i = 0; i < span.text.length; i++) {
      flat += span.text[i]
      positions.push(span.from + i)
    }
  }

  const first = flat.indexOf(needle)
  if (first === -1) return null
  if (flat.indexOf(needle, first + 1) !== -1) return null   // ambiguous

  const from = positions[first]
  const lastChar = positions[first + needle.length - 1]
  return { from, to: lastChar + 1 }
}

/** Collect the text nodes of a ProseMirror doc with their positions. */
export function collectTextSpans(doc: {
  descendants: (fn: (node: { isText?: boolean; text?: string }, pos: number) => void) => void
}): TextNodeSpan[] {
  const spans: TextNodeSpan[] = []
  doc.descendants((node, pos) => {
    if (node.isText && node.text) spans.push({ text: node.text, from: pos })
  })
  return spans
}

/**
 * Recover the range of a selection whose page died mid-turn.
 *
 * A resumed job carries the selected TEXT, not positions — ProseMirror
 * positions do not survive the editor that produced them. Relocating it
 * against the live document is what lets a rejoined selection rewrite preview
 * and settle where the user actually selected. No-ops once relocated, and
 * whenever there was no resumed selection to begin with.
 *
 * Module-level, taking the refs as arguments, deliberately: a useCallback here
 * would capture them and destabilise the streaming callbacks, whose identity
 * has to stay fixed (see the timeout note in CLAUDE.md — an unstable callback
 * in effect deps is how the test runner wedges). It is shared by all three
 * callers — tool-call preview, markup preview, and the final apply — because
 * the markup one was MISSING it, so after a refresh `selectionRange` stayed
 * null and the live preview was skipped for the whole turn.
 */
export function relocateResumedSelection(
  editor: { state: { doc: Parameters<typeof collectTextSpans>[0] } },
  refs: {
    pendingSelectionText: { current: string | null }
    selectionRange: { current: { from: number; to: number } | null }
    selectionEnd: { current: number | null }
  }
): void {
  const pending = refs.pendingSelectionText.current
  if (!pending) return
  const relocated = findTextRangeInSpans(collectTextSpans(editor.state.doc), pending)
  // A FAILED lookup must not consume the pending text. On a reload the editor
  // mounts before the server sync fills it, so the first chunks search an
  // empty document — and when this cleared `pending` regardless, the final
  // apply had nothing left to relocate with either, losing the diff as well
  // as the preview. Leave it set and let a later chunk (or the apply) retry.
  if (!relocated) return
  refs.pendingSelectionText.current = null
  refs.selectionRange.current = relocated
  refs.selectionEnd.current = relocated.to
}

export function clampSelectionRange(
  from: number,
  to: number,
  docSize: number
): { from: number; to: number } | null {
  if (!Number.isFinite(from) || !Number.isFinite(to) || docSize < 0) return null
  if (from < 0 || from > docSize) return null
  return { from, to: Math.min(Math.max(to, from), docSize) }
}

/**
 * Split the accumulated raw stream into conversational text and document
 * markup. Called on every chunk with the FULL text so far — it must tolerate
 * tags that have opened but not yet closed.
 */
// Where document markup begins in text that is otherwise chat. Hidden while
// streaming; the final parse applies it or reports it, and never shows it.
const MARKUP_START_RE = /<edits?\b|<{5,}\s*SEARCH|<canvas\b|<selection_replace>/i
const chatPart = (text: string): string => {
  const at = text.search(MARKUP_START_RE)
  return (at === -1 ? text : text.slice(0, at)).trim()
}

export function splitStreamingResponse(raw: string): StreamingSplit {
  let chatText: string
  let canvasText = ''
  let canvasChapter: string | undefined
  let canvasNewChapter: string | undefined
  let selectionReplaceText = ''
  let isSelectionEdit = false

  const canvasEnd = '</canvas>'
  const selectionStart = '<selection_replace>'
  const selectionEndTag = '</selection_replace>'

  // The opening tag may carry a chapter attribute; it counts once its `>`
  // has arrived. Until then chatPart below keeps the half-tag out of the bubble.
  const canvasOpen = /<canvas\b[^>]*>/i.exec(raw)
  const canvasIdx = canvasOpen ? canvasOpen.index : -1
  const selectionIdx = raw.indexOf(selectionStart)
  // First sign of an <edit> block (open tag or a SEARCH conflict marker).
  const editMatchIdx = raw.search(/<edit\b|<{5,}\s*SEARCH/i)

  if (selectionIdx !== -1) {
    isSelectionEdit = true
    chatText = chatPart(raw.substring(0, selectionIdx))
    const rest = raw.substring(selectionIdx + selectionStart.length)
    const endIdx = rest.indexOf(selectionEndTag)
    if (endIdx !== -1) {
      selectionReplaceText = rest.substring(0, endIdx)
      chatText += '\n\n' + chatPart(rest.substring(endIdx + selectionEndTag.length))
    } else {
      selectionReplaceText = rest
    }
  } else if (editMatchIdx !== -1 && (canvasIdx === -1 || editMatchIdx < canvasIdx)) {
    // Edit blocks are applied on completion; during streaming just hide
    // the noisy SEARCH/REPLACE markup and show the surrounding chat.
    chatText = raw.substring(0, editMatchIdx).trim()
  } else if (canvasIdx !== -1) {
    chatText = raw.substring(0, canvasIdx).trim()
    canvasChapter = chapterAttribute((canvasOpen as RegExpExecArray)[0])
    canvasNewChapter = newChapterAttribute((canvasOpen as RegExpExecArray)[0])
    const rest = raw.substring(canvasIdx + (canvasOpen as RegExpExecArray)[0].length)
    const endIdx = rest.indexOf(canvasEnd)
    if (endIdx !== -1) {
      canvasText = rest.substring(0, endIdx)
      chatText += '\n\n' + chatPart(rest.substring(endIdx + canvasEnd.length))
    } else {
      canvasText = rest
    }
  } else {
    // A tag still arriving (`<canvas chap`) is markup, not chat.
    chatText = MARKUP_START_RE.test(raw) ? chatPart(raw) : raw
  }

  return { chatText: stripDocStatus(chatText), canvasText, canvasChapter, canvasNewChapter, selectionReplaceText, isSelectionEdit }
}

/**
 * Assemble the user-facing warning suffix appended to the assistant bubble
 * after a completed stream. Order is deliberate: destructive-replacement
 * guards first, then skipped edits, then the exhausted no-action retries,
 * then the informational image-restore note.
 */
export function buildCompletionWarnings(params: {
  canvasIssue: 'truncated' | 'elided' | null
  editFailedCount: number
  /** All no-action retries used and the last reply still had no tags. */
  exhaustedNoActionRetries: boolean
  /** A finished selection edit had nowhere valid left to land. */
  selectionGone?: boolean
  /** The reply produced no usable document update and cannot be retried. */
  unretriableFailedUpdate?: boolean
  /** A document tool was called but its arguments yielded nothing to apply. */
  toolCallProducedNothing?: boolean
  /** Document-markup regions no channel could take, dropped from the bubble. */
  strayMarkup?: number
  reinsertedImages: number
}): string {
  const { canvasIssue, editFailedCount, exhaustedNoActionRetries, selectionGone, unretriableFailedUpdate, toolCallProducedNothing, strayMarkup, reinsertedImages } = params

  let warningNote = canvasIssue === 'truncated'
    ? '\n\n⚠️ The response was cut off before the document update finished, so no changes were applied (your document is unchanged). Please retry — for long documents, try editing a smaller selection at a time.'
    : canvasIssue === 'elided'
    ? '\n\n⚠️ The response abbreviated unchanged parts of the document, so applying it would have deleted content. No changes were applied. Please retry — for long documents, try editing a smaller selection at a time.'
    : ''
  if (editFailedCount > 0) {
    warningNote += `\n\n⚠️ ${editFailedCount} suggested change${editFailedCount > 1 ? 's' : ''} could not be located in the current document and ${editFailedCount > 1 ? 'were' : 'was'} skipped. The text to change may have moved or differ from what was matched.`
  }
  // Markup that no channel took is never shown raw; say it existed. Not when a
  // louder note already explains that the reply's update failed as a whole.
  if (strayMarkup && strayMarkup > 0 && !exhaustedNoActionRetries && !unretriableFailedUpdate) {
    const many = strayMarkup > 1
    warningNote += `\n\n⚠️ This reply also contained ${strayMarkup} document change${many ? 's' : ''} that could not be applied alongside the main one, so ${many ? 'they were' : 'it was'} left out of this message. Ask again if ${many ? 'they matter' : 'it matters'}.`
  }
  // The recovery round also came back without tags: say so instead
  // of letting "已改好" stand over an unchanged document.
  if (exhaustedNoActionRetries) {
    warningNote += `\n\n⚠️ The model never produced a valid document update or a clear "no change" declaration (retried ${MAX_NO_ACTION_RETRIES} times), so your document is unchanged. Ask again — naming the chapter or section usually helps.`
  }
  if (toolCallProducedNothing) {
    warningNote += '\n\n⚠️ The model started a document change but its request could not be used (empty or malformed arguments), so your document is unchanged. Try again — a shorter, more specific instruction usually helps.'
  }
  if (unretriableFailedUpdate) {
    warningNote += '\n\n⚠️ This reply produced no usable document update, and it was resumed after a reload — so there is no request left to retry with. Your document is unchanged; send the instruction again to have another go.'
  }
  if (selectionGone) {
    warningNote += '\n\n⚠️ The text you had selected is no longer where it was (the chapter changed while this ran), so nothing was written. The rewrite is above — paste it where you want it, or select the text again and retry.'
  }
  if (reinsertedImages > 0) {
    warningNote += `\n\nℹ️ ${reinsertedImages} image${reinsertedImages > 1 ? 's' : ''} missing from the rewrite ${reinsertedImages > 1 ? 'were' : 'was'} restored near ${reinsertedImages > 1 ? 'their' : 'its'} original position. Delete ${reinsertedImages > 1 ? 'them' : 'it'} manually if the removal was intended.`
  }
  return warningNote
}

/** Sent with the final step when the budget, not the model, ends the run (wc_agent/run.py; the Python is the loop of record). */
export const STEP_LIMIT_NOTE =
  'This turn has used its step budget. Do not call any more tools: answer the user now with what you have, and say what is left undone.'

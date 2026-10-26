import { htmlToPlainText } from './convert'

/**
 * Title-follows-heading, as a pure decision function.
 *
 * The editor change handler keeps a chapter's title in sync with a leading
 * <h1>. The first version synced UNCONDITIONALLY — whenever the h1 text
 * differed from the title, the title was overwritten. That made renaming a
 * chapter through the header input impossible on any document that starts
 * with an h1: the very next keystroke in the body (or LLM stream write)
 * found h1 ≠ title and reverted the rename (user-reported, 2026-08-30).
 *
 * The rule now is DIRECTIONAL: the title follows the heading only when the
 * heading itself changed in this edit. A rename through the header input
 * leaves the h1 untouched, so old-h1 === new-h1 and the rename sticks. Edit
 * the h1 later and it wins again — both rename surfaces work.
 *
 * Perf contract (do not "simplify" back to DOM parsing): this runs on every
 * editor update. Matching is a bounded regex over the first 4KB — no
 * innerHTML, which materializes <img> elements and re-decoded hundreds of base64
 * images per keystroke on large imports (see the original fix in App.tsx).
 */
const LEADING_H1 = /^\s*<h1[^>]*>([\s\S]{0,2000}?)<\/h1>/i

/** Plain text of a leading <h1>, or null if the content has none. */
export function leadingH1Text(html: string): string | null {
  const match = LEADING_H1.exec(html.slice(0, 4096))
  if (!match) return null
  const text = htmlToPlainText(match[1]).trim()
  return text || null
}

/**
 * The title the edited document should adopt, or null to leave it alone.
 *
 * @param prevContent the document's content before this edit
 * @param nextContent the editor's new HTML
 * @param currentTitle the chapter's current title
 */
export function titleFollowingHeading(
  prevContent: string,
  nextContent: string,
  currentTitle: string | undefined
): string | null {
  const nextH1 = leadingH1Text(nextContent)
  if (!nextH1) return null                      // no heading, or it was removed: never clear a title
  if (nextH1 === currentTitle) return null      // already in sync
  const prevH1 = leadingH1Text(prevContent)
  if (nextH1 === prevH1) return null            // heading untouched — the TITLE was renamed; respect it
  return nextH1
}

/** Minimal escaping for text placed inside an <h1> element. */
function escapeHtmlText(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/**
 * The reverse linkage: content with its leading <h1> rewritten to the new
 * title, or null when there is nothing to rewrite — no leading h1 (a rename
 * must not restructure the document by inserting one), heading already says
 * this, empty title, or the heading carries pending diff markup (rewriting
 * would silently destroy spans the user is still reviewing).
 *
 * Called on COMMIT of the title input (blur / Enter), not per keystroke: a
 * content update re-parses the whole document in the editor, which a very
 * large import cannot afford per key.
 */
export function contentWithRenamedHeading(content: string, newTitle: string): string | null {
  const title = newTitle.trim()
  if (!title) return null
  const match = LEADING_H1.exec(content.slice(0, 4096))
  if (!match) return null
  if (match[0].includes('data-diff')) return null
  if (htmlToPlainText(match[1]).trim() === title) return null
  const start = content.indexOf(match[0])
  const rewritten = match[0].replace(match[1], escapeHtmlText(title))
  return content.slice(0, start) + rewritten + content.slice(start + match[0].length)
}

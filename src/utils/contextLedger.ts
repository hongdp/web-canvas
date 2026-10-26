/**
 * The context ledger: what has already been sent to the model, in the order it
 * was sent. See docs/features/cache_first_context.md.
 *
 * Problem: an ordinary chat turn spent 42.87s before its first token, all of
 *   it prefill, because every attached reference chapter is rebuilt into the
 *   final user message each turn — and because the attachment order is a score
 *   recomputed from the new prompt, an UNCHANGED set of chapters was emitted in
 *   a different order turn over turn. The same engine replays an identical
 *   prefix in 0.2s.
 * Fix: chapters live in an append-only block ahead of the history. Score
 *   decides admission; insertion order decides position, forever. When
 *   something must leave the middle, keep the longest valid prefix and re-send
 *   only the suffix.
 *
 * This module is pure: it plans, it does not render. Rendering lives in
 * hooks/chat/dynamicContext.ts.
 */

/** One chapter as it currently sits in the ledger. */
export interface LedgerEntry {
  id: string
  /** Cheap fingerprint of the exact bytes sent, to detect an edited chapter. */
  hash: string
  /** Chars this entry contributes, for quoting the cost of losing it. */
  chars: number
  /**
   * The exact block that was sent. Kept so an entry renders the SAME bytes
   * every turn even after its chapter changed: with append-updates an old
   * copy stays in place, and only bytes that never change stay cached.
   * Absent on plans made without a renderer (the pre-append behaviour).
   */
  text?: string
  /**
   * A later entry with the same id replaces this one (an append-update).
   * Metadata only — the bytes are untouched; the replacing entry's header is
   * what tells the model this copy is outdated.
   */
  stale?: boolean
}

export interface ContextLedger {
  entries: LedgerEntry[]
}

export const EMPTY_LEDGER: ContextLedger = { entries: [] }

/** Why a chapter is leaving the ledger — drives what the user is told. */
export type DropReason =
  /** It became the active document; the volatile tail now holds it. */
  | 'now-active'
  /** Its content changed since it was sent. */
  | 'edited'
  /** The user's selection no longer includes it. */
  | 'user-removed'

export interface LedgerDrop {
  id: string
  reason: DropReason
}

export interface LedgerPlan {
  /** The ledger to send this turn. */
  ledger: ContextLedger
  /** Entries at the head that keep their exact bytes and position. */
  cachedPrefixCount: number
  /** Chars in that cached prefix — what the plan saved. */
  cachedPrefixChars: number
  /** Ids that keep their content but move, so they must be prefilled again. */
  resentIds: string[]
  /** Ids entering the ledger for the first time. */
  appendedIds: string[]
  /** Ids leaving, with the reason each one left. */
  drops: LedgerDrop[]
  /** Chars that must be prefilled again this turn (resent + appended). */
  resendChars: number
  /**
   * Ids whose new version was APPENDED while their old copy stayed in place
   * (append-update) — instead of cutting the ledger at the old copy.
   */
  updatedIds: string[]
  /** Chars held by outdated copies after this plan; consolidated past a budget. */
  staleChars: number
  /**
   * True when the user's own selection is what forces cached chapters out, so
   * the send should stop and ask. An active-document switch or an edit is not
   * a choice the user can reconsider, so neither raises this.
   */
}

/** What a chapter's block says it is: a first copy, or a newer version of one above. */
export type LedgerRenderKind = 'fresh' | 'update'

export interface LedgerPlanOptions {
  /**
   * Render a chapter's block. Supplying it turns on append-updates and makes
   * every new entry carry its exact bytes (`LedgerEntry.text`).
   */
  render?: (id: string, kind: LedgerRenderKind) => string
  /**
   * Outdated copies may hold at most this many chars before the ledger is
   * consolidated (cut at the first outdated copy, re-sent clean). Default:
   * the larger of 20k chars and 30% of the ledger.
   */
  maxStaleChars?: number
  /**
   * Entries kept as they are — position and bytes — whatever else would drop
   * them: a pinned chapter the writer has open (pinned_context.md §2.1).
   * Opening one used to drop it from the ledger and closing it re-added it,
   * two cache misses for a look at the outline. Never appended by this.
   */
  keepIds?: string[]
}

/** Minimal document shape the planner needs. */
export interface LedgerDocLike {
  id: string
  chars: number
  hash: string
}

/**
 * Fingerprint chapter bytes. FNV-1a over the string: cheap, allocation-free,
 * and collisions only cost a missed invalidation of one chapter — this is not
 * a security boundary.
 */
export function hashContent(text: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return `${(h >>> 0).toString(36)}-${text.length}`
}

/**
 * Plan this turn's ledger.
 *
 * `desiredIds` is what the selector wants attached, in the order it would like
 * NEW entries appended; entries already in the ledger keep their old position
 * regardless of where they appear here.
 *
 * The head of the ledger survives up to the first entry that must leave. That
 * cut point is what makes a chapter switch cheap when the chapter was near the
 * end and expensive when it was near the front.
 *
 * Append-updates (with `options.render`). An EDITED chapter used to be a cut:
 * everything after its old position was re-sent — measured on a real turn,
 * 4,224 of 46,312 prompt tokens cached (9%) after the writer revised the
 * outline mid-ledger. Now its old copy stays where it is, byte for byte, and
 * the new version is appended with a header saying it replaces the copy
 * above (append, never mutate — the same rule Manus and Codex follow for
 * their context). It is taken only when it is cheaper: when what sits after
 * the old copy is larger than the chapter itself. Outdated copies are
 * consolidated (a cut at the first of them) once they pass a budget, so the
 * ledger cannot grow without bound.
 */
export function planLedgerTurn(
  current: ContextLedger,
  desiredIds: string[],
  docs: LedgerDocLike[],
  activeDocumentId: string | null,
  options: LedgerPlanOptions = {}
): LedgerPlan {
  const byId = new Map(docs.map(d => [d.id, d]))
  const desired = new Set(desiredIds)
  const keep = new Set(options.keepIds ?? [])
  const render = options.render
  const entries = current.entries

  // Why each existing entry would have to leave, if at all.
  const dropReason = (entry: LedgerEntry): DropReason | null => {
    if (keep.has(entry.id) && byId.has(entry.id)) return null
    if (entry.id === activeDocumentId) return 'now-active'
    const doc = byId.get(entry.id)
    // A chapter that vanished from the book is treated as user-removed: the
    // user deleted it, and the bytes cannot be re-sent either way.
    if (!doc) return 'user-removed'
    if (!desired.has(entry.id)) return 'user-removed'
    // An outdated copy is judged by its chapter, not its bytes: it stays
    // while the chapter stays, whatever the chapter says now.
    if (entry.stale) return null
    if (doc.hash !== entry.hash) return 'edited'
    return null
  }

  const charsFrom = (i: number) => entries.slice(i).reduce((sum, e) => sum + e.chars, 0)

  // 1. Walk to the cut. An edited chapter becomes an append-update when that
  //    re-sends less than cutting would.
  let cut = entries.length
  const updates: number[] = []
  for (let i = 0; i < entries.length; i++) {
    const reason = dropReason(entries[i])
    if (reason === null) continue
    if (reason === 'edited' && render && charsFrom(i + 1) > (byId.get(entries[i].id)?.chars ?? 0)) {
      updates.push(i)
      continue
    }
    cut = i
    break
  }

  // 2. Consolidate when outdated copies pass the budget: cut at the first of
  //    them, and everything after is re-sent clean.
  const ledgerChars = charsFrom(0)
  const budget = options.maxStaleChars ?? Math.max(20_000, Math.floor(ledgerChars * 0.3))
  const outdated = (i: number) => entries[i].stale || updates.includes(i)
  const staleBefore = (limit: number) =>
    entries.slice(0, limit).reduce((sum, e, i) => sum + (outdated(i) ? e.chars : 0), 0)
  if (staleBefore(cut) > budget) {
    const first = entries.findIndex((_, i) => outdated(i))
    if (first !== -1 && first < cut) cut = first
  }
  const activeUpdates = updates.filter(i => i < cut)

  const kept = entries.slice(0, cut).map((e, i) => (activeUpdates.includes(i) ? { ...e, stale: true } : e))
  const cachedPrefixChars = kept.reduce((sum, e) => sum + e.chars, 0)
  const keptStaleIds = new Set(kept.filter(e => e.stale).map(e => e.id))

  // A block for `id`, marked as a newer version when an older copy of it
  // sits in the kept prefix.
  const entryFor = (id: string, doc: LedgerDocLike): LedgerEntry => {
    const kind: LedgerRenderKind = keptStaleIds.has(id) ? 'update' : 'fresh'
    return render
      ? { id, hash: doc.hash, chars: doc.chars, text: render(id, kind) }
      : { id, hash: doc.hash, chars: doc.chars }
  }

  // 3. Everything from the cut on: survivors keep their relative order but
  //    move, so they are re-sent; the rest are dropped with their reason.
  //    Outdated copies past the cut are simply gone — what is re-sent is
  //    re-sent current.
  const drops: LedgerDrop[] = []
  const resentIds: string[] = []
  const tail: LedgerEntry[] = []
  for (const entry of entries.slice(cut)) {
    if (entry.stale) continue
    const reason = dropReason(entry)
    if (reason !== null) {
      drops.push({ id: entry.id, reason })
      continue
    }
    tail.push(entryFor(entry.id, byId.get(entry.id) as LedgerDocLike))
    resentIds.push(entry.id)
  }

  // 4. Append-updates, then new admissions, after everything that survived.
  const present = new Set([...kept.filter(e => !e.stale), ...tail].map(e => e.id))
  const updatedIds: string[] = []
  for (const i of activeUpdates) {
    const id = entries[i].id
    if (present.has(id)) continue
    tail.push(entryFor(id, byId.get(id) as LedgerDocLike))
    updatedIds.push(id)
    present.add(id)
  }
  const appendedIds: string[] = []
  for (const id of desiredIds) {
    if (present.has(id) || id === activeDocumentId) continue
    const doc = byId.get(id)
    if (!doc) continue
    tail.push(entryFor(id, doc))
    appendedIds.push(id)
    present.add(id)
  }

  const resendChars = tail.reduce((sum, e) => sum + e.chars, 0)
  const all = [...kept, ...tail]

  return {
    ledger: { entries: all },
    cachedPrefixCount: kept.length,
    cachedPrefixChars,
    resentIds,
    appendedIds,
    drops,
    resendChars,
    updatedIds,
    staleChars: all.reduce((sum, e) => sum + (e.stale ? e.chars : 0), 0)
  }
}

/** The ledger's chapters, once each, in the order they first appear. */
export function ledgerChapterIds(ledger: ContextLedger): string[] {
  return [...new Set(ledger.entries.map(e => e.id))]
}

/** What the stability ordering needs to know about a document. */
export interface StabilityDocLike {
  id: string
  /** ISO timestamp of the last edit. Missing sorts as "very old". */
  updatedAt?: string
}

/**
 * Order new admissions from most stable to least stable, so the chapters most
 * likely to change next sit at the END of the ledger — where invalidating them
 * costs nothing but themselves.
 *
 * Removing an entry re-sends everything after it, so position is a bet on how
 * long a chapter will stay untouched. `updatedAt` is the honest signal: a
 * chapter finished last week is stable; the outline the writer revises after
 * every session is not.
 *
 * This replaces an earlier book-distance heuristic that was exactly wrong for
 * this app's main workflow. A permanently pinned outline is usually chapter
 * zero — far from wherever the writer is working — so distance sorted it
 * FIRST, the most expensive slot, while it is in fact the most frequently
 * edited document in the book. Book distance survives only as a tie-break.
 */
export function orderAdmissionsByStability(
  ids: string[],
  docs: StabilityDocLike[],
  bookOrder: string[],
  activeDocumentId: string | null
): string[] {
  const byId = new Map(docs.map(d => [d.id, d]))
  const activeIdx = bookOrder.indexOf(activeDocumentId ?? '')

  const editedAt = (id: string) => {
    const raw = byId.get(id)?.updatedAt
    const t = raw ? Date.parse(raw) : NaN
    return Number.isNaN(t) ? 0 : t
  }
  const distance = (id: string) => {
    const idx = bookOrder.indexOf(id)
    if (idx === -1 || activeIdx === -1) return Number.MAX_SAFE_INTEGER
    return Math.abs(idx - activeIdx)
  }

  return [...ids].sort((a, b) => {
    // Oldest edit first — it is the least likely to be invalidated.
    if (editedAt(a) !== editedAt(b)) return editedAt(a) - editedAt(b)
    // Then farthest from the writing frontier, for the same reason.
    return distance(b) - distance(a)
  })
}

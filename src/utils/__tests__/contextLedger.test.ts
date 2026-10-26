import { describe, it, expect } from 'vitest'
import {
  EMPTY_LEDGER,
  hashContent,
  planLedgerTurn,
  orderAdmissionsByStability,
  type ContextLedger,
  type LedgerDocLike
} from '../contextLedger'

/** A book of chapters a..e, 1000 chars each unless overridden. */
const makeDocs = (overrides: Record<string, string> = {}): LedgerDocLike[] =>
  ['a', 'b', 'c', 'd', 'e'].map(id => {
    const body = overrides[id] ?? `${id}-original`
    return { id, chars: overrides[id] ? body.length : 1000, hash: hashContent(body) }
  })

const ledgerOf = (ids: string[], docs: LedgerDocLike[]): ContextLedger => ({
  entries: ids.map(id => {
    const d = docs.find(x => x.id === id)!
    return { id, hash: d.hash, chars: d.chars }
  })
})

const ids = (l: ContextLedger) => l.entries.map(e => e.id)

describe('planLedgerTurn — the append-only invariant', () => {
  it('appends a new chapter without disturbing what was already sent', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b'], docs)

    const plan = planLedgerTurn(before, ['a', 'b', 'c'], docs, null)

    expect(ids(plan.ledger)).toEqual(['a', 'b', 'c'])
    expect(plan.cachedPrefixCount).toBe(2)
    expect(plan.appendedIds).toEqual(['c'])
    expect(plan.resentIds).toEqual([])
    expect(plan.resendChars).toBe(1000)
  })

  it('ignores the order the selector asks for — position is insertion order', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c'], docs)

    // The scorer ranked them c, a, b this turn. That must change nothing.
    const plan = planLedgerTurn(before, ['c', 'a', 'b'], docs, null)

    expect(ids(plan.ledger)).toEqual(['a', 'b', 'c'])
    expect(plan.cachedPrefixCount).toBe(3)
    expect(plan.resendChars).toBe(0)
  })

  it('is a no-op when nothing changed', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c'], docs)

    const plan = planLedgerTurn(before, ['a', 'b', 'c'], docs, null)

    expect(plan.cachedPrefixCount).toBe(3)
    expect(plan.cachedPrefixChars).toBe(3000)
    expect(plan.resendChars).toBe(0)
    expect(plan.drops).toEqual([])
  })
})

describe('planLedgerTurn — switching the active document', () => {
  it('keeps the whole prefix when the new active chapter was last', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c'], docs)

    const plan = planLedgerTurn(before, ['a', 'b'], docs, 'c')

    expect(ids(plan.ledger)).toEqual(['a', 'b'])
    expect(plan.cachedPrefixCount).toBe(2)
    expect(plan.resendChars).toBe(0)
    expect(plan.drops).toEqual([{ id: 'c', reason: 'now-active' }])
    // Not a choice the user can reconsider — no consent panel.
  })

  it('re-sends only the suffix when the new active chapter was in the middle', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c', 'd', 'e'], docs)

    const plan = planLedgerTurn(before, ['a', 'b', 'd', 'e'], docs, 'c')

    expect(ids(plan.ledger)).toEqual(['a', 'b', 'd', 'e'])
    expect(plan.cachedPrefixCount).toBe(2)      // a, b survive in place
    expect(plan.resentIds).toEqual(['d', 'e'])  // shifted up, must be prefilled
    expect(plan.resendChars).toBe(2000)
    expect(plan.drops).toEqual([{ id: 'c', reason: 'now-active' }])
  })

  it('never leaves a stale copy of the chapter now being edited', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c'], docs)

    // Even if the selector still asks for it, the active document belongs to
    // the volatile tail alone — two copies is how an <edit> SEARCH goes stale.
    const plan = planLedgerTurn(before, ['a', 'b', 'c'], docs, 'b')

    expect(ids(plan.ledger)).not.toContain('b')
  })

  it('re-admits the chapter the writer just left, at the end', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c'], docs)

    // Was editing 'x' (not in the ledger), switches to 'c', and now the old
    // active chapter is a reference candidate.
    const plan = planLedgerTurn(before, ['a', 'b', 'x'], [...docs, { id: 'x', chars: 500, hash: hashContent('x') }], 'c')

    expect(ids(plan.ledger)).toEqual(['a', 'b', 'x'])
    expect(plan.appendedIds).toEqual(['x'])
    expect(plan.cachedPrefixCount).toBe(2)
  })
})

describe('planLedgerTurn — an edited chapter invalidates from its position', () => {
  it('re-sends the edited chapter and everything after it', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c'], docs)
    const edited = makeDocs({ b: 'b-rewritten-by-the-user' })

    const plan = planLedgerTurn(before, ['a', 'b', 'c'], edited, null)

    // 'b' leaves its old position and re-enters at the end, so everything
    // before it stays cached and only the suffix is prefilled again.
    expect(ids(plan.ledger)).toEqual(['a', 'c', 'b'])
    expect(plan.cachedPrefixCount).toBe(1)          // only 'a' survives
    expect(plan.drops).toEqual([{ id: 'b', reason: 'edited' }])
    expect(plan.appendedIds).toEqual(['b'])
    expect(plan.resentIds).toEqual(['c'])
  })
})

describe('planLedgerTurn — user removal', () => {
  // The consent gate that used to block this case is gone (removed by
  // request): a removal proceeds silently. The plan still prices it, so
  // these assertions keep the cost arithmetic honest.
  it('prices a user removal that costs cached chapters', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c'], docs)

    const plan = planLedgerTurn(before, ['a', 'c'], docs, null)

    expect(plan.drops).toEqual([{ id: 'b', reason: 'user-removed' }])
    expect(plan.cachedPrefixCount).toBe(1)
    expect(plan.resentIds).toEqual(['c'])
    expect(plan.resendChars).toBe(1000)
  })

  it('treats a chapter deleted from the book as a removal, not a crash', () => {
    const docs = makeDocs()
    const before = ledgerOf(['a', 'b', 'c'], docs)
    const withoutB = docs.filter(d => d.id !== 'b')

    const plan = planLedgerTurn(before, ['a', 'c'], withoutB, null)

    expect(ids(plan.ledger)).toEqual(['a', 'c'])
    expect(plan.drops).toEqual([{ id: 'b', reason: 'user-removed' }])
  })
})

describe('planLedgerTurn — starting from nothing', () => {
  it('appends the first turn in the order the selector asked for', () => {
    const docs = makeDocs()

    const plan = planLedgerTurn(EMPTY_LEDGER, ['c', 'a'], docs, 'b')

    expect(ids(plan.ledger)).toEqual(['c', 'a'])
    expect(plan.cachedPrefixCount).toBe(0)
    expect(plan.appendedIds).toEqual(['c', 'a'])
  })

  it('never admits the active document itself', () => {
    const docs = makeDocs()

    const plan = planLedgerTurn(EMPTY_LEDGER, ['a', 'b'], docs, 'a')

    expect(ids(plan.ledger)).toEqual(['b'])
  })
})

describe('hashContent', () => {
  it('separates content that differs only in length', () => {
    expect(hashContent('abc')).not.toBe(hashContent('abcd'))
  })

  it('is stable for identical input', () => {
    expect(hashContent('第一章 风雪')).toBe(hashContent('第一章 风雪'))
  })

  it('changes when a single character changes', () => {
    expect(hashContent('第一章 风雪')).not.toBe(hashContent('第一章 风霜'))
  })
})

describe('the writing workflows this is for', () => {
  // A pinned outline plus the two or three chapters around the writing
  // frontier, which is what this app is actually used with.
  const book = ['outline', 'ch7', 'ch8', 'ch9']
  const docs: LedgerDocLike[] = [
    { id: 'outline', chars: 3000, hash: hashContent('outline-v1') },
    { id: 'ch7', chars: 9000, hash: hashContent('ch7') },
    { id: 'ch8', chars: 9000, hash: hashContent('ch8') },
    { id: 'ch9', chars: 500, hash: hashContent('ch9') }
  ]
  const times = [
    { id: 'outline', updatedAt: '2026-08-17T10:00:00.000Z' },
    { id: 'ch7', updatedAt: '2026-06-01T00:00:00.000Z' },
    { id: 'ch8', updatedAt: '2026-08-16T00:00:00.000Z' },
    { id: 'ch9', updatedAt: '2026-08-17T09:00:00.000Z' }
  ]

  it('A: writing ch9 from the outline and ch8 costs nothing after turn one', () => {
    const ordered = orderAdmissionsByStability(['outline', 'ch7', 'ch8'], times, book, 'ch9')
    expect(ordered).toEqual(['ch7', 'ch8', 'outline'])   // outline last, by design

    const first = planLedgerTurn(EMPTY_LEDGER, ordered, docs, 'ch9')
    const second = planLedgerTurn(first.ledger, ordered, docs, 'ch9')
    expect(second.resendChars).toBe(0)
    expect(second.cachedPrefixCount).toBe(3)
  })

  it('B: folding the new chapter back into the outline is nearly free', () => {
    // The outline becomes the active document. Because stability put it last,
    // dropping it costs nothing after it. Sorted by book order it would have
    // sat FIRST and taken 18,000 characters of chapters down with it.
    const ledger = planLedgerTurn(
      EMPTY_LEDGER,
      orderAdmissionsByStability(['outline', 'ch7', 'ch8'], times, book, 'ch9'),
      docs,
      'ch9'
    ).ledger

    const switched = planLedgerTurn(ledger, ['ch7', 'ch8', 'ch9'], docs, 'outline')

    expect(switched.drops).toEqual([{ id: 'outline', reason: 'now-active' }])
    expect(switched.cachedPrefixCount).toBe(2)          // ch7, ch8 stay put
    expect(switched.resendChars).toBe(500)              // only the new ch9
  })

  it('B: the edited outline re-enters at the END, so the next turn is cheap again', () => {
    const ledger = planLedgerTurn(EMPTY_LEDGER, ['ch7', 'ch8'], docs, 'outline').ledger
    const edited = docs.map(d => d.id === 'outline' ? { ...d, hash: hashContent('outline-v2') } : d)

    const back = planLedgerTurn(ledger, ['ch7', 'ch8', 'outline'], edited, 'ch9')

    expect(back.ledger.entries.map(e => e.id)).toEqual(['ch7', 'ch8', 'outline'])
    expect(back.cachedPrefixCount).toBe(2)
    expect(back.resendChars).toBe(3000)                 // the outline alone
  })

  it('C: going back to finish an earlier chapter is the expensive case, once', () => {
    // ch7 is the most stable, so it sits FIRST — and jumping back to edit it
    // re-sends everything after. The system then self-heals: ch7 leaves, and
    // when it returns its timestamp is fresh, so it sorts last from then on.
    const ledger = planLedgerTurn(
      EMPTY_LEDGER,
      orderAdmissionsByStability(['ch7', 'ch8', 'outline'], times, book, 'ch9'),
      docs,
      'ch9'
    ).ledger
    expect(ledger.entries.map(e => e.id)).toEqual(['ch7', 'ch8', 'outline'])

    const jumped = planLedgerTurn(ledger, ['ch8', 'outline'], docs, 'ch7')
    expect(jumped.cachedPrefixCount).toBe(0)
    expect(jumped.resentIds).toEqual(['ch8', 'outline'])

    // …and afterwards ch7 is the most recently edited, so it is ordered last.
    const freshTimes = times.map(t => t.id === 'ch7' ? { ...t, updatedAt: '2026-08-17T11:00:00.000Z' } : t)
    expect(orderAdmissionsByStability(['ch7', 'ch8', 'outline'], freshTimes, book, 'ch8'))
      .toEqual(['ch8', 'outline', 'ch7'])
  })
})

describe('orderAdmissionsByStability', () => {
  // The workflows this ordering exists for:
  //   A. a permanently pinned outline + writing the next chapter from the
  //      preceding one — the outline is referenced every turn AND revised
  //      after most sessions;
  //   B. folding the new chapter back into the outline, which makes the
  //      outline the active document;
  //   C. more rarely, going back to finish an earlier chapter using later text.
  const book = ['outline', 'ch1', 'ch2', 'ch3', 'ch4']
  const docs = [
    { id: 'outline', updatedAt: '2026-08-17T10:00:00.000Z' },  // revised constantly
    { id: 'ch1', updatedAt: '2026-06-01T00:00:00.000Z' },      // finished long ago
    { id: 'ch2', updatedAt: '2026-06-20T00:00:00.000Z' },
    { id: 'ch3', updatedAt: '2026-08-16T00:00:00.000Z' },      // written yesterday
    { id: 'ch4', updatedAt: '2026-08-17T09:00:00.000Z' }
  ]

  it('puts the constantly-revised outline LAST, not first', () => {
    // Book distance would have sorted the outline first — the most expensive
    // slot — because chapter zero is far from wherever the writer is.
    expect(orderAdmissionsByStability(['outline', 'ch1', 'ch3'], docs, book, 'ch4'))
      .toEqual(['ch1', 'ch3', 'outline'])
  })

  it('orders finished chapters before recently written ones', () => {
    expect(orderAdmissionsByStability(['ch3', 'ch1', 'ch2'], docs, book, 'ch4'))
      .toEqual(['ch1', 'ch2', 'ch3'])
  })

  it('falls back to book distance when edit times tie', () => {
    const sameDay = [
      { id: 'ch1', updatedAt: '2026-06-01T00:00:00.000Z' },
      { id: 'ch3', updatedAt: '2026-06-01T00:00:00.000Z' }
    ]
    // ch3 is adjacent to the active ch4, so it is likelier to be edited next
    // and belongs later.
    expect(orderAdmissionsByStability(['ch3', 'ch1'], sameDay, book, 'ch4'))
      .toEqual(['ch1', 'ch3'])
  })

  it('treats a document with no timestamp as maximally stable', () => {
    const mixed = [{ id: 'a' }, { id: 'b', updatedAt: '2026-08-17T00:00:00.000Z' }]
    expect(orderAdmissionsByStability(['b', 'a'], mixed, ['a', 'b'], null)).toEqual(['a', 'b'])
  })
})

// ── Append-updates (planLedgerTurn with a renderer) ─────────────────────────
// An edited chapter keeps its old copy in place, byte for byte, and its new
// version is appended — instead of re-sending everything after the old copy.

describe('planLedgerTurn — append-updates', () => {
  /** Chapters with explicit bodies and sizes; the renderer records what it drew. */
  const book = (bodies: Record<string, string>, sizes: Record<string, number> = {}): LedgerDocLike[] =>
    Object.entries(bodies).map(([id, body]) => ({ id, chars: sizes[id] ?? 1000, hash: hashContent(body) }))
  const renderer = (bodies: Record<string, string>) =>
    (id: string, kind: 'fresh' | 'update') => `[${kind}:${id}:${bodies[id]}]`

  const start = (bodies: Record<string, string>, order: string[], sizes?: Record<string, number>) =>
    planLedgerTurn(EMPTY_LEDGER, order, book(bodies, sizes), null, { render: renderer(bodies) }).ledger

  it('keeps an edited chapter\'s old copy where it is and appends the new version', () => {
    const v1 = { a: 'a1', outline: 'o1', c: 'c1', d: 'd1' }
    const ledger = start(v1, ['a', 'outline', 'c', 'd'])
    const v2 = { ...v1, outline: 'o2' }
    const plan = planLedgerTurn(ledger, ['a', 'outline', 'c', 'd'], book(v2), null, { render: renderer(v2) })

    // Nothing before the end was touched: every original entry survives, same bytes.
    expect(plan.cachedPrefixCount).toBe(4)
    expect(plan.ledger.entries.slice(0, 4).map(e => e.text)).toEqual(ledger.entries.map(e => e.text))
    expect(plan.ledger.entries[1]).toMatchObject({ id: 'outline', stale: true, text: '[fresh:outline:o1]' })
    // The new version comes last, marked as one.
    expect(plan.ledger.entries.at(-1)).toMatchObject({ id: 'outline', text: '[update:outline:o2]' })
    expect(plan.updatedIds).toEqual(['outline'])
    expect(plan.resentIds).toEqual([])
    expect(plan.resendChars).toBe(1000)
    expect(plan.staleChars).toBe(1000)
  })

  it('still cuts when the chapter is near the end — re-sending less than it would duplicate', () => {
    const v1 = { a: 'a1', b: 'b1', outline: 'o1' }
    const ledger = start(v1, ['a', 'b', 'outline'])
    const v2 = { ...v1, outline: 'o2' }
    const plan = planLedgerTurn(ledger, ['a', 'b', 'outline'], book(v2), null, { render: renderer(v2) })

    expect(plan.updatedIds).toEqual([])
    expect(plan.ledger.entries.map(e => [e.id, e.text])).toEqual([
      ['a', '[fresh:a:a1]'], ['b', '[fresh:b:b1]'], ['outline', '[fresh:outline:o2]']
    ])
    expect(plan.staleChars).toBe(0)
  })

  it('consolidates once outdated copies pass the budget: one cut, re-sent clean', () => {
    const v1 = { a: 'a1', outline: 'o1', c: 'c1', d: 'd1' }
    const ledger = start(v1, ['a', 'outline', 'c', 'd'])
    const v2 = { ...v1, outline: 'o2' }
    const plan = planLedgerTurn(ledger, ['a', 'outline', 'c', 'd'], book(v2), null, { render: renderer(v2), maxStaleChars: 500 })

    expect(plan.cachedPrefixCount).toBe(1)
    expect(plan.ledger.entries.map(e => e.text)).toEqual(['[fresh:a:a1]', '[fresh:c:c1]', '[fresh:d:d1]', '[fresh:outline:o2]'])
    expect(plan.ledger.entries.some(e => e.stale)).toBe(false)
  })

  it('keeps the outdated copy turn after turn, and appends again on a second edit', () => {
    const v1 = { a: 'a1', outline: 'o1', c: 'c1', d: 'd1', e: 'e1' }
    const order = ['a', 'outline', 'c', 'd', 'e']
    const sizes = { outline: 300 }
    let ledger = start(v1, order, sizes)
    const v2 = { ...v1, outline: 'o2' }
    ledger = planLedgerTurn(ledger, order, book(v2, sizes), null, { render: renderer(v2) }).ledger

    // Unchanged next turn: identical bytes, nothing re-sent.
    const quiet = planLedgerTurn(ledger, order, book(v2, sizes), null, { render: renderer(v2) })
    expect(quiet.ledger.entries.map(e => e.text)).toEqual(ledger.entries.map(e => e.text))
    expect(quiet.resendChars).toBe(0)

    // Edited again: the update entry is itself last, so it is replaced in place.
    const v3 = { ...v1, outline: 'o3' }
    const again = planLedgerTurn(ledger, order, book(v3, sizes), null, { render: renderer(v3) })
    expect(again.ledger.entries.filter(e => e.id === 'outline').map(e => [e.text, !!e.stale])).toEqual([
      ['[fresh:outline:o1]', true],
      ['[update:outline:o3]', false]
    ])
  })

  it('drops every copy of a chapter that leaves', () => {
    const v1 = { a: 'a1', outline: 'o1', c: 'c1', d: 'd1' }
    let ledger = start(v1, ['a', 'outline', 'c', 'd'])
    const v2 = { ...v1, outline: 'o2' }
    ledger = planLedgerTurn(ledger, ['a', 'outline', 'c', 'd'], book(v2), null, { render: renderer(v2) }).ledger

    // The writer opens the outline: it moves to the volatile tail.
    const opened = planLedgerTurn(ledger, ['a', 'c', 'd'], book(v2), 'outline', { render: renderer(v2) })
    expect(opened.ledger.entries.map(e => e.id)).toEqual(['a', 'c', 'd'])
    expect(opened.drops).toEqual([{ id: 'outline', reason: 'now-active' }])
  })

  it('marks a chapter re-sent after a cut as the newer version when an old copy stays above', () => {
    const v1 = { a: 'a1', outline: 'o1', c: 'c1', d: 'd1' }
    let ledger = start(v1, ['a', 'outline', 'c', 'd'])
    const v2 = { ...v1, outline: 'o2' }
    ledger = planLedgerTurn(ledger, ['a', 'outline', 'c', 'd'], book(v2), null, { render: renderer(v2) }).ledger
    // Now 'c' leaves: the cut is at 'c', after the outline's old copy.
    const plan = planLedgerTurn(ledger, ['a', 'outline', 'd'], book(v2), null, { render: renderer(v2) })
    expect(plan.ledger.entries.map(e => [e.id, e.text])).toEqual([
      ['a', '[fresh:a:a1]'],
      ['outline', '[fresh:outline:o1]'],
      ['d', '[fresh:d:d1]'],
      ['outline', '[update:outline:o2]']
    ])
  })
})

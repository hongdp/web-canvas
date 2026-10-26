/**
 * The chat's rows: messages as stored, with a turn that was steered shown
 * split where the steer landed.
 *
 * A message sent while a turn runs (a steer, agentic_chat_loop.md §0.8) is
 * stored as a user message AFTER the turn's assistant message — it was added
 * while that message was streaming — and the turn's later output went on
 * into the same bubble, above it. The run marks where the steer reached it
 * (`{type: 'steer'}` in its timeline); here the turn becomes the bubble up to
 * that mark, the user's message(s), and the rest as a new bubble. Display
 * only: the stored messages, the run's record and its transcript stay one
 * turn, so history and the prompt cache are untouched.
 */
import type { AgentTimelineItem, ChatMessage } from '../types/chat'

/** A part of an assistant turn's timeline, between steers. */
export interface TimelineSegment {
  items: AgentTimelineItem[]
  /** The first part carries the turn's prefix; the last its live state, summary and controls. */
  first: boolean
  last: boolean
}

export interface ChatRow {
  key: string
  msg: ChatMessage
  /** For an assistant turn shown split: which part this row shows. Absent = the whole message. */
  segment?: TimelineSegment
  /** A user message shown inside the turn it steered (no edit: it belongs to that turn). */
  steered?: boolean
}

type SteerMark = Extract<AgentTimelineItem, { type: 'steer' }>

/** Split a timeline at its steer marks: parts, and the steer that follows each but the last. */
export function splitTimeline(timeline: AgentTimelineItem[]): { parts: AgentTimelineItem[][]; steers: SteerMark[] } {
  const parts: AgentTimelineItem[][] = [[]]
  const steers: SteerMark[] = []
  for (const item of timeline) {
    if (item.type === 'steer') {
      steers.push(item)
      parts.push([])
    } else {
      parts[parts.length - 1].push(item)
    }
  }
  return { parts, steers }
}

export function chatRows(messages: ChatMessage[]): ChatRow[] {
  const byId = new Map(messages.map(m => [m.id, m]))
  // User messages a turn shows inside itself, at the mark: not again at their stored place.
  const shownInTurn = new Set<string>()
  for (const m of messages) {
    for (const item of m.agent?.timeline ?? []) {
      if (item.type === 'steer') item.messageIds.forEach(id => { if (byId.has(id)) shownInTurn.add(id) })
    }
  }

  const rows: ChatRow[] = []
  for (const msg of messages) {
    if (shownInTurn.has(msg.id)) continue
    const timeline = msg.role === 'assistant' ? msg.agent?.timeline : undefined
    if (!timeline || !timeline.some(i => i.type === 'steer')) {
      rows.push({ key: msg.id, msg })
      continue
    }
    const { parts, steers } = splitTimeline(timeline)
    parts.forEach((items, i) => {
      rows.push({ key: `${msg.id}#${i}`, msg, segment: { items, first: i === 0, last: i === parts.length - 1 } })
      const steer = steers[i]
      if (!steer) return
      const users = steer.messageIds.map(id => byId.get(id)).filter((m): m is ChatMessage => !!m)
      if (users.length > 0) {
        for (const u of users) rows.push({ key: `${msg.id}#${i}:${u.id}`, msg: u, steered: true })
      } else {
        // An id the chat does not have (sent from another device before it synced): the text the run took.
        rows.push({ key: `${msg.id}#${i}:steer`, msg: { id: `${msg.id}#steer${i}`, role: 'user', content: steer.text, timestamp: msg.timestamp }, steered: true })
      }
    })
  }
  return rows
}

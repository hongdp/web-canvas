/** One chapter a turn wrote to — a row of the bubble's "changed this turn" block (spec D2). */
export interface AgentTouchedChapter {
  documentId: string
  /** The title when the turn ran; shown if the chapter is renamed or deleted later. */
  titleAtRun: string
  kind: 'edits' | 'rewrite' | 'selection' | 'created' | 'polished' | 'renamed'
  /** Changes that landed (edit blocks, or 1 for a rewrite/selection). */
  changes: number
  /** Changes that could not be located. */
  failed: number
}

/**
 * What an agentic turn did, kept on its assistant bubble (spec §5.6). Trace
 * lines and counts only — never chapter text. A chapter's review status is
 * NOT stored: it is read from the chapter's live content, so it cannot go
 * stale after an accept or reject.
 */
/** One entry of a turn as it happened: a step's chat text, or a call it made. */
export type AgentTimelineItem =
  | { type: 'text'; text: string }
  | { type: 'tool'; line: string; ok: boolean }
  /**
   * Where a message the user sent mid-turn reached the run (a steer). The chat
   * shows the turn split there: the bubble so far, the user's message(s), and
   * the rest of the turn as a new bubble. Display only — the run, its record
   * and its transcript stay one turn.
   */
  | { type: 'steer'; messageIds: string[]; text: string }

/**
 * A deletion waiting for the user's confirmation (delete_chapter on a chapter
 * with text). Shown as Delete / Keep buttons; only the Delete button deletes —
 * a typed answer never does — and only while the chapter still has the text
 * the question was asked about (`contentHash`, the stored HTML's hash).
 */
export interface ChapterDeletionConfirm {
  action: 'delete_chapter'
  documentId: string
  title: string
  /** Its position in the book when asked (#n). */
  number: number
  /** Characters of its text, for the question. */
  chars: number
  contentHash: string
}

/**
 * Why a server-side run is suspended (backend_authority.md "Runaway runs"):
 * it repeated itself, nobody was watching, or it passed its token budget.
 */
export interface AgentRunPause {
  reason: 'repeating' | 'unattended' | 'token_budget' | 'question' | 'step_failed' | 'repeating_output' | string
  message: string
  /** The last steps, for the user to judge: the calls and what came back. */
  steps?: Array<{ calls: string[]; results: string[]; reasoning: string }>
  /** `question`: what the model asked (ask_user) and the choices it offered. */
  question?: string
  options?: string[]
  /** `question` asked by delete_chapter: the deletion the Delete button confirms. */
  confirm?: ChapterDeletionConfirm
}

/** One item of the model's checklist for a turn (utils/plan). */
export interface AgentPlanItem {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'done' | 'dropped'
}

/** The server-side run behind a bubble, while it is queued, running or paused. */
export interface AgentRunLink {
  id: string
  status: 'queued' | 'running' | 'paused' | 'done' | 'stopped' | 'error'
  /** Queued: requests ahead of this one. */
  position?: number
  pause?: AgentRunPause
}

export interface AgentTurnRecord {
  status: 'running' | 'done' | 'stopped' | 'step_limit' | 'queued' | 'paused'
  steps: number
  /** One line per executed tool call, in order. */
  trace: string[]
  touched: AgentTouchedChapter[]
  /**
   * The turn in order — each step's text, then the calls that step made — so
   * the bubble shows tool use where it happened, not in a list at the end.
   * `content` still holds the joined text (history, older clients).
   */
  timeline?: AgentTimelineItem[]
  /** Bubble text before the timeline (the attached-context label). */
  prefix?: string
  /** Bubble text after it (completion warnings, the step-limit note). */
  suffix?: string
  /** While running: the step in flight (its text so far, a progress line). */
  live?: string
  /**
   * While running: the chapters whose HTML the model has seen, by content
   * hash — a page reload restores them for the rejoined step (agent/types,
   * restoreSeen). Dropped when the turn ends.
   */
  seen?: Array<{ id: string; hash: string }>
  /** The server-side run this turn is, with its queue or pause state. */
  run?: AgentRunLink
  /** The model's checklist for the turn (the `plan` tool), shown under the bubble. */
  plan?: AgentPlanItem[]
  /** A tab-run ended asking the user this (`ask_user`); the next message answers it. */
  question?: { question: string; options: string[]; confirm?: ChapterDeletionConfirm }
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  images?: string[] // base64 Data URLs ("data:image/...;base64,...")
  timestamp: string
  provider?: string
  model?: string
  /** Assistant only: what an agentic turn read and changed. */
  agent?: AgentTurnRecord
  /**
   * Assistant only, grok: the reasoning items (encrypted) of the turn's final
   * step, kept so later turns can send them back and the model keeps what it
   * worked out — measured: a hidden choice made in turn 1 is recalled in
   * turn 2 with these, and lost without (2026-10-08). Opaque; cleared with
   * the chat. Only the most recent turns' items are sent (useChatLLM).
   */
  reasoningItems?: unknown[]
}


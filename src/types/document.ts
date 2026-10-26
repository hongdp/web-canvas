export interface CanvasDocument {
  id: string
  title: string
  content: string
  contentLoaded?: boolean
  /**
   * The server revision this text is based on (backend_authority.md §2.1);
   * sent with a save so the server can refuse it if another write landed
   * first. Absent: never synced (local-only, or a server without revisions).
   */
  revision?: number
  /**
   * An edit of the text or title the server has not confirmed. Persisted
   * with the chapter, so a reload or a closed tab cannot drop it — on the
   * next load the local text is kept and saved, instead of being replaced
   * by the server's copy.
   */
  unsynced?: boolean
  createdAt: string
  updatedAt: string
  /**
   * LLM-generated short summary (~120 words + key entities) used for the
   * always-on chapter index and context auto-selection. Navigation metadata,
   * not a source of truth — may lag behind `content` (see summaryContentHash).
   */
  summary?: string
  /** Hash of `content` at the time `summary` was generated. Mismatch = stale. */
  summaryContentHash?: string
  /**
   * The writer pinned it: it rides along on every agent turn (docs/features/
   * pinned_context.md). Metadata, synced to the server without a revision bump.
   */
  pinned?: boolean
}

export interface DocumentVersion {
  id: string
  documentId: string
  /**
   * Which book the snapshot belongs to. Optional because snapshots taken
   * before version history became per-book have none — those are attributed to
   * whichever book is open when they are next counted.
   */
  bookId?: string
  timestamp: string
  title: string
  content: string
}

/** A book's reference file as the server lists it (attachments_and_web.md §1). */
export interface AttachmentSection {
  title: string
  from: number
  to: number
}

export interface AttachmentMeta {
  id: string
  /** "A1", "A2"… in the book's order. */
  ref: string
  name: string
  chars: number
  paragraphs: number
  sections: AttachmentSection[]
}

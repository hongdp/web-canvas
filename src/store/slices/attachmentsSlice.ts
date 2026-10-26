/**
 * The active book's reference files (docs/features/attachments_and_web.md §1).
 *
 * Server-only: an attachment is a file on the API server, never in
 * IndexedDB — a 5 MB text file per book would crowd out the documents. The list
 * is the metadata the sidebar shows and the agent's index is built from;
 * the text is fetched by services/attachments when a tool reads it.
 */
import type { StateCreator } from 'zustand'
import type { AppState } from '../types'
import type { AttachmentMeta } from '../../types/document'
import { deleteAttachment, listAttachments, uploadAttachment } from '../../services/attachments'

export interface AttachmentsSlice {
  attachments: AttachmentMeta[]
  /** The book `attachments` belongs to; a list for another book is never shown or offered. */
  attachmentsBookId: string | null
  attachmentsBusy: boolean
  attachmentsError: string | null
  loadAttachments: (bookId: string) => Promise<void>
  addAttachments: (files: File[]) => Promise<void>
  removeAttachment: (id: string) => Promise<void>
}

export const createAttachmentsSlice: StateCreator<AppState, [], [], AttachmentsSlice> = (set, get) => ({
  attachments: [],
  attachmentsBookId: null,
  attachmentsBusy: false,
  attachmentsError: null,

  loadAttachments: async (bookId) => {
    if (!get().user) { set({ attachments: [], attachmentsBookId: bookId, attachmentsError: null }); return }
    try {
      const list = await listAttachments(bookId)
      // The user may have switched books while this was in flight.
      if (get().activeBookId === bookId) set({ attachments: list, attachmentsBookId: bookId, attachmentsError: null })
    } catch (e) {
      if (get().activeBookId === bookId) set({ attachments: [], attachmentsBookId: bookId, attachmentsError: e instanceof Error ? e.message : String(e) })
    }
  },

  addAttachments: async (files) => {
    const bookId = get().activeBookId
    if (!bookId || files.length === 0) return
    set({ attachmentsBusy: true, attachmentsError: null })
    try {
      for (const file of files) await uploadAttachment(bookId, file)
    } catch (e) {
      set({ attachmentsError: e instanceof Error ? e.message : String(e) })
    } finally {
      set({ attachmentsBusy: false })
      // References (A1, A2…) follow the server's order: re-read the list.
      await get().loadAttachments(bookId)
    }
  },

  removeAttachment: async (id) => {
    const bookId = get().activeBookId
    if (!bookId) return
    set({ attachmentsBusy: true, attachmentsError: null })
    try {
      await deleteAttachment(bookId, id)
    } catch (e) {
      set({ attachmentsError: e instanceof Error ? e.message : String(e) })
    } finally {
      set({ attachmentsBusy: false })
      await get().loadAttachments(bookId)
    }
  }
})

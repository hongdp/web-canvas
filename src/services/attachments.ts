/**
 * The book's reference files (docs/features/attachments_and_web.md;
 * scripts/server_attachments.py): list, upload, remove. Reading them, and the
 * web, is the server run's (wc_agent); the tab never fetches their text.
 */
import { useAppStore } from '../store/useAppStore'
import { CLIENT_ID, CLIENT_ID_HEADER } from '../store/documentSync'
import type { AttachmentMeta } from '../types/document'

function headers(json = true): Record<string, string> {
  return {
    ...(json ? { 'Content-Type': 'application/json' } : {}),
    'X-CSRF-Token': useAppStore.getState().csrfToken || '',
    [CLIENT_ID_HEADER]: CLIENT_ID
  }
}

async function expectOk(res: Response, what: string): Promise<Record<string, unknown>> {
  const body = await res.json().catch(() => ({})) as Record<string, unknown>
  if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `${what} failed (${res.status})`)
  return body
}

const base = (bookId: string) => `/api/books/${encodeURIComponent(bookId)}/attachments`

export async function listAttachments(bookId: string, fetchFn: typeof fetch = fetch): Promise<AttachmentMeta[]> {
  const body = await expectOk(await fetchFn(base(bookId)), 'Listing attachments')
  return Array.isArray(body.attachments) ? body.attachments as AttachmentMeta[] : []
}

export async function uploadAttachment(bookId: string, file: File, fetchFn: typeof fetch = fetch): Promise<AttachmentMeta> {
  const res = await fetchFn(`${base(bookId)}?name=${encodeURIComponent(file.name)}`, {
    method: 'POST',
    headers: { ...headers(false), 'Content-Type': 'application/octet-stream' },
    body: file
  })
  const body = await expectOk(res, 'Uploading the attachment')
  return body.attachment as AttachmentMeta
}

export async function deleteAttachment(bookId: string, id: string, fetchFn: typeof fetch = fetch): Promise<void> {
  await expectOk(await fetchFn(`${base(bookId)}/${encodeURIComponent(id)}`, { method: 'DELETE', headers: headers() }), 'Deleting the attachment')
}


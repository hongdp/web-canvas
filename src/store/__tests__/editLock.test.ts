import { describe, it, expect, beforeEach } from 'vitest'
import { useAppStore, isEditLocked } from '../useAppStore'

describe('the edit lock while something streams (agentic_chat_loop.md §0.4)', () => {
  beforeEach(() => {
    useAppStore.setState({ isStreaming: false, editLockedIds: null })
  })

  it('locks nothing when nothing streams', () => {
    useAppStore.setState({ editLockedIds: ['doc-1'] })
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(false)
  })

  it('locks every chapter for a streamer that names none (whole-book batches)', () => {
    useAppStore.getState().setStreaming(true)
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(true)
    expect(isEditLocked(useAppStore.getState(), 'doc-9')).toBe(true)
  })

  it('locks only the named chapters for a chat run', () => {
    const s = useAppStore.getState()
    s.setStreaming(true)
    s.setEditLockedIds(['doc-2'])
    expect(isEditLocked(useAppStore.getState(), 'doc-2')).toBe(true)
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(false)
  })

  it('forgets the list when streaming stops, so the next streamer starts fully locked', () => {
    const s = useAppStore.getState()
    s.setStreaming(true)
    s.setEditLockedIds([])
    s.setStreaming(false)
    expect(useAppStore.getState().editLockedIds).toBeNull()
    useAppStore.getState().setStreaming(true)
    expect(isEditLocked(useAppStore.getState(), 'doc-1')).toBe(true)
  })
})

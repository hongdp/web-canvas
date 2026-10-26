import { useState, useRef, useCallback, useEffect } from 'react'
import { Editor } from '@tiptap/react'
import { useAppStore } from '../store/useAppStore'
import { findJobsForBubbles } from '../services/remoteGeneration'
import type { AppState } from '../store/types'
import { getTimestampId, isBlankContent } from '../utils/text'
import { stripDiffMarkup } from '../utils/diff'
import { hashContent } from '../utils/contextLedger'
import { defaultPolishModel } from '../utils/runDefaults'
import type { HistorySourceMessage } from './chat/types'
import { ASSISTANT_PLACEHOLDER, INTERRUPTED_NOTICE, isUnfinishedBubble, REASONING_TAIL_CHARS, REASONING_PAINT_MS, relocateResumedSelection } from './chat/streamHandlers'
import { replaceSelectionWithHtml } from './chat/selectionReplace'
import { startServerRun, listServerRuns, getServerRun, serverRunAction, answerServerRun, steerServerRun, reportRunView, type ServerRunEvent, type ServerRunSummary, type ServerRunAction } from '../services/serverRuns'
import { onRunCatchUp, onRunEvent } from '../store/runEvents'
import { resyncBook } from '../store/bookEvents'
import { CLIENT_ID, needsTextSync } from '../store/documentSync'
import { clearPendingSave } from '../store/syncRuntime'
import { mergeVersions } from '../store/versionMerge'
import { applyRunEvent, ensureRunMessages, bubbleStillWaiting, type RunLive, runUsageDelta, type RunUsage } from './chat/serverRunEvents'
import type { ChapterDeletionConfirm } from '../types/chat'
import { currentTranslation } from '../i18n'

/**
 * The chat, as a renderer of the server's runs (backend_authority.md phase 4).
 *
 * Every turn runs in the API process (`scripts/server_runs.py`, the loop in
 * `scripts/wc_agent/`): this hook posts the turn, then renders the run's
 * `run.*` events from the book stream — the bubble through the reducers in
 * chat/serverRunEvents, the editor through the preview below. A reload or
 * another device lists the book's runs and rejoins them.
 *
 * Problem: the loop used to run in this tab, with the server loop behind a
 *   switch. A closed page stopped a turn after its first step (2026-10-16:
 *   a request to finish a chapter sent its first step, a backend job, and
 *   never the second), and
 *   every change to the loop had to be made twice.
 * Fix: the tab only renders. Nothing here decides what the model does next.
 */

/**
 * Resolves once the store satisfies `predicate`, or false once `timeoutMs`
 * passes. On a cold reload the chat history is only restored when the
 * background server sync completes.
 */
function waitForStore(predicate: (state: AppState) => boolean, timeoutMs = 15_000): Promise<boolean> {
  if (predicate(useAppStore.getState())) return Promise.resolve(true)
  return new Promise<boolean>(resolve => {
    let unsubscribe: (() => void) | null = null
    const timer = setTimeout(() => {
      unsubscribe?.()
      resolve(false)
    }, timeoutMs)
    unsubscribe = useAppStore.subscribe(state => {
      if (!predicate(state)) return
      clearTimeout(timer)
      unsubscribe?.()
      resolve(true)
    })
  })
}

/** The stored text of the chapter on screen. */
const storedOpen = (): string => {
  const st = useAppStore.getState()
  return st.documents.find(d => d.id === st.activeDocumentId)?.content ?? ''
}

/** A bubble whose turn runs on the server: the run list settles it, not the job list. */
const isServerRunBubble = (m: { agent?: { run?: { status: string } } }) =>
  !!m.agent?.run && (m.agent.run.status === 'queued' || m.agent.run.status === 'running' || m.agent.run.status === 'paused')

/**
 * Retire bubbles a tab-run left waiting — the turn ran in a tab that is gone.
 *
 * Before phase 4 a turn could run in the tab; one whose tab closed mid-turn
 * left its bubble at "Thinking..." for good. Such a bubble is retired only on
 * the server's word (it has no job for it): a lookup that fails touches nothing,
 * and the next load asks again.
 */
async function retireOrphanedBubbles(): Promise<void> {
  await waitForStore(state => state.messages.length > 0, 15_000)
  const s = useAppStore.getState()
  const unfinished = s.messages.filter(m => m.role === 'assistant' && isUnfinishedBubble(m.content) && m.content !== INTERRUPTED_NOTICE && !isServerRunBubble(m))
  if (unfinished.length === 0) return
  const lookup = await findJobsForBubbles(unfinished.map(m => m.id), s.activeBookId)
  if (!lookup.known) return
  const orphaned = new Set(unfinished.filter(m => !lookup.jobs.has(m.id)).map(m => m.id))
  if (orphaned.size === 0) return
  const latest = useAppStore.getState()
  latest.setMessages(latest.messages.map(m => orphaned.has(m.id) ? { ...m, content: INTERRUPTED_NOTICE } : m))
}

interface UseChatLLMProps {
  activeEditor: Editor | null
  selectedText: string
  uploadedImages: string[]
  setUploadedImages: (images: string[]) => void
  layoutMode: string
  setIsChatExpanded: (expanded: boolean) => void
  forceSave: () => void
  setSaveStatus: (status: 'saved' | 'unsaved') => void
}

/** The run this tab renders the editor side of. */
interface RenderedRun {
  id: string
  /** The chapter the turn was sent from: a selection rewrite lives there. */
  startId: string
  /** That chapter's text when the run attached — what Stop rebuilds its undo step from. */
  original: string
  /** Only the sending tab follows the run's `open` events and reports its view; other tabs watch. */
  sendingTab: boolean
  /** The turn rewrote a selection: once it ends, that selection is spent. */
  hadSelection: boolean
}

export function useChatLLM({
  activeEditor,
  selectedText,
  uploadedImages,
  setUploadedImages,
  layoutMode,
  setIsChatExpanded,
  forceSave,
  setSaveStatus
}: UseChatLLMProps) {
  const [chatInput, setChatInput] = useState('')
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null)
  const [editingMessageText, setEditingMessageText] = useState('')

  const chatInputRef = useRef<HTMLDivElement>(null)
  const chatEndRef = useRef<HTMLDivElement>(null)

  /**
   * The editor as it is NOW, not as it was when a callback was built: a run
   * rejoined on mount renders before the editor exists.
   */
  const activeEditorRef = useRef<Editor | null>(activeEditor)
  useEffect(() => {
    activeEditorRef.current = activeEditor
  }, [activeEditor])

  // A selection rewrite previews by relocating the selected TEXT in the
  // editor (the range never leaves the tab that made it, and a rejoining tab
  // never had one): the text waits here until the editor exists.
  const selectionRangeRef = useRef<{ from: number; to: number } | null>(null)
  const selectionEndRef = useRef<number | null>(null)
  const pendingSelectionTextRef = useRef<string | null>(null)
  const selectionRefs = useRef({
    pendingSelectionText: pendingSelectionTextRef,
    selectionRange: selectionRangeRef,
    selectionEnd: selectionEndRef
  }).current
  /** True once a selection preview has written into the editor this run. */
  const selectionPreviewedRef = useRef(false)

  // Reasoning display: a tail (thinking can run to thousands of chars),
  // painted at most a few times a second (it arrives token by token).
  const reasoningTailRef = useRef('')
  const lastReasoningPaintRef = useRef(0)

  // True once a live preview has written to the editor: every terminal path
  // MUST then converge the editor explicitly. The store may still hold the
  // pre-stream HTML, in which case Editor.tsx's content-prop effect sees no
  // change and would leave the half-streamed draft on screen.
  const canvasPreviewActiveRef = useRef(false)
  // The editor's HTML when a live preview first painted it: the base Stop
  // rebuilds its single undo step from. Not the turn's original — a run may
  // be previewing a chapter it created rather than the one it started on.
  const previewBaseRef = useRef<string | null>(null)
  // Which chapter the live preview is painting. The user may switch chapters
  // mid-run (agentic_chat_loop.md D2): from then on the editor shows another
  // chapter, and settling or keeping "the preview" must not touch it.
  const previewDocIdRef = useRef<string | null>(null)
  // What the run locks besides the chapter its preview paints: the start
  // chapter of a selection turn.
  const startLockRef = useRef<string | null>(null)
  // Chapters the server run locks (its `lock` events), merged into the edit lock.
  const serverLockRef = useRef<string[]>([])

  const serverRunRef = useRef<RenderedRun | null>(null)
  // The step in flight of each server run this tab watches (chat/serverRunEvents).
  const serverLiveRef = useRef(new Map<string, RunLive>())
  /** Each server run's usage this tab has already counted into the session totals. */
  const serverUsageRef = useRef(new Map<string, RunUsage>())
  // The run's `step` means its writes are in the store; the editor converges
  // once the document event's refetch has landed, so the settle is deferred.
  const serverSettleTimerRef = useRef<number | null>(null)
  const activeBookId = useAppStore(state => state.activeBookId)
  const user = useAppStore(state => state.user)
  const activeDocumentId = useAppStore(state => state.activeDocumentId)

  /** Count what a server run spent since its last report (the footer's Session Tokens and Last turn). */
  const countServerUsage = useCallback((runId: string, usage: ServerRunEvent['usage']) => {
    const counted = runUsageDelta(serverUsageRef.current.get(runId), usage)
    if (!counted) return
    serverUsageRef.current.set(runId, counted.total)
    const s = useAppStore.getState()
    s.addSessionTokens(counted.delta.promptTokens, counted.delta.completionTokens, counted.delta.cachedPromptTokens)
    s.setLastTurnCache({ provider: s.activeProvider, promptTokens: counted.delta.promptTokens, cachedTokens: counted.delta.cachedPromptTokens, firstTokenMs: null })
  }, [])

  /**
   * Is the chapter the preview painted still the one on screen?
   *
   * Problem: switching chapters mid-run left the preview flags set while the
   *   editor already showed the other chapter (Editor.tsx's content sync
   *   replaced the draft). Settling then wrote the FIRST chapter's HTML into
   *   the editor showing the second — and the next keystroke there would have
   *   saved it into the wrong chapter. Stop did the same into the undo stack.
   * Fix: once the open chapter is not the previewed one, nothing of the
   *   preview is on screen; settling and keeping only clear the flags.
   */
  const previewOnScreen = useCallback(() =>
    previewDocIdRef.current === null || previewDocIdRef.current === useAppStore.getState().activeDocumentId, [])

  /**
   * Tell the editor which chapters the user may not edit right now: the one
   * the live preview paints (typing there would be painted over), a selection
   * turn's chapter and what the run locks. Only while streaming — the store
   * clears the lock when streaming stops, and a late publish must not unlock
   * the next streamer.
   */
  const publishEditLock = useCallback(() => {
    const s = useAppStore.getState()
    if (!s.isStreaming) return
    const preview = canvasPreviewActiveRef.current ? previewDocIdRef.current : null
    s.setEditLockedIds([...new Set([startLockRef.current, preview, ...serverLockRef.current].filter((id): id is string => !!id))])
  }, [])

  /** Force the editor back to `html` after a live preview, without polluting undo. */
  const settleCanvasPreview = useCallback((html: string) => {
    if (!canvasPreviewActiveRef.current) return
    canvasPreviewActiveRef.current = false
    previewBaseRef.current = null
    const onScreen = previewOnScreen()
    previewDocIdRef.current = null
    publishEditLock()
    if (!onScreen) return
    const editor = activeEditorRef.current
    if (editor && editor.getHTML() !== html) {
      editor.chain().setMeta('addToHistory', false).setContent(html, { emitUpdate: false }).run()
    }
  }, [previewOnScreen, publishEditLock])

  /**
   * Keep the half-streamed draft after a stop, as ONE undo step, and return
   * it so the caller can write the same HTML to the store.
   *
   * Every preview write carried addToHistory:false, so the editor's history
   * knows nothing about the draft: left as is, Undo would step back through
   * the user's edits from BEFORE the turn while the draft stayed on screen.
   * Rebuild the transition instead — put the pre-stream document back outside
   * history, then apply the draft as a single recorded transaction. Undo now
   * returns exactly the pre-stream document (which the version snapshot taken
   * before the send also holds).
   *
   * Returns null when no preview reached the editor: nothing to keep.
   */
  const keepCanvasPreview = useCallback((originalHtml: string): string | null => {
    if (!canvasPreviewActiveRef.current) return null
    canvasPreviewActiveRef.current = false
    previewBaseRef.current = null
    const onScreen = previewOnScreen()
    previewDocIdRef.current = null
    publishEditLock()
    // The user moved to another chapter: the draft is no longer on screen,
    // so there is nothing to keep — and rebuilding an undo step here would
    // put the previewed chapter's text into the open one's history.
    if (!onScreen) return null
    const editor = activeEditorRef.current
    if (!editor) return null
    const draft = editor.getHTML()
    if (draft === originalHtml) return null
    editor.chain().setMeta('addToHistory', false).setContent(originalHtml, { emitUpdate: false }).run()
    // No emitUpdate: the caller writes the store explicitly, and Editor.tsx's
    // content sync then sees matching HTML on both sides.
    editor.commands.setContent(draft, { emitUpdate: false })
    return draft
  }, [previewOnScreen, publishEditLock])

  /**
   * Paint the run's live chapter text into the open editor. The store is
   * deliberately NOT written: it would churn persistence every tick and fight
   * Editor.tsx's content-prop sync, and the run commits the text itself.
   */
  const previewDocument = useCallback((html: string) => {
    const editor = activeEditorRef.current
    if (!editor) return
    /*
     * Problem: a run writes chapter after chapter, but the preview's flags were
     *   set once, by the first chapter. Painting the second left them naming
     *   the first, and Stop thought nothing was on screen and kept none of the
     *   half-written chapter the user was looking at.
     * Fix: the preview is only ever asked to paint the open chapter, so a
     *   different open chapter means the preview moved — restart it there,
     *   based on that chapter's stored text.
     */
    const openId = useAppStore.getState().activeDocumentId
    const moved = canvasPreviewActiveRef.current && previewDocIdRef.current !== openId
    if (!canvasPreviewActiveRef.current || moved) {
      // The stored text, not editor.getHTML(): right after the run opens a
      // chapter the editor may still be the previous chapter's.
      previewBaseRef.current = storedOpen()
      previewDocIdRef.current = openId
      canvasPreviewActiveRef.current = true
      // Painted over from here on: the user must not type into it.
      publishEditLock()
    }
    setSaveStatus('unsaved')
    editor.chain()
      .setMeta('addToHistory', false)
      .setContent(html, { emitUpdate: false })
      .run()
  }, [publishEditLock, setSaveStatus])

  /**
   * Paint a streamed selection rewrite. Silent (SILENT_PREVIEW_META): the
   * server commits the rewrite itself, so the preview stays out of the store,
   * or this tab's next save conflicts with that commit.
   */
  const previewSelection = useCallback((html: string) => {
    const editor = activeEditorRef.current
    const run = serverRunRef.current
    if (!editor || !run) return
    // The selection lives in the chapter the turn started on. A selection
    // preview writes real transactions, so once the user has moved to
    // another chapter it would rewrite that chapter at the same offsets.
    if (useAppStore.getState().activeDocumentId !== run.startId) return
    relocateResumedSelection(editor, selectionRefs)
    if (!selectionRangeRef.current) return
    const { from } = selectionRangeRef.current
    const currentEnd = selectionEndRef.current ?? selectionRangeRef.current.to
    // Null when the document has moved on since the selection was taken.
    const end = replaceSelectionWithHtml(editor, from, currentEnd, html, { silent: true })
    if (end === null) return
    selectionEndRef.current = end
    selectionPreviewedRef.current = true
    setSaveStatus('unsaved')
  }, [selectionRefs, setSaveStatus])

  /** This tab renders `run` from now on: the editor side of its events, the streaming flag, the lock. */
  const attachServerRun = useCallback((run: ServerRunSummary) => {
    const s = useAppStore.getState()
    const startId = run.activeDocumentId || s.activeDocumentId
    pendingSelectionTextRef.current = run.selectedText || null
    selectionRangeRef.current = null
    selectionEndRef.current = null
    selectionPreviewedRef.current = false
    serverRunRef.current = {
      id: run.id,
      startId,
      original: s.documents.find(d => d.id === startId)?.content ?? '',
      sendingTab: run.clientId === CLIENT_ID,
      hadSelection: !!run.selectedText
    }
    if (run.status === 'running') {
      s.setStreaming(true)
      startLockRef.current = run.selectedText ? startId : null
      publishEditLock()
    }
  }, [publishEditLock])

  /** Post the turn to the server. The bubble then follows the run's events. */
  const startServerTurn = useCallback(async (opts: {
    promptText: string
    images?: string[]
    historySource: HistorySourceMessage[]
    userMsgId: string
    assistantMsgId: string
    polish?: { documentId: string; language: 'zh' | 'en' }
  }) => {
    const s = useAppStore.getState()
    const cfg = s.providerConfigs[s.activeProvider]
    const preset = s.customSystemPrompts.find(p => p.id === s.activeSystemPromptId)
    try {
      if (!s.user || !s.activeBookId) throw new Error(currentTranslation().agent.signInToChat)
      // The server builds the request from the stored book, and the save
      // behind an edit is debounced: an edit typed in the seconds before
      // the send would be missing from the prompt, and the run's write on
      // the stale chapter would then race this tab's save for the revision.
      if (s.documents.some(d => d.contentLoaded !== false && needsTextSync(d))) {
        clearPendingSave()
        await s.syncToServer()
      }
      const { run, position } = await startServerRun(s.activeBookId, {
        prompt: opts.promptText,
        images: opts.images,
        provider: s.activeProvider,
        config: {
          apiKey: cfg.apiKey, model: cfg.model, baseUrl: cfg.baseUrl, maxOutputTokens: cfg.maxOutputTokens,
          geminiSafetySettings: cfg.geminiSafetySettings, reasoningEffort: cfg.reasoningEffort, documentProtocol: cfg.documentProtocol,
          agentTools: cfg.agentTools, agentMaxSteps: cfg.agentMaxSteps, continueAfterWrites: cfg.continueAfterWrites,
          polishModel: cfg.polishModel, runTokenBudget: cfg.runTokenBudget, longReasoningReminderTokens: cfg.longReasoningReminderTokens,
          webAccess: cfg.webAccess
        },
        activeDocumentId: opts.polish?.documentId ?? s.activeDocumentId,
        selectedText: opts.polish ? undefined : selectedText || undefined,
        history: opts.historySource.filter(m => m.id !== 'welcome').map(m => ({
          id: m.id, role: m.role, content: m.content, images: m.images, agent: m.agent, reasoningItems: m.reasoningItems
        })),
        userMessageId: opts.userMsgId,
        assistantMessageId: opts.assistantMsgId,
        customInstructions: preset?.content,
        polishPrompt: s.polishPrompt,
        contextWindowTokens: s.discoveredContextWindows[cfg.model],
        clientId: CLIENT_ID,
        ...(opts.polish ? { polish: opts.polish } : {})
      })
      const latest = useAppStore.getState()
      latest.setMessages(applyRunEvent(latest.messages, { type: 'run', kind: run.status === 'queued' ? 'queued' : 'started', runId: run.id, run, position }, serverLiveRef.current))
      // The run's first events may have arrived before this response.
      if (run.status === 'running' && serverRunRef.current?.id !== run.id) attachServerRun(run)
      forceSave()
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setErrorMsg(message)
      const latest = useAppStore.getState()
      if (!serverRunRef.current) latest.setStreaming(false)
      latest.setMessages(latest.messages.map(m => m.id === opts.assistantMsgId ? { ...m, content: `⚠️ ${message}` } : m))
    }
  }, [selectedText, attachServerRun, forceSave])

  /** Settle the preview once the run's write has reached the store (the document event's refetch). */
  const settleServerPreviewSoon = useCallback(() => {
    if (serverSettleTimerRef.current !== null) window.clearTimeout(serverSettleTimerRef.current)
    serverSettleTimerRef.current = window.setTimeout(() => {
      serverSettleTimerRef.current = null
      settleCanvasPreview(storedOpen())
    }, 400)
  }, [settleCanvasPreview])

  /** One `run.*` event, rendered: the bubble through the reducer, the editor through the preview. */
  const handleRunEvent = useCallback(async (event: ServerRunEvent) => {
    const s = useAppStore.getState()
    const current = serverRunRef.current
    const mine = current?.id === event.runId
    switch (event.kind) {
      case 'queued':
      case 'started': {
        if (!event.run) return
        s.setMessages(applyRunEvent(s.messages, event, serverLiveRef.current))
        if (event.kind === 'started') {
          if (event.run.status === 'running' && !mine) attachServerRun(event.run)
          reasoningTailRef.current = ''
          s.setStreamingReasoning('')
        }
        return
      }
      case 'step_started': {
        s.setMessages(applyRunEvent(s.messages, event, serverLiveRef.current))
        reasoningTailRef.current = ''
        s.setStreamingReasoning('')
        return
      }
      case 'reasoning': {
        if (!mine) return
        reasoningTailRef.current = (reasoningTailRef.current + (event.text ?? '')).slice(-REASONING_TAIL_CHARS)
        const now = Date.now()
        if (now - lastReasoningPaintRef.current < REASONING_PAINT_MS) return
        lastReasoningPaintRef.current = now
        s.setStreamingReasoning(reasoningTailRef.current)
        return
      }
      case 'delta':
      case 'progress':
      case 'corrective': {
        if (event.kind === 'delta' && reasoningTailRef.current) {
          reasoningTailRef.current = ''
          s.setStreamingReasoning('')
        }
        if (event.kind === 'corrective' && mine) settleCanvasPreview(storedOpen())
        s.setMessages(applyRunEvent(s.messages, event, serverLiveRef.current))
        return
      }
      case 'preview': {
        if (!mine || !current) return
        if (event.html === null || event.html === undefined) {
          if (event.settled) settleServerPreviewSoon()
          else settleCanvasPreview(storedOpen())
          return
        }
        if (event.documentId !== s.activeDocumentId) return
        // The user typed here meanwhile: never paint over their text.
        if (s.documents.find(d => d.id === event.documentId)?.unsynced) return
        previewDocument(event.html)
        return
      }
      case 'preview_selection': {
        if (!mine || !current || !event.html) return
        previewSelection(event.html)
        return
      }
      case 'lock': {
        if (!mine) return
        serverLockRef.current = event.documentIds ?? []
        publishEditLock()
        return
      }
      case 'open': {
        if (!mine || !current?.sendingTab || !event.documentId) return
        if (!s.documents.some(d => d.id === event.documentId) && s.activeBookId) await resyncBook({ bookId: s.activeBookId })
        const latest = useAppStore.getState()
        if (latest.documents.some(d => d.id === event.documentId)) latest.setActiveDocumentId(event.documentId)
        return
      }
      case 'step': {
        countServerUsage(event.runId, event.usage)
        if (mine) settleServerPreviewSoon()
        s.setMessages(applyRunEvent(s.messages, event, serverLiveRef.current))
        forceSave()
        return
      }
      case 'paused':
      case 'finished': {
        if (event.kind === 'finished') {
          countServerUsage(event.runId, event.result?.usage)
          serverUsageRef.current.delete(event.runId)
        }
        if (mine) {
          settleCanvasPreview(storedOpen())
          serverLockRef.current = []
          startLockRef.current = null
          // Problem: the selection a finished turn rewrote stayed in the store, and the next message sent it
          //   again — text the chapter no longer had, so that turn's selection rewrite went nowhere (2026-10-25).
          // Fix: a turn that rewrote a selection spends it; the next one starts with none unless the user selects again.
          if (event.kind === 'finished' && current?.hadSelection && current.sendingTab) s.setSelectedText('')
          serverRunRef.current = null
          reasoningTailRef.current = ''
          s.setStreamingReasoning('')
        }
        s.setStreaming(false)
        const latest = useAppStore.getState()
        latest.setMessages(applyRunEvent(latest.messages, event, serverLiveRef.current))
        // Versions the run took before changing a chapter (metadata; the text loads on demand).
        const snapshots = event.result?.snapshots
        if (event.kind === 'finished' && snapshots?.length && latest.activeBookId) {
          useAppStore.setState({ versions: mergeVersions(useAppStore.getState().versions, snapshots, latest.activeBookId) })
        }
        forceSave()
        return
      }
      default:
        return
    }
  }, [attachServerRun, settleCanvasPreview, settleServerPreviewSoon, previewDocument, previewSelection, publishEditLock, forceSave, countServerUsage])

  useEffect(() => onRunEvent(event => { void handleRunEvent(event) }), [handleRunEvent])

  /**
   * The book's runs, on load and on every book switch: a queued, running or
   * paused run gets its bubble (created when another device sent it) and its
   * state; a run that finished while this tab was away settles its bubble.
   */
  const reconcileServerRuns = useCallback(async () => {
    const s0 = useAppStore.getState()
    if (!s0.user || !s0.activeBookId) return
    const bookId = s0.activeBookId
    let listed: Awaited<ReturnType<typeof listServerRuns>>
    try {
      listed = await listServerRuns(bookId)
    } catch {
      return
    }
    // The run this tab follows may have left the list (finished runs are kept in memory for a while,
    // and not across an API restart): ask for it alone, or this tab keeps its chapters locked and its
    // chat "working" for good (2026-10-18: Accept all disabled on the chapter a finished run had written).
    const followed = serverRunRef.current
    if (followed && !listed.runs.some(r => r.id === followed.id)) {
      try {
        const run = await getServerRun(bookId, followed.id)
        listed = { ...listed, runs: [...listed.runs, run] }
      } catch {
        // Gone from the server too: nothing will finish it, so settle the tab here.
        if (serverRunRef.current?.id === followed.id) {
          settleCanvasPreview(storedOpen())
          serverLockRef.current = []
          startLockRef.current = null
          serverRunRef.current = null
          useAppStore.getState().setStreaming(false)
        }
      }
    }
    if (listed.runs.length === 0) return
    await waitForStore(state => state.messages.length > 0, 15_000)
    if (useAppStore.getState().activeBookId !== bookId) return
    for (const run of listed.runs) {
      const s = useAppStore.getState()
      // The run this tab follows ended or paused while its events were missed:
      // settle it the way the live event would, so the bubble, the streaming
      // flag, the reasoning timer and the edit locks all end together.
      if (serverRunRef.current?.id === run.id && (run.status === 'paused' || run.status === 'done' || run.status === 'stopped' || run.status === 'error')) {
        await handleRunEvent(run.status === 'paused'
          ? { type: 'run', kind: 'paused', runId: run.id, run, record: run.record, pause: run.pause ?? undefined }
          : { type: 'run', kind: 'finished', runId: run.id, run, status: run.status, result: run.result ?? undefined })
        continue
      }
      if (run.status === 'queued' || run.status === 'running' || run.status === 'paused') {
        const kind = run.status === 'queued' ? 'queued' : run.status === 'paused' ? 'paused' : 'started'
        let messages = applyRunEvent(ensureRunMessages(s.messages, run), { type: 'run', kind, runId: run.id, run, record: run.record, pause: run.pause ?? undefined }, serverLiveRef.current)
        if (run.status === 'running' && run.liveText) {
          serverLiveRef.current.set(run.id, { text: run.liveText, progress: null })
          messages = applyRunEvent(messages, { type: 'run', kind: 'delta', runId: run.id, text: '' }, serverLiveRef.current)
        }
        s.setMessages(messages)
        if (run.status === 'running' && serverRunRef.current?.id !== run.id) {
          attachServerRun(run)
          if (run.liveReasoning) s.setStreamingReasoning(run.liveReasoning.slice(-REASONING_TAIL_CHARS))
        }
      } else if (run.result && run.assistantMessageId) {
        const bubble = s.messages.find(m => m.id === run.assistantMessageId)
        if (bubble && bubbleStillWaiting(bubble)) {
          s.setMessages(applyRunEvent(s.messages, { type: 'run', kind: 'finished', runId: run.id, run, status: run.status, result: run.result }, serverLiveRef.current))
        }
      }
    }
  }, [attachServerRun, handleRunEvent, settleCanvasPreview])
  useEffect(() => { void reconcileServerRuns() }, [reconcileServerRuns, activeBookId, user])
  // Events may have been missed (the stream dropped, or the page was in the background): re-read the runs.
  useEffect(() => onRunCatchUp(() => { void reconcileServerRuns() }), [reconcileServerRuns])
  // A bubble a closed tab-run left waiting (before phase 4) is retired once the server confirms no job fills it.
  useEffect(() => { if (user && activeBookId) void retireOrphanedBubbles() }, [user, activeBookId])

  // The sending tab tells the run where the user is (agentic_chat_loop.md
  // §0.4: once they move, the run stops changing the view).
  useEffect(() => {
    const current = serverRunRef.current
    const s = useAppStore.getState()
    if (!current?.sendingTab || !s.activeBookId || !s.isStreaming) return
    void reportRunView(s.activeBookId, current.id, activeDocumentId)
  }, [activeDocumentId])

  /** The user's answer to a server run paused on `ask_user` (components/RunControls). */
  const handleRunAnswer = useCallback(async (runId: string, answer: string, confirm = false) => {
    const s = useAppStore.getState()
    if (!s.activeBookId) return
    try {
      await answerServerRun(s.activeBookId, runId, answer, confirm)
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e))
    }
  }, [])

  /**
   * The Delete button of a question delete_chapter asked in a tab-run, from
   * before every turn ran on the server (the answer is sent as the next
   * message after this). Deletes the chapter only while it still has the text
   * the question was about; true when it is gone and the answer may be sent.
   */
  const confirmChapterDeletion = useCallback(async (confirm: ChapterDeletionConfirm): Promise<boolean> => {
    let s = useAppStore.getState()
    if (!s.documents.some(d => d.id === confirm.documentId)) return true
    await s.ensureDocumentContents([confirm.documentId])
    s = useAppStore.getState()
    const doc = s.documents.find(d => d.id === confirm.documentId)
    if (!doc) return true
    if (doc.contentLoaded === false || hashContent(doc.content) !== confirm.contentHash || s.documents.length <= 1) {
      setErrorMsg(currentTranslation().agent.deleteChanged)
      return false
    }
    s.deleteDocument(confirm.documentId)
    return true
  }, [])

  /** Queue and pause controls under a bubble (components/RunControls). */
  const handleRunAction = useCallback(async (runId: string, action: ServerRunAction) => {
    const s = useAppStore.getState()
    if (!s.activeBookId) return
    try {
      await serverRunAction(s.activeBookId, runId, action)
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e))
    }
  }, [])

  /**
   * Hand a message to the run in flight (agentic_chat_loop.md §0.8). True when
   * the run took it (the message is then in the chat as the user's); false
   * when nothing runs here or the server refused (the caller queues it as a turn).
   */
  const steerRunningTurn = useCallback(async (promptText: string): Promise<boolean> => {
    const s = useAppStore.getState()
    const server = serverRunRef.current
    if (!server || !s.activeBookId) return false
    const userMsg = {
      id: getTimestampId('user'), role: 'user' as const, content: promptText, timestamp: new Date().toISOString(),
      provider: s.activeProvider, model: s.providerConfigs[s.activeProvider].model
    }
    try {
      await steerServerRun(s.activeBookId, server.id, promptText, userMsg.id)
    } catch (e) {
      // Not running any more (finished, paused, stopped): the caller sends it as a turn.
      if (!(e instanceof Error && e.message.includes('(409)'))) setErrorMsg(e instanceof Error ? e.message : String(e))
      return false
    }
    useAppStore.getState().addMessage(userMsg)
    forceSave()
    return true
  }, [forceSave])

  const handleSendMessage = useCallback(async (e?: React.FormEvent, customPrompt?: string) => {
    e?.preventDefault()

    const s = useAppStore.getState()
    const promptText = customPrompt ? customPrompt.trim() : chatInput.trim()
    if (!promptText) return

    const clearInput = () => {
      if (customPrompt) return
      setChatInput('')
      if (chatInputRef.current) chatInputRef.current.innerHTML = ''
    }

    // A message while a turn runs steers it: the run takes it as its next
    // user message, and the chat shows it with no bubble of its own. Text
    // only — a message with images is sent as a turn, which queues.
    if (s.isStreaming && uploadedImages.length === 0 && await steerRunningTurn(promptText)) {
      clearInput()
      return
    }

    if (layoutMode === 'portrait') setIsChatExpanded(true)
    setErrorMsg(null)
    s.createVersionSnapshot(`Auto-save before: "${promptText.substring(0, 30)}${promptText.length > 30 ? '...' : ''}"`)
    clearInput()

    const images = uploadedImages.length > 0 ? uploadedImages : undefined
    const userMsg = {
      id: getTimestampId('user'),
      role: 'user' as const,
      content: promptText,
      images,
      timestamp: new Date().toISOString(),
      provider: s.activeProvider,
      model: s.providerConfigs[s.activeProvider].model
    }
    s.addMessage(userMsg)
    setUploadedImages([])

    const assistantMsgId = getTimestampId('assistant')
    s.addMessage({
      id: assistantMsgId,
      role: 'assistant' as const,
      content: ASSISTANT_PLACEHOLDER,
      timestamp: new Date().toISOString(),
      provider: s.activeProvider,
      model: s.providerConfigs[s.activeProvider].model
    })
    // History = the conversation BEFORE this turn (s was captured pre-add).
    await startServerTurn({ promptText, images, historySource: s.messages, userMsgId: userMsg.id, assistantMsgId })
  }, [chatInput, uploadedImages, layoutMode, setIsChatExpanded, setUploadedImages, startServerTurn, steerRunningTurn])

  const handleResubmitMessage = useCallback(async (msgId: string, newContent: string) => {
    const s = useAppStore.getState()
    const trimmed = newContent.trim()
    if (!trimmed || s.isStreaming) return

    if (layoutMode === 'portrait') setIsChatExpanded(true)
    setEditingMessageId(null)
    setErrorMsg(null)

    const targetIdx = s.messages.findIndex(m => m.id === msgId)
    if (targetIdx === -1) return

    const truncatedMessages = s.messages.slice(0, targetIdx + 1).map((m, idx) =>
      idx === targetIdx ? { ...m, content: trimmed, timestamp: new Date().toISOString() } : m)
    s.createVersionSnapshot(`Auto-save before edit: "${trimmed.substring(0, 30)}${trimmed.length > 30 ? '...' : ''}"`)

    const assistantMsgId = getTimestampId('assistant')
    s.setMessages([...truncatedMessages, {
      id: assistantMsgId,
      role: 'assistant' as const,
      content: ASSISTANT_PLACEHOLDER,
      timestamp: new Date().toISOString(),
      provider: s.activeProvider,
      model: s.providerConfigs[s.activeProvider].model
    }])
    const editedMsg = truncatedMessages[truncatedMessages.length - 1]
    // History = everything before the edited (resubmitted) message.
    await startServerTurn({ promptText: trimmed, images: editedMsg?.images, historySource: truncatedMessages.slice(0, -1), userMsgId: editedMsg.id, assistantMsgId })
  }, [layoutMode, setIsChatExpanded, startServerTurn])

  /**
   * The Polish button (D9): a run of its own on the server, with no chat
   * model — a user line, then a reply with the chapter in its "changed this
   * turn" block, so the result is reviewable, undoable through the version
   * snapshot the server takes, and part of the conversation the model sees next.
   */
  const runPolish = useCallback(async (documentId: string) => {
    if (useAppStore.getState().isStreaming) return
    await useAppStore.getState().ensureDocumentContents([documentId])
    const s = useAppStore.getState()
    const doc = s.documents.find(d => d.id === documentId)
    if (!doc || isBlankContent(stripDiffMarkup(doc.content))) return

    const zh = s.language === 'zh'
    const promptText = zh ? `润色《${doc.title}》` : `Polish "${doc.title}"`
    const userMsgId = getTimestampId('user')
    s.addMessage({ id: userMsgId, role: 'user', content: promptText, timestamp: new Date().toISOString() })
    const assistantMsgId = getTimestampId('assistant')
    s.addMessage({
      id: assistantMsgId,
      role: 'assistant',
      content: ASSISTANT_PLACEHOLDER,
      timestamp: new Date().toISOString(),
      provider: s.activeProvider,
      model: s.providerConfigs[s.activeProvider].polishModel?.trim() ||
        defaultPolishModel(s.activeProvider, s.providerConfigs[s.activeProvider].model)
    })
    await startServerTurn({ promptText, historySource: [], userMsgId, assistantMsgId, polish: { documentId, language: zh ? 'zh' : 'en' } })
  }, [startServerTurn])

  // The Polish button asks through the store (it lives in the canvas header);
  // run the request once, through a ref so the effect depends on nothing but
  // the request itself (see the timeout note in CLAUDE.md).
  const runPolishRef = useRef(runPolish)
  useEffect(() => { runPolishRef.current = runPolish }, [runPolish])
  const polishRequest = useAppStore(state => state.polishRequest)
  useEffect(() => {
    if (!polishRequest) return
    useAppStore.getState().clearPolishRequest()
    void runPolishRef.current(polishRequest.documentId)
  }, [polishRequest])

  /**
   * Stop: the draft on screen is kept as one undo step here (the server never
   * saw it), and the server stops the step and holds the queue. The run's
   * `finished` event settles the bubble.
   */
  const handleStopGeneration = useCallback(() => {
    const server = serverRunRef.current
    const s = useAppStore.getState()
    if (!server) {
      s.setStreaming(false)
      return
    }
    const draft = keepCanvasPreview(previewBaseRef.current ?? server.original)
    if (draft !== null) s.updateActiveDocument({ content: draft })
    const live = serverLiveRef.current.get(server.id)
    if (live) live.keptDraft = draft !== null || selectionPreviewedRef.current
    if (s.activeBookId) void serverRunAction(s.activeBookId, server.id, 'stop').catch(e => setErrorMsg(e instanceof Error ? e.message : String(e)))
    s.setStreaming(false)
  }, [keepCanvasPreview])

  return {
    chatInput,
    setChatInput,
    chatInputRef,
    chatEndRef,
    errorMsg,
    setErrorMsg,
    editingMessageId,
    setEditingMessageId,
    editingMessageText,
    setEditingMessageText,
    handleSendMessage,
    handleResubmitMessage,
    handleStopGeneration,
    handleRunAction,
    handleRunAnswer,
    confirmChapterDeletion
  }
}

import React, { useEffect, useRef } from 'react'
import { useEditor, EditorContent } from '@tiptap/react'
import { DOMSerializer } from '@tiptap/pm/model'
import { BubbleMenu } from '@tiptap/react/menus'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import { normalizeBrParagraphs } from '../utils/convert'
import { isSilentPreview } from '../hooks/chat/selectionReplace'
import { saveScrollPosition, loadScrollPosition } from '../utils/scrollMemory'
import { collectDiffRanges, type DiffAction } from '../utils/diffResolution'
import { 
  Bold, 
  Italic, 
  Heading1, 
  Heading2, 
  Heading3,
  List, 
  ListOrdered, 
  Indent,
  Outdent,
  Code,
  Quote,
  Check,
  X,
  Sparkles,
  ArrowDownToLine,
  ArrowUpFromLine,
  Languages,
  Wand2,
  Undo2,
  Redo2,
} from 'lucide-react'
import { useAppStore, isEditLocked } from '../store/useAppStore'
// TipTap extension definitions live in editorExtensions.ts so this file only
// exports components (react-refresh/only-export-components).
import { IndentExtension, DiffAddition, DiffDeletion, CustomImage, BlurredSelection, ParagraphsFromLineBreaks } from './editorExtensions'

interface EditorProps {
  content: string
  onChange: (html: string) => void
  placeholder?: string
  isActive?: boolean
  /**
   * Portrait: the PAGE scrolls, so this component must not build its own
   * fixed-height shell (root box + inner scroller). See App.tsx.
   */
  rootScroll?: boolean
  /** Chapter id — keys the per-chapter scroll memory. */
  documentId?: string
}

export const Editor: React.FC<EditorProps> = ({ 
  content,
  documentId, 
  onChange, 
  placeholder = 'Start writing your document here or let the assistant draft it...',
  isActive = true,
  rootScroll = false
}) => {
  const { setSelectedText, setActiveEditor } = useAppStore()
  // Read-only only while the assistant is writing into THIS chapter; the
  // rest of the book stays editable during a turn (agentic_chat_loop.md §0.4).
  const editLocked = useAppStore(s => isEditLocked(s, documentId ?? s.activeDocumentId))

  // Track content strings that originated FROM this editor's onUpdate.
  // When the content prop changes because of our own edit (user typed/pasted → onUpdate
  // → onChange → Zustand → re-render → new content prop), we must NOT call setContent
  // because the editor already has the correct state. Calling setContent in this case
  // causes a race condition where slightly-stale store HTML overwrites the live editor.
  const contentFromEditorRef = useRef<string | null>(null)

  // Trailing debounce for publishing the selection to the store.
  // Problem: dragging a selection fired onSelectionUpdate on every mousemove
  //   tick, and each tick serialized the selected slice to HTML (O(selection))
  //   and wrote it to the store — which re-renders every store-connected
  //   component (the chapters sidebar re-hashed chapter contents, the footer
  //   recounted words). Selecting ~50 lines of a large chapter froze the UI.
  // Fix: collapse the ticks — serialize and publish ONCE after the selection
  //   settles, and skip no-op writes. Consumers (chat selection context,
  //   quick actions) only need the settled selection, never the mid-drag ones.
  const SELECTION_SETTLE_MS = 200
  const selectionTimerRef = useRef<number | null>(null)
  const lastPublishedSelectionRef = useRef('')

  const handleQuickAction = (action: 'rewrite' | 'shorten' | 'expand' | 'grammar') => {
    let prompt = ''
    switch (action) {
      case 'rewrite':
        // Fiction polish, not business prose: the old wording asked for
        // "more professional", which flattened narrative voice.
        prompt = 'Polish the selected passage as fiction: keep the voice, point of view, tense, and meaning intact; improve rhythm, word choice, and imagery; vary sentence length; cut clichés and filler; prefer showing over telling. Do not add new plot or summarize.'
        break
      case 'shorten':
        prompt = 'Make the selected text more concise and to the point.'
        break
      case 'expand':
        prompt = 'Elaborate on the selected text, adding more detail and depth.'
        break
      case 'grammar':
        prompt = 'Fix any spelling, grammar, or punctuation errors in the selected text.'
        break
    }
    window.dispatchEvent(new CustomEvent('send-quick-action', { detail: prompt }))
  }

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        strike: false,
      }),
      Placeholder.configure({
        placeholder,
      }),
      DiffAddition,
      DiffDeletion,
      BlurredSelection,
      IndentExtension,
      CustomImage,
      ParagraphsFromLineBreaks,
    ],
    // Problem: mobile Firefox froze for seconds after edits (worst on bulk
    //   deletes) in large chapters. Native spellcheck/autocorrect re-scan
    //   large contenteditable regions after every mutation — a known
    //   multi-second stall on Android for document-sized editors.
    // Fix: disable the native text services on the editing host; they are of
    //   little value for long-form drafting and cost O(document) per edit.
    editorProps: {
      attributes: {
        spellcheck: 'false',
        autocorrect: 'off',
        autocapitalize: 'off'
      },
      // Content copied out of a web page usually arrives as one block full of
      // <br> line breaks. Split it into real paragraphs on the way in — a
      // single giant node makes every later edit rebuild the whole chapter,
      // and defeats paragraph-level diffing and LLM context.
      transformPastedHTML: (html) => normalizeBrParagraphs(html)
    },
    content,
    onUpdate: ({ editor, transaction }) => {
      // A server run's selection preview: on screen only, never the store
      // (selectionReplace.ts explains the conflict it caused).
      if (isSilentPreview(transaction)) return
      const html = editor.getHTML()
      // Mark this content as originating from the editor itself
      contentFromEditorRef.current = html
      onChange(html)
    },
    onSelectionUpdate: ({ editor }) => {
      if (selectionTimerRef.current !== null) {
        window.clearTimeout(selectionTimerRef.current)
        selectionTimerRef.current = null
      }

      if (editor.state.selection.empty) {
        // Collapsing is cheap and should feel instant (hides quick actions).
        if (lastPublishedSelectionRef.current !== '') {
          lastPublishedSelectionRef.current = ''
          setSelectedText('')
        }
        return
      }

      selectionTimerRef.current = window.setTimeout(() => {
        selectionTimerRef.current = null
        if (editor.isDestroyed) return
        const { from, to, empty } = editor.state.selection
        if (empty) return
        const slice = editor.state.doc.slice(from, to)
        const serializer = DOMSerializer.fromSchema(editor.state.schema)
        // Serialize into an INERT document: elements created there have no
        // browsing context, so <img src="data:..."> nodes are never loaded
        // or decoded. Building them in the live document instead made every
        // settled selection re-decode each embedded image (a 50-line
        // selection in an import-heavy chapter can hold dozens), which is a
        // main-thread + shared-memory spike big enough to get the tab killed
        // on mobile Firefox. See the matching note in App.tsx.
        const inertDoc = document.implementation.createHTMLDocument('')
        const frag = serializer.serializeFragment(slice.content, { document: inertDoc })
        const div = inertDoc.createElement('div')
        div.appendChild(frag)
        if (div.innerHTML !== lastPublishedSelectionRef.current) {
          lastPublishedSelectionRef.current = div.innerHTML
          setSelectedText(div.innerHTML)
        }
      }, SELECTION_SETTLE_MS)
    }
  })

  // Synchronize incoming content updates (e.g. from LLM streaming, version restore) with TipTap.
  // CRITICAL: Skip when the content prop is just echoing back what we sent via onUpdate,
  // otherwise we get a race condition that causes user edits to "roll back".
  useEffect(() => {
    if (!editor) return

    // If this content originated from our own editor's onUpdate, skip the sync.
    // The editor already has the correct state; overwriting it would cause rollback.
    if (contentFromEditorRef.current === content) {
      return
    }

    // Content came from an external source (LLM streaming, version restore, etc.)
    if (content !== editor.getHTML()) {
      editor.commands.setContent(content, { emitUpdate: false })
    }
  }, [content, editor])

  // Read-only while the assistant writes into this chapter, to avoid sync
  // issues (a live preview paints over it).
  //
  // Problem: stopping a generation mid-stream rolled the editor back to the
  //   pre-stream document, yet a reload showed the half-streamed draft — the
  //   screen and the store disagreed.
  // Root cause: TipTap's setEditable(editable, emitUpdate = true) EMITS a
  //   synthetic `update` event by default, and onUpdate above publishes
  //   editor.getHTML() to the store. The live LLM preview is written with
  //   emitUpdate:false precisely so it never reaches the store — but the Stop
  //   button flips isStreaming, this effect called setEditable(true), and the
  //   preview leaked into the store (then IndexedDB and the server) through
  //   that emit, just before the abort path reset the editor. The same emit
  //   fired on every mount and every stream start, writing the editor's
  //   normalized HTML back over the store each time.
  // Fix: pass emitUpdate=false. Toggling read-only changes no content, so it
  //   must publish none; only real transactions reach the store.
  useEffect(() => {
    if (editor) {
      editor.setEditable(!editLocked, false)
    }
  }, [editor, editLocked])

  // ── Per-chapter scroll memory ────────────────────────────────────────────
  // Mobile Firefox discards a backgrounded tab and reloads it on return, so
  // the reading position has to survive a full remount, not just a re-render.
  const scrollRef = useRef<HTMLDivElement>(null)
  const restoredForRef = useRef<string | null>(null)

  // In portrait the PAGE scrolls (see App.tsx / responsive.css for the
  // Firefox/Android rationale); everywhere else the inner div does. Both
  // effects below must read and drive whichever scroller is actually live.
  useEffect(() => {
    const el = scrollRef.current
    if (!el || !documentId) return
    const rootMode = rootScroll
    const readTop = () => rootMode ? window.scrollY : el.scrollTop
    const target: Window | HTMLElement = rootMode ? window : el
    let timer: number | null = null
    const onScroll = () => {
      if (timer !== null) return
      // Trailing throttle: scrolling fires continuously, persisting must not.
      timer = window.setTimeout(() => {
        timer = null
        saveScrollPosition(documentId, readTop())
      }, 400)
    }
    target.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      if (timer !== null) window.clearTimeout(timer)
      target.removeEventListener('scroll', onScroll)
      // Capture the final position the throttle may have skipped.
      saveScrollPosition(documentId, readTop())
    }
  }, [documentId, rootScroll])

  useEffect(() => {
    const el = scrollRef.current
    if (!el || !documentId || !content) return
    if (restoredForRef.current === documentId) return
    const target = loadScrollPosition(documentId)
    if (target <= 0) {
      restoredForRef.current = documentId
      return
    }
    // The editor lays content out asynchronously; wait two frames so the
    // scroll height exists before jumping, and give up rather than clamp to
    // the bottom if the chapter got shorter.
    let raf2 = 0
    const raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => {
        if (rootScroll) {
          const scroller = document.scrollingElement
          if (scroller && scroller.scrollHeight > target) {
            window.scrollTo(0, target)
            restoredForRef.current = documentId
          }
        } else if (el.scrollHeight > target) {
          el.scrollTop = target
          restoredForRef.current = documentId
        }
      })
    })
    return () => {
      cancelAnimationFrame(raf1)
      if (raf2) cancelAnimationFrame(raf2)
    }
  }, [documentId, content, rootScroll])

  // Cancel a pending selection publish on unmount.
  useEffect(() => {
    return () => {
      if (selectionTimerRef.current !== null) {
        window.clearTimeout(selectionTimerRef.current)
      }
    }
  }, [])

  // Sync editor reference globally for bulk actions
  useEffect(() => {
    if (editor && isActive) {
      setActiveEditor(editor)
    }
    return () => {
      if (isActive) {
        setActiveEditor(null)
      }
    }
  }, [editor, isActive, setActiveEditor])

  // Prevent rendering if editor is not initialized
  if (!editor) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: '3rem', color: 'var(--text-muted)' }}>
        Loading document editor...
      </div>
    )
  }

  // Handle single diff accept/reject using ProseMirror transactions
  const handleResolveDiff = (diffId: string, action: DiffAction) => {
    const { state, view } = editor
    const tr = state.tr

    // Ranges are ordered end-of-document first, so earlier positions stay valid.
    collectDiffRanges(state.doc, action, diffId).forEach(range => {
      if (range.op === 'delete') {
        tr.delete(range.from, range.to)
      } else {
        tr.removeMark(range.from, range.to, state.schema.marks[range.mark])
      }
    })

    view.dispatch(tr)
    onChange(editor.getHTML())
  }

  return (
    <div className="editor-root" style={{
      display: 'flex',
      flexDirection: 'column',
      width: '100%',
      height: rootScroll ? 'auto' : '100%',
      overflow: rootScroll ? 'visible' : 'hidden',
      position: 'relative',
    }}>
      {/* Fixed Formatting Toolbar */}
      {editor && (
        <div className="editor-toolbar" style={{
          display: 'flex',
          gap: '6px',
          padding: '8px 16px',
          borderBottom: '1px solid var(--border-color)',
          alignItems: 'center',
          flexWrap: 'nowrap',
          overflowX: 'auto',
          position: 'sticky',
          top: 0,
          zIndex: 10,
          backgroundColor: 'var(--bg-glass)',
          backdropFilter: 'blur(12px)',
          WebkitBackdropFilter: 'blur(12px)',
        }}>
          {/* Undo / Redo */}
          <button
            onClick={() => editor.chain().focus().undo().run()}
            disabled={!editor.can().undo()}
            className="btn-icon"
            title="Undo (Ctrl+Z)"
            type="button"
            style={{
              backgroundColor: 'transparent',
              color: 'inherit',
              opacity: editor.can().undo() ? 1 : 0.35,
              cursor: editor.can().undo() ? 'pointer' : 'default',
            }}
          >
            <Undo2 size={16} />
          </button>
          <button
            onClick={() => editor.chain().focus().redo().run()}
            disabled={!editor.can().redo()}
            className="btn-icon"
            title="Redo (Ctrl+Shift+Z)"
            type="button"
            style={{
              backgroundColor: 'transparent',
              color: 'inherit',
              opacity: editor.can().redo() ? 1 : 0.35,
              cursor: editor.can().redo() ? 'pointer' : 'default',
            }}
          >
            <Redo2 size={16} />
          </button>

          <div style={{ width: '1px', height: '18px', backgroundColor: 'var(--border-color)', margin: '0 4px' }} />

          <button
            onClick={() => editor.chain().focus().toggleBold().run()}
            className={`btn-icon ${editor.isActive('bold') ? 'active' : ''}`}
            title="Bold (Ctrl+B)"
            type="button"
            style={{
              backgroundColor: editor.isActive('bold') ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('bold') ? 'var(--accent)' : 'inherit',
            }}
          >
            <Bold size={16} />
          </button>
          <button
            onClick={() => editor.chain().focus().toggleItalic().run()}
            className={`btn-icon ${editor.isActive('italic') ? 'active' : ''}`}
            title="Italic (Ctrl+I)"
            type="button"
            style={{
              backgroundColor: editor.isActive('italic') ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('italic') ? 'var(--accent)' : 'inherit',
            }}
          >
            <Italic size={16} />
          </button>
          
          <div style={{ width: '1px', height: '18px', backgroundColor: 'var(--border-color)', margin: '0 4px' }} />

          <button
            onClick={() => editor.chain().focus().toggleHeading({ level: 1 }).run()}
            className={`btn-icon ${editor.isActive('heading', { level: 1 }) ? 'active' : ''}`}
            title="Heading 1"
            type="button"
            style={{
              backgroundColor: editor.isActive('heading', { level: 1 }) ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('heading', { level: 1 }) ? 'var(--accent)' : 'inherit',
            }}
          >
            <Heading1 size={16} />
          </button>
          <button
            onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
            className={`btn-icon ${editor.isActive('heading', { level: 2 }) ? 'active' : ''}`}
            title="Heading 2"
            type="button"
            style={{
              backgroundColor: editor.isActive('heading', { level: 2 }) ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('heading', { level: 2 }) ? 'var(--accent)' : 'inherit',
            }}
          >
            <Heading2 size={16} />
          </button>
          <button
            onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}
            className={`btn-icon ${editor.isActive('heading', { level: 3 }) ? 'active' : ''}`}
            title="Heading 3"
            type="button"
            style={{
              backgroundColor: editor.isActive('heading', { level: 3 }) ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('heading', { level: 3 }) ? 'var(--accent)' : 'inherit',
            }}
          >
            <Heading3 size={16} />
          </button>

          <div style={{ width: '1px', height: '18px', backgroundColor: 'var(--border-color)', margin: '0 4px' }} />

          <button
            onClick={() => editor.chain().focus().toggleBulletList().run()}
            className={`btn-icon ${editor.isActive('bulletList') ? 'active' : ''}`}
            title="Bullet List"
            type="button"
            style={{
              backgroundColor: editor.isActive('bulletList') ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('bulletList') ? 'var(--accent)' : 'inherit',
            }}
          >
            <List size={16} />
          </button>
          <button
            onClick={() => editor.chain().focus().toggleOrderedList().run()}
            className={`btn-icon ${editor.isActive('orderedList') ? 'active' : ''}`}
            title="Numbered List"
            type="button"
            style={{
              backgroundColor: editor.isActive('orderedList') ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('orderedList') ? 'var(--accent)' : 'inherit',
            }}
          >
            <ListOrdered size={16} />
          </button>

          <div style={{ width: '1px', height: '18px', backgroundColor: 'var(--border-color)', margin: '0 4px' }} />

          <button
            onClick={() => {
              if (editor.isActive('listItem')) {
                const hasMarginLeft = editor.state.selection.$from.node().attrs.marginLeft
                if (hasMarginLeft) {
                  editor.chain().focus().outdent().run()
                } else if (editor.can().liftListItem('listItem')) {
                  editor.chain().focus().liftListItem('listItem').run()
                }
              } else {
                editor.chain().focus().outdent().run()
              }
            }}
            className="btn-icon"
            title="Decrease Indent"
            type="button"
            style={{
              backgroundColor: 'transparent',
              color: 'inherit',
            }}
          >
            <Outdent size={16} />
          </button>
          <button
            onClick={() => {
              if (editor.isActive('listItem')) {
                if (editor.can().sinkListItem('listItem')) {
                  editor.chain().focus().sinkListItem('listItem').run()
                } else {
                  editor.chain().focus().indent().run()
                }
              } else {
                editor.chain().focus().indent().run()
              }
            }}
            className="btn-icon"
            title="Increase Indent"
            type="button"
            style={{
              backgroundColor: 'transparent',
              color: 'inherit',
            }}
          >
            <Indent size={16} />
          </button>

          <div style={{ width: '1px', height: '18px', backgroundColor: 'var(--border-color)', margin: '0 4px' }} />

          <button
            onClick={() => editor.chain().focus().toggleBlockquote().run()}
            className={`btn-icon ${editor.isActive('blockquote') ? 'active' : ''}`}
            title="Blockquote"
            type="button"
            style={{
              backgroundColor: editor.isActive('blockquote') ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('blockquote') ? 'var(--accent)' : 'inherit',
            }}
          >
            <Quote size={16} />
          </button>
          <button
            onClick={() => editor.chain().focus().toggleCode().run()}
            className={`btn-icon ${editor.isActive('code') ? 'active' : ''}`}
            title="Inline Code"
            type="button"
            style={{
              backgroundColor: editor.isActive('code') ? 'var(--bg-tertiary)' : 'transparent',
              color: editor.isActive('code') ? 'var(--accent)' : 'inherit',
            }}
          >
            <Code size={16} />
          </button>
        </div>
      )}

      {/* Floating Bubble Menu for Text Selection */}
      {editor && (
        <BubbleMenu 
          editor={editor} 
          updateDelay={100}
          shouldShow={({ editor }) => {
            const { empty } = editor.state.selection
            const isInsideDiff = editor.isActive('diffAddition') || editor.isActive('diffDeletion')
            return !empty || isInsideDiff
          }}
          className="glass-panel editor-bubble-menu"
          style={{
            display: 'flex',
            gap: '4px',
            padding: '4px',
            borderRadius: '8px',
            boxShadow: 'var(--shadow-md)',
            pointerEvents: 'auto',
          }}
        >
          {(() => {
            const isAddition = editor.isActive('diffAddition')
            const isDeletion = editor.isActive('diffDeletion')
            const isInsideDiff = isAddition || isDeletion

            if (isInsideDiff) {
              const attrs = isAddition 
                ? editor.getAttributes('diffAddition') 
                : editor.getAttributes('diffDeletion')
              const diffId = attrs?.['data-diff-id'] || ''

              return (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '2px 6px' }}>
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginRight: '4px' }}>Edit:</span>
                  <button
                    onClick={() => handleResolveDiff(diffId, 'accept')}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      padding: '4px 8px',
                      fontSize: '0.8rem',
                      color: '#10b981',
                      border: 'none',
                      background: 'transparent',
                      cursor: 'pointer',
                      borderRadius: '4px',
                    }}
                    onMouseEnter={(e) => e.currentTarget.style.backgroundColor = 'rgba(16, 185, 129, 0.1)'}
                    onMouseLeave={(e) => e.currentTarget.style.backgroundColor = 'transparent'}
                    title="Accept change"
                    type="button"
                  >
                    <Check size={14} /> Accept
                  </button>
                  <button
                    onClick={() => handleResolveDiff(diffId, 'reject')}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      padding: '4px 8px',
                      fontSize: '0.8rem',
                      color: '#ef4444',
                      border: 'none',
                      background: 'transparent',
                      cursor: 'pointer',
                      borderRadius: '4px',
                    }}
                    onMouseEnter={(e) => e.currentTarget.style.backgroundColor = 'rgba(239, 68, 68, 0.1)'}
                    onMouseLeave={(e) => e.currentTarget.style.backgroundColor = 'transparent'}
                    title="Reject change"
                    type="button"
                  >
                    <X size={14} /> Reject
                  </button>
                </div>
              )
            }

            // Normal text selection formatting & quick actions toolbar
            return (
              <div style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
                <>
                  <>
                    <button
                      onClick={() => {
                        const { from, to, empty } = editor.state.selection
                        const text = empty ? '' : editor.state.doc.textBetween(from, to, ' ')
                        window.dispatchEvent(new CustomEvent('open-image-gen', { detail: text }))
                      }}
                      className="btn-icon"
                      title="Generate image from selection"
                      type="button"
                      style={{
                        background: 'var(--accent)',
                        color: 'var(--accent-text)',
                        borderRadius: '6px',
                        padding: '4px 8px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        fontSize: '0.78rem',
                        fontWeight: 600,
                        border: 'none',
                      }}
                    >
                      <Wand2 size={13} />
                      Gen Image
                    </button>
                    {<div style={{ width: '1px', height: '16px', backgroundColor: 'var(--border-color)' }} />}
                  </>
                </>
                <>
                  <button
                    onClick={() => handleQuickAction('rewrite')}
                      className="btn-icon"
                      title="Polish selection"
                      type="button"
                    >
                      <Sparkles size={16} style={{ color: 'var(--accent)' }} />
                    </button>
                    <button
                      onClick={() => handleQuickAction('shorten')}
                      className="btn-icon"
                      title="Shorten text"
                      type="button"
                    >
                      <ArrowDownToLine size={16} />
                    </button>
                    <button
                      onClick={() => handleQuickAction('expand')}
                      className="btn-icon"
                      title="Expand text"
                      type="button"
                    >
                      <ArrowUpFromLine size={16} />
                    </button>
                    <button
                      onClick={() => handleQuickAction('grammar')}
                      className="btn-icon"
                      title="Fix grammar & spelling"
                      type="button"
                    >
                      <Languages size={16} />
                    </button>
                </>
              </div>
            )
          })()}
        </BubbleMenu>
      )}

      {/* Editor Content Area */}
      <div ref={scrollRef} className="editor-content-scroll" style={{
        flex: rootScroll ? 'none' : 1,
        overflowY: rootScroll ? 'visible' : 'auto',
        outline: 'none',
      }}>
        <EditorContent editor={editor} />
      </div>
    </div>
  )
}

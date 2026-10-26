import React, { useState, useEffect } from 'react'
import { 
  Send, Trash2, AlertCircle, X, SquarePen, ChevronDown, ChevronUp, Image, Square, Clipboard
} from 'lucide-react'
import { StreamingStatus } from './StreamingStatus'
import { ASSISTANT_PLACEHOLDER } from '../hooks/chat/streamHandlers'
import { useAppStore } from '../store/useAppStore'
import { providerDisplayName } from '../utils/providers'
import { convertBlobUrlToDataUrl, convertGifToJpegIfNeeded } from '../utils/text'
import { useImageUpload } from '../hooks/useImageUpload'
import { useChatLLM } from '../hooks/useChatLLM'
import { useTranslation } from '../i18n'
import { AgentTimeline, AgentTurnSummary } from './AgentTurnSummary'
import { chatRows } from './chatRows'
import { RunControls } from './RunControls'

interface ChatPanelProps {
  chatWidth: number
  layoutMode: 'desktop' | 'portrait' | 'landscape' | 'tablet-square'
  isHistoryOpen: boolean
  forceSave: () => void
  setSaveStatus: (status: 'saved' | 'unsaved') => void
}

export const ChatPanel: React.FC<ChatPanelProps> = ({
  chatWidth,
  layoutMode,
  isHistoryOpen,
  forceSave,
  setSaveStatus
}) => {
  const { t } = useTranslation()
  const {
    activeProvider,
    providerConfigs,
    messages,
    clearChat,
    resetSessionTokens,
    isStreaming,
    streamingReasoning,
    selectedText,
    activeEditor
  } = useAppStore()

  const [isChatExpanded, setIsChatExpanded] = useState(false)
  const activeConfig = providerConfigs[activeProvider]
  // A message typed while a chat turn streams steers it (useChatLLM
  // steerRunningTurn), so the input stays open while streaming.

  const {
    uploadedImages,
    setUploadedImages,
    fileInputRef,
    handleImageUpload,
    handlePaste,
    handlePasteFromClipboard
  } = useImageUpload()

  const chatLLM = useChatLLM({
    activeEditor,
    selectedText,
    uploadedImages,
    setUploadedImages,
    layoutMode,
    setIsChatExpanded,
    forceSave,
    setSaveStatus
  })

  const { chatInput, setChatInput, chatInputRef, chatEndRef, errorMsg, setErrorMsg, handleSendMessage, handleStopGeneration } = chatLLM
  const { editingMessageId, setEditingMessageId, editingMessageText, setEditingMessageText, handleResubmitMessage } = chatLLM


  const getProviderLabel = (prov: string) => {
    return providerDisplayName(prov)
  }

  const getResponderName = (msg: typeof messages[0]) => {
    if (msg.role === 'user') return 'You' // Or use translation if preferred
    const prov = msg.provider || activeProvider
    const model = msg.model || activeConfig.model
    return `${getProviderLabel(prov)} (${model})`
  }

  const handleClearChat = () => {
    clearChat()
    resetSessionTokens()
  }

  // Listen for quick actions from the editor
  useEffect(() => {
    const handleQuickAction = (e: Event) => {
      const customEvent = e as CustomEvent<string>
      handleSendMessage(undefined, customEvent.detail)
    }
    window.addEventListener('send-quick-action', handleQuickAction)
    return () => window.removeEventListener('send-quick-action', handleQuickAction)
  }, [handleSendMessage])

  // Scroll to bottom of chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, chatEndRef])


  // The model has produced nothing yet: the bubble still holds the placeholder.
  // Worth distinguishing — the first token can be tens of seconds away.
  // A server run's bubble carries the attached-context label ahead of the
  // placeholder (serverRunEvents.paint), so the placeholder is matched as a
  // suffix as well.
  const lastMessage = messages[messages.length - 1]
  const waitingForFirstToken =
    lastMessage?.role === 'assistant' &&
    (lastMessage.content === ASSISTANT_PLACEHOLDER || lastMessage.content.endsWith(`\n\n${ASSISTANT_PLACEHOLDER}`))

  return (
    <>
    <section 
      className={`chat-panel ${isChatExpanded ? 'expanded' : ''} ${isHistoryOpen ? 'open' : ''}`} 
      style={{ 
        width: layoutMode === 'portrait' 
          ? '100%' 
          : layoutMode === 'landscape' 
          ? '40%' 
          : layoutMode === 'tablet-square' 
          ? '45%' 
          : `${chatWidth}px` 
      }}
    >
      <div className="chat-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          {layoutMode === 'portrait' && (
            <button 
              onClick={() => setIsChatExpanded(false)} 
              className="btn-icon" 
              title={t.app.collapseChat}
              type="button"
            >
              <ChevronDown size={18} />
            </button>
          )}
          <h2>{t.app.chatTitle} ({getProviderLabel(activeProvider)})</h2>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
          <button 
            onClick={handleClearChat} 
            className="btn-icon" 
            title={t.app.clearChat}
            type="button"
          >
            <Trash2 size={16} />
          </button>
        </div>
      </div>

      <div className="chat-messages">
        {chatRows(messages).map(({ key, msg, segment, steered }) => {
          return (
            <div key={key} className={`chat-message ${msg.role}`}>
              {editingMessageId === msg.id ? (
                <div className="chat-message-edit-container">
                  <textarea
                    value={editingMessageText}
                    onChange={(e) => setEditingMessageText(e.target.value)}
                    className="chat-message-edit-textarea"
                  />
                  <div className="chat-message-edit-actions">
                    <button
                      onClick={() => setEditingMessageId(null)}
                      className="btn-secondary"
                      type="button"
                    >
                      {t.app.dismiss}
                    </button>
                    <button
                      onClick={() => handleResubmitMessage(msg.id, editingMessageText)}
                      className="btn-primary"
                      type="button"
                    >
                      {t.app.saveAndSubmit}
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="chat-message-bubble">
                    {msg.images && msg.images.length > 0 && (
                      <div className="chat-message-images" style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '6px' }}>
                        {msg.images.map((img, idx) => (
                          <img 
                            key={idx} 
                            src={img} 
                            alt={`Attachment ${idx + 1}`} 
                            style={{ 
                              maxWidth: '120px', 
                              maxHeight: '120px', 
                              borderRadius: '6px', 
                              objectFit: 'cover',
                              border: '1px solid var(--border-color)' 
                            }} 
                          />
                        ))}
                      </div>
                    )}
                    {msg.role === 'assistant' && msg.agent?.timeline
                      ? <AgentTimeline record={msg.agent} segment={segment} />
                      : <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>}
                    {msg.role === 'assistant' && msg.agent && (!segment || segment.last) && <AgentTurnSummary record={msg.agent} />}
                    {msg.role === 'assistant' && msg.agent && (!segment || segment.last) && (msg.agent.run || msg.agent.question) && (
                      <RunControls
                        record={msg.agent}
                        onAction={chatLLM.handleRunAction}
                        disabled={isStreaming && !msg.agent.run}
                        onAnswer={(record, answer, confirm) => {
                          if (record.run && record.run.status === 'paused') void chatLLM.handleRunAnswer(record.run.id, answer, !!confirm)
                          else if (confirm) {
                            // The Delete button of a tab-run's question: delete first, then answer.
                            void chatLLM.confirmChapterDeletion(confirm).then(deleted => { if (deleted) handleSendMessage(undefined, answer) })
                          } else handleSendMessage(undefined, answer)
                        }}
                      />
                    )}

                  </div>
                  {(
                    <span className="chat-message-info" style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', flexWrap: 'wrap' }}>
                      <span>
                        {getResponderName(msg)}
                        {' • '}
                        {new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                      </span>
                      {msg.role === 'user' && !steered && editingMessageId !== msg.id && !isStreaming && (
                        <button
                          onClick={() => {
                            setEditingMessageId(msg.id)
                            setEditingMessageText(msg.content)
                          }}
                          className="btn-icon"
                          title={t.app.editMessage}
                          type="button"
                          style={{ padding: '0.1rem', background: 'transparent', border: 'none', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', opacity: 0.6 }}
                          onMouseEnter={e => e.currentTarget.style.opacity = '1'}
                          onMouseLeave={e => e.currentTarget.style.opacity = '0.6'}
                        >
                          <SquarePen size={12} />
                        </button>
                      )}
                    </span>
                  )}
                </>
              )}
            </div>
          )
        })}
        {isStreaming && (
          <StreamingStatus
            waiting={waitingForFirstToken}
            reasoning={streamingReasoning}
            waitingLabel={`${getProviderLabel(activeProvider)} ${t.app.waitingFirstToken}`}
            label={`${getProviderLabel(activeProvider)} ${t.app.streamingChanges}`}
          />
        )}
        <div ref={chatEndRef} />
      </div>

      {errorMsg && (
        <div style={{
          margin: '0.75rem',
          padding: '0.75rem',
          borderRadius: '8px',
          backgroundColor: 'rgba(239, 68, 68, 0.15)',
          border: '1px solid rgba(239, 68, 68, 0.3)',
          color: '#f87171',
          fontSize: '0.85rem',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '0.5rem'
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <AlertCircle size={16} style={{ flexShrink: 0 }} />
            <span>{errorMsg}</span>
          </div>
          <button 
            onClick={() => setErrorMsg(null)} 
            className="btn-icon" 
            title={t.app.dismiss}
            type="button"
            style={{ padding: '2px', color: '#f87171' }}
          >
            <X size={14} />
          </button>
        </div>
      )}



      <form onSubmit={handleSendMessage} className="chat-input-container">
        {uploadedImages.length > 0 && (
          <div className="chat-upload-previews" style={{ 
            display: 'flex', 
            gap: '8px', 
            padding: '8px 12px', 
            flexWrap: 'wrap',
            border: '1px solid var(--border-color)',
            borderBottom: 'none',
            backgroundColor: 'var(--bg-tertiary)',
            borderTopLeftRadius: '10px',
            borderTopRightRadius: '10px',
            marginBottom: '-1px'
          }}>
            {uploadedImages.map((img, idx) => (
              <div key={idx} style={{ position: 'relative', display: 'inline-block' }}>
                <img 
                  src={img} 
                  alt="Upload preview" 
                  className="chat-upload-preview-img"
                />
                <button
                  type="button"
                  onClick={() => setUploadedImages(prev => prev.filter((_, i) => i !== idx))}
                  style={{
                    position: 'absolute',
                    top: '-4px',
                    right: '-4px',
                    backgroundColor: 'rgba(239, 68, 68, 0.9)',
                    color: 'white',
                    border: 'none',
                    borderRadius: '50%',
                    width: '16px',
                    height: '16px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: '8px',
                    cursor: 'pointer',
                    padding: 0
                  }}
                >
                  <X size={8} />
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="chat-input-wrapper" style={{
          borderTopLeftRadius: uploadedImages.length > 0 ? '0px' : undefined,
          borderTopRightRadius: uploadedImages.length > 0 ? '0px' : undefined
        }}>
          {layoutMode === 'portrait' && (
            <button
              type="button"
              onClick={() => setIsChatExpanded(!isChatExpanded)}
              className={`btn-icon chat-expand-toggle-btn ${isChatExpanded ? 'expanded' : ''}`}
              title={isChatExpanded ? "Collapse Chat History" : "Expand Chat History"}
              style={{ marginRight: '0.25rem', padding: '0.25rem' }}
            >
              {isChatExpanded ? <ChevronDown size={18} /> : <ChevronUp size={18} />}
            </button>
          )}
          <div style={{ position: 'relative', flex: 1, display: 'flex' }}>
            <div
              ref={chatInputRef}
              contentEditable
              onInput={async e => {
                const container = e.currentTarget
                const imgs = Array.from(container.getElementsByTagName('img'))
                if (imgs.length > 0) {
                  const newImages: string[] = []
                  for (const img of imgs) {
                    const src = img.src
                    if (src) {
                      let dataUrl = src
                      if (src.startsWith('blob:')) {
                        try {
                          dataUrl = await convertBlobUrlToDataUrl(src)
                        } catch (err) {
                          console.error('Failed to convert blob URL:', err)
                        }
                      }
                      try {
                        const processedDataUrl = await convertGifToJpegIfNeeded(dataUrl)
                        newImages.push(processedDataUrl)
                      } catch (err) {
                        console.error('Failed to process pasted/dropped GIF image:', err)
                        newImages.push(dataUrl)
                      }
                    }
                    img.remove()
                  }
                  if (newImages.length > 0) {
                    setUploadedImages(prev => {
                      if (prev.length + newImages.length > 3) {
                        alert(t.app.maxImages)
                        return prev
                      }
                      return [...prev, ...newImages]
                    })
                  }
                }
                setChatInput(container.innerText || '')
              }}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  handleSendMessage()
                }
              }}
              onPaste={handlePaste}
              className="chat-textarea"
              style={{
                overflowY: 'auto',
                minHeight: '24px',
                maxHeight: '120px',
                userSelect: 'text',
                WebkitUserSelect: 'text',
                outline: 'none',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                width: '100%'
              }}
            />
            {!chatInput && (
              <span 
                style={{
                  position: 'absolute',
                  left: 0,
                  top: 0,
                  color: 'var(--text-muted)',
                  pointerEvents: 'none',
                  userSelect: 'none',
                  fontFamily: 'var(--font-ui)',
                  fontSize: '0.95rem',
                  lineHeight: 1.5,
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  width: '100%'
                }}
              >
                {t.app.instructPlaceholder
                  .replace('{provider}', providerDisplayName(activeProvider))
                  .replace('{model}', activeConfig.model)}
              </span>
            )}
          </div>
          <button
            type="button"
            className="btn-icon"
            title={t.app.uploadImage}
            onClick={() => fileInputRef.current?.click()}
            style={{ padding: '0.5rem', color: 'var(--text-secondary)' }}
          >
            <Image size={18} />
          </button>
          <button
            type="button"
            className="btn-icon"
            title={t.app.pasteImage}
            onClick={handlePasteFromClipboard}
            style={{ padding: '0.5rem', color: 'var(--text-secondary)' }}
          >
            <Clipboard size={18} />
          </button>
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleImageUpload}
            accept="image/*"
            multiple
            style={{ display: 'none' }}
          />
          {isStreaming && (
            <button 
              type="button" 
              className="btn-icon animate-pulse" 
              title={t.app.stopGeneration}
              onClick={() => {
                handleStopGeneration()
              }}
              style={{ 
                color: '#ef4444',
                cursor: 'pointer'
              }}
            >
              <Square size={16} fill="#ef4444" />
            </button>
          )}
          {(
            <button 
              type="submit" 
              className="btn-icon" 
              title={t.app.sendInstruction}
              disabled={!chatInput.trim() && uploadedImages.length === 0}
              style={{ 
                color: (chatInput.trim() || uploadedImages.length > 0) ? 'var(--accent)' : 'var(--text-muted)',
                cursor: (chatInput.trim() || uploadedImages.length > 0) ? 'pointer' : 'default'
              }}
            >
              <Send size={18} />
            </button>
          )}
        </div>
      </form>
    </section>

    </>
  )
}

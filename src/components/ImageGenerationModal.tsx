import React, { useState, useEffect, useRef, useCallback } from 'react'
import { X, Wand2 } from 'lucide-react'
import { generateImage, enhancePromptWithLLM, DEFAULT_IMAGE_STYLE_SYSTEM_PROMPT } from '../services/imageGen'
import {
  ASPECT_RATIOS,
  ALL_MODELS,
  PROVIDER_FULL_LABELS,
  PROVIDER_TO_LLM,
  fetchImageModels,
} from '../services/imageGenModels'
import type { AspectRatio } from '../services/imageGenModels'
import { useAppStore } from '../store/useAppStore'
import { ImageGenInputStep } from './imageGen/ImageGenInputStep'
import { ImageGenEnhanceStep } from './imageGen/ImageGenEnhanceStep'
import { ImageGenOptionsStep } from './imageGen/ImageGenOptionsStep'
import { ImageGenPreviewPane } from './imageGen/ImageGenPreviewPane'

interface ImageGenerationModalProps {
  isOpen: boolean
  onClose: () => void
  /** The currently selected text in the editor – pre-fills the prompt */
  initialPrompt?: string
  /** Plain-text content of the active document – used as additional context for LLM enhancement */
  documentContext?: string
  /** Called when the user clicks "Insert into Editor" */
  onInsertImage: (dataUrl: string, altText: string) => void
}

// ─── Main Component ──────────────────────────────────────────────────────────

export const ImageGenerationModal: React.FC<ImageGenerationModalProps> = ({
  isOpen,
  onClose,
  initialPrompt = '',
  documentContext = '',
  onInsertImage,
}) => {
  const {
    imageGenConfig, updateImageGenConfig,
    activeProvider, providerConfigs,
  } = useAppStore()

  // ── Input fields ──
  const [rawPrompt, setRawPrompt] = useState(initialPrompt)
  const [context, setContext] = useState('')
  const [negativePrompt, setNegativePrompt] = useState('')
  const [aspectRatio, setAspectRatio] = useState<AspectRatio>('1:1')
  const [style, setStyle] = useState<'vivid' | 'natural'>('vivid')

  // ── LLM Enhancement ──
  const [enhancedPrompt, setEnhancedPrompt] = useState('')
  const [isEnhancing, setIsEnhancing] = useState(false)
  const [enhanceError, setEnhanceError] = useState<string | null>(null)
  const enhanceAbortRef = useRef<AbortController | null>(null)

  // ── Image Generation ──
  const [isGenerating, setIsGenerating] = useState(false)
  const [generatedImage, setGeneratedImage] = useState<string | null>(null)
  const [revisedPrompt, setRevisedPrompt] = useState<string | null>(null)
  const [genError, setGenError] = useState<string | null>(null)

  // ── UI ──
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [showStylePrompt, setShowStylePrompt] = useState(false)
  const [zoom, setZoom] = useState(1)
  const [insertSuccess, setInsertSuccess] = useState(false)
  const [promptFocused, setPromptFocused] = useState(false)
  const [contextFocused, setContextFocused] = useState(false)
  const [enhFocused, setEnhFocused] = useState(false)
  const promptRef = useRef<HTMLTextAreaElement>(null)

  // ── Model discovery ──
  const [discoveredModels, setDiscoveredModels] = useState<string[] | null>(null)
  const [isDiscovering, setIsDiscovering] = useState(false)
  const [discoverError, setDiscoverError] = useState<string | null>(null)

  // Reset when modal opens (or when the caller-provided prompt/context change
  // while open). Implemented as the "adjust state during render" pattern to
  // avoid cascading setState-in-effect renders.
  const [prevResetKey, setPrevResetKey] = useState<{ isOpen: boolean; initialPrompt: string; documentContext: string }>({ isOpen: false, initialPrompt: '', documentContext: '' })
  if (prevResetKey.isOpen !== isOpen || prevResetKey.initialPrompt !== initialPrompt || prevResetKey.documentContext !== documentContext) {
    setPrevResetKey({ isOpen, initialPrompt, documentContext })
    if (isOpen) {
      setRawPrompt(initialPrompt || '')
      setContext(documentContext ? documentContext.slice(0, 800) : '')
      setEnhancedPrompt('')
      setEnhanceError(null)
      setGeneratedImage(null)
      setRevisedPrompt(null)
      setGenError(null)
      setInsertSuccess(false)
      setZoom(1)
    }
  }

  // DOM/network side effects of opening/closing stay in an effect
  useEffect(() => {
    if (isOpen) {
      setTimeout(() => promptRef.current?.focus(), 100)
    } else {
      // Cancel any in-flight enhancement when modal closes
      enhanceAbortRef.current?.abort()
    }
  }, [isOpen])

  // Derived values needed by hooks below — must be computed before any early return
  const provider = imageGenConfig.provider
  const sharedLlmProvider = PROVIDER_TO_LLM[provider]
  const sharedApiKey = sharedLlmProvider ? providerConfigs[sharedLlmProvider]?.apiKey : ''
  const effectiveApiKey = sharedApiKey || imageGenConfig.apiKey
  const usingSharedKey = Boolean(sharedApiKey)

  // ── Model Discovery hooks (must be before early return) ──

  const handleDiscoverModels = useCallback(async () => {
    if (!effectiveApiKey) {
      setDiscoverError('Add an API key first to discover available models.')
      return
    }
    setIsDiscovering(true)
    setDiscoverError(null)
    try {
      const models = await fetchImageModels(provider, effectiveApiKey, imageGenConfig.baseUrl)
      setDiscoveredModels(models)
      const currentModel = imageGenConfig.model || ALL_MODELS[provider][0]
      if (!models.includes(currentModel)) {
        updateImageGenConfig({ model: models[0] })
      }
    } catch (err) {
      setDiscoverError(err instanceof Error && err.message ? err.message : 'Failed to fetch models.')
    } finally {
      setIsDiscovering(false)
    }
  }, [provider, effectiveApiKey, imageGenConfig.baseUrl, imageGenConfig.model, updateImageGenConfig])

  // Reset discovered models when the provider changes ("adjust state during
  // render" pattern — avoids a cascading setState-in-effect render).
  const [prevProvider, setPrevProvider] = useState(provider)
  if (provider !== prevProvider) {
    setPrevProvider(provider)
    setDiscoveredModels(null)
    setDiscoverError(null)
  }

  // Auto-discover when the advanced panel opens (and key is available).
  useEffect(() => {
    if (showAdvanced && effectiveApiKey && !discoveredModels && !isDiscovering) {
      // Legitimate external-system sync (network fetch of the model list);
      // the synchronous setState inside is just the isDiscovering guard flag.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      handleDiscoverModels()
    }
  }, [showAdvanced, effectiveApiKey, provider]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!isOpen) return null

  const selectedAspect = ASPECT_RATIOS.find(r => r.value === aspectRatio) ?? ASPECT_RATIOS[0]
  const llmConfig = { ...providerConfigs[activeProvider], provider: activeProvider }
  const stylePromptText = imageGenConfig.styleSystemPrompt || DEFAULT_IMAGE_STYLE_SYSTEM_PROMPT
  const llmEnhancementEnabled = imageGenConfig.llmEnhancementEnabled !== false

  // The models to show in the dropdown — discovered list takes priority over static fallback
  const availableModels = discoveredModels ?? ALL_MODELS[provider]

  // The final prompt sent to image API – enhanced if available, else raw
  const finalPrompt = enhancedPrompt.trim() || rawPrompt.trim()

  // ── Handlers ──

  const handleEnhancePrompt = async () => {
    if (!rawPrompt.trim()) {
      setEnhanceError('Enter a prompt first.')
      return
    }
    if (!providerConfigs[activeProvider].apiKey && activeProvider !== 'ollama' && activeProvider !== 'runpod') {
      setEnhanceError(`No API key for the active LLM (${activeProvider}). Configure it in Settings.`)
      return
    }

    setIsEnhancing(true)
    setEnhancedPrompt('')
    setEnhanceError(null)
    const abortController = new AbortController()
    enhanceAbortRef.current = abortController

    try {
      const result = await enhancePromptWithLLM(
        rawPrompt,
        context,
        stylePromptText,
        llmConfig,
        (chunk) => setEnhancedPrompt(prev => prev + chunk),
        abortController.signal
      )
      setEnhancedPrompt(result)
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      if ((err instanceof Error && err.name === 'AbortError') || message.includes('abort')) return
      setEnhanceError(message || 'Failed to enhance prompt. Check your LLM settings.')
    } finally {
      setIsEnhancing(false)
      enhanceAbortRef.current = null
    }
  }

  const handleGenerate = async () => {
    if (!finalPrompt) {
      setGenError('Please enter a prompt first.')
      return
    }
    if (!effectiveApiKey) {
      setGenError(`No API key for ${PROVIDER_FULL_LABELS[provider]}. ${sharedLlmProvider ? `Add a ${sharedLlmProvider} API key in Settings.` : 'Add it in the Advanced panel below.'}`)
      return
    }

    setIsGenerating(true)
    setGeneratedImage(null)
    setRevisedPrompt(null)
    setGenError(null)

    const promptToSend = negativePrompt.trim()
      ? `${finalPrompt}. Avoid: ${negativePrompt.trim()}`
      : finalPrompt

    try {
      const result = await generateImage(promptToSend, {
        provider,
        apiKey: effectiveApiKey,
        model: imageGenConfig.model || ALL_MODELS[provider][0],
        baseUrl: imageGenConfig.baseUrl,
        width: selectedAspect.w,
        height: selectedAspect.h,
        aspectRatio: selectedAspect.value,
        style: provider === 'openai' ? style : undefined,
      })
      setGeneratedImage(result.dataUrl)
      if (result.revisedPrompt) setRevisedPrompt(result.revisedPrompt)
    } catch (err) {
      setGenError(err instanceof Error && err.message ? err.message : 'Image generation failed. Please check your API key and try again.')
    } finally {
      setIsGenerating(false)
    }
  }

  const handleInsert = () => {
    if (!generatedImage) return
    onInsertImage(generatedImage, finalPrompt.slice(0, 120))
    setInsertSuccess(true)
    setTimeout(onClose, 600)
  }

  return (
    <div
      className="modal-overlay"
      onClick={onClose}
      onKeyDown={e => { if (e.key === 'Escape') onClose() }}
      role="dialog"
      aria-modal="true"
      aria-label="AI Image Generation"
    >
      <div
        className="modal-content glass-panel"
        onClick={e => e.stopPropagation()}
        style={{
          maxWidth: generatedImage ? '900px' : '600px',
          width: '96vw',
          border: '1px solid var(--border-color)',
          backgroundColor: 'var(--bg-secondary)',
          color: 'var(--text-primary)',
          transition: 'max-width 0.3s ease',
          display: 'flex',
          flexDirection: 'column',
          gap: '0',
          maxHeight: '92vh',
          overflowY: 'auto',
          padding: '0',
        }}
      >
        {/* ── Header ── */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '1rem 1.25rem 0.85rem',
          borderBottom: '1px solid var(--border-color)',
          position: 'sticky', top: 0, zIndex: 10,
          backgroundColor: 'var(--bg-secondary)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
            <div style={{
              width: 34, height: 34, borderRadius: '9px',
              background: 'var(--accent)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: '0 2px 8px rgba(128, 128, 128, 0.35)',
            }}>
              <Wand2 size={17} style={{ color: 'var(--accent-text)' }} />
            </div>
            <div>
              <div style={{ fontSize: '0.975rem', fontWeight: 700 }}>AI Image Generation</div>
              <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '1px' }}>
                LLM prompt enhancement → image generation → insert to editor
              </div>
            </div>
          </div>
          <button onClick={onClose} className="btn-icon" title="Close" type="button">
            <X size={18} />
          </button>
        </div>

        {/* ── Body ── */}
        <div style={{ display: 'flex', gap: '0', flex: 1 }}>

          {/* ──── Left column: Controls ──── */}
          <div style={{
            flex: 1, minWidth: '280px',
            display: 'flex', flexDirection: 'column', gap: '0',
            padding: '1rem 1.25rem',
            overflowY: 'auto',
          }}>

            {/* ═══ STEP 1 – Input ═══ */}
            <ImageGenInputStep
              rawPrompt={rawPrompt}
              setRawPrompt={setRawPrompt}
              context={context}
              setContext={setContext}
              promptRef={promptRef}
              promptFocused={promptFocused}
              setPromptFocused={setPromptFocused}
              contextFocused={contextFocused}
              setContextFocused={setContextFocused}
              showStylePrompt={showStylePrompt}
              setShowStylePrompt={setShowStylePrompt}
              styleSystemPrompt={imageGenConfig.styleSystemPrompt}
              updateImageGenConfig={updateImageGenConfig}
            />

            {/* ═══ STEP 2 – LLM Enhancement ═══ */}
            <ImageGenEnhanceStep
              enhancedPrompt={enhancedPrompt}
              setEnhancedPrompt={setEnhancedPrompt}
              llmEnhancementEnabled={llmEnhancementEnabled}
              updateImageGenConfig={updateImageGenConfig}
              activeProvider={activeProvider}
              activeModel={providerConfigs[activeProvider].model}
              isEnhancing={isEnhancing}
              enhFocused={enhFocused}
              setEnhFocused={setEnhFocused}
              enhanceError={enhanceError}
              rawPrompt={rawPrompt}
              onEnhance={handleEnhancePrompt}
              onStopEnhance={() => { enhanceAbortRef.current?.abort(); setIsEnhancing(false) }}
            />

            {/* ═══ STEP 3 – Image Generation Options ═══ */}
            <ImageGenOptionsStep
              aspectRatio={aspectRatio}
              setAspectRatio={setAspectRatio}
              provider={provider}
              imageGenConfig={imageGenConfig}
              updateImageGenConfig={updateImageGenConfig}
              showAdvanced={showAdvanced}
              setShowAdvanced={setShowAdvanced}
              availableModels={availableModels}
              discoveredModels={discoveredModels}
              isDiscovering={isDiscovering}
              discoverError={discoverError}
              onDiscoverModels={handleDiscoverModels}
              effectiveApiKey={effectiveApiKey}
              usingSharedKey={usingSharedKey}
              sharedLlmProvider={sharedLlmProvider}
              style={style}
              setStyle={setStyle}
              negativePrompt={negativePrompt}
              setNegativePrompt={setNegativePrompt}
              finalPrompt={finalPrompt}
              enhancedPrompt={enhancedPrompt}
              genError={genError}
              isGenerating={isGenerating}
              isEnhancing={isEnhancing}
              onGenerate={handleGenerate}
            />
          </div>

          {/* ──── Right column: Image Preview ──── */}
          {generatedImage && (
            <ImageGenPreviewPane
              generatedImage={generatedImage}
              zoom={zoom}
              setZoom={setZoom}
              revisedPrompt={revisedPrompt}
              insertSuccess={insertSuccess}
              isGenerating={isGenerating}
              onInsert={handleInsert}
              onRegenerate={handleGenerate}
            />
          )}
        </div>
      </div>
    </div>
  )
}

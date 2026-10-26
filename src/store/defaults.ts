/**
 * Default constants for the app store: provider configs, mock documents,
 * system prompt presets, and image pipeline defaults. Pure data — no side
 * effects — so any store module can import it without cycle concerns.
 */
import type { ImageGenConfig, SystemPromptTemplate } from '../types/llm'
import type { CanvasDocument } from '../types/document'

/** Every provider's default config, from the provider registry (utils/providers). */
export { DEFAULT_CONFIGS } from '../utils/providers'


export const MOCK_DOCUMENTS: CanvasDocument[] = [
  {
    id: 'doc-1',
    title: 'Chapter 1: Introduction',
    content: `<h1>Getting Started with Web Canvas</h1>
<p>Welcome to <strong>Web Canvas</strong>! This is an LLM-powered environment designed for writing and document collaboration.</p>
<p>You can manage your chapters in the Chapters Sidebar on the left-most side of the screen.</p>
<p>Ask the assistant for anything in the chat on the left: it can read, search and edit every chapter of this book, and each change arrives as a diff for you to accept or reject.</p>
<p>Pin a chapter in the sidebar (the pin on its row) to keep it in the assistant's context on every turn — an outline, a character sheet.</p>`,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  },
  {
    id: 'doc-2',
    title: 'Chapter 2: Setup Guide',
    content: `<h1>Setup and Config</h1>
<p>Open <strong>Settings</strong> (the gear in the header), pick a provider and enter its API key. The model list loads from your key.</p>
<p>The assistant reads other chapters on its own when it needs them; pin the ones it should always have.</p>`,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  },
]

export const DEFAULT_SYSTEM_PROMPTS: SystemPromptTemplate[] = [
  {
    id: 'prompt-none',
    name: 'General Assistant',
    content: '',
  },
  {
    id: 'prompt-academic',
    name: 'Academic Style',
    content: 'Write in a highly academic, formal, and rigorous tone. Use precise terminology and passive voice where appropriate for scientific style.',
  },
  {
    id: 'prompt-concise',
    name: 'Concise Editor',
    content: 'Be extremely concise. Eliminate all unnecessary words, explanations, and redundant sentences. Focus on high information density.',
  },
  {
    id: 'prompt-creative',
    name: 'Creative Storyteller',
    content: 'Emphasize narrative flow, engaging vocabulary, sensory details, and vivid imagery. Adapt tone to be highly expressive.',
  }
]

export const DEFAULT_IMAGE_GEN_CONFIG: ImageGenConfig = {
  provider: 'openai',
  apiKey: import.meta.env.VITE_OPENAI_API_KEY || '',
  model: 'dall-e-3',
  baseUrl: '',
  styleSystemPrompt: '',       // empty = use DEFAULT_IMAGE_STYLE_SYSTEM_PROMPT from imageGen.ts
  llmEnhancementEnabled: true, // enhance prompts with LLM by default
}


/**
 * Default polish prompt (agentic_chat_loop.md D9). Measured on
 * grok-4.20-reasoning over a sample of grok-4.6 drafts: narration rhythm
 * close to a human-written reference, every dialogue line kept. User-editable in Settings; the user's active
 * writing preset is appended to `system`.
 */
export const DEFAULT_POLISH_PROMPT = {
  system: "你是中文小说的作者兼编辑。下面是写作说明，写作时遵守。",
  template: "下面是一章小说中的一段（约 {n} 字）。请改写这一段的叙述句子，让它连贯、成熟。\n\n硬性要求：\n- 情节、动作、细节、心理和对话的意思全部保留，对话原话照抄，不加新情节，篇幅与原文相当。\n- 必须真正改写句子：把零碎的短句用动作承接、因果、主从关系合成完整的句子。除对话外，和原文逐字相同的连续片段不得超过 20 个字。\n- 不要只把句号换成逗号，也不要用逗号把一串短句连成一长串。\n- 用 <p> 分段，分段与原文大致对应。只输出改写后的这一段，不要任何说明。\n- 篇幅控制在原文的正负 10% 以内。合句靠调整语序和连接，不靠添加「了、着、再次、一会儿」这类虚字凑字数。\n- 正常用逗号断句，一个小分句一般不超过 20 个字，不要写出一长串没有标点的句子。\n- 不用「并不……」「却还是……」这类先否定再找补的说法。\n\n上一段的最后一句（只作衔接参考，不要输出）：{prev}\n\n【要改写的这一段】\n{part}"
}

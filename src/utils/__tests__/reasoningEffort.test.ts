import { describe, it, expect } from 'vitest'
import {
  supportedReasoningEfforts,
  supportsReasoningEffort,
  resolveReasoningEffort,
  reasoningBudgetTokens,
  moonshotThinking,
  DEFAULT_REASONING_EFFORT
} from '../reasoningEffort'

describe('moonshot (Kimi)', () => {
  it('offers kimi-k3 its three efforts and the others none', () => {
    expect(supportedReasoningEfforts('moonshot', 'kimi-k3')).toEqual(['default', 'low', 'high', 'xhigh'])
    expect(supportedReasoningEfforts('moonshot', 'kimi-k2.6')).toEqual(['default'])
    expect(supportedReasoningEfforts('moonshot', 'kimi-k2.7-code')).toEqual(['default'])
  })

  it("sends kimi-k3 this app's low when nothing was chosen — its own default is max", () => {
    expect(resolveReasoningEffort('moonshot', 'kimi-k3', undefined)).toBe('low')
    expect(moonshotThinking('kimi-k3', 'low')).toEqual({ reasoning_effort: 'low' })
    expect(moonshotThinking('kimi-k3', 'xhigh')).toEqual({ reasoning_effort: 'max' })
    expect(moonshotThinking('kimi-k3', null)).toEqual({})
    // medium is not a Kimi level: nothing is sent rather than a 400.
    expect(resolveReasoningEffort('moonshot', 'kimi-k3', 'medium')).toBeNull()
  })

  it('keeps kimi-k2.6 thinking across turns and sends kimi-k2.7-code nothing', () => {
    expect(moonshotThinking('kimi-k2.6', null)).toEqual({ thinking: { type: 'enabled', keep: 'all' } })
    expect(moonshotThinking('kimi-k2.7-code', null)).toEqual({})
    expect(moonshotThinking('moonshot-v1-8k', 'low')).toEqual({})
  })
})

describe('supportedReasoningEfforts', () => {
  it('follows the provider docs per model family', () => {
    // xAI: grok-4.6 adds xhigh, grok-4.5 does not, grok-3-mini has no medium.
    expect(supportedReasoningEfforts('grok', 'grok-4.6')).toEqual(['default', 'low', 'medium', 'high', 'xhigh'])
    expect(supportedReasoningEfforts('grok', 'grok-4.5')).toEqual(['default', 'low', 'medium', 'high'])
    expect(supportedReasoningEfforts('grok', 'grok-3-mini')).toEqual(['default', 'low', 'high'])
  })

  it('follows what api.x.ai actually accepted when probed (2026-10-05)', () => {
    // grok-4.7 and grok-4.3 took every level; they were missing from the
    // table, so the UI offered only the provider default for them.
    expect(supportedReasoningEfforts('grok', 'grok-4.7')).toEqual(['default', 'low', 'medium', 'high', 'xhigh'])
    expect(supportedReasoningEfforts('grok', 'grok-4.3')).toEqual(['default', 'low', 'medium', 'high', 'xhigh'])
    expect(supportedReasoningEfforts('grok', 'grok-4.3-latest')).toEqual(['default', 'low', 'medium', 'high', 'xhigh'])
    // grok-4.20 rejects the parameter, so offering levels only cost a 400
    // and a retry per turn. Every 4.20 id shares that, whatever its suffix.
    expect(supportedReasoningEfforts('grok', 'grok-4.20-0309-reasoning')).toEqual(['default'])
    expect(supportedReasoningEfforts('grok', 'grok-4.20')).toEqual(['default'])
    expect(resolveReasoningEffort('grok', 'grok-4.20-0309-reasoning', 'high')).toBeNull()
    // A two-digit minor must not be mistaken for its first digit.
    expect(supportedReasoningEfforts('grok', 'grok-4.35')).toEqual(['default'])
  })

  it('offers nothing but the provider default for a model with no such control', () => {
    // An unknown model is not assumed to accept the parameter — sending it
    // would 400 a turn for a setting the user cannot benefit from.
    expect(supportedReasoningEfforts('grok', 'grok-2-1212')).toEqual(['default'])
    expect(supportedReasoningEfforts('openai', 'gpt-4o')).toEqual(['default'])
    expect(supportedReasoningEfforts('ollama', 'llama3')).toEqual(['default'])
    expect(supportedReasoningEfforts('made-up', 'whatever')).toEqual(['default'])
  })

  it('knows which models can be adjusted at all', () => {
    expect(supportsReasoningEffort('grok', 'grok-4.6')).toBe(true)
    expect(supportsReasoningEffort('openai', 'o3-mini')).toBe(true)
    expect(supportsReasoningEffort('openai', 'gpt-4o')).toBe(false)
  })
})

describe('resolveReasoningEffort', () => {
  it('defaults to this app, not the provider', () => {
    // grok-4.6 defaults to 'high' and spent minutes thinking before its first
    // visible token; for document editing that trade is wrong.
    expect(DEFAULT_REASONING_EFFORT).toBe('low')
    expect(resolveReasoningEffort('grok', 'grok-4.6', undefined)).toBe('low')
  })

  it('sends nothing when the user explicitly picks the provider default', () => {
    expect(resolveReasoningEffort('grok', 'grok-4.6', 'default')).toBeNull()
  })

  it('passes a level the model accepts', () => {
    expect(resolveReasoningEffort('grok', 'grok-4.6', 'xhigh')).toBe('xhigh')
    expect(resolveReasoningEffort('openai', 'gpt-5', 'minimal')).toBe('minimal')
    // Measured 2026-10-10: o3 refuses minimal, gpt-5.1 too; 5.2+ takes xhigh.
    expect(resolveReasoningEffort('openai', 'o3', 'minimal')).toBeNull()
    expect(resolveReasoningEffort('openai', 'gpt-6.1-sol', 'xhigh')).toBe('xhigh')
  })

  it('drops a level the model does not accept rather than risk a 400', () => {
    expect(resolveReasoningEffort('grok', 'grok-4.5', 'xhigh')).toBeNull()
    expect(resolveReasoningEffort('grok', 'grok-3-mini', 'medium')).toBeNull()
    // Including the app default, on a model with no reasoning control at all.
    expect(resolveReasoningEffort('openai', 'gpt-4o', undefined)).toBeNull()
    expect(resolveReasoningEffort('ollama', 'llama3', 'low')).toBeNull()
  })
})

describe('reasoningBudgetTokens', () => {
  it('rises with the level', () => {
    const budgets = (['minimal', 'low', 'medium', 'high', 'xhigh'] as const).map(reasoningBudgetTokens)
    expect(budgets).toEqual([...budgets].sort((a, b) => a - b))
    // Anthropic rejects a budget below 1024, so 'low' must clear it.
    expect(reasoningBudgetTokens('low')).toBeGreaterThanOrEqual(1024)
  })
})

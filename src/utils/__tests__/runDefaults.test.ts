import { describe, it, expect } from 'vitest'
import { defaultPolishModel, polishOffered } from '../runDefaults'

describe('polishOffered', () => {
  it('is on for grok, whose polish model is another model by default', () => {
    expect(defaultPolishModel('grok', 'grok-4.6')).toBe('grok-4.20-0309-reasoning')
    expect(polishOffered('grok', { model: 'grok-4.6' })).toBe(true)
  })

  it('is off when the polish model is the chat model itself (2026-10-19: Claude re-polishing its own prose)', () => {
    expect(polishOffered('anthropic', { model: 'claude-sonnet-5-5' })).toBe(false)
    expect(polishOffered('anthropic', { model: 'claude-sonnet-5-5', polishModel: ' claude-sonnet-5-5 ' })).toBe(false)
    expect(polishOffered('openai', { model: 'gpt-6-luna', polishModel: '' })).toBe(false)
  })

  it('is on once a different polish model is set', () => {
    expect(polishOffered('anthropic', { model: 'claude-sonnet-5-5', polishModel: 'claude-haiku-5-5' })).toBe(true)
  })
})

/**
 * The bubble renders an agentic turn in the order it happened: text, the
 * calls of that step, the next step's text… (no JSX: the runner only picks up
 * .ts test files).
 */
import { describe, it, expect } from 'vitest'
import { createElement, act } from 'react'
import { createRoot } from 'react-dom/client'
import { AgentTimeline } from '../AgentTurnSummary'
import type { AgentTurnRecord } from '../../types/chat'

function render(record: AgentTurnRecord): HTMLElement {
  const container = document.createElement('div')
  act(() => { createRoot(container).render(createElement(AgentTimeline, { record })) })
  return container
}

const lines = (el: HTMLElement) =>
  [...el.querySelectorAll('.agent-timeline > div')].map(d => `${d.className}|${d.textContent}`)

describe('AgentTimeline', () => {
  const base: AgentTurnRecord = {
    status: 'done',
    steps: 2,
    trace: ['📖 read #3', '✏️ rewrote #1'],
    touched: [],
    timeline: [
      { type: 'text', text: 'Let me read the outline.' },
      { type: 'tool', line: '📖 read #3', ok: true },
      { type: 'text', text: 'Done.' },
      { type: 'tool', line: '⚠️ edited #2: 1 of 2 located', ok: false }
    ],
    prefix: '（前言）',
    suffix: '⚠️ 1 suggested change could not be located'
  }

  it('interleaves each step\'s text with the calls it made, between the label and the warnings', () => {
    expect(lines(render(base))).toEqual([
      'agent-timeline-text agent-timeline-prefix|（前言）',
      'agent-timeline-text|Let me read the outline.',
      'agent-tool-line|📖 read #3',
      'agent-timeline-text|Done.',
      'agent-tool-line failed|⚠️ edited #2: 1 of 2 located',
      'agent-timeline-text|⚠️ 1 suggested change could not be located'
    ])
  })

  it('shows the step in flight after the finished ones while the turn runs', () => {
    const running = render({ ...base, status: 'running', suffix: undefined, live: 'Now writing chapter 4…' })
    expect(lines(running).at(-1)).toBe('agent-timeline-text|Now writing chapter 4…')

    const waiting = render({ ...base, status: 'running', suffix: undefined, live: undefined })
    expect(lines(waiting).at(-1)).toContain('agent-tool-line pending|')
  })
})

describe('AgentTurnSummary rows', () => {
  it('says a failed polish chunk kept its draft, and an unmatched edit was not located', async () => {
    const { AgentTurnSummary } = await import('../AgentTurnSummary')
    const { useAppStore } = await import('../../store/useAppStore')
    useAppStore.setState({
      language: 'en',
      documents: [
        { id: 'd1', title: '第一章 很长很长的章节标题', content: '<p>x</p>', createdAt: '', updatedAt: '' },
        { id: 'd2', title: '第二章', content: '<p>y</p>', createdAt: '', updatedAt: '' }
      ]
    })
    const container = document.createElement('div')
    act(() => {
      createRoot(container).render(createElement(AgentTurnSummary, {
        record: {
          status: 'done', steps: 1, trace: [], timeline: [],
          touched: [
            { documentId: 'd1', titleAtRun: '第一章', kind: 'polished', changes: 3, failed: 1 },
            { documentId: 'd2', titleAtRun: '第二章', kind: 'edits', changes: 2, failed: 1 }
          ]
        }
      }))
    })
    const rows = [...container.querySelectorAll('.agent-touched-row')]
    expect(rows[0].querySelector('.agent-touched-kind')?.textContent).toBe('polished (3 chunks) · 1 chunk kept as drafted')
    expect(rows[1].querySelector('.agent-touched-kind')?.textContent).toBe('2 changes · 1 not located')
    // Status and View sit in one group, so they wrap together instead of overflowing.
    expect(rows[0].querySelector('.agent-touched-actions')?.querySelectorAll('.agent-touched-status, .agent-touched-view')).toHaveLength(2)
  })
})

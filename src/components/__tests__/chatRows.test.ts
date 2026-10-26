import { describe, it, expect } from 'vitest'
import { chatRows, splitTimeline } from '../chatRows'
import type { ChatMessage } from '../../types/chat'

const user = (id: string, content: string): ChatMessage => ({ id, role: 'user', content, timestamp: 't' })
const turn = (id: string, timeline: NonNullable<ChatMessage['agent']>['timeline']): ChatMessage => ({
  id, role: 'assistant', content: 'joined', timestamp: 't',
  agent: { status: 'done', steps: 3, trace: [], touched: [], timeline }
})

describe('chatRows', () => {
  it('shows a steered turn split: the bubble so far, the steer, the rest as a new bubble', () => {
    const rows = chatRows([
      user('u1', '写第六章'),
      turn('a1', [{ type: 'text', text: '先读。' }, { type: 'tool', line: '📖 read', ok: true }, { type: 'steer', messageIds: ['u2'], text: '顺便改标题' }, { type: 'text', text: '标题改了。' }]),
      user('u2', '顺便改标题'),
      user('u3', '下一个请求')
    ])
    expect(rows.map(r => r.key)).toEqual(['u1', 'a1#0', 'a1#0:u2', 'a1#1', 'u3'])
    expect(rows[1].segment).toEqual({ items: [{ type: 'text', text: '先读。' }, { type: 'tool', line: '📖 read', ok: true }], first: true, last: false })
    expect(rows[2]).toMatchObject({ steered: true, msg: { id: 'u2' } })
    expect(rows[3].segment).toEqual({ items: [{ type: 'text', text: '标题改了。' }], first: false, last: true })
    // The steer is shown inside the turn, not again at its stored place.
    expect(rows.filter(r => r.msg.id === 'u2')).toHaveLength(1)
  })

  it('leaves a turn without steers whole, and shows a steer the chat does not have by its text', () => {
    expect(chatRows([user('u1', 'x'), turn('a1', [{ type: 'text', text: 'y' }])]).map(r => r.key)).toEqual(['u1', 'a1'])
    const rows = chatRows([turn('a1', [{ type: 'steer', messageIds: ['gone'], text: '另一台设备发的' }, { type: 'text', text: 'ok' }])])
    expect(rows.map(r => r.key)).toEqual(['a1#0', 'a1#0:steer', 'a1#1'])
    expect(rows[1].msg).toMatchObject({ role: 'user', content: '另一台设备发的' })
  })

  it('splits a timeline at each steer', () => {
    const { parts, steers } = splitTimeline([{ type: 'steer', messageIds: [], text: 'a' }, { type: 'text', text: 'x' }, { type: 'steer', messageIds: [], text: 'b' }])
    expect(parts).toEqual([[], [{ type: 'text', text: 'x' }], []])
    expect(steers.map(s => s.text)).toEqual(['a', 'b'])
  })
})

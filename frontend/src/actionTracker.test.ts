import { describe, expect, it, vi } from 'vitest'
import { ActionTracker, type ActionRow } from './actionTracker'
import { ApiError } from './api'

describe('批量操作回执', () => {
  it('125 个任务使用一个批次查询，提交后立即保留回执', async () => {
    const fetcher = vi.fn().mockResolvedValue({ id: 'batch', state: 'succeeded', reason: '人工核实',
      summary: { total: 125, pending: 0, succeeded: 125, failed: 0, cancelled: 0 } })
    const tracker = new ActionTracker(fetcher)
    tracker.accept({ batch_id: 'batch', action_ids: Array.from({ length: 125 }, (_, i) => `task-${i}`) }, '人工核实')
    expect(tracker.items).toHaveLength(1)
    expect(tracker.items[0]?.summary?.pending).toBe(125)
    await tracker.refresh()
    expect(fetcher).toHaveBeenCalledExactlyOnceWith('batch')
    expect(tracker.items[0]?.summary?.succeeded).toBe(125)
  })

  it('状态查询限流时保留已提交任务，随后查询恢复真实结果', async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new ApiError(429, '请求过于频繁'))
      .mockResolvedValue({ id: 'batch', state: 'failed', error: '1 个任务失败',
        summary: { total: 125, pending: 0, succeeded: 124, failed: 1, cancelled: 0 } })
    const tracker = new ActionTracker(fetcher)
    tracker.accept({ batch_id: 'batch', action_ids: Array.from({ length: 125 }, (_, i) => String(i)) }, '人工核实')
    await tracker.refresh()
    expect(tracker.items[0]?.state).toBe('pending')
    expect(tracker.items[0]?.tracking_error).toContain('任务已提交')
    await tracker.refresh()
    expect(tracker.items[0]?.state).toBe('failed')
    expect(tracker.items[0]?.tracking_error).toBe('')
  })

  it('慢查询不会产生重叠轮询或重复提交', async () => {
    let finish!: (row: ActionRow) => void
    const fetcher = vi.fn(() => new Promise<ActionRow>(resolve => { finish = resolve }))
    const tracker = new ActionTracker(fetcher)
    tracker.accept({ id: 'task', state: 'pending', reason: '人工核实' }, '人工核实')
    const first = tracker.refresh()
    await tracker.refresh()
    expect(fetcher).toHaveBeenCalledTimes(1)
    finish({ id: 'task', state: 'succeeded', reason: '人工核实' })
    await first
    await tracker.refresh()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('主动刷新能接续管理员重试后的批次进度', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce({ id: 'batch', state: 'pending', reason: '人工核实' })
      .mockResolvedValue({ id: 'batch', state: 'succeeded', reason: '人工核实' })
    const tracker = new ActionTracker(fetcher)
    tracker.accept({ id: 'batch', state: 'failed', reason: '人工核实' }, '人工核实')
    await tracker.refresh()
    expect(fetcher).not.toHaveBeenCalled()
    await tracker.refresh(true)
    expect(tracker.items[0]?.state).toBe('pending')
    await tracker.refresh()
    expect(tracker.items[0]?.state).toBe('succeeded')
  })

  it('125 个独立回执按最多 50 个合并查询，不逐条增加轮询请求', async () => {
    const single = vi.fn()
    const bulk = vi.fn(async (ids: string[]) => ({
      items: ids.map(id => ({ id, state: 'succeeded', reason: '核对' })), errors: {},
    }))
    const tracker = new ActionTracker(single, bulk)
    for (let i = 0; i < 125; i++) tracker.accept({ id: String(i), state: 'pending', reason: '核对' }, '核对')
    await tracker.refresh()
    expect(single).not.toHaveBeenCalled()
    expect(bulk.mock.calls.map(([ids]) => ids.length)).toEqual([50, 50, 25])
    expect(tracker.items.every(item => item.state === 'succeeded')).toBe(true)
    await tracker.refresh()
    expect(bulk).toHaveBeenCalledTimes(3)
  })

  it('合并查询中的单条失败不会遮住其他任务的完成结果', async () => {
    const bulk = vi.fn().mockResolvedValue({ items: [{ id: 'done', state: 'succeeded', reason: '核对' }],
      errors: { denied: { status: 403, detail: '你不是此群的管理员' } } })
    const tracker = new ActionTracker(vi.fn(), bulk)
    for (const id of ['done', 'denied', 'missing']) tracker.accept({ id, state: 'pending', reason: '核对' }, '核对')
    await tracker.refresh()
    expect(tracker.items.find(item => item.id === 'done')?.state).toBe('succeeded')
    expect(tracker.items.find(item => item.id === 'denied')?.tracking_error).toContain('你不是此群')
    expect(tracker.items.find(item => item.id === 'missing')?.tracking_error).toContain('未收到操作状态')
  })

  it('慢合并查询不重叠，查询失败保留全部回执并在下一次恢复', async () => {
    let reject!: (reason: Error) => void
    const bulk = vi.fn().mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
      .mockResolvedValue({ items: ['a', 'b'].map(id => ({ id, state: 'succeeded', reason: '核对' })), errors: {} })
    const tracker = new ActionTracker(vi.fn(), bulk)
    for (const id of ['a', 'b']) tracker.accept({ id, state: 'pending', reason: '核对' }, '核对')
    const first = tracker.refresh()
    await tracker.refresh()
    expect(bulk).toHaveBeenCalledTimes(1)
    reject(new ApiError(429, '暂时限流'))
    await first
    expect(tracker.items.every(item => item.state === 'pending' && item.tracking_error?.includes('暂时限流'))).toBe(true)
    await tracker.refresh()
    expect(tracker.items.every(item => item.state === 'succeeded' && item.tracking_error === '')).toBe(true)
  })
})

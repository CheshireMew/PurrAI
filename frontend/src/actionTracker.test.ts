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
})

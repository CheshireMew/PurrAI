import { api, ApiError } from './api'

export type ActionRow = {
  id: string
  state: string
  reason: string
  kind?: string
  error?: string
  tracking_error?: string
  payload?: Record<string, any>
  summary?: Record<string, number>
}

export type BatchReceipt = { batch_id: string; action_ids: string[] }
type ActionStatuses = { items: ActionRow[]; errors: Record<string, { status: number; detail: string }> }
type FetchAction = (id: string) => Promise<ActionRow>
type FetchStatuses = (ids: string[]) => Promise<ActionStatuses>

export class ActionTracker {
  items: ActionRow[] = []
  refreshing = false
  private retryAt = 0
  private failures = 0
  private cycleFailed = false

  private fetchAction: FetchAction
  private fetchStatuses?: FetchStatuses

  constructor(fetchAction?: FetchAction, fetchStatuses?: FetchStatuses) {
    this.fetchAction = fetchAction || (id => api<ActionRow>(`/api/actions/${id}`))
    this.fetchStatuses = fetchStatuses || (fetchAction ? undefined : ids => {
      const query = new URLSearchParams(ids.map(id => ['identifiers', id]))
      return api<ActionStatuses>(`/api/actions?${query}`)
    })
  }

  accept(receipt: ActionRow | BatchReceipt, reason: string) {
    const row: ActionRow = 'action_ids' in receipt ? {
      id: receipt.batch_id, kind: 'batch_release', reason,
      state: receipt.action_ids.length ? 'pending' : 'succeeded',
      summary: { total: receipt.action_ids.length, pending: receipt.action_ids.length,
        succeeded: 0, failed: 0, cancelled: 0 },
    } : { ...receipt, reason: receipt.reason || reason }
    const existing = this.items.find(item => item.id === row.id)
    if (existing) Object.assign(existing, row)
    else this.items.unshift(row)
    return row
  }

  async refresh(includeFinished = false) {
    if (this.refreshing || Date.now() < this.retryAt) return
    this.refreshing = true
    this.cycleFailed = false
    try {
      const pending = this.items.filter(item => includeFinished || item.state === 'pending')
      for (let start = 0; start < pending.length; start += 50) {
        const items = pending.slice(start, start + 50)
        if (items.length > 1 && this.fetchStatuses) {
          try {
            const result = await this.fetchStatuses(items.map(item => item.id))
            for (const item of items) {
              const row = result.items.find(row => row.id === item.id), error = result.errors[item.id]
              if (row) Object.assign(item, row, { tracking_error: '' })
              else this.failed(item, new ApiError(error?.status || 502, error?.detail || '未收到操作状态'))
            }
          } catch (error) {
            for (const item of items) this.failed(item, error)
            break
          }
        } else {
          for (const item of items) {
            try { Object.assign(item, await this.fetchAction(item.id), { tracking_error: '' }) }
            catch (error) { this.failed(item, error); break }
          }
        }
      }
      if (!this.cycleFailed) { this.failures = 0; this.retryAt = 0 }
    } finally { this.refreshing = false }
  }

  private failed(item: ActionRow, error: unknown) {
    if (!this.cycleFailed) this.failures++
    this.cycleFailed = true
    this.retryAt = Math.max(this.retryAt, Date.now() + Math.max(error instanceof ApiError ? error.retryAfter * 1000 : 0,
      Math.min(60000, 1000 * 2 ** Math.min(this.failures, 6))))
    // An unavailable status never discards the receipt or resubmits the operation.
    item.tracking_error = `任务已提交，结果暂时无法刷新：${error instanceof Error ? error.message : '连接失败'}`
    if (error instanceof ApiError && error.status === 401) throw error
  }
}

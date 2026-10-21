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

export class ActionTracker {
  items: ActionRow[] = []
  refreshing = false

  constructor(private fetchAction = (id: string) => api<ActionRow>(`/api/actions/${id}`)) {}

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
    if (this.refreshing) return
    this.refreshing = true
    try {
      for (const item of this.items.filter(item => includeFinished || item.state === 'pending')) {
        try {
          Object.assign(item, await this.fetchAction(item.id), { tracking_error: '' })
        } catch (error) {
          // Acceptance survives a failed status request. Keep the last known
          // outcome and retry this same batch instead of submitting it again.
          item.tracking_error = `任务已提交，结果暂时无法刷新：${error instanceof Error ? error.message : '连接失败'}`
          if (error instanceof ApiError && error.status === 401) throw error
        }
      }
    } finally { this.refreshing = false }
  }
}

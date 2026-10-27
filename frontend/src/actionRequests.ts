import { ApiError, post } from './api'
import type { ActionRow, BatchReceipt } from './actionTracker'

export type ActionBody = { action: string; reason: string }
export type ActionRequest = { path: string; body: ActionBody & { request_id: string }; error: string }
export type AcceptedAction = { receipt: ActionRow | BatchReceipt; reason: string }
const uncertain = '提交结果暂未确认，请点击“确认提交结果”；系统会沿用原请求，避免重复操作'
function browserStorage() {
  try { return sessionStorage } catch { return null }
}

export class ActionRequests {
  items: ActionRequest[] = []
  private user = 0
  private inFlight = new Map<string, Promise<AcceptedAction>>()

  constructor(private storage: Pick<Storage, 'getItem' | 'setItem'> | null = browserStorage(),
              private send = post) {}

  useUser(user: number) {
    this.user = user
    this.items = []
    try {
      const saved: unknown = JSON.parse(this.storage?.getItem(this.storageKey()) || '[]')
      if (Array.isArray(saved)) this.items = saved.filter((item): item is ActionRequest =>
        typeof item?.path === 'string' && item.path.startsWith('/api/') &&
        typeof item?.body?.request_id === 'string' && typeof item.body.action === 'string' &&
        typeof item.body.reason === 'string').map(item => ({ ...item, error: uncertain }))
    } catch { /* Storage can be unavailable; this session still retains requests in memory. */ }
  }

  private storageKey() { return `purrai:action-requests:${this.user}` }
  private key(path: string, action: string) { return JSON.stringify([this.user, path, action]) }
  private save() {
    try { this.storage?.setItem(this.storageKey(), JSON.stringify(this.items)) }
    catch { /* Keep the in-memory receipt even if browser storage is full. */ }
  }

  submit(path: string, body: ActionBody): Promise<AcceptedAction> {
    const key = this.key(path, body.action)
    const running = this.inFlight.get(key)
    if (running) return running
    let request = this.items.find(item => item.path === path && item.body.action === body.action)
    if (!request) {
      request = { path, body: { ...body, request_id: crypto.randomUUID() }, error: '' }
      this.items.push(request)
      this.save() // Persist the exact intent before sending, including its original reason.
    }
    const command = request, user = this.user
    const pending = this.perform(command, user).finally(() => this.inFlight.delete(key))
    this.inFlight.set(key, pending)
    return pending
  }

  retry(request: ActionRequest) { return this.submit(request.path, request.body) }

  private async perform(request: ActionRequest, user: number): Promise<AcceptedAction> {
    this.items = this.items.map(item => item.body.request_id === request.body.request_id ? { ...item, error: '' } : item)
    try {
      const receipt = await this.send(request.path, request.body) as ActionRow | BatchReceipt
      if (!(typeof (receipt as ActionRow)?.id === 'string' && typeof (receipt as ActionRow).state === 'string') &&
          !(typeof (receipt as BatchReceipt)?.batch_id === 'string' && Array.isArray((receipt as BatchReceipt).action_ids))) {
        throw new Error('未收到有效的操作回执')
      }
      if (this.user === user) {
        this.items = this.items.filter(item => item.body.request_id !== request.body.request_id)
        this.save()
      }
      return { receipt, reason: request.body.reason }
    } catch (error) {
      if (this.user === user) {
        if (error instanceof ApiError && [400, 404, 409, 410, 422].includes(error.status)) {
          this.items = this.items.filter(item => item.body.request_id !== request.body.request_id)
        } else this.items = this.items.map(item => item.body.request_id === request.body.request_id
          ? { ...item, error: uncertain } : item)
        this.save()
      }
      throw error
    }
  }
}

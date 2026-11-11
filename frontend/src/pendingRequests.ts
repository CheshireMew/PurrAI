import { ApiError } from './api'

export type RequestStorage = Pick<Storage, 'getItem' | 'setItem'> | null
export function browserStorage(): RequestStorage {
  try { return sessionStorage } catch { return null }
}

type RequestPolicy<Request, Result> = {
  namespace: string
  uncertain: string
  identify(request: Request): string
  valid(value: any): value is Request
  perform(request: Request): Promise<Result>
  rejectedStatuses?: number[]
}

// Both administrative actions and edits persist their exact intent before I/O.
// Their adapters own intent matching and receipt validation; this class owns recovery.
export class PendingRequests<Request extends { error: string }, Result> {
  items: Request[] = []
  private user = 0
  private inFlight = new Map<string, Promise<Result>>()

  constructor(private policy: RequestPolicy<Request, Result>, private storage: RequestStorage) {}

  useUser(user: number) {
    this.user = user
    this.items = []
    try {
      const saved: unknown = JSON.parse(this.storage?.getItem(this.storageKey()) || '[]')
      if (Array.isArray(saved)) this.items = saved.filter(this.policy.valid)
        .map(item => ({ ...item, error: this.policy.uncertain }))
    } catch { /* Storage failure leaves this session's requests usable in memory. */ }
  }

  private storageKey() { return `purrai:${this.policy.namespace}:${this.user}` }
  private save() {
    try { this.storage?.setItem(this.storageKey(), JSON.stringify(this.items)) }
    catch { /* Keep the original intent in memory when storage is unavailable or full. */ }
  }

  protected remember(request: Request) {
    this.items.push(request)
    this.save()
    return request
  }

  retry(request: Request): Promise<Result> {
    const identifier = this.policy.identify(request)
    const original = this.items.find(item => this.policy.identify(item) === identifier)
    if (!original) return Promise.reject(new Error('待确认请求不存在，请刷新后重试'))
    const key = JSON.stringify([this.user, identifier])
    const running = this.inFlight.get(key)
    if (running) return running
    const pending = this.perform(original, this.user).finally(() => this.inFlight.delete(key))
    this.inFlight.set(key, pending)
    return pending
  }

  private async perform(request: Request, user: number): Promise<Result> {
    const identifier = this.policy.identify(request)
    const update = (remove: boolean, error = '') => {
      if (user !== this.user) return
      this.items = remove ? this.items.filter(item => this.policy.identify(item) !== identifier)
        : this.items.map(item => this.policy.identify(item) === identifier ? { ...item, error } : item)
      this.save()
    }
    update(false)
    try {
      const result = await this.policy.perform(request)
      update(true)
      return result
    } catch (error) {
      const rejected = error instanceof ApiError
        && [400, 404, 409, 410, 422, ...(this.policy.rejectedStatuses || [])].includes(error.status)
      update(rejected, this.policy.uncertain)
      throw error
    }
  }
}

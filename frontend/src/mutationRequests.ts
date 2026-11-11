import { api } from './api'
import { browserStorage, PendingRequests, type RequestStorage } from './pendingRequests'

export type MutationRequest = {
  id: string; path: string; method: string; body: unknown; version?: string; title: string; error: string
}
const uncertain = '提交结果待确认'
export class MutationRequests extends PendingRequests<MutationRequest, unknown> {
  constructor(storage: RequestStorage = browserStorage(), send = api) {
    super({
      namespace: 'mutations', uncertain, rejectedStatuses: [428],
      identify: request => request.id,
      valid: (item): item is MutationRequest => typeof item?.id === 'string'
        && typeof item.path === 'string' && item.path.startsWith('/api/')
        && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(item.method) && typeof item.title === 'string',
      perform: async request => {
        const body = request.body && typeof request.body === 'object' ? { ...request.body, request_id: request.id } : request.body
        const result = await send(request.path, { method: request.method, body: JSON.stringify(body),
          headers: { 'Idempotency-Key': request.id, ...(request.version ? { 'If-Match': request.version } : {}) } })
        if (!result || typeof result !== 'object') throw new Error('未收到有效的保存结果')
        if (/\/rules(?:\/[^/]+)?$/.test(request.path) && !request.path.endsWith('/rules/batch') && typeof (result as any).id !== 'string') {
          throw new Error('未收到有效的规则保存结果')
        }
        return result
      },
    }, storage)
  }
  async submit<T = unknown>(path: string, method: string, body: unknown, version: string | undefined, title: string): Promise<T> {
    let request = this.items.find(item => item.path === path && item.method === method)
    if (request && (JSON.stringify(request.body) !== JSON.stringify(body) || request.version !== version)) {
      throw new Error('请先在下方确认上次提交结果，再提交新修改')
    }
    if (!request) {
      request = this.remember({ id: crypto.randomUUID(), path, method, body: JSON.parse(JSON.stringify(body)), version, title, error: '' })
    }
    return this.retry(request) as Promise<T>
  }
}

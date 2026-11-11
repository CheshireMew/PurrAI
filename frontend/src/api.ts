import { stateLabel } from './catalog'

export class ApiError extends Error {
  constructor(public status: number, message: string, public retryAfter = 0) { super(message) }
}

let csrf = ''
const readBackoffs = new Map<string, { until: number; error: ApiError }>()
export function setCsrf(token: string) { if (csrf !== token) readBackoffs.clear(); csrf = token }
export async function api<T = any>(path: string, init: RequestInit = {}, received?: (response: Response) => void): Promise<T> {
  const key = path.split('?')[0]!, reading = !init.method || ['GET', 'HEAD'].includes(init.method)
  const backoff = readBackoffs.get(key)
  if (reading && backoff && Date.now() < backoff.until) throw backoff.error
  const response = await fetch(path, {
    ...init, signal: init.signal ? AbortSignal.any([init.signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000),
    credentials: 'same-origin', headers: {
      'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...init.headers,
    },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = data.detail
    const message = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map((e: any) => `${e.loc?.slice(1).join('.')}: ${e.msg}`).join('；')
      : detail?.message ? `${detail.message}：${detail.missing?.join('、') || ''}` : '请求失败，请稍后重试'
    const retry = response.headers.get('Retry-After') || ''
    const seconds = /^\d+$/.test(retry) ? Number(retry) : Math.max(0, (Date.parse(retry) - Date.now()) / 1000)
    const error = new ApiError(response.status, message, Number.isFinite(seconds) ? seconds : 0)
    if (reading && error.status === 429) readBackoffs.set(key, { until: Date.now() + Math.max(2, error.retryAfter) * 1000, error })
    throw error
  }
  if (reading) readBackoffs.delete(key)
  received?.(response)
  return data as T
}
export function post(path: string, body: unknown = {}) { return api(path, { method: 'POST', body: JSON.stringify(body) }) }
export function put(path: string, body: unknown) { return api(path, { method: 'PUT', body: JSON.stringify(body) }) }
export function time(value: number) {
  return value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'medium' }).format(value * 1000) : '—'
}
export function label(state: string) {
  return stateLabel(state)
}

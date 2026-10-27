import { stateLabel } from './catalog'

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

let csrf = ''
export function setCsrf(token: string) { csrf = token }
export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
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
    throw new ApiError(response.status, message)
  }
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

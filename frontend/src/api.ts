export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

let csrf = ''
export function setCsrf(token: string) { csrf = token }
export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init, credentials: 'same-origin', headers: {
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
  return ({ unapproved: '等待所有者批准', pending: '等待处理', active: '保护中', paused: '已暂停', error: '需要处理', muting: '正在禁言',
    releasing: '等待恢复发言', removing: '正在移出', failing: '正在执行失败处理', passed: '已通过', failed: '失败', succeeded: '已完成',
    cancelled: '已取消', left: '已退群', superseded: '已换新验证' } as Record<string, string>)[state] || state
}

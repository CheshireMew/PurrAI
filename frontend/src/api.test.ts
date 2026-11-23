import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, label, setCsrf, time } from './api'
import { configureCatalog } from './catalog'
import catalogFixture from '../e2e/fixtures/catalog.json'
import type { AdminCatalog } from './types'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); setCsrf('') })
describe('后台接口契约', () => {
  it('发送会话与 CSRF，并保留实际错误原因', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '你没有此群的管理权限' }), { status: 403 }))
    vi.stubGlobal('fetch', fetcher); setCsrf('csrf-value')
    await expect(api('/api/groups/1/settings', { method: 'PATCH' })).rejects.toMatchObject({ status: 403, message: '你没有此群的管理权限' })
    expect(fetcher.mock.calls[0][1].headers['X-CSRF-Token']).toBe('csrf-value')
    expect(fetcher.mock.calls[0][1].credentials).toBe('same-origin')
  })
  it('将字段校验错误转换成可读文案', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: [{ loc: ['body', 'max_attempts'], msg: '至少为 1' }] }), { status: 422 })))
    await expect(api('/api/test')).rejects.toBeInstanceOf(ApiError)
  })
  it('定位嵌套奖品和导入记录，用中文解释服务端限制', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: [
      { loc: ['body', 'config', 'prizes', 0, 'name'], type: 'string_too_short', ctx: { min_length: 1 }, msg: 'String should have at least 1 character' },
      { loc: ['body', 'rows', 2, 'points'], type: 'greater_than_equal', ctx: { ge: 0 }, msg: 'Input should be greater than or equal to 0' },
    ] }), { status: 422 })))
    await expect(api('/api/test')).rejects.toMatchObject({ message: '奖品 1 的名称：不能为空；第 3 条记录的积分：不能小于 0' })
  })
  it('连接失败提供重试提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(api('/api/test')).rejects.toMatchObject({ status: 0, message: '无法连接后台，请检查网络后重试' })
  })
  it('操作仍在等待时不会显示成已完成', () => {
    configureCatalog(catalogFixture as AdminCatalog)
    expect(label('pending')).toBe('等待处理'); expect(label('succeeded')).toBe('已完成')
  })
  it('明确使用北京时间展示时间', () => {
    expect(time(Date.UTC(2026, 9, 17, 0, 0, 0) / 1000)).toContain('08:00:00')
  })
  it('遵守读取接口 Retry-After，等待期间不继续发请求且不阻止写入', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: '稍后重试' }),
      { status: 429, headers: { 'Retry-After': '30' } })).mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetcher)
    await expect(api('/api/retry-budget')).rejects.toMatchObject({ retryAfter: 30 })
    await expect(api('/api/retry-budget')).rejects.toMatchObject({ status: 429 })
    expect(fetcher).toHaveBeenCalledTimes(1)
    await api('/api/retry-budget', { method: 'POST' })
    expect(fetcher).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(30001)
    await api('/api/retry-budget')
    expect(fetcher).toHaveBeenCalledTimes(3)
  })
})

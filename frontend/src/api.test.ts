import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, label, setCsrf, time } from './api'
import { configureCatalog } from './catalog'
import catalogFixture from '../e2e/fixtures/catalog.json'
import type { AdminCatalog } from './types'

afterEach(() => vi.unstubAllGlobals())
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
  it('操作仍在等待时不会显示成已完成', () => {
    configureCatalog(catalogFixture as AdminCatalog)
    expect(label('pending')).toBe('等待处理'); expect(label('succeeded')).toBe('已完成')
  })
  it('明确使用北京时间展示时间', () => {
    expect(time(Date.UTC(2026, 9, 17, 0, 0, 0) / 1000)).toContain('08:00:00')
  })
})

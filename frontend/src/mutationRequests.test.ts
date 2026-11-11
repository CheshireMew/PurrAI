import { describe, expect, it, vi } from 'vitest'
import { MutationRequests } from './mutationRequests'
import { ApiError } from './api'

function storage() {
  const map = new Map<string, string>()
  return { getItem: (key: string) => map.get(key) || null, setItem: (key: string, value: string) => { map.set(key, value) } }
}

describe('编辑请求确认', () => {
  it('写入成功但回执丢失，刷新后仍使用原编号、内容和版本', async () => {
    const saved = storage(), results = new Map<string, unknown>()
    const send = vi.fn(async (_path: string, init: RequestInit) => {
      const id = (init.headers as Record<string, string>)['Idempotency-Key']!
      if (!results.has(id)) { results.set(id, { id: 'one-rule', version: 'new' }); throw new Error('response lost') }
      return results.get(id)
    })
    const before = new MutationRequests(saved, send as any); before.useUser(1)
    await expect(before.submit('/api/groups/-1/rules', 'PUT', { value: 'original' }, 'old', '保存规则')).rejects.toThrow('response lost')
    const after = new MutationRequests(saved, send as any); after.useUser(1)
    expect(after.items).toHaveLength(1)
    await expect(after.submit('/api/groups/-1/rules', 'PUT', { value: 'changed' }, 'old', '保存规则')).rejects.toThrow('确认上次')
    await after.retry(after.items[0]!)
    expect(send.mock.calls[0]?.[1]).toEqual(send.mock.calls[1]?.[1])
    expect(results.size).toBe(1)
    expect(after.items).toHaveLength(0)
  })

  it('冲突结束原请求，新草稿可用新编号提交，跨账号不混用', async () => {
    const send = vi.fn().mockRejectedValueOnce(new ApiError(409, '记录已被修改')).mockResolvedValue({ ok: true })
    const requests = new MutationRequests(storage(), send); requests.useUser(1)
    await expect(requests.submit('/api/groups/-1/whitelist', 'PUT', { note: 'old' }, 'v1', '保存白名单')).rejects.toThrow('已被修改')
    expect(requests.items).toHaveLength(0)
    await requests.submit('/api/groups/-1/whitelist', 'PUT', { note: 'new' }, 'v2', '保存白名单')
    expect(send.mock.calls[0][1].headers['Idempotency-Key']).not.toBe(send.mock.calls[1][1].headers['Idempotency-Key'])
    requests.useUser(2)
    expect(requests.items).toHaveLength(0)
  })
})

import { describe, expect, it, vi } from 'vitest'
import { ActionRequests } from './actionRequests'
import { MutationRequests } from './mutationRequests'

function storage() {
  const data = new Map<string, string>()
  return { getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value) } }
}
const adapters = [
  { name: '管理动作', create: (saved: any, send: any) => new ActionRequests(saved, send),
    submit: (requests: any) => requests.submit('/api/verifications/v/actions', { action: 'release', reason: '人工核实' }),
    receipt: { id: 'action', state: 'pending' }, namespace: 'action-requests',
    saved: { path: '/api/verifications/v/actions', body: { action: 'release', reason: '人工核实', request_id: 'legacy-id' }, error: '' } },
  { name: '编辑保存', create: (saved: any, send: any) => new MutationRequests(saved, send),
    submit: (requests: any) => requests.submit('/api/groups/-1/rules/r', 'PUT', { value: 'original' }, 'version', '保存规则'),
    receipt: { id: 'rule', version: 'new' }, namespace: 'mutations',
    saved: { id: 'legacy-id', path: '/api/groups/-1/rules/r', method: 'PUT', body: { value: 'original' }, version: 'version', title: '保存规则', error: '' } },
]

describe.each(adapters)('$name共用请求恢复', adapter => {
  it('兼容已有会话存储，确认时保留原始编号', async () => {
    const saved = storage(), key = `purrai:${adapter.namespace}:1`
    saved.setItem(key, JSON.stringify([adapter.saved]))
    const send = vi.fn().mockResolvedValue(adapter.receipt)
    const requests = adapter.create(saved, send); requests.useUser(1)
    await requests.retry(requests.items[0] as any)
    const init = send.mock.calls[0]![1]
    expect(init.request_id || init.headers['Idempotency-Key']).toBe('legacy-id')
    expect(JSON.parse(saved.getItem(key)!)).toEqual([])
  })

  it('旧账号请求完成时，不改变新账号的数据或存储，也不能跨账号重试', async () => {
    let finish!: (value: unknown) => void
    const saved = storage(), send = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockRejectedValue(new Error('offline'))
    const requests = adapter.create(saved, send); requests.useUser(1)
    const old = adapter.submit(requests), oldRequest = requests.items[0]!
    requests.useUser(2)
    await expect(requests.retry(oldRequest as any)).rejects.toThrow('待确认请求不存在')
    await expect(adapter.submit(requests)).rejects.toThrow('offline')
    const current = JSON.stringify(requests.items), persisted = saved.getItem(`purrai:${adapter.namespace}:2`)
    finish(adapter.receipt); await old
    expect(JSON.stringify(requests.items)).toBe(current)
    expect(saved.getItem(`purrai:${adapter.namespace}:2`)).toBe(persisted)
  })

  it('存储不可用时保留内存请求，并发确认只发送一次', async () => {
    const unavailable = { getItem: () => { throw new Error('blocked') }, setItem: () => { throw new Error('full') } }
    const send = vi.fn().mockRejectedValueOnce(new Error('lost receipt')).mockResolvedValue(adapter.receipt)
    const requests = adapter.create(unavailable, send); requests.useUser(1)
    await expect(adapter.submit(requests)).rejects.toThrow('lost receipt')
    const pending = requests.items[0]!
    await Promise.all([requests.retry(pending as any), requests.retry(pending as any)])
    expect(send).toHaveBeenCalledTimes(2)
    expect(send.mock.calls[0]![1]).toEqual(send.mock.calls[1]![1])
    expect(requests.items).toEqual([])
  })
})

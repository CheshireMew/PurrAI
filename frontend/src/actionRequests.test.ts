import { describe, expect, it, vi } from 'vitest'
import { ActionRequests } from './actionRequests'
import { ApiError } from './api'

function storage() {
  const saved = new Map<string, string>()
  return { getItem: (key: string) => saved.get(key) ?? null, setItem: (key: string, value: string) => { saved.set(key, value) } }
}
const path = '/api/verifications/v1/actions', body = { action: 'reissue', reason: '核实后重新出题' }

describe('未确认的管理请求', () => {
  it('回执丢失后刷新页面，沿用原请求和原因，后端只执行一次', async () => {
    const saved = storage(), accepted = new Map<string, object>(), sent: any[] = []
    const send = vi.fn(async (_path: string, request: any) => {
      sent.push(request)
      if (!accepted.has(request.request_id)) {
        accepted.set(request.request_id, { id: 'one-task', state: 'pending' })
        throw new TypeError('Failed to fetch')
      }
      return accepted.get(request.request_id)
    })
    const before = new ActionRequests(saved, send); before.useUser(1)
    await expect(before.submit(path, body)).rejects.toThrow('Failed to fetch')
    expect(before.items[0]?.error).toContain('提交结果暂未确认')
    const after = new ActionRequests(saved, send); after.useUser(1)
    const result = await after.submit(path, { ...body, reason: '修改过的输入不会变成第二次提交' })
    expect(sent[0]).toEqual(sent[1]); expect(accepted.size).toBe(1)
    expect(result.reason).toBe(body.reason); expect(result.receipt).toMatchObject({ id: 'one-task' })
    expect(after.items).toEqual([])
  })

  it('明确接收后的再次操作是新的管理决定，并发点击只提交一次', async () => {
    const send = vi.fn().mockResolvedValue({ id: 'task', state: 'pending' })
    const requests = new ActionRequests(storage(), send); requests.useUser(1)
    await Promise.all([requests.submit(path, body), requests.submit(path, body)])
    expect(send).toHaveBeenCalledTimes(1)
    await requests.submit(path, body)
    expect(send).toHaveBeenCalledTimes(2)
    expect(send.mock.calls[0][1].request_id).not.toBe(send.mock.calls[1][1].request_id)
  })

  it.each([401, 403, 408, 429, 500, 503])('HTTP %i 保留未确认请求，恢复后继续确认原回执', async status => {
    const send = vi.fn().mockRejectedValueOnce(new TypeError('lost receipt'))
      .mockRejectedValueOnce(new ApiError(status, '暂时不能查询'))
      .mockResolvedValue({ id: 'task', state: 'pending' })
    const requests = new ActionRequests(storage(), send); requests.useUser(1)
    await expect(requests.submit(path, body)).rejects.toThrow()
    await expect(requests.submit(path, body)).rejects.toThrow()
    expect(requests.items).toHaveLength(1)
    await requests.retry(requests.items[0]!)
    expect(new Set(send.mock.calls.map(args => args[1].request_id)).size).toBe(1)
    expect(requests.items).toHaveLength(0)
  })

  it('明确校验拒绝后可修改输入重新提交，空回执仍需要确认', async () => {
    const send = vi.fn().mockRejectedValueOnce(new ApiError(422, '原因过长'))
      .mockResolvedValueOnce({}).mockResolvedValue({ id: 'task', state: 'pending' })
    const requests = new ActionRequests(storage(), send); requests.useUser(1)
    await expect(requests.submit(path, body)).rejects.toThrow('原因过长')
    expect(requests.items).toHaveLength(0)
    await expect(requests.submit(path, body)).rejects.toThrow('未收到有效的操作回执')
    await requests.retry(requests.items[0]!)
    expect(send.mock.calls[0][1].request_id).not.toBe(send.mock.calls[1][1].request_id)
    expect(send.mock.calls[1][1].request_id).toBe(send.mock.calls[2][1].request_id)
  })

  it('恢复请求按账号隔离', async () => {
    const saved = storage(), send = vi.fn().mockRejectedValue(new TypeError('offline'))
    const requests = new ActionRequests(saved, send); requests.useUser(1)
    await expect(requests.submit(path, body)).rejects.toThrow()
    const identifier = requests.items[0]!.body.request_id
    requests.useUser(2); expect(requests.items).toHaveLength(0)
    requests.useUser(1); expect(requests.items[0]?.body.request_id).toBe(identifier)
  })
})

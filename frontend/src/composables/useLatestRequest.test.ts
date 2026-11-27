import { createRenderer } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { useLatestRequest } from './useLatestRequest'
import { ApiError } from '../api'
import { workspaceKey, type Workspace } from '../workspace'

function mounted(onUnauthorized?: () => void) {
  let requests!: ReturnType<typeof useLatestRequest>
  const expireSession = vi.fn()
  const renderer = createRenderer<object, object>({
    createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
    parentNode: () => null, nextSibling: () => null,
  })
  const app = renderer.createApp({ setup() { requests = useLatestRequest(onUnauthorized); return () => null } })
  app.provide(workspaceKey, { expireSession } as unknown as Workspace)
  app.mount({})
  return { requests, expireSession, close: () => app.unmount() }
}

describe('只接收当前选择的查询', () => {
  it('启动查询保留原始失败，并支持根组件自己的会话失效处理', async () => {
    const unauthorized = vi.fn(), { requests, expireSession, close } = mounted(unauthorized)
    const failure = new ApiError(503, '群读取暂时失败')
    await expect(requests.run(async () => { throw failure }, () => {}, reason => { throw reason })).rejects.toBe(failure)
    expect(requests.busy.value).toBe(false)
    await requests.run(async () => { throw new ApiError(401, '会话失效') }, () => {})
    expect(unauthorized).toHaveBeenCalledOnce()
    expect(expireSession).not.toHaveBeenCalled()
    close()
  })
  it('迟到的结果和失败不能覆盖新选择，也不能保留旧的加载状态', async () => {
    const { requests, close } = mounted(), values: string[] = []
    let resolve!: (value: string) => void, reject!: (error: Error) => void
    const old = requests.run(() => new Promise<string>(done => { resolve = done }), value => values.push(value))
    await requests.run(async () => '当前账户', value => values.push(value))
    expect(requests.busy.value).toBe(false)
    resolve('旧账户'); await old
    expect(values).toEqual(['当前账户'])
    const failing = requests.run(() => new Promise<string>((_, fail) => { reject = fail }), value => values.push(value))
    await requests.run(async () => '更新结果', value => values.push(value))
    reject(new Error('旧请求失败')); await failing
    expect(requests.error.value).toBe('')
    expect(values).toEqual(['当前账户', '更新结果'])
    close()
  })

  it('关闭或离开页面后不会再接收查询结果', async () => {
    const { requests, close } = mounted(), values: string[] = []
    let resolve!: (value: string) => void
    const pending = requests.run(() => new Promise<string>(done => { resolve = done }), value => values.push(value))
    close(); resolve('离开后的数据'); await pending
    expect(values).toEqual([])
    expect(requests.busy.value).toBe(false)
  })

  it('旧请求的迟到登录错误不会清除新会话', async () => {
    const { requests, expireSession, close } = mounted()
    let reject!: (error: Error) => void
    const old = requests.run(() => new Promise<string>((_, fail) => { reject = fail }), () => {})
    await requests.run(async () => '当前会话成功', () => {})
    reject(new ApiError(401, '旧会话已结束')); await old
    expect(expireSession).not.toHaveBeenCalled()
    await requests.run(async () => { throw new ApiError(401, '当前会话已结束') }, () => {})
    expect(expireSession).toHaveBeenCalledOnce()
    close()
  })

  it('只有当前查询失败时才恢复分页，离开页面也不会触发失败回调', async () => {
    const { requests, expireSession, close } = mounted(), accept = vi.fn(), fallback = vi.fn()
    let reject!: (error: Error) => void
    const old = requests.run(() => new Promise<string>((_, fail) => { reject = fail }), accept, fallback)
    expect(await requests.run(async () => '当前结果', accept, fallback)).toBe(true)
    reject(new Error('旧请求失败'))
    expect(await old).toBe(false)
    expect(fallback).not.toHaveBeenCalled()
    expect(await requests.run(async () => { throw new Error('当前请求失败') }, accept, fallback)).toBe(false)
    expect(fallback).toHaveBeenCalledOnce()
    const leaving = requests.run(() => new Promise<string>((_, fail) => { reject = fail }), accept, fallback)
    close(); reject(new ApiError(401, '离开后的旧会话错误'))
    expect(await leaving).toBe(false)
    expect(fallback).toHaveBeenCalledOnce()
    expect(expireSession).not.toHaveBeenCalled()
    const afterUnmount = vi.fn(async () => '卸载后异步链中的下一次查询')
    expect(await requests.run(afterUnmount, accept, fallback)).toBe(false)
    expect(afterUnmount).not.toHaveBeenCalled()
  })
})

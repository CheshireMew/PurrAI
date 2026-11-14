import { effectScope, reactive } from 'vue'
import { describe, expect, it } from 'vitest'
import { usePreviewResult } from './usePreviewResult'

describe('预览与当前输入绑定', () => {
  it('修改深层草稿或样本后立即失效，改回原值也不接收旧请求', async () => {
    const scope = effectScope(), input = reactive({ draft: { value: '甲' }, sample: '消息' })
    const preview = scope.run(() => usePreviewResult<string>(() => input))!
    await preview.run(async () => '第一次结果')
    input.draft.value = '乙'
    expect(preview.result.value).toBeNull()
    let resolve!: (value: string) => void
    const pending = preview.run(() => new Promise<string>(done => { resolve = done }))
    input.sample = '修改消息'; input.sample = '消息'
    resolve('旧输入的结果'); await pending
    expect(preview.result.value).toBeNull()
    scope.stop()
  })
  it('只接收最新请求，忽略迟到的失败，离开后不再更新', async () => {
    const scope = effectScope(), preview = scope.run(() => usePreviewResult<string>(() => null))!
    let reject!: (value: Error) => void, resolve!: (value: string) => void
    const earlier = preview.run(() => new Promise<string>((_, fail) => { reject = fail }))
    await preview.run(async () => '最新结果')
    reject(new Error('旧请求失败')); await earlier
    expect(preview.result.value).toBe('最新结果')
    const pending = preview.run(() => new Promise<string>(done => { resolve = done }))
    scope.stop(); resolve('离开后的结果'); await pending
    expect(preview.result.value).toBeNull()
  })
})

import { createRenderer, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRefresh } from './useRefresh'

vi.mock('../navigation', () => ({ restoreNavigationScroll: vi.fn(async () => {}) }))

function mount(load: () => Promise<unknown>, background: () => Promise<unknown>, enabled = () => true) {
  const key = ref(0)
  const renderer = createRenderer<object, object>({
    createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
    parentNode: () => null, nextSibling: () => null,
  })
  const app = renderer.createApp({ setup() { useRefresh(load, () => key.value, enabled, background); return () => null } })
  app.mount({})
  return { key, close: () => app.unmount() }
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('后台查询完成后再安排刷新', () => {
  it('慢于五秒的结果仍能完成，等待期间不再创建查询，离开页面后停止', async () => {
    vi.useFakeTimers(); vi.stubGlobal('document', { visibilityState: 'visible' })
    let finish!: () => void
    const background = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
    const screen = mount(async () => {}, background)
    await vi.advanceTimersByTimeAsync(5000)
    expect(background).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(12000)
    expect(background).toHaveBeenCalledOnce()
    finish(); await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(4999)
    expect(background).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(background).toHaveBeenCalledTimes(2)
    screen.close(); finish()
    await vi.advanceTimersByTimeAsync(20000)
    expect(background).toHaveBeenCalledTimes(2)
  })

  it('首次读取或主动刷新期间不轮询；页面隐藏或不需要刷新时不查询', async () => {
    vi.useFakeTimers()
    const document = { visibilityState: 'visible' }
    vi.stubGlobal('document', document)
    let finish!: () => void, enabled = true
    const load = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
    const background = vi.fn(async () => {})
    const screen = mount(load, background, () => enabled)
    await vi.advanceTimersByTimeAsync(10000)
    expect(background).not.toHaveBeenCalled()
    finish(); await vi.advanceTimersByTimeAsync(5000)
    expect(background).toHaveBeenCalledOnce()
    screen.key.value++
    await vi.advanceTimersByTimeAsync(10000)
    expect(load).toHaveBeenCalledTimes(2)
    expect(background).toHaveBeenCalledOnce()
    finish(); document.visibilityState = 'hidden'
    await vi.advanceTimersByTimeAsync(5000)
    document.visibilityState = 'visible'; enabled = false
    await vi.advanceTimersByTimeAsync(5000)
    expect(background).toHaveBeenCalledOnce()
    enabled = true
    await vi.advanceTimersByTimeAsync(5000)
    expect(background).toHaveBeenCalledTimes(2)
    screen.close()
  })
})

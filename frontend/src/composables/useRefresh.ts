import { onMounted, onUnmounted, watch } from 'vue'
import { restoreNavigationScroll } from '../navigation'

export function useRefresh(load: () => Promise<unknown>, refreshKey: () => number,
                           enabled?: () => boolean, background = load) {
  let pending = 0, disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  async function run(work: () => Promise<unknown>) {
    pending++
    try { await work() } finally { pending-- }
  }
  const refresh = async () => { await run(load); if (!disposed) await restoreNavigationScroll(true) }
  function schedule() { if (enabled && !disposed) timer = setTimeout(poll, 5000) }
  async function poll() {
    try {
      if (!disposed && !pending && document.visibilityState !== 'hidden' && enabled?.()) await run(background)
    } finally { schedule() }
  }
  onMounted(() => { schedule(); return refresh() })
  watch(refreshKey, refresh)
  onUnmounted(() => { disposed = true; clearTimeout(timer) })
}

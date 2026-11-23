import { onMounted, onUnmounted, watch } from 'vue'
import { restoreNavigationScroll } from '../navigation'

export function useRefresh(load: () => Promise<unknown>, refreshKey: () => number,
                           enabled?: () => boolean, background = load) {
  const refresh = async () => { await load(); await restoreNavigationScroll(true) }
  onMounted(refresh)
  watch(refreshKey, refresh)
  const timer = enabled ? setInterval(() => { if (document.visibilityState !== 'hidden' && enabled()) void background() }, 5000) : undefined
  onUnmounted(() => clearInterval(timer))
}

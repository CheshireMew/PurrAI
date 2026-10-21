import { onMounted, onUnmounted, watch } from 'vue'

export function useRefresh(load: () => Promise<unknown>, refreshKey: () => number,
                           enabled?: () => boolean) {
  onMounted(load)
  watch(refreshKey, load)
  const timer = enabled ? setInterval(() => { if (enabled()) void load() }, 5000) : undefined
  onUnmounted(() => clearInterval(timer))
}

import { onScopeDispose, shallowRef, watch } from 'vue'
import { useRequestScope } from './useRequestScope'

// Invalidate even when an edit is later undone: an older request is still obsolete.
export function usePreviewResult<T>(source: () => unknown) {
  const result = shallowRef<T | null>(null)
  const scope = useRequestScope()
  function invalidate() { scope.cancel(); result.value = null }
  watch(source, invalidate, { deep: true, flush: 'sync' })
  onScopeDispose(invalidate)
  async function run(read: () => Promise<T>) {
    invalidate()
    const active = scope.begin()
    if (!active) return
    try {
      const value = await read()
      if (active()) result.value = value
    } catch (error) { if (active()) throw error }
  }
  return { result, run, invalidate }
}

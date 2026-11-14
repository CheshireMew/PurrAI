import { onScopeDispose, shallowRef, watch } from 'vue'

// Invalidate even when an edit is later undone: an older request is still obsolete.
export function usePreviewResult<T>(source: () => unknown) {
  const result = shallowRef<T | null>(null)
  let revision = 0
  function invalidate() { revision++; result.value = null }
  watch(source, invalidate, { deep: true, flush: 'sync' })
  onScopeDispose(invalidate)
  async function run(read: () => Promise<T>) {
    invalidate()
    const current = revision
    try {
      const value = await read()
      if (current === revision) result.value = value
    } catch (error) { if (current === revision) throw error }
  }
  return { result, run, invalidate }
}

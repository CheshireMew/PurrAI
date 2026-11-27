import { onScopeDispose, ref } from 'vue'

// This lifetime gate knows nothing about HTTP, forms or the application session.
export function useRequestScope() {
  const revision = ref(0), disposed = ref(false)
  function cancel() { revision.value++ }
  function begin() {
    if (disposed.value) return null
    const current = ++revision.value
    return () => !disposed.value && current === revision.value
  }
  onScopeDispose(() => { disposed.value = true; cancel() })
  return { begin, cancel }
}

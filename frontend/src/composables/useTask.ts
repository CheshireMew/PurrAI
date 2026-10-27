import { computed, inject, ref, shallowRef } from 'vue'
import { ApiError } from '../api'
import { workspaceKey } from '../workspace'

export function useTask(onUnauthorized?: () => void) {
  const workspace = inject(workspaceKey, null)
  const pending = shallowRef<{ current: () => boolean }[]>([]), error = ref('')
  async function safe(task: () => Promise<void>, current: () => boolean = () => true) {
    const operation = { current }
    pending.value = [...pending.value, operation]; error.value = ''
    try { await task(); return true }
    catch (reason) {
      if (current()) error.value = reason instanceof Error ? reason.message : '操作失败，请重试'
      if (reason instanceof ApiError && reason.status === 401) {
        if (onUnauthorized) onUnauthorized()
        else workspace?.expireSession()
      }
      return false
    } finally { pending.value = pending.value.filter(item => item !== operation) }
  }
  return { busy: computed(() => pending.value.some(item => item.current())), error, safe }
}

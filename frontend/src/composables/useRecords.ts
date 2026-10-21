import { onUnmounted, ref, shallowRef } from 'vue'
import { api } from '../api'
import type { Page } from '../types'
import { useTask } from './useTask'

export function recordQuery(page: number, state: string, user: string, dates: [string, string] | null) {
  const query = new URLSearchParams({ page_number: String(page), state })
  if (user.trim()) query.set('user_id', user.trim())
  if (dates) {
    query.set('after', String(Date.parse(dates[0].replace(' ', 'T') + '+08:00') / 1000))
    query.set('before', String(Date.parse(dates[1].replace(' ', 'T') + '+08:00') / 1000))
  }
  return query
}

export function useRecords<T>(groupId: () => number, section: string) {
  const task = useTask(), rows = shallowRef<T[]>([]), total = ref(0), pagination = ref(1)
  const statusFilter = ref(''), userFilter = ref(''), dates = ref<[string, string] | null>(null)
  let version = 0
  async function load() {
    const current = ++version
    await task.safe(async () => {
      const query = recordQuery(pagination.value, statusFilter.value, userFilter.value, dates.value)
      const result = await api<T[] | Page<T>>(`/api/groups/${groupId()}/${section}?${query}`)
      if (current === version) {
        rows.value = Array.isArray(result) ? result : result.items
        total.value = Array.isArray(result) ? result.length : result.total
      }
    }, () => current === version)
  }
  async function filters() { pagination.value = 1; await load() }
  onUnmounted(() => { version++ })
  return { ...task, rows, total, pagination, statusFilter, userFilter, dates, load, filters }
}

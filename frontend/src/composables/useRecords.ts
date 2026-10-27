import { ref, shallowRef } from 'vue'
import { api } from '../api'
import type { Page } from '../types'
import { applyBeijingDates } from '../query'
import { useLatestRequest } from './useLatestRequest'

export function recordQuery(page: number, state: string, user: string, dates: [string, string] | null) {
  const query = new URLSearchParams({ page_number: String(page), state })
  if (user.trim()) query.set('user_id', user.trim())
  return applyBeijingDates(query, dates)
}

export function useRecords<T>(groupId: () => number, section: string) {
  const task = useLatestRequest(), rows = shallowRef<T[]>([]), total = ref(0), pagination = ref(1)
  const statusFilter = ref(''), userFilter = ref(''), dates = ref<[string, string] | null>(null)
  async function load() {
    await task.run(async () => {
      const query = recordQuery(pagination.value, statusFilter.value, userFilter.value, dates.value)
      return api<T[] | Page<T>>(`/api/groups/${groupId()}/${section}?${query}`)
    }, result => {
      rows.value = Array.isArray(result) ? result : result.items
      total.value = Array.isArray(result) ? result.length : result.total
    })
  }
  async function filters() { pagination.value = 1; await load() }
  return { ...task, rows, total, pagination, statusFilter, userFilter, dates, load, filters }
}

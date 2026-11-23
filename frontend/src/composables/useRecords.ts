import { computed, ref, shallowRef, watch } from 'vue'
import { api } from '../api'
import type { Page } from '../types'
import { applyBeijingDates } from '../query'
import { useLatestRequest } from './useLatestRequest'
import { route, savedFilters, saveFilters } from '../navigation'

export function recordQuery(page: number, state: string, user: string, dates: [string, string] | null) {
  const query = new URLSearchParams({ page_number: String(page), state })
  if (user.trim()) query.set('user_id', user.trim())
  return applyBeijingDates(query, dates)
}

export function useRecords<T>(groupId: () => number, section: string, extra?: () => Record<string, string>, restoreExtra?: (values: Record<string, string>) => void) {
  const task = useLatestRequest(), rows = shallowRef<T[]>([]), total = ref(0), pagination = ref(1)
  const statusFilter = ref(''), userFilter = ref(''), dates = ref<[string, string] | null>(null)
  let applied = {state: '', user: '', dates: null as [string, string] | null, extra: {} as Record<string, string>}
  let requested = applied, acceptedPage = 1, loadVersion = 0
  const loaded = ref(false), queryFailed = ref(false)
  let written = route.value.params.get('filter') || ''
  const restored = savedFilters(section)
  function restore() {
    const data = savedFilters(section)
    pagination.value = Number.isInteger(data.page) && data.page > 0 ? data.page : 1
    statusFilter.value = typeof data.state === 'string' ? data.state : ''
    userFilter.value = typeof data.user === 'string' ? data.user : ''
    dates.value = Array.isArray(data.dates) && data.dates.length === 2 && data.dates.every((item: unknown) => typeof item === 'string') ? [data.dates[0], data.dates[1]] : null
    const restoredExtra = Object.fromEntries(Object.keys(extra?.() || {}).map(key => [key,
      typeof data.extra?.[key] === 'string' ? data.extra[key] : key === 'manual_only' ? 'false' : '']))
    requested = {state: statusFilter.value, user: userFilter.value, dates: dates.value, extra: restoredExtra}
    restoreExtra?.(restoredExtra)
  }
  restore()
  async function load(change: boolean | number = false) {
    const version = ++loadVersion, candidate = requested, number = pagination.value
    queryFailed.value = false
    const success = await task.run(async () => {
      const query = recordQuery(number, candidate.state, candidate.user, candidate.dates)
      for (const [key, value] of Object.entries(candidate.extra)) if (value) query.set(key, value)
      return api<T[] | Page<T>>(`/api/groups/${groupId()}/${section}?${query}`)
    }, result => {
      rows.value = Array.isArray(result) ? result : result.items
      total.value = Array.isArray(result) ? result.length : result.total
      applied = candidate; acceptedPage = number; loaded.value = true
      const stored = {section, page: number, ...candidate}
      written = JSON.stringify(stored); saveFilters(stored, !change)
    })
    if (!success && version === loadVersion) {
      queryFailed.value = true
      if (loaded.value) { pagination.value = acceptedPage; requested = applied }
    }
  }
  async function filters() {
    requested = {state: statusFilter.value, user: userFilter.value, dates: dates.value ? [...dates.value] : null, extra: {...extra?.()}}
    pagination.value = 1; await load(true)
  }
  watch(() => route.value.params.get('filter') || '', value => {
    if (value !== written) { written = value; restore(); void load() }
  })
  const error = computed({ get: () => task.error.value && queryFailed.value && loaded.value
    ? `${task.error.value}；仍显示上次成功查询的结果` : task.error.value,
    set: value => { task.error.value = value } })
  return { ...task, error, loaded, queryFailed, rows, total, pagination, statusFilter, userFilter, dates, load, filters, restored, appliedFilters: () => applied }
}

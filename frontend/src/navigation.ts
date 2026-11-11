import { shallowRef } from 'vue'

export function parseNavigation(hash: string) {
  const [path, query = ''] = hash.replace(/^#\/?/, '').split('?', 2)
  const params = new URLSearchParams(query)
  const number = Number(params.get('group'))
  return { page: path || 'overview', group: Number.isSafeInteger(number) && number < 0 ? number : null, params }
}
export const route = shallowRef(parseNavigation(typeof location === 'undefined' ? '' : location.hash))

export function navigate(page: string, group: number | null, replace = false, additions?: Record<string, string | null>) {
  const params = route.value.page === page ? new URLSearchParams(route.value.params) : new URLSearchParams()
  if (group !== null) params.set('group', String(group)); else params.delete('group')
  for (const [key, value] of Object.entries(additions || {})) {
    if (value === null) params.delete(key); else params.set(key, value)
  }
  const hash = `#/${page}${params.size ? '?' + params.toString() : ''}`
  if (typeof location !== 'undefined' && hash !== location.hash) {
    history[replace ? 'replaceState' : 'pushState']({}, '', location.pathname + location.search + hash)
  }
  if (route.value.page !== page || route.value.group !== group || params.toString() !== route.value.params.toString()) {
    route.value = {page, group, params}
  }
}
export function observeNavigation() {
  const restore = () => { if (!location.hash || location.hash.startsWith('#/')) route.value = parseNavigation(location.hash) }
  window.addEventListener('popstate', restore)
  window.addEventListener('hashchange', restore)
  return () => { window.removeEventListener('popstate', restore); window.removeEventListener('hashchange', restore) }
}
export function savedFilters(section: string): Record<string, any> {
  try {
    const value = JSON.parse(route.value.params.get('filter') || '{}')
    return value && typeof value === 'object' && value.section === section ? value : {}
  } catch { return {} }
}
export function saveFilters(value: Record<string, unknown>, replace = false) {
  navigate(route.value.page, route.value.group, replace, {filter: JSON.stringify(value)})
}

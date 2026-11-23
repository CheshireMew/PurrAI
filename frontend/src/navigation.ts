import { nextTick, shallowRef } from 'vue'

export function parseNavigation(hash: string) {
  const [path, query = ''] = hash.replace(/^#\/?/, '').split('?', 2)
  const params = new URLSearchParams(query)
  const number = Number(params.get('group'))
  return { page: path || 'overview', group: Number.isSafeInteger(number) && number < 0 ? number : null, params }
}
export const route = shallowRef(parseNavigation(typeof location === 'undefined' ? '' : location.hash))

let position = 0
let committedUrl = ''
let pendingScroll: { url: string; y: number } | undefined
const url = () => location.pathname + location.search + location.hash
function rememberScroll() {
  if (pendingScroll || url() !== committedUrl) return
  history.replaceState({ ...history.state, purraiPosition: position, purraiScroll: window.scrollY }, '', url())
}
function scrollAfterRender(y: number) {
  pendingScroll = { url: url(), y }
  void nextTick(() => restoreNavigationScroll())
}
export async function restoreNavigationScroll(loaded = false) {
  await nextTick()
  if (!pendingScroll || pendingScroll.url !== url()) return
  const maximum = Math.max(0, document.documentElement.scrollHeight - innerHeight)
  if (!loaded && pendingScroll.y > maximum) return
  const y = Math.min(pendingScroll.y, maximum)
  pendingScroll = undefined
  window.scrollTo({ top: y, behavior: 'instant' })
  rememberScroll()
}

export function navigate(page: string, group: number | null, replace = false, additions?: Record<string, string | null>) {
  const changedPage = route.value.page !== page || route.value.group !== group
  const params = route.value.page === page ? new URLSearchParams(route.value.params) : new URLSearchParams()
  if (group !== null) params.set('group', String(group)); else params.delete('group')
  for (const [key, value] of Object.entries(additions || {})) {
    if (value === null) params.delete(key); else params.set(key, value)
  }
  const hash = `#/${page}${params.size ? '?' + params.toString() : ''}`
  if (typeof location !== 'undefined' && hash !== location.hash) {
    const previousUrl = url()
    rememberScroll()
    if (!replace) position++
    history[replace ? 'replaceState' : 'pushState']({ ...history.state, purraiPosition: position,
      purraiScroll: changedPage ? 0 : window.scrollY }, '', location.pathname + location.search + hash)
    committedUrl = url()
    if (!changedPage && pendingScroll?.url === previousUrl) pendingScroll.url = committedUrl
  }
  if (route.value.page !== page || route.value.group !== group || params.toString() !== route.value.params.toString()) {
    route.value = {page, group, params}
  }
  if (typeof window !== 'undefined' && changedPage) scrollAfterRender(0)
}
export function observeNavigation(beforeLeave: () => Promise<boolean> = async () => true,
                                  needsConfirmation: () => boolean = () => false) {
  position = history.state?.purraiPosition ?? 0
  committedUrl = url()
  history.replaceState({ ...history.state, purraiPosition: position, purraiScroll: window.scrollY }, '', committedUrl)
  const previousRestoration = history.scrollRestoration
  history.scrollRestoration = 'manual'
  let rollingBack: { origin: number; delta: number } | undefined
  let confirming = false, replay = false
  const commit = () => {
    position = history.state?.purraiPosition ?? position + 1
    committedUrl = url()
    const y = history.state?.purraiScroll ?? 0
    history.replaceState({ ...history.state, purraiPosition: position, purraiScroll: y }, '', committedUrl)
    route.value = parseNavigation(location.hash)
    scrollAfterRender(y)
  }
  const restore = () => {
    if (rollingBack) {
      if (history.state?.purraiPosition !== rollingBack.origin) return
      const target = rollingBack
      rollingBack = undefined
      confirming = true
      void beforeLeave().then(allowed => {
        confirming = false
        if (allowed) { replay = true; history.go(target.delta) }
      })
      return
    }
    if (confirming || url() === committedUrl) return
    if (!location.hash || location.hash.startsWith('#/')) {
      if (replay) { replay = false; commit(); return }
      if (needsConfirmation()) {
        // Return to the unchanged entry while asking. Replacing the target entry here
        // would destroy Back/Forward history when the user chooses to keep editing.
        const target = history.state?.purraiPosition ?? position + 1
        const delta = target - position
        if (!delta) return
        rollingBack = { origin: position, delta }
        history.go(-delta)
      } else { void beforeLeave(); commit() }
    }
  }
  window.addEventListener('popstate', restore)
  window.addEventListener('hashchange', restore)
  window.addEventListener('scroll', rememberScroll, { passive: true })
  return () => {
    window.removeEventListener('popstate', restore); window.removeEventListener('hashchange', restore)
    window.removeEventListener('scroll', rememberScroll); history.scrollRestoration = previousRestoration
  }
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

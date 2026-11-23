import { ElMessage, ElMessageBox } from 'element-plus'

export interface DraftGuard {
  active: () => boolean
  dirty: () => boolean
  busy: () => boolean
  close: () => void
}

const guards = new Set<DraftGuard>()
let confirmation: Promise<boolean> | undefined

export function registerDraft(guard: DraftGuard) {
  guards.add(guard)
  return () => { guards.delete(guard) }
}

export function hasPendingDrafts() {
  return [...guards].some(guard => guard.active() && (guard.dirty() || guard.busy()))
}

export async function confirmDrafts(items: DraftGuard[]) {
  const active = items.filter(guard => guard.active())
  if (active.some(guard => guard.busy())) {
    ElMessage.info('正在处理，请等待完成后再离开')
    return false
  }
  if (!active.some(guard => guard.dirty())) return true
  if (!confirmation) confirmation = ElMessageBox.confirm('尚有未保存的修改，放弃后无法恢复。', '保留当前草稿？', {
    confirmButtonText: '放弃修改', cancelButtonText: '继续编辑', type: 'warning',
    closeOnClickModal: false, closeOnHashChange: false, autofocus: false,
  }).then(() => true, () => false).finally(() => { confirmation = undefined })
  return confirmation
}

export async function confirmNavigation() {
  const active = [...guards].filter(guard => guard.active())
  if (!await confirmDrafts(active)) return false
  for (const guard of active) guard.close()
  return true
}

export function protectWindowClose(event: BeforeUnloadEvent) {
  if (!hasPendingDrafts()) return
  event.preventDefault()
  event.returnValue = ''
}

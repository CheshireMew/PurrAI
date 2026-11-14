import { ElMessageBox } from 'element-plus'

export function useDraftClose(dirty: () => boolean, close: () => void, busy: () => boolean = () => false) {
  let pending: Promise<boolean> | undefined
  async function confirmDiscard() {
    if (busy()) return false
    if (!dirty()) return true
    if (!pending) pending = ElMessageBox.confirm('尚有未保存的修改，放弃后无法恢复。', '保留当前草稿？', {
      confirmButtonText: '放弃修改', cancelButtonText: '继续编辑', type: 'warning',
      closeOnClickModal: false, autofocus: false,
    }).then(() => true, () => false).finally(() => { pending = undefined })
    return pending
  }
  async function requestClose() { if (await confirmDiscard()) close() }
  return { requestClose, confirmDiscard }
}

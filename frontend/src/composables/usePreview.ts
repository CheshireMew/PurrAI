import { computed, reactive } from 'vue'
import { usePreviewResult } from './usePreviewResult'
import { post } from '../api'
import type { PreviewInput, PreviewResult, Rule } from '../types'

export function previewMessages(result: PreviewResult) {
  return [...result.reasons, ...(result.ban_member ? ['将踢出并禁止重新加入'] : result.kick_member ? ['将踢出成员'] : []),
    ...(result.mute_member ? ['将禁言成员'] : []), ...(result.delete_message ? ['将删除消息'] : []), ...(result.notes || [])]
}
export function usePreview(groupId: () => number, context: () => unknown = () => null) {
  const input = reactive<PreviewInput>({ text: '', sender_name: '', newcomer: false, contact: false, forwarded: false, repeats: 0, similar: 0, burst: 0 })
  const { result, run, invalidate } = usePreviewResult<PreviewResult>(() => [groupId(), input, context()])
  const reasons = computed(() => result.value === null ? null : previewMessages(result.value))
  async function preview(rules?: Rule[], draft?: Rule) {
    await run(() => post(`/api/groups/${groupId()}/rules/preview`, { ...input, ...(rules ? { rules } : {}), ...(draft ? { draft: { ...draft, id: draft.id || '' } } : {}) }))
  }
  return { input, reasons, preview, invalidate }
}

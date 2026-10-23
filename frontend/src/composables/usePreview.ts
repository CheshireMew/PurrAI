import { reactive, ref } from 'vue'
import { post } from '../api'
import type { PreviewInput, PreviewResult, Rule } from '../types'

export function previewMessages(result: PreviewResult) {
  return [...result.reasons, ...(result.ban_member ? ['将踢出并禁止重新加入'] : result.kick_member ? ['将踢出成员'] : []),
    ...(result.mute_member ? ['将禁言成员'] : []), ...(result.delete_message ? ['将删除消息'] : []), ...(result.notes || [])]
}
export function usePreview(groupId: () => number) {
  const input = reactive<PreviewInput>({ text: '', sender_name: '', newcomer: false, contact: false, forwarded: false, repeats: 0, similar: 0, burst: 0 })
  const reasons = ref<string[] | null>(null)
  async function preview(rules?: Rule[]) {
    const result = await post(`/api/groups/${groupId()}/rules/preview`, { ...input, ...(rules ? { rules } : {}) }) as PreviewResult
    reasons.value = previewMessages(result)
  }
  return { input, reasons, preview }
}

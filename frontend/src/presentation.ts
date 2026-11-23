import type { TagProps } from 'element-plus'

const restrictionNames: Record<string, string> = {
  moderation: '管理处置禁言', verification: '入群验证禁言', verification_failed: '验证失败禁言',
  emoji_status: '状态表情违规禁言', ai_ad: 'AI 广告审核禁言', rule: '规则处罚禁言',
  sample_ad: '广告样本匹配禁言', profile: '昵称或简介违规禁言', identity: '身份信息违规禁言', ban: '封禁',
}
export function restrictionName(kind: string) { return restrictionNames[kind] || '限制发言' }
export function groupTypeName(type: string) {
  return ({ supergroup: '超级群', group: '普通群', channel: '频道', private: '私聊' } as Record<string, string>)[type] || '群聊'
}

export function statusTone(state: string): TagProps['type'] {
  if (['active', 'passed', 'succeeded', 'deleted'].includes(state)) return 'success'
  if (['error', 'failed'].includes(state)) return 'danger'
  if (['pending', 'muting', 'releasing', 'removing', 'failing'].includes(state)) return 'warning'
  return 'info'
}

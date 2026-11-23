import type { GroupSettings } from './types'

// One display vocabulary for editable choices and their inheritance comparison.
export const settingOptions: Partial<Record<keyof GroupSettings, Record<string, string>>> = {
  ai_ad_penalty: { delete: '只删除广告消息', permanent_mute: '达到累计次数后永久禁言' },
  channel_ad_penalty: { delete: '只删除广告消息', ban: '删除广告并封禁发言频道' },
  external_channel_penalty: { delete: '只删消息', permanent_mute: '删消息并永久禁言', ban: '删消息并踢出封禁' },
  learned_target_penalty: { delete: '只删消息', permanent_mute: '删消息并永久禁言', ban: '删消息并永久封禁' },
  verification_method: { image: '图片验证', text: '文字算术验证', channel: '订阅频道自动验证' },
  verification_failure_action: { permanent_mute: '永久禁言（留在群里）', kick: '踢出群聊（允许重新加入）' },
  identity_action: { kick: '踢出群聊（允许重新加入）', ban: '永久封禁' },
}
export function settingValue(key: keyof GroupSettings, value: unknown): string {
  const choice = settingOptions[key]?.[String(value)]
  if (choice) return choice
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  if (Array.isArray(value)) return value.join('、') || '空'
  return String(value ?? '未设置')
}

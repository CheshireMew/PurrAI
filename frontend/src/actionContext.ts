import type { Group } from './types'

export type ActionContext = {
  group_id: number; group_title: string; action: string; user_id?: number; user_name?: string
  duration_seconds?: number; member_count?: number
}
export function actionContext(group: Pick<Group, 'id' | 'title' | 'settings'>, action: string, userId?: number, userName?: string, duration?: number): ActionContext {
  return { group_id: group.id, group_title: group.title, action, user_id: userId, user_name: userName,
    duration_seconds: duration ?? (action === 'mute' ? group.settings.mute_seconds : action === 'ban' ? group.settings.failure_ban_seconds : undefined) }
}
export function describeAction(context: ActionContext, operations: Record<string, string>) {
  return [ `${context.group_title}（${context.group_id}）`,
    context.user_id !== undefined ? `成员 ${context.user_name ? context.user_name + ' · ' : ''}${context.user_id}` : '',
    operations[context.action] || context.action,
    context.duration_seconds !== undefined ? `本次 ${context.duration_seconds} 秒` : '',
    context.member_count !== undefined ? `${context.member_count} 位成员` : '',
  ].filter(Boolean).join(' · ')
}

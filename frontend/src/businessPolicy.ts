export type Expression = { op: string; [key: string]: any }
export type TextStep = { op: string; pattern?: string; value?: string | Record<string, string> }
export type BusinessRule = { id: string; title: string; reason: string; enabled: boolean; action: string; condition: Expression; reason_field?: string; advertiser_cooldown?: boolean }
export type BusinessStage = { id: string; title: string; enabled: boolean; first_match: boolean; before: string[]; condition: Expression; rules: BusinessRule[] }
export type RecognitionEntry = { id: string; title: string; enabled: boolean; pattern?: string; value?: string }
export type RecognitionPolicy = Record<'utility_patterns' | 'candidate_patterns' | 'other_bot_patterns' | 'output_patterns' | 'phone_prefixes' | 'shortener_domains', RecognitionEntry[]>
export type ReasonEntry = { id: string; title: string; enabled: boolean; mode: 'exact' | 'prefix' | 'contains'; value: string }
export type ReasonPolicy = Record<'ban' | 'contact' | 'spam' | 'repeat' | 'advertising_stats' | 'spam_stats' | 'ai_history' | 'target_learning' | 'media_gate', ReasonEntry[]>
export type BusinessDocument = {
  format: number
  views: { id: string; title: string; source: string; steps: TextStep[] }[]
  conditions: { id: string; title: string; enabled: boolean; condition: Expression }[]
  stages: BusinessStage[]
  reason_policy?: ReasonPolicy
  recognition?: RecognitionPolicy
  ai_review_instructions?: string
}
export type BusinessPolicy = { document: BusinessDocument; version: string }
export const operations: Record<string, string> = {
  all: '全部满足', any: '任意满足', not: '不满足', match: '文本匹配', ref: '引用公共条件',
  each: '逐项或分句检查', empty: '为空', truthy: '为真', equals: '相等', always: '始终满足',
}
export const actions: Record<string, string> = { delete: '只删除消息', mute: '删除并永久禁言', ban: '删除并永久封禁', exclude: '仅排除聊天' }
export function expression(op = 'match'): Expression {
  if (op === 'all' || op === 'any') return { op, items: [expression()] }
  if (op === 'not') return { op, item: expression() }
  if (op === 'match') return { op, input: 'text', pattern: '', mode: 'search' }
  if (op === 'ref') return { op, ref: '' }
  if (op === 'each') return { op, input: 'text', split: '[。！？!?；;，,\\n]', steps: [], condition: expression() }
  if (op === 'equals') return { op, input: 'text', value: '' }
  return op === 'always' ? { op } : { op, input: 'text' }
}
export function textFields(document: BusinessDocument) {
  return [
    { id: 'text', title: '原始文本' }, ...document.views.map(view => ({ id: view.id, title: view.title })),
    { id: 'source_titles', title: '转发来源标题列表' }, { id: 'direct', title: '用户直接提问' },
    { id: 'urls', title: '消息包含链接' },
    { id: '$item', title: '当前分句或列表项' }, { id: '$match', title: '当前匹配片段' },
    { id: '$prefix', title: '匹配前的文字' }, { id: '$start', title: '匹配开始位置' },
  ]
}

export const reasonCategoryLabels: Record<string, string> = { ban: '旧记录的封禁判定', contact: '旧记录的联系人判定', spam: '刷屏处置', repeat: '重复消息清理', advertising_stats: '广告统计', spam_stats: '刷屏统计', ai_history: '历史 AI 广告样本导入', target_learning: '推广目标学习', media_gate: '发图门槛旧任务识别' }

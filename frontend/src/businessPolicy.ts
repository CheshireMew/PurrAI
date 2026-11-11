export type Expression = { op: string; [key: string]: any }
export type TextStep = { op: string; pattern?: string; value?: string | Record<string, string> }
export type BusinessRule = { id: string; title: string; reason: string; enabled: boolean; action: string; condition: Expression; reason_field?: string }
export type BusinessStage = { id: string; title: string; enabled: boolean; first_match: boolean; before: string[]; condition: Expression; rules: BusinessRule[] }
export type BusinessDocument = {
  format: number
  views: { id: string; title: string; source: string; steps: TextStep[] }[]
  conditions: { id: string; title: string; enabled: boolean; condition: Expression }[]
  stages: BusinessStage[]
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

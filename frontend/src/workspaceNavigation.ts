export const workspaceSections = [
  { id: 'overview', name: '总览', page: 'overview' },
  { id: 'groups', name: '群设置', page: 'groups' },
  { id: 'members', name: '成员管理', page: 'members' },
  { id: 'interaction', name: '群互动', page: 'points' },
  { id: 'records', name: '记录', page: 'violations' },
  { id: 'system', name: '系统维护', page: 'system', rootOnly: true },
]

interface WorkspacePage {
  id: string
  name: string
  section: string
  requiresGroup?: boolean
  rootOnly?: boolean
  secondary?: boolean
}

export const workspacePages: WorkspacePage[] = [
  { id: 'overview', name: '总览', section: 'overview' },
  { id: 'groups', name: '基本设置', section: 'groups' },
  { id: 'rules', name: '防广告规则', section: 'groups', requiresGroup: true },
  { id: 'members', name: '成员查询', section: 'members', requiresGroup: true },
  { id: 'verifications', name: '入群验证', section: 'members', requiresGroup: true },
  { id: 'whitelist', name: '白名单', section: 'members', requiresGroup: true },
  { id: 'points', name: '积分管理', section: 'interaction', requiresGroup: true },
  { id: 'lotteries', name: '抽奖管理', section: 'interaction', requiresGroup: true },
  { id: 'auto-replies', name: '自动回复', section: 'interaction', requiresGroup: true },
  { id: 'violations', name: '违规记录', section: 'records', requiresGroup: true },
  { id: 'messages', name: '消息归档', section: 'records', requiresGroup: true },
  { id: 'operations', name: '操作记录', section: 'records', requiresGroup: true },
  { id: 'chat', name: '聊天排查', section: 'records', requiresGroup: true, secondary: true },
  { id: 'system', name: '运行状态', section: 'system', rootOnly: true },
  { id: 'admins', name: '权限说明', section: 'system', rootOnly: true },
]

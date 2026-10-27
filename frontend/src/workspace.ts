import { inject, type InjectionKey, type Ref } from 'vue'
import type { ActionTracker } from './actionTracker'
import type { ActionBody, ActionRequest, ActionRequests } from './actionRequests'
import type { AdminCatalog, Group, User } from './types'

export interface Workspace {
  me: Ref<User | null>
  groups: Ref<Group[]>
  catalog: Ref<AdminCatalog | null>
  actionTracker: Pick<ActionTracker, 'items' | 'accept' | 'refresh'>
  actionRequests: Pick<ActionRequests, 'items'>
  submitAction(path: string, body: ActionBody): Promise<void>
  retryAction(request: ActionRequest): Promise<void>
  reloadGroups(): Promise<void>
  expireSession(): void
  confirmAction(path: string, action: string, title: string): Promise<void>
}
export const workspaceKey: InjectionKey<Workspace> = Symbol('workspace')
export function useWorkspace() {
  const value = inject(workspaceKey)
  if (!value) throw new Error('管理工作台尚未初始化')
  return value
}

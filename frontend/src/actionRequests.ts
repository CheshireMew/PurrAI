import { post } from './api'
import type { ActionRow, BatchReceipt } from './actionTracker'
import type { ActionContext } from './actionContext'
import { browserStorage, PendingRequests, type RequestStorage } from './pendingRequests'

export type ActionBody = { action: string; reason: string; duration_seconds?: number; verification_ids?: string[]; snapshot?: string }
export type ActionRequest = { path: string; body: ActionBody & { request_id: string }; error: string; context?: ActionContext }
export type AcceptedAction = { receipt: ActionRow | BatchReceipt; reason: string; context?: ActionContext }
const uncertain = '提交结果暂未确认，请点击“确认提交结果”；系统会沿用原请求，避免重复操作'
export class ActionRequests extends PendingRequests<ActionRequest, AcceptedAction> {
  constructor(storage: RequestStorage = browserStorage(), send = post) {
    super({
      namespace: 'action-requests', uncertain,
      identify: request => request.body.request_id,
      valid: (item): item is ActionRequest => typeof item?.path === 'string' && item.path.startsWith('/api/')
        && typeof item?.body?.request_id === 'string' && typeof item.body.action === 'string'
        && typeof item.body.reason === 'string',
      perform: async request => {
        const receipt = await send(request.path, request.body) as ActionRow | BatchReceipt
        if (!(typeof (receipt as ActionRow)?.id === 'string' && typeof (receipt as ActionRow).state === 'string') &&
            !(typeof (receipt as BatchReceipt)?.batch_id === 'string' && Array.isArray((receipt as BatchReceipt).action_ids))) {
          throw new Error('未收到有效的操作回执')
        }
        return { receipt, reason: request.body.reason, ...(request.context ? {context: request.context} : {}) }
      },
    }, storage)
  }

  submit(path: string, body: ActionBody, context?: ActionContext): Promise<AcceptedAction> {
    let request = this.items.find(item => item.path === path && item.body.action === body.action)
    if (!request) {
      request = this.remember({ path, body: { ...body, request_id: crypto.randomUUID() }, error: '', ...(context ? { context } : {}) })
    }
    return this.retry(request)
  }
}

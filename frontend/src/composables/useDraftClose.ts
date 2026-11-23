import { onScopeDispose } from 'vue'
import { confirmDrafts, registerDraft } from '../drafts'

export function useDraftClose(dirty: () => boolean, close: () => void, busy: () => boolean = () => false,
                              active: () => boolean = () => true) {
  const guard = { active, dirty, busy, close }
  onScopeDispose(registerDraft(guard))
  const confirmDiscard = () => confirmDrafts([guard])
  async function requestClose() { if (await confirmDiscard()) close() }
  return { requestClose, confirmDiscard }
}

import type { TagProps } from 'element-plus'

export function statusTone(state: string): TagProps['type'] {
  if (['active', 'passed', 'succeeded', 'deleted'].includes(state)) return 'success'
  if (['error', 'failed'].includes(state)) return 'danger'
  if (['pending', 'muting', 'releasing', 'removing', 'failing'].includes(state)) return 'warning'
  return 'info'
}

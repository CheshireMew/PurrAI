import { useTask } from './useTask'
import { useRequestScope } from './useRequestScope'

// A superseded read cannot replace data, errors or loading state from the latest read.
export function useLatestRequest(onUnauthorized?: () => void) {
  const task = useTask(onUnauthorized), scope = useRequestScope()
  async function run<T>(read: () => Promise<T>, accept: (value: T) => void, reject?: (reason: unknown) => void) {
    const active = scope.begin()
    if (!active) return false
    const success = await task.safe(async () => {
      const result = await read()
      if (active()) accept(result)
    }, active, reject)
    return success && active()
  }
  return { ...task, run, cancel: scope.cancel }
}

import { onUnmounted, ref } from 'vue'
import { useTask } from './useTask'

// A superseded read cannot replace data, errors or loading state from the latest read.
export function useLatestRequest() {
  const task = useTask(), version = ref(0)
  function cancel() { version.value++ }
  async function run<T>(read: () => Promise<T>, accept: (value: T) => void) {
    const current = ++version.value
    return task.safe(async () => {
      const result = await read()
      if (current === version.value) accept(result)
    }, () => current === version.value)
  }
  onUnmounted(cancel)
  return { ...task, run, cancel }
}

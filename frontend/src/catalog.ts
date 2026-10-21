import { shallowRef } from 'vue'
import type { AdminCatalog } from './types'

export const catalog = shallowRef<AdminCatalog | null>(null)
export function configureCatalog(value: AdminCatalog) { catalog.value = value }
export function stateLabel(state: string) { return catalog.value?.states[state] || state }

<script setup lang="ts">
import { computed } from 'vue'
import type { DateRange } from '../query'

const model = defineModel<DateRange>({ required: true })
function update(index: number, value: string | null) {
  const next: [string, string] = [...(model.value || ['', ''])]
  next[index] = value || ''
  model.value = next.some(Boolean) ? next : null
}
const start = computed({ get: () => model.value?.[0] || '', set: value => update(0, value) })
const end = computed({ get: () => model.value?.[1] || '', set: value => update(1, value) })
</script>

<template>
<div class="date-range-filter" role="group" aria-label="北京时间范围">
  <el-date-picker v-model="start" type="datetime" value-format="YYYY-MM-DD HH:mm:ss" placeholder="开始（北京时间）" aria-label="开始（北京时间）" popper-class="date-filter-popup"/>
  <el-date-picker v-model="end" type="datetime" value-format="YYYY-MM-DD HH:mm:ss" placeholder="结束（北京时间）" aria-label="结束（北京时间）" popper-class="date-filter-popup"/>
</div>
</template>

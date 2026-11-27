<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage, type TableInstance } from 'element-plus'
import type { Verification, Operation, Page } from '../types'
import { useRecords } from '../composables/useRecords'
import { combineTasks } from '../composables/useTask'
import { useLatestRequest } from '../composables/useLatestRequest'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'
import { statusTone } from '../presentation'
import BatchReleaseDialog from '../components/BatchReleaseDialog.vue'
import DateRangeFilter from '../components/DateRangeFilter.vue'
import { actionContext } from '../actionContext'
import { applyBeijingDates } from '../query'

const props = defineProps<{ groupId: number; refreshKey: number }>()
const selected = computed(() => props.groupId)
const { catalog, groups, confirmAction: submitAction } = useWorkspace()
const operationNames = computed(() => catalog.value!.operations)
const { rows, total, pagination, statusFilter, userFilter, dates, busy: recordsBusy, error: recordsError, load: loadRecords, filters, appliedFilters } = useRecords<Verification>(() => props.groupId, 'verifications')
const historyReads = useLatestRequest()
const { busy, error } = combineTasks({ busy: recordsBusy, error: recordsError }, historyReads)
const history = ref<Operation[]>([])
const chosen = ref<Verification[]>([]), batchSelection = ref<Record<string, unknown> | null>(null)
const table = ref<TableInstance>(), polling = ref(false)
watch(rows, async current => {
  await nextTick()
  const available = new Set(current.filter(row => catalog.value!.active_verifications.includes(row.state)).map(row => row.id))
  const removed = chosen.value.filter(row => !available.has(row.id))
  for (const row of removed) table.value?.toggleRowSelection(row, false)
  if (removed.length) ElMessage.info(`${removed.length} 位成员已结束验证或不在当前列表中，已从选择中移除`)
})
function previewBatch(mode: string) {
  if (mode === 'selected') batchSelection.value = { verification_ids: chosen.value.map(item => item.id) }
  else if (mode === 'filtered') {
    const applied = appliedFilters()
    const query = applyBeijingDates(new URLSearchParams(), applied.dates)
    batchSelection.value = { state: applied.state, ...(applied.user.trim() ? { user_id: Number(applied.user) } : {}),
      after: Number(query.get('after') || 0), before: Number(query.get('before') || 0) }
  } else batchSelection.value = {}
}
async function load(change: boolean | number = false) {
  await Promise.all([loadRecords(change), historyReads.run(() => api<Page<Operation>>(`/api/groups/${props.groupId}/actions`),
    result => { history.value = result.items })])
}
async function confirmAction(row: Verification, action: string, title: string) {
  const group = groups.value.find(item => item.id === props.groupId)
  if (!group) return
  await submitAction(`/api/verifications/${row.id}/actions`, action, title, actionContext(group, action, row.user_id, row.user_name))
  await load()
}
async function backgroundLoad() {
  polling.value = true
  try { await load() } finally { polling.value = false }
}
useRefresh(load, () => props.refreshKey, () => !busy.value, backgroundLoad)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section class="panel" v-loading="busy && !polling">
<div class="panel-title">
<h2>验证记录</h2>
<div class="panel-actions"><el-button @click="previewBatch('all')">批量放行</el-button>
<el-button @click="previewBatch('filtered')">预览筛选中的待验证成员</el-button>
<el-button v-if="chosen.length" @click="previewBatch('selected')">放行所选 {{ chosen.length }} 人</el-button></div>
</div>
<form class="filterbar" aria-label="筛选验证记录" @submit.prevent="filters">
<el-input v-model="userFilter" placeholder="Telegram 用户 ID" aria-label="筛选用户 ID" clearable/>
<el-select v-model="statusFilter" placeholder="全部状态" clearable aria-label="筛选验证状态">
<el-option v-for="s in ['pending', 'muting', 'releasing', 'removing', 'failing', 'passed', 'failed', 'left']" :key="s" :value="s" :label="label(s)"/>
</el-select>
<DateRangeFilter v-model="dates"/>
<el-button type="primary" native-type="submit">查询</el-button>
</form>
<el-table ref="table" :data="rows" empty-text="暂无验证记录" row-key="id" @selection-change="chosen = $event">
<el-table-column type="selection" reserve-selection width="45" :selectable="(row: Verification) => catalog!.active_verifications.includes(row.state)"/>
<el-table-column label="成员" min-width="160">
<template #default="{ row }">{{ row.user_name }}<div class="cell-sub">{{ row.user_id }}</div>
</template>
</el-table-column>
<el-table-column label="状态" width="140">
<template #default="{ row }">
<el-tag :type="statusTone(row.state)">{{ label(row.state) }}</el-tag>
</template>
</el-table-column>
<el-table-column label="截止时间" min-width="170">
<template #default="{ row }">{{ time(row.deadline) }}</template>
</el-table-column>
<el-table-column prop="attempts" label="错误次数" width="90"/>
<el-table-column label="处理说明" min-width="180">
<template #default="{ row }">{{ catalog!.failure_actions[row.failure_action] || row.failure_action }}<div>{{ row.error }}</div>
</template>
</el-table-column>
<el-table-column label="操作" width="180" fixed="right">
<template #default="{ row }">
<el-button v-if="row.allowed_operations.includes('reissue')" size="small" @click="confirmAction(row, 'reissue', '重新出题')">重出题</el-button>
<el-button v-if="row.allowed_operations.includes('release')" size="small" @click="confirmAction(row, 'release', '人工放行')">放行</el-button>
<el-button v-if="row.allowed_operations.includes('ban')" size="small" type="danger" plain @click="confirmAction(row, 'ban', '临时封禁')">封禁</el-button>
<el-button v-if="row.allowed_operations.includes('unban')" size="small" @click="confirmAction(row, 'unban', '解除 PurrAI 入群限制')">解除入群限制</el-button>
<span v-if="!row.allowed_operations.length" class="muted">{{ row.failure_action === 'kick' ? '已踢出，可重新加入' : '已结束' }}</span>
</template>
</el-table-column>
</el-table>
<el-pagination v-model:current-page="pagination" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load"/>
</section>
<section class="panel">
<div class="panel-title">
<h2>最近的人工操作</h2>
<span class="muted">保留操作者、原因与实际执行结果</span>
</div>
<el-table :data="history" empty-text="暂无人工操作">
<el-table-column label="时间" min-width="160">
<template #default="{ row }">{{ time(row.created_at) }}</template>
</el-table-column>
<el-table-column label="操作" width="140">
<template #default="{ row }">{{ operationNames[row.kind] || row.kind }}</template>
</el-table-column>
<el-table-column prop="actor_id" label="操作者" min-width="130"/>
<el-table-column prop="reason" label="原因" min-width="180"/>
<el-table-column label="状态" width="130">
<template #default="{ row }">{{ label(row.state) }}</template>
</el-table-column>
<el-table-column prop="error" label="处理说明" min-width="180"/>
</el-table>
</section>
<BatchReleaseDialog :group-id="groupId" :selection="batchSelection" @close="batchSelection = null" @saved="load"/>
</template>

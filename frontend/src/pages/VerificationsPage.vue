<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Verification, Operation, Page } from '../types'
import { useRecords } from '../composables/useRecords'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ groupId: number; refreshKey: number }>()
const selected = computed(() => props.groupId)
const { catalog, confirmAction: submitAction } = useWorkspace()
const operationNames = computed(() => catalog.value!.operations)
const { rows, total, pagination, statusFilter, userFilter, dates, busy, error, safe, load: loadRecords, filters } = useRecords<Verification>(() => props.groupId, 'verifications')
const history = ref<Operation[]>([])
let version = 0
async function load() {
  const current = ++version
  await Promise.all([loadRecords(), safe(async () => {
    const result = await api<Page<Operation>>(`/api/groups/${props.groupId}/actions`)
    if (current === version) history.value = result.items
  })])
}
async function confirmAction(path: string, action: string, title: string) { await submitAction(path, action, title); await load() }
useRefresh(load, () => props.refreshKey, () => !busy.value)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section class="panel" v-loading="busy">
<div class="panel-title">
<h2>验证记录</h2>
<el-button @click="confirmAction(`/api/groups/${selected}/release-pending`, 'release', '批量放行待验证成员')">批量放行</el-button>
</div>
<div class="filterbar">
<el-input v-model="userFilter" placeholder="Telegram 用户 ID" aria-label="筛选用户 ID" clearable/>
<el-select v-model="statusFilter" placeholder="全部状态" clearable aria-label="筛选验证状态">
<el-option v-for="s in ['pending', 'muting', 'releasing', 'removing', 'failing', 'passed', 'failed', 'left']" :key="s" :value="s" :label="label(s)"/>
</el-select>
<el-date-picker v-model="dates" type="datetimerange" value-format="YYYY-MM-DD HH:mm:ss" start-placeholder="开始（北京时间）" end-placeholder="结束（北京时间）"/>
<el-button @click="filters">查询</el-button>
</div>
<el-table :data="rows" empty-text="暂无验证记录">
<el-table-column label="成员" min-width="160">
<template #default="{ row }">{{ row.user_name }}<div class="cell-sub">{{ row.user_id }}</div>
</template>
</el-table-column>
<el-table-column label="状态" width="140">
<template #default="{ row }">
<el-tag>{{ label(row.state) }}</el-tag>
</template>
</el-table-column>
<el-table-column label="截止时间" min-width="170">
<template #default="{ row }">{{ time(row.deadline) }}</template>
</el-table-column>
<el-table-column prop="attempts" label="错误次数" width="90"/>
<el-table-column label="处理说明" min-width="180">
<template #default="{ row }">{{ { permanent_mute: '永久禁言，留在群里', permanent_ban: '已踢出并禁止重新加入', kick: '已踢出，可重新加入', decline: '已拒绝申请', legacy_ban: '旧版或手动临时封禁' }[row.failure_action as string] }}<div>{{ row.error }}</div>
</template>
</el-table-column>
<el-table-column label="操作" min-width="260">
<template #default="{ row }">
<template v-if="catalog!.active_verifications.includes(row.state)">
<el-button size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'reissue', '重新出题')">重出题</el-button>
<el-button size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'release', '人工放行')">放行</el-button>
<el-button size="small" type="danger" plain @click="confirmAction(`/api/verifications/${row.id}/actions`, 'ban', '临时封禁')">封禁</el-button>
</template>
<template v-else-if="row.state === 'failed' && row.failure_action === 'permanent_mute'">
<el-button size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'release', '人工放行永久禁言')">放行</el-button>
<el-button size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'reissue', '重新出题')">重出题</el-button>
</template>
<el-button v-else-if="row.state === 'failed' && ['decline', 'legacy_ban', 'permanent_ban'].includes(row.failure_action)" size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'unban', '解除 PurrAI 入群限制')">解除入群限制</el-button>
<span v-else-if="row.failure_action === 'kick'" class="muted">已踢出，可重新加入</span>
<span v-else class="muted">已结束</span>
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
</template>

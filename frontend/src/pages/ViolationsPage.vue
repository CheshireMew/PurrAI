<script setup lang="ts">
import { computed } from 'vue'
import type { Violation } from '../types'
import { useRecords } from '../composables/useRecords'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ groupId: number; refreshKey: number }>()
const selected = computed(() => props.groupId)
const { catalog, confirmAction: submitAction } = useWorkspace()
const operationNames = computed(() => catalog.value!.operations)
const { rows, total, pagination, userFilter, dates, busy, error, load, filters } = useRecords<Violation>(() => props.groupId, 'violations')
async function confirmAction(path: string, action: string, title: string) { await submitAction(path, action, title); await load() }
useRefresh(load, () => props.refreshKey)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section class="panel" v-loading="busy">
<div class="panel-title">
<h2>违规记录</h2>
<span class="muted">已删除的 Telegram 消息无法原地恢复</span>
</div>
<div class="filterbar">
<el-input v-model="userFilter" placeholder="Telegram 用户 ID" aria-label="违规用户 ID" clearable/>
<el-date-picker v-model="dates" type="datetimerange" value-format="YYYY-MM-DD HH:mm:ss" start-placeholder="开始（北京时间）" end-placeholder="结束（北京时间）"/>
<el-button @click="filters">查询</el-button>
</div>
<el-table :data="rows" empty-text="暂无违规记录">
<el-table-column label="时间 / 成员" min-width="180">
<template #default="{ row }">{{ time(row.created_at) }}<div class="cell-sub">{{ row.user_id }} {{ row.sender_chat ? '（频道身份）' : '' }}</div>
</template>
</el-table-column>
<el-table-column prop="sender_name" label="昵称及用户名" min-width="170"/>
<el-table-column prop="excerpt" label="消息摘录" min-width="200"/>
<el-table-column label="命中原因" min-width="180">
<template #default="{ row }">{{ row.reasons.join('；') }}</template>
</el-table-column>
<el-table-column label="执行结果" min-width="180">
<template #default="{ row }">
<div v-for="a in row.actions" :key="a.id">{{ { delete: '删消息', profile_mute: '昵称永久禁言', emoji_status_mute: '状态表情永久禁言', warn: '历史通知', moderation_mute: '历史禁言' }[a.kind as string] || operationNames[a.kind] || a.kind }}：{{ label(a.state) }}<small v-if="a.error">{{ a.error }}</small>
</div>
</template>
</el-table-column>
<el-table-column label="纠正操作" min-width="280">
<template #default="{ row }">
<template v-if="!row.sender_chat">
<el-button v-if="row.warning_active" size="small" @click="confirmAction(`/api/groups/${selected}/violations/${row.id}/actions`, 'revoke_warning', '撤销警告')">撤销警告</el-button>
<el-button v-if="(row.profile_violation || row.rule_mute || row.warning_active) && !row.actions.some((a: { kind: string }) => ['contact_ban', 'emoji_status_ban'].includes(a.kind))" size="small" @click="confirmAction(`/api/groups/${selected}/violations/${row.id}/actions`, 'unmute', '解除 PurrAI 禁言')">解禁言</el-button>
<el-button v-if="row.warning_active || row.actions.some((a: { kind: string }) => ['contact_ban', 'emoji_status_ban'].includes(a.kind))" size="small" @click="confirmAction(`/api/groups/${selected}/violations/${row.id}/actions`, 'unban', '解除 PurrAI 封禁')">解封禁</el-button>
</template>
</template>
</el-table-column>
</el-table>
<el-pagination v-model:current-page="pagination" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load"/>
</section>
</template>

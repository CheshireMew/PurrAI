<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { post } from '../api'
import type { SystemStatus } from '../types'
import { useTask } from '../composables/useTask'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ refreshKey: number }>()
const { actionTracker } = useWorkspace()
const reads = useTask(), actions = useTask(), system = ref<Partial<SystemStatus>>({})
const busy = computed(() => reads.busy.value || actions.busy.value)
const error = computed({ get: () => actions.error.value || reads.error.value,
  set: value => { actions.error.value = value; reads.error.value = value } })
async function load() { await reads.safe(async () => { system.value = await api<SystemStatus>('/api/system') }) }
async function systemAction(path: string) {
  if (await actions.safe(async () => { await post(path); ElMessage.success('操作已提交'); await actionTracker.refresh(true) })) await load()
}
useRefresh(load, () => props.refreshKey, () => !busy.value)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>

<section class="panel">
<div class="panel-title">
<h2>运行状态</h2>
<el-button @click="systemAction('/api/system/backup')">生成数据库备份</el-button>
</div>
<div class="system-grid">
<div>
<span>Telegram 连接</span>
<strong>{{ system.connected ? '已连接' : '未连接' }}</strong>
</div>
<div>
<span>Bot 配置</span>
<strong>{{ system.bot_configured ? '已配置' : '未配置' }}</strong>
</div>
<div>
<span>后台登录</span>
<strong>{{ system.login_configured ? '已配置' : '未配置' }}</strong>
</div>
<div>
<span>待执行任务</span>
<strong>{{ system.pending_actions ?? '—' }}</strong>
</div>
</div>
<p>最近收取消息：{{ time(system.last_poll || 0) }}</p>
<p class="muted">最近备份：{{ system.last_backup || '尚未生成' }}</p>
<p v-if="system.chat_budget" class="muted">今日 AI 模型请求 {{ system.chat_budget.global_calls }}/{{ system.chat_budget.global_call_limit }}；Token 预算 {{ system.chat_budget.global_tokens }}/{{ system.chat_budget.global_token_limit }}。按北京时间零点重置，达到上限只暂停 AI 聊天。</p>
<el-alert v-if="system.archive_status" :title="system.archive_status" type="warning" :closable="false"/>
<el-alert v-if="system.last_error" :title="system.last_error" type="warning" :closable="false"/>
</section>
<section class="panel">
<h2>失败操作</h2>
<el-table :data="system.failed_actions || []" empty-text="没有失败操作">
<el-table-column prop="kind" label="操作" width="150"/>
<el-table-column prop="group_id" label="群 ID" width="170"/>
<el-table-column prop="error" label="失败原因" min-width="220"/>
<el-table-column label="操作" width="100">
<template #default="{ row }">
<el-button text @click="systemAction(`/api/actions/${row.id}/retry`)">重试</el-button>
</template>
</el-table-column>
</el-table>
</section>
<section class="panel">
<h2>未处理事件</h2>
<el-table :data="system.failed_events || []" empty-text="没有失败事件">
<el-table-column prop="id" label="事件 ID" width="160"/>
<el-table-column prop="error" label="失败原因"/>
<el-table-column label="操作" width="100">
<template #default="{ row }">
<el-button text @click="systemAction(`/api/events/${row.id}/retry`)">重试</el-button>
</template>
</el-table-column>
</el-table>
</section>

</template>

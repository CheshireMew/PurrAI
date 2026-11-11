<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { post } from '../api'
import type { Operation, Page, SystemStatus, WorkerStatus } from '../types'
import DeliveryReviewDialog from '../components/DeliveryReviewDialog.vue'
import BackupsDialog from '../components/BackupsDialog.vue'
import { useTask } from '../composables/useTask'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ refreshKey: number }>()
const { actionTracker, catalog } = useWorkspace()
const reads = useTask(), actions = useTask(), system = ref<Partial<SystemStatus> & { workers?: WorkerStatus[] }>({})
const busy = computed(() => reads.busy.value || actions.busy.value)
const error = computed({ get: () => actions.error.value || reads.error.value,
  set: value => { actions.error.value = value; reads.error.value = value } })
const failurePage = ref(1), eventPage = ref(1), failureTotal = ref(0), eventTotal = ref(0)
const reviewing = ref<Operation | null>(null)
const backupsOpen = ref(false)
const alerts = ref<{name: string; active: boolean; message: string; changed_at: number}[]>([])
async function load() { await reads.safe(async () => {
  const [status, failures, events, episodes] = await Promise.all([api<SystemStatus>('/api/system'),
    api<Page<Operation>>(`/api/system/failures?page_number=${failurePage.value}`),
    api<Page<{id: number; error: string}>>(`/api/system/failures?event=true&page_number=${eventPage.value}`),
    api<typeof alerts.value>('/api/system/alerts')])
  alerts.value = episodes
  system.value = { ...status, failed_actions: failures.items, failed_events: events.items }
  failureTotal.value = failures.total; eventTotal.value = events.total
}) }
async function systemAction(path: string) {
  if (await actions.safe(async () => { await post(path); ElMessage.success('操作已提交'); await actionTracker.refresh(true) })) await load()
}
useRefresh(load, () => props.refreshKey, () => !busy.value)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>

<section class="panel" v-loading="reads.busy.value">
<div class="panel-title">
<h2>运行状态</h2>
<el-button :loading="actions.busy.value" @click="systemAction('/api/system/backup')">生成数据库备份</el-button>
<el-button @click="backupsOpen = true">备份计划与文件</el-button>
</div>
<div class="system-grid">
<div>
<span>Telegram 连接</span>
<strong :class="{ 'status-error': system.connected === false }">{{ system.connected === undefined ? '—' : system.connected ? '已连接' : '未连接' }}</strong>
</div>
<div>
<span>Bot 配置</span>
<strong>{{ system.bot_configured === undefined ? '—' : system.bot_configured ? '已配置' : '未配置' }}</strong>
</div>
<div>
<span>后台登录</span>
<strong>{{ system.login_configured === undefined ? '—' : system.login_configured ? '已配置' : '未配置' }}</strong>
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
<el-alert v-for="alert in alerts.filter(item => item.active)" :key="alert.name" :title="`${alert.name}：${alert.message}`" type="warning" :closable="false"/>
<p v-for="worker in system.workers?.filter(row => row.state !== 'running')" :key="worker.name">{{ worker.name }} · {{ worker.state === 'retrying' ? '正在恢复' : '已停止' }} · 已重启 {{ worker.restarts }} 次</p>
</section>
<section class="panel">
<h2>失败操作</h2>
<el-table :data="system.failed_actions || []" empty-text="没有失败操作">
<el-table-column label="操作" width="150"><template #default="{ row }">{{ catalog!.operations[row.kind] || row.kind }}</template></el-table-column>
<el-table-column prop="group_id" label="群 ID" width="170"/>
<el-table-column prop="error" label="失败原因" min-width="220"/>
<el-table-column label="操作" width="200">
<template #default="{ row }">
<el-button text :disabled="actions.busy.value" @click="systemAction(`/api/actions/${row.id}/retry`)">重试</el-button>
<el-button v-if="row.can_review_delivery" text @click="reviewing = row">登记核实结果</el-button>
</template>
</el-table-column>
</el-table>
<el-pagination v-model:current-page="failurePage" :page-size="20" :total="failureTotal" layout="prev, pager, next, total" @current-change="load"/>
</section>
<section class="panel">
<h2>未处理事件</h2>
<el-table :data="system.failed_events || []" empty-text="没有失败事件">
<el-table-column prop="id" label="事件 ID" width="160"/>
<el-table-column prop="error" label="失败原因"/>
<el-table-column label="操作" width="100">
<template #default="{ row }">
<el-button text :disabled="actions.busy.value" @click="systemAction(`/api/events/${row.id}/retry`)">重试</el-button>
</template>
</el-table-column>
</el-table>
<el-pagination v-model:current-page="eventPage" :page-size="20" :total="eventTotal" layout="prev, pager, next, total" @current-change="load"/>
</section>
<DeliveryReviewDialog :action="reviewing" @close="reviewing = null" @saved="load"/>
<BackupsDialog :open="backupsOpen" @close="backupsOpen = false" @saved="load"/>

</template>

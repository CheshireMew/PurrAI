<script setup lang="ts">
import { ref, watch } from 'vue'
import { api, time } from '../api'
import { useLatestRequest } from '../composables/useLatestRequest'
import { useRefresh } from '../composables/useRefresh'
import { useWorkspace } from '../workspace'
import type { ChatStatus } from '../types'
import { route, savedFilters, saveFilters } from '../navigation'

type Row = {id: number; message_id: number; topic_id: number; state: string; direct: boolean; attempts: number;
  reason: string; created_at: number; available_at: number; timing: Record<string, number | boolean> | null}
type Diagnostics = {items: Row[]; total: number; usage: {day: string; topic_id: number; calls: number; tokens: number}[]; decisions?: {message_id: number; topic_id: number; reason: string; created_at: number}[]}
const props = defineProps<{groupId: number; refreshKey: number}>()
const { busy, error, run } = useLatestRequest(), { groups } = useWorkspace()
const topic = ref(''), state = ref(''), page = ref(1), data = ref<Diagnostics>({items: [], total: 0, usage: []}), status = ref<ChatStatus | null>(null)
let applied = {topic: '', state: ''}, written = route.value.params.get('filter') || ''
function restore() {
  const value = savedFilters('chat')
  topic.value = typeof value.topic === 'string' ? value.topic : ''
  state.value = typeof value.state === 'string' ? value.state : ''
  page.value = Number.isInteger(value.page) && value.page > 0 ? value.page : 1
  applied = {topic: topic.value, state: state.value}
}
restore()
const states: Record<string, string> = {pending: '等待处理', generating: '请求模型', sending: '正在发送', done: '已回复',
  silent: '保持静默', skipped: '已跳过', merged: '合并处理', failed: '失败', retracting: '正在撤回', retracted: '已撤回', retraction_failed: '撤回失败'}
async function load(change: boolean | number = false) {
  await run(async () => {
    if (applied.topic && !/^\d+$/.test(applied.topic)) throw new Error('话题 ID 请填写非负整数，普通群聊天使用 0')
    const stored = {section: 'chat', ...applied, page: page.value}
    written = JSON.stringify(stored); saveFilters(stored, !change)
    const query = new URLSearchParams({page_number: String(page.value), state: applied.state})
    if (applied.topic) query.set('topic_id', applied.topic)
    const [diagnostics, runtime] = await Promise.all([api<Diagnostics>(`/api/groups/${props.groupId}/chat/diagnostics?${query}`), api<ChatStatus>(`/api/groups/${props.groupId}/chat`)])
    return {diagnostics, runtime}
  }, result => { data.value = result.diagnostics; status.value = result.runtime })
}
async function search() { applied = {topic: topic.value.trim(), state: state.value}; page.value = 1; await load(true) }
watch(() => route.value.params.get('filter') || '', value => { if (value !== written) { written = value; restore(); void load() } })
useRefresh(load, () => props.refreshKey)
</script>

<template>
<section class="panel" v-loading="busy"><h2>群聊天状态</h2><el-alert v-if="error" :title="error" type="error" :closable="false"/>
<p>{{ groups.find(group => group.id === groupId)?.settings.chat_enabled ? '本群已开启聊天' : '本群未开启聊天' }} · {{ status?.configured ? `模型 ${status.model}` : '模型尚未配置' }} · 待处理 {{ status?.pending ?? '—' }}</p>
<p v-if="status?.budget">今日请求 {{ status.budget.group_calls }} / {{ status.budget.group_call_limit }}；Token 额度占用 {{ status.budget.group_tokens }} / {{ status.budget.group_token_limit }}。包括未确认用量的预留，额度不是厂商账单。</p>
<el-alert v-if="status?.error" :title="status.error" type="warning" :closable="false"/>
<form class="filterbar" @submit.prevent="search"><el-input v-model="topic" aria-label="聊天话题 ID" placeholder="全部话题；普通聊天为 0" clearable/><el-select v-model="state" aria-label="聊天处理状态" placeholder="全部状态" clearable><el-option v-for="(name, value) in states" :key="value" :value="value" :label="name"/></el-select><el-button native-type="submit">查询聊天处理</el-button></form>
<p>最近约 48 小时的聊天任务。过滤、合并、静默和等待均可查询；下方保留最近观察到的输入决定，不保存额外聊天正文。</p>
<el-table :data="data.items" empty-text="没有匹配的聊天任务"><el-table-column label="时间 / 消息" min-width="170"><template #default="{ row }">{{ time(row.created_at) }}<div>{{ row.message_id }} · 话题 {{ row.topic_id }}</div></template></el-table-column><el-table-column label="结果" min-width="150"><template #default="{ row }">{{ states[row.state] || row.state }} · {{ row.direct ? '直接呼叫' : '主动参与' }} · {{ row.attempts }} 次</template></el-table-column><el-table-column prop="reason" label="处理原因" min-width="250"/><el-table-column label="耗时 / 用量" min-width="210"><template #default="{ row }"><template v-if="row.timing">模型 {{ row.timing.model_seconds ?? '—' }} 秒 · 最近处理 {{ row.timing.last_attempt_seconds ?? '—' }} 秒<div>{{ row.timing.token_usage_confirmed ? `实际 ${row.timing.actual_tokens} Token` : `预留 ${row.timing.reserved_tokens ?? 0} Token，未确认实际用量` }}</div></template><span v-else>未记录耗时</span></template></el-table-column></el-table>
<el-pagination v-model:current-page="page" :page-size="20" :total="data.total" layout="prev, pager, next, total" @current-change="load"/>
</section>
<section class="panel"><h2>最近输入处理决定</h2><el-table :data="data.decisions || []" max-height="300"><el-table-column label="时间"><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column><el-table-column prop="message_id" label="消息编号"/><el-table-column prop="topic_id" label="话题"/><el-table-column prop="reason" label="为什么参与或忽略" min-width="280"/></el-table></section>
<section class="panel"><h2>最近 90 天的请求用量</h2><el-table :data="data.usage" max-height="360" empty-text="暂无已记录用量"><el-table-column prop="day" label="北京时间日期"/><el-table-column prop="topic_id" label="话题"/><el-table-column prop="calls" label="请求次数"/><el-table-column prop="tokens" label="Token 额度占用"/></el-table></section>
</template>

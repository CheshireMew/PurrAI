<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, post, time } from '../api'
import { useLatestRequest } from '../composables/useLatestRequest'
import { useTask } from '../composables/useTask'
import type { ArchivedMessage, ArchivePage, Rule } from '../types'

type Verdict = { reasons: string[]; delete_message: boolean; ban_member: boolean; mute_member: boolean }
type Result = {items: {id: string; message_id: number; text: string; before: Verdict; after: Verdict; changed: boolean; incomplete: boolean; unresolved_links: boolean}[]; changed_count: number; notes: string[]}
const props = defineProps<{open: boolean; groupId: number; draft?: Rule; initialIds?: string[]}>()
const emit = defineEmits<{close: []}>()
const visible = computed({get: () => props.open, set: value => { if (!value) emit('close') }})
const reads = useLatestRequest(), testing = useTask(), error = computed(() => testing.error.value || reads.error.value)
const rows = ref<ArchivedMessage[]>([]), query = ref(''), page = ref(1), total = ref(0), chosen = ref<ArchivedMessage[]>([])
const newcomer = ref(false), result = ref<Result | null>(null)
async function load() {
  await reads.run(() => api<ArchivePage>(`/api/groups/${props.groupId}/messages?${new URLSearchParams({query: query.value, page_number: String(page.value)})}`), data => { rows.value = data.items; total.value = data.total })
}
watch(() => props.open, value => {
  if (value) { result.value = null; testing.error.value = ''; chosen.value = []; query.value = ''; page.value = 1; void load() }
  else reads.cancel()
})
async function testSamples() {
  await testing.safe(async () => {
    const ids = [...new Set([...(props.initialIds || []), ...chosen.value.map(row => row.id)])]
    if (!ids.length || ids.length > 100) throw new Error('请选择 1 至 100 条归档样本')
    result.value = await post(`/api/groups/${props.groupId}/rules/preview-archive`, {archive_ids: ids,
      newcomer: newcomer.value, ...(props.draft ? {draft: {...props.draft, id: props.draft.id || ''}} : {})})
  })
}
function consequence(verdict: Verdict) { return verdict.ban_member ? '删消息并永久封禁' : verdict.mute_member ? '删消息并永久禁言' : verdict.delete_message ? '只删消息' : '未命中' }
</script>

<template>
<el-dialog v-model="visible" title="归档样本试跑" width="min(1000px, 96vw)" :close-on-click-modal="false">
<p>{{ draft ? '比较当前规则与正在编辑的规则草稿，尚未保存。' : '按当前规则重新评估所选消息。' }} 使用保存的原消息元数据，不执行处罚。</p>
<el-alert v-if="error" :title="error" type="error" :closable="false"/>
<form class="filterbar" @submit.prevent="page = 1; load()"><el-input v-model="query" placeholder="搜索归档样本" aria-label="搜索归档样本"/><el-button native-type="submit">搜索样本</el-button><el-checkbox v-model="newcomer">假设仍在新成员观察期</el-checkbox></form>
<p v-if="initialIds?.length">归档页面已选 {{ initialIds.length }} 条；可继续添加样本。</p>
<el-table :data="rows" row-key="id" v-loading="reads.busy.value" max-height="280" @selection-change="chosen = $event"><el-table-column type="selection" reserve-selection width="45"/><el-table-column label="时间 / 消息编号" min-width="160"><template #default="{ row }">{{ time(row.sent_at) }} · {{ row.message_id }}</template></el-table-column><el-table-column prop="sender_name" label="发送者" width="130"/><el-table-column label="样本" min-width="250"><template #default="{ row }">{{ row.text || row.caption || row.message_type }}</template></el-table-column></el-table>
<el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load"/>
<p><el-button type="primary" :loading="testing.busy.value" @click="testSamples">试跑所选样本</el-button></p>
<template v-if="result"><p>处置或原因变化 {{ result.changed_count }} 条 / {{ result.items.length }} 条</p><p v-for="note in result.notes" :key="note">{{ note }}</p><el-table :data="result.items" max-height="350"><el-table-column prop="message_id" label="消息编号" width="110"/><el-table-column prop="text" label="原文" min-width="200"/><el-table-column label="当前规则" min-width="220"><template #default="{ row }">{{ consequence(row.before) }}<p>{{ row.before.reasons.join('；') }}</p></template></el-table-column><el-table-column label="草稿结果" min-width="220"><template #default="{ row }">{{ consequence(row.after) }}{{ row.changed ? '（变化）' : '' }}<p>{{ row.after.reasons.join('；') }}</p><p v-if="row.incomplete">归档不完整，仅评估已保存内容</p><p v-if="row.unresolved_links">缺少完整的链接目标核实证据</p></template></el-table-column></el-table></template>
<template #footer><el-button @click="visible = false">关闭</el-button></template>
</el-dialog>
</template>

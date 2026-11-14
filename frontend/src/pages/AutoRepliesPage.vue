<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { useRecords } from '../composables/useRecords'
import { useRefresh } from '../composables/useRefresh'
import { useWorkspace } from '../workspace'
import { useDraftClose } from '../composables/useDraftClose'
import { usePreviewResult } from '../composables/usePreviewResult'

interface AutoReply {
  id: string; version: string; keyword: string; reply: string; mode: 'exact' | 'contains'; enabled: boolean
  priority: number; cooldown_seconds: number
}
interface Preview { matched: boolean; rule: AutoReply | null }
const props = defineProps<{ groupId: number; refreshKey: number }>()
const { mutations } = useWorkspace()
const query = reactive({ query: '' })
const { rows, total, pagination, busy, error, safe, load, filters } = useRecords<AutoReply>(
  () => props.groupId, 'auto-replies', () => ({ ...query }), values => { query.query = values.query || '' })
const open = ref(false), editing = ref<AutoReply | null>(null)
const defaults = () => ({ keyword: '', reply: '', mode: 'exact' as 'exact' | 'contains', enabled: true, priority: 0, cooldown_seconds: 10 })
const form = reactive(defaults())
const sample = ref(''), draftSample = ref(''), original = ref(''), rulesRevision = ref(0)
const savedPreview = usePreviewResult<Preview>(() => [props.groupId, sample.value, rulesRevision.value, props.refreshKey])
const draftPreview = usePreviewResult<Preview>(() => [props.groupId, form, draftSample.value, open.value])
const result = savedPreview.result, draftResult = draftPreview.result
const { requestClose } = useDraftClose(() => JSON.stringify(body()) !== original.value, () => { open.value = false }, () => busy.value)
function edit(row: AutoReply | null = null) {
  editing.value = row; Object.assign(form, defaults(), row || {}); draftSample.value = ''; draftPreview.invalidate()
  original.value = JSON.stringify(body())
  error.value = ''; open.value = true
}
function body() {
  return { keyword: form.keyword, reply: form.reply, mode: form.mode, enabled: form.enabled,
    priority: form.priority, cooldown_seconds: form.cooldown_seconds }
}
async function save() {
  const success = await safe(async () => {
    if (!form.keyword.trim() || !form.reply.trim()) throw new Error('请填写关键词和回复内容')
    await mutations.submit(`/api/groups/${props.groupId}/auto-replies${editing.value ? '/' + editing.value.id : ''}`,
      editing.value ? 'PUT' : 'POST', body(), editing.value?.version, '保存自动回复')
    ElMessage.success('自动回复已保存')
  })
  if (success) { open.value = false; rulesRevision.value++; await load() }
}
async function toggle(row: AutoReply) {
  await safe(async () => {
    await mutations.submit(`/api/groups/${props.groupId}/auto-replies/${row.id}`, 'PUT',
      { ...row, enabled: !row.enabled }, row.version, row.enabled ? '停用自动回复' : '启用自动回复')
    ElMessage.success(row.enabled ? '已停用' : '已启用')
  })
  rulesRevision.value++; await load()
}
async function preview(draft = false) {
  await safe(async () => {
    const value = draft ? draftSample.value : sample.value
    if (!value.trim()) throw new Error('请填写测试消息')
    await (draft ? draftPreview : savedPreview).run(() => api<Preview>(`/api/groups/${props.groupId}/auto-replies/preview`, {
      method: 'POST', body: JSON.stringify({ text: value, ...(draft ? { draft: body() } : {}) }) }))
  })
}
useRefresh(load, () => props.refreshKey)
</script>

<template>
<el-alert v-if="error && !open" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section class="panel" v-loading="busy">
  <div class="panel-title"><h2>关键词自动回复</h2><el-button type="primary" @click="edit()">添加自动回复</el-button></div>
  <p class="muted">命中后回复固定文字，无需 AI。每条消息只回复一条，优先级越大越优先；同优先级先添加的优先。</p>
  <form class="filterbar" @submit.prevent="filters()">
    <el-input v-model="query.query" aria-label="搜索自动回复" placeholder="搜索关键词或回复内容" maxlength="250" clearable/>
    <el-button native-type="submit">查询</el-button>
  </form>
  <el-table :data="rows" row-key="id" empty-text="还没有自动回复，可点击上方添加">
    <el-table-column prop="keyword" label="关键词" min-width="160"/>
    <el-table-column prop="reply" label="回复内容" min-width="240" show-overflow-tooltip/>
    <el-table-column label="匹配" width="110"><template #default="{ row }">{{ row.mode === 'exact' ? '完整匹配' : '包含匹配' }}</template></el-table-column>
    <el-table-column prop="priority" label="优先级" width="90"/>
    <el-table-column label="冷却" width="100"><template #default="{ row }">{{ row.cooldown_seconds }} 秒</template></el-table-column>
    <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
    <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }">
      <el-button text :disabled="busy" @click="edit(row)">编辑</el-button>
      <el-button text :disabled="busy" @click="toggle(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
    </template></el-table-column>
  </el-table>
  <el-pagination v-model:current-page="pagination" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load"/>
</section>
<section class="panel">
  <h2>测试自动回复</h2>
  <p class="muted">预览已保存且启用的规则，不向群内发消息，不计算冷却。完整匹配忽略首尾空白、英文大小写和全半角差别；包含匹配允许关键词出现在消息中间。</p>
  <form @submit.prevent="preview()">
    <el-input v-model="sample" type="textarea" :rows="3" maxlength="4000" aria-label="自动回复测试消息" placeholder="输入群友会发送的文字"/>
    <el-button class="test-button" native-type="submit" :loading="busy">测试已保存规则</el-button>
  </form>
  <div v-if="result" role="status">
    <p>{{ result.matched ? `命中：${result.rule!.keyword}` : '未命中启用的自动回复规则' }}</p>
    <pre v-if="result.rule" class="reply-preview">{{ result.rule.reply }}</pre>
  </div>
</section>
<el-dialog v-model="open" :title="editing ? '编辑自动回复' : '添加自动回复'" class="form-dialog" width="min(620px, 94vw)" :before-close="requestClose" :close-on-click-modal="false" destroy-on-close>
  <el-form label-position="top" @submit.prevent="save">
    <el-form-item label="关键词"><el-input v-model="form.keyword" maxlength="250" aria-label="自动回复关键词"/></el-form-item>
    <el-form-item label="回复内容"><el-input v-model="form.reply" type="textarea" :rows="6" maxlength="4000" show-word-limit aria-label="自动回复内容" placeholder="发送的完整文字，支持换行"/></el-form-item>
    <el-form-item label="匹配方式"><el-radio-group v-model="form.mode"><el-radio value="exact">完整匹配</el-radio><el-radio value="contains">包含匹配</el-radio></el-radio-group></el-form-item>
    <el-form-item label="优先级"><el-input-number v-model="form.priority" :min="0" :max="1000" aria-label="自动回复优先级"/></el-form-item>
    <el-form-item label="冷却时间（秒）"><el-input-number v-model="form.cooldown_seconds" :min="0" :max="86400" aria-label="自动回复冷却时间"/></el-form-item>
    <p class="muted">同一条规则在本群共用冷却时间，0 秒表示不冷却。停用后立即停止匹配。暂停群保护不影响自动回复。</p>
    <el-form-item label="启用"><el-switch v-model="form.enabled" aria-label="启用自动回复"/></el-form-item>
    <el-divider/>
    <el-form-item label="试一条消息"><el-input v-model="draftSample" maxlength="4000" aria-label="自动回复草稿测试消息"/></el-form-item>
    <el-button :loading="busy" @click="preview(true)">测试当前草稿</el-button>
    <div v-if="draftResult" role="status"><p>{{ draftResult.matched ? '将回复：' : '当前草稿未命中（停用的规则不匹配）' }}</p><pre v-if="draftResult.rule" class="reply-preview">{{ draftResult.rule.reply }}</pre></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false"/>
  </el-form>
  <template #footer><el-button :disabled="busy" @click="requestClose">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存</el-button></template>
</el-dialog>
</template>

<style scoped>
.reply-preview { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; padding: 16px; border: 1px solid var(--el-border-color); border-radius: 8px; max-height: 320px; overflow: auto; }
.test-button { margin-top: 12px; }
</style>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, time } from '../api'
import { useWorkspace } from '../workspace'
import { useTask } from '../composables/useTask'
import { useDraftClose } from '../composables/useDraftClose'
import { usePreviewResult } from '../composables/usePreviewResult'

const props = defineProps<{ groupId: number }>()
const { mutations } = useWorkspace()
const open = ref(false), page = ref(1), query = ref(''), activeTab = ref('samples')
const { busy, error, safe } = useTask()
const data = ref<any>(null), history = ref<any>(null), historyPage = ref(1), historyState = ref('')
const editing = ref<any>(null), editor = ref(false), previewText = ref(''), quoted = ref(false)
const previewScope = ref('text')
const form = reactive({ text: '', sender_name: '', scope: 'text', enabled: true, reason: '' })
const settingsBaseline = ref(''), settingsVersion = ref(0), sampleBaseline = ref(''), samplesRevision = ref(0)
const settingsDirty = computed(() => !!data.value && JSON.stringify(data.value.settings) !== settingsBaseline.value)
const { requestClose: closePanel } = useDraftClose(() => settingsDirty.value,
  () => { open.value = false }, () => busy.value, () => open.value)
const { requestClose: closeSample } = useDraftClose(() => JSON.stringify(form) !== sampleBaseline.value,
  () => { editor.value = false }, () => busy.value, () => editor.value)
const { result: preview, run: runPreview, invalidate } = usePreviewResult<any>(() => [props.groupId, open.value,
  previewText.value, quoted.value, previewScope.value, samplesRevision.value])
const stateLabels: Record<string, string> = { pending: '等待审核', requesting: '正在审核', clean: '非广告', advertisement: 'AI确认广告', matched: '本地样本命中', handled: '规则已处理', ignored: '功能口令或空正文', failed: '审核失败', cancelled: '已取消', superseded: '消息已编辑', exempt: '已豁免' }
const scopeLabels: Record<string, string> = { text: '正文可独立判断', bio: '简介参考', context: '需结合资料或上下文' }
const sourceLabels: Record<string, string> = { model: 'AI确认', manual: '管理员确认', history: '历史判定' }
const base = () => `/api/groups/${props.groupId}`
async function load() {
  const group = props.groupId
  const result = await api(`${base()}/advertisement-samples?page_number=${page.value}&query=${encodeURIComponent(query.value)}`)
  if (group === props.groupId) {
    const keepDraft = settingsDirty.value
    const draft = data.value?.settings
    data.value = result
    if (keepDraft) data.value.settings = draft
    else { settingsBaseline.value = JSON.stringify(result.settings); settingsVersion.value = result.version }
    samplesRevision.value++
  }
}
async function loadHistory() {
  const group = props.groupId
  const result = await api(`${base()}/advertisement-reviews?page_number=${historyPage.value}&state=${historyState.value}`)
  if (group === props.groupId) history.value = result
}
async function show() {
  data.value = null; settingsBaseline.value = ''; activeTab.value = 'samples'; open.value = true
  await safe(async () => { await load(); await loadHistory() })
}
function edit(row: any = null) {
  editing.value = row
  Object.assign(form, row ? { text: row.text, sender_name: row.sender_name, scope: row.scope, enabled: row.enabled, reason: row.reason } : { text: '', sender_name: '', scope: 'text', enabled: true, reason: '' })
  sampleBaseline.value = JSON.stringify(form)
  editor.value = true
}
async function save() {
  await safe(async () => {
    const row = editing.value
    await mutations.submit(`${base()}/advertisement-samples${row ? '/' + row.id : ''}`, row ? 'PUT' : 'POST', { ...form }, row ? String(row.version) : undefined, '保存广告样本')
    editor.value = false; await load(); ElMessage.success('样本已保存')
  })
}
async function saveSettings() {
  await safe(async () => {
    const result = await mutations.submit<any>(`${base()}/advertisement-settings`, 'PUT', data.value.settings, String(settingsVersion.value), '保存广告审核设置')
    settingsBaseline.value = JSON.stringify(data.value.settings)
    settingsVersion.value = result.version
    await load(); ElMessage.success('审核设置已保存')
  })
}
const importing = ref<any>(null)
async function importHistory(apply: boolean) {
  await safe(async () => {
    importing.value = await mutations.submit(`${base()}/advertisement-samples/import-history`, 'POST', { apply }, undefined, apply ? '导入历史广告判定' : '预览历史广告判定')
    if (apply) { await load(); ElMessage.success(`已导入 ${importing.value.count} 条历史判定`) }
  })
}
async function test() {
  await safe(async () => { await runPreview(() => api(`${base()}/advertisement-samples/preview`, { method: 'POST', body: JSON.stringify({ text: previewText.value, quoted: quoted.value, scope: previewScope.value }) })) })
}
async function retry(row: any) {
  await safe(async () => {
    await mutations.submit(`${base()}/advertisement-reviews/retry`, 'POST', { ids: [row.id] }, undefined, '重试广告审核')
    await loadHistory()
  })
}
watch(() => props.groupId, () => { open.value = false; editor.value = false; data.value = history.value = importing.value = null; invalidate(); page.value = historyPage.value = 1 })
</script>

<template>
  <el-button @click="show">广告样本与审核</el-button>
  <el-dialog v-model="open" title="广告样本与审核" class="form-dialog" width="min(1100px, 96vw)" :before-close="closePanel" :close-on-click-modal="false">
    <div v-if="data" v-loading="busy">
      <el-alert v-if="!data.model_ready" title="尚未配置 AI；本地样本仍可匹配，待审消息会保留" type="warning" :closable="false"/>
      <el-tabs v-model="activeTab">
        <el-tab-pane label="广告样本" name="samples">
          <div class="ad-tools"><el-input v-model="query" placeholder="搜索样本正文" aria-label="搜索广告样本" @keyup.enter="safe(load)"/><el-button @click="page = 1; safe(load)">搜索</el-button><el-button type="primary" @click="edit()">添加已确认广告</el-button><el-button @click="importHistory(false)">导入历史判定</el-button></div>
          <el-alert v-if="importing" :title="`${importing.applied ? '已导入' : '可导入'} ${importing.count} 条历史判定。导入后由 AI 批量复核正文适用范围，再用于本地匹配；不会处罚历史消息。`" type="info" :closable="false"/>
          <el-button v-if="importing && !importing.applied && importing.count" @click="importHistory(true)">确认导入</el-button>
          <el-table :data="data.items" empty-text="暂无已确认广告样本">
            <el-table-column prop="text" label="广告内容" min-width="260"><template #default="{ row }"><span class="ad-text">{{ row.text }}</span></template></el-table-column>
            <el-table-column label="来源" width="120"><template #default="{ row }">{{ sourceLabels[row.source] }}<div v-if="row.message_id">消息 {{ row.message_id }}</div></template></el-table-column>
            <el-table-column label="状态" min-width="180"><template #default="{ row }">{{ row.corrected ? '原判定已纠正' : !row.enabled ? '已停用' : row.usable ? '参与本地匹配' : row.review_state === 'pending' || row.review_state === 'requesting' ? '等待 AI 复核' : '仅保留判定' }}<div>{{ scopeLabels[row.scope] }}</div><div v-if="row.review_error">{{ row.review_error }}</div></template></el-table-column>
            <el-table-column label="操作" width="80"><template #default="{ row }"><el-button text @click="edit(row)">编辑</el-button></template></el-table-column>
          </el-table>
          <el-pagination v-model:current-page="page" :page-size="20" :total="data.total" layout="prev, pager, next, total" @current-change="safe(load)"/>
          <h3>试一条消息</h3>
          <el-input v-model="previewText" type="textarea" :rows="3" aria-label="样本匹配测试消息"/>
          <div class="ad-tools"><el-select v-model="previewScope" aria-label="样本测试范围"><el-option label="消息正文" value="text"/><el-option label="用户简介" value="bio"/></el-select><el-checkbox v-model="quoted">包含引用或转发</el-checkbox><el-button :disabled="!previewText.trim()" @click="test">测试本地匹配</el-button></div>
          <el-alert v-if="preview" :type="preview.action !== 'review' ? 'warning' : 'info'" :closable="false" :title="preview.action !== 'review' ? `命中样本，相似度 ${preview.result.score.toFixed(3)}，${preview.action === 'delete' ? '删除消息' : '删除并永久禁言'}` : '未达到本地处置条件，交由 AI 审核'"/>
          <p v-if="preview?.result" class="ad-text">对照样本：{{ preview.result.sample.text }}</p>
        </el-tab-pane>
        <el-tab-pane label="审核记录" name="history">
          <div class="ad-tools"><el-select v-model="historyState" aria-label="审核状态" @change="historyPage = 1; safe(loadHistory)"><el-option label="全部状态" value=""/><el-option v-for="(label, value) in stateLabels" :key="value" :label="label" :value="value"/></el-select><el-button @click="safe(loadHistory)">刷新</el-button></div>
          <el-table :data="history?.items || []" empty-text="暂无审核记录">
            <el-table-column label="消息" min-width="240"><template #default="{ row }"><span class="ad-text">{{ row.message.text || row.message.caption }}</span><div>{{ row.message._profile_event ? '入群资料检查' : `消息 ${row.message_id}` }} · {{ time(row.created_at) }}</div></template></el-table-column>
            <el-table-column label="当时的用户简介" min-width="240"><template #default="{ row }"><span class="ad-text">{{ row.message._sender_profile?.status === 'available' ? row.message._sender_profile.bio : row.message._sender_profile?.status === 'empty' ? 'Telegram未返回简介内容' : '未能读取简介' }}</span><div v-if="row.message._sender_profile?.checked_at">{{ time(row.message._sender_profile.checked_at) }}</div><details v-if="row.message._ad_context?.length"><summary>该账号近期发言</summary><p v-for="item in row.message._ad_context" :key="item.id" class="ad-text">{{ item.text }}</p></details></template></el-table-column>
            <el-table-column label="判定" min-width="230"><template #default="{ row }">{{ stateLabels[row.state] || row.state }}<div>{{ row.result.reason || row.error }}</div><div v-if="row.result.sample_id">样本 {{ row.result.sample_id }} · {{ row.result.score }}</div></template></el-table-column>
            <el-table-column label="操作" width="80"><template #default="{ row }"><el-button v-if="['failed', 'pending'].includes(row.state)" text @click="retry(row)">重试</el-button></template></el-table-column>
          </el-table>
          <el-pagination v-model:current-page="historyPage" :page-size="20" :total="history?.total || 0" layout="prev, pager, next, total" @current-change="safe(loadHistory)"/>
        </el-tab-pane>
        <el-tab-pane label="审核设置" name="settings">
          <el-form label-position="top">
            <el-form-item label="广告审核"><el-switch v-model="data.settings.enabled" aria-label="广告审核开关"/></el-form-item>
            <el-form-item label="本地样本匹配"><el-switch v-model="data.settings.similarity_enabled" aria-label="样本匹配开关"/></el-form-item>
            <el-form-item label="本地处置相似度阈值"><el-input-number v-model="data.settings.threshold" :min="0.75" :max="1" :step="0.01" :precision="2"/></el-form-item>
            <el-form-item label="最少有效字符数"><el-input-number v-model="data.settings.minimum_length" :min="8" :max="200"/></el-form-item>
            <el-form-item label="至少保留原样本的比例"><el-input-number v-model="data.settings.minimum_sample_coverage" :min="0.4" :max="1" :step="0.05" :precision="2"/></el-form-item>
            <el-form-item label="样本命中处置"><el-select v-model="data.settings.sample_action"><el-option label="删除消息" value="delete"/><el-option label="删除并永久禁言" value="permanent_mute"/></el-select></el-form-item>
            <el-form-item label="每次最多审核消息数"><el-input-number v-model="data.settings.batch_size" :min="1" :max="24"/></el-form-item>
            <el-form-item label="同一账号参考发言数"><el-input-number v-model="data.settings.context_messages" :min="0" :max="10" aria-label="同一账号参考发言数"/></el-form-item>
            <el-form-item label="参考发言时间范围（秒）"><el-input-number v-model="data.settings.context_window_seconds" :min="30" :max="3600" aria-label="参考发言时间范围（秒）"/></el-form-item>
            <p>聊天消息一次请求完成广告判断和回复，其余消息独立审核，共用现有 AI 总额度。</p>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </div>
    <template #footer><el-alert v-if="error && !editor" :title="error" type="error" :closable="false"/>
      <el-button :disabled="busy" @click="closePanel">关闭</el-button>
      <el-button v-if="activeTab === 'settings'" type="primary" :loading="busy" :disabled="!data || !settingsDirty" @click="saveSettings">保存审核设置</el-button>
    </template>
  </el-dialog>
  <el-dialog v-model="editor" :title="editing ? '编辑广告样本' : '添加已确认广告'" class="form-dialog" width="min(650px, 95vw)" :before-close="closeSample" :close-on-click-modal="false">
    <el-form label-position="top">
      <el-form-item label="广告内容"><el-input v-model="form.text" type="textarea" :rows="5" :maxlength="4096" aria-label="广告内容"/></el-form-item>
      <el-form-item label="发送者昵称（可选）"><el-input v-model="form.sender_name" :maxlength="500"/></el-form-item>
      <el-form-item label="判定依据"><el-select v-model="form.scope" aria-label="样本判定依据"><el-option label="正文自身就是广告，可用于正文匹配" value="text"/><el-option label="简介参考，不能单独处罚" value="bio"/><el-option label="依赖资料或上下文，仅保留判定" value="context"/></el-select></el-form-item>
      <el-form-item label="确认或修改原因"><el-input v-model="form.reason" :maxlength="500" aria-label="确认或修改原因"/></el-form-item>
      <el-form-item label="启用"><el-switch v-model="form.enabled" aria-label="启用广告样本"/></el-form-item>
    </el-form>
    <template #footer><el-alert v-if="error" :title="error" type="error" :closable="false"/>
      <el-button :disabled="busy" @click="closeSample">取消</el-button><el-button type="primary" :loading="busy" :disabled="!form.text.trim() || !form.reason.trim()" @click="save">保存样本</el-button></template>
  </el-dialog>
</template>

<style scoped>
.ad-tools { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 12px 0; }
.ad-tools .el-input { flex: 1; min-width: 180px; }
.ad-tools .el-select { width: 180px; }
.ad-text { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>

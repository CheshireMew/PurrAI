<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, time } from '../api'
import { useTask } from '../composables/useTask'
import { useRefresh } from '../composables/useRefresh'
import { useWorkspace } from '../workspace'
import { useDraftClose } from '../composables/useDraftClose'
import { parsePointsImport, pointsImportTemplate, pointsTemplateDownload } from '../pointsImport'

interface Account { id: string; display_name: string; user_id: number | null; balance: number; version: number; rank: number; source_rank: number | null; note: string; trust: { earned_points: number; active_days: number; level: number } }
interface Batch { id: string; batch_key: string; count: number; total_points: number; created_at: number }
interface Page { items: Account[]; total: number; summary: { accounts: number; points: number; pending: number }; imports: Batch[] }
interface Entry { id: string; kind: string; delta: number; balance_after: number; reason: string; actor_id: number; created_at: number; result: { previous_identity?: { display_name: string; user_id: number | null }; display_name: string; user_id: number | null } }
interface Settings { enabled: boolean; signin_points: number; chat_points: number; chat_daily_limit: number; chat_min_characters: number; chat_interval_seconds: number; packet_enabled: boolean; packet_expiry_hours: number; commands: Record<string, string[]>; trust_enabled: boolean; newcomer_exemption_points: number; advertising_exemption_points: number; trust_excluded_user_ids: number[] }
const props = defineProps<{ groupId: number; refreshKey: number }>()
const { mutations } = useWorkspace()
const { busy, error, safe } = useTask()
const data = ref<Page>({ items: [], total: 0, summary: { accounts: 0, points: 0, pending: 0 }, imports: [] })
const loaded = ref(false), loadError = ref(''), appliedQuery = ref(''), appliedPending = ref(false)
let acceptedPage = 1
const emptyText = computed(() => loadError.value ? '积分账户加载失败，请重试' : !loaded.value ? '正在加载积分账户…'
  : appliedQuery.value || appliedPending.value ? '没有符合条件的积分账户，请调整筛选' : '还没有积分账户，可导入历史积分或新增账户')
const query = ref(''), pending = ref(false), page = ref(1)
const base = () => `/api/groups/${props.groupId}/points`
const settings = ref<{ config: Settings; version: number } | null>(null), settingsBaseline = ref('')
const settingsDirty = computed(() => !!settings.value && settingsSnapshot() !== settingsBaseline.value)
useDraftClose(() => settingsDirty.value, restoreSettings, () => busy.value, () => !!settings.value)
const excludedUserIds = ref('')
const trustLabels = ['常规审核', '新人限制豁免', 'AI 广告审核豁免']
const aliases = reactive<Record<string, string>>({ signin: '', balance: '', ranking: '', packet: '', add: '', subtract: '', lookup: '' })
const commandLabels: Record<string, string> = { signin: '签到口令', balance: '查询口令', ranking: '排行口令', packet: '红包口令', add: '管理员加分口令', subtract: '管理员扣分口令', lookup: '回复查询口令' }
function settingsSnapshot() { return JSON.stringify({ config: settings.value?.config, excluded: excludedUserIds.value, aliases }) }
function acceptSettings(value: { config: Settings; version: number }) {
  settings.value = value
  excludedUserIds.value = value.config.trust_excluded_user_ids.join('\n')
  for (const key in aliases) aliases[key] = (value.config.commands[key] || []).join('\n')
  settingsBaseline.value = settingsSnapshot()
}
function restoreSettings() {
  if (!settings.value || !settingsBaseline.value) return
  const baseline = JSON.parse(settingsBaseline.value)
  settings.value.config = baseline.config; excludedUserIds.value = baseline.excluded
  Object.assign(aliases, baseline.aliases)
}
let generation = 0
async function load() {
  const current = ++generation, candidate = { query: query.value, pending: pending.value, page: page.value }
  loadError.value = ''
  const success = await safe(async () => {
    const [result, config] = await Promise.all([
      api<Page>(`${base()}?${new URLSearchParams({ query: candidate.query, pending: String(candidate.pending), page_number: String(candidate.page) })}`),
      api<{ config: Settings; version: number }>(`${base()}/settings`),
    ])
    if (current === generation) {
      data.value = result
      loaded.value = true; appliedQuery.value = candidate.query; appliedPending.value = candidate.pending; acceptedPage = candidate.page
      if (!settingsDirty.value) acceptSettings(config)
    }
  }, () => current === generation)
  if (!success && current === generation) { loadError.value = error.value; if (loaded.value) page.value = acceptedPage }
}
function filter() { page.value = 1; void load() }
async function saveSettings() {
  await safe(async () => {
    const ids = excludedUserIds.value.trim().split(/\s+/).filter(Boolean)
    if (ids.some(value => !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) <= 0)) throw new Error('请每行填写一个有效的 Telegram 数字 ID')
    const config = { ...settings.value!.config, trust_excluded_user_ids: ids.map(Number), commands: Object.fromEntries(Object.entries(aliases).map(([key, value]) => [key, value.split('\n').map(s => s.trim()).filter(Boolean)])) }
    acceptSettings(await mutations.submit(`${base()}/settings`, 'PUT', { config, version: settings.value!.version }, undefined, '保存积分设置'))
    ElMessage.success('积分设置已保存')
  })
}
const mode = ref(''), selected = ref<Account | null>(null)
const form = reactive({ name: '', userId: '', delta: 0, reason: '' })
const formBaseline = ref('')
const { requestClose: closeAccount } = useDraftClose(() => JSON.stringify(form) !== formBaseline.value,
  () => { mode.value = ''; error.value = '' }, () => busy.value, () => !!mode.value)
function edit(kind: string, row: Account | null = null) {
  selected.value = row
  Object.assign(form, { name: row?.display_name || '', userId: row?.user_id?.toString() || '', delta: 0, reason: '' })
  formBaseline.value = JSON.stringify(form)
  error.value = ''; mode.value = kind
}
function numericId() {
  if (!form.userId.trim()) return null
  if (!/^\d+$/.test(form.userId.trim()) || !Number.isSafeInteger(Number(form.userId)) || Number(form.userId) <= 0) throw new Error('请填写有效的 Telegram 数字 ID')
  return Number(form.userId)
}
async function save() {
  const success = await safe(async () => {
    const row = selected.value
    if (mode.value === 'adjust') {
      if (!Number.isSafeInteger(form.delta) || !form.delta || !form.reason.trim()) throw new Error('请填写非零整数积分和原因')
      await mutations.submit(`${base()}/${row!.id}/adjust`, 'POST', { delta: form.delta, reason: form.reason, version: row!.version }, undefined, '调整积分')
    } else {
      const user_id = numericId()
      if (!form.name.trim() || (mode.value === 'create' && !user_id)) throw new Error('请填写昵称和 Telegram 数字 ID')
      if (row && !form.reason.trim()) throw new Error('请填写身份修改原因')
      await mutations.submit(row ? `${base()}/${row.id}/identity` : base(), row ? 'PUT' : 'POST',
        { display_name: form.name, user_id, ...(row ? { version: row.version, reason: form.reason } : {}) }, undefined, row ? '保存积分身份' : '创建积分账户')
    }
    ElMessage.success('已保存')
  })
  if (success) { mode.value = ''; await load() }
}
const historyOpen = ref(false), historyPage = ref(1), history = ref<{ account: Account; items: Entry[]; total: number } | null>(null)
async function showHistory(row?: Account) {
  if (row) { selected.value = row; historyPage.value = 1; history.value = null }
  historyOpen.value = true
  await safe(async () => { history.value = await api(`${base()}/${selected.value!.id}/history?page_number=${historyPage.value}`) })
}
const importOpen = ref(false), importBody = ref<any>(null), preview = ref<any>(null), importName = ref('')
const { requestClose: closeImport } = useDraftClose(() => !!preview.value && !preview.value.already_imported,
  () => { importOpen.value = false; error.value = '' }, () => busy.value, () => importOpen.value)
async function chooseFile(event: Event) {
  importBody.value = null; preview.value = null
  const file = (event.target as HTMLInputElement).files?.[0]
  importName.value = file?.name || ''
  if (!file) return
  await safe(async () => {
    if (file.size > 5 * 1024 * 1024) throw new Error('请选择不超过 5 MB 的 JSON 文件')
    const body = parsePointsImport(await file.text())
    const result = await api(`${base()}/import/preview`, { method: 'POST', body: JSON.stringify(body) })
    importBody.value = body; preview.value = result
  })
}
async function importData() {
  const success = await safe(async () => {
    await mutations.submit(`${base()}/import`, 'POST', importBody.value, undefined, '导入历史积分')
    ElMessage.success('历史积分已入账')
  })
  if (success) { importOpen.value = false; await load() }
}
const kindName: Record<string, string> = { import: '历史迁入', adjustment: '积分调整', identity: '身份修改', create: '开户', signin: '签到', chat: '聊天', packet_send: '发红包', packet_claim: '领红包', packet_refund: '红包退款' }
useRefresh(load, () => props.refreshKey)
</script>

<template>
<el-alert v-if="error && !mode && !historyOpen && !importOpen" :title="error" type="error" show-icon :closable="false" class="error-banner"/>
<p v-if="loadError && loaded" class="muted" role="status">刷新失败，仍显示上次成功查询的积分数据。</p>
<section class="points-summary">
  <div><span>积分总额</span><strong>{{ loaded ? data.summary.points.toLocaleString() : '—' }}</strong></div>
  <div><span>积分账户</span><strong>{{ loaded ? data.summary.accounts : '—' }}</strong></div>
  <div><span>待绑定身份</span><strong>{{ loaded ? data.summary.pending : '—' }}</strong></div>
</section>
<details v-if="settings" class="panel points-settings">
  <summary>积分设置{{ settings.config.enabled ? '' : ' · 未开启群内功能' }}</summary>
  <el-form label-position="top" class="points-config" @submit.prevent="saveSettings">
    <el-form-item label="群内积分功能"><el-switch v-model="settings.config.enabled" aria-label="群内积分功能"/></el-form-item>
    <el-form-item label="每日签到奖励"><el-input-number v-model="settings.config.signin_points" :min="0" :max="10000" :precision="0" aria-label="每日签到奖励"/></el-form-item>
    <el-form-item label="每条聊天加分"><el-input-number v-model="settings.config.chat_points" :min="0" :max="100" :precision="0" aria-label="每条聊天加分"/></el-form-item>
    <el-form-item label="聊天每日最多加分"><el-input-number v-model="settings.config.chat_daily_limit" :min="0" :max="10000" :precision="0" aria-label="聊天每日最多加分"/></el-form-item>
    <el-form-item label="聊天至少几个汉字"><el-input-number v-model="settings.config.chat_min_characters" :min="1" :max="100" :precision="0" aria-label="聊天至少几个汉字"/></el-form-item>
    <el-form-item label="两次聊天加分间隔（秒）"><el-input-number v-model="settings.config.chat_interval_seconds" :min="10" :max="3600" :precision="0" aria-label="聊天加分间隔"/></el-form-item>
    <el-form-item label="积分红包"><el-switch v-model="settings.config.packet_enabled" aria-label="积分红包"/></el-form-item>
    <el-form-item label="红包有效时间（小时）"><el-input-number v-model="settings.config.packet_expiry_hours" :min="1" :max="168" :precision="0" aria-label="红包有效时间"/></el-form-item>
    <el-form-item label="按历史积分逐级豁免"><el-switch v-model="settings.config.trust_enabled" aria-label="按历史积分逐级豁免"/></el-form-item>
    <el-form-item label="新人限制豁免积分"><el-input-number v-model="settings.config.newcomer_exemption_points" :min="1" :max="9007199254740991" :precision="0" aria-label="新人限制豁免积分"/></el-form-item>
    <el-form-item label="AI 广告审核豁免积分"><el-input-number v-model="settings.config.advertising_exemption_points" :min="settings.config.newcomer_exemption_points" :max="9007199254740991" :precision="0" aria-label="AI 广告审核豁免积分"/></el-form-item>
    <el-form-item label="取消自动豁免的成员 ID"><el-input v-model="excludedUserIds" type="textarea" :rows="2" aria-label="取消自动豁免的成员 ID" placeholder="每行一个 Telegram 数字 ID"/></el-form-item>
    <p class="muted config-wide">按本人在本群的历史最高积分判断，签到积分也计入；消费或扣分后仍保留资格。达到新人门槛解除新人限制，达到广告门槛后跳过 AI 广告审核及样本匹配。</p>
    <el-form-item v-for="(_, key) in aliases" :key="key" :label="commandLabels[key]"><el-input v-model="aliases[key]" type="textarea" :rows="2" :aria-label="commandLabels[key]" placeholder="每行一个口令，清空可停用"/></el-form-item>
    <p class="muted config-wide">每天按北京时间结算。奖励或每日上限设为 0 可关闭对应加分；转发、重复内容、违规消息不加分。红包格式：口令 总积分 份数，回复某位群友时为专属红包。</p>
    <p class="muted config-wide">管理员回复群友消息，发送加分或扣分口令及正整数即可调整积分，口令与数字之间的空格可省略。所有群友都可用回复查询口令查看对方，不回复则查自己。口令留空可停用。</p>
    <el-button type="primary" :loading="busy" native-type="submit">保存积分设置</el-button>
  </el-form>
</details>
<section class="panel" v-loading="busy">
  <div class="panel-title"><h2>积分榜</h2><div><el-button @click="importOpen = true; importBody = null; preview = null; importName = ''; error = ''">导入历史积分</el-button><el-button type="primary" @click="edit('create')">新增账户</el-button></div></div>
  <form class="filterbar" @submit.prevent="filter">
    <el-input v-model="query" aria-label="搜索积分账户" placeholder="昵称或 Telegram ID" clearable/>
    <el-checkbox v-model="pending" @change="filter">只看待绑定</el-checkbox>
    <el-button native-type="submit">查询</el-button>
  </form>
  <div class="points-cards">
    <article v-for="row in data.items" :key="row.id">
      <div class="points-card-heading"><strong>{{ row.rank }}. {{ row.display_name || '未显示昵称' }}</strong><b>{{ row.balance.toLocaleString() }} 分</b></div>
      <p class="muted">{{ row.user_id || '待绑定身份' }}<span v-if="row.source_rank"> · 原排名 {{ row.source_rank }}</span></p>
      <p>历史最高 {{ row.trust.earned_points }} 分 · {{ trustLabels[row.trust.level] }}</p>
      <p v-if="row.note" class="points-note">{{ row.note }}</p>
      <div><el-button text @click="showHistory(row)">流水</el-button><el-button text @click="edit('adjust', row)">增减</el-button><el-button text @click="edit('identity', row)">{{ row.user_id ? '身份' : '绑定' }}</el-button></div>
    </article>
    <el-empty v-if="!data.items.length" :description="emptyText"/>
  </div>
  <el-table class="points-table" :data="data.items" row-key="id" :empty-text="emptyText">
    <el-table-column prop="rank" label="排名" width="70"/>
    <el-table-column label="群友" min-width="210"><template #default="{ row }"><strong>{{ row.display_name || '未显示昵称' }}</strong><div v-if="row.note" class="muted points-note">{{ row.note }}</div></template></el-table-column>
    <el-table-column label="积分" width="110" align="right"><template #default="{ row }"><strong>{{ row.balance.toLocaleString() }}</strong></template></el-table-column>
    <el-table-column label="历史积分与豁免" min-width="180"><template #default="{ row }">历史最高 {{ row.trust.earned_points }} 分<div>{{ trustLabels[row.trust.level] }}</div></template></el-table-column>
    <el-table-column label="Telegram ID" min-width="150"><template #default="{ row }">{{ row.user_id || '待绑定' }}</template></el-table-column>
    <el-table-column prop="source_rank" label="原排名" width="80"/>
    <el-table-column label="操作" width="235" fixed="right"><template #default="{ row }"><span class="points-actions"><el-button text @click="showHistory(row)">流水</el-button><el-button text @click="edit('adjust', row)">增减</el-button><el-button text @click="edit('identity', row)">{{ row.user_id ? '身份' : '绑定' }}</el-button></span></template></el-table-column>
  </el-table>
  <el-pagination v-if="loaded" v-model:current-page="page" :page-size="20" :total="data.total" layout="prev, pager, next, total" @current-change="load"/>
</section>
<section v-if="data.imports.length" class="panel">
  <h2>迁入记录</h2>
  <el-table :data="data.imports"><el-table-column prop="batch_key" label="批次" min-width="230"/><el-table-column prop="count" label="人数" width="80"/><el-table-column prop="total_points" label="积分" width="110"/><el-table-column label="迁入时间" min-width="180"><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column></el-table>
</section>
<el-dialog :model-value="!!mode" :title="mode === 'adjust' ? '增减积分' : mode === 'create' ? '新增积分账户' : '绑定或纠正身份'" class="form-dialog" width="min(540px, 94vw)" :before-close="closeAccount" :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @close="mode = ''">
  <el-form label-position="top" @submit.prevent="save">
    <p v-if="selected">{{ selected.display_name || '未显示昵称' }} · 当前 {{ selected.balance }} 积分</p>
    <template v-if="mode === 'adjust'">
      <el-form-item label="变动积分"><el-input-number v-model="form.delta" :precision="0" aria-label="变动积分"/></el-form-item>
      <p class="muted">正数增加，负数扣除。保存后为 {{ (selected?.balance || 0) + (form.delta || 0) }} 积分。</p>
    </template>
    <template v-else>
      <el-form-item label="昵称"><el-input v-model="form.name" maxlength="250" aria-label="积分账户昵称"/></el-form-item>
      <el-form-item label="Telegram 数字 ID"><el-input v-model="form.userId" inputmode="numeric" aria-label="Telegram 数字 ID"/></el-form-item>
      <p v-if="mode === 'identity'" class="muted">核实本人后填写数字 ID；清空可解除绑定。同名账户不会自动合并。</p>
    </template>
    <el-form-item v-if="mode !== 'create'" label="原因"><el-input v-model="form.reason" type="textarea" maxlength="1000" aria-label="修改原因"/></el-form-item>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  </el-form>
  <template #footer><el-button :disabled="busy" @click="closeAccount">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存</el-button></template>
</el-dialog>
<el-dialog v-model="historyOpen" title="积分流水" class="form-dialog" width="min(900px, 96vw)">
  <template v-if="history"><p>{{ history.account.display_name || '未显示昵称' }} · 当前 {{ history.account.balance }} 积分</p>
  <el-table :data="history.items" v-loading="busy">
    <el-table-column label="时间" min-width="170"><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column>
    <el-table-column label="类型" width="100"><template #default="{ row }">{{ kindName[row.kind] || row.kind }}</template></el-table-column>
    <el-table-column label="变动" width="80"><template #default="{ row }">{{ row.delta > 0 ? '+' : '' }}{{ row.delta }}</template></el-table-column>
    <el-table-column prop="balance_after" label="变动后" width="90"/>
    <el-table-column label="原因" min-width="220"><template #default="{ row }">{{ row.reason }}<div v-if="row.result.previous_identity" class="muted">{{ row.result.previous_identity.display_name || '无昵称' }} / {{ row.result.previous_identity.user_id || '未绑定' }} → {{ row.result.display_name }} / {{ row.result.user_id || '未绑定' }}</div></template></el-table-column>
    <el-table-column label="操作人" min-width="100"><template #default="{ row }">{{ row.actor_id || '本地 CLI' }}</template></el-table-column>
  </el-table><el-pagination v-model:current-page="historyPage" :page-size="20" :total="history.total" layout="prev, pager, next" @current-change="showHistory()"/></template>
  <el-alert v-if="error" :title="error" type="error" :closable="false"/>
</el-dialog>
<el-dialog v-model="importOpen" title="导入历史积分" class="form-dialog" width="min(600px, 94vw)" :before-close="closeImport" :close-on-click-modal="false" :show-close="!busy" :close-on-press-escape="!busy">
  <p>选择积分 JSON 文件，先核对人数与总额。同一批次重复提交不会重复加分。</p>
  <p><a class="import-template" :href="pointsTemplateDownload" download="points-import-template.json">下载 JSON 模板</a></p>
  <details class="import-format"><summary>查看文件格式与必填项</summary><p>最外层填写唯一批次编号 batch_key、数据来源 source 和积分记录列表 rows。同一批数据重试时保留原批次编号。</p>
  <pre>{{ JSON.stringify(pointsImportTemplate, null, 2) }}</pre><p>每条记录必填来源编号 source_key、昵称 display_name 和非负整数积分 points。数字 ID user_id、原排名 source_rank 和备注 note 为选填；未知 ID 留空值 null，不凭同名合并。</p></details>
  <input :key="String(importOpen)" type="file" accept=".json,application/json" aria-label="积分导入文件" :disabled="busy" @change="chooseFile"/>
  <p class="muted">每条记录包含来源编号、昵称和积分；仅凭昵称导入的账户保留为待绑定。</p>
  <div v-if="preview" role="status"><p>{{ importName }}</p><strong>{{ preview.count }} 人 · {{ preview.total_points.toLocaleString() }} 积分</strong><p>{{ preview.already_imported ? '这批数据已入账，无需重复导入。' : '核对无误后即可入账。' }}</p></div>
  <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  <template #footer><el-button :disabled="busy" @click="closeImport">取消</el-button><el-button type="primary" :disabled="!preview || preview.already_imported" :loading="busy" @click="importData">确认导入</el-button></template>
</el-dialog>
</template>

<style scoped>
.points-summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
.points-summary > div { padding: 22px; background: var(--el-bg-color); border: 1px solid var(--el-border-color-light); border-radius: 12px; }
.points-summary span { color: var(--el-text-color-secondary); display: block; margin-bottom: 10px; }
.points-summary strong { font-size: 28px; font-variant-numeric: tabular-nums; }
.points-note { font-size: 12px; line-height: 1.5; margin-top: 5px; }
.points-settings summary { cursor: pointer; font-weight: 600; }
.points-config { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px 24px; margin-top: 24px; }
.config-wide { grid-column: 1 / -1; }
.points-actions { display: flex; white-space: nowrap; }
.points-cards { display: none; }
.points-cards article { padding: 16px 0; border-bottom: 1px solid var(--el-border-color-light); }
.points-card-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.points-card-heading strong { overflow-wrap: anywhere; }
.points-card-heading b { white-space: nowrap; }
@media(max-width: 600px) { .points-table { display: none; } .points-cards { display: block; } }
@media(max-width: 600px) { .points-summary { gap: 8px; } .points-summary > div { padding: 12px 8px; } .points-summary strong { font-size: 22px; } .points-summary span { font-size: 12px; } .panel-title { align-items: flex-start; gap: 12px; flex-wrap: wrap; } }
</style>

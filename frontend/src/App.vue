<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Grid, ChatDotRound, Key, Filter, Warning, User, Setting, Refresh, SwitchButton, CircleCheck } from '@element-plus/icons-vue'
import { api, ApiError, label, post, put, setCsrf, time } from './api'
import CatMark from './CatMark.vue'
import MessageArchive from './MessageArchive.vue'

type Row = Record<string, any>
const me = ref<Row | null>(null)
const starting = ref(true), busy = ref(false), error = ref('')
const page = ref('overview'), selected = ref<number | null>(null), groups = ref<Row[]>([])
const stats = ref<Row>({}), rows = ref<Row[]>([]), system = ref<Row>({}), total = ref(0)
const pagination = ref(1), statusFilter = ref(''), userFilter = ref(''), dates = ref<[Date, Date] | null>(null)
const editing = ref(''), dialog = ref(false), editingId = ref('')
const form = reactive<Row>({})
const settingsBaseline = ref<Row>({})
const emojiPacksText = ref('')
const chatPacksText = ref(''), antifraudPacksText = ref('')
const chatStatus = ref<Row>({})
const previewText = ref(''), previewReasons = ref<string[] | null>(null)
const previewNew = ref(false), previewContact = ref(false), previewRepeats = ref(0), previewSimilar = ref(0), previewBurst = ref(0), previewName = ref('')
const actions = ref<Row[]>([])
const history = ref<Row[]>([])
const operationNames: Record<string, string> = { activate: '启用保护', settings: '修改配置', rule: '修改规则', whitelist: '白名单', release: '人工放行', verify_mute: '重新禁言验证', challenge: '重新出题', ban: '临时封禁', manual_mute: '管理员临时禁言', manual_ban: '管理员临时封禁', profile_mute: '昵称命中永久禁言', emoji_status_check: '检查状态表情', emoji_status_mute: '状态表情命中永久禁言', join_link: '生成申请入群链接', verification_failure: '验证失败处理', admin_notify: 'Bot 执行回执', backup: '数据库备份', unban: '解除封禁', unmute: '解除禁言', revoke_warning: '撤销警告', batch_release: '批量提交', retry: '重试操作', retry_event: '重试事件' }
const nav = [
  { id: 'overview', name: '总览', icon: Grid, detail: '看看群里发生了什么' },
  { id: 'groups', name: '群管理', icon: ChatDotRound, detail: '让每个群都有合适的保护' },
  { id: 'verifications', name: '入群验证', icon: Key, detail: '从进群的第一步开始把关' },
  { id: 'rules', name: '防广告规则', icon: Filter, detail: '把规则讲清楚，把打扰挡下来' },
  { id: 'violations', name: '违规记录', icon: Warning, detail: '每次处理，都有原因可查' },
  { id: 'messages', name: '消息归档', icon: ChatDotRound, detail: '保留消息与编辑版本，方便查询和后续分析' },
  { id: 'whitelist', name: '白名单', icon: CircleCheck, detail: '给信任的成员留一条通道' },
  { id: 'admins', name: '权限说明', icon: User, detail: '群管理员自动管理本群，超级管理员负责全局维护' },
  { id: 'system', name: '系统状态', icon: Setting, detail: '连接、任务和备份，一眼看清' },
]
const navigation = computed(() => nav.filter(item => me.value?.root || !['admins', 'system'].includes(item.id)))
const current = computed(() => nav.find(item => item.id === page.value)!)
const group = computed(() => groups.value.find(g => g.id === selected.value))
const needsGroup = computed(() => ['verifications', 'rules', 'violations', 'whitelist', 'messages'].includes(page.value))
const numericSettings = [
  ['verification_timeout', '验证时限（秒）', 30, 3600], ['max_attempts', '最多错误次数', 1, 10],
  ['failure_ban_seconds', '手动封禁及申请重试间隔（秒）', 30, 86400], ['newcomer_seconds', '新成员限制期（秒）', 0, 86400], ['newcomer_messages', '前几条消息继续观察（0 关闭）', 0, 100],
  ['media_min_text_messages', '发图及表情前独立文字条数（0 关闭）', 0, 100],
  ['repeat_window', '重复检测窗口（秒）', 5, 3600], ['repeat_count', '重复消息触发条数', 2, 100],
  ['similar_window', '近似内容窗口（秒）', 5, 3600], ['similar_count', '近似内容触发条数', 2, 100],
  ['similar_threshold', '近似程度（百分比）', 70, 100], ['similar_min_length', '近似检测最短长度', 8, 200],
  ['burst_window', '连续发送窗口（秒）', 2, 300], ['burst_count', '连续发送触发条数', 3, 100],
  ['mute_seconds', '手动禁言（秒，默认 3 天）', 30, 2592000],
] as const
const boolSettings = [['archive_enabled', '记录群消息归档'], ['join_requests', '申请入群先验证'], ['verification_enabled', '新成员图片验证'], ['newcomer_links', '限制期禁止链接'],
  ['newcomer_contacts', '限制期禁止联系人'], ['newcomer_mentions', '观察期限制 @提及'], ['repeat_enabled', '重复刷屏检测'], ['block_invites', '拦截群邀请链接'], ['similar_enabled', '近似内容刷屏检测'], ['burst_enabled', '连续发送检测']]
let timer: ReturnType<typeof setInterval> | undefined
let requestVersion = 0

async function safe(task: () => Promise<void>) {
  error.value = ''
  busy.value = true
  try { await task() } catch (e) {
    error.value = e instanceof Error ? e.message : '操作失败，请重试'
    if (e instanceof ApiError && e.status === 401) me.value = null
  } finally { busy.value = false }
}
async function load() {
  const version = ++requestVersion
  const section = page.value, id = selected.value
  await safe(async () => {
    let data: any
    if (section === 'overview') {
      data = await api('/api/dashboard'); if (version === requestVersion) stats.value = data
    } else if (section === 'groups') {
      data = await api('/api/groups'); if (version === requestVersion) groups.value = data
    } else if (section === 'admins') {
      data = await api('/api/admins'); if (version === requestVersion) rows.value = data.superadmin_ids.map((id: number) => ({ id }))
    } else if (section === 'system') {
      data = await api('/api/system'); if (version === requestVersion) system.value = data
    } else if (section === 'messages') { rows.value = []
    } else if (id !== null) {
      const query = new URLSearchParams({ page_number: String(pagination.value), state: statusFilter.value })
      if (userFilter.value.trim()) query.set('user_id', userFilter.value.trim())
      if (dates.value) { query.set('after', String(+dates.value[0] / 1000)); query.set('before', String(+dates.value[1] / 1000)) }
      data = await api(`/api/groups/${id}/${section}?${query}`)
      if (version === requestVersion) { rows.value = Array.isArray(data) ? data : data.items; total.value = data.total || 0 }
      if (section === 'verifications') { const activity = await api(`/api/groups/${id}/actions`); if (version === requestVersion) history.value = activity.items }
    } else { rows.value = [] }
  })
}
watch([page, selected], () => { pagination.value = 1; statusFilter.value = ''; userFilter.value = ''; dates.value = null; rows.value = []; load() })
async function filters() { pagination.value = 1; await load() }
function edit(kind: string, row: Row = {}) {
  Object.keys(form).forEach(k => delete form[k])
  previewReasons.value = null
  editing.value = kind; editingId.value = row.id || ''
  const defaults: Record<string, Row> = { rule: { kind: 'keyword', value: '', mode: 'contains', scope: 'message', context_value: '', enabled: true },
    whitelist: { user_id: '', verification: true, moderation: true, note: '' },
    settings: { verification_failure_action: 'permanent_mute', media_min_text_messages: 3, emoji_status_blocked_packs: ['bygbb666', 'gshechaba'], chat_enabled: false, chat_proactive: true,
      chat_name: '柴郡喵', chat_persona: '随和、有一点幽默，关心群友，有自己的看法。', chat_history: 40, chat_interval: 90,
      chat_stickers: true, chat_sticker_interval: 60, chat_antifraud: true, chat_antifraud_interval: 21600 } }
  Object.assign(form, JSON.parse(JSON.stringify(kind === 'settings' ? { ...defaults.settings, ...row.settings } : { ...defaults[kind], ...row })))
  if (kind === 'settings') editingId.value = String(row.id)
  dialog.value = true
  if (kind === 'settings') {
    settingsBaseline.value = JSON.parse(JSON.stringify(form))
    emojiPacksText.value = ''
    chatPacksText.value = (form.chat_sticker_packs || []).join('\n')
    antifraudPacksText.value = (form.chat_antifraud_packs || []).join('\n')
    chatStatus.value = {}
    const id = String(row.id)
    api(`/api/groups/${id}/chat`).then(data => { if (editingId.value === id) chatStatus.value = data })
      .catch(() => { if (editingId.value === id) chatStatus.value = { error: '暂时无法读取聊天服务状态' } })
  }
}
async function changeEmojiPacks(operation: 'add' | 'remove' | 'clear', pack?: string) {
  if (operation === 'clear') {
    try { await ElMessageBox.confirm('清空本群的状态表情包黑名单？已有禁言不会自动解除。', '清空名单', { confirmButtonText: '确认清空', cancelButtonText: '取消' }) }
    catch { return }
  }
  const id = editingId.value
  await safe(async () => {
    const packs = operation === 'add' ? emojiPacksText.value.split(/[\s,，]+/).filter(Boolean) : pack ? [pack] : []
    if (operation === 'add' && !packs.length) throw new Error('请填写要新增的表情包链接')
    const result = await post(`/api/groups/${id}/emoji-status-packs`, { operation, packs, request_id: crypto.randomUUID() })
    if (editingId.value === id) {
      form.emoji_status_blocked_packs = result.packs
      settingsBaseline.value.emoji_status_blocked_packs = [...result.packs]
      if (operation === 'add') emojiPacksText.value = ''
    }
    const current = groups.value.find(row => String(row.id) === id)
    if (current) current.settings.emoji_status_blocked_packs = result.packs
    ElMessage.success(operation === 'add' ? result.added.length ? `已新增 ${result.added.length} 个，原名单已保留` : '已在名单中，无需重复添加' : operation === 'remove' ? '已移除' : '已清空名单')
  })
}
async function resetChat() {
  await safe(async () => {
    await post(`/api/groups/${editingId.value}/chat/reset`)
    ElMessage.success('已重新开始聊天；消息归档仍然保留')
  })
}
async function save() {
  await safe(async () => {
    if (editing.value === 'rule') {
      if (!String(form.value || '').trim()) throw new Error('请填写规则内容')
      const path = `/api/groups/${selected.value}/rules${editingId.value ? '/' + editingId.value : ''}`
      await (editingId.value ? put(path, form) : post(path, form))
    } else if (editing.value === 'settings') {
      if (emojiPacksText.value.trim()) throw new Error('请先点击“新增表情包”保存输入的链接，或清空新增输入框')
      form.chat_sticker_packs = chatPacksText.value.split(/[\s,，]+/).filter(Boolean)
      form.chat_antifraud_packs = antifraudPacksText.value.split(/[\s,，]+/).filter(Boolean)
      const changes = Object.fromEntries(Object.entries(form).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(settingsBaseline.value[key])))
      const baseline = Object.fromEntries(Object.keys(changes).map(key => [key, settingsBaseline.value[key]]))
      await api(`/api/groups/${editingId.value}/settings`, { method: 'PATCH', body: JSON.stringify({ settings: changes, baseline }) })
      groups.value = await api('/api/groups')
    } else if (editing.value === 'whitelist') {
      if (!/^\d+$/.test(String(form.user_id))) throw new Error('请填写有效的 Telegram 用户 ID')
      await put(`/api/groups/${selected.value}/whitelist`, { ...form, user_id: Number(form.user_id) })
    }
    dialog.value = false; ElMessage.success('已保存')
  })
  if (!dialog.value) await load()
}
async function confirmAction(path: string, action: string, title: string) {
  let reason: string
  try { reason = (await ElMessageBox.prompt('请填写处理原因，操作会保留记录。', title,
    { inputValidator: (s: string) => Boolean(s?.trim()) || '请填写原因', confirmButtonText: '提交操作', cancelButtonText: '取消' })).value }
  catch { return }
  await safe(async () => {
    const result = await post(path, { action, reason, request_id: crypto.randomUUID() })
    if (result.action_ids) { actions.value.unshift(...await Promise.all(result.action_ids.map((id: string) => api(`/api/actions/${id}`)))); ElMessage.success(`已提交 ${result.action_ids.length} 个放行任务`) }
    else { actions.value.unshift(result); ElMessage.success(result.state === 'succeeded' ? '操作已完成' : '操作已提交，执行结果将在下方更新') }
  })
  await load()
}
async function groupAction(row: Row, action: string) {
  if (action === 'revoke') {
    try { await ElMessageBox.confirm('撤销后将停止此群的聊天和管理任务，已有禁言不会自动解除。', '撤销群接入', { confirmButtonText: '撤销接入', cancelButtonText: '取消', type: 'warning' }) }
    catch { return }
  }
  await safe(async () => {
    if (action === 'approve' || action === 'revoke') await post(`/api/groups/${row.id}/access`, { approved: action === 'approve' })
    else if (action === 'join-link') { const task = await post(`/api/groups/${row.id}/join-link`, { action: 'release', reason: '生成申请入群链接', request_id: crypto.randomUUID() }); actions.value.unshift(task) }
    else if (action === 'activate') await post(`/api/groups/${row.id}/activate`)
    else if (action === 'permissions') { const data = await post(`/api/groups/${row.id}/permissions`); ElMessage.info(data.ok ? '机器人权限完整' : `缺少权限：${data.missing.join('、')}`) }
    else await api(`/api/groups/${row.id}/settings`, { method: 'PATCH', body: JSON.stringify({ enabled: false }) })
    groups.value = await api('/api/groups')
  })
}
async function preview() {
  await safe(async () => { const data = await post(`/api/groups/${selected.value}/rules/preview`,
    { text: previewText.value, newcomer: previewNew.value, contact: previewContact.value, repeats: previewRepeats.value, similar: previewSimilar.value, burst: previewBurst.value, sender_name: previewName.value }); previewReasons.value = [...data.reasons, ...(data.mute_member ? ['将禁言成员'] : []), ...(data.delete_message ? ['将删除消息'] : [])] })
}
async function previewDraft() {
  await safe(async () => {
    const draft = rows.value.filter(r => r.id !== editingId.value).concat({ ...form })
    const result = await post(`/api/groups/${selected.value}/rules/preview`, { text: previewText.value, rules: draft, newcomer: previewNew.value, contact: previewContact.value, repeats: previewRepeats.value, similar: previewSimilar.value, burst: previewBurst.value, sender_name: previewName.value })
    previewReasons.value = [...result.reasons, ...(result.mute_member ? ['将禁言成员'] : []), ...(result.delete_message ? ['将删除消息'] : [])]
  })
}
async function removeWhite(row: Row) {
  try { await ElMessageBox.confirm(`将 ${row.user_id} 移出白名单？`, '移出白名单', { confirmButtonText: '移出', cancelButtonText: '取消' }) } catch { return }
  await safe(async () => { await api(`/api/groups/${selected.value}/whitelist/${row.user_id}`, { method: 'DELETE' }) }); await load()
}
async function systemAction(path: string) { await safe(async () => { await post(path); ElMessage.success('操作已提交') }); await load() }
async function logout() { await safe(async () => { await post('/auth/logout'); me.value = null }) }
onMounted(async () => {
  try { me.value = await api('/api/me'); setCsrf(me.value!.csrf); groups.value = await api('/api/groups'); selected.value = groups.value[0]?.id ?? null; await load() }
  catch (e) { if (!(e instanceof ApiError && e.status === 401)) error.value = (e as Error).message }
  finally { starting.value = false }
  const loginError = new URLSearchParams(location.search).get('login_error'); if (loginError) error.value = loginError
  timer = setInterval(async () => {
    if (!me.value || busy.value || dialog.value) return
    for (const item of actions.value.filter(a => a.state === 'pending')) {
      try { Object.assign(item, await api(`/api/actions/${item.id}`)) } catch { /* Errors remain visible on explicit refresh. */ }
    }
    if (['overview', 'verifications', 'system'].includes(page.value)) await load()
  }, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div v-if="starting" class="startup">正在连接 PurrAI…</div>
  <main v-else-if="!me" class="login-wrap">
    <section class="login-story"><div class="wordmark"><CatMark class="cat"/> PurrAI</div><span class="eyebrow">给讨论留一点安静</span><h1>群里的热闹，<br>交给真正的成员。</h1><p>从入群验证到广告处理，<br>让每一次管理都有迹可循。</p><div class="story-foot">入群验证 <i>·</i> 群管理 <i>·</i> 广告拦截</div></section>
    <section class="login-card"><span class="eyebrow">管理员工作台</span><h2>欢迎回来</h2><p>这里是可选网页后台。也可私聊机器人发送 /admin，直接在 Telegram 管理群。</p><el-alert v-if="error" :title="error" type="error" :closable="false" show-icon/><a href="/auth/telegram/start" class="telegram-login"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="m21 3-3.4 17-5.3-4-2.8 2.7.6-4.6 8.3-7.5-10.3 6.5-5-1.6z"/></svg> 使用 Telegram 登录</a><small>仅限群管理员和超级管理员。群权限按 Telegram 当前身份确认。</small><div class="login-rule"></div><p class="muted">PurrAI · 自己的群，自己的规则</p></section>
  </main>
  <div v-else class="workspace">
    <aside class="sidebar"><a class="wordmark" href="/" aria-label="PurrAI 首页"><CatMark class="cat"/> PurrAI</a><div class="sidebar-caption">群管理工作台</div><nav><button v-for="item in navigation" :key="item.id" :class="{ active: page === item.id }" @click="page = item.id"><el-icon><component :is="item.icon"/></el-icon><span>{{ item.name }}</span></button></nav><div class="sidebar-bottom"><span class="live-dot"></span> 按自己的规则守护群聊<div>{{ me.root ? '超级管理员' : '群管理员' }} · {{ me.id }}</div></div></aside>
    <div class="main-area"><header class="topbar"><span>PurrAI <span class="slash">/</span> {{ current.name }}</span><div class="top-actions"><el-select v-model="selected" placeholder="选择群" aria-label="选择群" style="width: 220px"><el-option v-for="g in groups" :key="g.id" :value="g.id" :label="g.title"/></el-select><el-button :icon="Refresh" circle aria-label="刷新" @click="load"/><el-button :icon="SwitchButton" circle aria-label="退出登录" @click="logout"/></div></header>
      <main class="content"><div class="page-heading"><div><span class="eyebrow">{{ needsGroup ? group?.title || '请选择群' : 'PURRAI WORKSPACE' }}</span><h1>{{ current.name }}</h1><p>{{ current.detail }}</p></div><span class="timezone">时间均为北京时间</span></div>
        <el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
        <el-alert v-if="needsGroup && !selected" title="还没有可以管理的群。请先将机器人加入超级群，再由本群管理员启用保护。" type="info" :closable="false"/>
        <section v-if="page === 'overview'" v-loading="busy"><div class="stat-grid"><article v-for="(title, key) in { active_groups: '保护中的群', pending: '等待完成验证', passed_today: '24 小时验证通过', violations_today: '24 小时广告记录' }" :key="key" class="stat-card"><span>{{ title }}</span><strong>{{ stats[key] ?? '—' }}</strong><small>{{ key === 'pending' ? '包含等待禁言、放行和失败处理的任务' : '按你有权限管理的群统计' }}</small></article></div><div class="overview-grid"><section class="panel protection-card"><div class="panel-title"><h2>群保护概况</h2><el-button text @click="page = 'groups'">管理群 →</el-button></div><el-empty v-if="!groups.length" description="机器人加入群后会显示在这里"/><div v-for="g in groups" :key="g.id" class="group-summary"><span class="group-avatar">{{ g.title?.slice(0, 1) || '群' }}</span><div><strong>{{ g.title }}</strong><small>{{ g.id }}</small></div><el-tag :type="g.status === 'active' ? 'success' : g.status === 'error' ? 'danger' : 'info'">{{ label(g.status) }}</el-tag></div></section><section class="panel attention-card"><span class="eyebrow">需要留意</span><h2>让处理有始有终</h2><p>未完成的验证和失败操作会持续保留。先查看原因，再决定重新出题或人工放行。</p><div class="attention-numbers"><div><strong>{{ stats.failed_actions ?? '—' }}</strong><span>失败操作</span></div><div><strong>{{ stats.failed_today ?? '—' }}</strong><span>24 小时验证失败</span></div></div><el-button @click="page = 'verifications'">查看验证记录</el-button></section></div></section>
        <section v-else-if="page === 'groups'" class="panel" v-loading="busy"><div class="panel-title"><h2>已发现的群 <small>{{ groups.length }} 个</small></h2><span class="muted">仅限所有者批准的群；批准后由群管理员启用保护</span></div><el-table :data="groups" empty-text="请先将机器人加入超级群"><el-table-column label="群" min-width="200"><template #default="{ row }"><strong>{{ row.title }}</strong><div class="cell-sub">{{ row.id }} · {{ row.type }}</div></template></el-table-column><el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.approved ? label(row.status) : '等待所有者批准' }}</el-tag></template></el-table-column><el-table-column label="操作" min-width="330"><template #default="{ row }"><el-button v-if="me?.root && !row.approved" size="small" type="primary" @click="groupAction(row, 'approve')">批准接入</el-button><template v-if="row.approved"><el-button v-if="!row.enabled" size="small" type="primary" @click="groupAction(row, 'activate')">启用保护</el-button><el-button v-if="row.enabled" size="small" @click="groupAction(row, 'pause')">暂停保护</el-button><el-button size="small" @click="groupAction(row, 'permissions')">检查权限</el-button><el-button size="small" @click="edit('settings', row)">群设置</el-button><el-button v-if="row.settings.join_requests" size="small" @click="groupAction(row, 'join-link')">申请入群链接</el-button></template><el-button v-if="me?.root && row.approved" size="small" type="danger" @click="groupAction(row, 'revoke')">撤销接入</el-button></template></el-table-column></el-table></section>
        <section v-else-if="page === 'verifications' && selected" class="panel" v-loading="busy"><div class="panel-title"><h2>验证记录</h2><el-button @click="confirmAction(`/api/groups/${selected}/release-pending`, 'release', '批量放行待验证成员')">批量放行</el-button></div><div class="filterbar"><el-input v-model="userFilter" placeholder="Telegram 用户 ID" aria-label="筛选用户 ID" clearable/><el-select v-model="statusFilter" placeholder="全部状态" clearable aria-label="筛选验证状态"><el-option v-for="s in ['pending', 'muting', 'releasing', 'removing', 'failing', 'passed', 'failed', 'left']" :key="s" :value="s" :label="label(s)"/></el-select><el-date-picker v-model="dates" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间"/><el-button @click="filters">查询</el-button></div><el-table :data="rows" empty-text="暂无验证记录"><el-table-column label="成员" min-width="160"><template #default="{ row }">{{ row.user_name }}<div class="cell-sub">{{ row.user_id }}</div></template></el-table-column><el-table-column label="状态" width="140"><template #default="{ row }"><el-tag>{{ label(row.state) }}</el-tag></template></el-table-column><el-table-column label="截止时间" min-width="170"><template #default="{ row }">{{ time(row.deadline) }}</template></el-table-column><el-table-column prop="attempts" label="错误次数" width="90"/><el-table-column label="处理说明" min-width="180"><template #default="{ row }">{{ { permanent_mute: '永久禁言，留在群里', kick: '已踢出，可重新加入', decline: '已拒绝申请', legacy_ban: '旧版或手动临时封禁' }[row.failure_action as string] }}<div>{{ row.error }}</div></template></el-table-column><el-table-column label="操作" min-width="260"><template #default="{ row }"><template v-if="['muting', 'pending', 'releasing', 'removing', 'failing'].includes(row.state)"><el-button size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'reissue', '重新出题')">重出题</el-button><el-button size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'release', '人工放行')">放行</el-button><el-button size="small" type="danger" plain @click="confirmAction(`/api/verifications/${row.id}/actions`, 'ban', '临时封禁')">封禁</el-button></template><template v-else-if="row.state === 'failed' && row.failure_action === 'permanent_mute'"><el-button size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'release', '人工放行永久禁言')">放行</el-button><el-button size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'reissue', '重新出题')">重出题</el-button></template><el-button v-else-if="row.state === 'failed' && ['decline', 'legacy_ban'].includes(row.failure_action)" size="small" @click="confirmAction(`/api/verifications/${row.id}/actions`, 'unban', '解除 PurrAI 入群限制')">解除入群限制</el-button><span v-else-if="row.failure_action === 'kick'" class="muted">已踢出，可重新加入</span><span v-else class="muted">已结束</span></template></el-table-column></el-table><el-pagination v-model:current-page="pagination" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load"/></section>
        <template v-else-if="page === 'rules' && selected"><section class="panel" v-loading="busy"><div class="panel-title"><h2>本群广告规则</h2><el-button type="primary" @click="edit('rule')">添加规则</el-button></div><el-table :data="rows" empty-text="还没有关键词或域名规则，可先添加规则并在下方测试"><el-table-column label="类型" width="150"><template #default="{ row }">{{ { keyword: '关键词', block_domain: '禁止域名', allow_domain: '允许域名', block_mention: '禁止引流账号', allow_mention: '允许提及账号' }[row.kind as string] }}</template></el-table-column><el-table-column prop="value" label="内容" min-width="200"/><el-table-column label="匹配" width="120"><template #default="{ row }">{{ row.kind === 'keyword' ? ({ contains: '包含匹配', exact: '完整匹配', compact: '忽略干扰', pinyin: '谐音匹配' }[row.mode as string] || row.mode) : row.kind.endsWith('mention') ? '账号匹配' : '域名与子域名' }}</template></el-table-column><el-table-column label="范围" min-width="160"><template #default="{ row }">{{ { message: '正文，只删消息', name: '昵称，删消息并永久禁言', either: '昵称或正文，各自处置', name_and_message: '昵称＋正文组合' }[(row.scope || 'message') as string] }}</template></el-table-column><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column><el-table-column label="操作" width="100"><template #default="{ row }"><el-button text @click="edit('rule', row)">编辑</el-button></template></el-table-column></el-table></section><section class="panel preview-panel"><h2>试一条消息</h2><p class="muted">只查看命中原因，不发送消息或执行处罚。域名白名单不会豁免关键词检查。</p><el-input v-model="previewName" aria-label="测试昵称及用户名" placeholder="测试昵称 @username"/><el-input v-model="previewText" type="textarea" :rows="3" placeholder="输入要测试的消息，例如包含链接的推广文案" aria-label="规则测试消息"/><div class="preview-options"><el-checkbox v-model="previewNew">新成员限制期</el-checkbox><el-checkbox v-model="previewContact">包含联系人</el-checkbox><span>窗口内重复次数</span><el-input-number v-model="previewRepeats" :min="0" :max="100"/><span>近似内容次数</span><el-input-number v-model="previewSimilar" :min="0" :max="100"/><span>连续发送次数</span><el-input-number v-model="previewBurst" :min="0" :max="100"/><el-button type="primary" @click="preview">测试规则</el-button></div><el-alert v-if="previewReasons !== null" :type="previewReasons.length ? 'warning' : 'success'" :title="previewReasons.length ? previewReasons.join('；') : '未命中当前规则'" :closable="false"/></section></template>
        <MessageArchive v-else-if="page === 'messages' && selected" :key="selected" :group-id="selected" :enabled="group?.settings.archive_enabled !== false"/>
        <section v-else-if="page === 'violations' && selected" class="panel" v-loading="busy"><div class="panel-title"><h2>违规记录</h2><span class="muted">已删除的 Telegram 消息无法原地恢复</span></div><div class="filterbar"><el-input v-model="userFilter" placeholder="Telegram 用户 ID" aria-label="违规用户 ID" clearable/><el-date-picker v-model="dates" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间"/><el-button @click="filters">查询</el-button></div><el-table :data="rows" empty-text="暂无违规记录"><el-table-column label="时间 / 成员" min-width="180"><template #default="{ row }">{{ time(row.created_at) }}<div class="cell-sub">{{ row.user_id }} {{ row.sender_chat ? '（频道身份）' : '' }}</div></template></el-table-column><el-table-column prop="sender_name" label="昵称及用户名" min-width="170"/><el-table-column prop="excerpt" label="消息摘录" min-width="200"/><el-table-column label="命中原因" min-width="180"><template #default="{ row }">{{ row.reasons.join('；') }}</template></el-table-column><el-table-column label="执行结果" min-width="180"><template #default="{ row }"><div v-for="a in row.actions" :key="a.id">{{ { delete: '删消息', profile_mute: '昵称永久禁言', emoji_status_mute: '状态表情永久禁言', warn: '历史通知', moderation_mute: '历史禁言' }[a.kind as string] || operationNames[a.kind] || a.kind }}：{{ label(a.state) }}<small v-if="a.error">{{ a.error }}</small></div></template></el-table-column><el-table-column label="纠正操作" min-width="280"><template #default="{ row }"><template v-if="!row.sender_chat"><el-button v-if="row.warning_active" size="small" @click="confirmAction(`/api/groups/${selected}/violations/${row.id}/actions`, 'revoke_warning', '撤销警告')">撤销警告</el-button><el-button v-if="row.profile_violation || row.warning_active" size="small" @click="confirmAction(`/api/groups/${selected}/violations/${row.id}/actions`, 'unmute', '解除 PurrAI 禁言')">解禁言</el-button><el-button v-if="row.warning_active" size="small" @click="confirmAction(`/api/groups/${selected}/violations/${row.id}/actions`, 'unban', '解除 PurrAI 封禁')">解封禁</el-button></template></template></el-table-column></el-table><el-pagination v-model:current-page="pagination" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load"/></section>
        <section v-else-if="page === 'whitelist' && selected" class="panel" v-loading="busy"><div class="panel-title"><h2>本群白名单</h2><el-button type="primary" @click="edit('whitelist')">添加成员</el-button></div><el-table :data="rows" empty-text="暂无白名单成员"><el-table-column prop="user_id" label="Telegram 用户 ID" min-width="180"/><el-table-column label="免验证" width="100"><template #default="{ row }">{{ row.verification ? '是' : '否' }}</template></el-table-column><el-table-column label="免自动处罚" width="120"><template #default="{ row }">{{ row.moderation ? '是' : '否' }}</template></el-table-column><el-table-column prop="note" label="备注" min-width="170"/><el-table-column label="操作" width="150"><template #default="{ row }"><el-button text @click="edit('whitelist', row)">编辑</el-button><el-button text type="danger" @click="removeWhite(row)">移出</el-button></template></el-table-column></el-table></section>
        <section v-else-if="page === 'admins'" class="panel" v-loading="busy"><div class="panel-title"><h2>管理权限说明</h2></div><p>只允许所有者批准的自用和朋友群接入；批准后，群主和群管理员自动获得本群管理权限。新增或撤销管理员，请直接在 Telegram 群设置中操作。</p><p>超级管理员可以管理所有已接入群，并使用备份和系统任务维护。超级管理员由服务器配置指定，可私聊机器人发送 /id 获取自己的数字编号。</p><el-table :data="rows" empty-text="尚未配置超级管理员"><el-table-column prop="id" label="超级管理员 Telegram 数字 ID" min-width="220"/></el-table></section>
        <template v-else-if="page === 'system'"><section class="panel"><div class="panel-title"><h2>运行状态</h2><el-button @click="systemAction('/api/system/backup')">生成数据库备份</el-button></div><div class="system-grid"><div><span>Telegram 连接</span><strong>{{ system.connected ? '已连接' : '未连接' }}</strong></div><div><span>Bot 配置</span><strong>{{ system.bot_configured ? '已配置' : '未配置' }}</strong></div><div><span>后台登录</span><strong>{{ system.login_configured ? '已配置' : '未配置' }}</strong></div><div><span>待执行任务</span><strong>{{ system.pending_actions ?? '—' }}</strong></div></div><p>最近收取消息：{{ time(system.last_poll) }}</p><p class="muted">最近备份：{{ system.last_backup || '尚未生成' }}</p><p v-if="system.chat_budget" class="muted">今日 AI 模型请求 {{ system.chat_budget.global_calls }}/{{ system.chat_budget.global_call_limit }}；Token 预算 {{ system.chat_budget.global_tokens }}/{{ system.chat_budget.global_token_limit }}。按北京时间零点重置，达到上限只暂停 AI 聊天。</p><el-alert v-if="system.archive_status" :title="system.archive_status" type="warning" :closable="false"/><el-alert v-if="system.last_error" :title="system.last_error" type="warning" :closable="false"/></section><section class="panel"><h2>失败操作</h2><el-table :data="system.failed_actions || []" empty-text="没有失败操作"><el-table-column prop="kind" label="操作" width="150"/><el-table-column prop="group_id" label="群 ID" width="170"/><el-table-column prop="error" label="失败原因" min-width="220"/><el-table-column label="操作" width="100"><template #default="{ row }"><el-button text @click="systemAction(`/api/actions/${row.id}/retry`)">重试</el-button></template></el-table-column></el-table></section><section class="panel"><h2>未处理事件</h2><el-table :data="system.failed_events || []" empty-text="没有失败事件"><el-table-column prop="id" label="事件 ID" width="160"/><el-table-column prop="error" label="失败原因"/><el-table-column label="操作" width="100"><template #default="{ row }"><el-button text @click="systemAction(`/api/events/${row.id}/retry`)">重试</el-button></template></el-table-column></el-table></section></template>
        <section v-if="page === 'verifications' && selected" class="panel"><div class="panel-title"><h2>最近的人工操作</h2><span class="muted">保留操作者、原因与实际执行结果</span></div><el-table :data="history" empty-text="暂无人工操作"><el-table-column label="时间" min-width="160"><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column><el-table-column label="操作" width="140"><template #default="{ row }">{{ operationNames[row.kind] || row.kind }}</template></el-table-column><el-table-column prop="actor_id" label="操作者" min-width="130"/><el-table-column prop="reason" label="原因" min-width="180"/><el-table-column label="状态" width="130"><template #default="{ row }">{{ label(row.state) }}</template></el-table-column><el-table-column prop="error" label="处理说明" min-width="180"/></el-table></section>
        <section v-if="actions.length" class="panel action-results"><h2>本次提交的操作</h2><div v-for="a in actions" :key="a.id" class="action-line"><span>{{ a.reason }}</span><el-tag>{{ label(a.state) }}</el-tag><small>{{ a.error || (a.state === 'pending' ? '等待 Telegram 执行确认' : '') }}</small><a v-if="a.payload?.invite_link" :href="a.payload.invite_link" target="_blank" rel="noopener">{{ a.payload.invite_link }}</a></div></section>
        <footer>PurrAI <span>让群聊回到交流本身。</span></footer>
      </main>
    </div>
    <el-dialog v-model="dialog" :title="{ rule: '广告规则', settings: '群设置', whitelist: '白名单成员' }[editing]" width="min(620px, 94vw)" :close-on-click-modal="false" destroy-on-close>
      <el-form label-position="top" @submit.prevent="save">
        <template v-if="editing === 'rule'"><el-form-item label="规则类型"><el-select v-model="form.kind"><el-option value="keyword" label="关键词"/><el-option value="block_domain" label="禁止域名"/><el-option value="allow_domain" label="允许域名"/><el-option value="block_mention" label="禁止引流账号"/><el-option value="allow_mention" label="允许提及账号"/></el-select></el-form-item><el-form-item label="规则内容"><el-input v-model="form.value" :maxlength="250" placeholder="关键词，或 example.com 形式的域名"/></el-form-item><el-form-item v-if="form.kind === 'keyword'" label="匹配方式"><el-radio-group v-model="form.mode"><el-radio value="contains">包含匹配</el-radio><el-radio value="exact">完整匹配</el-radio><el-radio value="compact">忽略表情和分隔符</el-radio><el-radio value="pinyin">谐音匹配</el-radio></el-radio-group></el-form-item><el-form-item v-if="form.kind === 'keyword'" label="检查范围"><el-select v-model="form.scope"><el-option value="message" label="正文（命中只删消息）"/><el-option value="name" label="昵称及用户名（删消息并永久禁言）"/><el-option value="either" label="昵称或正文，各自处置"/><el-option value="name_and_message" label="昵称＋正文必须同时命中"/></el-select></el-form-item><el-form-item v-if="form.kind === 'keyword' && form.scope === 'name_and_message'" label="正文还需同时命中的关键词"><el-input v-model="form.context_value" maxlength="250"/></el-form-item><p class="muted">谐音与忽略分隔符仅对你配置的词组匹配。昵称违规删除当条消息并永久禁言；正文违规只删消息，不累计警告。管理员手动解禁后自动加入本群白名单。</p><el-form-item label="启用"><el-switch v-model="form.enabled"/></el-form-item><el-form-item label="测试昵称及用户名"><el-input v-model="previewName" placeholder="例如：显示昵称 @username"/></el-form-item><el-form-item label="保存前测试"><el-input v-model="previewText" type="textarea" :rows="2" placeholder="输入消息，测试这次修改后的规则"/></el-form-item><el-button @click="previewDraft">预览当前草稿</el-button><el-alert v-if="previewReasons !== null" :type="previewReasons.length ? 'warning' : 'success'" :title="previewReasons.length ? previewReasons.join('；') : '未命中规则'" :closable="false" style="margin-top:12px"/></template>
        <template v-if="editing === 'settings'"><el-form-item label="已入群成员验证失败处理"><el-select v-model="form.verification_failure_action" aria-label="验证失败处理"><el-option value="permanent_mute" label="永久禁言（留在群里）"/><el-option value="kick" label="踢出群聊（允许重新加入）"/></el-select></el-form-item><p class="muted">超时或错误次数用尽后执行所选方式；默认永久禁言，可由管理员放行或重出题。申请入群者尚未进群，失败仅拒绝申请。</p><div class="settings-toggles"><el-form-item v-for="[key, name] in boolSettings" :key="key" :label="name"><el-switch v-model="form[key]"/></el-form-item></div><div class="settings-grid"><el-form-item v-for="[key, name, minimum, maximum] in numericSettings" :key="key" :label="name"><el-input-number v-model="form[key]" :min="minimum" :max="maximum"/></el-form-item></div><p class="muted">新成员观察持续到时限结束且前几条消息已检查。申请模式需要机器人邀请权限，并使用需要审核的入群链接；直进群仍会做群内验证。关闭验证不影响已开始的任务，可批量放行。</p></template>
        <template v-if="editing === 'settings'">
          <el-form-item label="名字旁状态表情包黑名单"><div style="display:flex;flex-direction:column;gap:6px;width:100%"><div v-for="pack in form.emoji_status_blocked_packs" :key="pack" style="display:flex;align-items:center;justify-content:space-between;gap:8px"><span style="overflow-wrap:anywhere">{{ pack }}</span><el-button text type="danger" :disabled="busy" :aria-label="`移除 ${pack}`" @click="changeEmojiPacks('remove', pack)">移除</el-button></div><span v-if="!form.emoji_status_blocked_packs?.length" class="muted">当前名单为空</span></div></el-form-item>
          <el-form-item label="新增状态表情包"><el-input v-model="emojiPacksText" type="textarea" :rows="2" placeholder="只填写新链接或短名称，多个可以换行；原名单自动保留"/></el-form-item>
          <div style="display:flex;gap:8px;flex-wrap:wrap"><el-button :disabled="busy" @click="changeEmojiPacks('add')">新增表情包</el-button><el-button type="danger" plain :disabled="busy || !form.emoji_status_blocked_packs?.length" @click="changeEmojiPacks('clear')">清空状态表情包黑名单</el-button></div>
          <p class="muted">新增、移除和清空会立即保存，不影响其他尚未保存的群设置；重复链接自动忽略。</p>
          <p class="muted">名字旁的表情状态来自这些表情包时永久禁言，成员仍留在群内。仅在入群和发消息时触发检查，普通贴纸消息不触发这项处罚。默认已加入 bygbb666 和 GsHeChaba；清空名单不会自动解除已有禁言。</p>
          <el-divider content-position="left">群聊天</el-divider>
          <p class="muted">{{ chatStatus.configured ? `模型：${chatStatus.model}` : '使用前请在服务器填写聊天 API 地址、Key 和模型名。' }} 最近消息按群及话题分开。</p>
          <p v-if="chatStatus.budget" class="muted">今日模型请求 {{ chatStatus.budget.group_calls }}/{{ chatStatus.budget.group_call_limit }}；Token 预算 {{ chatStatus.budget.group_tokens }}/{{ chatStatus.budget.group_token_limit }}。额度每日按北京时间重置，由所有者配置；用完只暂停 AI 聊天。</p>
          <el-alert v-if="chatStatus.error" :title="chatStatus.error" type="warning" :closable="false"/>
          <div class="settings-toggles">
            <el-form-item label="开启群聊天"><el-switch v-model="form.chat_enabled"/></el-form-item>
            <el-form-item label="主动参与讨论"><el-switch v-model="form.chat_proactive"/></el-form-item>
          </div>
          <el-form-item label="聊天昵称"><el-input v-model="form.chat_name" :minlength="2" :maxlength="24" placeholder="群友也可以叫这个昵称来唤醒机器人"/></el-form-item>
          <el-form-item label="性格与说话习惯"><el-input v-model="form.chat_persona" type="textarea" :rows="3" :maxlength="1000" show-word-limit/></el-form-item>
          <div class="settings-grid">
            <el-form-item label="最近上下文条数"><el-input-number v-model="form.chat_history" :min="10" :max="80"/></el-form-item>
            <el-form-item label="主动接话检查间隔（秒）"><el-input-number v-model="form.chat_interval" :min="15" :max="3600"/></el-form-item>
          </div>
          <p class="muted">被 @、叫昵称或被回复时接话；刚聊过的群友可以接着说。主动接话会先让模型判断是否适合发言，不会到点就强行插话。</p>
          <div class="settings-toggles">
            <el-form-item label="聊天使用贴纸"><el-switch v-model="form.chat_stickers"/></el-form-item>
            <el-form-item label="偶尔发防诈贴纸"><el-switch v-model="form.chat_antifraud"/></el-form-item>
          </div>
          <el-form-item label="聊天贴纸包"><el-input v-model="chatPacksText" type="textarea" :rows="4" placeholder="每行一个 t.me/addstickers 链接或短名称；留空停用"/></el-form-item>
          <el-form-item label="聊天贴纸最短间隔（秒）"><el-input-number v-model="form.chat_sticker_interval" :min="15" :max="86400"/></el-form-item>
          <el-form-item label="防诈贴纸包"><el-input v-model="antifraudPacksText" type="textarea" :rows="2" placeholder="每行一个 addstickers 链接或短名称；默认 FTX888"/></el-form-item>
          <el-form-item label="防诈提醒最短间隔（秒）"><el-input-number v-model="form.chat_antifraud_interval" :min="3600" :max="604800"/></el-form-item>
          <p class="muted">AI 可自由选择贴纸包已有的全部 emoji 标签，不限固定情绪；文字里的普通 emoji 也可自由使用。贴纸不重复最近 12 张。防诈提醒只在开启群聊天后、北京时间 9—23 点、最近 10 分钟有讨论且累计 50 条新消息时发送，默认至少间隔 6 小时。</p>
          <p v-if="chatStatus.stickers" class="muted">已加载 {{ chatStatus.stickers.ready }}/{{ chatStatus.stickers.total }} 个贴纸包；聊天 emoji 标签：{{ chatStatus.stickers.emoji_count || 0 }} 种<span v-if="chatStatus.stickers.unavailable?.length">；暂不可用：{{ chatStatus.stickers.unavailable.join('、') }}</span></p>
          <el-alert v-if="chatStatus.stickers?.error" :title="chatStatus.stickers.error" type="warning" :closable="false"/>
          <el-button @click="resetChat">重新开始聊天上下文</el-button>
        </template>
        <template v-if="editing === 'whitelist'"><el-form-item label="Telegram 用户 ID"><el-input v-model="form.user_id"/></el-form-item><el-form-item label="免图片验证"><el-switch v-model="form.verification"/></el-form-item><el-form-item label="免自动处罚"><el-switch v-model="form.moderation"/></el-form-item><el-form-item label="备注"><el-input v-model="form.note" maxlength="300"/></el-form-item></template>
      </el-form><el-alert v-if="error" :title="error" type="error" :closable="false"/>
      <template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存</el-button></template>
    </el-dialog>
  </div>
</template>

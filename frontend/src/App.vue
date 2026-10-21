<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Grid, ChatDotRound, Key, Filter, Warning, User as UserIcon, Setting, Refresh, SwitchButton, CircleCheck } from '@element-plus/icons-vue'
import { api, ApiError, post, setCsrf } from './api'
import { ActionTracker, type ActionRow, type BatchReceipt } from './actionTracker'
import { catalog, configureCatalog } from './catalog'
import { workspaceKey } from './workspace'
import { useTask } from './composables/useTask'
import type { AdminCatalog, Group, User } from './types'
import CatMark from './CatMark.vue'
import MessageArchive from './MessageArchive.vue'
import ActionResults from './components/ActionResults.vue'
import OverviewPage from './pages/OverviewPage.vue'
import GroupsPage from './pages/GroupsPage.vue'
import VerificationsPage from './pages/VerificationsPage.vue'
import RulesPage from './pages/RulesPage.vue'
import ViolationsPage from './pages/ViolationsPage.vue'
import WhitelistPage from './pages/WhitelistPage.vue'
import PermissionsPage from './pages/PermissionsPage.vue'
import SystemPage from './pages/SystemPage.vue'

const me = ref<User | null>(null), starting = ref(true)
const page = ref('overview'), selected = ref<number | null>(null), groups = ref<Group[]>([]), refreshKey = ref(0)
const { busy, error, safe } = useTask(expireSession)
const actionTracker = reactive(new ActionTracker())
const nav = [
  { id: 'overview', name: '总览', icon: Grid, detail: '看看群里发生了什么' },
  { id: 'groups', name: '群管理', icon: ChatDotRound, detail: '让每个群都有合适的保护' },
  { id: 'verifications', name: '入群验证', icon: Key, detail: '从进群的第一步开始把关' },
  { id: 'rules', name: '防广告规则', icon: Filter, detail: '把规则讲清楚，把打扰挡下来' },
  { id: 'violations', name: '违规记录', icon: Warning, detail: '每次处理，都有原因可查' },
  { id: 'messages', name: '消息归档', icon: ChatDotRound, detail: '保留消息与编辑版本，方便查询和后续分析' },
  { id: 'whitelist', name: '白名单', icon: CircleCheck, detail: '给信任的成员留一条通道' },
  { id: 'admins', name: '权限说明', icon: UserIcon, detail: '群管理员自动管理本群，超级管理员负责全局维护' },
  { id: 'system', name: '系统状态', icon: Setting, detail: '连接、任务和备份，一眼看清' },
]
const navigation = computed(() => nav.filter(item => me.value?.root || !['admins', 'system'].includes(item.id)))
const current = computed(() => nav.find(item => item.id === page.value)!)
const group = computed(() => groups.value.find(value => value.id === selected.value))
const needsGroup = computed(() => ['verifications', 'rules', 'violations', 'whitelist', 'messages'].includes(page.value))
async function reloadGroups() { groups.value = await api<Group[]>('/api/groups') }
function expireSession() { me.value = null }
async function confirmAction(path: string, action: string, title: string) {
  let reason: string
  try { reason = (await ElMessageBox.prompt('请填写处理原因，操作会保留记录。', title,
    { inputValidator: (value: string) => Boolean(value?.trim()) || '请填写原因', confirmButtonText: '提交操作', cancelButtonText: '取消' })).value }
  catch { return }
  await safe(async () => {
    const result = await post(path, { action, reason, request_id: crypto.randomUUID() }) as ActionRow | BatchReceipt
    actionTracker.accept(result, reason)
    if ('action_ids' in result) ElMessage.success(`已提交 ${result.action_ids.length} 个放行任务`)
    else ElMessage.success(result.state === 'succeeded' ? '操作已完成' : '操作已提交，执行结果将在下方更新')
  })
}
provide(workspaceKey, { me, groups, catalog, actionTracker, reloadGroups, expireSession, confirmAction })
async function refreshView() { refreshKey.value++; await safe(() => actionTracker.refresh(true)) }
async function logout() { await safe(async () => { await post('/auth/logout'); me.value = null }) }
let timer: ReturnType<typeof setInterval> | undefined
onMounted(async () => {
  try {
    me.value = await api<User>('/api/me'); setCsrf(me.value.csrf)
    const [metadata] = await Promise.all([api<AdminCatalog>('/api/catalog'), reloadGroups()])
    configureCatalog(metadata); selected.value = groups.value[0]?.id ?? null
  } catch (reason) {
    me.value = null
    if (!(reason instanceof ApiError && reason.status === 401)) error.value = (reason as Error).message
  } finally { starting.value = false }
  const loginError = new URLSearchParams(location.search).get('login_error'); if (loginError) error.value = loginError
  timer = setInterval(async () => {
    if (!me.value || busy.value) return
    try { await actionTracker.refresh() }
    catch (reason) { if (reason instanceof ApiError && reason.status === 401) expireSession() }
  }, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div v-if="starting" class="startup">正在连接 PurrAI…</div>
<main v-else-if="!me" class="login-wrap">
<section class="login-story">
<div class="wordmark">
<CatMark class="cat"/> PurrAI</div>
<span class="eyebrow">给讨论留一点安静</span>
<h1>群里的热闹，<br>交给真正的成员。</h1>
<p>从入群验证到广告处理，<br>让每一次管理都有迹可循。</p>
<div class="story-foot">入群验证 <i>·</i> 群管理 <i>·</i> 广告拦截</div>
</section>
<section class="login-card">
<span class="eyebrow">管理员工作台</span>
<h2>欢迎回来</h2>
<p>这里是可选网页后台。也可私聊机器人发送 /admin，直接在 Telegram 管理群。</p>
<el-alert v-if="error" :title="error" type="error" :closable="false" show-icon/>
<a href="/auth/telegram/start" class="telegram-login">
<svg viewBox="0 0 24 24" aria-hidden="true">
<path fill="currentColor" d="m21 3-3.4 17-5.3-4-2.8 2.7.6-4.6 8.3-7.5-10.3 6.5-5-1.6z"/>
</svg> 使用 Telegram 登录</a>
<small>仅限群管理员和超级管理员。群权限按 Telegram 当前身份确认。</small>
<div class="login-rule">
</div>
<p class="muted">PurrAI · 自己的群，自己的规则</p>
</section>
</main>
<div v-else class="workspace">
<aside class="sidebar">
<a class="wordmark" href="/" aria-label="PurrAI 首页">
<CatMark class="cat"/> PurrAI</a>
<div class="sidebar-caption">群管理工作台</div>
<nav>
<button v-for="item in navigation" :key="item.id" :class="{ active: page === item.id }" @click="page = item.id">
<el-icon>
<component :is="item.icon"/>
</el-icon>
<span>{{ item.name }}</span>
</button>
</nav>
<div class="sidebar-bottom">
<span class="live-dot">
</span> 按自己的规则守护群聊<div>{{ me.root ? '超级管理员' : '群管理员' }} · {{ me.id }}</div>
</div>
</aside>
<div class="main-area">
<header class="topbar">
<span>PurrAI <span class="slash">/</span> {{ current.name }}</span>
<div class="top-actions">
<el-select v-model="selected" placeholder="选择群" aria-label="选择群" style="width: 220px">
<el-option v-for="g in groups" :key="g.id" :value="g.id" :label="g.title"/>
</el-select>
<el-button :icon="Refresh" circle aria-label="刷新" @click="refreshView"/>
<el-button :icon="SwitchButton" circle aria-label="退出登录" @click="logout"/>
</div>
</header>
<main class="content">
<div class="page-heading">
<div>
<span class="eyebrow">{{ needsGroup ? group?.title || '请选择群' : 'PURRAI WORKSPACE' }}</span>
<h1>{{ current.name }}</h1>
<p>{{ current.detail }}</p>
</div>
<span class="timezone">时间均为北京时间</span>
</div>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<el-alert v-if="needsGroup && !selected" title="还没有可以管理的群。请先将机器人加入超级群，再由本群管理员启用保护。" type="info" :closable="false"/>
<OverviewPage v-if="page === 'overview'" :refresh-key="refreshKey" @navigate="page = $event"/>
<GroupsPage v-else-if="page === 'groups'" :refresh-key="refreshKey"/>
<VerificationsPage v-else-if="page === 'verifications' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<RulesPage v-else-if="page === 'rules' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<MessageArchive v-else-if="page === 'messages' && selected" :key="selected" :group-id="selected" :enabled="group?.settings.archive_enabled !== false"/>
<ViolationsPage v-else-if="page === 'violations' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<WhitelistPage v-else-if="page === 'whitelist' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<PermissionsPage v-else-if="page === 'admins'" :refresh-key="refreshKey"/>
<SystemPage v-else-if="page === 'system'" :refresh-key="refreshKey"/>
<ActionResults/>
<footer>PurrAI <span>让群聊回到交流本身。</span>
</footer>
</main>
</div>
</div>

</template>

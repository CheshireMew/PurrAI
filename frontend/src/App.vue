<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, reactive, ref, watch, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Grid, ChatDotRound, Key, Filter, Warning, User as UserIcon, Setting, Refresh, SwitchButton, CircleCheck, Menu, Close, Document } from '@element-plus/icons-vue'
import { api, ApiError, post, setCsrf } from './api'
import { ActionTracker } from './actionTracker'
import { ActionRequests, type AcceptedAction, type ActionBody, type ActionRequest } from './actionRequests'
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
const menuExpanded = ref(false)
const { busy, error, safe } = useTask(expireSession)
const actionTracker = reactive(new ActionTracker())
const actionRequests = reactive(new ActionRequests())
const nav = [
  { id: 'overview', name: '总览', icon: Grid, detail: '看看群里发生了什么' },
  { id: 'groups', name: '群管理', icon: ChatDotRound, detail: '让每个群都有合适的保护' },
  { id: 'verifications', name: '入群验证', icon: Key, detail: '从进群的第一步开始把关' },
  { id: 'rules', name: '防广告规则', icon: Filter, detail: '把规则讲清楚，把打扰挡下来' },
  { id: 'violations', name: '违规记录', icon: Warning, detail: '每次处理，都有原因可查' },
  { id: 'messages', name: '消息归档', icon: Document, detail: '保留消息与编辑版本，方便查询和后续分析' },
  { id: 'whitelist', name: '白名单', icon: CircleCheck, detail: '给信任的成员留一条通道' },
  { id: 'admins', name: '权限说明', icon: UserIcon, detail: '群管理员自动管理本群，超级管理员负责全局维护' },
  { id: 'system', name: '系统状态', icon: Setting, detail: '连接、任务和备份，一眼看清' },
]
const navigation = computed(() => nav.filter(item => me.value?.root || !['admins', 'system'].includes(item.id)))
const current = computed(() => nav.find(item => item.id === page.value)!)
const needsGroup = computed(() => ['verifications', 'rules', 'violations', 'whitelist', 'messages'].includes(page.value))
watch(page, async () => {
  menuExpanded.value = false
  await nextTick()
  document.querySelector('.sidebar button.active')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
})
async function reloadGroups() {
  groups.value = await api<Group[]>('/api/groups')
  if (!groups.value.some(group => group.id === selected.value)) selected.value = groups.value[0]?.id ?? null
}
function expireSession() { me.value = null }
function acceptAction({ receipt, reason }: AcceptedAction) {
  actionTracker.accept(receipt, reason)
  if ('action_ids' in receipt) ElMessage.success(`已提交 ${receipt.action_ids.length} 个放行任务`)
  else if (receipt.state === 'cancelled') ElMessage.info('操作已取消，请查看下方结果')
  else if (receipt.state === 'failed') ElMessage.warning('操作未完成，请查看下方原因')
  else ElMessage.success(receipt.state === 'succeeded' ? '操作已完成' : '操作已提交，执行结果将在下方更新')
  void refreshActions()
}
async function submitAction(path: string, body: ActionBody) {
  await safe(async () => acceptAction(await actionRequests.submit(path, body)))
}
async function retryAction(request: ActionRequest) {
  await safe(async () => acceptAction(await actionRequests.retry(request)))
}
async function confirmAction(path: string, action: string, title: string) {
  let reason: string
  try { reason = (await ElMessageBox.prompt('请填写处理原因，操作会保留记录。', title,
    { inputValidator: (value: string) => Boolean(value?.trim()) || '请填写原因', confirmButtonText: '提交操作', cancelButtonText: '取消' })).value }
  catch { return }
  await submitAction(path, { action, reason })
}
provide(workspaceKey, { me, groups, catalog, actionTracker, actionRequests, submitAction, retryAction, reloadGroups, expireSession, confirmAction })
async function refreshView() {
  await safe(async () => { await Promise.all([reloadGroups(), actionTracker.refresh(true)]) })
  refreshKey.value++
}
async function logout() { await safe(async () => { await post('/auth/logout'); me.value = null }) }
async function refreshActions() {
  if (!me.value) return
  try { await actionTracker.refresh() }
  catch (reason) { if (reason instanceof ApiError && reason.status === 401) expireSession() }
}
let timer: ReturnType<typeof setInterval> | undefined
onMounted(async () => {
  try {
    me.value = await api<User>('/api/me'); setCsrf(me.value.csrf)
    actionRequests.useUser(me.value.id)
    const [metadata] = await Promise.all([api<AdminCatalog>('/api/catalog'), reloadGroups()])
    configureCatalog(metadata); selected.value = groups.value[0]?.id ?? null
  } catch (reason) {
    me.value = null
    if (!(reason instanceof ApiError && reason.status === 401)) error.value = (reason as Error).message
  } finally { starting.value = false }
  const loginError = new URLSearchParams(location.search).get('login_error'); if (loginError) error.value = loginError
  timer = setInterval(() => { void refreshActions() }, 1000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div v-if="starting" class="startup" role="status"><CatMark class="startup-mark"/><span>正在连接 PurrAI…</span></div>
<main v-else-if="!me" class="login-wrap">
<section class="login-story">
<div class="wordmark">
<CatMark class="cat"/> PurrAI</div>
<h1>群里的热闹，<br>交给真正的成员。</h1>
<p>从入群验证到广告处理，<br>让每一次管理都有迹可循。</p>
<div class="story-foot">入群验证 <i>·</i> 群管理 <i>·</i> 广告拦截</div>
</section>
<section class="login-card">
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
<a class="skip-link" href="#main-content">跳到主要内容</a>
<aside class="sidebar">
<div class="sidebar-brand">
<a class="wordmark" href="/" aria-label="PurrAI 首页">
<CatMark class="cat"/> PurrAI</a>
<button class="mobile-menu" :aria-expanded="menuExpanded" aria-controls="workspace-nav" :aria-label="menuExpanded ? '收起导航' : '展开导航'" @click="menuExpanded = !menuExpanded"><el-icon><component :is="menuExpanded ? Close : Menu"/></el-icon></button>
</div>
<nav id="workspace-nav" aria-label="管理导航" :class="{ expanded: menuExpanded }">
<button v-for="item in navigation" :key="item.id" :class="{ active: page === item.id, 'nav-system': item.id === 'admins' }" :aria-current="page === item.id ? 'page' : undefined" @click="page = item.id">
<el-icon>
<component :is="item.icon"/>
</el-icon>
<span>{{ item.name }}</span>
</button>
</nav>
<div class="sidebar-bottom">
<span class="account-avatar"><el-icon><UserIcon/></el-icon></span><div><strong>{{ me.root ? '超级管理员' : '群管理员' }}</strong><span>{{ me.id }}</span></div>
</div>
</aside>
<div class="main-area">
<header class="topbar">
<span class="breadcrumb">工作台 <span class="slash">/</span> {{ current.name }}</span>
<div class="top-actions">
<el-select v-model="selected" placeholder="选择群" aria-label="选择群" class="group-selector" filterable :disabled="!groups.length">
<el-option v-for="g in groups" :key="g.id" :value="g.id" :label="g.title"/>
</el-select>
<el-tooltip content="刷新当前页面" placement="bottom"><el-button :icon="Refresh" circle aria-label="刷新" :aria-busy="busy" @click="refreshView"/></el-tooltip>
<el-tooltip content="退出登录" placement="bottom"><el-button :icon="SwitchButton" circle aria-label="退出登录" @click="logout"/></el-tooltip>
</div>
</header>
<main id="main-content" class="content" tabindex="-1">
<div class="page-heading">
<div>
<h1>{{ current.name }}</h1>
<p>{{ current.detail }}</p>
</div>
</div>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<el-alert v-if="needsGroup && !selected" title="还没有可以管理的群。请先将机器人加入超级群，再由本群管理员启用保护。" type="info" :closable="false"/>
<OverviewPage v-if="page === 'overview'" :refresh-key="refreshKey" @navigate="page = $event"/>
<GroupsPage v-else-if="page === 'groups'" :refresh-key="refreshKey"/>
<VerificationsPage v-else-if="page === 'verifications' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<RulesPage v-else-if="page === 'rules' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<MessageArchive v-else-if="page === 'messages' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<ViolationsPage v-else-if="page === 'violations' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<WhitelistPage v-else-if="page === 'whitelist' && selected" :key="selected" :group-id="selected" :refresh-key="refreshKey"/>
<PermissionsPage v-else-if="page === 'admins'" :refresh-key="refreshKey"/>
<SystemPage v-else-if="page === 'system'" :refresh-key="refreshKey"/>
<ActionResults/>
<footer>PurrAI <span>时间均为北京时间</span>
</footer>
</main>
</div>
</div>

</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, reactive, ref, watch, nextTick, type Component } from 'vue'
import { describeAction, type ActionContext } from './actionContext'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Grid, ChatDotRound, User as UserIcon, Setting, Refresh, SwitchButton, Document, MoreFilled } from '@element-plus/icons-vue'
import { api, ApiError, post, setCsrf } from './api'
import { ActionTracker } from './actionTracker'
import { ActionRequests, type AcceptedAction, type ActionBody, type ActionRequest } from './actionRequests'
import { MutationRequests, type MutationRequest } from './mutationRequests'
import { catalog, configureCatalog } from './catalog'
import { workspaceKey } from './workspace'
import { useTask } from './composables/useTask'
import { useLatestRequest } from './composables/useLatestRequest'
import type { AdminCatalog, Group, User } from './types'
import CatMark from './CatMark.vue'
import MessageArchive from './MessageArchive.vue'
import ActionResults from './components/ActionResults.vue'
import OverviewPage from './pages/OverviewPage.vue'
import GroupsPage from './pages/GroupsPage.vue'
import VerificationsPage from './pages/VerificationsPage.vue'
import RulesPage from './pages/RulesPage.vue'
import AutoRepliesPage from './pages/AutoRepliesPage.vue'
import GroupMenuPage from './pages/GroupMenuPage.vue'
import PointsPage from './pages/PointsPage.vue'
import LotteriesPage from './pages/LotteriesPage.vue'
import ViolationsPage from './pages/ViolationsPage.vue'
import WhitelistPage from './pages/WhitelistPage.vue'
import PermissionsPage from './pages/PermissionsPage.vue'
import SystemPage from './pages/SystemPage.vue'
import MembersPage from './pages/MembersPage.vue'
import OperationsPage from './pages/OperationsPage.vue'
import ChatDiagnosticsPage from './pages/ChatDiagnosticsPage.vue'
import { navigate, observeNavigation, route } from './navigation'
import { confirmNavigation, hasPendingDrafts, protectWindowClose } from './drafts'
import { workspacePages, workspaceSections } from './workspaceNavigation'

const me = ref<User | null>(null), starting = ref(true)
const page = ref(route.value.page), selected = ref<number | null>(route.value.group), groups = ref<Group[]>([]), refreshKey = ref(0)
const groupWarning = ref('')
const startupError = ref('')
const { busy, error, safe } = useTask(expireSession)
const groupReads = useLatestRequest(expireSession)
const actionTracker = reactive(new ActionTracker())
const actionRequests = reactive(new ActionRequests())
const mutations = reactive(new MutationRequests())
const sectionIcons: Record<string, Component> = { overview: Grid, groups: Setting, members: UserIcon, interaction: ChatDotRound, records: Document }
const pageComponents: Record<string, Component> = { groups: GroupsPage, members: MembersPage, points: PointsPage,
  lotteries: LotteriesPage, operations: OperationsPage, chat: ChatDiagnosticsPage, verifications: VerificationsPage,
  rules: RulesPage, 'auto-replies': AutoRepliesPage, 'group-menu': GroupMenuPage, messages: MessageArchive, violations: ViolationsPage,
  whitelist: WhitelistPage, admins: PermissionsPage, system: SystemPage }
const availablePages = computed(() => workspacePages.filter(item => me.value?.root || !item.rootOnly))
const navigation = workspaceSections.filter(item => item.id !== 'system')
const currentPage = computed(() => workspacePages.find(item => item.id === page.value)!)
const activeNavigation = computed(() => currentPage.value.section)
const current = computed(() => workspaceSections.find(item => item.id === activeNavigation.value)!)
const sectionPages = computed(() => availablePages.value.filter(item => item.section === activeNavigation.value && (!item.secondary || item.id === page.value)))
const pageProps = computed(() => ({ refreshKey: refreshKey.value,
  ...(currentPage.value.requiresGroup || page.value === 'groups' ? {groupId: selected.value} : {}) }))
if (!workspacePages.some(item => item.id === page.value)) page.value = 'overview'
let restoringNavigation = false
watch(route, current => {
  restoringNavigation = true
  page.value = availablePages.value.some(item => item.id === current.page) ? current.page : 'overview'
  if (current.group !== null && groups.value.some(item => item.id === current.group)) selected.value = current.group
  restoringNavigation = false
}, {flush: 'sync'})
watch([page, selected], () => { if (!starting.value && !restoringNavigation) navigate(page.value, selected.value) }, {flush: 'sync'})
const needsGroup = computed(() => currentPage.value.requiresGroup)
async function changePage(value: string) {
  if (value !== page.value && await confirmNavigation()) page.value = value
}
async function changeGroup(value: number) {
  if (value !== selected.value && await confirmNavigation()) selected.value = value
}
function skipToContent() { document.getElementById('main-content')?.focus() }
watch(page, async () => {
  error.value = ''
  await nextTick()
  const container = document.querySelector<HTMLElement>('.sidebar nav')
  const button = container?.querySelector<HTMLElement>('button.active')
  if (container && button) {
    const parent = container.getBoundingClientRect(), child = button.getBoundingClientRect()
    if (child.top < parent.top) container.scrollTop += child.top - parent.top
    else if (child.bottom > parent.bottom) container.scrollTop += child.bottom - parent.bottom
    if (child.left < parent.left) container.scrollLeft += child.left - parent.left
    else if (child.right > parent.right) container.scrollLeft += child.right - parent.right
  }
})
async function reloadGroups() {
  let warning = ''
  await groupReads.run(() => api<Group[]>('/api/groups', {}, response => {
    warning = response.headers.get('X-PurrAI-Groups-Partial') === 'true'
      ? '部分群权限暂未核实，当前显示已确认的群；稍后可刷新重试。' : ''
  }), result => {
  groups.value = result; groupWarning.value = warning
  if (!groups.value.some(group => group.id === selected.value)) selected.value = groups.value.find(group => group.previous_ids?.includes(selected.value!))?.id ?? groups.value[0]?.id ?? null
  }, reason => { throw reason })
}
function expireSession() { groupReads.cancel(); me.value = null }
function acceptAction({ receipt, reason, context }: AcceptedAction) {
  actionTracker.accept(receipt, reason, context)
  if ('action_ids' in receipt) ElMessage.success(`已提交 ${receipt.action_ids.length} 个放行任务`)
  else if (receipt.state === 'cancelled') ElMessage.info('操作已取消，请查看下方结果')
  else if (receipt.state === 'failed') ElMessage.warning('操作未完成，请查看下方原因')
  else ElMessage.success(receipt.state === 'succeeded' ? '操作已完成' : '操作已提交，执行结果将在下方更新')
  void refreshActions()
}
async function submitAction(path: string, body: ActionBody, context?: ActionContext) {
  return safe(async () => acceptAction(await actionRequests.submit(path, body, context)))
}
async function retryAction(request: ActionRequest) {
  await safe(async () => acceptAction(await actionRequests.retry(request)))
}
async function retryMutation(request: MutationRequest) {
  if (await safe(async () => { await mutations.retry(request); ElMessage.success('已确认提交结果') })) {
    await reloadGroups(); refreshKey.value++
  }
}
async function confirmAction(path: string, action: string, title: string, context: ActionContext) {
  let reason: string
  try { reason = (await ElMessageBox.prompt(`${describeAction(context, catalog.value!.operations)}\n请填写处理原因，操作会保留记录。`, title,
    { inputValidator: (value: string) => Boolean(value?.trim()) || '请填写原因', confirmButtonText: '提交操作', cancelButtonText: '取消' })).value }
  catch { return }
  await submitAction(path, { action, reason, ...(context.duration_seconds !== undefined ? {duration_seconds: context.duration_seconds} : {}) }, context)
}
provide(workspaceKey, { me, groups, catalog, actionTracker, actionRequests, mutations, retryMutation, submitAction, retryAction, reloadGroups, expireSession, confirmAction,
  reportError: message => { error.value = message } })
async function refreshView() {
  await safe(async () => { await Promise.all([reloadGroups(), actionTracker.refresh(true)]) })
  refreshKey.value++
}
async function logout() {
  if (await confirmNavigation()) await safe(async () => { await post('/auth/logout'); me.value = null })
}
async function refreshActions() {
  if (!me.value || document.visibilityState === 'hidden') return
  try { await actionTracker.refresh() }
  catch (reason) { if (reason instanceof ApiError && reason.status === 401) expireSession() }
}
let timer: ReturnType<typeof setInterval> | undefined
async function connect() {
  starting.value = true; startupError.value = ''; error.value = ''
  try {
    me.value = await api<User>('/api/me'); setCsrf(me.value.csrf)
    actionRequests.useUser(me.value.id)
    mutations.useUser(me.value.id)
    const [metadata] = await Promise.all([api<AdminCatalog>('/api/catalog'), reloadGroups()])
    configureCatalog(metadata)
    if (!availablePages.value.some(item => item.id === page.value)) page.value = 'overview'
    navigate(page.value, selected.value, true)
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 401) me.value = null
    else startupError.value = (reason as Error).message
  } finally { starting.value = false }
}
onMounted(async () => {
  window.addEventListener('beforeunload', protectWindowClose)
  await connect()
  const loginError = new URLSearchParams(location.search).get('login_error'); if (loginError) error.value = loginError
  timer = setInterval(() => { void refreshActions() }, 3000)
})
const stopNavigation = observeNavigation(confirmNavigation, hasPendingDrafts)
onUnmounted(() => { clearInterval(timer); stopNavigation(); window.removeEventListener('beforeunload', protectWindowClose) })
</script>

<template>
  <div v-if="starting" class="startup" role="status"><CatMark class="startup-mark"/><span>正在连接 PurrAI…</span></div>
<main v-else-if="startupError" class="connection-error">
<section class="panel"><div class="wordmark"><CatMark class="cat"/> PurrAI</div><h1>暂时无法加载管理后台</h1>
<el-alert :title="startupError" type="error" :closable="false" show-icon/>
<el-button type="primary" @click="connect">重试连接</el-button></section>
</main>
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
<a class="skip-link" href="#main-content" @click.prevent="skipToContent">跳到主要内容</a>
<aside class="sidebar">
<div class="sidebar-brand">
<a class="wordmark" href="/" aria-label="PurrAI 首页" @click.prevent="changePage('overview')">
<CatMark class="cat"/> PurrAI</a>
</div>
<nav id="workspace-nav" aria-label="管理导航">
<button v-for="item in navigation" :key="item.id" :class="{ active: activeNavigation === item.id }" :aria-current="activeNavigation === item.id ? 'page' : undefined" @click="activeNavigation !== item.id && changePage(item.page)">
<el-icon>
<component :is="sectionIcons[item.id]"/>
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
<div class="group-switcher">
<label for="group-selector" class="group-selector-label">当前群组</label>
<el-select id="group-selector" :model-value="selected" placeholder="选择群" aria-label="选择群" class="group-selector" filterable :disabled="!groups.length" @update:model-value="changeGroup">
<template #prefix><el-icon><ChatDotRound/></el-icon></template>
<el-option v-for="g in groups" :key="g.id" :value="g.id" :label="g.title"/>
</el-select>
</div>
<div class="top-actions">
<el-tooltip v-if="me.local_dev" content="操作只写入本机测试数据，不连接真实 Telegram 群"><el-tag type="info">本地测试</el-tag></el-tooltip>
<el-tooltip content="刷新当前页面" placement="bottom"><el-button :icon="Refresh" circle aria-label="刷新" :aria-busy="busy" @click="refreshView"/></el-tooltip>
<el-tooltip v-if="me.root" content="系统维护" placement="bottom"><el-button :icon="Setting" circle aria-label="系统维护" :aria-pressed="activeNavigation === 'system'" @click="changePage('system')"/></el-tooltip>
<el-tooltip v-if="!me.local_dev" content="退出登录" placement="bottom"><el-button :icon="SwitchButton" circle aria-label="退出登录" @click="logout"/></el-tooltip>
</div>
</header>
<main id="main-content" class="content" tabindex="-1">
<div class="page-heading">
<div>
<h1>{{ current.name }}</h1>
</div>
<el-dropdown v-if="activeNavigation === 'records'" trigger="click" @command="changePage"><el-button text :icon="MoreFilled" aria-label="记录选项"/><template #dropdown><el-dropdown-menu><el-dropdown-item command="chat">聊天排查</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
</div>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<el-alert v-if="groupWarning" :title="groupWarning" type="warning" :closable="false"/>
<el-alert v-if="needsGroup && !selected" title="还没有可以管理的群。请先将机器人加入超级群，再由本群管理员启用保护。" type="info" :closable="false"/>
<OverviewPage v-if="page === 'overview'" :refresh-key="refreshKey" @navigate="changePage"/>
<el-tabs v-else :model-value="`workspace-${page}`" class="workspace-tabs" :before-leave="confirmNavigation" @update:model-value="page = String($event).slice('workspace-'.length)">
<el-tab-pane v-for="item in sectionPages" :key="item.id" :label="item.name" :name="`workspace-${item.id}`">
<component :is="pageComponents[item.id]" v-if="page === item.id && (!item.requiresGroup || selected)" :key="`${item.id}:${selected}`" v-bind="pageProps"/>
</el-tab-pane>
</el-tabs>
<ActionResults/>
<footer>PurrAI <span>时间均为北京时间</span>
</footer>
</main>
</div>
</div>

</template>

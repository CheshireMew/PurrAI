<script setup lang="ts">
import { computed, reactive, ref, watch, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, post } from '../api'
import type { ChatStatus, Group, GroupSettings, SharedPolicy } from '../types'
import { useWorkspace } from '../workspace'
import { useTask } from '../composables/useTask'
import { useDraftClose } from '../composables/useDraftClose'
import EmergencyProtection from './EmergencyProtection.vue'

const props = defineProps<{ group: Group | null; sharedBaseline?: SharedPolicy | null }>()
const sharedMode = computed(() => props.group?.id === 0)
const commonBaseline = ref<SharedPolicy | null>(null)
const emit = defineEmits<{ close: []; saved: [] }>()
const { catalog, groups, reloadGroups, mutations } = useWorkspace()
const { busy, error, safe } = useTask()
const dialog = ref(false), editingId = ref('')
const activeTab = ref('verification')
const settingsTabs = [
  { name: 'verification', label: '入群验证' }, { name: 'moderation', label: '防广告' },
  { name: 'chat', label: '群聊天' }, { name: 'emoji', label: '状态表情' }, { name: 'archive', label: '消息归档' },
  { name: 'inheritance', label: '继承与恢复' },
  { name: 'protection', label: '联合刷屏与应急' },
]
const copySettings = (value: GroupSettings): GroupSettings => JSON.parse(JSON.stringify(value))
const form = reactive<GroupSettings>(copySettings(catalog.value!.settings.defaults))
const settingsBaseline = ref<GroupSettings>(copySettings(catalog.value!.settings.defaults))
const emojiPacksText = ref(''), chatPacksText = ref(''), antifraudPacksText = ref('')
const ignoredChatText = ref('')
const commandExceptionsText = ref('')
const allowedLinksText = ref('')
const stagedReset = ref<(keyof GroupSettings)[]>([])
const resetValues = ref<Partial<GroupSettings>>({})
function draftSettings(): GroupSettings {
  return { ...form,
    chat_sticker_packs: chatPacksText.value.split(/[\s,，]+/).filter(Boolean),
    chat_antifraud_packs: antifraudPacksText.value.split(/[\s,，]+/).filter(Boolean),
    chat_ignored_texts: ignoredChatText.value.split(/\r?\n/).map(text => text.trim()).filter(Boolean),
    chat_command_exceptions: commandExceptionsText.value.split(/\r?\n/).map(text => text.trim()).filter(Boolean),
    telegram_allowed_links: allowedLinksText.value.split(/\s+/).filter(Boolean),
  }
}
function syncTextFields() {
  chatPacksText.value = form.chat_sticker_packs.join('\n')
  antifraudPacksText.value = form.chat_antifraud_packs.join('\n')
  ignoredChatText.value = form.chat_ignored_texts.join('\n')
  commandExceptionsText.value = form.chat_command_exceptions.join('\n')
  allowedLinksText.value = form.telegram_allowed_links.join('\n')
}
const pendingReset = computed(() => {
  const draft = draftSettings()
  return stagedReset.value.filter(key => JSON.stringify(draft[key]) === JSON.stringify(resetValues.value[key]))
})
const dirty = computed(() => !!emojiPacksText.value.trim() || stagedReset.value.length > 0 || JSON.stringify(draftSettings()) !== JSON.stringify(settingsBaseline.value))
const { requestClose } = useDraftClose(() => dirty.value, () => { dialog.value = false }, () => busy.value)
const chatStatus = ref<Partial<ChatStatus>>({})
const inheritance = ref<{ defaults: GroupSettings; settings: GroupSettings; overrides: string[] } | null>(null)
const inheritedRows = computed(() => Object.keys(form).map(key => ({ key: key as keyof GroupSettings,
  label: catalog.value!.settings.labels?.[key] || key })))
const showValue = (value: unknown) => typeof value === 'boolean' ? value ? '开启' : '关闭' : Array.isArray(value) ? value.join('、') || '空' : String(value)
type NumericKey = { [K in keyof GroupSettings]: GroupSettings[K] extends number ? K : never }[keyof GroupSettings]
type BooleanKey = { [K in keyof GroupSettings]: GroupSettings[K] extends boolean ? K : never }[keyof GroupSettings]
const verificationKeys = new Set(['join_requests', 'verification_enabled', 'verification_timeout', 'max_attempts', 'failure_ban_seconds'])
const sectionFor = (key: string) => /^(coordinated_|raid_|emergency_)/.test(key) ? 'protection' : key === 'archive_enabled' ? 'archive' : verificationKeys.has(key) ? 'verification' : 'moderation'
const numericSettings = computed(() => (Object.entries(catalog.value!.settings.numbers) as [NumericKey, string][]).filter(([key]) => sectionFor(key) === activeTab.value))
const boolSettings = computed(() => (Object.entries(catalog.value!.settings.booleans) as [BooleanKey, string][]).filter(([key]) => sectionFor(key) === activeTab.value))
const bounds = (key: keyof GroupSettings) => catalog.value!.settings.fields[key]
let version = 0
watch(() => props.group, group => {
  const current = ++version
  if (!group) { dialog.value = false; return }
  editingId.value = String(group.id)
  commonBaseline.value = props.sharedBaseline ? JSON.parse(JSON.stringify(props.sharedBaseline)) : null
  activeTab.value = 'verification'
  Object.assign(form, copySettings({ ...catalog.value!.settings.defaults, ...group.settings }))
  settingsBaseline.value = copySettings({ ...form })
  stagedReset.value = []; resetValues.value = {}
  emojiPacksText.value = ''; chatPacksText.value = form.chat_sticker_packs.join('\n')
  antifraudPacksText.value = form.chat_antifraud_packs.join('\n'); chatStatus.value = {}; error.value = ''
  ignoredChatText.value = form.chat_ignored_texts.join('\n')
  commandExceptionsText.value = form.chat_command_exceptions.join('\n')
  allowedLinksText.value = form.telegram_allowed_links.join('\n')
  dialog.value = true
  inheritance.value = null
  if (group.id !== 0) api<{ defaults: GroupSettings; settings: GroupSettings; overrides: string[] }>(`/api/groups/${group.id}/settings/inheritance`)
    .then(data => { if (current === version) inheritance.value = data })
    .catch(() => { if (current === version) error.value = '暂时无法读取设置继承来源，可重新打开重试' })
  if (group.id !== 0) api<ChatStatus>(`/api/groups/${group.id}/chat`).then(data => { if (current === version) chatStatus.value = data })
    .catch(() => { if (current === version) chatStatus.value = { error: '暂时无法读取聊天服务状态' } })
}, { immediate: true })
watch(dialog, value => { if (!value) { version++; emit('close') } })
onUnmounted(() => { version++ })
async function changeEmojiPacks(operation: 'add' | 'remove' | 'clear', pack?: string) {
  if (operation === 'clear') {
    try { await ElMessageBox.confirm('清空本群的状态表情包黑名单？已有封禁或禁言不会自动解除。', '清空名单', { confirmButtonText: '确认清空', cancelButtonText: '取消' }) }
    catch { return }
  }
  const id = editingId.value
  await safe(async () => {
    const packs = operation === 'add' ? emojiPacksText.value.split(/[\s,，]+/).filter(Boolean) : pack ? [pack] : []
    if (operation === 'add' && !packs.length) throw new Error('请填写要新增的表情包链接')
    if (sharedMode.value) {
      const old = form.emoji_status_blocked_packs
      const names = packs
      const updated = operation === 'add' ? [...new Set([...old, ...names])] : operation === 'clear' ? [] : old.filter(value => !names.includes(value))
      commonBaseline.value = await mutations.submit<SharedPolicy>('/api/settings/defaults', 'PATCH', { settings: { emoji_status_blocked_packs: updated }, baseline: commonBaseline.value }, undefined, '保存通用名单')
      form.emoji_status_blocked_packs = commonBaseline.value.settings.emoji_status_blocked_packs
      settingsBaseline.value.emoji_status_blocked_packs = [...form.emoji_status_blocked_packs]
      emojiPacksText.value = ''; await reloadGroups(); ElMessage.success('已保存通用名单'); return
    }
    const result = await mutations.submit<{ packs: string[]; added: string[]; removed: string[] }>(`/api/groups/${id}/emoji-status-packs`, 'POST', { operation, packs }, undefined, '修改状态表情名单')
    if (editingId.value === id) {
      form.emoji_status_blocked_packs = result.packs; settingsBaseline.value.emoji_status_blocked_packs = [...result.packs]
      if (operation === 'add') emojiPacksText.value = ''
    }
    const current = groups.value.find(row => String(row.id) === id)
    if (current) current.settings.emoji_status_blocked_packs = result.packs
    ElMessage.success(operation === 'add' ? result.added.length ? `已新增 ${result.added.length} 个，原名单已保留` : '已在名单中，无需重复添加' : operation === 'remove' ? '已移除' : '已清空名单')
  })
}
async function resetChat() {
  await safe(async () => { await post(`/api/groups/${editingId.value}/chat/reset`); ElMessage.success('已重新开始聊天；消息归档仍然保留') })
}
async function changeInheritance(key: keyof GroupSettings, restore: boolean) {
  if (!inheritance.value) return
  await safe(async () => {
    const result = await api<Group>(`/api/groups/${editingId.value}/settings`, { method: 'PATCH', body: JSON.stringify({
      ...(restore ? { reset: [key] } : { settings: { [key]: form[key] } }), baseline: { [key]: settingsBaseline.value[key] },
    }) })
    Object.assign(form, { [key]: result.settings[key] }); settingsBaseline.value[key] = result.settings[key] as never
    stagedReset.value = stagedReset.value.filter(item => item !== key)
    inheritance.value!.overrides = result.setting_overrides || []
    if (key === 'chat_sticker_packs') chatPacksText.value = form.chat_sticker_packs.join('\n')
    if (key === 'chat_antifraud_packs') antifraudPacksText.value = form.chat_antifraud_packs.join('\n')
    if (key === 'chat_ignored_texts') ignoredChatText.value = form.chat_ignored_texts.join('\n')
    if (key === 'chat_command_exceptions') commandExceptionsText.value = form.chat_command_exceptions.join('\n')
    if (key === 'telegram_allowed_links') allowedLinksText.value = form.telegram_allowed_links.join('\n')
    await reloadGroups(); ElMessage.success(restore ? '此项已恢复通用设置' : '此项已固定为本群设置')
  })
}
function restoreDefaults() {
  if (!inheritance.value) return
  resetValues.value = copySettings(inheritance.value.defaults)
  Object.assign(form, resetValues.value)
  stagedReset.value = Object.keys(form) as (keyof GroupSettings)[]
  emojiPacksText.value = ''; syncTextFields()
}
async function save() {
  const id = editingId.value
  if (await safe(async () => {
    if (emojiPacksText.value.trim()) throw new Error('请先点击“新增表情包”保存输入的链接，或清空新增输入框')
    const reset = pendingReset.value
    const changes = Object.fromEntries(Object.entries(draftSettings()).filter(([key, value]) => !reset.includes(key as keyof GroupSettings) && (stagedReset.value.includes(key as keyof GroupSettings) || JSON.stringify(value) !== JSON.stringify(settingsBaseline.value[key as keyof GroupSettings]))))
    const baseline = Object.fromEntries([...Object.keys(changes), ...reset].map(key => [key, settingsBaseline.value[key as keyof GroupSettings]]))
    if (sharedMode.value) await mutations.submit('/api/settings/defaults', 'PATCH', { settings: changes, baseline: commonBaseline.value }, undefined, '保存通用设置')
    else await api(`/api/groups/${id}/settings`, { method: 'PATCH', body: JSON.stringify({ settings: changes, baseline, ...(reset.length ? { reset } : {}) }) })
    await reloadGroups(); ElMessage.success('已保存'); emit('saved')
  })) { if (editingId.value === id) dialog.value = false }
}
</script>

<template>
<el-dialog v-model="dialog" :title="sharedMode ? '通用设置' : '群设置'" class="settings-dialog" width="min(760px, 94vw)" :before-close="requestClose" :close-on-click-modal="false" destroy-on-close>
<p v-if="sharedMode">修改会应用到没有单独设置该项目的群。</p>
<p class="settings-group-name">{{ group?.title }}</p>
<el-alert v-if="pendingReset.length" title="已将全部分页恢复为通用设置，保存后生效。随后改动的项目将作为本群设置。" type="info" :closable="false"/>
<el-tabs v-model="activeTab" class="settings-tabs">
<el-tab-pane v-for="tab in settingsTabs" :key="tab.name" :label="tab.label" :name="tab.name">
<template v-if="tab.name === 'inheritance' && activeTab === 'inheritance'">
<p v-if="sharedMode">通用设置是各群的继承来源；本群覆盖请在对应群中管理。</p>
<el-table v-else-if="inheritance" :data="inheritedRows">
<el-table-column prop="label" label="设置" min-width="160"/>
<el-table-column label="当前值" min-width="160"><template #default="{ row }">{{ showValue(form[row.key as keyof GroupSettings]) }}</template></el-table-column>
<el-table-column label="通用值" min-width="160"><template #default="{ row }">{{ showValue(inheritance.defaults[row.key as keyof GroupSettings]) }}</template></el-table-column>
<el-table-column label="来源" width="130"><template #default="{ row }">{{ pendingReset.includes(row.key) ? '保存后跟随通用' : inheritance.overrides.includes(row.key) ? '本群设置' : '跟随通用' }}</template></el-table-column>
<el-table-column label="操作" width="130"><template #default="{ row }"><el-button text :disabled="busy" @click="changeInheritance(row.key, inheritance!.overrides.includes(row.key))">{{ inheritance.overrides.includes(row.key) ? '恢复此项' : '固定为本群值' }}</el-button></template></el-table-column>
</el-table>
<p v-else>正在读取继承来源…</p>
</template>
<el-form v-if="activeTab === tab.name" label-position="top" class="settings-form" @submit.prevent="save">
<section v-show="activeTab === 'verification'" aria-label="入群验证设置">
<el-form-item label="验证方式">
<el-select v-model="form.verification_method" aria-label="验证方式">
<el-option value="image" label="图片验证"/>
<el-option value="text" label="文字算术验证"/>
<el-option value="channel" label="订阅频道自动验证"/>
</el-select>
</el-form-item>
<el-form-item v-if="form.verification_method === 'channel'" label="订阅验证频道">
<el-input v-model="form.verification_channel" aria-label="订阅验证频道" placeholder="@频道用户名或 t.me 频道链接"/>
</el-form-item>
<p v-if="form.verification_method === 'channel'" class="muted">机器人须为频道管理员。已订阅者自动通过；其他成员订阅后自动放行，无需点击检查。</p>
<p v-else-if="form.verification_method === 'text'" class="muted">随机加减法题，点击四个选项中的正确答案。仅入群者本人可作答，无需发送文字。</p>
<el-form-item label="已入群成员验证失败处理">
<el-select v-model="form.verification_failure_action" aria-label="验证失败处理">
<el-option value="permanent_mute" label="永久禁言（留在群里）"/>
<el-option value="kick" label="踢出群聊（允许重新加入）"/>
</el-select>
</el-form-item>
<p class="muted">超时或错误次数用尽后执行所选方式；默认永久禁言，可由管理员放行或重出题。申请入群者尚未进群，失败仅拒绝申请。</p>
</section>
<EmergencyProtection v-if="activeTab === 'protection' && group && !sharedMode" :group-id="group.id"/>
<section v-if="['verification', 'moderation', 'archive', 'protection'].includes(activeTab)" :aria-label="activeTab === 'protection' ? '联合刷屏与应急设置' : activeTab === 'moderation' ? '防广告设置' : activeTab === 'archive' ? '消息归档设置' : '验证选项'">
<div class="settings-toggles">
<el-form-item v-for="[key, name] in boolSettings" :key="key" :label="name">
<el-switch v-model="form[key]" :aria-label="name"/>
</el-form-item>
</div>
<div class="settings-grid">
<el-form-item v-for="[key, name] in numericSettings" :key="key" :label="name">
<el-input-number v-model="form[key]" :min="bounds(key).minimum" :max="bounds(key).maximum"/>
</el-form-item>
</div>
</section>
<section v-show="activeTab === 'moderation'" aria-label="防广告处理说明">
<p class="muted">开启“带按钮消息删除并永久封禁”后，普通成员发送带内联按钮的消息会被删除并永久封禁，不限入群时间，也包括未入群的频道评论者。访客机器人代发时处罚发起用户；管理员、防广告白名单和本机器人消息豁免。</p>
<p class="muted">发图门槛仅限有可靠入群记录的新成员：发够指定条数的不同纯文字，或入群满 15 天即解除。没有入群记录的成员免除此门槛，广告检测继续生效。</p>
<p class="muted">开启“只拦截广告联系人名片”时，普通名片可以分享，广告名片和黑名单联系方式会删除消息并永久封禁；关闭时，普通成员分享任何联系人都会封禁。群主、管理员及防广告白名单豁免。</p>
<p class="muted">明确广告招揽和外群链接不限入群时长，删消息并永久封禁。公开链接会先核实群组类型；外部私密邀请统一处理。本群已知链接、允许链接及允许提及账号豁免链接检查；名单不豁免广告正文。明确举报或防骗引用只删外群链接，不自动永久封禁。</p>
<el-form-item label="允许的 Telegram 链接">
<el-input v-model="allowedLinksText" type="textarea" :rows="3" placeholder="每行一个群组、频道或邀请链接"/>
</el-form-item>
<p class="muted">新成员观察期默认 3 天，并检查前 3 条消息；时限和条数都满足后结束。期间限制链接、@账号或群组、用户及频道转发，命中删除消息。钱包及代币合约地址拦截对所有普通成员生效，不限入群时间，命中只删消息。申请模式需要机器人邀请权限，并使用需要审核的入群链接；直进群仍会做群内验证。关闭验证不影响已开始的任务，可批量放行。</p>
</section>
<section v-show="activeTab === 'archive'" aria-label="归档说明"><p class="muted">保存机器人收到的群消息、编辑版本和处置结果。关闭记录不会清除已有数据；图片、视频原文件不会自动下载。已有消息可在“消息归档”中查询和导出。</p></section>
<section v-show="activeTab === 'emoji'" aria-label="状态表情设置">
<el-form-item label="名字旁状态表情包黑名单">
<div style="display:flex;flex-direction:column;gap:6px;width:100%">
<div v-for="pack in form.emoji_status_blocked_packs" :key="pack" style="display:flex;align-items:center;justify-content:space-between;gap:8px">
<span style="overflow-wrap:anywhere">{{ pack }}</span>
<el-button text type="danger" :disabled="busy" :aria-label="`移除 ${pack}`" @click="changeEmojiPacks('remove', pack)">移除</el-button>
</div>
<span v-if="!form.emoji_status_blocked_packs?.length" class="muted">当前名单为空</span>
</div>
</el-form-item>
<el-form-item label="新增状态表情包">
<el-input v-model="emojiPacksText" type="textarea" :rows="2" placeholder="只填写新链接或短名称，多个可以换行；原名单自动保留"/>
</el-form-item>
<div style="display:flex;gap:8px;flex-wrap:wrap">
<el-button :disabled="busy" @click="changeEmojiPacks('add')">新增表情包</el-button>
<el-button type="danger" plain :disabled="busy || !form.emoji_status_blocked_packs?.length" @click="changeEmojiPacks('clear')">清空状态表情包黑名单</el-button>
</div>
<p class="muted">新增、移除和清空会立即保存，不影响其他尚未保存的群设置；重复链接自动忽略。</p>
<p class="muted">入群时命中会永久封禁；群内发言命中会删除对应消息并永久禁言，成员留在群内。仅在入群和发消息时检查名字旁的表情状态，普通贴纸消息不触发这项处罚。程序初始名单：{{ catalog!.settings.defaults.emoji_status_blocked_packs.join('、') || '未设置' }}，实际检查以当前名单为准。清空名单不会自动解除已有封禁或禁言。</p>
</section>
<section v-show="activeTab === 'chat'" aria-label="群聊天设置">
<p class="muted">{{ chatStatus.configured ? `模型：${chatStatus.model}` : '使用前请在服务器填写聊天 API 地址、Key 和模型名。' }} 最近消息按群及话题分开。</p>
<p v-if="chatStatus.budget" class="muted">今日模型请求 {{ chatStatus.budget.group_calls }}/{{ chatStatus.budget.group_call_limit }}；Token 预算 {{ chatStatus.budget.group_tokens }}/{{ chatStatus.budget.group_token_limit }}。额度每日按北京时间重置，由所有者配置；用完只暂停 AI 聊天。</p>
<el-alert v-if="chatStatus.error" :title="chatStatus.error" type="warning" :closable="false"/>
<div class="settings-toggles">
<el-form-item label="开启群聊天">
<el-switch v-model="form.chat_enabled" aria-label="开启群聊天"/>
</el-form-item>
<el-form-item label="主动参与讨论">
<el-switch v-model="form.chat_proactive" aria-label="主动参与讨论"/>
</el-form-item>
<el-form-item label="AI广告识别与累计处罚">
<el-switch v-model="form.chat_advertiser_cooldown" aria-label="AI广告识别与累计处罚"/>
</el-form-item>
</div>
<p class="muted">默认开启。AI判定广告后静默该账号30分钟；本群最近3天累计3次永久禁言。若同时命中“AI确认广告后处罚”的关键词规则，首次即删除消息并永久禁言。同一条消息不重复计数。</p>
<el-form-item label="聊天昵称">
<el-input v-model="form.chat_name" :minlength="bounds('chat_name').minLength" :maxlength="bounds('chat_name').maxLength" placeholder="群友也可以叫这个昵称来唤醒机器人"/>
</el-form-item>
<el-form-item label="性格与说话习惯">
<el-input v-model="form.chat_persona" type="textarea" :rows="3" :maxlength="bounds('chat_persona').maxLength" show-word-limit/>
</el-form-item>
<el-form-item label="聊天忽略口令">
<el-input v-model="ignoredChatText" type="textarea" :rows="3" placeholder="每行一条其他机器人的按钮文字，例如签到、积分、币圈教程"/>
</el-form-item>
<p class="muted">整条消息与口令相同时不进入 AI 聊天或上下文；“今天积分怎么算”等正常句子仍可讨论。积分红包后带数字、明确发给其他机器人的结构化指令直接排除，留空只关闭自定义名单。</p>
<el-form-item label="自动识别重复口令">
<el-switch v-model="form.chat_command_learning" aria-label="自动识别重复口令"/>
</el-form-item>
<el-form-item label="7 天内识别次数（至少 3 位群友）">
<el-input-number v-model="form.chat_command_threshold" :min="bounds('chat_command_threshold').minimum" :max="bounds('chat_command_threshold').maximum"/>
</el-form-item>
<p class="muted">只统计短功能口令；广告、链接、普通问句、重复投递和编辑不计数。达到门槛后从 AI 输入和历史上下文中排除，不删除群消息。</p>
<el-table v-if="chatStatus.commands?.items.length" :data="chatStatus.commands.items" size="small">
<el-table-column prop="text" label="口令"/>
<el-table-column prop="count" label="次数" width="65"/>
<el-table-column prop="users" label="人数" width="65"/>
<el-table-column label="状态" width="80"><template #default="{ row }">{{ row.ignored ? '已忽略' : '观察中' }}</template></el-table-column>
</el-table>
<el-form-item label="口令识别例外">
<el-input v-model="commandExceptionsText" type="textarea" :rows="2" placeholder="每行一条，恢复被自动识别的正常用语"/>
</el-form-item>
<div class="settings-grid">
<el-form-item label="最近上下文条数">
<el-input-number v-model="form.chat_history" :min="bounds('chat_history').minimum" :max="bounds('chat_history').maximum"/>
</el-form-item>
<el-form-item label="主动接话检查间隔（秒）">
<el-input-number v-model="form.chat_interval" :min="bounds('chat_interval').minimum" :max="bounds('chat_interval').maximum"/>
</el-form-item>
</div>
<p class="muted">被 @、叫昵称或被回复时接话；刚聊过的群友可以接着说。不再固定等待，模型生成及接口限流仍需处理时间。普通含图消息不主动接话；明确提问时仍回答，说明看不到图片并依据正文和引用文字，不猜测图中内容。主动接话会先让模型判断是否适合发言。</p>
<div class="settings-toggles">
<el-form-item label="聊天使用贴纸">
<el-switch v-model="form.chat_stickers" aria-label="聊天使用贴纸"/>
</el-form-item>
<el-form-item label="偶尔发防诈贴纸">
<el-switch v-model="form.chat_antifraud" aria-label="偶尔发防诈贴纸"/>
</el-form-item>
</div>
<el-form-item label="聊天贴纸包">
<el-input v-model="chatPacksText" type="textarea" :rows="4" placeholder="每行一个 t.me/addstickers 链接或短名称；留空停用"/>
</el-form-item>
<el-form-item label="聊天贴纸最短间隔（秒）">
<el-input-number v-model="form.chat_sticker_interval" :min="bounds('chat_sticker_interval').minimum" :max="bounds('chat_sticker_interval').maximum"/>
</el-form-item>
<el-form-item label="防诈贴纸包">
<el-input v-model="antifraudPacksText" type="textarea" :rows="2" placeholder="每行一个 addstickers 链接或短名称；默认 FTX888"/>
</el-form-item>
<el-form-item label="防诈提醒最短间隔（秒）">
<el-input-number v-model="form.chat_antifraud_interval" :min="bounds('chat_antifraud_interval').minimum" :max="bounds('chat_antifraud_interval').maximum"/>
</el-form-item>
<p class="muted">AI 可自由选择贴纸包已有的全部 emoji 标签，不限固定情绪；文字里的普通 emoji 也可自由使用。贴纸不重复最近 12 张。防诈提醒只在开启群聊天后、北京时间 9—23 点、最近 10 分钟有讨论且累计 50 条新消息时发送，默认至少间隔 6 小时。</p>
<p v-if="chatStatus.stickers" class="muted">已加载 {{ chatStatus.stickers.ready }}/{{ chatStatus.stickers.total }} 个贴纸包；聊天 emoji 标签：{{ chatStatus.stickers.emoji_count || 0 }} 种<span v-if="chatStatus.stickers.unavailable?.length">；暂不可用：{{ chatStatus.stickers.unavailable.join('、') }}</span>
</p>
<el-alert v-if="chatStatus.stickers?.error" :title="chatStatus.stickers.error" type="warning" :closable="false"/>
<el-button v-if="!sharedMode" @click="resetChat">重新开始聊天上下文</el-button>
</section>
</el-form>
</el-tab-pane>
</el-tabs>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<template #footer>
<el-button v-if="!sharedMode" :disabled="busy || !inheritance" @click="restoreDefaults">全部恢复通用设置</el-button>
<span class="settings-save-note">保存会应用所有分页中的修改</span>
<el-button :disabled="busy" @click="requestClose">取消</el-button>
<el-button type="primary" :loading="busy" @click="save">保存</el-button>
</template>
</el-dialog>
</template>

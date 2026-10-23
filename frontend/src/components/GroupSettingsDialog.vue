<script setup lang="ts">
import { computed, reactive, ref, watch, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, post } from '../api'
import type { ChatStatus, Group, GroupSettings } from '../types'
import { useWorkspace } from '../workspace'
import { useTask } from '../composables/useTask'

const props = defineProps<{ group: Group | null }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const { catalog, groups, reloadGroups } = useWorkspace()
const { busy, error, safe } = useTask()
const dialog = ref(false), editingId = ref('')
const copySettings = (value: GroupSettings): GroupSettings => JSON.parse(JSON.stringify(value))
const form = reactive<GroupSettings>(copySettings(catalog.value!.settings.defaults))
const settingsBaseline = ref<GroupSettings>(copySettings(catalog.value!.settings.defaults))
const emojiPacksText = ref(''), chatPacksText = ref(''), antifraudPacksText = ref('')
const ignoredChatText = ref('')
const chatStatus = ref<Partial<ChatStatus>>({})
type NumericKey = { [K in keyof GroupSettings]: GroupSettings[K] extends number ? K : never }[keyof GroupSettings]
type BooleanKey = { [K in keyof GroupSettings]: GroupSettings[K] extends boolean ? K : never }[keyof GroupSettings]
const numericSettings = computed(() => Object.entries(catalog.value!.settings.numbers) as [NumericKey, string][])
const boolSettings = computed(() => Object.entries(catalog.value!.settings.booleans) as [BooleanKey, string][])
const bounds = (key: keyof GroupSettings) => catalog.value!.settings.fields[key]
let version = 0
watch(() => props.group, group => {
  const current = ++version
  if (!group) { dialog.value = false; return }
  editingId.value = String(group.id)
  Object.assign(form, copySettings({ ...catalog.value!.settings.defaults, ...group.settings }))
  settingsBaseline.value = copySettings({ ...form })
  emojiPacksText.value = ''; chatPacksText.value = form.chat_sticker_packs.join('\n')
  antifraudPacksText.value = form.chat_antifraud_packs.join('\n'); chatStatus.value = {}; error.value = ''
  ignoredChatText.value = form.chat_ignored_texts.join('\n')
  dialog.value = true
  api<ChatStatus>(`/api/groups/${group.id}/chat`).then(data => { if (current === version) chatStatus.value = data })
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
    const result = await post(`/api/groups/${id}/emoji-status-packs`, { operation, packs, request_id: crypto.randomUUID() }) as { packs: string[]; added: string[]; removed: string[] }
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
async function save() {
  const id = editingId.value
  if (await safe(async () => {
    if (emojiPacksText.value.trim()) throw new Error('请先点击“新增表情包”保存输入的链接，或清空新增输入框')
    form.chat_sticker_packs = chatPacksText.value.split(/[\s,，]+/).filter(Boolean)
    form.chat_antifraud_packs = antifraudPacksText.value.split(/[\s,，]+/).filter(Boolean)
    form.chat_ignored_texts = ignoredChatText.value.split(/\r?\n/).map(text => text.trim()).filter(Boolean)
    const changes = Object.fromEntries(Object.entries(form).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(settingsBaseline.value[key as keyof GroupSettings])))
    const baseline = Object.fromEntries(Object.keys(changes).map(key => [key, settingsBaseline.value[key as keyof GroupSettings]]))
    await api(`/api/groups/${id}/settings`, { method: 'PATCH', body: JSON.stringify({ settings: changes, baseline }) })
    await reloadGroups(); ElMessage.success('已保存'); emit('saved')
  })) { if (editingId.value === id) dialog.value = false }
}
</script>

<template>
<el-dialog v-model="dialog" title="群设置" width="min(620px, 94vw)" :close-on-click-modal="false" destroy-on-close>
<el-form label-position="top" @submit.prevent="save">

<el-form-item label="已入群成员验证失败处理">
<el-select v-model="form.verification_failure_action" aria-label="验证失败处理">
<el-option value="permanent_mute" label="永久禁言（留在群里）"/>
<el-option value="kick" label="踢出群聊（允许重新加入）"/>
</el-select>
</el-form-item>
<p class="muted">超时或错误次数用尽后执行所选方式；默认永久禁言，可由管理员放行或重出题。申请入群者尚未进群，失败仅拒绝申请。</p>
<div class="settings-toggles">
<el-form-item v-for="[key, name] in boolSettings" :key="key" :label="name">
<el-switch v-model="form[key]"/>
</el-form-item>
</div>
<div class="settings-grid">
<el-form-item v-for="[key, name] in numericSettings" :key="key" :label="name">
<el-input-number v-model="form[key]" :min="bounds(key).minimum" :max="bounds(key).maximum"/>
</el-form-item>
</div>
<p class="muted">发图门槛仅限有可靠入群记录的新成员：发够指定条数的不同纯文字，或入群满 15 天即解除。没有入群记录的成员免除此门槛，广告检测继续生效。</p>
<p class="muted">普通成员分享联系人会删除消息、踢出并禁止重新加入，不限入群时长；群主、管理员及防广告白名单豁免。</p>
<p class="muted">新成员观察期默认 3 天，并检查前 3 条消息；时限和条数都满足后结束。期间限制链接、@账号或群组、用户及频道转发、加密货币地址，命中删除消息。申请模式需要机器人邀请权限，并使用需要审核的入群链接；直进群仍会做群内验证。关闭验证不影响已开始的任务，可批量放行。</p>


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
<p class="muted">名字旁的表情状态来自这些表情包时，踢出并禁止重新加入；由发言触发检查时还会删除对应消息。仅在入群和发消息时触发检查，普通贴纸消息不触发这项处罚。默认已加入 bygbb666 和 GsHeChaba；清空名单不会自动解除已有封禁或禁言。</p>
<el-divider content-position="left">群聊天</el-divider>
<p class="muted">{{ chatStatus.configured ? `模型：${chatStatus.model}` : '使用前请在服务器填写聊天 API 地址、Key 和模型名。' }} 最近消息按群及话题分开。</p>
<p v-if="chatStatus.budget" class="muted">今日模型请求 {{ chatStatus.budget.group_calls }}/{{ chatStatus.budget.group_call_limit }}；Token 预算 {{ chatStatus.budget.group_tokens }}/{{ chatStatus.budget.group_token_limit }}。额度每日按北京时间重置，由所有者配置；用完只暂停 AI 聊天。</p>
<el-alert v-if="chatStatus.error" :title="chatStatus.error" type="warning" :closable="false"/>
<div class="settings-toggles">
<el-form-item label="开启群聊天">
<el-switch v-model="form.chat_enabled"/>
</el-form-item>
<el-form-item label="主动参与讨论">
<el-switch v-model="form.chat_proactive"/>
</el-form-item>
</div>
<el-form-item label="聊天昵称">
<el-input v-model="form.chat_name" :minlength="bounds('chat_name').minLength" :maxlength="bounds('chat_name').maxLength" placeholder="群友也可以叫这个昵称来唤醒机器人"/>
</el-form-item>
<el-form-item label="性格与说话习惯">
<el-input v-model="form.chat_persona" type="textarea" :rows="3" :maxlength="bounds('chat_persona').maxLength" show-word-limit/>
</el-form-item>
<el-form-item label="聊天忽略口令">
<el-input v-model="ignoredChatText" type="textarea" :rows="3" placeholder="每行一条其他机器人的按钮文字，例如签到、积分、币圈教程"/>
</el-form-item>
<p class="muted">整条消息与口令相同时不进入 AI 聊天或聊天上下文，忽略空白和英文大小写；“今天积分怎么算”等正常句子仍可讨论。留空则关闭口令过滤，不影响其他机器人的功能。</p>
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
<el-switch v-model="form.chat_stickers"/>
</el-form-item>
<el-form-item label="偶尔发防诈贴纸">
<el-switch v-model="form.chat_antifraud"/>
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
<el-button @click="resetChat">重新开始聊天上下文</el-button>

</el-form>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<template #footer>
<el-button @click="dialog = false">取消</el-button>
<el-button type="primary" :loading="busy" @click="save">保存</el-button>
</template>
</el-dialog>
</template>

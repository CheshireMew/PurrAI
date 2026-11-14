<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import type { Rule, SharedPolicy } from '../types'
import { useWorkspace } from '../workspace'
import { useTask } from '../composables/useTask'
import { usePreview } from '../composables/usePreview'
import { useDraftClose } from '../composables/useDraftClose'
import ArchiveRulePreviewDialog from './ArchiveRulePreviewDialog.vue'

const props = defineProps<{ open: boolean; rule: Rule | null; rules: Rule[]; groupId: number }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const { me, mutations } = useWorkspace()
const sharedBaseline = ref<SharedPolicy | null>(null)
const samplesOpen = ref(false)
let revision = 0
const dialog = computed({ get: () => props.open, set: value => { if (!value) emit('close') } })
const { busy, error, safe } = useTask()
const form = reactive<Rule>({ kind: 'keyword', value: '', mode: 'contains', scope: 'message', context_value: '', penalty: 'delete', enabled: true })
const original = ref('')
const { requestClose } = useDraftClose(() => JSON.stringify(form) !== original.value, () => emit('close'), () => busy.value)
watch(() => [form.kind, form.scope], () => {
  if (form.penalty === 'ai_confirmed_mute' && (form.kind !== 'keyword' || form.scope === 'name_and_message')) form.penalty = 'delete'
})
const { input: previewInput, reasons: previewReasons, preview, invalidate } = usePreview(() => props.groupId, () => [form, props.open])
watch(() => props.open, async open => {
  const current = ++revision
  sharedBaseline.value = null
  if (!open) return
  samplesOpen.value = false
  Object.assign(form, { id: undefined, version: undefined, kind: 'keyword', value: '', mode: 'contains', scope: 'message', context_value: '', penalty: 'delete', enabled: true }, props.rule || {})
  original.value = JSON.stringify(form)
  invalidate(); previewInput.text = ''; previewInput.sender_name = ''; error.value = ''
  if (me.value?.root) {
    try {
      const baseline = await api<SharedPolicy>('/api/settings/defaults')
      if (current === revision) sharedBaseline.value = baseline
    } catch (reason) { if (current === revision) error.value = (reason as Error).message }
  }
})
async function previewDraft() {
  await safe(() => preview(undefined, { ...form }))
}
async function save() {
  if (await safe(async () => {
    if (!form.value.trim()) throw new Error('请填写规则内容')
    const path = `/api/groups/${props.groupId}/rules${props.rule?.id ? '/' + props.rule.id : ''}`
    await mutations.submit(path, props.rule?.id ? 'PUT' : 'POST', { ...form }, props.rule?.version, '保存广告规则')
    ElMessage.success('已保存')
  })) { emit('saved'); emit('close') }
}
async function saveShared() {
  if (await safe(async () => {
    if (!form.value.trim()) throw new Error('请填写规则内容')
    const baseline = sharedBaseline.value
    if (!baseline) throw new Error('通用规则尚未加载，请重新打开后重试')
    const index = baseline.rules.findIndex(rule => rule.id === props.rule?.id)
    const rules = baseline.rules.map(rule => ({ ...rule }))
    const draft = { ...form, id: index >= 0 ? props.rule?.id : '' }
    if (index >= 0) rules[index] = draft
    else rules.push(draft)
    await mutations.submit('/api/settings/defaults', 'PATCH', { rules, baseline }, undefined, '保存通用规则')
    ElMessage.success('已保存通用规则')
  })) { emit('saved'); emit('close') }
}
async function inheritRule() {
  if (await safe(async () => {
    await mutations.submit(`/api/groups/${props.groupId}/rules/${props.rule!.id}/inherit`, 'POST', {}, props.rule?.version, '恢复通用规则')
    ElMessage.success('已恢复通用规则')
  })) { emit('saved'); emit('close') }
}
</script>

<template>
<el-dialog v-model="dialog" title="广告规则" class="form-dialog" width="min(620px, 94vw)" :before-close="requestClose" :close-on-click-modal="false" destroy-on-close>
<el-form label-position="top" @submit.prevent="save">

<el-form-item label="规则类型">
<el-select v-model="form.kind">
<el-option value="keyword" label="关键词"/>
<el-option value="block_domain" label="禁止域名"/>
<el-option value="allow_domain" label="允许域名"/>
<el-option value="block_mention" label="禁止引流账号"/>
<el-option value="allow_mention" label="允许提及账号"/>
<el-option value="block_target" label="推广目标黑名单"/>
</el-select>
</el-form-item>
<el-form-item label="规则内容">
<el-input v-model="form.value" :maxlength="250" :placeholder="form.kind === 'block_target' ? '@推广账号、Telegram 链接、频道 ID、手机号、邮箱或网站域名' : '关键词，或 example.com 形式的域名'"/>
</el-form-item>
<el-form-item v-if="form.kind === 'keyword'" label="匹配方式">
<el-radio-group v-model="form.mode">
<el-radio value="contains">包含匹配</el-radio>
<el-radio value="exact">完整匹配</el-radio>
<el-radio value="compact">忽略表情和分隔符</el-radio>
<el-radio value="pinyin">谐音匹配</el-radio>
</el-radio-group>
</el-form-item>
<el-form-item v-if="form.kind === 'keyword'" label="检查范围">
<el-select v-model="form.scope">
<el-option value="message" label="正文（按所选处置处理）"/>
<el-option value="name" :label="form.penalty === 'ai_confirmed_mute' ? '昵称及用户名' : '昵称及用户名（删消息并永久禁言）'"/>
<el-option value="either" :label="form.penalty === 'ai_confirmed_mute' ? '昵称或正文，任一处命中' : '昵称或正文，各自处置'"/>
<el-option value="name_and_message" label="昵称＋正文必须同时命中"/>
</el-select>
</el-form-item>
<el-form-item v-if="form.kind === 'keyword'" :label="form.penalty === 'ai_confirmed_mute' ? '关键词命中处理' : '正文命中处理'">
<el-select v-model="form.penalty" aria-label="关键词命中处理">
<el-option value="delete" label="只删消息"/>
<el-option value="permanent_mute" label="删消息并永久禁言"/>
<el-option value="ai_confirmed_mute" label="AI确认广告后删消息并永久禁言" :disabled="form.scope === 'name_and_message'"/>
</el-select>
</el-form-item>
<el-form-item v-if="form.kind === 'keyword' && form.scope === 'name_and_message'" label="正文还需同时命中的关键词">
<el-input v-model="form.context_value" maxlength="250"/>
</el-form-item>
<p v-if="form.penalty === 'ai_confirmed_mute'" class="muted">关键词本身不处罚。沿用现有AI聊天开关、参与条件、冷却和额度；实际判定为广告后，首次命中即删除消息并永久禁言。谐音匹配可识别同音写法。</p>
<p v-else class="muted">只匹配你配置的词。谐音匹配兼容简繁体、分隔符和汉字间夹入的字母数字。昵称违规删除当条消息并永久禁言；正文违规按本条规则选择只删消息，或删消息并永久禁言，不累计警告。管理员手动解禁后自动加入本群白名单。</p>
<el-form-item label="启用">
<el-switch v-model="form.enabled" aria-label="启用规则"/>
</el-form-item>
<el-alert v-if="form.kind === 'block_target'" title="推广目标命中会删除消息并永久封禁；明确举报引用仅删除消息。停用或修改通用规则会影响继承它的群。" type="warning" :closable="false"/>
<el-form-item label="测试昵称及用户名">
<el-input v-model="previewInput.sender_name" placeholder="例如：显示昵称 @username"/>
</el-form-item>
<el-form-item label="保存前测试">
<el-input v-model="previewInput.text" type="textarea" :rows="2" placeholder="输入消息，测试这次修改后的规则"/>
</el-form-item>
<el-button :loading="busy" @click="previewDraft">预览当前草稿</el-button>
<el-button @click="samplesOpen = true">用归档样本比较草稿</el-button>
<el-alert v-if="previewReasons !== null" :type="previewReasons.length ? 'warning' : 'success'" :title="previewReasons.length ? previewReasons.join('；') : '未命中规则'" :closable="false" style="margin-top:12px"/>

</el-form>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<template #footer>
<el-button v-if="props.rule?.inherited_from && !props.rule.inherited" :loading="busy" @click="inheritRule">恢复通用规则</el-button>
<el-button v-if="me?.root" :loading="busy" :disabled="!sharedBaseline" @click="saveShared">保存为通用规则</el-button>
<el-button :disabled="busy" @click="requestClose">取消</el-button>
<el-button type="primary" :loading="busy" @click="save">保存</el-button>
</template>
</el-dialog>
<ArchiveRulePreviewDialog :open="samplesOpen && open" :group-id="groupId" :draft="form" @close="samplesOpen = false"/>
</template>

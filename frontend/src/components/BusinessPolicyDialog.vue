<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import type { BusinessDocument, BusinessPolicy, BusinessRule } from '../businessPolicy'
import { actions, expression } from '../businessPolicy'
import { useWorkspace } from '../workspace'
import { useTask } from '../composables/useTask'
import { useDraftClose } from '../composables/useDraftClose'
import { usePreviewResult } from '../composables/usePreviewResult'
import ConditionEditor from './ConditionEditor.vue'
import TextStepsEditor from './TextStepsEditor.vue'

const props = defineProps<{ open: boolean; groupId: number }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const dialog = computed({ get: () => props.open, set: value => { if (!value) emit('close') } })
const { mutations } = useWorkspace()
const { busy, error, safe } = useTask()
const baseline = ref<BusinessPolicy | null>(null), document = ref<BusinessDocument | null>(null)
const tab = ref('rules'), stageId = ref('advertising'), ruleId = ref(''), conditionId = ref(''), viewId = ref('')
const stage = computed(() => document.value?.stages.find(item => item.id === stageId.value))
const rule = computed(() => stage.value?.rules.find(item => item.id === ruleId.value))
const condition = computed(() => document.value?.conditions.find(item => item.id === conditionId.value))
const view = computed(() => document.value?.views.find(item => item.id === viewId.value))
const sample = ref({ text: '', sender_name: '', source_title: '', contact: false, contact_name: '', direct: false })
type Preview = { reasons: string[]; name_reasons: string[]; ban_member: boolean; mute_member: boolean; delete_message: boolean; chat_reasons: string[] }
const { result, run: runPreview, invalidate } = usePreviewResult<{ before: Preview; after: Preview; changed: boolean }>(() => [document.value, sample.value, props.groupId, props.open])
const dirty = computed(() => !!baseline.value && JSON.stringify(document.value) !== JSON.stringify(baseline.value.document))
const { requestClose } = useDraftClose(() => dirty.value, () => emit('close'), () => busy.value)
let revision = 0
async function load() {
  const current = ++revision
  await safe(async () => {
    const response = await api<BusinessPolicy>('/api/settings/business-policy')
    if (current !== revision || !props.open) return
    baseline.value = response; document.value = structuredClone(response.document)
    stageId.value = response.document.stages.find(item => item.id === 'advertising')?.id || response.document.stages[0]?.id || ''
    conditionId.value = response.document.conditions[0]?.id || ''; viewId.value = response.document.views[0]?.id || ''
    ruleId.value = stage.value?.rules[0]?.id || ''; invalidate()
  })
}
watch(() => props.open, open => { if (open) { baseline.value = null; document.value = null; error.value = ''; tab.value = 'rules'; void load() } else revision++ })
watch(stageId, () => { ruleId.value = stage.value?.rules[0]?.id || '' })
function addRule() {
  if (!stage.value) return
  const item: BusinessRule = { id: 'rule-' + crypto.randomUUID(), title: '新规则', reason: '新规则命中', enabled: true,
    action: stage.value.id.startsWith('chat_') ? 'exclude' : 'delete', condition: expression() }
  stage.value.rules.push(item); ruleId.value = item.id
}
function removeRule() {
  if (!stage.value || !rule.value) return
  stage.value.rules = stage.value.rules.filter(item => item.id !== ruleId.value); ruleId.value = stage.value.rules[0]?.id || ''
}
function addCondition() {
  if (!document.value) return
  conditionId.value = 'condition-' + crypto.randomUUID()
  document.value.conditions.push({ id: conditionId.value, title: '新公共条件', enabled: true, condition: expression() })
}
function addView() {
  if (!document.value) return
  viewId.value = 'view-' + crypto.randomUUID()
  document.value.views.push({ id: viewId.value, title: '新文本处理', source: 'text', steps: [] })
}
function moveRule(delta: number) {
  if (!stage.value) return
  const index = stage.value.rules.findIndex(item => item.id === ruleId.value), target = index + delta
  if (index < 0 || target < 0 || target >= stage.value.rules.length) return
  stage.value.rules.splice(target, 0, stage.value.rules.splice(index, 1)[0]!)
}
async function preview() {
  await safe(async () => {
    const payload = JSON.stringify({ group_id: props.groupId, document: document.value, ...sample.value })
    await runPreview(() => api('/api/settings/business-policy/preview', { method: 'POST', body: payload }))
  })
}
async function save() {
  await safe(async () => {
    const sent = JSON.stringify(document.value)
    const saved = await mutations.submit<BusinessPolicy>('/api/settings/business-policy', 'PUT',
      { document: document.value }, baseline.value?.version, '保存业务规则')
    baseline.value = saved
    if (JSON.stringify(document.value) === sent) document.value = structuredClone(saved.document)
    ElMessage.success('业务规则已保存，后续识别和处罚复核立即使用新规则'); emit('saved')
  })
}
function conclusion(value: Preview) {
  return [value.ban_member ? '删除并永久封禁' : value.mute_member ? '删除并永久禁言' : value.delete_message ? '只删除消息' : '不处罚',
    value.chat_reasons.length ? '排除聊天' : '允许进入聊天'].join('；')
}
</script>

<template>
<el-dialog v-model="dialog" title="业务组合规则" class="business-policy-dialog form-dialog" top="3vh" width="min(1120px, 96vw)" :before-close="requestClose" :close-on-click-modal="false" destroy-on-close>
<p>这些规则对所有群生效。群内的“明确广告引流永久封禁”开关继续控制广告检查；聊天排除规则只影响聊天。保存前可比较当前规则和草稿的结果。</p>
<el-alert v-if="error" :title="error" type="error" show-icon @close="error = ''"/>
<div v-if="document" v-loading="busy">
<el-tabs v-model="tab">
<el-tab-pane name="rules" label="识别规则">
  <div class="stage-line"><el-select v-model="stageId" aria-label="业务规则组"><el-option v-for="item in document.stages" :key="item.id" :value="item.id" :label="item.title"/></el-select><el-switch v-if="stage" v-model="stage.enabled" active-text="启用规则组" aria-label="启用业务规则组"/></div>
  <template v-if="stage">
    <div class="policy-columns"><div class="rule-list"><el-button @click="addRule">添加组合规则</el-button><button v-for="item in stage.rules" :key="item.id" type="button" :class="{ chosen: item.id === ruleId }" @click="ruleId = item.id">{{ item.title }}<span>{{ item.enabled ? actions[item.action] : '已停用' }}</span></button></div>
    <el-form v-if="rule" label-position="top" class="rule-form">
      <el-form-item label="规则名称"><el-input v-model="rule.title" aria-label="业务规则名称" maxlength="160"/></el-form-item>
      <div class="stage-line"><el-switch v-model="rule.enabled" active-text="启用本条规则" aria-label="启用业务规则"/><el-select v-model="rule.action" aria-label="业务规则处置"><el-option v-for="action in stage.id.startsWith('chat_') ? ['exclude'] : ['delete', 'mute', 'ban']" :key="action" :value="action" :label="actions[action]"/></el-select></div>
      <el-form-item label="命中条件"><ConditionEditor v-model="rule.condition" :document="document"/></el-form-item>
      <el-form-item label="命中说明"><el-input v-model="rule.reason" aria-label="业务规则命中说明" maxlength="500"/></el-form-item>
      <el-form-item label="说明末尾附加的文本"><el-select :model-value="rule.reason_field || ''" aria-label="命中说明附加文本" @change="$event ? rule.reason_field = $event : delete rule.reason_field"><el-option value="" label="不附加文本"/><el-option v-for="item in document.views" :key="item.id" :value="item.id" :label="item.title"/></el-select></el-form-item>
      <div class="stage-line"><el-button @click="moveRule(-1)">提前检查</el-button><el-button @click="moveRule(1)">延后检查</el-button><el-button @click="removeRule">移除此规则</el-button></div>
    </el-form><el-empty v-else description="本组没有识别规则"/></div>
    <el-collapse><el-collapse-item title="规则组顺序与公共豁免条件" name="stage">
      <el-input v-model="stage.title" aria-label="规则组名称"/><el-checkbox v-model="stage.first_match">命中首条后停止本组检查</el-checkbox>
      <el-select v-model="stage.before" multiple aria-label="先检查的规则组"><el-option v-for="item in document.stages.filter(item => item.id !== stageId && item.id.startsWith('chat_') === stage!.id.startsWith('chat_'))" :key="item.id" :value="item.id" :label="item.title"/></el-select>
      <ConditionEditor v-model="stage.condition" :document="document"/>
    </el-collapse-item></el-collapse>
  </template>
</el-tab-pane>
<el-tab-pane name="conditions" label="公共条件与豁免">
  <el-select v-model="conditionId" aria-label="公共条件"><el-option v-for="item in document.conditions" :key="item.id" :value="item.id" :label="item.title"/></el-select>
  <template v-if="condition"><el-input v-model="condition.title" aria-label="公共条件名称"/><el-switch v-model="condition.enabled" active-text="启用公共条件"/><ConditionEditor v-model="condition.condition" :document="document"/></template>
  <el-button @click="addCondition">添加公共条件</el-button>
</el-tab-pane>
<el-tab-pane name="views" label="文本处理">
  <el-select v-model="viewId" aria-label="文本处理视图"><el-option v-for="item in document.views" :key="item.id" :value="item.id" :label="item.title"/></el-select>
  <template v-if="view"><el-input v-model="view.title" aria-label="文本处理名称"/><el-select v-model="view.source" aria-label="处理前的文本"><el-option value="text" label="原始文本"/><el-option v-for="item in document.views.filter(item => item.id !== viewId)" :key="item.id" :value="item.id" :label="item.title"/></el-select><TextStepsEditor v-model="view.steps"/></template>
  <el-button @click="addView">添加文本处理</el-button>
</el-tab-pane>
</el-tabs>
<section class="draft-preview">
<h3>比较当前规则和草稿</h3>
<el-input v-model="sample.sender_name" aria-label="业务规则测试昵称" placeholder="昵称及用户名"/>
<el-input v-model="sample.text" type="textarea" :rows="3" aria-label="业务规则测试正文" placeholder="测试正文"/>
<el-input v-model="sample.source_title" aria-label="业务规则测试转发来源" placeholder="转发来源标题，可留空"/>
<div class="stage-line"><el-checkbox v-model="sample.direct">用户直接提问</el-checkbox><el-checkbox v-model="sample.contact">附带联系人</el-checkbox><el-input v-if="sample.contact" v-model="sample.contact_name" aria-label="业务规则测试联系人" placeholder="联系人名称"/><el-button :loading="busy" @click="preview">比较规则草稿</el-button></div>
<div v-if="result" class="preview-columns"><div v-for="(value, name) in { '当前规则': result.before, '草稿规则': result.after }" :key="name"><h4>{{ name }}</h4><p>{{ conclusion(value) }}</p><p>{{ [...value.reasons, ...value.name_reasons, ...value.chat_reasons].filter((reason, index, items) => items.indexOf(reason) === index).join('；') || '未命中规则' }}</p></div></div>
</section>
</div>
<template #footer><span v-if="dirty" class="unsaved">草稿尚未保存</span><el-button :disabled="busy" @click="requestClose">关闭</el-button><el-button type="primary" :disabled="!document || !dirty" :loading="busy" @click="save">保存业务规则</el-button></template>
</el-dialog>
</template>
<style scoped>
:global(.business-policy-dialog) { max-height: 94dvh; display: flex; flex-direction: column; margin-bottom: 3vh }
:global(.business-policy-dialog .el-dialog__body) { min-height: 0; overflow-y: auto }
:global(.business-policy-dialog .el-dialog__header), :global(.business-policy-dialog .el-dialog__footer) { flex-shrink: 0 }
.stage-line { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 10px 0 }.stage-line .el-select { width: 280px }
.policy-columns { display: grid; grid-template-columns: 230px minmax(0, 1fr); gap: 16px; margin: 16px 0 }
.rule-list { display: flex; flex-direction: column; gap: 8px; max-height: 650px; overflow-y: auto }
.rule-list > button:not(.el-button) { text-align: left; background: var(--el-fill-color-blank); border: 1px solid var(--el-border-color); border-radius: 8px; padding: 10px; color: var(--el-text-color-primary); cursor: pointer; overflow-wrap: anywhere }
.rule-list > button.chosen { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9) }.rule-list span { display: block; color: var(--el-text-color-secondary); margin-top: 4px; font-size: 12px }
.rule-form { min-width: 0 }.rule-form :deep(.el-form-item__content) { display: block }.draft-preview { border-top: 1px solid var(--el-border-color); margin-top: 20px; padding-top: 10px; display: grid; gap: 8px }
.preview-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 12px }.preview-columns > div { background: var(--el-fill-color-light); border-radius: 8px; padding: 12px; overflow-wrap: anywhere }.unsaved { margin-right: 12px; color: var(--el-text-color-secondary) }
:deep(.el-tab-pane) > .el-input, :deep(.el-tab-pane) > .el-select { margin: 8px 0 }
@media(max-width: 700px) { .policy-columns, .preview-columns { grid-template-columns: 1fr }.rule-list { max-height: 180px }.stage-line .el-select { width: 100% } }
</style>

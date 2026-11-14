<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { useTask } from '../composables/useTask'
import { useWorkspace } from '../workspace'
import type { Rule, Violation } from '../types'
import { actionContext } from '../actionContext'
import { useDraftClose } from '../composables/useDraftClose'

type Evidence = { rules: (Rule & {global_version: string; other_sources: number})[]; global_allowed: boolean;
  affected_groups: {id: number; title: string; rule_ids?: string[]}[]; user_id: number; excerpt: string }
const props = defineProps<{ groupId: number; violation: Violation | null }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const open = computed({ get: () => Boolean(props.violation), set: value => { if (!value) emit('close') } })
const { mutations, actionTracker, groups } = useWorkspace()
const { busy, error, safe } = useTask()
const evidence = ref<Evidence | null>(null), selected = ref<string[]>([]), scope = ref('local'), reason = ref(''), action = ref('')
const { requestClose } = useDraftClose(() => !!reason.value || !!action.value || selected.value.length > 0 || scope.value !== 'local', () => emit('close'), () => busy.value)
let revision = 0
watch(() => props.violation, async row => {
  const current = ++revision
  evidence.value = null; selected.value = []; scope.value = 'local'; reason.value = ''; action.value = ''; error.value = ''
  if (row) await safe(async () => {
    const result = await api<Evidence>(`/api/groups/${props.groupId}/violations/${row.id}/correction`)
    if (current === revision) evidence.value = result
  })
})
async function save() {
  const row = props.violation, data = evidence.value
  if (!row || !data) return
  if (await safe(async () => {
    if (!reason.value.trim()) throw new Error('请填写核实原因')
    const rule_versions = Object.fromEntries(data.rules.filter(rule => selected.value.includes(rule.id!))
      .map(rule => [rule.id!, scope.value === 'global' ? rule.global_version : rule.version]))
    const result = await mutations.submit<{action_ids: string[]}>(`/api/groups/${props.groupId}/violations/${row.id}/correction`,
      'POST', { reason: reason.value.trim(), action: action.value, scope: scope.value, rule_versions }, undefined, '纠正误判')
    const group = groups.value.find(item => item.id === props.groupId)
    for (const id of result.action_ids) actionTracker.accept({ id, state: 'pending', reason: reason.value }, reason.value,
      group ? actionContext(group, action.value || '纠正误判', row.user_id, row.sender_name) : undefined)
    ElMessage.success(result.action_ids.length ? '纠正已保存，解除限制任务已提交' : '纠正已保存')
  })) { emit('saved'); emit('close') }
}
</script>

<template>
<el-dialog v-model="open" title="纠正误判" class="form-dialog" width="min(720px, 94vw)" :before-close="requestClose" :close-on-click-modal="false">
<div v-loading="busy">
<p>成员 {{ violation?.user_id }} · {{ violation?.excerpt }}</p>
<p>保留原始证据和处置历史，取消尚未执行的相关自动处罚。已删除的消息无法原地恢复。</p>
<el-alert v-if="error" :title="error" type="error" :closable="false"/>
<el-form v-if="evidence" label-position="top">
<el-form-item label="核实原因"><el-input v-model="reason" type="textarea" maxlength="300"/></el-form-item>
<el-form-item label="成员限制">
<el-select v-model="action" aria-label="纠正成员限制">
<el-option value="" label="保留当前限制，仅登记误判和处理规则"/>
<el-option v-for="operation in violation?.allowed_operations.filter(item => ['unmute', 'unban'].includes(item))" :key="operation" :value="operation" :label="operation === 'unban' ? '解除这次 PurrAI 封禁' : '解除这次 PurrAI 禁言'"/>
</el-select></el-form-item>
<el-form-item v-if="evidence.rules.length" label="关联的推广目标（勾选后停用）">
<el-checkbox-group v-model="selected">
<div v-for="rule in evidence.rules" :key="rule.id"><el-checkbox :value="rule.id">{{ rule.value }} · {{ rule.enabled ? '启用' : '停用' }} · 其他独立来源 {{ rule.other_sources }} 条</el-checkbox></div>
</el-checkbox-group></el-form-item>
<p v-else>这条记录没有关联的自动学习推广目标。</p>
<el-form-item v-if="selected.length" label="停用范围">
<el-radio-group v-model="scope"><el-radio value="local">只在本群停用</el-radio><el-radio v-if="evidence.global_allowed" value="global">停用通用规则</el-radio></el-radio-group>
</el-form-item>
<el-alert v-if="selected.length && scope === 'global'" :title="`将影响继承这些通用规则的群：${evidence.affected_groups.filter(group => !group.rule_ids || group.rule_ids.some(id => selected.includes(id))).map(group => group.title).join('、') || '无'}。已设置本群覆盖的规则保留；已有处罚不会批量解除。`" type="warning" :closable="false"/>
</el-form>
</div>
<template #footer><el-button :disabled="busy" @click="requestClose">取消</el-button><el-button type="primary" :loading="busy" :disabled="!evidence" @click="save">确认纠正</el-button></template>
</el-dialog>
</template>

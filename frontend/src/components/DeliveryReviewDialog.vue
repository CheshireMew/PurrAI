<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useWorkspace } from '../workspace'
import { useTask } from '../composables/useTask'
import type { Operation } from '../types'
import { useDraftClose } from '../composables/useDraftClose'

const props = defineProps<{ action: Operation | null }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const open = computed({ get: () => Boolean(props.action), set: value => { if (!value) emit('close') } })
const { mutations } = useWorkspace(), { busy, error, safe } = useTask()
const outcome = ref(''), reason = ref('')
const { requestClose } = useDraftClose(() => !!outcome.value || !!reason.value, () => emit('close'), () => busy.value)
watch(() => props.action, () => { outcome.value = ''; reason.value = ''; error.value = '' })
async function submit() {
  if (await safe(async () => {
    if (!outcome.value || !reason.value.trim()) throw new Error('请选择实际结果并填写核实情况')
    await mutations.submit(`/api/actions/${props.action!.id}/resolve`, 'POST', {
      outcome: outcome.value, reason: reason.value.trim(),
    }, undefined, '登记人工核实结果')
    ElMessage.success('核实结果已登记，任务不会自动重发')
  })) { emit('saved'); emit('close') }
}
</script>

<template>
<el-dialog v-model="open" title="登记发送核实结果" class="form-dialog" width="min(600px, 94vw)" :before-close="requestClose" :close-on-click-modal="false">
<p>任务 {{ action?.id }} · {{ action?.error }}</p>
<p>请先在 Telegram 核实实际结果。登记后保留原始错误和人工说明，从待处理失败列表移出；不会自动重发，也不会把人工判断记成 Telegram API 成功回执。</p>
<el-form label-position="top"><el-form-item label="实际核实结果"><el-radio-group v-model="outcome"><el-radio value="completed">确认已完成</el-radio><el-radio value="not_completed">确认未完成</el-radio></el-radio-group></el-form-item><el-form-item label="核实情况"><el-input v-model="reason" type="textarea" maxlength="300"/></el-form-item></el-form>
<el-alert v-if="error" :title="error" type="error" :closable="false"/>
<template #footer><el-button :disabled="busy" @click="requestClose">取消</el-button><el-button type="primary" :loading="busy" @click="submit">登记核实结果</el-button></template>
</el-dialog>
</template>

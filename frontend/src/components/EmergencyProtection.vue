<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api, time } from '../api'
import { useWorkspace } from '../workspace'
import { useTask } from '../composables/useTask'

const props = defineProps<{groupId: number}>()
const { mutations } = useWorkspace(), { busy, error, safe } = useTask()
const state = ref<{active: boolean; until?: number; reason?: string} | null>(null)
async function load() { await safe(async () => { state.value = await api(`/api/groups/${props.groupId}/emergency`) }) }
watch(() => props.groupId, load, {immediate: true})
async function change() {
  const active = !state.value?.active
  let reason = ''
  try {
    const result = await ElMessageBox.prompt(active ? '临时要求新成员验证并拦截新人链接，到期恢复原配置。请填写启用原因。' : '恢复日常配置，已经产生的验证和处罚保留。请填写恢复原因。', active ? '启用临时应急保护' : '恢复日常保护', {inputValidator: value => !!value?.trim() && value.trim().length <= 300 || '请填写 1 至 300 字的原因', confirmButtonText: '确认', cancelButtonText: '取消'})
    reason = result.value.trim()
  } catch { return }
  if (await safe(() => mutations.submit(`/api/groups/${props.groupId}/emergency`, 'POST', {active, reason}, undefined, '调整临时应急保护'))) await load()
}
</script>

<template>
<section v-loading="busy"><h3>临时应急保护</h3><el-alert v-if="error" :title="error" type="error" :closable="false"/>
<p v-if="state?.active">正在启用，{{ time(state.until || 0) }} 自动结束。原因：{{ state.reason }}</p><p v-else>当前使用日常配置。集中入群检测默认关闭，可在下方开启。</p>
<p>应急期间要求新入群成员完成验证，并暂时拦截新人的链接消息；保留管理员和白名单豁免。结束后恢复原配置，已生成的验证与成员处罚仍需单独处理。</p>
<el-button :disabled="busy || !state" @click="change">{{ state?.active ? '恢复日常保护' : '启用临时应急保护' }}</el-button></section>
</template>

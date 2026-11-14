<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { post } from '../api'
import { useWorkspace } from '../workspace'
import { useTask } from '../composables/useTask'
import type { BatchReceipt } from '../actionTracker'
import { actionContext } from '../actionContext'
import { useDraftClose } from '../composables/useDraftClose'

const props = defineProps<{ groupId: number; selection: Record<string, unknown> | null }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const open = computed({ get: () => Boolean(props.selection), set: value => { if (!value) emit('close') } })
const { mutations, actionTracker, groups } = useWorkspace()
const group = computed(() => groups.value.find(item => item.id === props.groupId))
const { busy, error, safe } = useTask()
const preview = ref<{ items: {id: string; user_id: number; user_name: string; state: string}[]; total: number; snapshot: string } | null>(null)
const reason = ref(''), previewPage = ref(1)
const { requestClose } = useDraftClose(() => !!reason.value, () => emit('close'), () => busy.value)
let revision = 0
watch(() => props.selection, async selection => {
  const current = ++revision
  preview.value = null; reason.value = ''; error.value = ''; previewPage.value = 1
  if (selection) await safe(async () => {
    const result = await post(`/api/groups/${props.groupId}/release-pending/preview`, selection)
    if (current === revision) preview.value = result
  })
})
async function submit() {
  const reviewed = preview.value
  if (!reviewed?.total) return
  if (await safe(async () => {
    if (!reason.value.trim()) throw new Error('请填写放行原因')
    const receipt = await mutations.submit<BatchReceipt>(`/api/groups/${props.groupId}/release-pending`, 'POST', {
      action: 'release', reason: reason.value.trim(), verification_ids: reviewed.items.map(item => item.id), snapshot: reviewed.snapshot,
    }, undefined, `${group.value?.title || props.groupId} · 批量放行 ${reviewed.total} 位成员`)
    if (group.value) actionTracker.accept(receipt, reason.value.trim(), { ...actionContext(group.value, 'batch_release'), member_count: reviewed.total })
    ElMessage.success(`已提交 ${receipt.action_ids.length} 个放行任务`)
  })) { emit('saved'); emit('close') }
}
</script>

<template>
<el-dialog v-model="open" title="核对批量放行名单" class="form-dialog" width="min(680px, 94vw)" :before-close="requestClose" :close-on-click-modal="false">
<div v-loading="busy"><el-alert v-if="error" :title="error" type="error" :closable="false"/>
<template v-if="preview"><p>{{ group?.title }}（{{ groupId }}） · 本次只放行下面的 {{ preview.total }} 位成员。预览后新加入的成员不会被包含；成员状态变化时需要重新核对。</p>
<el-table :data="preview.items.slice((previewPage - 1) * 10, previewPage * 10)" max-height="340"><el-table-column prop="user_name" label="成员"/><el-table-column prop="user_id" label="用户 ID"/></el-table>
<el-pagination v-model:current-page="previewPage" :page-size="10" :total="preview.total" layout="prev, pager, next, total"/>
<el-form label-position="top"><el-form-item label="放行原因"><el-input v-model="reason" maxlength="300"/></el-form-item></el-form>
</template></div>
<template #footer><el-button :disabled="busy" @click="requestClose">取消</el-button><el-button type="primary" :loading="busy" :disabled="!preview?.total" @click="submit">确认放行名单中的成员</el-button></template>
</el-dialog>
</template>

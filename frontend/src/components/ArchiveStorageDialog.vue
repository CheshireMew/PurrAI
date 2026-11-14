<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, time } from '../api'
import { useTask } from '../composables/useTask'
import { useWorkspace } from '../workspace'
import { useDraftClose } from '../composables/useDraftClose'

type Capacity = { bytes: number; limit_bytes: number; group_bytes: number; group_limit_bytes: number | null;
  groups: {id: number; title: string; bytes: number; limit_bytes: number | null}[]; can_edit_quotas: boolean }
type Receipt = { id: string; count: number; created_at: number; purged: boolean; filters: Record<string, unknown> }
const props = defineProps<{ open: boolean; groupId: number }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const visible = computed({ get: () => props.open, set: value => { if (!value) emit('close') } })
const { mutations } = useWorkspace(), { busy, error, safe } = useTask()
const capacity = ref<Capacity | null>(null), receipts = ref<Receipt[]>([])
const globalMb = ref(512), groupMb = ref(128), limited = ref(false), selected = ref(''), reason = ref(''), savedFile = ref(false)
const quota = () => JSON.stringify([globalMb.value, limited.value ? groupMb.value : null])
const baseline = ref('')
const { requestClose } = useDraftClose(() => (!!capacity.value && capacity.value.can_edit_quotas && quota() !== baseline.value) || !!selected.value || !!reason.value || savedFile.value, () => emit('close'), () => busy.value)
const mb = (bytes: number) => `${(bytes / 1048576).toFixed(2)} MB`
let revision = 0
async function load(preserveDraft = false) {
  const current = ++revision
  await safe(async () => {
    const [value, exports] = await Promise.all([api<Capacity>(`/api/groups/${props.groupId}/archive/capacity`),
      api<Receipt[]>(`/api/groups/${props.groupId}/archive/exports`)])
    if (current !== revision) return
    capacity.value = value; receipts.value = exports
    if (!preserveDraft) {
      globalMb.value = Math.ceil(value.limit_bytes / 1048576)
      groupMb.value = value.group_limit_bytes ? Math.ceil(value.group_limit_bytes / 1048576) : 128
      limited.value = value.group_limit_bytes !== null; baseline.value = quota()
    }
  })
}
watch(() => props.open, open => { if (open) { capacity.value = null; selected.value = ''; reason.value = ''; savedFile.value = false; void load() } else revision++ })
async function saveQuota() {
  if (await safe(async () => {
    const sent = quota()
    await mutations.submit(`/api/groups/${props.groupId}/archive/capacity`, 'PATCH', {
      group_mb: limited.value ? groupMb.value : null, global_mb: globalMb.value,
    }, undefined, '修改归档容量')
    baseline.value = sent
    ElMessage.success('额度已保存，后续写入按新额度检查')
  })) { await load(true); emit('saved') }
}
async function purge() {
  const receipt = receipts.value.find(item => item.id === selected.value)
  if (!receipt || !savedFile.value || !reason.value.trim()) { error.value = '请选择导出记录，确认已保存文件并填写整理原因'; return }
  try { await ElMessageBox.confirm(`确认整理这次导出的 ${receipt.count} 条归档及其版本？不会删除 Telegram 消息、违规证据或操作历史；导出后内容变化时会拒绝清理。`, '整理已导出归档', { confirmButtonText: '确认整理', cancelButtonText: '取消', type: 'warning' }) } catch { return }
  if (await safe(async () => {
    const result = await mutations.submit<{removed: number}>(`/api/groups/${props.groupId}/archive/purge`, 'POST', {
      export_id: receipt.id, confirm_count: receipt.count, reason: reason.value.trim(),
    }, undefined, '整理已导出归档')
    ElMessage.success(`已整理 ${result.removed} 条，数据库空闲页可供后续写入复用`)
  })) { selected.value = ''; reason.value = ''; savedFile.value = false; await load(true); emit('saved') }
}
</script>

<template>
<el-dialog v-model="visible" title="归档容量与整理" class="form-dialog" width="min(760px, 94vw)" :before-close="requestClose" :close-on-click-modal="false">
<div v-loading="busy"><el-alert v-if="error" :title="error" type="error" :closable="false"/>
<template v-if="capacity"><p>全局 {{ mb(capacity.bytes) }} / {{ mb(capacity.limit_bytes) }}；本群 {{ mb(capacity.group_bytes) }} / {{ capacity.group_limit_bytes ? mb(capacity.group_limit_bytes) : '共用全局剩余额度' }}</p>
<el-table v-if="capacity.groups.length" :data="capacity.groups" max-height="240"><el-table-column prop="title" label="群"/><el-table-column label="占用"><template #default="{ row }">{{ mb(row.bytes) }}</template></el-table-column><el-table-column label="分配上限"><template #default="{ row }">{{ row.limit_bytes ? mb(row.limit_bytes) : '共用全局额度' }}</template></el-table-column></el-table>
<el-form v-if="capacity.can_edit_quotas" label-position="top"><el-form-item label="全局归档上限（MB）"><el-input-number v-model="globalMb" :min="16" :max="1048576"/></el-form-item><el-form-item label="本群分配上限"><el-checkbox v-model="limited">单独限制本群额度</el-checkbox><el-input-number v-if="limited" v-model="groupMb" :min="1" :max="1048576"/></el-form-item></el-form>
<h3>整理已导出的记录</h3><p>先在归档页面按时间等条件导出并保存文件，再点击下方刷新。只有完整导出且内容未再变化的记录可以整理，每次最多 5000 条；已有违规证据和操作历史保留。清理不会缩小数据库文件，空闲页供后续写入复用。</p>
<el-button :disabled="busy" @click="load(true)">刷新导出记录</el-button>
<el-form label-position="top"><el-form-item label="已完成的导出"><el-select v-model="selected" aria-label="选择已完成导出"><el-option v-for="receipt in receipts.filter(item => !item.purged && item.count > 0)" :key="receipt.id" :value="receipt.id" :label="`${time(receipt.created_at)} · ${receipt.count} 条`"/></el-select></el-form-item><el-form-item label="整理原因"><el-input v-model="reason" maxlength="300"/></el-form-item><el-checkbox v-model="savedFile">已保存并核对该次导出的文件</el-checkbox><p><el-button type="danger" :disabled="!selected || !savedFile || busy" @click="purge">整理选中导出的归档</el-button></p></el-form>
</template></div>
<template #footer><el-button :disabled="busy" @click="requestClose">关闭</el-button><el-button v-if="capacity?.can_edit_quotas" type="primary" :loading="busy" @click="saveQuota">保存归档额度</el-button></template>
</el-dialog>
</template>

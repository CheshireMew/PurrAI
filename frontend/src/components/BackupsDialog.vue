<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type * as Api from '../adminApi'
import { ElMessage } from 'element-plus'
import { api, time } from '../api'
import { combineTasks, useTask } from '../composables/useTask'
import { useLatestRequest } from '../composables/useLatestRequest'
import { useWorkspace } from '../workspace'
import { useDraftClose } from '../composables/useDraftClose'

type Plan = Omit<Api.BackupPlan, 'version'>
type Backup = Api.BackupFile
const props = defineProps<{ open: boolean }>(), emit = defineEmits<{close: []; saved: []}>()
const open = computed({get: () => props.open, set: value => { if (!value) emit('close') }})
const { mutations } = useWorkspace(), actions = useTask(), reads = useLatestRequest(), safe = actions.safe
const { busy, error } = combineTasks(actions, reads)
const plan = ref<Plan | null>(null), items = ref<Backup[]>([]), free = ref(0), state = ref<{last_success?: number; error?: string}>({})
const pending = ref<{file: string; error: string; created_at: number}[]>([])
const baseline = ref(''), planVersion = ref('missing')
const { requestClose, confirmDiscard } = useDraftClose(() => !!plan.value && JSON.stringify(plan.value) !== baseline.value, () => emit('close'), () => busy.value, () => props.open)
async function load(preserveDraft = false) {
  await reads.run(() => api<{plan: Plan; version: string; items: Backup[]; pending: typeof pending.value; free_bytes: number; schedule: typeof state.value}>('/api/system/backups'), data => {
    if (!preserveDraft) { plan.value = data.plan; baseline.value = JSON.stringify(data.plan); planVersion.value = data.version }
    items.value = data.items; pending.value = data.pending; free.value = data.free_bytes; state.value = data.schedule
  })
}
watch(() => props.open, value => { if (value) { plan.value = null; void load() } else reads.cancel() }, {flush: 'sync'})
async function save() {
  if (await safe(async () => {
    const sent = JSON.stringify(plan.value)
    const result = await mutations.submit<Plan & {version: string}>('/api/system/backups/plan', 'PATCH',
      {...plan.value, version: planVersion.value}, undefined, '保存备份计划与保留授权')
    planVersion.value = result.version
    baseline.value = sent
    ElMessage.success('计划已保存，按北京时间执行')
  })) emit('saved')
}
async function exercise(row: Backup) {
  if (await safe(async () => {
    await mutations.submit('/api/system/backups/exercise', 'POST', {file: row.file}, undefined, '恢复演练备份')
    ElMessage.success('恢复演练已完成，运行中的数据库保持原样')
  })) { await load(true); emit('saved') }
}
async function refresh() { if (await confirmDiscard()) await load() }
</script>

<template>
<el-dialog v-model="open" title="备份计划与文件" class="form-dialog" width="min(880px, 94vw)" :before-close="requestClose" :close-on-click-modal="false">
<div v-loading="busy"><el-alert v-if="error" :title="error" type="error" :closable="false"/>
<p>备份目录剩余 {{ (free / 1073741824).toFixed(2) }} GB。最近计划成功：{{ time(state.last_success || 0) }}</p>
<el-alert v-if="state.error" :title="state.error" type="warning" :closable="false"/>
<el-alert v-for="operation in pending" :key="operation.file" :title="`${operation.file}：${operation.error || '备份尚未完成，系统将沿原操作恢复'}`" type="warning" :closable="false"/>
<el-form v-if="plan" label-position="top">
<el-form-item label="定时备份"><el-switch v-model="plan.enabled" aria-label="启用定时备份"/></el-form-item>
<div class="settings-grid"><el-form-item label="北京时间几点执行"><el-input-number v-model="plan.hour_beijing" :min="0" :max="23"/></el-form-item><el-form-item label="间隔天数"><el-input-number v-model="plan.interval_days" :min="1" :max="30"/></el-form-item></div>
<el-form-item label="备份后自动恢复演练"><el-switch v-model="plan.exercise_after_backup" aria-label="备份后自动恢复演练"/></el-form-item>
<el-form-item label="自动整理授权"><el-checkbox v-model="plan.prune_enabled">允许按下列规则整理本程序登记的备份</el-checkbox></el-form-item>
<p>只整理程序登记且大小、修改时间未变化的备份，跳过用户文件、旧版未登记文件和正在执行的任务；至少保留最新一份。未授权时不会自动删除备份。</p>
<div class="settings-grid"><el-form-item label="保留天数"><el-input-number v-model="plan.retention_days" :min="1" :max="3650"/></el-form-item><el-form-item label="最多保留份数"><el-input-number v-model="plan.retention_count" :min="1" :max="1000"/></el-form-item><el-form-item label="登记备份总量上限（MB）"><el-input-number v-model="plan.max_total_mb" :min="16" :max="1048576"/></el-form-item></div>
</el-form>
<h3>现有备份</h3><el-table :data="items" max-height="340" empty-text="尚无备份文件">
<el-table-column prop="file" label="文件" min-width="250"/>
<el-table-column label="大小 / 版本" min-width="150"><template #default="{ row }">{{ (row.bytes / 1048576).toFixed(2) }} MB · {{ row.revision }}<p>{{ row.managed ? '程序登记' : '未授权自动整理' }}</p></template></el-table-column>
<el-table-column label="恢复演练" min-width="180"><template #default="{ row }"><template v-if="row.exercise">{{ row.exercise.integrity_ok && row.exercise.foreign_keys_ok ? '完整性通过' : '完整性异常' }} · {{ row.exercise.compatible ? '版本匹配' : '需配套旧版本或迁移' }}<div>{{ time(row.exercise.checked_at) }}</div><details><summary>恢复后的记录数量</summary><pre>{{ row.exercise.counts }}</pre></details></template><span v-else>尚未演练</span></template></el-table-column>
<el-table-column label="操作" width="100"><template #default="{ row }"><el-button text :disabled="busy" @click="exercise(row)">恢复演练</el-button></template></el-table-column>
</el-table>
<p>演练在独立 SQLite 临时数据库中恢复并检查，不覆盖当前数据。正式恢复仍需停服后使用已有 restore 命令。</p>
</div>
<template #footer><el-button :disabled="busy" @click="refresh">刷新</el-button><el-button :disabled="busy" @click="requestClose">关闭</el-button><el-button type="primary" :disabled="!plan" :loading="busy" @click="save">保存备份计划</el-button></template>
</el-dialog>
</template>

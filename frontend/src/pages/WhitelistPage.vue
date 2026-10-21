<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { put } from '../api'
import type { Whitelist } from '../types'
import { useRecords } from '../composables/useRecords'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ groupId: number; refreshKey: number }>()
const { rows, busy, error, safe, load } = useRecords<Whitelist>(() => props.groupId, 'whitelist')
const dialog = ref(false)
const form = reactive<Omit<Whitelist, 'user_id'> & { user_id: string | number }>({ user_id: '', verification: true, moderation: true, note: '' })
function edit(row?: Whitelist) { Object.assign(form, { user_id: '', verification: true, moderation: true, note: '' }, row); error.value = ''; dialog.value = true }
async function save() {
  if (await safe(async () => {
    if (!/^\d+$/.test(String(form.user_id))) throw new Error('请填写有效的 Telegram 用户 ID')
    await put(`/api/groups/${props.groupId}/whitelist`, { ...form, user_id: Number(form.user_id) })
    ElMessage.success('已保存')
  })) { dialog.value = false; await load() }
}
async function removeWhite(row: Whitelist) {
  try { await ElMessageBox.confirm(`将 ${row.user_id} 移出白名单？`, '移出白名单', { confirmButtonText: '移出', cancelButtonText: '取消' }) } catch { return }
  await safe(async () => { await api(`/api/groups/${props.groupId}/whitelist/${row.user_id}`, { method: 'DELETE' }) }); await load()
}
useRefresh(load, () => props.refreshKey)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section class="panel" v-loading="busy">
<div class="panel-title">
<h2>本群白名单</h2>
<el-button type="primary" @click="edit()">添加成员</el-button>
</div>
<el-table :data="rows" empty-text="暂无白名单成员">
<el-table-column prop="user_id" label="Telegram 用户 ID" min-width="180"/>
<el-table-column label="免验证" width="100">
<template #default="{ row }">{{ row.verification ? '是' : '否' }}</template>
</el-table-column>
<el-table-column label="免自动处罚" width="120">
<template #default="{ row }">{{ row.moderation ? '是' : '否' }}</template>
</el-table-column>
<el-table-column prop="note" label="备注" min-width="170"/>
<el-table-column label="操作" width="150">
<template #default="{ row }">
<el-button text @click="edit(row)">编辑</el-button>
<el-button text type="danger" @click="removeWhite(row)">移出</el-button>
</template>
</el-table-column>
</el-table>
</section>
<el-dialog v-model="dialog" title="白名单成员" width="min(620px, 94vw)" :close-on-click-modal="false" destroy-on-close>
<el-form label-position="top" @submit.prevent="save">

<el-form-item label="Telegram 用户 ID">
<el-input v-model="form.user_id"/>
</el-form-item>
<el-form-item label="免图片验证">
<el-switch v-model="form.verification"/>
</el-form-item>
<el-form-item label="免自动处罚">
<el-switch v-model="form.moderation"/>
</el-form-item>
<el-form-item label="备注">
<el-input v-model="form.note" maxlength="300"/>
</el-form-item>

</el-form>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<template #footer>
<el-button @click="dialog = false">取消</el-button>
<el-button type="primary" :loading="busy" @click="save">保存</el-button>
</template>
</el-dialog>
</template>

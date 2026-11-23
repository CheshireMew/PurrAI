<script setup lang="ts">
import { groupTypeName } from '../presentation'
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { post } from '../api'
import type { Group, SharedPolicy } from '../types'
import { actionContext } from '../actionContext'
import GroupSettingsDialog from '../components/GroupSettingsDialog.vue'
import { useTask } from '../composables/useTask'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'
import { statusTone } from '../presentation'

const props = defineProps<{ refreshKey: number; groupId: number | null }>()
const { me, groups, reloadGroups, submitAction } = useWorkspace()
const currentGroup = computed(() => groups.value.find(group => group.id === props.groupId))
const { busy, error, safe } = useTask(), settingsGroup = ref<Group | null>(null)
const admissionOpen = ref(false), admissionId = ref('')
const sharedBaseline = ref<SharedPolicy | null>(null)
async function openShared() {
  await safe(async () => {
    sharedBaseline.value = await api<SharedPolicy>('/api/settings/defaults')
    settingsGroup.value = { id: 0, title: '通用设置', type: 'supergroup', status: 'active', approved: true, enabled: true, settings: sharedBaseline.value.settings }
  })
}
const { busy: admissionBusy, error: admissionError, safe: admissionSafe } = useTask()
function openAdmission() { admissionId.value = ''; admissionError.value = ''; admissionOpen.value = true }
async function admitGroup() {
  const value = admissionId.value.trim(), groupId = Number(value)
  if (!/^-[1-9]\d*$/.test(value) || !Number.isSafeInteger(groupId)) {
    admissionError.value = '请填写群的负数数字 ID，例如 -1001234567890'; return
  }
  if (await admissionSafe(async () => {
    await post('/api/groups', { group_id: groupId })
    await reloadGroups()
  })) { admissionOpen.value = false; ElMessage.success('群已接入，可在群设置中开启保护和聊天') }
}
async function load() { await safe(reloadGroups) }
async function groupAction(row: Group, action: string) {
  if (action === 'revoke') {
    try { await ElMessageBox.confirm('撤销后将停止此群的聊天和管理任务，已有禁言不会自动解除。', '撤销群接入', { confirmButtonText: '撤销接入', cancelButtonText: '取消', type: 'warning' }) }
    catch { return }
  }
  await safe(async () => {
    if (action === 'approve' || action === 'revoke') await post(`/api/groups/${row.id}/access`, { approved: action === 'approve' })
    else if (action === 'join-link') await submitAction(`/api/groups/${row.id}/join-link`, { action: 'release', reason: '生成申请入群链接' }, actionContext(row, '生成申请入群链接'))
    else if (action === 'activate') await post(`/api/groups/${row.id}/activate`)
    else if (action === 'permissions') { const data = await post(`/api/groups/${row.id}/permissions`); ElMessage.info(data.ok ? '机器人权限完整' : `缺少权限：${data.missing.join('、')}`) }
    else await api(`/api/groups/${row.id}/settings`, { method: 'PATCH', body: JSON.stringify({ enabled: false }) })
    await reloadGroups()
  })
}
useRefresh(load, () => props.refreshKey)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section class="panel" v-loading="busy">
<template v-if="currentGroup">
<div class="panel-title"><h2>群保护</h2><el-tag :type="currentGroup.approved ? statusTone(currentGroup.status) : 'warning'">{{ currentGroup.approved ? label(currentGroup.status) : '等待批准接入' }}</el-tag></div>
<div class="group-settings-actions">
<template v-if="currentGroup.approved">
<el-button type="primary" @click="settingsGroup = currentGroup">编辑设置</el-button>
<el-button @click="groupAction(currentGroup, currentGroup.enabled ? 'pause' : 'activate')">{{ currentGroup.enabled ? '暂停保护' : '启用保护' }}</el-button>
<el-dropdown trigger="click" @command="(value: string) => groupAction(currentGroup!, value)"><el-button>更多操作</el-button><template #dropdown><el-dropdown-menu>
<el-dropdown-item command="permissions">检查权限</el-dropdown-item>
<el-dropdown-item v-if="currentGroup.settings.join_requests" command="join-link">申请入群链接</el-dropdown-item>
<el-dropdown-item v-if="me?.root" command="revoke" divided>撤销接入</el-dropdown-item>
</el-dropdown-menu></template></el-dropdown>
</template>
<el-button v-else-if="me?.root" type="primary" @click="groupAction(currentGroup, 'approve')">批准接入</el-button>
</div>
</template>
<el-empty v-else description="还没有可以设置的群"/>
</section>
<details v-if="me?.root" class="panel group-administration">
<summary>群接入与通用设置</summary>
<div class="panel-title">
<h2>已发现的群 <small>{{ groups.length }} 个</small>
</h2>
<span class="muted">仅限所有者批准的群；批准后由群管理员启用保护</span>
<el-button v-if="me?.root" type="primary" @click="openAdmission">接入群组</el-button>
<el-button v-if="me?.root" @click="openShared">通用设置</el-button>
</div>
<el-table :data="groups" empty-text="请先将机器人加入超级群">
<el-table-column label="群" min-width="200">
<template #default="{ row }">
<strong>{{ row.title }}</strong>
<div class="cell-sub">{{ row.id }} · {{ groupTypeName(row.type) }}</div>
</template>
</el-table-column>
<el-table-column label="状态" width="110">
<template #default="{ row }">
<el-tag :type="row.approved ? statusTone(row.status) : 'warning'">{{ row.approved ? label(row.status) : '等待所有者批准' }}</el-tag>
</template>
</el-table-column>
<el-table-column label="操作" min-width="330">
<template #default="{ row }">
<el-button v-if="me?.root && !row.approved" size="small" type="primary" @click="groupAction(row, 'approve')">批准接入</el-button>
<template v-if="row.approved">
<el-button v-if="!row.enabled" size="small" type="primary" @click="groupAction(row, 'activate')">启用保护</el-button>
<el-button v-if="row.enabled" size="small" @click="groupAction(row, 'pause')">暂停保护</el-button>
<el-button size="small" @click="groupAction(row, 'permissions')">检查权限</el-button>
<el-button size="small" @click="settingsGroup = row">群设置</el-button>
<el-button v-if="row.settings.join_requests" size="small" @click="groupAction(row, 'join-link')">申请入群链接</el-button>
</template>
<el-button v-if="me?.root && row.approved" size="small" type="danger" @click="groupAction(row, 'revoke')">撤销接入</el-button>
</template>
</el-table-column>
</el-table>
</details>
<el-dialog v-model="admissionOpen" title="接入群组" width="min(480px, 94vw)" :close-on-click-modal="!admissionBusy" :show-close="!admissionBusy">
<p>公开群和私密群均可，机器人须已加入群。邀请链接不能代替群 ID。</p>
<el-form @submit.prevent="admitGroup">
<el-form-item label="群 ID"><el-input v-model="admissionId" aria-label="群 ID" placeholder="-1001234567890" :disabled="admissionBusy"/></el-form-item>
<el-alert v-if="admissionError" :title="admissionError" type="error" :closable="false"/>
</el-form>
<template #footer>
<el-button :disabled="admissionBusy" @click="admissionOpen = false">取消</el-button>
<el-button type="primary" :loading="admissionBusy" @click="admitGroup">确认接入</el-button>
</template>
</el-dialog>
<GroupSettingsDialog :group="settingsGroup" :shared-baseline="sharedBaseline" @close="settingsGroup = null"/>
</template>

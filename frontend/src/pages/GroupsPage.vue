<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { post } from '../api'
import type { Group } from '../types'
import GroupSettingsDialog from '../components/GroupSettingsDialog.vue'
import { useTask } from '../composables/useTask'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ refreshKey: number }>()
const { me, groups, reloadGroups, actionTracker } = useWorkspace()
const { busy, error, safe } = useTask(), settingsGroup = ref<Group | null>(null)
async function load() { await safe(reloadGroups) }
async function groupAction(row: Group, action: string) {
  if (action === 'revoke') {
    try { await ElMessageBox.confirm('撤销后将停止此群的聊天和管理任务，已有禁言不会自动解除。', '撤销群接入', { confirmButtonText: '撤销接入', cancelButtonText: '取消', type: 'warning' }) }
    catch { return }
  }
  await safe(async () => {
    if (action === 'approve' || action === 'revoke') await post(`/api/groups/${row.id}/access`, { approved: action === 'approve' })
    else if (action === 'join-link') { const task = await post(`/api/groups/${row.id}/join-link`, { action: 'release', reason: '生成申请入群链接', request_id: crypto.randomUUID() }); actionTracker.accept(task, '生成申请入群链接') }
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
<div class="panel-title">
<h2>已发现的群 <small>{{ groups.length }} 个</small>
</h2>
<span class="muted">仅限所有者批准的群；批准后由群管理员启用保护</span>
</div>
<el-table :data="groups" empty-text="请先将机器人加入超级群">
<el-table-column label="群" min-width="200">
<template #default="{ row }">
<strong>{{ row.title }}</strong>
<div class="cell-sub">{{ row.id }} · {{ row.type }}</div>
</template>
</el-table-column>
<el-table-column label="状态" width="110">
<template #default="{ row }">
<el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.approved ? label(row.status) : '等待所有者批准' }}</el-tag>
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
</section>
<GroupSettingsDialog :group="settingsGroup" @close="settingsGroup = null"/>
</template>

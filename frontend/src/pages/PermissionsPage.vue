<script setup lang="ts">
import { ref } from 'vue'
import { useTask } from '../composables/useTask'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ refreshKey: number }>()
const rows = ref<{ id: number }[]>([])
const { busy, error, safe } = useTask()
async function load() { await safe(async () => { const data = await api<{ superadmin_ids: number[] }>('/api/admins'); rows.value = data.superadmin_ids.map(id => ({ id })) }) }
useRefresh(load, () => props.refreshKey)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section class="panel" v-loading="busy">
<div class="panel-title">
<h2>管理权限说明</h2>
</div>
<p>只允许所有者批准的自用和朋友群接入；批准后，群主和群管理员自动获得本群管理权限。新增或撤销管理员，请直接在 Telegram 群设置中操作。</p>
<p>超级管理员可以管理所有已接入群，并使用备份和系统任务维护。超级管理员由服务器配置指定，可私聊机器人发送 /id 获取自己的数字编号。</p>
<el-table :data="rows" empty-text="尚未配置超级管理员">
<el-table-column prop="id" label="超级管理员 Telegram 数字 ID" min-width="220"/>
</el-table>
</section>
</template>

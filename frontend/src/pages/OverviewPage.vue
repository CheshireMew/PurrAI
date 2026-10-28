<script setup lang="ts">
import { ref } from 'vue'
import { useTask } from '../composables/useTask'
import { api, label } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ refreshKey: number }>()
const emit = defineEmits<{ navigate: [page: string] }>()
const { groups, me } = useWorkspace(), stats = ref<Record<string, number>>({})
const { busy, error, safe } = useTask()
async function load() { await safe(async () => { stats.value = await api<Record<string, number>>('/api/dashboard') }) }
useRefresh(load, () => props.refreshKey, () => !busy.value)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section v-loading="busy" aria-label="群管理总览">
<div class="stat-grid" aria-label="管理统计">
<article v-for="(title, key) in { active_groups: '保护中的群', pending: '等待完成验证', passed_today: '24 小时验证通过', violations_today: '24 小时广告记录' }" :key="key" class="stat-card">
<span>{{ title }}</span>
<strong>{{ stats[key] ?? '—' }}</strong>
<small v-if="key === 'pending'">含等待处理的任务</small>
</article>
</div>
<p class="stats-caption">按你有权限管理的群统计</p>
<div class="overview-grid">
<section class="panel protection-card">
<div class="panel-title">
<h2>群保护概况</h2>
<el-button text @click="emit('navigate', 'groups')">管理群 →</el-button>
</div>
<el-empty v-if="!groups.length" description="机器人加入群后会显示在这里"/>
<div v-for="g in groups" :key="g.id" class="group-summary">
<span class="group-avatar">{{ g.title?.slice(0, 1) || '群' }}</span>
<div>
<strong>{{ g.title }}</strong>
<small>{{ g.id }}</small>
</div>
<el-tag :type="g.status === 'active' ? 'success' : g.status === 'error' ? 'danger' : 'info'">{{ label(g.status) }}</el-tag>
</div>
</section>
<section class="panel attention-card">
<h2>待跟进事项</h2>
<p>未完成的验证和失败操作会持续保留。先查看原因，再决定重新出题或人工放行。</p>
<div class="attention-numbers">
<div>
<strong>{{ stats.failed_actions ?? '—' }}</strong>
<span>失败操作</span>
</div>
<div>
<strong>{{ stats.failed_today ?? '—' }}</strong>
<span>24 小时验证失败</span>
</div>
</div>
<el-button @click="emit('navigate', 'verifications')">查看验证记录</el-button>
<el-button v-if="me?.root && stats.failed_actions" text @click="emit('navigate', 'system')">查看失败操作</el-button>
</section>
</div>
</section>
</template>

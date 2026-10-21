<script setup lang="ts">
import { computed } from 'vue'
import { label } from '../api'
import { useWorkspace } from '../workspace'
const { actionTracker } = useWorkspace()
const actions = computed(() => actionTracker.items)
</script>

<template>
<section v-if="actions.length" class="panel action-results">
<h2>本次提交的操作</h2>
<div v-for="a in actions" :key="a.id" class="action-line">
<span>{{ a.reason }}</span>
<el-tag>{{ label(a.state) }}</el-tag>
<small v-if="a.summary">共 {{ a.summary.total }} 项：成功 {{ a.summary.succeeded }}，等待 {{ a.summary.pending }}，失败 {{ a.summary.failed }}，取消 {{ a.summary.cancelled }}</small>
<small>{{ a.tracking_error || a.error || (a.state === 'pending' ? '等待 Telegram 执行确认' : '') }}</small>
<a v-if="a.payload?.invite_link" :href="a.payload.invite_link" target="_blank" rel="noopener">{{ a.payload.invite_link }}</a>
</div>
</section>
</template>

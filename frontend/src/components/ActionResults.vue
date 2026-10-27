<script setup lang="ts">
import { computed } from 'vue'
import { label } from '../api'
import { useWorkspace } from '../workspace'
const { actionTracker, actionRequests, retryAction } = useWorkspace()
const actions = computed(() => actionTracker.items)
</script>

<template>
<section v-if="actions.length || actionRequests.items.length" class="panel action-results">
<h2>本次提交的操作</h2>
<div v-for="request in actionRequests.items" :key="request.body.request_id" class="action-line">
<span>{{ request.body.reason }}</span>
<el-tag>提交结果待确认</el-tag>
<small>{{ request.error || '正在提交' }}</small>
<el-button v-if="request.error" size="small" @click="retryAction(request)">确认提交结果</el-button>
</div>
<div v-for="a in actions" :key="a.id" class="action-line">
<span>{{ a.reason }}</span>
<el-tag>{{ label(a.state) }}</el-tag>
<small v-if="a.summary">共 {{ a.summary.total }} 项：成功 {{ a.summary.succeeded }}，等待 {{ a.summary.pending }}，失败 {{ a.summary.failed }}，取消 {{ a.summary.cancelled }}</small>
<small>{{ a.tracking_error || a.error || (a.state === 'pending' ? '等待 Telegram 执行确认' : '') }}</small>
<a v-if="a.payload?.invite_link" :href="a.payload.invite_link" target="_blank" rel="noopener">{{ a.payload.invite_link }}</a>
</div>
</section>
</template>

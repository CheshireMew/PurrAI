<script setup lang="ts">
import { ref } from 'vue'
import type { Rule } from '../types'
import RuleEditor from '../components/RuleEditor.vue'
import { useRecords } from '../composables/useRecords'
import { usePreview } from '../composables/usePreview'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'

const props = defineProps<{ groupId: number; refreshKey: number }>()
const { rows, busy, error, safe, load } = useRecords<Rule>(() => props.groupId, 'rules')
const editorOpen = ref(false), editingRule = ref<Rule | null>(null)
const { input: previewInput, reasons: previewReasons, preview: runPreview } = usePreview(() => props.groupId)
function edit(row: Rule | null = null) { editingRule.value = row; editorOpen.value = true }
async function preview() { await safe(() => runPreview()) }
useRefresh(load, () => props.refreshKey)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>

<section class="panel" v-loading="busy">
<div class="panel-title">
<h2>本群广告规则</h2>
<el-button type="primary" @click="edit()">添加规则</el-button>
</div>
<el-table :data="rows" empty-text="还没有关键词或域名规则，可先添加规则并在下方测试">
<el-table-column label="类型" width="150">
<template #default="{ row }">{{ { keyword: '关键词', block_domain: '禁止域名', allow_domain: '允许域名', block_mention: '禁止引流账号', allow_mention: '允许提及账号' }[row.kind as string] }}</template>
</el-table-column>
<el-table-column prop="value" label="内容" min-width="200"/>
<el-table-column label="匹配" width="120">
<template #default="{ row }">{{ row.kind === 'keyword' ? ({ contains: '包含匹配', exact: '完整匹配', compact: '忽略干扰', pinyin: '谐音匹配' }[row.mode as string] || row.mode) : row.kind.endsWith('mention') ? '账号匹配' : '域名与子域名' }}</template>
</el-table-column>
<el-table-column label="范围" min-width="160">
<template #default="{ row }">{{ { message: row.penalty === 'permanent_mute' ? '正文，删消息并永久禁言' : '正文，只删消息', name: '昵称，删消息并永久禁言', either: '昵称或正文，各自处置', name_and_message: '昵称＋正文组合' }[(row.scope || 'message') as string] }}</template>
</el-table-column>
<el-table-column label="状态" width="100">
<template #default="{ row }">
<el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
</template>
</el-table-column>
<el-table-column label="操作" width="80" fixed="right">
<template #default="{ row }">
<el-button text @click="edit(row)">编辑</el-button>
</template>
</el-table-column>
</el-table>
</section>
<section class="panel preview-panel">
<h2>试一条消息</h2>
<p class="muted">只查看命中原因，不发送消息或执行处罚。域名白名单不会豁免关键词检查。</p>
<el-input v-model="previewInput.sender_name" aria-label="测试昵称及用户名" placeholder="测试昵称 @username"/>
<el-input v-model="previewInput.text" type="textarea" :rows="3" placeholder="输入要测试的消息，例如包含链接的推广文案" aria-label="规则测试消息"/>
<div class="preview-options">
<el-checkbox v-model="previewInput.newcomer">新成员限制期</el-checkbox>
<el-checkbox v-model="previewInput.contact">包含联系人</el-checkbox>
<el-checkbox v-model="previewInput.forwarded">用户或频道转发</el-checkbox>
<span>窗口内重复次数</span>
<el-input-number v-model="previewInput.repeats" :min="0" :max="100"/>
<span>近似内容次数</span>
<el-input-number v-model="previewInput.similar" :min="0" :max="100"/>
<span>连续发送次数</span>
<el-input-number v-model="previewInput.burst" :min="0" :max="100"/>
<el-button type="primary" :loading="busy" @click="preview">测试规则</el-button>
</div>
<el-alert v-if="previewReasons !== null" :type="previewReasons.length ? 'warning' : 'success'" :title="previewReasons.length ? previewReasons.join('；') : '未命中当前规则'" :closable="false"/>
</section>

<RuleEditor :open="editorOpen" :rule="editingRule" :rules="rows" :group-id="groupId" @close="editorOpen = false" @saved="load"/>
</template>

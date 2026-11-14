<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { Rule } from '../types'
import RuleEditor from '../components/RuleEditor.vue'
import BusinessPolicyDialog from '../components/BusinessPolicyDialog.vue'
import { useRecords } from '../composables/useRecords'
import { usePreview } from '../composables/usePreview'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useRefresh } from '../composables/useRefresh'
import { savedFilters } from '../navigation'

const props = defineProps<{ groupId: number; refreshKey: number }>()
const { catalog, me, mutations } = useWorkspace()
const query = reactive({ query: '', kind: '', enabled: '', source: '', ...savedFilters('rules/query').extra })
const applied = reactive({ ...query })
const { rows, total, pagination, busy, error, safe, load, filters } = useRecords<Rule>(() => props.groupId, 'rules/query', () => ({ ...applied }), values => { Object.assign(query, values); Object.assign(applied, values) })
const selected = ref<Rule[]>([])
async function search() { Object.assign(applied, query); selected.value = []; await filters() }
async function changeSelected(enabled: boolean, scope = 'local') {
  try { await ElMessageBox.confirm(`确认${enabled ? '启用' : '停用'}选中的 ${selected.value.length} 条${scope === 'global' ? '通用' : '本群'}规则？${scope === 'global' ? '其他仍继承的群也会受影响。' : ''}`, '批量修改规则', { confirmButtonText: '确认修改', cancelButtonText: '取消' }) } catch { return }
  await safe(async () => {
    await mutations.submit(`/api/groups/${props.groupId}/rules/batch`, 'POST', {
      versions: Object.fromEntries(selected.value.map(rule => [rule.id, rule.version])), enabled, scope,
    }, undefined, '批量启停规则')
    selected.value = []; await load(); ElMessage.success('已保存选中规则')
  })
}
const editorOpen = ref(false), editingRule = ref<Rule | null>(null)
const businessOpen = ref(false)
const { input: previewInput, reasons: previewReasons, preview: runPreview } = usePreview(() => props.groupId, () => [rows.value, props.refreshKey])
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
<form class="filterbar" @submit.prevent="search">
<el-input v-model="query.query" aria-label="搜索规则" placeholder="搜索规则内容" clearable/>
<el-select v-model="query.kind" aria-label="规则类型筛选" placeholder="全部类型" clearable><el-option v-for="(name, kind) in catalog?.rules?.kinds" :key="kind" :value="kind" :label="name"/></el-select>
<el-select v-model="query.enabled" aria-label="规则状态筛选" placeholder="全部状态" clearable><el-option value="true" label="启用"/><el-option value="false" label="停用"/></el-select>
<el-select v-model="query.source" aria-label="规则来源筛选" placeholder="全部来源" clearable><el-option value="shared" label="继承通用"/><el-option value="local" label="本群设置"/><el-option value="learned" label="自动学习"/></el-select>
<el-button native-type="submit">查询规则</el-button>
</form>
<div v-if="selected.length" class="filterbar"><span>已选 {{ selected.length }} 条</span><el-button @click="changeSelected(true)">本群启用</el-button><el-button @click="changeSelected(false)">本群停用</el-button><el-button v-if="me?.root" @click="changeSelected(false, 'global')">停用通用规则</el-button></div>
<el-table :data="rows" row-key="id" empty-text="没有匹配的规则" @selection-change="selected = $event">
<el-table-column type="selection" width="45"/>
<el-table-column label="类型" width="150">
<template #default="{ row }">{{ row.kind_label || catalog?.rules?.kinds[row.kind] || row.kind }}</template>
</el-table-column>
<el-table-column prop="value" label="内容" min-width="200"/>
<el-table-column label="来源 / 命中" min-width="200"><template #default="{ row }"><div>{{ row.inherited ? '继承通用' : '本群设置' }}{{ row.origin === 'learned' ? ' · 自动学习' : '' }}</div><div>已记录命中 {{ row.hit_count || 0 }} 条 · {{ time(row.last_hit_at || 0) }}</div><details v-if="row.sources?.length"><summary>来源消息</summary><p v-for="source in row.sources" :key="`${source.group_id}:${source.message_id}`">群 {{ source.origin_group_id || source.group_id }} · 消息 {{ source.origin_message_id || source.message_id }}{{ source.origin_group_id ? '（升级前）' : '' }} · {{ time(source.created_at) }}{{ source.withdrawn ? '（已纠正）' : '' }}</p></details></template></el-table-column>
<el-table-column label="匹配" width="120">
<template #default="{ row }">{{ row.matching_label || catalog?.rules?.modes[row.mode] || row.mode }}</template>
</el-table-column>
<el-table-column label="处置" min-width="240">
<template #default="{ row }">{{ row.consequence || '查看规则预览确认处置' }}</template>
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
<el-pagination v-model:current-page="pagination" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load"/>
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
<el-input-number v-model="previewInput.repeats" aria-label="窗口内重复次数" :min="0" :max="100"/>
<span>近似内容次数</span>
<el-input-number v-model="previewInput.similar" aria-label="近似内容次数" :min="0" :max="100"/>
<span>连续发送次数</span>
<el-input-number v-model="previewInput.burst" aria-label="连续发送次数" :min="0" :max="100"/>
<el-button type="primary" :loading="busy" @click="preview">测试规则</el-button>
</div>
<el-alert v-if="previewReasons !== null" :type="previewReasons.length ? 'warning' : 'success'" :title="previewReasons.length ? previewReasons.join('；') : '未命中当前规则'" :closable="false"/>
</section>

<RuleEditor :open="editorOpen" :rule="editingRule" :rules="rows" :group-id="groupId" @close="editorOpen = false" @saved="load"/>
<section v-if="me?.root" class="panel"><div class="panel-title"><h2>业务组合规则</h2><el-button @click="businessOpen = true">管理业务规则</el-button></div><p>维护原有广告、昵称和聊天排除规则的词组、组合条件、豁免和处置。</p></section>
<BusinessPolicyDialog v-if="me?.root" :open="businessOpen" :group-id="groupId" @close="businessOpen = false" @saved="load"/>
</template>

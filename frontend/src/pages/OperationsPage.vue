<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import DeliveryReviewDialog from '../components/DeliveryReviewDialog.vue'
import DateRangeFilter from '../components/DateRangeFilter.vue'
import { useRecords } from '../composables/useRecords'
import { useRefresh } from '../composables/useRefresh'
import { useWorkspace } from '../workspace'
import { label, time } from '../api'
import type { Operation } from '../types'
import { savedFilters } from '../navigation'

const props = defineProps<{ groupId: number; refreshKey: number }>()
const { catalog, me } = useWorkspace(), reviewing = ref<Operation | null>(null)
const extra = reactive({ kind: '', actor_id: '', query: '', manual_only: 'false', system_only: '', ...savedFilters('actions').extra })
const applied = reactive({ ...extra })
const source = computed({get: () => extra.manual_only === 'true' ? 'manual' : extra.system_only === 'true' ? 'auto' : '',
  set: value => { extra.manual_only = value === 'manual' ? 'true' : 'false'; extra.system_only = value === 'auto' ? 'true' : '' }})
const { rows, total, pagination, statusFilter, userFilter, dates, busy, error, load, filters } = useRecords<Operation>(
  () => props.groupId, 'actions', () => ({ ...applied }), values => { Object.assign(extra, values); Object.assign(applied, values) })
async function search() { Object.assign(applied, extra); await filters() }
useRefresh(load, () => props.refreshKey)
</script>

<template>
<section class="panel" v-loading="busy"><h2>全部操作记录</h2>
<form class="filterbar" @submit.prevent="search">
<el-input v-model="userFilter" placeholder="目标成员 ID" aria-label="操作目标成员 ID" clearable/>
<el-input v-model="extra.actor_id" placeholder="操作者 ID" aria-label="操作记录操作者 ID" clearable/>
<el-input v-model="extra.query" placeholder="原因或失败说明" aria-label="搜索操作说明" clearable/>
<el-select v-model="extra.kind" aria-label="筛选操作类型" placeholder="全部类型" clearable><el-option v-for="(name, kind) in catalog!.operations" :key="kind" :value="kind" :label="name"/></el-select>
<el-select v-model="statusFilter" aria-label="筛选操作状态" placeholder="全部状态" clearable><el-option v-for="state in ['pending', 'succeeded', 'failed', 'cancelled', 'reviewed']" :key="state" :value="state" :label="label(state)"/></el-select>
<el-select v-model="source" aria-label="筛选操作来源" placeholder="全部来源" clearable><el-option value="auto" label="自动操作"/><el-option value="manual" label="人工操作"/></el-select>
<DateRangeFilter v-model="dates"/>
<el-button type="primary" native-type="submit">查询操作记录</el-button>
</form>
<el-alert v-if="error" :title="error" type="error" :closable="false"/>
<el-table :data="rows" empty-text="没有匹配的操作记录">
<el-table-column label="时间" min-width="170"><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column>
<el-table-column label="操作" min-width="180"><template #default="{ row }">{{ catalog!.operations[row.kind] || row.kind }}<div class="cell-sub">{{ row.id }}</div></template></el-table-column>
<el-table-column label="成员 / 操作者" min-width="170"><template #default="{ row }">{{ row.user_id || row.payload?.result?.user_id || '—' }} / {{ row.actor_id || '系统' }}</template></el-table-column>
<el-table-column prop="reason" label="原因" min-width="200"/>
<el-table-column v-if="me?.root" label="核实" width="130"><template #default="{ row }"><el-button v-if="row.can_review_delivery" text @click="reviewing = row">登记核实结果</el-button></template></el-table-column>
<el-table-column label="状态与结果" min-width="250"><template #default="{ row }">{{ label(row.state) }}<div>{{ row.error }}</div><p v-if="row.manual_review">人工核实{{ row.manual_review.outcome === 'completed' ? '已完成' : '未完成' }}：{{ row.manual_review.reason }}</p><details v-if="row.payload?.result"><summary>变更内容</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{ JSON.stringify(row.payload.result, null, 2) }}</pre></details></template></el-table-column>
</el-table>
<el-pagination v-model:current-page="pagination" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load"/>
</section>
<DeliveryReviewDialog :action="reviewing" @close="reviewing = null" @saved="load"/>
</template>

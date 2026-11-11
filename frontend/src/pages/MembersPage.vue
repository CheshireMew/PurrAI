<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api, label, time } from '../api'
import { useWorkspace } from '../workspace'
import { useLatestRequest } from '../composables/useLatestRequest'
import type { Operation, Page, Verification, Violation, Whitelist } from '../types'
import { route, savedFilters, saveFilters } from '../navigation'

type MemberDetail = { user_id: number; current: Record<string, any> | null; current_error: string; checked_at: number | null;
  member: { joined_at: number; join_observed: boolean; passed_at: number; owned_restriction?: Record<string, any> | null } | null;
  whitelist: Whitelist | null; verifications: Page<Verification>; violations: Page<Violation>; actions: Page<Operation> }
const props = defineProps<{ groupId: number; refreshKey: number }>()
const { groups, catalog, submitAction } = useWorkspace()
const target = ref(''), applied = ref<number | null>(null), detail = ref<MemberDetail | null>(null)
const action = ref('mute'), reason = ref(''), customDuration = ref(false), duration = ref(30), unit = ref(60)
const { busy, error, run } = useLatestRequest()
const initial = savedFilters('members')
target.value = typeof initial.user === 'string' ? initial.user : ''
let written = route.value.params.get('filter') || ''
const statuses: Record<string, string> = { creator: '群主', administrator: '管理员', member: '正常成员', restricted: '受限制', left: '已离群', kicked: '被封禁' }
watch(() => props.refreshKey, () => {
  if (applied.value) void run(() => api<MemberDetail>(`/api/groups/${props.groupId}/members/${applied.value}`), result => { detail.value = result })
})
async function search(remember = true) {
  await run(async () => {
    const id = Number(target.value.trim())
    if (!/^[1-9]\d*$/.test(target.value.trim()) || !Number.isSafeInteger(id)) throw new Error('请填写成员的正数数字 ID')
    return api<MemberDetail>(`/api/groups/${props.groupId}/members/${id}`)
  }, result => {
    detail.value = result; applied.value = result.user_id
    const value = {section: 'members', user: String(result.user_id)}
    written = JSON.stringify(value); saveFilters(value, !remember)
  })
}
onMounted(() => { if (target.value) void search(false) })
watch(() => route.value.params.get('filter') || '', value => {
  if (value !== written) {
    written = value; const saved = savedFilters('members'); target.value = typeof saved.user === 'string' ? saved.user : ''
    if (target.value) void search(false); else { detail.value = null; applied.value = null }
  }
})
async function submit() {
  const id = applied.value
  if (!id || !reason.value.trim()) { error.value = '请先查询成员并填写处理原因'; return }
  const seconds = customDuration.value && ['mute', 'ban'].includes(action.value) ? duration.value * unit.value : undefined
  if (seconds !== undefined && (seconds < 30 || seconds > 2592000)) { error.value = '时长须在 30 秒到 30 天之间'; return }
  const group = groups.value.find(item => item.id === props.groupId)
  try { await ElMessageBox.confirm(`${group?.title || props.groupId} · 成员 ${id}\n${catalog.value!.operations[action.value]}${seconds ? `，本次 ${seconds} 秒` : ''}\n原因：${reason.value.trim()}`, '确认成员操作', { confirmButtonText: '确认提交', cancelButtonText: '取消' }) }
  catch { return }
  await submitAction(`/api/groups/${props.groupId}/members/${id}/actions`, { action: action.value, reason: reason.value.trim(), ...(seconds ? { duration_seconds: seconds } : {}) })
}
</script>

<template>
<section class="panel" v-loading="busy">
<h2>查询成员</h2>
<form class="filterbar" @submit.prevent="search()"><el-input v-model="target" aria-label="成员数字 ID" placeholder="Telegram 用户数字 ID"/><el-button native-type="submit" type="primary">查询成员</el-button></form>
<el-alert v-if="error" :title="error" type="error" :closable="false"/>
<template v-if="detail">
<p>成员 {{ detail.user_id }} · {{ detail.current ? statuses[detail.current.status] || detail.current.status : '当前状态未核实' }} · 核实时间 {{ time(detail.checked_at || 0) }}</p>
<el-alert v-if="detail.current_error" :title="detail.current_error" type="warning" :closable="false"/>
<p v-if="detail.member?.join_observed">观察到的入群时间：{{ time(detail.member.joined_at) }}；验证通过：{{ time(detail.member.passed_at) }}</p>
<p v-else>未保存可靠的入群时间，不能据此判断成员在群时长。</p>
<p>白名单：{{ detail.whitelist ? `免验证${detail.whitelist.verification ? '是' : '否'}，免自动处罚${detail.whitelist.moderation ? '是' : '否'}` : '未加入' }}</p>
<p v-if="detail.member?.owned_restriction">PurrAI 记录的限制：{{ detail.member.owned_restriction.kind }}；到期 {{ detail.member.owned_restriction.until_date ? time(detail.member.owned_restriction.until_date) : '永久' }}</p>
<el-form label-position="top">
<el-form-item label="成员操作"><el-select v-model="action" aria-label="成员操作"><el-option v-for="item in ['release', 'reissue', 'mute', 'ban', 'unmute', 'unban']" :key="item" :value="item" :label="catalog!.operations[item]"/></el-select></el-form-item>
<el-form-item v-if="['mute', 'ban'].includes(action)" label="本次处罚时长"><el-checkbox v-model="customDuration">单独设置本次时长</el-checkbox><template v-if="customDuration"><el-input-number v-model="duration" :min="1" :max="2592000" aria-label="本次时长数值"/><el-select v-model="unit" aria-label="时长单位"><el-option :value="1" label="秒"/><el-option :value="60" label="分钟"/><el-option :value="3600" label="小时"/><el-option :value="86400" label="天"/></el-select></template><span v-else>沿用本群设置</span></el-form-item>
<el-form-item label="处理原因"><el-input v-model="reason" maxlength="300" aria-label="成员处理原因"/></el-form-item>
<el-button type="primary" @click="submit">核对并提交</el-button>
</el-form>
</template>
</section>
<template v-if="detail">
<section class="panel"><h2>验证记录（共 {{ detail.verifications.total }} 条，最近 20 条）</h2><el-table :data="detail.verifications.items"><el-table-column prop="user_name" label="成员"/><el-table-column label="状态"><template #default="{ row }">{{ label(row.state) }}</template></el-table-column><el-table-column label="截止"><template #default="{ row }">{{ time(row.deadline) }}</template></el-table-column><el-table-column prop="error" label="说明"/></el-table></section>
<section class="panel"><h2>违规记录（共 {{ detail.violations.total }} 条，最近 20 条）</h2><el-table :data="detail.violations.items"><el-table-column prop="excerpt" label="摘录"/><el-table-column label="命中原因"><template #default="{ row }">{{ row.reasons.join('；') }}</template></el-table-column><el-table-column label="时间"><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column></el-table></section>
<section class="panel"><h2>操作记录（共 {{ detail.actions.total }} 条，最近 20 条）</h2><el-table :data="detail.actions.items"><el-table-column label="操作"><template #default="{ row }">{{ catalog!.operations[row.kind] || row.kind }}</template></el-table-column><el-table-column label="状态"><template #default="{ row }">{{ label(row.state) }}</template></el-table-column><el-table-column prop="reason" label="原因"/><el-table-column prop="error" label="说明"/></el-table></section>
</template>
</template>

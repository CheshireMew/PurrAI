<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api, time } from '../api'
import { useTask } from '../composables/useTask'
import { useRefresh } from '../composables/useRefresh'
import { useWorkspace } from '../workspace'
import { useDraftClose } from '../composables/useDraftClose'
import { beijingInput, entryStates, lotteryStates, newLottery, type Lottery, type LotteryConfig, type LotteryDetail, type LotteryPrize } from '../lottery'

const props = defineProps<{ groupId: number; refreshKey: number }>()
const { mutations } = useWorkspace()
const { busy, error, safe } = useTask()
const base = () => `/api/groups/${props.groupId}/lotteries`
const rows = ref<Lottery[]>([]), total = ref(0), page = ref(1)
const loaded = ref(false), loadError = ref(''), notice = ref('')
const editor = ref(false), editing = ref<Lottery | null>(null), detail = ref<LotteryDetail | null>(null)
const entryPage = ref(1), entryState = ref('')
const form = reactive<LotteryConfig>(newLottery()), endTime = ref('')
const draftSnapshot = () => JSON.stringify([form, endTime.value])
const draftBaseline = ref('')
const { requestClose: closeEditor } = useDraftClose(() => draftSnapshot() !== draftBaseline.value,
  () => { editor.value = false }, () => busy.value, () => editor.value)
const weightTotal = computed(() => form.prizes.reduce((sum, prize) => sum + (prize.weight || 0), 0))
let listGeneration = 0, detailGeneration = 0
async function load() {
  const generation = ++listGeneration
  loadError.value = ''
  const success = await safe(async () => {
    const data = await api<{ items: Lottery[]; total: number }>(`${base()}?page_number=${page.value}`)
    if (generation === listGeneration) { rows.value = data.items; total.value = data.total; loaded.value = true }
  }, () => generation === listGeneration)
  if (!success && generation === listGeneration) loadError.value = error.value
}
async function showDetail(id: string, reset = true) {
  if (reset) { entryPage.value = 1; entryState.value = '' }
  const generation = ++detailGeneration
  await safe(async () => {
    const data = await api<LotteryDetail>(`${base()}/${id}?page_number=${entryPage.value}&entry_state=${entryState.value}`)
    if (generation === detailGeneration) detail.value = data
  }, () => generation === detailGeneration)
}
function edit(row: Lottery | null = null) {
  notice.value = ''
  editing.value = row
  Object.assign(form, JSON.parse(JSON.stringify(row?.config || newLottery())))
  endTime.value = beijingInput(form.ends_at)
  draftBaseline.value = draftSnapshot()
  error.value = ''; editor.value = true
}
function changeMode() {
  if (form.mode === 'instant') {
    form.entry_cost ||= 50
    if (form.draw_mode === 'participants') form.draw_mode = 'manual'
  } else form.prizes = form.prizes.filter(prize => prize.kind !== 'none')
}
function changePrize(prize: LotteryPrize) {
  if (prize.kind !== 'points') prize.points = 0
  else prize.points ||= 100
  if (prize.kind === 'none') { prize.quantity = 0; prize.name = '未中奖' }
  else prize.quantity ||= 1
}
async function save() {
  await safe(async () => {
    const config: LotteryConfig = JSON.parse(JSON.stringify(form))
    config.ends_at = config.draw_mode === 'time' && endTime.value ? Date.parse(endTime.value.replace(' ', 'T') + ':00+08:00') / 1000 : 0
    if (config.draw_mode === 'time' && (!config.ends_at || !Number.isFinite(config.ends_at))) throw new Error('请设置有效的结束时间')
    const row = await mutations.submit<Lottery>(editing.value ? `${base()}/${editing.value.id}` : base(), editing.value ? 'PUT' : 'POST',
      { config, version: editing.value?.version || 0 }, undefined, '保存抽奖草稿')
    editor.value = false; notice.value = '抽奖草稿已保存'
    await load(); await showDetail(row.id)
  })
}
const actionLabels: Record<string, string> = { publish: '发布到本群', draw: '立即开奖', cancel: '取消活动并退款', close: '结束即时抽奖', announce: '重发公告 / 结果', copy: '复用配置', fulfill: '登记已发放' }
async function operate(row: Lottery, operation: string, entryId = '') {
  const messages: Record<string, string> = {
    publish: '按当前规则发布到本群，发布后规则不可修改。', draw: '立即停止报名并开奖；不足最低成团人数时取消并退款。',
    cancel: '取消本场活动，已收取的报名积分原路退回。', close: '结束后停止抽奖，已完成的抽奖保留结果，已消耗积分不退回。',
    announce: '重新发送一份公告或结果。若上次发送结果不明，请先核实群内是否已收到。', fulfill: '仅登记这份人工奖品已发放，不会自动发送奖品。',
  }
  if (operation !== 'copy') {
    try { await ElMessageBox.confirm(`${row.config.title}\n${messages[operation]}`, actionLabels[operation], { confirmButtonText: '确认', cancelButtonText: '返回' }) }
    catch { return }
  }
  await safe(async () => {
    const result = await mutations.submit<Lottery>(`${base()}/${row.id}/operate`, 'POST', { operation, version: row.version, entry_id: entryId }, undefined, actionLabels[operation]!)
    await load()
    if (operation === 'copy') { detail.value = null; edit(result) }
    else await showDetail(result.id, false)
    notice.value = operation === 'copy' ? '已复用为独立新草稿，请核对配置和时间后发布' : '已处理，公告发送情况请查看活动详情'
  })
}
function isCancellable(row: Lottery) { return row.state === 'draft' || row.state === 'open' && row.config.mode === 'raffle' }
function typeName(row: Lottery) { return row.config.mode === 'instant' ? '积分即时抽奖' : row.config.entry_cost ? '积分报名抽奖' : '免费报名抽奖' }
function schedule(row: Lottery) {
  return row.config.draw_mode === 'time' ? `${time(row.config.ends_at)} ${row.config.mode === 'raffle' ? '开奖' : '结束'}`
    : row.config.draw_mode === 'participants' ? `满 ${row.config.participant_target} 人开奖` : row.config.mode === 'raffle' ? '手动开奖' : '手动结束'
}
watch([entryPage, entryState], () => { if (detail.value) void showDetail(detail.value.id, false) })
useRefresh(async () => { await load(); if (detail.value) await showDetail(detail.value.id, false) }, () => props.refreshKey)
</script>

<template>
  <el-alert v-if="error && !editor && !detail" :title="error" type="error" :closable="false" show-icon/>
  <el-alert v-if="notice && !editor && !detail" :title="notice" type="success" @close="notice = ''"/>
  <p v-if="loadError && loaded" class="muted" role="status">刷新失败，仍显示上次成功加载的活动。</p>
  <div class="lottery-toolbar"><span>{{ loaded ? `${total} 场活动` : '活动数量待确认' }}</span><el-button type="primary" :disabled="busy" @click="edit()">新建抽奖</el-button></div>
  <el-empty v-if="!rows.length" :description="loadError ? '抽奖活动加载失败，请重试' : !loaded ? '正在加载抽奖活动…' : '还没有抽奖活动'"/>
  <div class="lottery-grid">
    <article v-for="row in rows" :key="row.id" class="lottery-card">
      <div class="lottery-title"><h2>{{ row.config.title }}</h2><el-tag :type="row.state === 'open' ? 'success' : 'info'">{{ lotteryStates[row.state] }}</el-tag></div>
      <p>{{ typeName(row) }}<span v-if="row.config.entry_cost"> · 每次 {{ row.config.entry_cost }} 积分</span></p>
      <p>{{ schedule(row) }}</p>
      <p>{{ row.participant_count }} 人参与<span v-if="row.config.mode === 'instant'"> · 已抽 {{ row.draw_count }} 次</span></p>
      <div class="lottery-actions"><el-button @click="showDetail(row.id)">查看活动</el-button><el-button :disabled="busy" @click="operate(row, 'copy')">复用配置</el-button></div>
    </article>
  </div>
  <el-pagination v-if="total > 20" v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load"/>

  <el-dialog v-model="editor" :title="editing ? '编辑抽奖草稿' : '新建抽奖'" class="form-dialog" width="min(760px, 96vw)" :before-close="closeEditor" :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy">
    <el-form label-position="top" @submit.prevent="save">
      <div class="lottery-form-grid">
        <el-form-item label="活动标题"><el-input v-model="form.title" maxlength="80" aria-label="活动标题"/></el-form-item>
        <el-form-item label="抽奖方式"><el-select v-model="form.mode" aria-label="抽奖方式" @change="changeMode"><el-option label="报名后统一开奖" value="raffle"/><el-option label="消耗积分，立即抽奖" value="instant"/></el-select></el-form-item>
        <el-form-item label="每次消耗积分"><el-input-number v-model="form.entry_cost" :min="form.mode === 'instant' ? 1 : 0" :max="1000000" :precision="0" aria-label="每次消耗积分"/></el-form-item>
        <el-form-item label="参与前积分门槛"><el-input-number v-model="form.min_balance" :min="0" :max="9007199254740991" :precision="0" aria-label="参与前积分门槛"/></el-form-item>
        <el-form-item label="统计最近多少天"><el-input-number v-model="form.active_window_days" :min="1" :max="365" :precision="0" aria-label="统计最近多少天"/></el-form-item>
        <el-form-item label="至少活跃多少天"><el-input-number v-model="form.min_active_days" :min="0" :max="form.active_window_days" :precision="0" aria-label="至少活跃多少天"/></el-form-item>
      </div>
      <p class="muted">报名消耗设为 0 即免费。积分门槛不额外扣分；活跃天数按获得聊天积分的日期统计，含今天，设为 0 关闭。</p>
      <el-form-item label="活动说明"><el-input v-model="form.description" type="textarea" :rows="2" maxlength="500" aria-label="活动说明"/></el-form-item>
      <h3>奖品</h3>
      <div v-for="(prize, index) in form.prizes" :key="index" class="prize-row">
        <el-form-item label="类型"><el-select v-model="prize.kind" :aria-label="`奖品 ${index + 1} 类型`" @change="changePrize(prize)"><el-option label="人工发放" value="manual"/><el-option label="自动发积分" value="points"/><el-option v-if="form.mode === 'instant'" label="未中奖" value="none"/></el-select></el-form-item>
        <el-form-item label="名称"><el-input v-model="prize.name" maxlength="60" :aria-label="`奖品 ${index + 1} 名称`"/></el-form-item>
        <el-form-item v-if="prize.kind === 'points'" label="奖励积分"><el-input-number v-model="prize.points" :min="1" :max="1000000" :precision="0" :aria-label="`奖品 ${index + 1} 奖励积分`"/></el-form-item>
        <el-form-item v-if="prize.kind !== 'none'" label="数量"><el-input-number v-model="prize.quantity" :min="1" :max="form.mode === 'instant' ? 100000 : 20" :precision="0" :aria-label="`奖品 ${index + 1} 数量`"/></el-form-item>
        <el-form-item v-if="form.mode === 'instant'" :label="`概率 ${weightTotal ? (prize.weight / weightTotal * 100).toFixed(2) : 0}%`"><el-input-number v-model="prize.weight" :min="1" :max="1000000" :precision="0" :aria-label="`奖品 ${index + 1} 概率权重`"/></el-form-item>
        <el-button :disabled="form.prizes.length === 1" :aria-label="`移除奖品 ${index + 1}`" @click="form.prizes.splice(index, 1)">移除</el-button>
      </div>
      <el-button :disabled="form.prizes.length >= 10" @click="form.prizes.push({ name: '', kind: 'manual', points: 0, quantity: 1, weight: 1 })">添加奖品</el-button>
      <p class="muted">{{ form.mode === 'instant' ? '概率由各项权重占总权重的比例决定。奖品领完后，对应概率计为未中奖，其余概率不变；全部奖品领完后不再扣分。' : '每人一次报名机会，最多中奖一次，每场最多 20 个中奖名额。参与人数少于名额时按奖品顺序发放。' }}非积分奖品由管理员人工发放。</p>
      <div class="lottery-form-grid">
        <el-form-item :label="form.mode === 'instant' ? '结束方式' : '开奖方式'"><el-select v-model="form.draw_mode" aria-label="开奖或结束方式"><el-option :label="form.mode === 'instant' ? '手动结束' : '手动开奖'" value="manual"/><el-option label="定时" value="time"/><el-option v-if="form.mode === 'raffle'" label="满人数自动开奖" value="participants"/></el-select></el-form-item>
        <el-form-item v-if="form.draw_mode === 'time'" label="结束时间（北京时间）"><el-date-picker v-model="endTime" type="datetime" format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DD HH:mm" aria-label="结束时间（北京时间）"/></el-form-item>
        <el-form-item v-if="form.draw_mode === 'participants'" label="开奖人数"><el-input-number v-model="form.participant_target" :min="1" :max="100000" :precision="0" aria-label="开奖人数"/></el-form-item>
        <el-form-item v-if="form.mode === 'raffle'" label="最低成团人数"><el-input-number v-model="form.min_participants" :min="1" :max="100000" :precision="0" aria-label="最低成团人数"/></el-form-item>
        <el-form-item v-else label="每人每天最多抽几次"><el-input-number v-model="form.daily_limit" :min="1" :max="100" :precision="0" aria-label="每人每天最多抽几次"/></el-form-item>
      </div>
      <p class="muted">{{ form.mode === 'raffle' ? '正常开奖后未中奖不退报名积分。活动取消或未达到最低成团人数时退款。' : '每日次数按北京时间零点重置，结束活动不会退回已完成抽奖的积分。' }}发布后规则固定，往期活动可以复用为新草稿。</p>
    </el-form>
    <template #footer><el-alert v-if="error" :title="error" type="error" :closable="false" show-icon/>
      <el-alert v-if="notice" :title="notice" type="success" @close="notice = ''"/>
      <el-button :disabled="busy" @click="closeEditor">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存草稿</el-button>
    </template>
  </el-dialog>

  <el-dialog :model-value="!!detail" :title="detail?.config.title || '抽奖详情'" class="form-dialog" width="min(1000px, 96vw)" @close="detail = null; detailGeneration++">
    <template v-if="detail">
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon/>
      <el-alert v-if="notice" :title="notice" type="success" @close="notice = ''"/>
      <div class="lottery-actions">
        <el-tag>{{ lotteryStates[detail.state] }}</el-tag>
        <el-button v-if="detail.state === 'draft'" :disabled="busy" @click="edit(detail); detail = null">编辑草稿</el-button>
        <el-button v-if="detail.state === 'draft'" type="primary" :disabled="busy" @click="operate(detail, 'publish')">发布到本群</el-button>
        <el-button v-if="detail.state === 'open'" type="primary" :disabled="busy" @click="operate(detail, detail.config.mode === 'raffle' ? 'draw' : 'close')">{{ detail.config.mode === 'raffle' ? '立即开奖' : '结束即时抽奖' }}</el-button>
        <el-button :disabled="busy" @click="operate(detail, 'copy')">复用配置</el-button>
        <el-button v-if="detail.state !== 'draft'" :disabled="busy" @click="operate(detail, 'announce')">重发公告 / 结果</el-button>
        <el-button v-if="isCancellable(detail)" :disabled="busy" type="danger" plain @click="operate(detail, 'cancel')">取消活动并退款</el-button>
        <el-button :disabled="busy" @click="showDetail(detail.id, false)">刷新记录</el-button>
      </div>
      <p>{{ typeName(detail) }} · {{ schedule(detail) }} · {{ detail.participant_count }} 人参与<span v-if="detail.config.mode === 'instant'"> · 已抽 {{ detail.draw_count }} 次</span></p>
      <p v-if="detail.config.description" class="lottery-description">{{ detail.config.description }}</p>
      <p>每次消耗 {{ detail.config.entry_cost }} 积分；参与前积分门槛 {{ detail.config.min_balance }} 分。<span v-if="detail.config.min_active_days">最近 {{ detail.config.active_window_days }} 天至少 {{ detail.config.min_active_days }} 天获得聊天积分。</span><span v-if="detail.config.mode === 'raffle'">每人一次机会，至少 {{ detail.config.min_participants }} 人成团，不足取消并退款。</span><span v-else>每日最多 {{ detail.config.daily_limit }} 次。</span></p>
      <ul class="lottery-prizes"><li v-for="(prize, index) in detail.config.prizes" :key="index">{{ prize.name }}<span v-if="prize.kind === 'points'"> · {{ prize.points }} 积分</span><span v-if="prize.kind !== 'none'"> · 剩余 {{ detail.remaining[index] }}/{{ prize.quantity }} 份</span><span v-if="detail.config.mode === 'instant'"> · 概率 {{ (prize.weight / detail.config.prizes.reduce((sum, item) => sum + item.weight, 0) * 100).toFixed(2) }}%</span></li></ul>
      <p v-if="detail.config.mode === 'raffle'" class="muted">正常开奖后未中奖不退报名积分；活动取消退款。每人最多中奖一次，人数少于名额时按奖品顺序发放。</p>
      <p v-else class="muted">奖品领完后对应概率计为未中奖，其余概率不变；全部奖品领完后停止扣分。每次独立随机。</p>
      <el-alert v-for="delivery in detail.deliveries.filter(item => item.state === 'failed')" :key="delivery.id" type="warning" :title="`公告发送失败：${delivery.error}`" :closable="false"/>
      <h3>参与和领奖记录</h3>
      <el-select v-model="entryState" aria-label="筛选参与记录" @change="entryPage = 1"><el-option label="全部记录" value=""/><el-option v-for="(label, value) in entryStates" :key="value" :label="label" :value="value"/></el-select>
      <div class="lottery-records">
        <article v-for="entry in detail.entries" :key="entry.id" class="lottery-entry">
          <div><strong>{{ entry.display_name || entry.user_id }}</strong><span class="muted"> · {{ entry.user_id }} · {{ time(entry.created_at) }}</span></div>
          <p>{{ entryStates[entry.state] }}<span v-if="entry.prize_label"> · {{ entry.prize_label }}</span> · 消耗 {{ entry.fee }} 分<span v-if="entry.state === 'won'"> · {{ entry.fulfilled ? '已发放' : entry.reward_points ? '积分待入账' : '待人工发放' }}</span></p>
          <el-button v-if="entry.state === 'won' && !entry.fulfilled && !entry.reward_points" :disabled="busy" @click="operate(detail, 'fulfill', entry.id)">登记已发放</el-button>
        </article>
        <el-empty v-if="!detail.entries.length" description="暂无记录"/>
      </div>
      <el-pagination v-if="detail.total_entries > 50" v-model:current-page="entryPage" :page-size="50" :total="detail.total_entries" layout="prev, pager, next"/>
    </template>
  </el-dialog>
</template>

<style scoped>
.lottery-toolbar,.lottery-title,.lottery-actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.lottery-toolbar { justify-content:space-between; margin-bottom:20px; }
.lottery-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr)); gap:18px; }
.lottery-card { border:1px solid var(--el-border-color-light); border-radius:12px; padding:22px; background:var(--el-bg-color); overflow-wrap:anywhere; }
.lottery-title { justify-content:space-between; align-items:flex-start; }
.lottery-title h2 { font-size:18px; margin:0; flex:1; }
.lottery-card p { color:var(--el-text-color-secondary); }
.lottery-actions { margin:16px 0; }
.lottery-actions .el-button + .el-button { margin-left:0; }
.lottery-form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 20px; }
.lottery-form-grid :deep(.el-select),.lottery-form-grid :deep(.el-date-editor) { width:100%; }
.prize-row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; padding:16px 0; border-bottom:1px solid var(--el-border-color-light); }
.prize-row .el-form-item { flex:1 1 130px; min-width:0; margin-bottom:8px; }
.prize-row :deep(.el-input-number),.prize-row :deep(.el-select) { width:100%; }
.lottery-description { white-space:pre-wrap; }
.lottery-prizes { padding-left:20px; line-height:1.9; }
.lottery-records { margin-top:16px; }
.lottery-entry { padding:16px 0; border-bottom:1px solid var(--el-border-color-light); overflow-wrap:anywhere; }
.lottery-entry p { margin:8px 0; }
@media(max-width:600px) { .lottery-form-grid { grid-template-columns:1fr; } .lottery-card { padding:16px; } }
</style>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, time } from './api'

const props = defineProps<{ groupId: number; enabled: boolean }>()
const rows = ref<any[]>([]), total = ref(0), page = ref(1), busy = ref(false), error = ref('')
const opened = ref(false), detail = ref<any>(null), detailBusy = ref(false)
const filters = reactive({ query: '', sender_id: '', message_type: '', flagged: false, deletion: '', dates: null as string[] | null })
const applied = ref('')
const states: Record<string, string> = { not_requested: '未发起删除', pending: '等待删除', failed: '删除失败', deleted: 'PurrAI 已确认删除', already_absent: '删除时已不存在' }
const types: Record<string, string> = { text: '文字', photo: '图片', video: '视频', document: '文件', animation: '动图', sticker: '贴纸', voice: '语音', audio: '音频', video_note: '视频留言', contact: '联系人', location: '位置', poll: '投票', service: '群事件', unknown: '历史摘录' }
const exportUrl = computed(() => `/api/groups/${props.groupId}/messages/export?${applied.value}`)
let version = 0, detailVersion = 0
async function load(apply = false) {
  error.value = ''; busy.value = true
  const current = ++version
  try {
    if (apply) {
      if (filters.sender_id && !/^-?\d+$/.test(filters.sender_id.trim())) throw new Error('请填写有效的用户或频道数字 ID')
      const query = new URLSearchParams()
      for (const key of ['query', 'sender_id', 'message_type', 'deletion'] as const) if (filters[key].trim()) query.set(key, filters[key].trim())
      if (filters.flagged) query.set('flagged', 'true')
      if (filters.dates) {
        query.set('after', String(Date.parse(filters.dates[0]!.replace(' ', 'T') + '+08:00') / 1000))
        query.set('before', String(Date.parse(filters.dates[1]!.replace(' ', 'T') + '+08:00') / 1000))
      }
      applied.value = query.toString(); page.value = 1
    }
    const result = await api(`/api/groups/${props.groupId}/messages?${applied.value}&page_number=${page.value}`)
    if (current === version) { rows.value = result.items; total.value = result.total }
  } catch (e) { if (current === version) error.value = (e as Error).message }
  finally { if (current === version) busy.value = false }
}
async function show(row: any) {
  const current = ++detailVersion
  detail.value = null; opened.value = true; detailBusy.value = true; error.value = ''
  try {
    const result = await api(`/api/groups/${props.groupId}/messages/${row.id}`)
    if (current === detailVersion) detail.value = result
  } catch (e) { if (current === detailVersion) error.value = (e as Error).message }
  finally { if (current === detailVersion) detailBusy.value = false }
}
watch(opened, value => { if (!value) { detailVersion++; detail.value = null; detailBusy.value = false } }, { flush: 'sync' })
watch(() => props.groupId, () => { opened.value = false; load(true) })
onUnmounted(() => { version++; detailVersion++ })
onMounted(() => load(true))
</script>

<template>
  <section class="panel archive-panel" v-loading="busy">
    <div class="panel-title"><h2>消息归档</h2><el-tag :type="enabled ? 'success' : 'info'">{{ enabled ? '正在记录新消息' : '已停止记录新消息' }}</el-tag></div>
    <p class="muted">保存机器人收到的群消息、编辑版本和处置结果，关闭记录不会清除已有数据。发送者及媒体信息保存在详情和导出中，图片、视频原文件不会自动下载。</p>
    <div class="filterbar">
      <el-input v-model="filters.query" aria-label="归档关键词" placeholder="搜索正文、昵称或历史编辑" clearable @keyup.enter="load(true)"/>
      <el-input v-model="filters.sender_id" aria-label="归档发送者 ID" placeholder="用户或频道数字 ID" clearable/>
      <el-select v-model="filters.message_type" aria-label="归档消息类型" placeholder="全部消息类型" clearable><el-option v-for="(name, key) in types" :key="key" :value="key" :label="name"/></el-select>
      <el-select v-model="filters.deletion" aria-label="归档删除状态" placeholder="全部删除状态" clearable><el-option v-for="(name, key) in states" :key="key" :value="key" :label="name"/></el-select>
      <el-date-picker v-model="filters.dates" type="datetimerange" value-format="YYYY-MM-DD HH:mm:ss" start-placeholder="开始（北京时间）" end-placeholder="结束（北京时间）"/>
      <el-checkbox v-model="filters.flagged">只看命中规则</el-checkbox>
      <el-button type="primary" @click="load(true)">查询归档</el-button>
      <a class="archive-download" :href="exportUrl">导出当前查询 JSONL</a>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <el-table :data="rows" empty-text="暂无匹配的归档消息">
      <el-table-column label="发送时间 / 编号" min-width="175"><template #default="{ row }">{{ time(row.sent_at) }}<div class="cell-sub">{{ row.message_id }}{{ row.source === 'violation_excerpt' ? '（旧摘录，时间为记录时间）' : '' }}</div></template></el-table-column>
      <el-table-column label="发送者" min-width="170"><template #default="{ row }">{{ row.sender_name || '未知' }}<div class="cell-sub">{{ row.sender_id }} <span v-if="row.username">@{{ row.username }}</span> {{ row.sender_type === 'chat' ? '（频道身份）' : '' }}</div></template></el-table-column>
      <el-table-column label="内容" min-width="250"><template #default="{ row }"><span class="archive-excerpt">{{ row.text || row.caption || `（${types[row.message_type] || row.message_type}）` }}</span><div class="cell-sub">{{ row.revision_count }} 个版本 <span v-if="!row.original_observed">· 未收到原始版本</span></div></template></el-table-column>
      <el-table-column label="命中原因" min-width="180"><template #default="{ row }">{{ row.violation?.reasons.join('；') || '未命中规则' }}</template></el-table-column>
      <el-table-column label="删除结果" min-width="170"><template #default="{ row }">{{ states[row.deletion_state] }}<div class="cell-sub">{{ row.deletion_error }}</div></template></el-table-column>
      <el-table-column label="操作" width="95"><template #default="{ row }"><el-button text @click="show(row)">查看详情</el-button></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load(false)"/>
    <p class="muted">“未发起删除”仅表示 PurrAI 没有删除任务，不能证明消息仍在群中。其他人删除消息通常没有 Bot 通知；接入前的历史消息无法补抓。导出包含当前查询全部匹配消息及其收到的版本。</p>
  </section>
  <el-drawer v-model="opened" title="归档消息详情" size="min(760px, 96vw)">
    <div v-loading="detailBusy" class="archive-detail">
      <el-alert v-if="error" :title="error" type="error" :closable="false"/>
      <template v-if="detail">
        <p>消息 {{ detail.message_id }} · {{ detail.sender_name }} <span v-if="detail.username">@{{ detail.username }}</span>（{{ detail.sender_id }}）</p>
        <p>{{ states[detail.deletion_state] }} {{ detail.deletion_error }}</p>
        <el-alert v-if="!detail.original_observed" title="未收到原始版本；历史违规摘录也无法还原完整原文。" type="info" :closable="false"/>
        <section v-for="revision in detail.revisions" :key="revision.id" class="archive-revision">
          <h3>{{ revision.is_edit ? '编辑版本' : '收到的版本' }} · {{ time(revision.event_at) }}</h3>
          <pre class="archive-body">{{ revision.payload.text || revision.payload.caption || '（无文字内容）' }}</pre>
          <p v-if="revision.reasons.length">当时命中：{{ revision.reasons.join('；') }}</p>
          <details><summary>链接、媒体与消息元数据</summary><pre>{{ JSON.stringify(revision.payload, null, 2) }}</pre></details>
        </section>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.archive-download{color:var(--el-color-primary);font-size:14px;padding:8px 0;text-decoration:underline}
.archive-excerpt{white-space:pre-wrap;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.archive-detail{min-height:100px;overflow-wrap:anywhere}.archive-revision{border-top:1px solid #e3e9e4;margin-top:24px;padding-top:16px}
.archive-revision h3{font-size:14px}.archive-revision pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:100%;font:13px/1.8 "Microsoft YaHei",sans-serif}
.archive-revision details{font-size:13px;color:#657a6d}.archive-revision summary{cursor:pointer}
</style>

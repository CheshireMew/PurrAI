<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { stateLabel } from '../catalog'
import { useWorkspace } from '../workspace'
import { useLatestRequest } from '../composables/useLatestRequest'
import { useRefresh } from '../composables/useRefresh'
import { useDraftClose } from '../composables/useDraftClose'

type MenuAction = 'signin' | 'balance' | 'lottery'
type MenuStyle = 'default' | 'primary' | 'success' | 'danger'
interface Button { text: string; action: MenuAction; style: MenuStyle }
interface Config { rows: Button[][] }
interface MenuDocument {
  config: Config; version: number; active: boolean; needs_update: boolean
  delivery: { id: string; state: string; error: string; operation: 'publish' | 'clear' } | null
}
const props = defineProps<{ groupId: number; refreshKey: number }>()
const { mutations } = useWorkspace()
const { busy, error, safe, run } = useLatestRequest()
const doc = ref<MenuDocument | null>(null), form = ref<Config>({ rows: [] }), baseline = ref('')
const dirty = computed(() => !!baseline.value && JSON.stringify(form.value) !== baseline.value)
const labels: Record<MenuAction, string> = { signin: '每日签到', balance: '查询积分', lottery: '查看抽奖' }
const colors: Record<MenuStyle, string> = { default: '默认', primary: '蓝色', success: '绿色', danger: '红色' }
const count = computed(() => form.value.rows.reduce((total, row) => total + row.length, 0))
const pending = computed(() => doc.value?.delivery?.state === 'pending')
const status = computed(() => !doc.value ? '' : pending.value
  ? (doc.value.delivery?.operation === 'publish' ? '正在更新群菜单…' : '正在清除群菜单…')
  : doc.value.delivery?.state === 'failed' ? '上次菜单操作未确认完成，请查看下方原因'
  : doc.value.active ? (doc.value.needs_update ? '配置已保存，等待更新到群' : '菜单已显示在群内') : '尚未显示菜单或已清除')
const base = () => `/api/groups/${props.groupId}/menu`
function accept(value: MenuDocument) {
  doc.value = value
  form.value = JSON.parse(JSON.stringify(value.config))
  baseline.value = JSON.stringify(form.value)
}
async function load() {
  await run(() => api<MenuDocument>(base()), value => {
    if (!dirty.value) accept(value)
    else if (doc.value) doc.value = { ...doc.value, active: value.active, needs_update: value.needs_update, delivery: value.delivery }
  })
}
async function save() {
  await safe(async () => {
    const value = await mutations.submit<MenuDocument>(base(), 'PUT', { config: form.value, version: doc.value!.version }, undefined, '保存底部菜单')
    accept(value)
    ElMessage.success('菜单配置已保存，点击“更新到群”即可应用')
  })
}
async function operate(operation: 'publish' | 'clear') {
  await safe(async () => {
    const value = await mutations.submit<MenuDocument>(`${base()}/operate`, 'POST',
      { operation, version: doc.value!.version }, undefined, operation === 'publish' ? '更新群底部菜单' : '清除群底部菜单')
    if (dirty.value) doc.value = value
    else accept(value)
    ElMessage.success(operation === 'publish' ? '已提交菜单更新' : '已提交菜单清除')
  })
}
function addButton(row: Button[]) {
  let number = 1
  const names = new Set(form.value.rows.flat().map(button => button.text))
  while (names.has(`按钮 ${number}`)) number++
  row.push({ text: `按钮 ${number}`, action: 'signin', style: 'default' })
}
function addRow() { const row: Button[] = []; addButton(row); form.value.rows.push(row) }
function move<T>(items: T[], index: number, delta: number) {
  const next = index + delta
  if (next >= 0 && next < items.length) [items[index], items[next]] = [items[next]!, items[index]!]
}
function removeButton(row: Button[], index: number, rowIndex: number) {
  row.splice(index, 1)
  if (!row.length) form.value.rows.splice(rowIndex, 1)
}
const { confirmDiscard } = useDraftClose(() => dirty.value, () => { if (doc.value) accept(doc.value) }, () => busy.value)
async function reloadConfig() {
  if (!await confirmDiscard()) return
  if (doc.value) accept(doc.value)
  await load()
}
watch(() => props.groupId, () => { doc.value = null; baseline.value = ''; form.value = { rows: [] }; void load() })
useRefresh(load, () => props.refreshKey, () => pending.value)
</script>

<template>
<el-alert v-if="error" :title="error" type="error" show-icon class="error-banner" @close="error = ''"/>
<section class="panel" v-loading="busy">
  <div class="panel-title"><h2>底部菜单</h2><div>
    <el-button v-if="dirty" :disabled="busy" @click="reloadConfig">重新载入配置</el-button>
    <el-button :disabled="busy" @click="load">刷新状态</el-button>
  </div></div>
  <template v-if="doc">
    <p class="muted">群友点击按钮后发送按钮文字，机器人执行绑定的功能。保存配置后，点击“更新到群”替换现有菜单；“清除群菜单”也可移除其他机器人留下的菜单。</p>
    <p role="status">{{ status }}</p>
    <el-alert v-if="doc.delivery && ['failed', 'cancelled'].includes(doc.delivery.state)"
      :title="`${doc.delivery.operation === 'publish' ? '菜单更新' : '菜单清除'}${stateLabel(doc.delivery.state)}`"
      :description="doc.delivery.error" type="warning" show-icon :closable="false" class="delivery-error"/>
    <div class="menu-layout">
      <div class="menu-editor">
        <fieldset v-for="(row, rowIndex) in form.rows" :key="rowIndex" class="menu-edit-row" :disabled="busy">
          <legend>第 {{ rowIndex + 1 }} 行</legend>
          <div class="row-actions">
            <el-button size="small" :disabled="busy || rowIndex === 0" :aria-label="`上移第${rowIndex + 1}行`" @click="move(form.rows, rowIndex, -1)">上移</el-button>
            <el-button size="small" :disabled="busy || rowIndex === form.rows.length - 1" :aria-label="`下移第${rowIndex + 1}行`" @click="move(form.rows, rowIndex, 1)">下移</el-button>
          </div>
          <div v-for="(button, index) in row" :key="index" class="menu-button-editor">
            <el-input v-model="button.text" maxlength="64" :disabled="busy" :aria-label="`第${rowIndex + 1}行第${index + 1}个按钮文字`" placeholder="按钮文字"/>
            <el-select v-model="button.action" :disabled="busy" :aria-label="`第${rowIndex + 1}行第${index + 1}个按钮功能`">
              <el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value"/>
            </el-select>
            <el-select v-model="button.style" :disabled="busy" :aria-label="`第${rowIndex + 1}行第${index + 1}个按钮颜色`">
              <el-option v-for="(label, value) in colors" :key="value" :label="label" :value="value"/>
            </el-select>
            <div class="button-actions">
              <el-button text :disabled="busy || index === 0" :aria-label="`左移第${rowIndex + 1}行第${index + 1}个按钮`" @click="move(row, index, -1)">←</el-button>
              <el-button text :disabled="busy || index === row.length - 1" :aria-label="`右移第${rowIndex + 1}行第${index + 1}个按钮`" @click="move(row, index, 1)">→</el-button>
              <el-button text type="danger" :disabled="busy" :aria-label="`移除第${rowIndex + 1}行第${index + 1}个按钮`" @click="removeButton(row, index, rowIndex)">移除</el-button>
            </div>
          </div>
          <el-button size="small" :disabled="busy || row.length >= 8 || count >= 32" @click="addButton(row)">添加按钮</el-button>
        </fieldset>
        <el-button :disabled="busy || form.rows.length >= 8 || count >= 32" @click="addRow">添加一行</el-button>
      </div>
      <div class="menu-preview" aria-label="底部菜单预览">
        <h3>预览</h3>
        <div class="preview-chat">
          <div class="preview-input">消息</div>
          <div v-for="(row, index) in form.rows" :key="index" class="preview-row">
            <span v-for="(button, buttonIndex) in row" :key="buttonIndex" class="preview-button" :class="button.style">{{ button.text || '按钮' }}</span>
          </div>
        </div>
        <p class="muted">实际颜色随 Telegram 主题和客户端版本显示。签到和积分沿用本群积分设置；抽奖按钮展示正在进行的活动。</p>
      </div>
    </div>
    <div class="menu-footer">
      <el-button type="primary" :loading="busy" :disabled="!dirty || !form.rows.length" @click="save">保存配置</el-button>
      <el-button type="success" :disabled="busy || dirty" @click="operate('publish')">更新到群</el-button>
      <el-button :disabled="busy" @click="operate('clear')">清除群菜单</el-button>
    </div>
  </template>
  <el-empty v-else-if="!busy" description="菜单加载失败"><el-button @click="load">重试</el-button></el-empty>
</section>
</template>

<style scoped>
.menu-layout { display: grid; grid-template-columns: minmax(0, 3fr) minmax(240px, 2fr); gap: 24px; align-items: start; }
.menu-edit-row { min-width: 0; border: 1px solid var(--el-border-color); border-radius: 8px; padding: 14px; margin: 0 0 16px; }
.menu-edit-row legend { padding: 0 6px; font-weight: 600; }
.row-actions { display: flex; justify-content: flex-end; margin-bottom: 10px; }
.menu-button-editor { display: grid; grid-template-columns: minmax(120px, 2fr) minmax(110px, 1fr) minmax(90px, 1fr); gap: 8px; margin-bottom: 12px; }
.button-actions { grid-column: 1 / -1; display: flex; justify-content: flex-end; }
.button-actions .el-button + .el-button { margin-left: 4px; }
.menu-preview h3 { margin-top: 0; }
.preview-chat { padding: 12px; background: var(--el-fill-color-light); border: 1px solid var(--el-border-color); border-radius: 12px; }
.preview-input { background: var(--el-bg-color); color: var(--el-text-color-placeholder); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.preview-row { display: flex; gap: 6px; margin-top: 6px; }
.preview-button { flex: 1; min-width: 0; overflow-wrap: anywhere; text-align: center; padding: 12px 6px; background: var(--el-fill-color-darker); border-radius: 8px; font-weight: 600; }
.preview-button.primary { background: #456fb7; color: white; }
.preview-button.success { background: #418844; color: white; }
.preview-button.danger { background: #ba4141; color: white; }
.menu-footer { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 24px; }
.menu-footer .el-button + .el-button { margin-left: 0; }
.delivery-error { margin-bottom: 18px; }
@media (max-width: 1000px) { .menu-layout { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .menu-button-editor { grid-template-columns: 1fr 1fr; } .menu-button-editor > .el-input { grid-column: 1 / -1; } }
</style>

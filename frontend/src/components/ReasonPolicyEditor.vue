<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ReasonPolicy } from '../businessPolicy'

const model = defineModel<ReasonPolicy>({ required: true })
const categories = [
  { key: 'ban', label: '旧记录的封禁判定' },
  { key: 'contact', label: '旧记录的联系人判定' },
  { key: 'spam', label: '刷屏处置' },
  { key: 'repeat', label: '重复消息清理' },
  { key: 'advertising_stats', label: '广告统计' },
  { key: 'spam_stats', label: '刷屏统计' },
  { key: 'ai_history', label: '历史 AI 广告样本导入' },
  { key: 'target_learning', label: '推广目标学习' },
  { key: 'media_gate', label: '发图门槛旧任务识别' },
] as const
const selected = ref<keyof ReasonPolicy>('ban')
const entries = computed(() => model.value[selected.value])
function add() {
  entries.value.push({ id: 'reason-' + crypto.randomUUID(), title: '', enabled: true, mode: 'prefix', value: '' })
}
</script>

<template>
  <div class="reason-editor">
    <div class="toolbar"><el-select v-model="selected" aria-label="判定分类用途">
      <el-option v-for="item in categories" :key="item.key" :label="item.label" :value="item.key"/>
    </el-select><el-button @click="add">添加判定匹配</el-button></div>
    <p>匹配系统保存的判定说明。新记录已明确保存的处置优先；修改旧记录匹配不会追溯执行处罚。</p>
    <div v-for="(item, index) in entries" :key="item.id" class="reason-row">
      <el-input v-model="item.title" aria-label="判定匹配名称" placeholder="名称" maxlength="160"/>
      <el-select v-model="item.mode" aria-label="判定匹配方式"><el-option value="exact" label="完全一致"/><el-option value="prefix" label="以此开头"/><el-option value="contains" label="包含文字"/></el-select>
      <el-input v-model="item.value" aria-label="判定匹配文字" placeholder="判定说明中的文字" maxlength="500"/>
      <el-switch v-model="item.enabled" aria-label="启用判定匹配"/>
      <el-button @click="entries.splice(index, 1)">移除</el-button>
    </div>
    <el-empty v-if="!entries.length" description="本类规则为空，不执行文字匹配"/>
  </div>
</template>

<style scoped>
.toolbar { display: flex; gap: 12px; flex-wrap: wrap }.toolbar .el-select { width: 260px }
p { color: var(--el-text-color-secondary); line-height: 1.7 }
.reason-row { display: grid; grid-template-columns: 160px 130px minmax(0, 1fr) 45px 65px; gap: 10px; align-items: center; margin: 12px 0 }
@media(max-width: 700px) { .reason-row { grid-template-columns: minmax(0, 1fr) 45px 65px }.reason-row > :first-child,.reason-row > :nth-child(2) { grid-column: 1 / -1 } }
</style>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { RecognitionPolicy } from '../businessPolicy'

const model = defineModel<RecognitionPolicy>({ required: true })
const categories = [
  { key: 'utility_patterns', label: '直接忽略的功能口令', pattern: true, help: '整条消息匹配后不参与聊天和 AI 广告审核；只排除口令，不处罚成员。' },
  { key: 'candidate_patterns', label: '可学习的功能口令', pattern: true, help: '去除空白和末尾数字后匹配；达到群内配置的次数和人数才忽略。' },
  { key: 'other_bot_patterns', label: '其他机器人的口令', pattern: true, help: '仅检查发给其他机器人的口令名称，匹配后不参与聊天。' },
  { key: 'output_patterns', label: '机器人输出限制', pattern: true, help: '匹配机器人准备发送的文字，命中时停止发送；不会处罚群友。' },
  { key: 'phone_prefixes', label: '电话号码提示词', pattern: false, help: '按原文字面识别提示词后面的电话号码，用于广告目标匹配和记录；提示词本身不是广告证据。' },
  { key: 'shortener_domains', label: '短链接服务名单', pattern: false, help: '名单中的服务按完整链接记录广告目标，避免将同一服务的所有链接一起拉黑。' },
] as const
const selected = ref<keyof RecognitionPolicy>('utility_patterns')
const category = computed(() => categories.find(item => item.key === selected.value)!)
const entries = computed(() => model.value[selected.value])
function add() {
  entries.value.push({ id: 'recognition-' + crypto.randomUUID(), title: '', enabled: true,
    ...(category.value.pattern ? { pattern: '' } : { value: '' }) })
}
</script>

<template>
  <div class="recognition-editor">
    <div class="toolbar">
      <el-select v-model="selected" aria-label="识别规则类别">
        <el-option v-for="item in categories" :key="item.key" :label="item.label" :value="item.key"/>
      </el-select>
      <el-button @click="add">添加识别规则</el-button>
    </div>
    <p class="help">{{ category.help }} {{ category.pattern ? '匹配表达式支持正则表达式，忽略英文字母大小写。' : '' }}</p>
    <div v-for="(item, index) in entries" :key="item.id" class="recognition-row">
      <el-input v-model="item.title" aria-label="识别规则名称" placeholder="规则名称" maxlength="160"/>
      <el-input v-if="category.pattern" v-model="item.pattern" aria-label="识别匹配表达式" placeholder="匹配表达式" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }" maxlength="4000"/>
      <el-input v-else v-model="item.value" aria-label="识别词条" :placeholder="selected === 'shortener_domains' ? '服务域名，不含协议和路径' : '提示词原文'" maxlength="250"/>
      <el-switch v-model="item.enabled" aria-label="启用识别规则"/>
      <el-button @click="entries.splice(index, 1)">移除</el-button>
    </div>
    <el-empty v-if="!entries.length" description="本类规则为空，不执行匹配"/>
  </div>
</template>

<style scoped>
.toolbar { display: flex; gap: 12px; flex-wrap: wrap }.toolbar .el-select { width: 260px }
.help { color: var(--el-text-color-secondary); line-height: 1.7 }
.recognition-row { display: grid; grid-template-columns: 170px minmax(0, 1fr) 45px 65px; gap: 10px; align-items: center; margin: 12px 0 }
@media(max-width: 700px) { .recognition-row { grid-template-columns: minmax(0, 1fr) 45px 65px }.recognition-row > :first-child { grid-column: 1 / -1 } }
</style>

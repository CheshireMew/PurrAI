<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { TextStep } from '../businessPolicy'
const model = defineModel<TextStep[]>({ required: true })
const character = ref(''), replacement = ref('')
function change(step: TextStep, op: string) {
  for (const key of Object.keys(step)) delete (step as any)[key]
  Object.assign(step, { op }, op === 'replace' ? { pattern: '', value: '' } : op === 'translate' ? { value: {} } : {})
}
function mapping(step: TextStep): Record<string, string> { return step.value as Record<string, string> }
function add(step: TextStep) {
  if ([...character.value].length !== 1) { ElMessage.error('转换前请填写一个字符'); return }
  mapping(step)[character.value] = replacement.value
  character.value = ''; replacement.value = ''
}
</script>
<template>
<div class="steps">
<div v-for="(step, index) in model" :key="index" class="step">
  <div class="step-head"><el-select :model-value="step.op" aria-label="文本处理方式" @change="change(step, $event)">
    <el-option value="normalize" label="统一全半角、大小写及不可见字符"/><el-option value="compact" label="只保留文字和数字"/>
    <el-option value="replace" label="按表达式替换文字"/><el-option value="translate" label="按字符转换表处理"/>
  </el-select><el-button :disabled="index === 0" @click="model.splice(index - 1, 0, model.splice(index, 1)[0]!)">上移</el-button><el-button @click="model.splice(index, 1)">移除步骤</el-button></div>
  <template v-if="step.op === 'replace'"><el-input v-model="step.pattern" aria-label="替换表达式" placeholder="匹配表达式"/><el-input v-model="step.value as string" aria-label="替换内容" placeholder="替换后的文本，留空表示移除"/></template>
  <template v-if="step.op === 'translate'">
    <div class="characters"><div v-for="(_, key) in mapping(step)" :key="key" class="character"><span>{{ key }}</span><el-input v-model="mapping(step)[key]" :aria-label="`字符 ${key} 转换为`" maxlength="16"/><el-button text @click="delete mapping(step)[key]">移除</el-button></div></div>
    <div class="step-head"><el-input v-model="character" aria-label="新增转换字符" placeholder="转换前"/><el-input v-model="replacement" aria-label="新增转换结果" placeholder="转换后" maxlength="16"/><el-button @click="add(step)">添加字符转换</el-button></div>
  </template>
</div>
<el-button @click="model.push({ op: 'normalize' })">添加文本处理步骤</el-button>
</div>
</template>
<style scoped>
.step { border: 1px solid var(--el-border-color); padding: 10px; border-radius: 8px; margin-bottom: 8px; display: grid; gap: 8px }
.step-head { display: flex; gap: 8px; align-items: center }.step-head .el-select { flex: 1; min-width: 150px }
.characters { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 6px; max-height: 220px; overflow-y: auto }
.character { display: flex; gap: 8px; align-items: center }.character span { min-width: 20px }
@media(max-width: 650px) { .step-head { flex-wrap: wrap } }
</style>

<script setup lang="ts">
import type { BusinessDocument, Expression } from '../businessPolicy'
import { expression, operations, textFields } from '../businessPolicy'
import TextStepsEditor from './TextStepsEditor.vue'
const model = defineModel<Expression>({ required: true })
defineProps<{ document: BusinessDocument }>()
function change(op: string) { model.value = expression(op) }
function changeField(value: string) {
  model.value.input = value
  if (model.value.op === 'each') {
    if (value === 'source_titles') delete model.value.split
    else model.value.split ??= '[。！？!?；;，,\\n]'
    model.value.steps ??= []
  }
}
</script>
<template>
<div class="condition">
<el-select :model-value="model.op" aria-label="条件类型" @change="change"><el-option v-for="(title, op) in operations" :key="op" :value="op" :label="title"/></el-select>
<template v-if="model.op === 'all' || model.op === 'any'">
  <div v-for="(_, index) in model.items" :key="index" class="child"><ConditionEditor v-model="model.items[index]" :document="document"/><el-button :disabled="model.items.length <= 1" @click="model.items.splice(index, 1)">移除条件</el-button></div>
  <el-button @click="model.items.push(expression())">添加条件</el-button>
</template>
<ConditionEditor v-else-if="model.op === 'not'" v-model="model.item" :document="document"/>
<template v-else-if="model.op === 'ref'">
  <el-select v-model="model.ref" aria-label="引用的公共条件"><el-option v-for="item in document.conditions" :key="item.id" :value="item.id" :label="item.title"/></el-select>
  <el-select :model-value="model.input || ''" aria-label="引用条件使用的文本" @change="$event ? model.input = $event : delete model.input"><el-option value="" label="保持当前文本"/><el-option v-for="field in textFields(document).filter(item => !['source_titles', 'direct', '$start'].includes(item.id))" :key="field.id" :value="field.id" :label="field.title"/></el-select>
</template>
<template v-else-if="model.op !== 'always'">
  <el-select :model-value="model.input" aria-label="检查的文本字段" @change="changeField"><el-option v-for="field in textFields(document)" :key="field.id" :value="field.id" :label="field.title"/></el-select>
  <template v-if="model.op === 'match'">
    <el-input v-model="model.pattern" type="textarea" :rows="2" aria-label="匹配表达式" placeholder="关键词可直接填写；用 | 分隔多个词，也支持正则表达式"/>
    <el-select v-model="model.mode" aria-label="表达式匹配方式"><el-option value="search" label="任意位置首次匹配"/><el-option value="match" label="从开头匹配"/><el-option value="fullmatch" label="整条文本匹配"/><el-option value="find" label="逐个匹配直到满足附加条件"/></el-select>
    <el-checkbox :model-value="!!model.where" @change="$event ? model.where = expression() : delete model.where">匹配片段还需满足附加条件</el-checkbox>
    <template v-if="model.where"><label>读取匹配前的字符数（0 表示全部）</label><el-input-number v-model="model.prefix_chars" :min="0" :max="1000"/><ConditionEditor v-model="model.where" :document="document"/></template>
  </template>
  <template v-if="model.op === 'each'">
    <el-input v-if="model.input !== 'source_titles'" v-model="model.split" aria-label="分句表达式" placeholder="用于分句的表达式"/>
    <TextStepsEditor v-model="model.steps"/>
    <ConditionEditor v-model="model.condition" :document="document"/>
  </template>
  <template v-if="model.op === 'equals'"><el-input-number v-if="typeof model.value === 'number'" v-model="model.value" aria-label="相等比较值"/><el-input v-else v-model="model.value" aria-label="相等比较值"/></template>
</template>
</div>
</template>
<style scoped>
.condition { display: grid; gap: 8px; border-left: 3px solid var(--el-border-color); padding: 10px; min-width: 0; background: var(--el-fill-color-blank) }
.condition > .el-select { width: 100% }.child { display: grid; gap: 5px; min-width: 0 }.child > .el-button { justify-self: start }
</style>

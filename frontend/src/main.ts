import { createApp, h } from 'vue'
import {
  ElAlert, ElButton, ElCheckbox, ElConfigProvider, ElDatePicker, ElDialog, ElDrawer,
  ElEmpty, ElForm, ElFormItem, ElIcon, ElInput, ElInputNumber, ElLoading, ElOption,
  ElPagination, ElRadio, ElRadioGroup, ElSelect, ElSwitch, ElTable, ElTableColumn,
  ElTabPane, ElTabs, ElTag, ElTooltip,
} from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/checkbox/style/css'
import 'element-plus/es/components/date-picker/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/drawer/style/css'
import 'element-plus/es/components/empty/style/css'
import 'element-plus/es/components/form/style/css'
import 'element-plus/es/components/form-item/style/css'
import 'element-plus/es/components/icon/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/input-number/style/css'
import 'element-plus/es/components/loading/style/css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/option/style/css'
import 'element-plus/es/components/pagination/style/css'
import 'element-plus/es/components/radio/style/css'
import 'element-plus/es/components/radio-group/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/switch/style/css'
import 'element-plus/es/components/table/style/css'
import 'element-plus/es/components/table-column/style/css'
import 'element-plus/es/components/tab-pane/style/css'
import 'element-plus/es/components/tabs/style/css'
import 'element-plus/es/components/tag/style/css'
import 'element-plus/es/components/tooltip/style/css'
import App from './App.vue'
import './style.css'

const app = createApp({ render: () => h(ElConfigProvider, { locale: zhCn }, () => h(App)) })
for (const component of [
  ElAlert, ElButton, ElCheckbox, ElDatePicker, ElDialog, ElDrawer, ElEmpty, ElForm,
  ElFormItem, ElIcon, ElInput, ElInputNumber, ElOption, ElPagination, ElRadio,
  ElRadioGroup, ElSelect, ElSwitch, ElTable, ElTableColumn, ElTabPane, ElTabs, ElTag, ElTooltip,
]) app.use(component)
app.directive('loading', ElLoading.directive)
app.mount('#app')

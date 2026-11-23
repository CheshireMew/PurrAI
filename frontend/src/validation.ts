import { catalog } from './catalog'

// Display names for API form fields, never matching or enforcement rules.
const names: Record<string, string> = {
  title: '活动标题', description: '活动说明', mode: '方式', entry_cost: '每次消耗积分', min_balance: '参与前积分门槛',
  active_window_days: '统计天数', min_active_days: '至少活跃天数', draw_mode: '开奖或结束方式', ends_at: '结束时间',
  participant_target: '开奖人数', min_participants: '最低成团人数', daily_limit: '每日抽奖次数', prizes: '奖品',
  name: '名称', kind: '类型', points: '积分', quantity: '数量', weight: '概率权重', config: '设置',
  batch_key: '批次编号', source: '数据来源', rows: '积分记录', source_key: '来源编号', display_name: '昵称',
  user_id: 'Telegram 数字 ID', source_rank: '原排名', note: '备注', delta: '变动积分', reason: '原因', version: '数据版本',
  keyword: '关键词', reply: '回复内容', priority: '优先级', cooldown_seconds: '冷却秒数', value: '规则内容',
  text: '内容', sender_name: '发送者昵称', scope: '适用范围', penalty: '处置方式', name_penalty: '昵称处置方式',
  signin_points: '每日签到奖励', chat_points: '每条聊天加分', chat_daily_limit: '聊天每日最多加分',
  chat_min_characters: '聊天至少几个汉字', chat_interval_seconds: '聊天加分间隔', packet_expiry_hours: '红包有效时间',
  newcomer_exemption_points: '新人限制豁免积分', advertising_exemption_points: 'AI 广告审核豁免积分',
  trust_excluded_user_ids: '取消自动豁免的成员 ID', commands: '口令设置',
}

type ValidationIssue = { loc?: (string | number)[]; type?: string; msg?: string; ctx?: Record<string, any> }
export function validationMessage(issue: ValidationIssue) {
  const location = (issue.loc || []).filter(part => !['body', 'query', 'path'].includes(String(part)))
  const last = String(location.at(-1) ?? '')
  let field = catalog.value?.settings.labels?.[last] || names[last] || '输入内容'
  const prize = location.indexOf('prizes'), row = location.indexOf('rows')
  if (prize >= 0 && typeof location[prize + 1] === 'number') field = `奖品 ${Number(location[prize + 1]) + 1} 的${names[last] || '内容'}`
  if (row >= 0 && typeof location[row + 1] === 'number') field = `第 ${Number(location[row + 1]) + 1} 条记录的${names[last] || '内容'}`
  const ctx = issue.ctx || {}
  const messages: Record<string, string> = {
    missing: '请填写此项', string_too_short: ctx.min_length === 1 ? '不能为空' : `至少填写 ${ctx.min_length} 个字符`,
    string_too_long: `不能超过 ${ctx.max_length} 个字符`, greater_than_equal: `不能小于 ${ctx.ge}`,
    greater_than: `必须大于 ${ctx.gt}`, less_than_equal: `不能大于 ${ctx.le}`, less_than: `必须小于 ${ctx.lt}`,
    int_parsing: '请填写整数', int_type: '请填写整数', int_from_float: '请填写整数',
    float_parsing: '请填写有效数字', float_type: '请填写有效数字', finite_number: '请填写有效数字',
    bool_type: '请选择开启或关闭', bool_parsing: '请选择开启或关闭', literal_error: '请从提供的选项中选择',
    string_type: '请填写文字', list_type: '应为记录列表', too_short: `至少包含 ${ctx.min_length} 项`,
    too_long: `不能超过 ${ctx.max_length} 项`, model_attributes_type: '应为完整的 JSON 对象，请使用下载的模板',
    model_type: '应为完整的 JSON 对象，请使用下载的模板', dict_type: '应为 JSON 对象',
  }
  // Preserve explicit Chinese validation from the server, without exposing its English wrapper.
  const message = issue.msg && /[\u3400-\u9fff]/.test(issue.msg)
    ? issue.msg.replace(/^Value error,\s*/, '')
    : messages[issue.type || ''] || '格式不正确，请检查后重试'
  return `${field}：${message}`
}

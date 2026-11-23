export const pointsImportTemplate = {
  batch_key: '请改为本批数据的唯一编号', source: '请填写原始数据来源',
  rows: [{ source_key: '成员在原数据中的唯一编号', display_name: '示例昵称', points: 0,
    user_id: null, source_rank: null, note: '' }],
}
export const pointsTemplateDownload = 'data:application/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(pointsImportTemplate, null, 2))

export function parsePointsImport(text: string) {
  let value: any
  try { value = JSON.parse(text.replace(/^\uFEFF/, '')) }
  catch { throw new Error('无法读取 JSON 文件，请检查引号、逗号和括号，或使用下载的模板填写') }
  if (!value || Array.isArray(value) || typeof value !== 'object') {
    throw new Error('文件最外层应包含批次编号（batch_key）、数据来源（source）和积分记录（rows），请按模板填写')
  }
  if (typeof value.batch_key !== 'string' || !value.batch_key.trim()) throw new Error('批次编号（batch_key）不能为空；同一批数据重试时请保留原编号')
  if (typeof value.source !== 'string' || !value.source.trim()) throw new Error('请填写数据来源（source）')
  if (!Array.isArray(value.rows) || !value.rows.length) throw new Error('积分记录（rows）应为至少包含一条记录的列表')
  for (const [index, row] of value.rows.entries()) {
    if (!row || Array.isArray(row) || typeof row !== 'object') throw new Error(`第 ${index + 1} 条积分记录应为 JSON 对象`)
    for (const [key, label] of Object.entries({ source_key: '来源编号', display_name: '昵称', points: '积分' })) {
      if (!(key in row)) throw new Error(`第 ${index + 1} 条记录缺少${label}（${key}），请对照模板补全`)
    }
  }
  return value
}

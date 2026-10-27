export type DateRange = [string, string] | null

export function applyBeijingDates(query: URLSearchParams, dates: DateRange) {
  if (dates) {
    const values = dates.map(value => Date.parse(value.replace(' ', 'T') + '+08:00') / 1000)
    if (values.some(value => !Number.isFinite(value)) || values[0]! > values[1]!) {
      throw new Error('请填写有效的北京时间范围，结束时间不能早于开始时间')
    }
    query.set('after', String(values[0]))
    query.set('before', String(values[1]))
  }
  return query
}

import { describe, expect, it } from 'vitest'
import { previewMessages } from './usePreview'
import { recordQuery } from './useRecords'

describe('共享预览与记录筛选', () => {
  it('规则草稿和已保存规则使用相同的实际处置说明', () => {
    expect(previewMessages({ reasons: ['分享联系人'], kick_member: true, delete_message: true }))
      .toEqual(['分享联系人', '将踢出成员', '将删除消息'])
    expect(previewMessages({ reasons: [], mute_member: true })).toEqual(['将禁言成员'])
    expect(previewMessages({ reasons: [] })).toEqual([])
  })
  it('分页、成员和时间筛选保留各自参数，时间明确按北京时间解析', () => {
    const query = recordQuery(2, 'pending', ' 42 ', ['2026-10-21 00:00:00', '2026-10-22 00:00:00'])
    expect(query.get('page_number')).toBe('2')
    expect(query.get('state')).toBe('pending')
    expect(query.get('user_id')).toBe('42')
    expect(Number(query.get('before')) - Number(query.get('after'))).toBe(86400)
    expect(Number(query.get('after'))).toBe(Date.parse('2026-10-21T00:00:00+08:00') / 1000)
    expect(recordQuery(1, '', '', null).has('after')).toBe(false)
  })
})

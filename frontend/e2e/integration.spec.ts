import { expect, test } from '@playwright/test'
import { openPage } from './navigation'

test('真实后端完成积分写入、流水和菜单发布', async ({ page, request }) => {
  // This guard precedes every mutation. A custom base URL must never turn an
  // integration test into writes against an administrator's real instance.
  expect((await request.get('/__local/status')).ok()).toBe(true)
  expect((await (await request.get('/__local/status')).json()).mode).toBe('local-development')
  expect((await (await request.get('/api/me')).json()).local_dev).toBe(true)
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const id = 8000000 + Math.floor(Date.now() % 1000000)
  const name = `集成成员 ${id}`
  await page.goto('/#/points?group=-10010001')
  await page.getByRole('button', { name: '新增账户', exact: true }).click()
  let dialog = page.getByRole('dialog', { name: '新增积分账户', exact: true })
  await dialog.getByRole('textbox', { name: '积分账户昵称' }).fill(name)
  await dialog.getByRole('textbox', { name: 'Telegram 数字 ID' }).fill(String(id))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toBeHidden()
  await page.getByRole('textbox', { name: '搜索积分账户' }).fill(String(id))
  await page.getByRole('button', { name: '查询', exact: true }).click()
  const row = page.locator('.points-table').getByRole('row').filter({ hasText: name })
  await row.getByRole('button', { name: '增减', exact: true }).click()
  dialog = page.getByRole('dialog', { name: '增减积分', exact: true })
  await dialog.getByRole('spinbutton', { name: '变动积分' }).fill('27')
  await dialog.getByRole('textbox', { name: '修改原因' }).fill('真实接口回归')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(row.getByRole('cell', { name: '27', exact: true })).toBeVisible()
  await row.getByRole('button', { name: '流水', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '积分流水' }).getByText('真实接口回归', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await openPage(page, '底部菜单')
  await page.getByRole('textbox', { name: '第1行第1个按钮文字', exact: true }).fill('每日集成签到')
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await page.getByRole('button', { name: '更新到群', exact: true }).click()
  await expect.poll(async () => (await (await request.get('/api/groups/-10010001/menu')).json()).delivery?.state).toBe('succeeded')
  expect((await (await request.get('/api/groups/-10010001/menu')).json()).config.rows[0][0].text).toBe('每日集成签到')
  await openPage(page, '抽奖管理')
  await page.getByRole('button', { name: '新建抽奖', exact: true }).click()
  const editor = page.getByRole('dialog', { name: '新建抽奖', exact: true })
  const title = `集成抽奖 ${id}`
  await editor.getByRole('textbox', { name: '活动标题', exact: true }).fill(title)
  await editor.getByRole('textbox', { name: '奖品 1 名称', exact: true }).fill('测试奖品')
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click()
  const detail = page.getByRole('dialog', { name: title, exact: true })
  await detail.getByRole('button', { name: '发布到本群', exact: true }).click()
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect.poll(async () => {
    const lotteries = (await (await request.get('/api/groups/-10010001/lotteries')).json()).items
    return lotteries.find((item: { config: {title: string} }) => item.config.title === title).state
  }).toBe('open')
  expect(errors).toEqual([])
})

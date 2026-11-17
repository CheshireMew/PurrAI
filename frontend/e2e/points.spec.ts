import { expect, test } from '@playwright/test'
import catalog from './fixtures/catalog.json' with { type: 'json' }

test('养成计划可查积分、调整、绑定、导入和修改口令', async ({ page }, testInfo) => {
  let account = { id: 'account1', display_name: '群友甲', user_id: null as number | null, balance: 100, version: 1, rank: 1, source_rank: 1, note: '' }
  let settings = { version: 0, config: { enabled: false, signin_points: 10, chat_points: 1, chat_daily_limit: 50, chat_min_characters: 3, chat_interval_seconds: 60, packet_enabled: true, packet_expiry_hours: 24, commands: { signin: ['签到', '签到加积分'], balance: ['积分'], ranking: ['积分排行'], packet: ['积分红包'] } } }
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const body = request.method() === 'GET' ? null : request.postDataJSON()
    let data: any = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: true, csrf: 'test-csrf' }
    else if (path === '/api/catalog') data = catalog
    else if (path === '/api/groups') data = [{ id: -1001, title: '柴宝养成计划', approved: true, enabled: true, status: 'active', settings: catalog.settings.defaults }]
    else if (path.endsWith('/points/settings')) {
      if (request.method() === 'PUT') { expect(body.config.commands.signin).toContain('每日打卡'); expect(body.config.commands.add).toEqual(['活动加分']); expect(body.config.commands.subtract).toEqual(['活动扣分']); expect(body.config.commands.lookup).toEqual(['查看余额']); settings = { ...body, version: settings.version + 1 } }
      data = settings
    } else if (path.endsWith('/adjust')) { expect(request.headers()['idempotency-key']).toBeTruthy(); account.balance += body.delta; account.version++; data = account }
    else if (path.endsWith('/identity')) { account.user_id = body.user_id; account.display_name = body.display_name; account.version++; data = account }
    else if (path.endsWith('/history')) data = { account, total: 1, items: [{ id: 'entry1', kind: 'import', delta: 100, balance_after: 100, reason: '历史积分迁入', actor_id: 0, created_at: 1794830400, result: {} }] }
    else if (path.endsWith('/import/preview') || path.endsWith('/import')) data = { count: 1, total_points: 50, already_imported: false }
    else if (path.endsWith('/points')) data = { items: [account], total: 1, summary: { accounts: 1, points: account.balance, pending: account.user_id ? 0 : 1 }, imports: [] }
    await route.fulfill({ json: data })
  })
  await page.goto('/#/points?group=-1001')
  await expect(page.getByRole('heading', { name: '积分榜', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '增减', exact: true }).click()
  let dialog = page.getByRole('dialog', { name: '增减积分', exact: true })
  await dialog.getByRole('spinbutton', { name: '变动积分' }).fill('20')
  await dialog.getByRole('textbox', { name: '修改原因' }).fill('活动奖励')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('cell', { name: '120', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '绑定', exact: true }).click()
  dialog = page.getByRole('dialog', { name: '绑定或纠正身份' })
  await dialog.getByRole('textbox', { name: 'Telegram 数字 ID' }).fill('123456')
  await dialog.getByRole('textbox', { name: '修改原因' }).fill('核实本人')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toBeHidden()
  await page.getByRole('button', { name: '流水', exact: true }).click()
  await expect(page.getByRole('cell', { name: '历史积分迁入' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '积分流水' })).toBeHidden()
  await page.locator('summary').click()
  await page.getByRole('textbox', { name: '签到口令' }).fill('每日打卡')
  await page.getByRole('textbox', { name: '管理员加分口令' }).fill('活动加分')
  await page.getByRole('textbox', { name: '管理员扣分口令' }).fill('活动扣分')
  await page.getByRole('textbox', { name: '回复查询口令' }).fill('查看余额')
  await page.getByRole('button', { name: '保存积分设置' }).click()
  await expect(page.getByText('积分设置已保存', { exact: true })).toBeVisible()
  await page.locator('summary').click()
  await page.getByRole('button', { name: '导入历史积分', exact: true }).click()
  await page.getByLabel('积分导入文件').setInputFiles({ name: 'points.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ batch_key: 'batch1', source: 'test', rows: [{ source_key: 'source1', display_name: '新人', points: 50 }] })) })
  dialog = page.getByRole('dialog', { name: '导入历史积分' })
  await expect(dialog.getByText('1 人 · 50 积分')).toBeVisible()
  await dialog.getByRole('button', { name: '确认导入' }).click()
  await expect(dialog).toBeHidden()
  await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: '积分榜', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true })
})

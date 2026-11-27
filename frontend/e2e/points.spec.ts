import { expect, test } from '@playwright/test'
import { openPage } from './navigation'
import catalog from './fixtures/catalog.json' with { type: 'json' }
import pointsSeed from '../../src/purrai/assets/points_policy.v1.json' with { type: 'json' }

test('积分展示实际待结算消息和等待原因，AI额度说明包含广告审核', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  let mode = 'local', pending = 3
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: true, csrf: 'test' }
    else if (path === '/api/catalog') data = catalog
    else if (path === '/api/groups') data = [{ id: -1001, title: '积分等待测试群', approved: true, enabled: true, status: 'active', settings: catalog.settings.defaults }]
    else if (path.endsWith('/points/settings')) data = { config: { ...pointsSeed, enabled: true }, version: 1 }
    else if (path.endsWith('/points')) data = { items: [], total: 0, imports: [], summary: { accounts: 0, points: 0, pending: 0 },
      settlement: { mode, pending_messages: pending, oldest_at: pending ? 1795300000 : null, local_window_hours: 47 } }
    else if (path === '/api/system') data = { chat_budget: { global_calls: 10, global_call_limit: 10, global_tokens: 100, global_token_limit: 100 } }
    else if (path === '/api/system/alerts') data = []
    await route.fulfill({ json: data })
  })
  await page.goto('/#/points?group=-1001')
  const status = page.getByRole('status', { name: '聊天积分结算状态' })
  await expect(status).toContainText('待结算聊天消息 3 条')
  await expect(status).toContainText('47 小时后结算')
  await expect(status).toContainText('签到和人工调整不受此等待影响')
  await page.screenshot({ path: testInfo.outputPath('points-settlement-mobile.png'), fullPage: true })
  mode = 'model'; pending = 0
  await page.reload()
  await expect(status).toContainText('待结算聊天消息 0 条')
  await expect(status).toContainText('模型暂不可用或额度耗尽时继续等待')
  await expect(status).not.toContainText('最早收到于')
  await openPage(page, '系统维护')
  await expect(page.getByText(/达到上限后，AI 聊天暂停，AI 广告审核等待额度恢复/)).toBeVisible()
  expect(errors).toEqual([])
})

test('关闭旧流水并查看另一账户后，迟到的旧响应不能覆盖当前账户', async ({ page }) => {
  const accounts = ['账户甲', '账户乙'].map((name, index) => ({ id: `history-${index}`, display_name: name,
    user_id: 1000 + index, balance: 20, version: 1, rank: index + 1, source_rank: null, note: '',
    trust: { earned_points: 20, active_days: 1, level: 0 } }))
  let release!: () => void
  const oldResponse = new Promise<void>(done => { release = done })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: true, csrf: 'test' }
    else if (path === '/api/catalog') data = catalog
    else if (path === '/api/groups') data = [{ id: -1001, title: '流水测试群', approved: true, enabled: true, status: 'active', settings: catalog.settings.defaults }]
    else if (path.endsWith('/points/settings')) data = { config: { enabled: true, trust_excluded_user_ids: [], commands: {} }, version: 0 }
    else if (path.endsWith('/history')) {
      const old = path.includes('history-0')
      if (old) await oldResponse
      data = { account: accounts[old ? 0 : 1], items: [], total: 0 }
    } else if (path.endsWith('/points')) data = { items: accounts, total: 2, imports: [], summary: { accounts: 2, points: 40, pending: 0 } }
    await route.fulfill({ json: data })
  })
  try {
    await page.goto('/#/points?group=-1001')
    const table = page.locator('.points-table')
    const firstRequest = page.waitForRequest(request => request.url().includes('history-0/history'))
    await table.getByRole('row').filter({ hasText: '账户甲' }).getByRole('button', { name: '流水', exact: true }).click()
    await firstRequest
    await page.keyboard.press('Escape')
    const dialog = page.getByRole('dialog', { name: '积分流水', exact: true })
    await expect(dialog).toBeHidden()
    await table.getByRole('row').filter({ hasText: '账户乙' }).getByRole('button', { name: '流水', exact: true }).click()
    await expect(dialog).toContainText('账户乙')
    const received = page.waitForResponse(response => response.url().includes('history-0/history'))
    release(); await received
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
    await expect(dialog).toContainText('账户乙')
    await expect(dialog).not.toContainText('账户甲')
  } finally { release() }
})

test('积分管理可查积分、调整、绑定、导入和修改口令', async ({ page }, testInfo) => {
  let account = { id: 'account1', display_name: '群友甲', user_id: null as number | null, balance: 100, version: 1, rank: 1, source_rank: 1, note: '', trust: { earned_points: 220, active_days: 8, level: 1 } }
  let settings = { version: 0, config: { enabled: false, signin_points: 10, chat_points: 1, chat_daily_limit: 50, chat_min_characters: 3, chat_interval_seconds: 60, packet_enabled: true, packet_expiry_hours: 24, trust_enabled: true, newcomer_exemption_points: 50, advertising_exemption_points: 100, trust_excluded_user_ids: [] as number[], commands: { signin: ['签到', '签到加积分'], balance: ['积分'], ranking: ['积分排行'], packet: ['积分红包'] } } }
  let otherSettings = structuredClone(settings)
  otherSettings.config.signin_points = 25
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const body = request.method() === 'GET' ? null : request.postDataJSON()
    let data: any = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: true, csrf: 'test-csrf' }
    else if (path === '/api/catalog') data = catalog
    else if (path === '/api/groups') data = [-1001, -1002].map((id, index) => ({ id, title: `积分测试群 ${index + 1}`, approved: true, enabled: true, status: 'active', settings: catalog.settings.defaults }))
    else if (path.startsWith('/api/groups/-1002/points')) {
      if (path.endsWith('/settings')) {
        if (request.method() === 'PUT') otherSettings = { ...body, version: otherSettings.version + 1 }
        data = otherSettings
      } else data = { items: [], total: 0, summary: { accounts: 0, points: 0, pending: 0 }, imports: [] }
    }
    else if (path.endsWith('/points/settings')) {
      if (request.method() === 'PUT') { expect(body.config.commands.signin).toContain('每日打卡'); expect(body.config.commands.add).toEqual(['活动加分']); expect(body.config.commands.subtract).toEqual(['活动扣分']); expect(body.config.commands.lookup).toEqual(['查看余额']); expect(body.config.advertising_exemption_points).toBe(600); expect(body.config.trust_excluded_user_ids).toEqual([123456]); settings = { ...body, version: settings.version + 1 } }
      data = settings
    } else if (path.endsWith('/adjust')) { expect(request.headers()['idempotency-key']).toBeTruthy(); account.balance += body.delta; account.version++; data = account }
    else if (path.endsWith('/identity')) { account.user_id = body.user_id; account.display_name = body.display_name; account.version++; data = account }
    else if (path.endsWith('/history')) data = { account, total: 1, items: [{ id: 'entry1', kind: 'import', delta: 100, balance_after: 100, reason: '历史积分迁入', actor_id: 0, created_at: 1794830400, result: {} }] }
    else if (path.endsWith('/import/preview') || path.endsWith('/import')) data = { count: 1, total_points: 50, already_imported: false }
    else if (path.endsWith('/points')) data = { items: [account], total: 1, summary: { accounts: 1, points: account.balance, pending: account.user_id ? 0 : 1 }, imports: [] }
    await route.fulfill({ json: data })
  })
  await page.goto('/#/members?group=-1001')
  const navigation = page.getByRole('navigation', { name: '管理导航' })
  await expect(navigation.getByRole('button', { name: '养成计划', exact: true })).toHaveCount(0)
  await openPage(page, '积分管理')
  await expect(navigation.getByRole('button', { name: '群互动', exact: true })).toHaveAttribute('aria-current', 'page')
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
  await page.getByRole('spinbutton', { name: 'AI 广告审核豁免积分' }).fill('600')
  await page.getByRole('textbox', { name: '取消自动豁免的成员 ID' }).fill('123456')
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
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
  await expect(page.getByRole('combobox', { name: '选择群', exact: true })).toBeInViewport()
  await page.locator('.group-selector .el-select__wrapper').click()
  await page.getByRole('option', { name: '积分测试群 2', exact: true }).click()
  await expect(page).toHaveURL(/points\?group=-1002/)
  await expect(page.getByRole('tab', { name: '积分管理', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('heading', { name: '积分榜', exact: true })).toBeVisible()
  await expect(page.locator('.points-cards')).not.toContainText('群友甲')
  await page.locator('summary').click()
  await expect(page.getByRole('spinbutton', { name: '每日签到奖励', exact: true })).toHaveValue('25')
  await page.getByRole('spinbutton', { name: '每日签到奖励', exact: true }).fill('26')
  await page.getByRole('button', { name: '保存积分设置', exact: true }).click()
  await expect(page.getByText('积分设置已保存', { exact: true }).last()).toBeVisible()
  expect(otherSettings.config.signin_points).toBe(26)
  expect(settings.config.signin_points).toBe(10)
  await page.locator('.group-selector .el-select__wrapper').click()
  await page.getByRole('option', { name: '积分测试群 1', exact: true }).click()
  await expect(page.locator('.points-cards')).toContainText('群友甲')
  await page.locator('summary').click()
  await expect(page.getByRole('spinbutton', { name: '每日签到奖励', exact: true })).toHaveValue('10')
  await openPage(page, '成员管理')
  await expect(page).toHaveURL(/members\?group=-1001/)
})

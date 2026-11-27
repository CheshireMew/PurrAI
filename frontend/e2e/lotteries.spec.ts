import { expect, test } from '@playwright/test'
import catalog from './fixtures/catalog.json' with { type: 'json' }
import { newLottery } from '../src/lottery'

async function mockApi(page: import('@playwright/test').Page) {
  const rows: any[] = [{ id: 'past', group_id: -1001, state: 'drawn', version: 3, participant_count: 12, draw_count: 12,
    created_at: 1795300000, config: { title: '往期月末福利', description: '感谢大家的日常分享', mode: 'raffle', entry_cost: 100,
      min_balance: 500, active_window_days: 7, min_active_days: 3, draw_mode: 'time', ends_at: 1795400000,
      participant_target: 10, min_participants: 2, daily_limit: 1,
      prizes: [{ name: '会员月卡', kind: 'manual', points: 0, quantity: 2, weight: 1 }] } }]
  const requests: any[] = []
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const body = request.method() === 'GET' ? null : request.postDataJSON()
    let data: any = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: true, csrf: 'test-csrf' }
    else if (path === '/api/catalog') data = catalog
    else if (path === '/api/groups') data = [{ id: -1001, title: '测试群', approved: true, enabled: true, status: 'active', settings: catalog.settings.defaults }]
    else if (path.includes('/lotteries')) {
      const id = path.split('/lotteries/')[1]?.split('/')[0]
      const existing = rows.find(row => row.id === id)
      if (request.method() !== 'GET') {
        expect(request.headers()['idempotency-key']).toBeTruthy()
        requests.push({ path, body })
      }
      if (body?.operation === 'copy') {
        data = { ...structuredClone(existing), id: `copy${rows.length}`, state: 'draft', version: 1, participant_count: 0, draw_count: 0 }
        data.config.ends_at = 0
        rows.unshift(data)
      } else if (body?.operation) {
        expect(body.version).toBe(existing.version)
        existing.version++
        if (body.operation === 'publish') existing.state = 'open'
        if (body.operation === 'draw') existing.state = 'drawn'
        if (body.operation === 'cancel') existing.state = 'cancelled'
        if (body.operation === 'close') existing.state = 'closed'
        data = existing
      } else if (request.method() === 'PUT') {
        expect(body.version).toBe(existing.version)
        Object.assign(existing, { config: body.config, version: existing.version + 1 })
        data = existing
      } else if (request.method() === 'POST') {
        data = { id: `new${rows.length}`, group_id: -1001, config: body.config, state: 'draft', version: 1,
          participant_count: 0, draw_count: 0, created_at: 1795400000 }
        rows.unshift(data)
      } else if (existing) {
        data = { ...existing, remaining: existing.config.prizes.map((p: any) => p.quantity), entries: [], total_entries: 0,
          deliveries: existing.state === 'open' ? [{ id: 'delivery1', state: 'pending', error: '', phase: 'announcement' }] : [] }
      } else data = { items: rows, total: rows.length }
    }
    await route.fulfill({ json: data })
  })
  return requests
}

test('抽奖列表和参与记录翻页失败恢复原页码，重试成功才显示新页', async ({ page }) => {
  await mockApi(page)
  let listFailure = true, detailFailure = true
  const activity = { id: 'paging', group_id: -1001, state: 'open', version: 1, created_at: 1795300000,
    participant_count: 51, draw_count: 0, config: { ...newLottery(), title: '分页活动' } }
  await page.route('**/api/groups/*/lotteries?*', async route => {
    const number = new URL(route.request().url()).searchParams.get('page_number') || '1'
    if (number === '2' && listFailure) return route.fulfill({ status: 503, json: { detail: '活动第二页暂不可用' } })
    await route.fulfill({ json: { items: [{ ...activity, config: { ...activity.config, title: `第${number}页活动` } }], total: 21 } })
  })
  await page.route('**/api/groups/*/lotteries/paging?*', async route => {
    const number = new URL(route.request().url()).searchParams.get('page_number') || '1'
    if (number === '2' && detailFailure) return route.fulfill({ status: 503, json: { detail: '参与记录第二页暂不可用' } })
    await route.fulfill({ json: { ...activity, remaining: [1], total_entries: 51, deliveries: [],
      entries: [{ id: `entry-${number}`, user_id: 42, display_name: `第${number}页成员`, state: 'joined', created_at: 1795300000, prize_label: '', fulfilled: false, cost: 0 }] } })
  })
  await page.goto('/#/lotteries?group=-1001')
  await expect(page.getByRole('heading', { name: '第1页活动', exact: true })).toBeVisible()
  const pagination = page.locator('.el-pagination').first()
  await pagination.locator('.btn-next').click()
  await expect(page.getByText('活动第二页暂不可用', { exact: true })).toBeVisible()
  await expect(pagination.locator('.el-pager .is-active')).toHaveText('1')
  await expect(page.getByRole('heading', { name: '第1页活动', exact: true })).toBeVisible()
  listFailure = false
  await pagination.locator('.btn-next').click()
  await expect(page.getByRole('heading', { name: '第2页活动', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '查看活动', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '分页活动', exact: true })
  await expect(dialog.getByText('第1页成员', { exact: true })).toBeVisible()
  await dialog.locator('.el-pagination .btn-next').click()
  await expect(dialog.getByText('参与记录第二页暂不可用', { exact: true })).toBeVisible()
  await expect(dialog.locator('.el-pager .is-active')).toHaveText('1')
  await expect(dialog.getByText('第1页成员', { exact: true })).toBeVisible()
  detailFailure = false
  await dialog.locator('.el-pagination .btn-next').click()
  await expect(dialog.getByText('第2页成员', { exact: true })).toBeVisible()
})

test('往期抽奖可复用为新草稿，核对时间、参与门槛后发布', async ({ page }, testInfo) => {
  const requests = await mockApi(page)
  await page.goto('/#/lotteries?group=-1001')
  await expect(page.locator('.workspace-tabs > .el-tabs__header').getByRole('tab', { name: '抽奖管理', exact: true })).toHaveAttribute('aria-selected', 'true')
  await page.getByRole('button', { name: '复用配置', exact: true }).click()
  let editor = page.getByRole('dialog', { name: '编辑抽奖草稿' })
  await expect(editor).toBeVisible()
  await expect(editor.getByRole('textbox', { name: '活动标题', exact: true })).toHaveValue('往期月末福利')
  await expect(editor.getByRole('spinbutton', { name: '参与前积分门槛' })).toHaveValue('500')
  await expect(editor.getByRole('spinbutton', { name: '至少活跃多少天' })).toHaveValue('3')
  const dateInput = editor.getByRole('combobox', { name: '结束时间（北京时间）' })
  await expect(dateInput).toHaveValue('')
  const title = '本月群友福利与积分参与规则的完整长标题'.repeat(3)
  await editor.getByRole('textbox', { name: '活动标题', exact: true }).fill(title)
  await dateInput.fill('2027-01-01 20:00')
  await dateInput.press('Enter')
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(editor).toBeHidden()
  const detail = page.getByRole('dialog', { name: title, exact: true })
  await expect(detail.getByText('0 人参与', { exact: false })).toBeVisible()
  await expect(detail.getByText('抽奖草稿已保存', { exact: true })).toBeVisible()
  await expect(page.locator('.el-message')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('lottery-success-with-long-title.png') })
  await detail.getByRole('button', { name: '发布到本群', exact: true }).click()
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect(detail.getByText('进行中', { exact: true })).toBeVisible()
  await expect(detail.getByRole('button', { name: '编辑草稿', exact: true })).toHaveCount(0)
  const saved = requests.find(item => item.path.endsWith('/copy1') && item.body.config)
  expect(saved.body.config.ends_at).toBe(Date.parse('2027-01-01T20:00:00+08:00') / 1000)
  await detail.getByRole('button', { name: '立即开奖', exact: true }).click()
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect(detail.getByText('已开奖', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(detail).toBeHidden()
  await expect(page.locator('.el-overlay:visible')).toHaveCount(0)
  await expect(page.locator('.el-message')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('lotteries-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('lotteries-mobile.png'), fullPage: true })
})

test('可配置即时抽奖奖池、概率和每日上限，也可取消免费报名活动', async ({ page }, testInfo) => {
  const requests = await mockApi(page)
  await page.goto('/#/lotteries?group=-1001')
  await page.getByRole('button', { name: '新建抽奖', exact: true }).click()
  const editor = page.getByRole('dialog', { name: '新建抽奖', exact: true })
  await editor.getByRole('textbox', { name: '活动标题', exact: true }).fill('每日幸运抽奖')
  await editor.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '抽奖方式', exact: true }) }).locator('.el-select__wrapper').click()
  await page.getByRole('option', { name: '消耗积分，立即抽奖' }).click()
  await editor.getByRole('spinbutton', { name: '每次消耗积分', exact: true }).fill('20')
  await editor.getByRole('spinbutton', { name: '每人每天最多抽几次', exact: true }).fill('3')
  await editor.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '奖品 1 类型' }) }).locator('.el-select__wrapper').click()
  await page.getByRole('listbox', { name: '奖品 1 类型', exact: true }).getByRole('option', { name: '自动发积分', exact: true }).click()
  await editor.getByRole('textbox', { name: '奖品 1 名称' }).fill('幸运积分')
  await editor.getByRole('spinbutton', { name: '奖品 1 数量' }).fill('10')
  await editor.getByRole('button', { name: '添加奖品', exact: true }).click()
  await editor.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '奖品 2 类型' }) }).locator('.el-select__wrapper').click()
  await page.getByRole('listbox', { name: '奖品 2 类型', exact: true }).getByRole('option', { name: '未中奖', exact: true }).click()
  await editor.getByRole('spinbutton', { name: '奖品 2 概率权重' }).fill('9')
  await expect(editor.getByText('概率 10.00%', { exact: true })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await editor.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('instant-editor-mobile.png') })
  await editor.getByRole('button', { name: '保存草稿', exact: true }).scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('instant-prizes-mobile.png') })
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(editor).toBeHidden()
  const body = requests.find(item => item.body.config?.title === '每日幸运抽奖').body.config
  expect(body.mode).toBe('instant')
  expect(body.daily_limit).toBe(3)
  expect(body.prizes[1]).toEqual({ name: '未中奖', kind: 'none', points: 0, quantity: 0, weight: 9 })
  const detail = page.getByRole('dialog', { name: '每日幸运抽奖', exact: true })
  await detail.getByRole('button', { name: '发布到本群', exact: true }).click()
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect(detail.getByRole('button', { name: '结束即时抽奖', exact: true })).toBeVisible()
  await detail.getByRole('button', { name: '结束即时抽奖', exact: true }).click()
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect(detail.getByText('已结束', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '新建抽奖', exact: true }).click()
  await editor.getByRole('textbox', { name: '活动标题', exact: true }).fill('免费福利')
  await editor.getByRole('textbox', { name: '奖品 1 名称' }).fill('礼品')
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click()
  const free = page.getByRole('dialog', { name: '免费福利', exact: true })
  await free.getByRole('button', { name: '取消活动并退款', exact: true }).click()
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect(free.getByText('已取消', { exact: true })).toBeVisible()
})

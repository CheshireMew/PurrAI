import { expect, test, type Page } from '@playwright/test'
import catalog from './fixtures/catalog.json' with { type: 'json' }

async function mockMenu(page: Page) {
  const menu = { version: 0, active: false, needs_update: false, delivery: null as any,
    config: { rows: [[{ text: '签到', action: 'signin', style: 'success' },
      { text: '积分', action: 'balance', style: 'danger' }, { text: '抽奖', action: 'lottery', style: 'primary' }]] } }
  const operations: string[] = []
  let failSave = false
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const body = request.method() === 'GET' ? null : request.postDataJSON()
    let data: any = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: true, csrf: 'test-csrf' }
    else if (path === '/api/catalog') data = catalog
    else if (path === '/api/groups') data = [-1001, -1002].map(id => ({ id, title: `菜单测试群 ${id}`, approved: true, enabled: true, status: 'active', settings: catalog.settings.defaults }))
    else if (path.includes('/menu')) {
      if (request.method() !== 'GET') {
        expect(request.headers()['idempotency-key']).toBeTruthy()
        expect(body.version).toBe(menu.version)
      }
      if (request.method() === 'PUT') {
        if (failSave) return route.fulfill({ status: 409, json: { detail: '菜单设置已更新，请刷新后再操作' } })
        menu.config = body.config; menu.version++
      } else if (body?.operation) {
        operations.push(body.operation); menu.version++
        menu.active = body.operation === 'publish'
        menu.delivery = { id: `menu-${menu.version}`, operation: body.operation, state: 'succeeded', error: '' }
      }
      data = menu
    }
    await route.fulfill({ json: data })
  })
  return { menu, operations, failNextSave: () => { failSave = true } }
}

test('底部菜单编辑、预览、更新、清除和恢复', async ({ page }, testInfo) => {
  const state = await mockMenu(page)
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/#/group-menu?group=-1001')
  await expect(page.getByRole('heading', { name: '底部菜单', exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: '第1行第1个按钮文字', exact: true }).fill('每日打卡')
  await page.getByRole('combobox', { name: '第1行第1个按钮颜色', exact: true }).press('Enter')
  await page.getByRole('option', { name: '蓝色', exact: true }).click()
  await page.getByRole('button', { name: '右移第1行第1个按钮', exact: true }).click()
  await page.getByRole('button', { name: '添加一行', exact: true }).click()
  await page.getByRole('textbox', { name: '第2行第1个按钮文字', exact: true }).fill('今日活动')
  await page.getByRole('combobox', { name: '第2行第1个按钮功能', exact: true }).press('Enter')
  await page.getByRole('option', { name: '查看抽奖', exact: true }).click()
  await page.getByRole('button', { name: '上移第2行', exact: true }).click()
  await expect(page.getByRole('button', { name: '更新到群', exact: true })).toBeDisabled()
  const preview = page.getByLabel('底部菜单预览')
  await expect(preview.locator('.preview-row').first()).toHaveText('今日活动')
  await expect(preview.locator('.preview-button.primary').filter({ hasText: '每日打卡' })).toBeVisible()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.getByRole('button', { name: '更新到群', exact: true })).toBeEnabled()
  expect(state.menu.config.rows[1]![1]).toEqual({ text: '每日打卡', action: 'signin', style: 'primary' })
  await page.getByRole('button', { name: '更新到群', exact: true }).click()
  await expect(page.getByText('菜单已显示在群内', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('menu-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('button', { name: '清除群菜单', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  await page.screenshot({ path: testInfo.outputPath('menu-mobile.png'), fullPage: true })
  await page.getByRole('button', { name: '清除群菜单', exact: true }).click()
  await expect(page.getByText('尚未显示菜单或已清除', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '更新到群', exact: true }).click()
  await expect(page.getByText('菜单已显示在群内', { exact: true })).toBeVisible()
  expect(state.operations).toEqual(['publish', 'clear', 'publish'])
  expect(errors).toEqual([])
})

test('保存冲突保留菜单草稿，切群时可继续编辑或放弃', async ({ page }) => {
  const state = await mockMenu(page)
  await page.goto('/#/group-menu?group=-1001')
  const label = page.getByRole('textbox', { name: '第1行第1个按钮文字', exact: true })
  await label.fill('保留我的草稿')
  state.failNextSave()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.getByText('菜单设置已更新，请刷新后再操作', { exact: true })).toBeVisible()
  await expect(label).toHaveValue('保留我的草稿')
  await page.getByRole('button', { name: '刷新状态', exact: true }).click()
  await expect(label).toHaveValue('保留我的草稿')
  await page.getByRole('button', { name: '重新载入配置', exact: true }).click()
  await page.getByRole('button', { name: '放弃修改', exact: true }).click()
  await expect(label).toHaveValue('签到')
  await label.fill('继续保留草稿')
  await page.locator('.workspace-tabs').getByRole('tab', { name: '自动回复', exact: true }).click()
  await page.getByRole('button', { name: '继续编辑', exact: true }).click()
  await expect(label).toHaveValue('继续保留草稿')
  await page.locator('.workspace-tabs').getByRole('tab', { name: '自动回复', exact: true }).click()
  await page.getByRole('button', { name: '放弃修改', exact: true }).click()
  await expect(page.getByRole('heading', { name: '关键词自动回复', exact: true })).toBeVisible()
})

import { expect, test } from '@playwright/test'
import catalog from './fixtures/catalog.json' with { type: 'json' }

test('关键词自动回复可以创建、预览、修改和停用', async ({ page }, testInfo) => {
  const replies: any[] = []
  let version = 0
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const body = request.method() === 'GET' ? null : request.postDataJSON()
    let data: any = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: false, csrf: 'test-csrf' }
    else if (path === '/api/catalog') data = catalog
    else if (path === '/api/groups') data = [{ id: -1001, title: '自动回复测试群', approved: true, enabled: true,
      status: 'active', settings: catalog.settings.defaults }]
    else if (path.endsWith('/auto-replies/preview')) {
      const candidates = body.draft ? [body.draft] : [...replies].sort((a, b) => b.priority - a.priority)
      const text = body.text.normalize('NFKC').trim().toLowerCase()
      const row = candidates.find((item: any) => item.enabled && (item.mode === 'exact'
        ? text === item.keyword.normalize('NFKC').trim().toLowerCase() : text.includes(item.keyword)))
      data = { matched: Boolean(row), rule: row || null }
    } else if (path.includes('/auto-replies')) {
      if (request.method() === 'GET') data = { items: replies, total: replies.length }
      else {
        expect(request.headers()['idempotency-key']).toBeTruthy()
        if (request.method() === 'PUT') expect(request.headers()['if-match']).toBe(replies[0].version)
        data = { ...body, id: 'reply-1', version: String(++version) }
        replies.splice(0, replies.length, data)
      }
    }
    await route.fulfill({ json: data })
  })
  await page.goto('/#/auto-replies?group=-1001')
  await expect(page.getByRole('heading', { name: '关键词自动回复', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '添加自动回复', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '添加自动回复', exact: true })
  await dialog.getByRole('textbox', { name: '自动回复关键词', exact: true }).fill('群规')
  await dialog.getByRole('textbox', { name: '自动回复内容', exact: true }).fill('欢迎来到本群！\n请友善交流，避免刷屏。')
  await dialog.getByRole('textbox', { name: '自动回复草稿测试消息', exact: true }).fill('群规')
  await dialog.getByRole('button', { name: '测试当前草稿', exact: true }).click()
  await expect(dialog.locator('.reply-preview')).toContainText('请友善交流')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('cell', { name: '群规', exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: '自动回复测试消息', exact: true }).fill('群规')
  await page.getByRole('button', { name: '测试已保存规则', exact: true }).click()
  await expect(page.locator('.reply-preview')).toContainText('欢迎来到本群')
  await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true })
  await page.getByRole('button', { name: '编辑', exact: true }).click()
  const editor = page.getByRole('dialog', { name: '编辑自动回复', exact: true })
  await editor.getByRole('textbox', { name: '自动回复关键词', exact: true }).fill('帮助')
  await editor.getByRole('button', { name: '保存', exact: true }).click()
  await expect(editor).toBeHidden()
  await expect(page.getByRole('cell', { name: '帮助', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '停用', exact: true }).click()
  await expect(page.getByRole('button', { name: '启用', exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: '自动回复测试消息', exact: true }).fill('帮助')
  await page.getByRole('button', { name: '测试已保存规则', exact: true }).click()
  await expect(page.getByText('未命中启用的自动回复规则', { exact: true })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: '关键词自动回复', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true })
})

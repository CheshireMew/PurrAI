import { expect, test } from '@playwright/test'
import catalog from './fixtures/catalog.json' with { type: 'json' }

test('广告样本可保存、试匹配、停用，并查看独立审核与设置', async ({ page }, testInfo) => {
  let samples: any[] = []
  let config = { enabled: true, similarity_enabled: true, threshold: 0.9, minimum_length: 14, minimum_sample_coverage: 0.55, sample_action: 'delete', batch_size: 12 }
  let version = 0
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const body = request.method() === 'GET' ? null : request.postDataJSON()
    let data: any = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: true, csrf: 'test-csrf' }
    else if (path === '/api/catalog') data = catalog
    else if (path === '/api/groups') data = [{ id: -1001, title: '广告审核测试群', approved: true, enabled: true, status: 'active', settings: catalog.settings.defaults }]
    else if (path.endsWith('/advertisement-samples/preview')) data = { enabled: true, action: samples[0]?.enabled ? 'delete' : 'review', result: samples[0]?.enabled ? { score: 1, sample: samples[0] } : null }
    else if (path.endsWith('/advertisement-settings')) {
      expect(request.headers()['if-match']).toBe(String(version))
      expect(request.headers()['idempotency-key']).toBeTruthy()
      config = body; version++; data = { settings: config, version }
    } else if (path.endsWith('/advertisement-samples/s1')) {
      expect(request.headers()['if-match']).toBe(String(samples[0].version))
      samples[0] = { ...samples[0], ...body, version: samples[0].version + 1 }; data = samples[0]
    } else if (path.endsWith('/advertisement-samples')) {
      if (request.method() === 'POST') {
        expect(request.headers()['idempotency-key']).toBeTruthy()
        data = { ...body, id: 's1', source: 'manual', version: 1, usable: true, review_state: 'done' }; samples.push(data)
      } else data = { settings: config, version, items: samples, total: samples.length, reviews: {}, model_ready: true }
    } else if (path.endsWith('/advertisement-reviews')) data = { items: [{ id: 'r1', message_id: 15, message: { text: '模型漏答后会继续等待审核' }, result: {}, state: 'pending', error: '模型漏答此消息，等待重试', created_at: 1794880000 }], total: 1 }
    await route.fulfill({ json: data })
  })
  await page.goto('/#/rules?group=-1001')
  await page.getByRole('button', { name: '广告样本与审核', exact: true }).click()
  const panel = page.getByRole('dialog', { name: '广告样本与审核', exact: true })
  await panel.getByRole('button', { name: '添加已确认广告' }).click()
  const editor = page.getByRole('dialog', { name: '添加已确认广告', exact: true })
  await editor.getByRole('textbox', { name: '广告正文', exact: true }).fill('任意可替换的完整测试样本文字不依赖内置词表')
  await editor.getByRole('textbox', { name: '确认或修改原因' }).fill('管理员独立核实')
  await editor.getByRole('button', { name: '保存样本' }).click()
  await expect(editor).toBeHidden()
  await expect(panel.getByRole('cell', { name: '参与本地匹配 正文可独立判断' })).toBeVisible()
  await panel.getByRole('textbox', { name: '样本匹配测试消息' }).fill(samples[0].text)
  await panel.getByRole('button', { name: '测试本地匹配' }).click()
  await expect(panel.getByText('命中样本，相似度 1.000，删除消息')).toBeVisible()
  await panel.getByRole('button', { name: '编辑', exact: true }).click()
  const editing = page.getByRole('dialog', { name: '编辑广告样本' })
  await editing.getByRole('switch', { name: '启用广告样本' }).press('Space')
  await editing.getByRole('button', { name: '保存样本' }).click()
  await expect(editing).toBeHidden()
  await expect(panel.getByRole('cell', { name: '已停用 正文可独立判断' })).toBeVisible()
  await panel.getByRole('button', { name: '测试本地匹配' }).click()
  await expect(panel.getByText('未达到本地处置条件，交由 AI 审核')).toBeVisible()
  await panel.getByRole('tab', { name: '审核记录' }).click()
  await expect(panel.getByText('模型漏答此消息，等待重试')).toBeVisible()
  await panel.getByRole('tab', { name: '审核设置' }).click()
  await panel.getByRole('switch', { name: '样本匹配开关' }).press('Space')
  await panel.getByRole('button', { name: '保存审核设置' }).click()
  await expect(page.getByText('审核设置已保存', { exact: true })).toBeVisible()
  expect(config.similarity_enabled).toBe(false)
  await page.screenshot({ path: testInfo.outputPath('settings.png'), fullPage: true })
})

import { openPage, settingsTab } from './navigation'
import { expect, test, type Page } from '@playwright/test'
import catalog from './fixtures/catalog.json' with { type: 'json' }
import pointsSeed from '../../src/purrai/assets/points_policy.v1.json' with { type: 'json' }

async function setup(page: Page) {
  const state = {
    catalogFailures: 0, passed: false, verificationReads: 0, correction: null as any,
    settings: { config: structuredClone(pointsSeed), version: 1 },
    ad: { enabled: true, similarity_enabled: true, threshold: 0.9, minimum_length: 14, minimum_sample_coverage: 0.55,
      sample_action: 'delete', batch_size: 12, context_messages: 5, context_window_seconds: 900 },
    adVersion: 1, importReads: 0, settingsWrites: [] as any[],
  }
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname
    const body = request.method() === 'GET' ? null : request.postDataJSON()
    let data: any = { items: [], total: 0 }
    if (path === '/api/me') data = { id: 1, root: true, csrf: 'test-csrf' }
    else if (path === '/api/catalog') {
      if (state.catalogFailures-- > 0) return route.fulfill({ status: 503, json: { detail: '管理配置暂时无法加载' } })
      data = catalog
    } else if (path === '/api/groups') data = [-1001, -1002].map((id, index) => ({ id, title: `体验测试群 ${index + 1}`,
      type: 'supergroup', status: 'active', approved: true, enabled: true, settings: catalog.settings.defaults }))
    else if (path === '/api/settings/defaults') data = { settings: catalog.settings.defaults, rules: [] }
    else if (path.endsWith('/settings/inheritance')) data = { defaults: catalog.settings.defaults, settings: catalog.settings.defaults, overrides: [] }
    else if (path.endsWith('/verifications')) {
      state.verificationReads++
      data = { items: [{ id: 'v1', user_id: 42, user_name: '待验证成员', state: state.passed ? 'passed' : 'pending',
        deadline: Date.now() / 1000 + 600, attempts: 0, failure_action: 'permanent_mute', error: '',
        allowed_operations: state.passed ? [] : ['release', 'reissue'] }], total: 1 }
    } else if (path.endsWith('/points/settings')) {
      if (request.method() === 'PUT') { state.settingsWrites.push(body); state.settings = { ...body, version: 2 } }
      data = state.settings
    } else if (path.endsWith('/points/import/preview') || path.endsWith('/points/import')) {
      state.importReads++
      data = { count: body.rows.length, total_points: body.rows.reduce((sum: number, row: any) => sum + row.points, 0), already_imported: false }
    } else if (path.endsWith('/points')) data = { items: [], total: 0, summary: { accounts: 0, points: 0, pending: 0 }, imports: [] }
    else if (path.endsWith('/rules/query')) data = { items: Array.from({ length: 20 }, (_, i) => ({ id: `r${i}`, version: '1',
      kind: 'keyword', value: `任意规则 ${i + 1}`, mode: 'contains', scope: 'message', context_value: '', penalty: 'delete', enabled: true,
      consequence_short: '正文命中只删除消息', consequence: '测试规则完整处置说明：只按保存的配置处理，管理员及相应白名单沿用已有豁免。' })), total: 20 }
    else if (path.endsWith('/advertisement-samples/preview')) data = { action: 'delete', result: { score: 1, sample: { text: body.text } } }
    else if (path.endsWith('/advertisement-samples')) data = { items: [], total: 0, settings: state.ad, version: state.adVersion, model_ready: true }
    else if (path.endsWith('/advertisement-settings')) {
      state.ad = body; state.adVersion++; data = { settings: state.ad, version: state.adVersion }
    } else if (path.endsWith('/chat/diagnostics')) data = { items: Array.from({ length: 20 }, (_, id) => ({ id, message_id: id,
      topic_id: 0, state: 'done', direct: true, attempts: 1, reason: '已回复', created_at: Date.now() / 1000 })), total: 20, usage: [] }
    else if (path.endsWith('/chat')) data = { configured: false, model: '', pending: 0, error: '' }
    else if (path.endsWith('/violations')) data = { items: [{ id: 'v1', user_id: 42, sender_name: '体验成员',
      created_at: Date.now() / 1000, excerpt: '受控复核记录', reasons: [], actions: [], allowed_operations: ['unmute'] }], total: 1 }
    else if (path.endsWith('/violations/v1/correction')) {
      if (request.method() === 'POST') { state.correction = body; data = { action_ids: [] } }
      else data = { rules: [], global_allowed: true, affected_groups: [], user_id: 42, excerpt: '受控复核记录' }
    } else if (path.endsWith('/lotteries') && request.method() === 'POST') {
      return route.fulfill({ status: 422, json: { detail: [
        { loc: ['body', 'config', 'title'], type: 'string_too_short', msg: 'String should have at least 1 character', ctx: { min_length: 1 } },
        { loc: ['body', 'config', 'prizes', 0, 'name'], type: 'string_too_short', msg: 'String should have at least 1 character', ctx: { min_length: 1 } },
      ] } })
    }
    await route.fulfill({ json: data })
  })
  return { state, errors }
}

async function go(page: Page, name: string) {
  await openPage(page, name)
  await expect(page.locator('h1')).toBeVisible()
}
const discardDialog = (page: Page) => page.getByRole('dialog', { name: '保留当前草稿？', exact: true })
async function decideDraft(page: Page, name: '继续编辑' | '放弃修改') {
  const dialog = discardDialog(page)
  await dialog.getByRole('button', { name, exact: true }).click()
  await expect(dialog).toBeHidden()
}

test('归档查询失败保留已确认结果、导出条件和地址，成功后一起更新', async ({ page }, testInfo) => {
  await setup(page)
  let failed = true
  await page.route('**/api/groups/*/messages?*', async route => {
    const query = new URL(route.request().url()).searchParams.get('query') || ''
    if (query && failed) return route.fulfill({ status: 503, json: { detail: '受控查询失败' } })
    await route.fulfill({ json: { items: query ? [] : [{ id: 'm1', message_id: 1, sender_id: 42, sender_name: '归档成员',
      sender_type: 'user', message_type: 'text', text: '已确认的旧结果', sent_at: 1795300000, revision_count: 1,
      original_observed: true, deletion_state: 'not_requested' }], total: query ? 0 : 1, recording: { state: 'active', reason: '' } } })
  })
  await page.goto('/#/messages?group=-1001')
  await expect(page.getByText('已确认的旧结果')).toBeVisible()
  const link = page.getByRole('link', { name: '导出当前查询 JSONL' })
  const previousExport = await link.getAttribute('href'), previousUrl = page.url()
  await page.getByRole('textbox', { name: '归档关键词', exact: true }).fill('不存在的新查询')
  await page.getByRole('button', { name: '查询归档', exact: true }).click()
  await expect(page.getByText('受控查询失败', { exact: true })).toBeVisible()
  await expect(page.getByText('已确认的旧结果')).toBeVisible()
  await expect(link).toHaveAttribute('href', previousExport!)
  expect(page.url()).toBe(previousUrl)
  await expect(page.getByRole('status').filter({ hasText: '仍显示上次成功查询的结果' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('archive-query-failure.png') })
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.getByText('受控查询失败', { exact: true })).toHaveCount(0)
  await expect(page.getByText('已确认的旧结果')).toBeVisible()
  await expect(link).toHaveAttribute('href', previousExport!)
  failed = false
  await page.getByRole('button', { name: '查询归档', exact: true }).click()
  await expect(page.getByText('已确认的旧结果')).toHaveCount(0)
  await expect(link).not.toHaveAttribute('href', previousExport!)
  await expect(page.getByText('查询失败，仍显示上次成功查询的结果；导出也使用上次成功的条件。')).toHaveCount(0)
})

test('列表失败的新筛选不能变成批量处置条件', async ({ page }) => {
  await setup(page)
  let selection: any
  await page.route('**/api/groups/*/verifications?*', async route => {
    if (new URL(route.request().url()).searchParams.has('user_id')) return route.fulfill({ status: 503, json: { detail: '受控筛选失败' } })
    await route.fallback()
  })
  await page.route('**/release-pending/preview', async route => {
    selection = route.request().postDataJSON()
    await route.fulfill({ json: { items: [], total: 0, snapshot: 'test' } })
  })
  await page.goto('/#/verifications?group=-1001')
  await expect(page.getByRole('cell', { name: '待验证成员 42', exact: true })).toBeVisible()
  const previousUrl = page.url()
  await page.getByRole('textbox', { name: '筛选用户 ID', exact: true }).fill('99')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.getByText('受控筛选失败；仍显示上次成功查询的结果')).toBeVisible()
  expect(page.url()).toBe(previousUrl)
  await page.getByRole('button', { name: '预览筛选中的待验证成员', exact: true }).click()
  await expect.poll(() => selection).toEqual({ state: '', after: 0, before: 0 })
  await page.getByRole('dialog', { name: '核对批量放行名单', exact: true }).getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.getByText('受控筛选失败；仍显示上次成功查询的结果')).toHaveCount(0)
  await expect(page.getByRole('cell', { name: '待验证成员 42', exact: true })).toBeVisible()
})

test('成员草稿可继续编辑或放弃，内部限制名称中文显示，断网只显示一处错误', async ({ page }, testInfo) => {
  await setup(page)
  await page.route('**/api/groups/*/members/42', route => route.fulfill({ json: { user_id: 42,
    current: { status: 'restricted' }, current_error: '', checked_at: 1795300000,
    member: { joined_at: 0, join_observed: false, passed_at: 0, owned_restriction: { kind: 'moderation', until_date: 0 } },
    whitelist: null, verifications: { items: [], total: 0 }, violations: { items: [], total: 0 }, actions: { items: [], total: 0 } } }))
  await page.goto('/#/members?group=-1001')
  await page.getByRole('textbox', { name: '成员数字 ID', exact: true }).fill('42')
  await page.getByRole('button', { name: '查询成员', exact: true }).click()
  await expect(page.getByText('PurrAI 记录的限制：管理处置禁言；到期 永久')).toBeVisible()
  const reason = page.getByRole('textbox', { name: '成员处理原因', exact: true })
  await reason.fill('需要保留的完整处理原因')
  await openPage(page, '总览')
  await decideDraft(page, '继续编辑')
  await expect(reason).toHaveValue('需要保留的完整处理原因')
  await page.screenshot({ path: testInfo.outputPath('member-draft-retained.png') })
  await openPage(page, '总览')
  await decideDraft(page, '放弃修改')
  await go(page, '成员管理')
  await page.getByRole('textbox', { name: '成员数字 ID', exact: true }).fill('42')
  await page.getByRole('button', { name: '查询成员', exact: true }).click()
  await expect(reason).toHaveValue('')
  await page.route('**/api/**', route => route.abort('internetdisconnected'))
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.locator('.el-alert--error')).toHaveCount(1)
  await expect(page.locator('.el-alert--error')).toContainText('无法连接后台')
})

test('白名单范围准确，高对比开关有可见滑块，品牌图标可加载', async ({ page }, testInfo) => {
  await setup(page)
  await page.emulateMedia({ forcedColors: 'active' })
  await page.goto('/#/whitelist?group=-1001')
  await page.getByRole('button', { name: '添加成员', exact: true }).click()
  const control = page.getByRole('switch', { name: '免入群验证', exact: true })
  const colors = () => control.locator('..').evaluate(element => {
    const core = element.querySelector('.el-switch__core')!, thumb = element.querySelector('.el-switch__action')!
    return { core: getComputedStyle(core).backgroundColor, thumb: getComputedStyle(thumb).backgroundColor,
      x: thumb.getBoundingClientRect().x }
  })
  await expect(control).toHaveAttribute('aria-checked', 'true')
  const on = await colors()
  expect(on.core).not.toBe(on.thumb)
  await page.screenshot({ path: testInfo.outputPath('forced-colors-on.png') })
  await control.press('Space')
  await expect(control).toHaveAttribute('aria-checked', 'false')
  await expect.poll(async () => (await colors()).x).not.toBe(on.x)
  const off = await colors()
  expect(off.core).not.toBe(off.thumb)
  await page.screenshot({ path: testInfo.outputPath('forced-colors-off.png') })
  const icon = await page.locator('link[rel="icon"]').getAttribute('href')
  const response = await page.request.get(icon!)
  expect(response.status()).toBe(200)
  expect(response.headers()['content-type']).toContain('image/svg+xml')
})

test('积分和抽奖读取失败不显示虚构零值，筛选空态和弹窗错误独立', async ({ page }, testInfo) => {
  await setup(page)
  let failed = true
  await page.route('**/api/groups/*/points?*', async route => {
    if (failed) return route.fulfill({ status: 503, json: { detail: '积分读取失败' } })
    await route.fulfill({ json: { items: [], total: 0, summary: { accounts: 5, points: 250, pending: 1 }, imports: [] } })
  })
  await page.route('**/api/groups/*/lotteries?*', route => route.fulfill({ status: 503, json: { detail: '活动读取失败' } }))
  await page.goto('/#/points?group=-1001')
  await expect(page.locator('.points-summary strong')).toHaveText(['—', '—', '—'])
  await expect(page.getByText('还没有积分账户，可导入历史积分或新增账户')).toHaveCount(0)
  await expect(page.locator('.points-table')).toContainText('积分账户加载失败，请重试')
  failed = false
  await page.getByRole('textbox', { name: '搜索积分账户', exact: true }).fill('不存在的成员')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.locator('.points-table')).toContainText('没有符合条件的积分账户，请调整筛选')
  await expect(page.locator('.points-summary strong')).toHaveText(['250', '5', '1'])
  await page.getByRole('button', { name: '导入历史积分', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '导入历史积分', exact: true })
  await dialog.getByLabel('积分导入文件').setInputFiles({ name: 'invalid.json', mimeType: 'application/json', buffer: Buffer.from('[]') })
  await expect(dialog.locator('.el-alert--error')).toBeVisible()
  await expect(page.locator('.el-alert--error')).toHaveCount(1)
  await page.screenshot({ path: testInfo.outputPath('points-modal-single-error.png') })
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await go(page, '抽奖管理')
  await expect(page.getByText('活动数量待确认')).toBeVisible()
  await expect(page.getByText('抽奖活动加载失败，请重试')).toBeVisible()
  await expect(page.getByText('还没有抽奖活动')).toHaveCount(0)
})

test('自动回复无匹配与从未创建分别显示', async ({ page }) => {
  await setup(page)
  await page.route('**/api/groups/*/auto-replies?*', route => route.fulfill({ json: { items: [], total: 0 } }))
  await page.goto('/#/auto-replies?group=-1001')
  await expect(page.getByText('还没有自动回复，可点击上方添加')).toBeVisible()
  await page.getByRole('textbox', { name: '搜索自动回复', exact: true }).fill('未命中的关键词')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.getByText('没有匹配的自动回复，请调整搜索条件')).toBeVisible()
  await page.getByRole('textbox', { name: '搜索自动回复', exact: true }).fill('')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.getByText('还没有自动回复，可点击上方添加')).toBeVisible()
})

test('低高度发送复核的校验错误与操作按钮都在可见区域', async ({ page }, testInfo) => {
  await setup(page)
  await page.setViewportSize({ width: 720, height: 450 })
  await page.route('**/api/groups/*/actions?*', route => route.fulfill({ json: { items: [{ id: 'a1', kind: 'manual_mute',
    state: 'failed', created_at: 1795300000, actor_id: 1, reason: '本机复核样本', error: '发送结果待核实', can_review_delivery: true }], total: 1 } }))
  await page.goto('/#/operations?group=-1001')
  await page.getByRole('button', { name: '登记核实结果', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '登记发送核实结果', exact: true })
  await dialog.getByRole('button', { name: '登记核实结果', exact: true }).click()
  const alert = dialog.locator('.el-alert--error')
  await expect(alert).toContainText('请选择实际结果并填写核实情况')
  const errorBounds = await alert.boundingBox(), buttonBounds = await dialog.getByRole('button', { name: '登记核实结果', exact: true }).boundingBox()
  expect(errorBounds!.y).toBeGreaterThanOrEqual(0)
  expect(errorBounds!.y + errorBounds!.height).toBeLessThanOrEqual(450)
  expect(buttonBounds!.y + buttonBounds!.height).toBeLessThanOrEqual(450)
  expect(await alert.evaluate(element => !element.closest('.el-dialog__body'))).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('delivery-error-visible.png') })
})

test('后台刷新保留有效勾选，结束验证后移除选择', async ({ page }) => {
  const { state, errors } = await setup(page)
  await page.goto('/#/verifications?group=-1001')
  await page.locator('.el-table__body-wrapper').first().locator('.el-checkbox').first().click()
  await expect(page.getByRole('button', { name: '放行所选 1 人' })).toBeVisible()
  await expect.poll(() => state.verificationReads, { timeout: 9000 }).toBeGreaterThan(1)
  await expect(page.getByRole('button', { name: '放行所选 1 人' })).toBeVisible()
  state.passed = true
  await expect(page.getByRole('button', { name: '放行所选 1 人' })).toBeHidden({ timeout: 9000 })
  await expect(page.getByText('1 位成员已结束验证或不在当前列表中，已从选择中移除')).toBeVisible()
  expect(errors).toEqual([])
})

test('积分草稿保护换页、换群、后退、刷新，取消后仍可保存', async ({ page }) => {
  const { state, errors } = await setup(page)
  await page.goto('/#/overview?group=-1001')
  await go(page, '积分管理')
  await page.locator('summary').click()
  await page.getByRole('spinbutton', { name: '每日签到奖励', exact: true }).fill('31')
  await page.getByRole('spinbutton', { name: '每日签到奖励', exact: true }).fill('10')
  await page.getByRole('spinbutton', { name: '每日签到奖励', exact: true }).press('Tab')
  await go(page, '总览')
  await go(page, '积分管理')
  await page.locator('summary').click()
  await page.getByRole('spinbutton', { name: '每日签到奖励', exact: true }).fill('37')
  await page.getByRole('tab', { name: '抽奖管理', exact: true }).click()
  await decideDraft(page, '继续编辑')
  await expect(page.getByRole('tab', { name: '积分管理', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('spinbutton', { name: '每日签到奖励', exact: true })).toHaveValue('37')
  await openPage(page, '总览')
  await decideDraft(page, '继续编辑')
  await expect(page.getByRole('spinbutton', { name: '每日签到奖励', exact: true })).toHaveValue('37')
  await page.locator('.group-selector .el-select__wrapper').click()
  await page.getByRole('option', { name: '体验测试群 2' }).click()
  await decideDraft(page, '继续编辑')
  await expect(page).toHaveURL(/group=-1001/)
  await page.goBack()
  await decideDraft(page, '继续编辑')
  await expect(page).toHaveURL(/points\?group=-1001/)
  await expect(page.getByRole('spinbutton', { name: '每日签到奖励', exact: true })).toHaveValue('37')
  const nativePrompt = page.waitForEvent('dialog')
  const reloadAttempt = page.evaluate(() => location.reload()).catch(() => {})
  const prompt = await nativePrompt
  expect(prompt.type()).toBe('beforeunload')
  await prompt.dismiss()
  await reloadAttempt
  await expect(page.getByRole('spinbutton', { name: '每日签到奖励', exact: true })).toHaveValue('37')
  await page.getByRole('button', { name: '保存积分设置', exact: true }).click()
  await expect(page.getByText('积分设置已保存', { exact: true })).toBeVisible()
  expect(state.settingsWrites[0].config.signin_points).toBe(37)
  await go(page, '总览')
  expect(errors).toEqual([])
})

test('规则编辑的后退可取消或放弃，前进仍回到原页面', async ({ page }) => {
  await setup(page)
  await page.goto('/#/overview?group=-1001')
  await go(page, '防广告规则')
  await page.getByRole('button', { name: '添加规则', exact: true }).click()
  const editor = page.getByRole('dialog', { name: '广告规则', exact: true })
  await editor.getByPlaceholder('关键词，或 example.com 形式的域名', { exact: true }).fill('未保存的任意规则')
  await page.goBack()
  await decideDraft(page, '继续编辑')
  await expect(editor).toBeVisible()
  await expect(page).toHaveURL(/rules\?group=-1001/)
  await page.goBack()
  await decideDraft(page, '放弃修改')
  await expect(page.getByRole('tab', { name: '基本设置', exact: true })).toHaveAttribute('aria-selected', 'true')
  await page.goForward()
  await expect(page.getByRole('heading', { name: '群设置', level: 1 })).toBeVisible()
})

for (const width of [1280, 390]) {
  test(`抽奖草稿关闭可恢复，保存与中文错误持续可见 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 720 })
    await setup(page)
    await page.goto('/#/lotteries?group=-1001')
    await page.getByRole('button', { name: '新建抽奖' }).click()
    const editor = page.getByRole('dialog', { name: '新建抽奖', exact: true })
    await editor.getByLabel('活动标题', { exact: true }).fill('保留的草稿')
    await page.keyboard.press('Escape')
    await decideDraft(page, '继续编辑')
    await expect(editor.getByLabel('活动标题', { exact: true })).toHaveValue('保留的草稿')
    await editor.getByLabel('活动标题', { exact: true }).fill('')
    await editor.getByRole('button', { name: '保存草稿' }).click()
    await expect(editor.getByText('活动标题：不能为空；奖品 1 的名称：不能为空', { exact: true })).toBeVisible()
    const save = await editor.getByRole('button', { name: '保存草稿' }).boundingBox()
    expect(save!.y + save!.height).toBeLessThanOrEqual(720)
    expect(await editor.locator('.el-dialog').evaluate(e => e.getBoundingClientRect().bottom)).toBeLessThanOrEqual(720)
    await page.screenshot({ path: testInfo.outputPath(`lottery-${width}.png`) })
  })
}

test('广告样本预览随输入失效，设置和样本草稿有关闭保护', async ({ page }) => {
  const { errors, state } = await setup(page)
  await page.goto('/#/rules?group=-1001')
  await page.getByRole('button', { name: '广告样本与审核' }).click()
  const panel = page.getByRole('dialog', { name: '广告样本与审核', exact: true })
  await panel.getByLabel('样本匹配测试消息', { exact: true }).fill('测试正文甲')
  await panel.getByRole('button', { name: '测试本地匹配' }).click()
  await expect(panel.getByText('命中样本，相似度 1.000，删除消息')).toBeVisible()
  await panel.getByLabel('样本匹配测试消息', { exact: true }).fill('测试正文乙')
  await expect(panel.getByText('命中样本，相似度 1.000，删除消息')).toBeHidden()
  await panel.getByRole('button', { name: '添加已确认广告' }).click()
  const sample = page.getByRole('dialog', { name: '添加已确认广告', exact: true })
  await sample.getByLabel('广告内容', { exact: true }).fill('尚未保存的任意样本')
  await sample.getByRole('button', { name: '取消', exact: true }).click()
  await decideDraft(page, '继续编辑')
  await expect(sample.getByLabel('广告内容', { exact: true })).toHaveValue('尚未保存的任意样本')
  await sample.getByRole('button', { name: '取消', exact: true }).click()
  await decideDraft(page, '放弃修改')
  await panel.getByRole('tab', { name: '审核设置' }).click()
  await panel.getByRole('switch', { name: '广告审核开关' }).press('Space')
  await panel.getByRole('button', { name: '关闭', exact: true }).click()
  await decideDraft(page, '继续编辑')
  await expect(panel.getByRole('switch', { name: '广告审核开关' })).toHaveAttribute('aria-checked', 'false')
  await panel.getByRole('button', { name: '保存审核设置' }).click()
  await expect(page.getByText('审核设置已保存', { exact: true })).toBeVisible()
  expect(state.ad.enabled).toBe(false)
  await panel.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(panel).toBeHidden()
  expect(errors).toEqual([])
})

test('启动配置失败保持身份，重试后恢复后台', async ({ page }) => {
  const { state, errors } = await setup(page)
  state.catalogFailures = 1
  await page.goto('/#/rules?group=-1001')
  await expect(page.getByRole('heading', { name: '暂时无法加载管理后台' })).toBeVisible()
  await expect(page.getByRole('link', { name: '使用 Telegram 登录' })).toBeHidden()
  await page.getByRole('button', { name: '重试连接' }).click()
  await expect(page.getByRole('heading', { name: '群设置', level: 1 })).toBeVisible()
  expect(errors).toEqual([])
})

test('换页回顶部，返回恢复滚动和筛选；键盘跳转保留地址', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await setup(page)
  await page.goto('/#/rules?group=-1001')
  await expect(page.locator('.el-table__body-wrapper').first().getByRole('row')).toHaveCount(20)
  await page.evaluate(() => window.scrollTo(0, 700))
  await go(page, '积分管理')
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0)
  await page.goBack()
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(700)
  const before = page.url()
  await page.locator('.skip-link').focus()
  await page.keyboard.press('Enter')
  expect(page.url()).toBe(before)
  await page.reload()
  await expect(page.getByRole('heading', { name: '群设置', level: 1 })).toBeVisible()
})

for (const width of [1280, 390]) {
  test(`继承设置使用中文，当前设置的恢复操作可达 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 })
    await setup(page)
    await page.goto('/#/groups?group=-1001')
    await page.getByRole('button', { name: '编辑设置', exact: true }).first().click()
    const settings = page.getByRole('dialog', { name: '群设置', exact: true })
    await settingsTab(settings, '继承与恢复')
    if (width === 390) {
      const card = settings.locator('.inheritance-cards article').filter({ has: page.locator('strong', { hasText: 'AI广告累计处理' }) })
      await expect(card).toContainText('达到累计次数后永久禁言')
      await card.getByRole('button', { name: '固定为本群值' }).scrollIntoViewIfNeeded()
      const button = await card.getByRole('button', { name: '固定为本群值' }).boundingBox()
      expect(button!.x).toBeGreaterThan(0); expect(button!.x + button!.width).toBeLessThanOrEqual(width)
    } else {
      await expect(settings.locator('.inheritance-table')).toBeVisible()
      const table = await settings.locator('.inheritance-table').boundingBox()
      expect(table!.height).toBeLessThan(600)
      await expect(settings.getByRole('button', { name: '固定为本群值', exact: true }).first()).toBeVisible()
    }
    expect(await settings.locator('.el-dialog').evaluate(e => e.scrollWidth <= e.clientWidth + 1)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`inheritance-${width}.png`) })
  })
}

test('误判纠正明确保留限制，提交仍使用原 API 约定', async ({ page }) => {
  const { state } = await setup(page)
  await page.goto('/#/violations?group=-1001')
  await page.getByRole('button', { name: '纠正误判', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '纠正误判', exact: true })
  await expect(dialog.getByText('保留当前限制，仅登记误判和处理规则', { exact: true })).toBeVisible()
  await expect(dialog.getByRole('status')).toContainText('保留当前成员限制，不解除禁言或封禁')
  await dialog.getByRole('textbox', { name: '核实原因', exact: true }).fill('核实记录，暂不解除限制')
  await dialog.getByRole('button', { name: '确认纠正' }).click()
  await expect(dialog).toBeHidden()
  expect(state.correction.action).toBe('')
})

test('积分导入可下载模板，错误指向结构和记录，预览后可确认', async ({ page }) => {
  const { state, errors } = await setup(page)
  await page.goto('/#/points?group=-1001')
  await page.getByRole('button', { name: '导入历史积分', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '导入历史积分', exact: true })
  const download = page.waitForEvent('download')
  await dialog.getByRole('link', { name: '下载 JSON 模板' }).click()
  expect((await download).suggestedFilename()).toBe('points-import-template.json')
  await dialog.getByLabel('积分导入文件').setInputFiles({ name: 'wrong.json', mimeType: 'application/json', buffer: Buffer.from('[]') })
  await expect(dialog.getByText(/文件最外层应包含批次编号/)).toBeVisible()
  expect(state.importReads).toBe(0)
  await dialog.getByLabel('积分导入文件').setInputFiles({ name: 'missing.json', mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify({ batch_key: 'test', source: 'test', rows: [{ source_key: '1', points: 50 }] })) })
  await expect(dialog.getByText('第 1 条记录缺少昵称（display_name），请对照模板补全')).toBeVisible()
  await dialog.getByLabel('积分导入文件').setInputFiles({ name: 'valid.json', mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify({ batch_key: 'test', source: 'test', rows: [{ source_key: '1', display_name: '任意成员', points: 50 }] })) })
  await expect(dialog.getByText('1 人 · 50 积分')).toBeVisible()
  await dialog.getByRole('button', { name: '确认导入' }).click()
  await expect(dialog).toBeHidden()
  expect(state.importReads).toBe(2)
  expect(errors).toEqual([])
})

test('规则列表说明按需展开，测试和业务入口位于列表前', async ({ page }) => {
  await setup(page)
  await page.goto('/#/rules?group=-1001')
  const first = page.locator('.rule-consequence').first()
  await expect(first.locator('p')).toBeHidden()
  await first.locator('summary').click()
  await expect(first.locator('p')).toContainText('测试规则完整处置说明')
  await first.locator('summary').click()
  const business = await page.getByRole('button', { name: '管理业务规则', exact: true }).boundingBox()
  expect(business!.y).toBeLessThan(300)
  await page.getByRole('button', { name: '测试消息', exact: true }).click()
  await expect(page.getByLabel('规则测试消息', { exact: true })).toBeFocused()
})

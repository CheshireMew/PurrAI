import { expect, test, type Page } from '@playwright/test'
import catalog from './fixtures/catalog.json' with { type: 'json' }
import businessSeed from '../../src/purrai/assets/business_rules.v1.json' with { type: 'json' }

const settings = { verification_failure_action: 'permanent_mute', archive_enabled: true, verification_enabled: true, verification_timeout: 180, max_attempts: 3, failure_ban_seconds: 600,
  newcomer_seconds: 259200, newcomer_forwards: true, newcomer_wallets: true, newcomer_links: true, newcomer_contacts: true, repeat_enabled: true,
  repeat_window: 30, repeat_count: 5, warning_window: 86400, warning_limit: 3, mute_seconds: 259200, block_invites: false,
  join_requests: false, newcomer_messages: 3, newcomer_mentions: true, similar_enabled: true, similar_window: 60,
  similar_count: 4, similar_threshold: 85, similar_min_length: 12, burst_enabled: true, burst_window: 10, burst_count: 8 }
const groups = [{ id: -1001, title: 'PurrAI 开发讨论', type: 'supergroup', approved: true, status: 'active', enabled: true, settings },
  { id: -1002, title: '猫咪摄影交流', type: 'supergroup', approved: true, status: 'pending', enabled: false, settings }]
const archivedMessage = { id: 'm1', group_id: -1001, message_id: 88, sender_id: 42, sender_name: '样本成员', username: 'sample',
  sender_type: 'user', message_type: 'text', text: '被删除的广告原文', caption: '', revision_count: 2, original_observed: true,
  source: 'telegram', sent_at: 1792368000, deletion_state: 'deleted', deletion_error: '',
  violation: { reasons: ['关键词：广告'] }, revisions: [
    { id: 1, is_edit: false, event_at: 1792368000, reasons: [], payload: { text: '编辑前的正常原文' } },
    { id: 2, is_edit: true, event_at: 1792368010, reasons: ['关键词：广告'], payload: { text: '被删除的广告原文', entities: [{ type: 'text_link', url: 'https://hidden.example' }] } } ] }
async function mock(page: Page, authenticated = true, root = true) {
  const rules: any[] = []
  let shared: any = { settings: structuredClone(catalog.settings.defaults), rules: [] }
  const packLists = new Map(groups.map(group => [group.id, ['bygbb666', 'gshechaba', 'tgvip_16']]))
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), method = route.request().method()
    let data: any = {}, status = 200
    if (url.pathname === '/api/me') { data = authenticated ? { id: 123456, root, csrf: 'test' } : { detail: '请先登录' }; status = authenticated ? 200 : 401 }
    else if (url.pathname === '/api/settings/defaults') {
      if (method === 'PATCH') { const body = route.request().postDataJSON(); shared = { settings: { ...shared.settings, ...body.settings }, rules: body.rules ?? shared.rules } }
      data = shared
    }
    else if (url.pathname === '/api/catalog') data = catalog
    else if (url.pathname === '/api/groups') data = groups.map(group => ({ ...group, settings: { ...group.settings, emoji_status_blocked_packs: packLists.get(group.id) } }))
    else if (url.pathname.endsWith('/emoji-status-packs')) {
      const id = Number(url.pathname.split('/')[3]), body = route.request().postDataJSON()
      const current = packLists.get(id) || []
      const requested = (body.packs || []).map((pack: string) => pack.split('/').pop()!.toLowerCase())
      const packs = body.operation === 'clear' ? [] : body.operation === 'remove' ? current.filter(pack => !requested.includes(pack)) : [...new Set([...current, ...requested])]
      packLists.set(id, packs)
      data = { packs, added: packs.filter(pack => !current.includes(pack)), removed: current.filter(pack => !packs.includes(pack)) }
    }
    else if (url.pathname.endsWith('/chat')) data = { configured: true, model: 'example-chat', pending: 0, error: '',
      stickers: { ready: 21, total: 21, emoji_count: 214, unavailable: [], error: '' } }
    else if (url.pathname.endsWith('/chat/diagnostics')) data = {items: [], total: 0, usage: [], decisions: []}
    else if (url.pathname.endsWith('/emergency')) data = {active: false}
    else if (url.pathname === '/api/system/failures') data = {items: [], total: 0}
    else if (url.pathname === '/api/system/alerts') data = []
    else if (url.pathname.endsWith('/release-pending/preview')) data = {items: [{id: 'v1', user_id: 42, user_name: '新成员', state: 'pending'}], total: 1, snapshot: 'mock-snapshot'}
    else if (url.pathname === '/api/dashboard') data = { groups: 2, active_groups: 1, pending: 3, passed_today: 28, failed_today: 2, violations_today: 16, failed_actions: 1 }
    else if (url.pathname.endsWith('/rules/preview')) {
      const body = route.request().postDataJSON()
      data = body.contact ? { reasons: ['分享联系人，删除消息并永久封禁'], kick_member: true, ban_member: true, mute_member: false, delete_message: true }
        : [...(body.rules || []), ...(body.draft ? [body.draft] : [])].some((r: any) => r.penalty === 'permanent_mute')
        ? { reasons: ['去除干扰关键词：微信代收日入上万'], mute_member: true, delete_message: true }
        : [...(body.rules || []), ...(body.draft ? [body.draft] : [])].some((r: any) => r.mode === 'pinyin' && r.scope === 'name')
        ? { reasons: ['昵称谐音关键词：同城'], mute_member: true, delete_message: true }
        : { reasons: ['关键词：广告'] }
    }
    else if (url.pathname.endsWith('/rules/query')) data = { items: rules, total: rules.length, page: 1, size: 20 }
    else if (url.pathname.endsWith('/settings/inheritance')) data = { defaults: catalog.settings.defaults, settings: catalog.settings.defaults, overrides: [] }
    else if (url.pathname.endsWith('/rules')) {
      if (method === 'POST') { const body = route.request().postDataJSON(); data = { ...body, id: 'r1',
        consequence: body.penalty === 'permanent_mute' ? '正文命中删除消息并永久禁言' : '正文命中只删除消息' }; rules.push(data) } else data = rules
    }
    else if (url.pathname.endsWith('/actions') && method === 'GET') data = { items: [], total: 0 }
    else if (url.pathname.endsWith('/verifications')) data = { items: [{ id: 'v1', user_id: 42, user_name: '新成员', state: 'pending', allowed_operations: ['release', 'reissue', 'ban'], attempts: 0, deadline: Date.now() / 1000 + 180 }], total: 1 }
    else if (url.pathname.endsWith('/actions')) data = { id: 'a1', state: 'pending', reason: '人工核实' }
    else if (url.pathname.startsWith('/api/actions/')) data = { id: 'a1', state: 'succeeded', reason: '人工核实' }
    else if (url.pathname.endsWith('/messages/export')) {
      await route.fulfill({ body: JSON.stringify(archivedMessage) + '\n', contentType: 'application/x-ndjson',
        headers: { 'Content-Disposition': 'attachment; filename="purrai-messages.jsonl"' } }); return
    }
    else if (url.pathname.endsWith('/messages/m1')) data = archivedMessage
    else if (url.pathname.endsWith('/messages')) data = { items: [archivedMessage], total: 1, recording: { state: 'active', reason: '' } }
    else if (url.pathname.endsWith('/violations')) data = { items: [], total: 0 }
    else if (url.pathname.endsWith('/whitelist')) data = []
    else if (url.pathname === '/api/admins') data = { items: [], superadmin_ids: [123456] }
    else if (url.pathname === '/api/system') data = { connected: true, bot_configured: true, login_configured: true, pending_actions: 2, failed_actions: [], failed_events: [] }
    await route.fulfill({ status, json: data })
  })
}

for (const width of [1440, 390]) {
  test(`业务组合规则可编辑预览保存并重新打开 ${width}`, async ({ page }, testInfo) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.setViewportSize({ width, height: 1000 })
    await mock(page)
    let saved: any = { document: structuredClone(businessSeed), version: 'business-v1' }
    let writes = 0, previews = 0
    await page.route('**/api/settings/business-policy**', async route => {
      const request = route.request()
      if (request.url().endsWith('/preview')) {
        const body = request.postDataJSON()
        expect(body.document.stages.find((stage: any) => stage.id === 'advertising').rules.at(-1).condition.pattern).toBe('琥珀雪桥')
        expect(body.text).toBe('琥珀雪桥')
        previews++
        return route.fulfill({ json: { changed: true,
          before: { reasons: [], name_reasons: [], chat_reasons: [], delete_message: false, mute_member: false, ban_member: false },
          after: { reasons: ['任意规则命中'], name_reasons: [], chat_reasons: [], delete_message: true, mute_member: true, ban_member: false },
        } })
      }
      if (request.method() === 'PUT') {
        expect(request.headers()['if-match']).toBe(saved.version)
        const body = request.postDataJSON()
        expect(body.request_id).toBeTruthy()
        saved = { document: body.document, version: `business-v${++writes + 1}` }
      }
      await route.fulfill({ json: saved })
    })
    await page.goto('/')
    if (width < 700) await page.getByRole('button', { name: '展开导航', exact: true }).click()
    await page.getByRole('button', { name: '防广告规则', exact: true }).click()
    await page.getByRole('button', { name: '管理业务规则', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '业务组合规则', exact: true })
    await dialog.getByRole('button', { name: '添加组合规则', exact: true }).click()
    await dialog.getByLabel('业务规则名称', { exact: true }).fill('任意词组合')
    await dialog.getByRole('tabpanel', { name: '识别规则', exact: true }).getByLabel('匹配表达式', { exact: true }).fill('琥珀雪桥')
    await test.step('选择规则处置', async () => {
      await dialog.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '业务规则处置', exact: true }) }).locator('.el-select__wrapper').click()
      await page.getByRole('option', { name: '删除并永久禁言', exact: true }).click()
    })
    await dialog.getByLabel('业务规则测试正文', { exact: true }).fill('琥珀雪桥')
    await dialog.getByRole('button', { name: '比较规则草稿', exact: true }).click()
    await expect(dialog.getByText('删除并永久禁言；允许进入聊天', { exact: true })).toBeVisible()
    expect(previews).toBe(1)
    await dialog.getByRole('button', { name: '保存业务规则', exact: true }).click()
    await expect(dialog.getByRole('button', { name: '保存业务规则', exact: true })).toBeDisabled()
    expect(writes).toBe(1)
    const added = saved.document.stages.find((stage: any) => stage.id === 'advertising').rules.at(-1)
    expect(added.action).toBe('mute'); expect(added.condition.pattern).toBe('琥珀雪桥')
    await dialog.getByRole('tab', { name: '公共条件与豁免', exact: true }).click()
    await expect(dialog.getByLabel('公共条件名称', { exact: true })).toBeVisible()
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy()
    await dialog.getByRole('tab', { name: '文本处理', exact: true }).click()
    await expect(dialog.getByLabel('字符 風 转换为', { exact: true })).toHaveValue('风')
    await dialog.getByRole('tab', { name: '识别规则', exact: true }).click()
    await dialog.locator('.rule-list').getByRole('button', { name: /任意词组合.*删除并永久禁言/ }).click()
    await expect(dialog.getByLabel('业务规则名称', { exact: true })).toHaveValue('任意词组合')
    await page.screenshot({ path: testInfo.outputPath(`business-policy-${width}.png`), fullPage: true })
    await dialog.getByRole('button', { name: '关闭', exact: true }).click()
    await page.getByRole('button', { name: '管理业务规则', exact: true }).click()
    await dialog.locator('.rule-list').getByRole('button', { name: /任意词组合.*删除并永久禁言/ }).click()
    await expect(dialog.getByRole('tabpanel', { name: '识别规则', exact: true }).getByLabel('匹配表达式', { exact: true })).toHaveValue('琥珀雪桥')
    await dialog.locator('.el-switch').filter({ has: page.getByRole('switch', { name: '启用业务规则', exact: true }) }).locator('.el-switch__core').click()
    await dialog.getByRole('button', { name: '保存业务规则', exact: true }).click()
    await expect(dialog.getByRole('button', { name: '保存业务规则', exact: true })).toBeDisabled()
    expect(saved.document.stages.find((stage: any) => stage.id === 'advertising').rules.at(-1).enabled).toBe(false)
    expect(errors).toEqual([])
  })
}

async function whitelistFixture(page: Page) {
  await mock(page)
  const state = { readStatus: 200, deleteStatus: 200, saveStatus: 200, reads: 0,
    items: [{ user_id: 42, verification: true, moderation: true, note: '白名单样例' }] }
  await page.route(/\/api\/groups\/-1001\/whitelist(?:\?.*)?$/, route => {
    if (route.request().method() === 'PUT') return route.fulfill({ status: state.saveStatus,
      json: state.saveStatus === 200 ? route.request().postDataJSON() : { detail: '保存白名单失败，请重试' } })
    state.reads++
    return route.fulfill({ status: state.readStatus,
      json: state.readStatus === 200 ? state.items : { detail: '读取白名单失败，请重试' } })
  })
  await page.route('**/api/groups/-1001/whitelist/42', route => {
    if (state.deleteStatus === 200) state.items = []
    return route.fulfill({ status: state.deleteStatus,
      json: state.deleteStatus === 200 ? { ok: true } : { detail: '移出白名单失败，请重试' } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '白名单', exact: true }).click()
  await expect(page.getByText('白名单样例', { exact: true })).toBeVisible()
  return state
}

test('白名单移出失败的原因在刷新后继续显示', async ({ page }) => {
  const state = await whitelistFixture(page)
  state.deleteStatus = 503
  await page.getByRole('button', { name: '移出', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '移出', exact: true }).click()
  const error = page.locator('.error-banner').filter({ hasText: '移出白名单失败，请重试' })
  await expect(error).toBeVisible()
  const previousReads = state.reads
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect.poll(() => state.reads).toBeGreaterThan(previousReads)
  await expect(error).toBeVisible()
  await expect(page.getByText('白名单样例', { exact: true })).toBeVisible()
  await error.locator('.el-alert__close-btn').click()
  await expect(error).toHaveCount(0)
})

test('白名单成功移出后更新列表并保留完成反馈', async ({ page }) => {
  const state = await whitelistFixture(page)
  await page.getByRole('button', { name: '移出', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '移出', exact: true }).click()
  await expect(page.getByText('白名单样例', { exact: true })).toHaveCount(0)
  await expect(page.getByText('已移出白名单', { exact: true })).toBeVisible()
  state.readStatus = 503
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.locator('.error-banner')).toContainText('读取白名单失败，请重试')
  await expect(page.getByText('已移出白名单', { exact: true })).toBeVisible()
})

test('白名单保存失败保留草稿和原因', async ({ page }) => {
  const state = await whitelistFixture(page)
  state.saveStatus = 503
  await page.getByRole('button', { name: '编辑', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('备注', { exact: true }).fill('保留这份备注')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('保存白名单失败，请重试')
  await expect(dialog.getByLabel('备注', { exact: true })).toHaveValue('保留这份备注')
})

test('状态表情说明与实际处罚和默认名单一致', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '状态表情', exact: true }).click()
  await expect(dialog.getByText('入群时命中会永久封禁', { exact: false })).toBeVisible()
  await expect(dialog.getByText('群内发言命中会删除对应消息并永久禁言，成员留在群内', { exact: false })).toBeVisible()
  const defaults = dialog.getByText('程序初始名单', { exact: false })
  for (const pack of catalog.settings.defaults.emoji_status_blocked_packs) await expect(defaults).toContainText(pack)
})

test('初始状态表情名单说明读取配置目录而非群名单', async ({ page }) => {
  await mock(page)
  const metadata = structuredClone(catalog)
  metadata.settings.defaults.emoji_status_blocked_packs = ['audit_initial_pack']
  await page.route('**/api/catalog', route => route.fulfill({ json: metadata }))
  await page.route('**/api/groups', route => route.fulfill({ json: [{ ...groups[0],
    settings: { ...metadata.settings.defaults, emoji_status_blocked_packs: ['group_only_pack'] } }] }))
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '状态表情', exact: true }).click()
  const defaults = dialog.getByText('程序初始名单', { exact: false })
  await expect(defaults).toContainText('audit_initial_pack')
  await expect(defaults).not.toContainText('group_only_pack')
  await expect(dialog.getByRole('button', { name: '移除 group_only_pack', exact: true })).toBeVisible()
})

test('系统重试失败的原因在自动刷新后继续显示', async ({ page }) => {
  await mock(page)
  await page.clock.install()
  let reads = 0
  await page.route('**/api/system', route => {
    reads++
    return route.fulfill({ json: { connected: true, failed_events: [], failed_actions: [
      { id: 'uncertain', kind: 'archive_export', state: 'failed', error: '发送结果未确认' },
    ] } })
  })
  await page.route('**/api/actions/uncertain/retry', route => route.fulfill({
    status: 409, json: { detail: '发送结果未确认，请先核实 Telegram 中的结果' },
  }))
  await page.route('**/api/system/failures?**', route => route.fulfill({json: {
    items: new URL(route.request().url()).searchParams.has('event') ? [] : [{id: 'uncertain', kind: 'archive_export', state: 'failed', error: '发送结果未确认'}], total: 1,
  }}))
  await page.goto('/')
  await page.getByRole('button', { name: '系统状态', exact: true }).click()
  await page.getByRole('button', { name: '重试', exact: true }).click()
  const banner = page.locator('.error-banner').filter({ hasText: '发送结果未确认，请先核实 Telegram 中的结果' })
  await expect(banner).toBeVisible()
  await page.clock.fastForward(6000)
  await expect.poll(() => reads).toBeGreaterThan(1)
  await expect(banner).toBeVisible()
})

test('超级管理员可按数字 ID 接入私密群，列表立即更新', async ({ page }, testInfo) => {
  await mock(page)
  const admitted = [...groups], submissions: any[] = []
  await page.route('**/api/groups', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON(); submissions.push(body)
      const group = { ...groups[1], id: body.group_id, title: '新接入的私密群' }
      admitted.push(group); return route.fulfill({ json: group })
    }
    return route.fulfill({ json: admitted })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '接入群组', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('群 ID').fill('https://t.me/+test')
  await dialog.getByRole('button', { name: '确认接入' }).click()
  await expect(dialog).toContainText('请填写群的负数数字 ID')
  expect(submissions).toHaveLength(0)
  await dialog.getByLabel('群 ID').fill('-1003792975859')
  await dialog.getByRole('button', { name: '确认接入' }).click()
  await expect(dialog).not.toBeVisible()
  await expect(page.getByRole('table').getByText('新接入的私密群', { exact: true })).toBeVisible()
  expect(submissions).toEqual([{ group_id: -1003792975859 }])
  await page.screenshot({ path: testInfo.outputPath('group-admitted.png'), fullPage: true })
})

test('接入群失败时保留输入并在弹窗内说明原因', async ({ page }) => {
  await mock(page)
  await page.route('**/api/groups', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 422, json: { detail: '无法访问此群，请核对群 ID 并先将机器人加入群' } })
    : route.fallback())
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '接入群组', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('群 ID').fill('-1003792975859')
  await dialog.getByRole('button', { name: '确认接入' }).click()
  await expect(dialog).toContainText('无法访问此群')
  await expect(dialog.getByLabel('群 ID')).toHaveValue('-1003792975859')
})

test('普通群管理员没有接入任意群的入口', async ({ page }) => {
  await mock(page, true, false)
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await expect(page.getByRole('button', { name: '接入群组', exact: true })).toHaveCount(0)
})

test('重出题提交回执丢失后刷新页面仍确认原请求，不重复出题', async ({ page }) => {
  await mock(page)
  await page.clock.install()
  let completed = false
  await page.route('**/api/actions/only-one-challenge', route => route.fulfill({ json: {
    id: 'only-one-challenge', state: completed ? 'succeeded' : 'pending', reason: '核实后重新出题',
  } }))
  const requests: any[] = [], effects = new Map<string, string>()
  await page.route('**/api/verifications/v1/actions', async route => {
    const body = route.request().postDataJSON(); requests.push(body)
    if (!effects.has(body.request_id)) {
      effects.set(body.request_id, 'only-one-challenge')
      return route.abort('failed')
    }
    return route.fulfill({ json: { id: effects.get(body.request_id), state: 'pending', reason: body.reason } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '入群验证', exact: true }).click()
  await page.getByRole('button', { name: '重出题', exact: true }).click()
  await page.getByRole('dialog').getByRole('textbox').fill('核实后重新出题')
  await page.getByRole('button', { name: '提交操作', exact: true }).click()
  await expect(page.locator('.action-results')).toContainText('提交结果暂未确认')
  await page.reload()
  await expect(page.locator('.action-results')).toContainText('核实后重新出题')
  await page.getByRole('button', { name: '确认提交结果', exact: true }).click()
  await expect(page.locator('.action-results')).toContainText('等待 Telegram 执行确认')
  expect(requests).toHaveLength(2)
  expect(requests[0]).toEqual(requests[1]); expect(effects.size).toBe(1)
  await expect(page.getByRole('button', { name: '确认提交结果', exact: true })).toHaveCount(0)
  completed = true
  await page.clock.fastForward(1000)
  await expect(page.locator('.action-results')).toContainText('已完成')
})

for (const [state, label] of [['active', '正在记录新消息'], ['disabled', '已停止记录新消息'], ['capacity_paused', '容量不足，已暂停记录']]) {
  test(`群管理员看到真实归档状态 ${state}`, async ({ page }, testInfo) => {
    await mock(page, true, false)
    await page.route('**/api/groups/*/messages?*', route => route.fulfill({ json: {
      items: [], total: 0, recording: { state, reason: state === 'capacity_paused' ? '数据库达到归档容量上限，已暂停新增归档；已有记录保留' : '' },
    } }))
    await page.goto('/')
    await page.getByRole('button', { name: '消息归档', exact: true }).click()
    await expect(page.getByText(label, { exact: true })).toBeVisible()
    if (state === 'capacity_paused') {
      await expect(page.getByText('数据库达到归档容量上限，已暂停新增归档；已有记录保留')).toBeVisible()
      await page.screenshot({ path: testInfo.outputPath('archive-capacity-paused.png'), fullPage: true })
    }
  })
}

test('顶部刷新归档保留已应用查询和分页并拒绝过期响应', async ({ page }) => {
  await mock(page)
  let refreshes = 0, hold = false
  let release!: () => void
  const oldResponse = new Promise<void>(resolve => { release = resolve })
  const requests: URL[] = []
  await page.route('**/api/groups/*/messages?*', async route => {
    const url = new URL(route.request().url()); requests.push(url)
    const current = hold ? ++refreshes : 0
    if (current === 1) await oldResponse
    await route.fulfill({ json: { items: [{ ...archivedMessage, text: current === 1 ? '过期归档结果' : current > 1 ? '新到达归档消息' : '初次归档结果' }], total: 45, recording: { state: 'active', reason: '' } } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '消息归档', exact: true }).click()
  await page.getByLabel('归档关键词').fill('已应用关键词')
  await page.getByRole('button', { name: '查询归档', exact: true }).click()
  await page.locator('.el-pagination .number').filter({ hasText: /^2$/ }).click()
  await expect.poll(() => requests.at(-1)?.searchParams.get('page_number')).toBe('2')
  await page.getByLabel('归档关键词').fill('尚未应用的草稿')
  hold = true
  try {
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect.poll(() => refreshes).toBe(1)
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect(page.getByText('新到达归档消息', { exact: true })).toBeVisible()
    const response = page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/messages'))
    release(); await response
    await expect(page.getByText('过期归档结果', { exact: true })).toHaveCount(0)
    await expect(page.getByText('新到达归档消息', { exact: true })).toBeVisible()
    expect(requests.at(-1)!.searchParams.get('query')).toBe('已应用关键词')
    expect(requests.at(-1)!.searchParams.get('page_number')).toBe('2')
    await expect(page.getByLabel('归档关键词')).toHaveValue('尚未应用的草稿')
  } finally { release() }
})

test.describe('浏览器在其他时区时仍按北京时间筛选', () => {
  test.use({ timezoneId: 'America/Los_Angeles' })
  for (const [section, title] of [['verifications', '入群验证'], ['violations', '违规记录']]) {
    test(`${title}查询使用固定北京时间`, async ({ page }) => {
      await mock(page); await page.goto('/')
      await page.getByRole('button', { name: title, exact: true }).click()
      await page.getByPlaceholder('开始（北京时间）').fill('2026-10-21 00:00:00')
      await page.getByPlaceholder('结束（北京时间）').fill('2026-10-22 00:00:00')
      await page.getByPlaceholder('结束（北京时间）').press('Enter')
      const sent = page.waitForRequest(r => new URL(r.url()).pathname.endsWith(`/${section}`)
        && new URL(r.url()).searchParams.has('after'))
      await page.getByRole('button', { name: '查询', exact: true }).click()
      const query = new URL((await sent).url()).searchParams
      expect(Number(query.get('after'))).toBe(Date.parse('2026-10-21T00:00:00+08:00') / 1000)
      expect(Number(query.get('before'))).toBe(Date.parse('2026-10-22T00:00:00+08:00') / 1000)
    })
  }
})

for (const lateFailure of [false, true]) {
  test(`归档旧详情${lateFailure ? '失败' : '成功'}不会覆盖后来打开的消息`, async ({ page }) => {
    await mock(page)
    const a = { ...archivedMessage, id: 'm1', message_id: 101, text: '较早打开的消息' }
    const b = { ...archivedMessage, id: 'm2', message_id: 102, text: '后来打开的消息' }
    await page.route('**/api/groups/-1001/messages?*', route => route.fulfill({ json: { items: [a, b], total: 2 } }))
    let release!: () => void, entered!: () => void
    const held = new Promise<void>(resolve => { release = resolve })
    const started = new Promise<void>(resolve => { entered = resolve })
    await page.route('**/api/groups/-1001/messages/m1', async route => {
      entered(); await held
      await route.fulfill(lateFailure ? { status: 500, json: { detail: '旧消息请求失败' } } : { json: a })
    })
    await page.route('**/api/groups/-1001/messages/m2', route => route.fulfill({ json: b }))
    try {
      await page.goto('/')
      await page.getByRole('button', { name: '消息归档', exact: true }).click()
      await page.getByRole('row').filter({ hasText: '较早打开的消息' }).getByRole('button', { name: '查看详情' }).click()
      await started
      await page.locator('.el-drawer__close-btn').click()
      await page.getByRole('row').filter({ hasText: '后来打开的消息' }).getByRole('button', { name: '查看详情' }).click()
      await expect(page.locator('.archive-detail')).toContainText('消息 102')
      const late = page.waitForResponse(r => r.url().endsWith('/messages/m1'))
      release()
      await (await late).finished()
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
      await expect(page.locator('.archive-detail')).toContainText('消息 102')
      await expect(page.locator('.archive-detail')).not.toContainText('消息 101')
      await expect(page.locator('.archive-detail')).not.toContainText('旧消息请求失败')
      await expect(page.locator('.archive-detail .el-loading-mask')).toHaveCount(0)
    } finally { release() }
  })
}

test('批量放行 125 人只查询批次，查询失败仍保留回执并恢复进度', async ({ page }) => {
  await mock(page)
  let progressRequests = 0, individualRequests = 0
  await page.route('**/api/groups/-1001/release-pending', route => route.fulfill({ json: {
    batch_id: 'large-batch', action_ids: Array.from({ length: 125 }, (_, i) => `child-${i}`),
  } }))
  await page.route('**/api/groups/-1001/release-pending/preview', route => route.fulfill({json: {
    items: Array.from({length: 125}, (_, index) => ({id: `v${index}`, user_id: index + 1, user_name: `成员 ${index + 1}`, state: 'pending'})), total: 125, snapshot: 'reviewed-125',
  }}))
  await page.route('**/api/actions/*', async route => {
    if (!route.request().url().endsWith('/large-batch')) {
      individualRequests++
      await route.fulfill({ status: 429, json: { detail: '不应逐条查询' } })
      return
    }
    progressRequests++
    if (progressRequests === 1) {
      await route.fulfill({ status: 429, json: { detail: '暂时限流' } })
      return
    }
    await route.fulfill({ json: { id: 'large-batch', kind: 'batch_release', state: 'failed',
      reason: '人工核实批量放行', error: '1 个任务失败：Telegram 权限不足',
      summary: { total: 125, pending: 0, succeeded: 123, failed: 1, cancelled: 1 } } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '入群验证', exact: true }).click()
  await page.getByRole('button', { name: '批量放行', exact: true }).click()
  await page.getByRole('dialog').getByRole('textbox').fill('人工核实批量放行')
  await page.getByRole('button', { name: '确认放行名单中的成员', exact: true }).click()
  const result = page.locator('.action-results')
  await expect(result.locator('.action-line')).toHaveCount(1)
  await expect(result).toContainText('共 125 项')
  await expect(result).toContainText('任务已提交，结果暂时无法刷新', { timeout: 10000 })
  await expect(result).toContainText('成功 123，等待 0，失败 1，取消 1', { timeout: 10000 })
  await expect(result).toContainText('Telegram 权限不足')
  expect(individualRequests).toBe(0)
  expect(progressRequests).toBe(2)
})

test('登录页解释授权范围', async ({ page }) => {
  await mock(page, false); await page.goto('/')
  await expect(page.getByRole('link', { name: '使用 Telegram 登录' })).toBeVisible()
  await expect(page.getByText('仅限群管理员和超级管理员。群权限按 Telegram 当前身份确认。')).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('login.png'), fullPage: true, animations: 'disabled' })
})

for (const width of [1440, 1024, 390]) {
  test(`${width}px 下所有后台页面可访问且内容不撑开页面`, async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.setViewportSize({ width, height: 900 })
    await mock(page); await page.goto('/')
    for (const name of ['总览', '群管理', '入群验证', '防广告规则', '违规记录', '消息归档', '白名单', '权限说明', '系统状态']) {
      const navigation = page.getByRole('navigation', { name: '管理导航' })
      await navigation.getByRole('button', { name, exact: true }).click()
      await expect(navigation.getByRole('button', { name, exact: true })).toHaveAttribute('aria-current', 'page')
      await expect(page.getByRole('heading', { name, level: 1, exact: true })).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
      if (name === '消息归档') await expect(page.getByRole('button', { name: '查看详情', exact: true })).toBeInViewport()
      if (name === '入群验证') await expect(page.getByRole('button', { name: '放行', exact: true })).toBeInViewport()
    }
    await page.getByRole('button', { name: '群管理', exact: true }).click()
    await page.getByRole('button', { name: '群设置', exact: true }).first().click()
    const dialog = page.getByRole('dialog', { name: '群设置' })
    for (const name of ['入群验证', '防广告', '群聊天', '状态表情', '消息归档']) {
      await dialog.getByRole('tab', { name, exact: true }).click()
      await expect(dialog.getByRole('tabpanel')).toBeVisible()
      const save = await dialog.getByRole('button', { name: '保存', exact: true }).boundingBox()
      expect(save).not.toBeNull()
      expect(save!.y + save!.height).toBeLessThanOrEqual(900)
    }
    expect(errors).toEqual([])
  })
}

test('手机可展开全部导航，选择页面后收起，键盘可跳到正文', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mock(page); await page.goto('/')
  const toggle = page.getByRole('button', { name: '展开导航', exact: true })
  await toggle.click()
  await expect(page.getByRole('button', { name: '收起导航' })).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('button', { name: '系统状态', exact: true })).toBeInViewport()
  await page.getByRole('button', { name: '系统状态', exact: true }).click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await page.goto('/')
  await expect(page.locator('.workspace')).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: '跳到主要内容' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.locator('#main-content')).toBeFocused()
})

test('群设置跨分页保留修改，保存失败后仍可继续编辑重试', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '群设置' })
  await dialog.getByLabel('最多错误次数', { exact: true }).fill('4')
  await dialog.getByRole('tab', { name: '群聊天', exact: true }).click()
  await dialog.getByLabel('聊天昵称', { exact: true }).fill('小橘')
  await dialog.getByRole('tab', { name: '消息归档', exact: true }).click()
  await dialog.getByRole('switch', { name: '记录群消息归档', exact: true }).press('Space')
  await dialog.getByRole('tab', { name: '入群验证', exact: true }).click()
  await expect(dialog.getByLabel('最多错误次数', { exact: true })).toHaveValue('4')
  let attempts = 0
  const submissions: any[] = []
  await page.route('**/api/groups/-1001/settings', route => {
    submissions.push(route.request().postDataJSON())
    return route.fulfill(++attempts === 1 ? { status: 409, json: { detail: '设置已更新，请核对后重试' } } : { json: {} })
  })
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('设置已更新，请核对后重试')
  await dialog.getByRole('tab', { name: '群聊天', exact: true }).click()
  await expect(dialog.getByLabel('聊天昵称', { exact: true })).toHaveValue('小橘')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  expect(submissions).toHaveLength(2)
  expect(submissions[1].settings).toMatchObject({ max_attempts: 4, chat_name: '小橘', archive_enabled: false })
})

test('筛选输入框回车提交一次查询并保留关键词', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '消息归档', exact: true }).click()
  await expect(page.getByRole('cell', { name: '被删除的广告原文' })).toBeVisible()
  let queries = 0
  await page.route('**/api/groups/-1001/messages?*', route => {
    if (new URL(route.request().url()).searchParams.get('query') === '广告') queries++
    return route.fulfill({ json: { items: [], total: 0 } })
  })
  await page.getByLabel('归档关键词').fill('广告')
  await page.getByLabel('归档关键词').press('Enter')
  await expect(page.getByText('暂无匹配的归档消息', { exact: true })).toBeVisible()
  expect(queries).toBe(1)
  await expect(page.getByLabel('归档关键词')).toHaveValue('广告')
})
test('总览、群设置和规则保存均可操作', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message))
  await mock(page); await page.goto('/')
  await expect(page.getByText('24 小时验证通过')).toBeVisible()
  await expect(page.locator('.group-summary').first().locator('.el-tag')).toHaveCSS('opacity', '1')
  await page.screenshot({ path: test.info().outputPath('overview-desktop.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await page.getByRole('button', { name: '防广告规则', exact: true }).click()
  await page.getByRole('button', { name: '添加规则' }).click()
  await page.getByPlaceholder('关键词，或 example.com 形式的域名').fill('广告')
  await page.getByPlaceholder('输入消息，测试这次修改后的规则').fill('广告测试')
  await page.getByRole('button', { name: '预览当前草稿' }).click()
  await expect(page.getByRole('dialog').getByText('关键词：广告', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('cell', { name: '广告', exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: '规则测试消息' }).fill('广告文案')
  await page.getByRole('button', { name: '测试规则' }).click()
  await expect(page.getByText('关键词：广告', { exact: true })).toBeVisible()
  expect(errors).toEqual([])
})
test('人工放行先显示处理中，再显示执行完成', async ({ page }) => {
  await mock(page); await page.clock.install()
  let completed = false, reads = 0
  await page.route('**/api/actions/a1', route => {
    reads++
    return route.fulfill({ json: { id: 'a1', state: completed ? 'succeeded' : 'pending', reason: '人工核实' } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '入群验证', exact: true }).click()
  await page.getByRole('button', { name: '放行', exact: true }).click()
  await page.getByRole('dialog').getByRole('textbox').fill('人工核实')
  await page.getByRole('button', { name: '提交操作' }).click()
  await expect(page.getByText('本次提交的操作')).toBeVisible()
  await expect(page.getByText('等待 Telegram 执行确认')).toBeVisible()
  await expect.poll(() => reads).toBe(1)
  completed = true
  await page.clock.fastForward(1000)
  await expect(page.locator('.action-results').getByText('已完成')).toBeVisible({ timeout: 10000 })
})

test('多个独立操作定时合并刷新，完成后停止查询', async ({ page }) => {
  await mock(page); await page.clock.install()
  let submissions = 0, singles = 0, batches = 0, completed = false
  await page.route('**/api/verifications/v1/actions', route => {
    submissions++
    return route.fulfill({ json: { id: `task-${submissions}`, state: 'pending', reason: '人工核实' } })
  })
  await page.route('**/api/actions/task-*', route => {
    singles++
    return route.fulfill({ json: { id: new URL(route.request().url()).pathname.split('/').pop(),
      state: completed ? 'succeeded' : 'pending', reason: '人工核实' } })
  })
  await page.route('**/api/actions?*', route => {
    batches++
    const ids = new URL(route.request().url()).searchParams.getAll('identifiers')
    return route.fulfill({ json: { items: ids.map(id => ({ id, state: completed ? 'succeeded' : 'pending', reason: '人工核实' })), errors: {} } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '入群验证', exact: true }).click()
  for (let i = 0; i < 3; i++) {
    await page.getByRole('button', { name: '重出题', exact: true }).click()
    await page.getByRole('dialog').getByRole('textbox').fill('人工核实')
    await page.getByRole('button', { name: '提交操作', exact: true }).click()
    await expect(page.getByRole('dialog')).not.toBeVisible()
    await expect(page.locator('.action-results .action-line')).toHaveCount(i + 1)
  }
  await expect.poll(() => batches).toBeGreaterThanOrEqual(2)
  const previousBatches = batches, previousSingles = singles
  completed = true
  await page.clock.fastForward(1000)
  await expect(page.locator('.action-results').getByText('已完成', { exact: true })).toHaveCount(3)
  expect(batches).toBeGreaterThan(previousBatches)
  expect(singles).toBe(previousSingles)
  const finishedBatches = batches
  await page.clock.fastForward(10000)
  expect(batches).toBe(finishedBatches)
  expect(submissions).toBe(3)
})

test('群聊天口令计数可查看并能修改门槛、例外和开关', async ({ page }) => {
  await mock(page)
  await page.route('**/api/groups/-1001/chat', route => route.fulfill({ json: {
    configured: true, model: 'example-chat', pending: 0, error: '',
    commands: { enabled: true, threshold: 5, minimum_users: 3, window_days: 7,
      items: [{ text: '每日任务', count: 6, users: 4, ignored: true }, { text: '领取奖励', count: 2, users: 2, ignored: false }] },
  } }))
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '群聊天', exact: true }).click()
  await expect(dialog.getByRole('cell', { name: '每日任务', exact: true })).toBeVisible()
  await expect(dialog.getByRole('cell', { name: '已忽略', exact: true })).toBeVisible()
  await expect(dialog.getByRole('cell', { name: '观察中', exact: true })).toBeVisible()
  await dialog.locator('.el-form-item').filter({ hasText: '7 天内识别次数' }).getByRole('spinbutton').fill('8')
  await dialog.getByLabel('口令识别例外', { exact: true }).fill('每日任务')
  await dialog.locator('.el-form-item').filter({ hasText: '自动识别重复口令' }).locator('.el-switch').click()
  const saved = page.waitForRequest(r => r.url().endsWith('/settings') && r.method() === 'PATCH')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  const body = (await saved).postDataJSON().settings
  expect(body.chat_command_threshold).toBe(8)
  expect(body.chat_command_learning).toBe(false)
  expect(body.chat_command_exceptions).toEqual(['每日任务'])
})

test('群聊天可配置自然参与、昵称和性格并重新开始上下文', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '群聊天', exact: true }).click()
  await expect(dialog.getByText('模型：example-chat', { exact: false })).toBeVisible()
  await expect(dialog.getByText('聊天 emoji 标签：214 种', { exact: false })).toBeVisible()
  await dialog.getByLabel('聊天昵称', { exact: true }).fill('小橘')
  await dialog.getByLabel('性格与说话习惯', { exact: true }).fill('随和，喜欢电影，聊天简短自然。')
  await expect(dialog.getByLabel('聊天忽略口令', { exact: true })).toHaveValue('签到\n积分\n抽奖\n注册币安\n注册 OKX\n币圈教程')
  await dialog.getByLabel('聊天忽略口令', { exact: true }).fill('签到\n查余额\n注册 OKX')
  await dialog.getByLabel('聊天贴纸包', { exact: true }).fill('https://t.me/addstickers/myadestes\nHMS_Cheshire')
  await dialog.getByLabel('防诈贴纸包', { exact: true }).fill('FTX888')
  await expect(dialog.locator('.el-form-item').filter({ hasText: '聊天贴纸最短间隔（秒）' }).getByRole('spinbutton')).toHaveValue('60')
  await dialog.locator('.el-form-item').filter({ hasText: '聊天贴纸最短间隔（秒）' }).getByRole('spinbutton').fill('30')
  await dialog.locator('.el-form-item').filter({ hasText: '开启群聊天' }).locator('.el-switch').click()
  const reset = page.waitForRequest(r => r.url().endsWith('/chat/reset') && r.method() === 'POST')
  await dialog.getByRole('button', { name: '重新开始聊天上下文' }).click()
  await reset
  const saved = page.waitForRequest(r => r.url().endsWith('/settings') && r.method() === 'PATCH')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  const body = (await saved).postDataJSON().settings
  expect(body.chat_enabled).toBe(true)
  expect(body).not.toHaveProperty('chat_proactive')
  expect(body.chat_name).toBe('小橘')
  expect(body.chat_persona).toContain('喜欢电影')
  expect(body.chat_ignored_texts).toEqual(['签到', '查余额', '注册 OKX'])
  expect(body).not.toHaveProperty('chat_history')
  expect(body).not.toHaveProperty('chat_stickers')
  expect(body.chat_sticker_interval).toBe(30)
  expect(body.chat_sticker_packs).toEqual(['https://t.me/addstickers/myadestes', 'HMS_Cheshire'])
  expect(catalog.settings.defaults.chat_antifraud_packs).toEqual(['FTX888'])
  expect(body).not.toHaveProperty('chat_antifraud_packs') // unchanged defaults are omitted from the patch
  expect(body).not.toHaveProperty('verification_enabled')
})
test('窄屏无页面横向溢出并可切换管理页面', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await mock(page); await page.goto('/')
  await expect(page.getByText('保护正常的群')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  await page.screenshot({ path: test.info().outputPath('overview-mobile.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '入群验证', exact: true }).click()
  await expect(page.getByRole('heading', { name: '验证记录' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
})

test('本群管理员可以启用保护，界面不显示全局维护入口', async ({ page }) => {
  await mock(page, true, false); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await expect(page.getByRole('button', { name: '启用保护', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '系统状态', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '权限说明', exact: true })).toHaveCount(0)
  const request = page.waitForRequest(r => r.url().endsWith('/api/groups/-1002/activate') && r.method() === 'POST')
  await page.getByRole('button', { name: '启用保护', exact: true }).click()
  await request
})

test('昵称谐音规则可预览并按昵称范围保存', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '防广告规则', exact: true }).click()
  await page.getByRole('button', { name: '添加规则' }).click()
  const dialog = page.getByRole('dialog')
  await page.getByPlaceholder('关键词，或 example.com 形式的域名').fill('同城')
  await dialog.getByText('谐音匹配', { exact: true }).click()
  await dialog.locator('.el-form-item').filter({ hasText: '检查范围' }).locator('.el-select__wrapper').click()
  await page.getByRole('option', { name: '昵称及用户名（删消息并永久禁言）', exact: true }).click()
  await dialog.getByPlaceholder('例如：显示昵称 @username').fill('童城')
  await dialog.getByPlaceholder('输入消息，测试这次修改后的规则').fill('大家好')
  await dialog.getByRole('button', { name: '预览当前草稿' }).click()
  await expect(dialog.getByText(/将禁言成员/)).toBeVisible()
  await expect(dialog.getByText(/将删除消息/)).toBeVisible()
  const saved = page.waitForRequest(r => r.url().endsWith('/rules') && r.method() === 'POST')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  const body = (await saved).postDataJSON()
  expect(body.mode).toBe('pinyin'); expect(body.scope).toBe('name')
})

test('消息归档可筛选、查看编辑版本并导出，窄屏无横向溢出', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '消息归档', exact: true }).click()
  await expect(page.getByRole('cell', { name: '被删除的广告原文' })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'PurrAI 已确认删除' })).toBeVisible()
  await page.getByRole('textbox', { name: '归档关键词' }).fill('广告')
  await page.getByText('只看命中规则', { exact: true }).click()
  const query = page.waitForRequest(r => r.url().includes('/messages?') && r.url().includes('flagged=true'))
  await page.getByRole('button', { name: '查询归档' }).click()
  expect((await query).url()).toContain('query=')
  await page.getByRole('button', { name: '查看详情' }).click()
  await expect(page.getByText('编辑前的正常原文', { exact: true })).toBeVisible()
  await expect(page.locator('.archive-detail').getByText('被删除的广告原文', { exact: true })).toBeVisible()
  await page.locator('.el-drawer__close-btn').click()
  const download = page.waitForEvent('download')
  await page.getByRole('link', { name: '导出当前查询 JSONL' }).click()
  expect((await download).suggestedFilename()).toBe('purrai-messages.jsonl')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('button', { name: '查询归档' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  await page.screenshot({ path: test.info().outputPath('archive-mobile.png'), fullPage: true, animations: 'disabled' })
})

test('群设置提供永久禁言与踢出两个验证失败选项并保存所选值', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  const selection = dialog.locator('.el-form-item').filter({ hasText: '已入群成员验证失败处理' }).locator('.el-select__wrapper')
  await expect(selection).toContainText('永久禁言（留在群里）')
  await selection.click()
  await expect(page.getByRole('option', { name: '永久禁言（留在群里）', exact: true })).toBeVisible()
  await page.getByRole('option', { name: '踢出群聊（允许重新加入）', exact: true }).click()
  const saved = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/settings'))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await saved).postDataJSON().settings.verification_failure_action).toBe('kick')
})

test('联系人规则对所有普通成员生效并显示踢出结果', async ({ page }) => {
  await mock(page)
  await page.route('**/api/groups/*/violations*', route => route.fulfill({ json: { items: [{
    id: 'contact-1', allowed_operations: ['unban'], permanent_ban: true, user_id: 42, message_id: 10, sender_name: '发送者', excerpt: '',
    created_at: Date.now() / 1000, reasons: ['分享联系人，删除消息并永久封禁'],
    actions: [{ id: 'contact-kick-1', kind: 'contact_ban', state: 'succeeded', error: '' }],
  }], total: 1 } }))
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '防广告', exact: true }).click()
  await expect(dialog.getByText('开启“只拦截广告联系人名片”时，普通名片可以分享，广告名片和黑名单联系方式会删除消息并永久封禁；关闭时，普通成员分享任何联系人都会封禁。群主、管理员及防广告白名单豁免。')).toBeVisible()
  await expect(dialog.locator('.el-form-item').filter({ hasText: '限制期禁止联系人' })).toHaveCount(0)
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '防广告规则', exact: true }).click()
  await page.getByText('包含联系人', { exact: true }).click()
  await page.getByRole('button', { name: '测试规则', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('将踢出并禁止重新加入')
  await expect(page.getByRole('alert')).not.toContainText('将禁言成员')
  await page.getByRole('button', { name: '违规记录', exact: true }).click()
  await expect(page.getByText('分享联系人永久封禁：已完成')).toBeVisible()
  await expect(page.getByRole('button', { name: '解封禁', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '解禁言', exact: true })).toHaveCount(0)
})

test('外群封禁设置可保存允许链接并在违规详情提供解封', async ({ page }) => {
  await mock(page)
  await page.route('**/api/groups/*/violations*', route => route.fulfill({ json: { items: [{
    id: 'ad-1', allowed_operations: ['unban'], permanent_ban: true, user_id: 42, message_id: 10, sender_name: '广告发送者', excerpt: '来内部群学习',
    created_at: Date.now() / 1000, reasons: ['明确广告引流：招揽加入交易群或策略频道'],
    actions: [{ id: 'ad-ban-1', kind: 'advertisement_ban', state: 'succeeded', error: '' }],
  }], total: 1 } }))
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '防广告', exact: true }).click()
  await expect(dialog.locator('.el-form-item').filter({ hasText: catalog.settings.booleans.external_group_ban }).getByRole('switch')).toHaveAttribute('aria-checked', 'true')
  await dialog.getByPlaceholder('每行一个群组、频道或邀请链接').fill('https://t.me/+AllowedInvite123\nhttps://t.me/partner_group')
  const sent = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/settings'))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await sent).postDataJSON().settings.telegram_allowed_links).toEqual(['https://t.me/+AllowedInvite123', 'https://t.me/partner_group'])
  await page.getByRole('button', { name: '违规记录', exact: true }).click()
  await expect(page.getByText('广告或外群引流永久封禁：已完成')).toBeVisible()
  await expect(page.getByRole('button', { name: '解封禁', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '解禁言', exact: true })).toHaveCount(0)
})

test('正文关键词可保存删消息并永久禁言并在预览显示', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '防广告规则', exact: true }).click()
  await page.getByRole('button', { name: '添加规则', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByPlaceholder('关键词，或 example.com 形式的域名').fill('微信代收日入上万')
  await dialog.locator('.el-form-item').filter({ hasText: '正文命中处理' }).locator('.el-select__wrapper').click()
  await page.getByRole('option', { name: '删消息并永久禁言', exact: true }).click()
  await dialog.getByPlaceholder('输入消息，测试这次修改后的规则').fill('微信代收日入上万')
  await dialog.getByRole('button', { name: '预览当前草稿', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('将禁言成员')
  await expect(dialog.getByRole('alert')).toContainText('将删除消息')
  const saved = page.waitForRequest(r => r.method() === 'POST' && r.url().endsWith('/rules'))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await saved).postDataJSON().penalty).toBe('permanent_mute')
  await expect(page.getByText('正文命中删除消息并永久禁言', { exact: true })).toBeVisible()
})

test('发图文字门槛默认三条并可以逐群关闭', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '防广告', exact: true }).click()
  const field = dialog.locator('.el-form-item').filter({ hasText: '发图及表情前独立文字条数' }).getByRole('spinbutton')
  await expect(dialog.getByText('发图门槛仅限有可靠入群记录的新成员', { exact: false })).toBeVisible()
  await expect(dialog.getByText('或入群满 15 天即解除', { exact: false })).toBeVisible()
  await expect(field).toHaveValue('3')
  await field.fill('0'); await field.press('Tab')
  const saved = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/settings'))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await saved).postDataJSON().settings.media_min_text_messages).toBe(0)
})

test('AI广告处理默认开启并可按群关闭', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '群聊天', exact: true }).click()
  const toggle = dialog.getByRole('switch', { name: 'AI广告识别与累计处罚', exact: true })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await toggle.press('Space')
  const saved = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/settings'))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await saved).postDataJSON().settings.chat_advertiser_cooldown).toBe(false)
})

test('钱包地址拦截显示全员范围并可逐群关闭', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '防广告', exact: true }).click()
  const wallet = dialog.getByRole('switch', { name: '全员拦截钱包及代币合约地址', exact: true })
  await expect(wallet).toHaveAttribute('aria-checked', 'true')
  await expect(dialog.getByText('钱包及代币合约地址拦截对所有普通成员生效', { exact: false })).toBeVisible()
  await wallet.press('Space')
  await expect(wallet).toHaveAttribute('aria-checked', 'false')
  const saved = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/settings'))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await saved).postDataJSON().settings.newcomer_wallets).toBe(false)
})

test('设置默认值和范围直接使用后端配置目录', async ({ page }) => {
  await mock(page)
  const metadata = structuredClone(catalog)
  metadata.settings.defaults.media_min_text_messages = 6
  metadata.settings.fields.media_min_text_messages.minimum = 2
  metadata.settings.fields.media_min_text_messages.maximum = 9
  await page.route('**/api/catalog', route => route.fulfill({ json: metadata }))
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  await page.getByRole('dialog').getByRole('tab', { name: '防广告', exact: true }).click()
  const field = page.getByRole('dialog').getByLabel('发图及表情前独立文字条数（0 关闭）', { exact: true })
  await expect(field).toHaveValue('6')
  await expect(field).toHaveAttribute('min', '2')
  await expect(field).toHaveAttribute('max', '9')
})

test('状态表情包黑名单默认包含指定包且可独立清空', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '状态表情', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '移除 bygbb666', exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '移除 gshechaba', exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '清空状态表情包黑名单', exact: true }).click()
  await page.getByRole('button', { name: '取消', exact: true }).last().click()
  await expect(dialog.getByRole('button', { name: '移除 bygbb666', exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '清空状态表情包黑名单', exact: true }).click()
  const saved = page.waitForRequest(r => r.method() === 'POST' && r.url().endsWith('/emoji-status-packs'))
  await page.getByRole('button', { name: '确认清空', exact: true }).click()
  expect((await saved).postDataJSON().operation).toBe('clear')
  await expect(dialog.getByText('当前名单为空', { exact: true })).toBeVisible()
})

test('状态表情包可以只新增一个链接并去重、单独移除，保留未保存设置', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('tab', { name: '防广告', exact: true }).click()
  const repeats = dialog.getByLabel('重复消息触发条数', { exact: true })
  await repeats.fill('7'); await repeats.press('Tab')
  await dialog.getByRole('tab', { name: '状态表情', exact: true }).click()
  const input = dialog.getByLabel('新增状态表情包', { exact: true })
  await input.fill('https://t.me/addemoji/NewPack')
  const added = page.waitForRequest(r => r.url().endsWith('/emoji-status-packs'))
  await dialog.getByRole('button', { name: '新增表情包', exact: true }).click()
  expect((await added).postDataJSON().packs).toEqual(['https://t.me/addemoji/NewPack'])
  await expect(dialog.getByRole('button', { name: '移除 newpack', exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '移除 bygbb666', exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '移除 gshechaba', exact: true })).toBeVisible()
  await dialog.getByRole('tab', { name: '防广告', exact: true }).click()
  await expect(repeats).toHaveValue('7')
  await dialog.getByRole('tab', { name: '状态表情', exact: true }).click()
  await input.fill('NEWPACK')
  await dialog.getByRole('button', { name: '新增表情包', exact: true }).click()
  await expect(page.getByText('已在名单中，无需重复添加', { exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '移除 newpack', exact: true })).toHaveCount(1)
  await dialog.getByRole('button', { name: '移除 newpack', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '移除 newpack', exact: true })).toHaveCount(0)
  const saved = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/settings'))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  const body = (await saved).postDataJSON()
  expect(body.settings.repeat_count).toBe(7)
  expect(body.settings).not.toHaveProperty('emoji_status_blocked_packs')
})


test('验证和违规页面只展示后端提供的操作', async ({ page }) => {
  await mock(page)
  await page.route('**/api/groups/*/verifications*', route => route.fulfill({ json: { items: [{
    id: 'choices', user_id: 42, user_name: '成员', state: 'passed', failure_action: '',
    allowed_operations: ['reissue'], attempts: 0, deadline: 0,
  }], total: 1 } }))
  await page.route('**/api/groups/*/violations*', route => route.fulfill({ json: { items: [{
    id: 'choices', user_id: 42, reasons: [], actions: [], created_at: 0,
    allowed_operations: ['unmute'], profile_violation: false, warning_active: false,
  }], total: 1 } }))
  await page.goto('/')
  await page.getByRole('button', { name: '入群验证', exact: true }).click()
  await expect(page.getByRole('button', { name: '重出题', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '放行', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '封禁', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '违规记录', exact: true }).click()
  await expect(page.getByRole('button', { name: '解禁言', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '解封禁', exact: true })).toHaveCount(0)
})

for (const target of ['list', 'detail']) {
  test(`归档${target === 'list' ? '列表' : '详情'}会话失效后回到登录页`, async ({ page }) => {
    await mock(page)
    await page.route(target === 'list' ? '**/api/groups/*/messages?*' : '**/api/groups/*/messages/m1',
      route => route.fulfill({ status: 401, json: { detail: '请先登录' } }))
    await page.goto('/')
    await page.getByRole('button', { name: '消息归档', exact: true }).click()
    if (target === 'detail') await page.getByRole('button', { name: '查看详情', exact: true }).click()
    await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible()
  })
}


test('超级管理员可以保存通用人设，群管理员只能修改本群', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '通用设置', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '通用设置', exact: true })
  await dialog.getByRole('tab', { name: '群聊天', exact: true }).click()
  await dialog.getByLabel('性格与说话习惯', { exact: true }).fill('通用测试人设')
  const saved = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/api/settings/defaults'))
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await saved).postDataJSON().settings.chat_persona).toBe('通用测试人设')
  await expect(dialog).not.toBeVisible()
  await mock(page, true, false); await page.reload()
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await expect(page.getByRole('button', { name: '通用设置', exact: true })).toHaveCount(0)
})

test('本群可以恢复通用设置', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).first().click()
  const reset = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/api/groups/-1001/settings'))
  await page.getByRole('dialog').getByRole('button', { name: '恢复通用设置', exact: true }).click()
  const keys = (await reset).postDataJSON().reset
  expect(keys).toContain('chat_persona'); expect(keys).toContain('emoji_status_blocked_packs')
  await expect(page.getByRole('dialog')).not.toBeVisible()
})

test('超级管理员可以把关键词保存为通用规则', async ({ page }) => {
  await mock(page); await page.goto('/')
  await page.getByRole('button', { name: '防广告规则', exact: true }).click()
  await page.getByRole('button', { name: '添加规则', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByPlaceholder('关键词，或 example.com 形式的域名').fill('通用测试广告')
  const save = dialog.getByRole('button', { name: '保存为通用规则', exact: true })
  await expect(save).toBeEnabled()
  const submitted = page.waitForRequest(r => r.method() === 'PATCH' && r.url().endsWith('/api/settings/defaults'))
  await save.click()
  const body = (await submitted).postDataJSON()
  expect(body.rules).toHaveLength(1)
  expect(body.rules[0].value).toBe('通用测试广告')
  expect(body.baseline.rules).toEqual([])
  await expect(dialog).not.toBeVisible()
})

test('新增规则回执丢失后刷新仍确认同一请求', async ({ page }) => {
  await mock(page)
  const requests: { id: string; body: any }[] = []
  const accepted = new Map<string, any>()
  await page.route(/\/api\/groups\/-1001\/rules(?:\/query)?(?:\?.*)?$/, async route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: new URL(route.request().url()).pathname.endsWith('/query')
      ? { items: [...accepted.values()], total: accepted.size } : [...accepted.values()] })
    const id = route.request().headers()['idempotency-key']!, body = route.request().postDataJSON()
    requests.push({ id, body })
    if (!accepted.has(id)) {
      accepted.set(id, { ...body, id: 'one-rule', version: 'one-version' })
      return route.abort('failed')
    }
    return route.fulfill({ json: accepted.get(id) })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '防广告规则', exact: true }).click()
  await page.getByRole('button', { name: '添加规则', exact: true }).click()
  await page.getByPlaceholder('关键词，或 example.com 形式的域名').fill('原始规则')
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认提交结果', exact: true })).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: '确认提交结果', exact: true }).click()
  await expect(page.getByText('已确认提交结果', { exact: true })).toBeVisible()
  expect(requests).toHaveLength(2)
  expect(requests[0]).toEqual(requests[1])
  expect(accepted.size).toBe(1)
})

test('规则冲突保留草稿并提交打开时的记录版本', async ({ page }) => {
  await mock(page)
  const row = { id: 'r-conflict', version: 'original-version', kind: 'keyword', value: '原规则', mode: 'contains', scope: 'message', penalty: 'delete', enabled: true, context_value: '' }
  await page.route(/\/api\/groups\/-1001\/rules(?:\/query)?(?:\?.*)?$/, route => route.fulfill({ json:
    new URL(route.request().url()).pathname.endsWith('/query') ? { items: [row], total: 1 } : [row] }))
  let version = ''
  await page.route('**/api/groups/-1001/rules/r-conflict', route => {
    version = route.request().headers()['if-match'] || ''
    return route.fulfill({ status: 409, json: { detail: '记录已被修改，请刷新后核对；你的草稿已保留' } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '防广告规则', exact: true }).click()
  await page.getByRole('button', { name: '编辑', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByPlaceholder('关键词，或 example.com 形式的域名').fill('保留的草稿')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByText('记录已被修改，请刷新后核对；你的草稿已保留', { exact: true })).toBeVisible()
  await expect(dialog.getByPlaceholder('关键词，或 example.com 形式的域名')).toHaveValue('保留的草稿')
  expect(version).toBe('original-version')
})

test('部分群查询失败保留已核实的群并说明可刷新', async ({ page }) => {
  await mock(page, true, false)
  await page.route('**/api/groups', route => route.fulfill({ headers: { 'X-PurrAI-Groups-Partial': 'true' }, json: [groups[0]] }))
  await page.goto('/')
  await expect(page.getByText('部分群权限暂未核实，当前显示已确认的群；稍后可刷新重试。', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await expect(page.getByText('PurrAI 开发讨论', { exact: true }).first()).toBeVisible()
})

test('推广目标分页展示真实处罚并按版本批量停用', async ({ page }) => {
  await mock(page)
  const rule = { id: 'learned-target', version: 'target-version', kind: 'block_target', value: 'account:ad_target',
    mode: 'exact', scope: 'message', context_value: '', penalty: 'delete', enabled: true, inherited: true,
    kind_label: '推广目标黑名单', matching_label: '推广目标精确匹配', consequence: '删除消息并永久封禁；明确举报引用仅删除',
    origin: 'learned', hit_count: 7, last_hit_at: 1794300000,
    sources: [{ group_id: -1001, message_id: 88, created_at: 1794300000 }] }
  let searched = ''
  await page.route('**/api/groups/-1001/rules/query?**', route => {
    searched = new URL(route.request().url()).searchParams.get('query') || ''
    return route.fulfill({ json: { items: [rule], total: 21, page: 1, size: 20 } })
  })
  let body: any
  await page.route('**/api/groups/-1001/rules/batch', route => {
    body = route.request().postDataJSON(); return route.fulfill({ json: { ok: true, count: 1 } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '防广告规则', exact: true }).click()
  await expect(page.getByRole('cell', { name: '推广目标黑名单', exact: true })).toBeVisible()
  await expect(page.getByText('删除消息并永久封禁；明确举报引用仅删除', { exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: '搜索规则', exact: true }).fill('ad_target')
  await page.getByRole('button', { name: '查询规则', exact: true }).click()
  await expect.poll(() => searched).toBe('ad_target')
  await page.locator('.el-table__body-wrapper .el-checkbox').first().click()
  await page.getByRole('button', { name: '本群停用', exact: true }).click()
  await page.getByRole('button', { name: '确认修改', exact: true }).click()
  await expect(page.getByText('已保存选中规则', { exact: true })).toBeVisible()
  expect(body.versions).toEqual({ 'learned-target': 'target-version' })
  expect(body.scope).toBe('local'); expect(body.enabled).toBe(false)
  await page.screenshot({ path: test.info().outputPath('rules.png'), fullPage: true })
})

test('误判纠正明确选择关联目标和影响范围并保留冲突草稿', async ({ page }) => {
  await mock(page)
  const violation = { id: 'ad-violation', user_id: 42, created_at: 1794300000, excerpt: '待核实的推广消息',
    sender_name: '样本成员', sender_chat: false, reasons: ['广告命中'], actions: [], allowed_operations: ['unban'] }
  await page.route('**/api/groups/-1001/violations?**', route => route.fulfill({ json: { items: [violation], total: 1 } }))
  let body: any
  await page.route('**/api/groups/-1001/violations/ad-violation/correction', route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { global_allowed: true,
      rules: [{ id: 'target', value: 'account:ad_target', enabled: true, version: 'local-v', global_version: 'global-v', other_sources: 2 }],
      affected_groups: [{ id: -1001, title: 'PurrAI 开发讨论', rule_ids: ['target'] }], user_id: 42, excerpt: violation.excerpt } })
    body = route.request().postDataJSON()
    return route.fulfill({ status: 409, json: { detail: '规则已被修改，请核对；草稿保留' } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '违规记录', exact: true }).click()
  await page.getByRole('button', { name: '纠正误判', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '纠正误判', exact: true })
  await dialog.getByLabel('核实原因', { exact: true }).fill('已经核实，需要撤销')
  await dialog.locator('label.el-checkbox').filter({ hasText: 'account:ad_target' }).click()
  await expect(dialog.getByRole('checkbox', { name: /account:ad_target/ })).toBeChecked()
  await dialog.getByText('停用通用规则', { exact: true }).click()
  await expect(dialog.getByText(/将影响继承这些通用规则的群/)).toBeVisible()
  await dialog.locator('.el-form-item').filter({ hasText: '成员限制' }).locator('.el-select__wrapper').click()
  await page.getByRole('option', { name: '解除这次 PurrAI 封禁', exact: true }).click()
  await dialog.getByRole('button', { name: '确认纠正', exact: true }).click()
  await expect(dialog.getByRole('alert').filter({ hasText: '规则已被修改' })).toBeVisible()
  await expect(dialog.getByLabel('核实原因', { exact: true })).toHaveValue('已经核实，需要撤销')
  expect(body.rule_versions).toEqual({ target: 'global-v' }); expect(body.action).toBe('unban')
  await page.screenshot({ path: test.info().outputPath('correction.png'), fullPage: true })
})

test('单项恢复继承保留其他分页中的设置草稿', async ({ page }) => {
  await mock(page)
  const configured = { ...catalog.settings.defaults, chat_name: '本群昵称' }
  await page.route('**/api/groups', route => route.fulfill({ json: [{ ...groups[0], settings: configured, setting_overrides: ['chat_name'] }] }))
  await page.route('**/api/groups/-1001/settings/inheritance', route => route.fulfill({ json: {
    defaults: catalog.settings.defaults, settings: configured, overrides: ['chat_name'] } }))
  const bodies: any[] = []
  await page.route('**/api/groups/-1001/settings', route => {
    const body = route.request().postDataJSON(); bodies.push(body)
    return route.fulfill({ json: { ...groups[0], settings: catalog.settings.defaults, setting_overrides: [] } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '群管理', exact: true }).click()
  await page.getByRole('button', { name: '群设置', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('最多错误次数', { exact: false }).fill('4')
  await dialog.getByRole('tab', { name: '继承与恢复', exact: true }).click()
  const row = dialog.locator('tr').filter({ hasText: '本群昵称' })
  await row.getByRole('button', { name: '恢复此项', exact: true }).click()
  await expect(page.getByText('此项已恢复通用设置', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect(bodies[0].reset).toEqual(['chat_name'])
  expect(bodies[0].baseline).toEqual({ chat_name: '本群昵称' })
  expect(bodies[1].settings).toEqual({ max_attempts: 4 })
})

test('归档详情明确提示版本遗漏和展示内容过期', async ({ page }) => {
  await mock(page)
  await page.route('**/api/groups/-1001/messages/m1', route => route.fulfill({ json: { ...archivedMessage,
    recording_gap: { omitted_at_least: 3, last_skipped_at: 1794300000, reason: '已达到单条消息版本上限', latest_not_recorded: true } } }))
  await page.goto('/')
  await page.getByRole('button', { name: '消息归档', exact: true }).click()
  await page.getByRole('button', { name: '查看详情', exact: true }).click()
  await expect(page.getByText(/至少 3 次更新未保存/)).toBeVisible()
  await expect(page.getByText(/当前展示内容不是最新收到的版本/)).toBeVisible()
})

test('功能完善：地址刷新和前进后退恢复规则查询与所选群', async ({ page }) => {
  await mock(page)
  const requested: string[] = []
  await page.route('**/api/groups/*/rules/query?**', route => {
    requested.push(route.request().url()); return route.fulfill({json: {items: [], total: 0}})
  })
  await page.goto('/#/rules?group=-1002')
  const search = page.getByRole('textbox', {name: '搜索规则', exact: true})
  await search.fill('保留已应用的规则筛选')
  await page.getByRole('button', {name: '查询规则', exact: true}).click()
  await expect.poll(() => requested.at(-1)).toContain('query=')
  const saved = page.url()
  await page.reload()
  await expect(search).toHaveValue('保留已应用的规则筛选')
  await expect.poll(() => requested.at(-1)).toContain('/-1002/rules/query')
  await page.getByRole('button', {name: '消息归档', exact: true}).click()
  await page.getByRole('textbox', {name: '归档关键词', exact: true}).fill('另一页查询')
  await page.getByRole('button', {name: '查询归档', exact: true}).click()
  await page.goBack()
  await page.goBack()
  await expect(search).toHaveValue('保留已应用的规则筛选')
  await expect(page).toHaveURL(saved)
  await page.goForward()
  await expect(page.getByRole('heading', {name: '消息归档', exact: true, level: 1})).toBeVisible()
})

test('功能完善：成员单次时长在确认后提交且查询可刷新恢复', async ({ page }) => {
  await mock(page)
  await page.route('**/api/groups/-1001/members/42', route => route.fulfill({json: {
    user_id: 42, current: {status: 'member'}, current_error: '', checked_at: 1794300000, member: null, whitelist: null,
    verifications: {items: [], total: 0}, violations: {items: [], total: 0}, actions: {items: [], total: 0},
  }}))
  let body: any
  await page.route('**/api/groups/-1001/members/42/actions', route => {
    body = route.request().postDataJSON(); return route.fulfill({json: {id: 'member-action', state: 'pending'}})
  })
  await page.goto('/#/members?group=-1001')
  await page.getByRole('textbox', {name: '成员数字 ID', exact: true}).fill('42')
  await page.getByRole('button', {name: '查询成员', exact: true}).click()
  await expect(page.getByText('成员 42 · 正常成员', {exact: false})).toBeVisible()
  await page.reload()
  await expect(page.getByRole('textbox', {name: '成员数字 ID', exact: true})).toHaveValue('42')
  await page.getByText('单独设置本次时长', {exact: true}).click()
  await page.getByRole('spinbutton', {name: '本次时长数值', exact: true}).fill('30')
  await page.getByRole('textbox', {name: '成员处理原因', exact: true}).fill('核实后只禁言半小时')
  await page.getByRole('button', {name: '核对并提交', exact: true}).click()
  expect((await page.locator('.el-message-box').boundingBox())!.width).toBeLessThanOrEqual(520)
  await page.getByRole('button', {name: '确认提交', exact: true}).click()
  await expect.poll(() => body?.duration_seconds).toBe(1800)
  expect(body.reason).toBe('核实后只禁言半小时')
  await expect(page.getByRole('dialog', {name: '确认成员操作', exact: true})).toHaveCount(0)
  await page.screenshot({path: test.info().outputPath('members.png'), fullPage: true, animations: 'disabled'})
})

test('功能完善：应急保护与原设置分开并要求核对原因', async ({ page }) => {
  await mock(page)
  let active = false, body: any
  await page.route('**/api/groups/-1001/emergency', route => {
    if (route.request().method() === 'POST') {body = route.request().postDataJSON(); active = body.active}
    return route.fulfill({json: {active, reason: body?.reason, until: 1794400000}})
  })
  await page.goto('/')
  await page.getByRole('button', {name: '群管理', exact: true}).click()
  await page.getByRole('button', {name: '群设置', exact: true}).first().click()
  await page.getByRole('tab', {name: '联合刷屏与应急', exact: true}).click()
  await page.getByRole('button', {name: '启用临时应急保护', exact: true}).click()
  const prompt = page.getByRole('dialog', {name: '启用临时应急保护', exact: true})
  await prompt.getByRole('textbox').fill('集中入群，临时核实')
  await prompt.getByRole('button', {name: '确认', exact: true}).click()
  await expect(prompt).toHaveCount(0)
  await expect(page.getByRole('button', {name: '恢复日常保护', exact: true})).toBeVisible()
  expect(body.active).toBe(true); expect(body.reason).toBe('集中入群，临时核实')
  await page.screenshot({path: test.info().outputPath('emergency.png'), fullPage: true, animations: 'disabled'})
})

test('功能完善：新页面在手机展示且聊天诊断保留话题筛选', async ({ page }) => {
  await mock(page)
  await page.setViewportSize({width: 390, height: 844})
  await page.goto('/#/chat?group=-1001')
  await page.getByRole('textbox', {name: '聊天话题 ID', exact: true}).fill('23')
  await page.getByRole('button', {name: '查询聊天处理', exact: true}).click()
  await page.reload()
  await expect(page.getByRole('textbox', {name: '聊天话题 ID', exact: true})).toHaveValue('23')
  for (const id of ['chat', 'members', 'operations']) {
    await page.goto(`/#/${id}?group=-1001`)
    await expect(page.locator('.panel').first()).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy()
  }
  await page.screenshot({path: test.info().outputPath('operations-mobile.png'), fullPage: true})
})

test('功能完善：归档样本使用未保存草稿且不提交规则', async ({ page }) => {
  await mock(page)
  let body: any, writes = 0
  await page.route('**/api/groups/-1001/rules', route => { writes++; return route.fulfill({json: {id: 'unexpected'}}) })
  await page.route('**/api/groups/-1001/rules/preview-archive', route => {
    body = route.request().postDataJSON()
    const before = {reasons: [], delete_message: false}, after = {reasons: ['草稿关键词'], delete_message: true}
    return route.fulfill({json: {items: [{id: 'm1', message_id: 88, text: '原归档', before, after, changed: true}], changed_count: 1, notes: ['只使用已保存内容，不执行处罚']}})
  })
  await page.goto('/#/rules?group=-1001')
  await page.getByRole('button', {name: '添加规则', exact: true}).click()
  await page.getByLabel('规则内容', {exact: true}).fill('尚未保存的草稿关键词')
  await page.getByRole('button', {name: '用归档样本比较草稿', exact: true}).click()
  const samples = page.getByRole('dialog', {name: '归档样本试跑', exact: true})
  await samples.locator('.el-table__body-wrapper .el-checkbox').first().click()
  await samples.getByRole('button', {name: '试跑所选样本', exact: true}).click()
  await expect(samples.getByText('草稿关键词', {exact: true})).toBeVisible()
  expect(body.draft.value).toBe('尚未保存的草稿关键词'); expect(body.archive_ids).toEqual(['m1']); expect(writes).toBe(0)
})

test('功能完善：备份计划明确保留授权并可查看恢复演练结果', async ({ page }) => {
  await mock(page)
  let plan = {enabled: false, hour_beijing: 3, interval_days: 1, retention_days: 30, retention_count: 10, max_total_mb: 2048, prune_enabled: false, exercise_after_backup: true}
  let exercise = false, saved: any
  await page.route('**/api/system/backups', route => route.fulfill({json: {plan, free_bytes: 10485760000, schedule: {}, items: [{
    file: 'purrai-20261110-000000-000000.sqlite3', bytes: 1048576, created_at: 1794300000, revision: '0025', managed: true,
    ...(exercise ? {exercise: {integrity_ok: true, foreign_keys_ok: true, compatible: true, checked_at: 1794300000, counts: {groups: 2}}} : {}),
  }]}}))
  await page.route('**/api/system/backups/plan', route => {saved = route.request().postDataJSON(); plan = saved; return route.fulfill({json: plan})})
  await page.route('**/api/system/backups/exercise', route => {exercise = true; return route.fulfill({json: {ok: true}})})
  await page.goto('/#/system?group=-1001')
  await page.getByRole('button', {name: '备份计划与文件', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: '备份计划与文件', exact: true})
  await dialog.getByText('允许按下列规则整理本程序登记的备份', {exact: true}).click()
  await dialog.getByRole('button', {name: '保存备份计划', exact: true}).click()
  await expect.poll(() => saved?.prune_enabled).toBe(true)
  expect(saved.enabled).toBe(false)
  await dialog.getByRole('button', {name: '恢复演练', exact: true}).click()
  await expect(dialog.getByRole('cell').filter({hasText: '完整性通过 · 版本匹配'})).toBeVisible()
})

test('功能完善：发送核实登记不会触发重试', async ({ page }) => {
  await mock(page)
  let reviewed = false, body: any, retries = 0
  await page.route('**/api/system/failures?**', route => route.fulfill({json: {items: reviewed || route.request().url().includes('event=true') ? [] : [{id: 'uncertain', kind: 'challenge', state: 'failed', error: '发送结果未确认', can_review_delivery: true}], total: reviewed ? 0 : 1}}))
  await page.route('**/api/actions/uncertain/resolve', route => {body = route.request().postDataJSON(); reviewed = true; return route.fulfill({json: {ok: true}})})
  await page.route('**/api/actions/uncertain/retry', route => {retries++; return route.fulfill({json: {ok: true}})})
  await page.goto('/#/system?group=-1001')
  await page.getByRole('button', {name: '登记核实结果', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: '登记发送核实结果', exact: true})
  await dialog.getByText('确认未完成', {exact: true}).click()
  await dialog.getByLabel('核实情况', {exact: true}).fill('已查看 Telegram，原图片未发送')
  await dialog.getByRole('button', {name: '登记核实结果', exact: true}).click()
  await expect.poll(() => body?.outcome).toBe('not_completed')
  expect(retries).toBe(0)
})

test('功能完善：归档额度可查看逐群用量并保存', async ({ page }) => {
  await mock(page)
  let body: any
  await page.route('**/api/groups/-1001/archive/capacity', route => {
    if (route.request().method() === 'PATCH') {body = route.request().postDataJSON(); return route.fulfill({json: {ok: true}})}
    return route.fulfill({json: {bytes: 1048576, limit_bytes: 536870912, group_bytes: 1048576, group_limit_bytes: null,
      can_edit_quotas: true, groups: [{id: -1001, title: '本群容量记录', bytes: 1048576, limit_bytes: null}]}})
  })
  await page.route('**/api/groups/-1001/archive/exports', route => route.fulfill({json: []}))
  await page.goto('/#/messages?group=-1001')
  await page.getByRole('button', {name: '容量与整理', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: '归档容量与整理', exact: true})
  await expect(dialog.getByText('本群容量记录', {exact: true})).toBeVisible()
  await dialog.getByText('单独限制本群额度', {exact: true}).click()
  await dialog.getByRole('button', {name: '保存归档额度', exact: true}).click()
  await expect.poll(() => body?.group_mb).toBe(128)
  expect(body.global_mb).toBe(512)
  await expect(dialog.getByRole('button', {name: '整理选中导出的归档', exact: true})).toBeDisabled()
})

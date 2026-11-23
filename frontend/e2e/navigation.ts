import { expect, type Locator, type Page } from '@playwright/test'
import { workspacePages, workspaceSections } from '../src/workspaceNavigation'

const legacyNames: Record<string, string> = { 群管理: 'groups', 成员管理: 'members', 聊天状态: 'chat', 系统状态: 'system' }

export async function openPage(page: Page, name: string) {
  const target = workspacePages.find(item => item.id === legacyNames[name] || item.name === name)
    ?? workspacePages.find(item => item.id === workspaceSections.find(section => section.name === name)?.page)
  if (!target) throw new Error(`未知页面：${name}`)
  const section = workspaceSections.find(item => item.id === target.section)!
  if (section.id === 'system') await page.getByRole('button', { name: '系统维护', exact: true }).click()
  else await page.getByRole('navigation', { name: '管理导航' }).getByRole('button', { name: section.name, exact: true }).click()
  if (target.secondary) {
    await page.getByRole('button', { name: '记录选项', exact: true }).click()
    await page.getByRole('menuitem', { name: target.name, exact: true }).click()
  } else if (target.id !== 'overview') {
    await page.locator('.workspace-tabs > .el-tabs__header').getByRole('tab', { name: target.name, exact: true }).click()
  }
}

export async function settingsTab(dialog: Locator | Page, name: string) {
  const tab = dialog.getByRole('tab', { name, exact: true })
  if (!await tab.count()) {
    await dialog.getByRole('button', { name: '更多设置', exact: true }).click()
    const page = 'page' in dialog ? dialog.page() : dialog
    await page.getByRole('menuitem', { name, exact: true }).click()
  } else await tab.click()
}

export async function groupAdministration(page: Page) {
  const details = page.locator('.group-administration')
  if (!await details.evaluate(element => (element as HTMLDetailsElement).open)) await details.locator('summary').click()
  await expect(details).toHaveAttribute('open', '')
}

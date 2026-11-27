import { expect, test } from '@playwright/test'
import { primaryButton, resetBackend } from './helpers'

test.beforeEach(async ({ request }) => {
  await resetBackend(request)
})

test('creates a campaign through the New Campaign dialog and opens its editor', async ({
  page,
}) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'New Campaign' }).click()

  const dialog = page.getByRole('dialog', { name: 'New Campaign' })
  await dialog.getByLabel('Name').fill('E2E Created Campaign')
  await dialog.getByLabel('Geo').selectOption('DE')
  await dialog.getByLabel('Offer').fill('beta')
  await dialog.getByRole('option', { name: /Beta Finance Offer/ }).click()
  await dialog.getByRole('button', { name: 'Create' }).click()

  // Successful creation opens the new campaign's editor with a clean baseline.
  await expect(page).toHaveURL(/#\/campaigns\/[0-9a-f-]{36}$/)
  await expect(page.getByRole('heading', { name: 'E2E Created Campaign' })).toBeVisible()
  await expect(page.getByText('Clean')).toBeVisible()

  const googleRow = page.locator('tr', { hasText: 'Google — DE' })
  const offerRow = page.locator('tr', { hasText: 'Offer — 18002' })
  await expect(googleRow).toBeVisible()
  await expect(offerRow).toBeVisible()
  await expect(page.locator('.offer-list-item', { hasText: 'Beta Finance Offer' })).toContainText(
    '100%',
  )

  // A clean draft offers the fetch action.
  await expect(primaryButton(page)).toHaveText('Get streams from Keitaro')

  // The created campaign appears in the list.
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'E2E Created Campaign' })).toBeVisible()
})

test('creation dialog validates before submitting', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'New Campaign' }).click()

  const dialog = page.getByRole('dialog', { name: 'New Campaign' })
  await dialog.getByRole('button', { name: 'Create' }).click()

  await expect(dialog.getByText('Enter a campaign name.')).toBeVisible()
  await expect(dialog.getByText('Select a country.')).toBeVisible()
  await expect(dialog.getByText('Select an offer from the search results.')).toBeVisible()
  // Still on the list: nothing was created.
  await expect(page).not.toHaveURL(/#\/campaigns\//)
})

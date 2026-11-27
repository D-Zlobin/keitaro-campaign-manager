import { expect, test } from '@playwright/test'
import { IMPORT_KEITARO_ID, importCampaignThroughUi, resetBackend, streamRow } from './helpers'

test.beforeEach(async ({ request }) => {
  await resetBackend(request)
})

test('imports an existing Keitaro campaign with active and disabled streams', async ({ page }) => {
  await importCampaignThroughUi(page)

  await expect(page.getByText('Clean')).toBeVisible()

  await expect(streamRow(page, 'Google — DE')).toBeVisible()
  await expect(streamRow(page, 'Offer — 18001')).toBeVisible()

  // The disabled remote stream is imported as a visually distinct row.
  const disabledRow = streamRow(page, 'Legacy Stream')
  await expect(disabledRow).toContainText('Disabled')
  await expect(disabledRow.getByRole('button', { name: 'Restore' })).toBeVisible()

  // Offer associations are visible by default.
  await expect(page.getByText('Alpha Diet Offer')).toBeVisible()
  await expect(page.locator('.offer-list').filter({ hasText: 'Alpha Diet Offer' })).toContainText(
    '100%',
  )
})

test('rejects importing the same campaign twice', async ({ page }) => {
  await importCampaignThroughUi(page)

  await page.goto('/')
  await page.getByLabel('Open by Keitaro campaign ID').fill(String(IMPORT_KEITARO_ID))
  await page.getByRole('button', { name: 'Open' }).click()

  await expect(page.getByRole('alert')).toContainText(/already imported/i)
  await expect(page).not.toHaveURL(/#\/campaigns\//)
})

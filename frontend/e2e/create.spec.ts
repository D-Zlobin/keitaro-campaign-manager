import { expect, test } from '@playwright/test'
import { primaryButton, remoteState, resetBackend } from './helpers'

test.beforeEach(async ({ request }) => {
  await resetBackend(request)
})

test('creates a campaign through the New Campaign dialog and opens its editor', async ({
  page,
  request,
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

  const campaignsResponse = await request.get('/api/v1/campaigns')
  expect(campaignsResponse.ok()).toBeTruthy()
  const { campaigns } = (await campaignsResponse.json()) as {
    campaigns: Array<{ name: string; keitaro_id: number }>
  }
  const created = campaigns.find((campaign) => campaign.name === 'E2E Created Campaign')!
  const remote = await remoteState(request, created.keitaro_id)

  expect(remote.campaign).toMatchObject({
    name: 'E2E Created Campaign',
    type: 'weight',
    state: 'active',
    domain_id: 1813,
    group_id: 10332,
    traffic_source_id: 836,
  })
  expect(remote.streams).toHaveLength(2)
  const [google, offer] = [...remote.streams].sort((a, b) => a.position - b.position)
  expect(google).toMatchObject({
    name: 'Google — DE',
    type: 'regular',
    schema: 'redirect',
    state: 'active',
    position: 0,
    weight: 50,
    action_type: 'redirect',
    action_payload: 'https://www.google.com/',
    filters: [{ name: 'country', mode: 'accept', payload: ['DE'] }],
    offers: [],
  })
  expect(offer).toMatchObject({
    name: 'Offer — 18002',
    type: 'regular',
    schema: 'landings',
    state: 'active',
    position: 1,
    weight: 50,
    action_type: 'http',
    filters: [{ name: 'country', mode: 'reject', payload: ['DE'] }],
    offers: [{ offer_id: 18002, state: 'active', share: 100 }],
  })

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

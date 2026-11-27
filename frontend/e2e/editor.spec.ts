import { expect, test } from '@playwright/test'
import {
  driftRemote,
  importCampaignThroughUi,
  primaryButton,
  remoteStreams,
  resetBackend,
  streamRow,
  IMPORT_KEITARO_ID,
} from './helpers'

test.beforeEach(async ({ request }) => {
  await resetBackend(request)
})

test('draft edits persist across a browser reload', async ({ page }) => {
  await importCampaignThroughUi(page)

  // Delete (soft) the Google stream: the offer stream takes 100%.
  await streamRow(page, 'Google — DE').getByRole('button', { name: 'Delete' }).click()
  await expect(page.getByText('Dirty')).toBeVisible()
  await expect(streamRow(page, 'Google — DE')).toContainText('Disabled')
  await expect(primaryButton(page)).toHaveText('Upload streams to Keitaro')

  // The draft was persisted server-side: a full browser reload shows the
  // same unsynchronized state.
  await page.reload()
  await expect(page.getByText('Dirty')).toBeVisible()
  await expect(streamRow(page, 'Google — DE')).toContainText('Disabled')
  await expect(primaryButton(page)).toHaveText('Upload streams to Keitaro')
})

test('a dirty draft rejects a plain fetch and is replaced only after confirmation', async ({
  page,
  request,
}) => {
  await importCampaignThroughUi(page)
  await streamRow(page, 'Google — DE').getByRole('button', { name: 'Delete' }).click()
  await expect(page.getByText('Dirty')).toBeVisible()

  // A fetch without the explicit confirmation flag conflicts (409) and
  // changes neither baseline nor draft.
  const editorUrl = page.url()
  const campaignId = editorUrl.split('#/campaigns/')[1]
  const conflict = await request.post(`/api/v1/campaigns/${campaignId}/fetch`, {
    data: { confirm_discard: false },
  })
  expect(conflict.status()).toBe(409)
  expect((await conflict.json()).error.code).toBe('dirty_draft')

  // The operator-facing path: discard requires an explicit confirmation.
  await page.getByRole('button', { name: 'Discard changes and reload' }).click()
  await page.getByRole('button', { name: 'Discard and reload' }).click()

  // The draft is replaced with the fetched remote state.
  await expect(page.getByText('Clean')).toBeVisible()
  await expect(streamRow(page, 'Google — DE')).not.toContainText('Disabled')
  await expect(primaryButton(page)).toHaveText('Get streams from Keitaro')
})

test('hard reset discards local state and reloads streams from Keitaro', async ({ page }) => {
  await importCampaignThroughUi(page)
  const panel = page.locator('.offers-panel').filter({ hasText: 'Alpha Diet Offer' })
  await panel.getByRole('button', { name: 'Pin' }).click()
  await expect(panel.getByText('Pinned')).toBeVisible()

  const hardReset = page.getByRole('button', { name: 'Hard reset from Kitaro' })
  await expect(hardReset).toHaveClass(/danger/)
  await hardReset.click()
  await page.getByRole('button', { name: 'Confirm hard reset' }).click()

  await expect(panel.getByText('Pinned')).toHaveCount(0)
  await expect(streamRow(page, 'Google — DE')).toBeVisible()
  await expect(primaryButton(page)).toHaveText('Get streams from Keitaro')
})

test('hard reset recovers a campaign stuck in syncing', async ({ page, request }) => {
  await importCampaignThroughUi(page)
  const campaignId = page.url().split('#/campaigns/')[1]
  const marked = await request.post(`/e2e/campaigns/${campaignId}/syncing`)
  expect(marked.ok()).toBeTruthy()
  await page.reload()
  await expect(page.getByText('Syncing', { exact: true })).toBeVisible()

  const hardReset = page.getByRole('button', { name: 'Hard reset from Kitaro' })
  await expect(hardReset).toBeEnabled()
  await hardReset.click()
  await page.getByRole('button', { name: 'Confirm hard reset' }).click()

  await expect(page.getByText('Clean', { exact: true })).toBeVisible()
  await expect(page.getByText('Syncing', { exact: true })).toHaveCount(0)
  await expect(primaryButton(page)).toHaveText('Get streams from Keitaro')
  await expect(primaryButton(page)).toBeEnabled()
})

test('upload succeeds and updates only the intended remote fields', async ({ page, request }) => {
  await importCampaignThroughUi(page)
  await streamRow(page, 'Google — DE').getByRole('button', { name: 'Delete' }).click()
  await expect(page.getByText('Dirty')).toBeVisible()

  await primaryButton(page).click()

  // After a confirmed upload the editor is clean and back to the fetch action.
  await expect(page.getByText('Clean')).toBeVisible()
  await expect(primaryButton(page)).toHaveText('Get streams from Keitaro')

  // The remote reflects exactly the intended change: the deleted stream is
  // disabled with weight 0, the surviving stream carries 100.
  const streams = await remoteStreams(request, IMPORT_KEITARO_ID)
  const byId = new Map(streams.map((stream) => [stream.id, stream]))
  expect(byId.get(9101)?.state).toBe('disabled')
  expect(Number(byId.get(9101)?.weight)).toBe(0)
  expect(byId.get(9102)?.state).toBe('active')
  expect(Number(byId.get(9102)?.weight)).toBe(100)
})

test('upload conflicts when the remote changed, writes nothing, and keeps the draft', async ({
  page,
  request,
}) => {
  await importCampaignThroughUi(page)
  await streamRow(page, 'Google — DE').getByRole('button', { name: 'Delete' }).click()
  await expect(page.getByText('Dirty')).toBeVisible()

  // Someone else edits the campaign in Keitaro after our last fetch.
  await driftRemote(request, IMPORT_KEITARO_ID)

  await primaryButton(page).click()

  // The preflight detects the drift: 409, no writes, draft preserved.
  await expect(page.getByText(/changed in Keitaro/)).toBeVisible()
  await expect(page.getByText('Dirty')).toBeVisible()
  await expect(primaryButton(page)).toHaveText('Upload streams to Keitaro')
  const streams = await remoteStreams(request, IMPORT_KEITARO_ID)
  const byId = new Map(streams.map((stream) => [stream.id, stream]))
  // The drifted weight (51) is untouched and the offer stream was never set to 100.
  expect(Number(byId.get(9101)?.weight)).toBe(51)
  expect(byId.get(9101)?.state).toBe('active')
  expect(Number(byId.get(9102)?.weight)).toBe(50)
})

test('pinned offers keep their share while the rest redistribute', async ({ page }) => {
  await importCampaignThroughUi(page)
  const panel = page.locator('.offers-panel').filter({ hasText: 'Alpha Diet Offer' })
  const search = panel.getByRole('combobox', { name: /Search offers to add/ })
  const alpha = panel.locator('.offer-list-item', { hasText: 'Alpha Diet Offer' })

  // Pin-only change on the clean import: badge appears, draft stays clean.
  await alpha.getByRole('button', { name: 'Pin' }).click()
  await expect(panel.getByText('Pinned')).toBeVisible()
  await expect(page.getByText('Clean')).toBeVisible()
  await alpha.getByRole('button', { name: 'Unpin' }).click()
  await expect(panel.getByText('Pinned')).toHaveCount(0)

  // Two offers at 50/50, then pin Alpha.
  await search.fill('gamma')
  await page.getByRole('option', { name: /Gamma Sweep Offer/ }).click()
  await panel.getByRole('button', { name: 'Add offer' }).click()
  await expect(panel.getByText('50%')).toHaveCount(2)
  await alpha.getByRole('button', { name: 'Pin' }).click()
  await expect(panel.getByText('Pinned')).toBeVisible()

  // Adding a third offer: the pinned offer keeps 50, the rest split 25/25.
  await search.fill('delta')
  await page.getByRole('option', { name: /Delta Nutra Offer/ }).click()
  await panel.getByRole('button', { name: 'Add offer' }).click()
  await expect(alpha).toContainText('50%')
  await expect(alpha).toContainText('Pinned')
  await expect(panel.locator('.offer-list-item', { hasText: 'Gamma Sweep Offer' })).toContainText(
    '25%',
  )
  await expect(panel.locator('.offer-list-item', { hasText: 'Delta Nutra Offer' })).toContainText(
    '25%',
  )
  await expect(page.getByText('Dirty')).toBeVisible()

  // Unpinning redistributes equally again: 34/33/33.
  await alpha.getByRole('button', { name: 'Unpin' }).click()
  await expect(panel.getByText('Pinned')).toHaveCount(0)
  await expect(panel.getByText('34%')).toHaveCount(1)
  await expect(panel.getByText('33%')).toHaveCount(2)
})

test('offers can be added and removed with equal shares per stream', async ({ page }) => {
  await importCampaignThroughUi(page)
  const panel = page.locator('.offers-panel').filter({ hasText: 'Alpha Diet Offer' })
  // The offer already active in the stream is excluded from the selector.
  const search = panel.getByRole('combobox', { name: /Search offers to add/ })
  await search.fill('alpha')
  await expect(page.getByText('No offers match your search.')).toBeVisible()

  // Add a second offer: both share 50/50, stream-level shares are unchanged.
  await search.fill('gamma')
  await page.getByRole('option', { name: /Gamma Sweep Offer/ }).click()
  await panel.getByRole('button', { name: 'Add offer' }).click()
  await expect(panel).toContainText('Alpha Diet Offer')
  await expect(panel).toContainText('Gamma Sweep Offer')
  await expect(panel.getByText('50%')).toHaveCount(2)
  await expect(page.getByText('Dirty')).toBeVisible()

  // Removing an offer re-concentrates the share on the remaining one.
  await panel
    .locator('.offer-list-item', { hasText: 'Gamma Sweep Offer' })
    .getByRole('button', { name: 'Remove' })
    .click()
  await expect(panel.locator('.offer-list-item', { hasText: 'Gamma Sweep Offer' })).toContainText(
    'Disabled',
  )
  await expect(panel).toContainText('100%')

  // Re-adding the removed offer reactivates the same association (no duplicate).
  await search.fill('gamma')
  await page.getByRole('option', { name: /Gamma Sweep Offer/ }).click()
  await panel.getByRole('button', { name: 'Add offer' }).click()
  await expect(panel.getByText('50%')).toHaveCount(2)
})

test('upload deletes removed offers remotely but keeps them disabled locally', async ({
  page,
  request,
}) => {
  await importCampaignThroughUi(page)
  const panel = page.locator('.offers-panel').filter({ hasText: 'Alpha Diet Offer' })
  const search = panel.getByRole('combobox', { name: /Search offers to add/ })
  await search.fill('gamma')
  await page.getByRole('option', { name: /Gamma Sweep Offer/ }).click()
  await panel.getByRole('button', { name: 'Add offer' }).click()
  const alpha = panel.locator('.offer-list-item', { hasText: 'Alpha Diet Offer' })
  await alpha.getByRole('button', { name: 'Remove' }).click()
  await expect(alpha).toContainText('Disabled')

  await primaryButton(page).click()

  await expect(page.getByText('Clean', { exact: true })).toBeVisible()
  await expect(alpha).toContainText('Disabled')
  await expect(alpha).toContainText('0%')
  const remote = await remoteStreams(request, IMPORT_KEITARO_ID)
  const offerStream = remote.find((stream) => stream.id === 9102)
  expect(
    offerStream?.offers.map(({ offer_id, state, share }) => ({ offer_id, state, share })),
  ).toEqual([{ offer_id: 18003, state: 'active', share: 100 }])
})

import { expect } from '@playwright/test'
import type { APIRequestContext, Page } from '@playwright/test'

/** Keitaro campaign ID seeded by the e2e server for import scenarios. */
export const IMPORT_KEITARO_ID = 4242

/** Reset the fake upstreams and wipe the database; call before every test. */
export async function resetBackend(request: APIRequestContext): Promise<void> {
  const response = await request.post('/e2e/reset')
  expect(response.ok()).toBeTruthy()
}

/** Simulate an external remote edit so the next upload preflight conflicts. */
export async function driftRemote(request: APIRequestContext, keitaroId: number): Promise<void> {
  const response = await request.post('/e2e/remote/drift', {
    data: { keitaro_campaign_id: keitaroId },
  })
  expect(response.ok()).toBeTruthy()
}

export interface RemoteStream {
  id: number
  name: string
  state: string
  weight: number | string
}

/** Read the fake remote's current streams for outcome assertions. */
export async function remoteStreams(
  request: APIRequestContext,
  keitaroId: number,
): Promise<RemoteStream[]> {
  const response = await request.get('/e2e/remote/state', {
    params: { keitaro_campaign_id: keitaroId },
  })
  expect(response.ok()).toBeTruthy()
  const body = (await response.json()) as { streams: RemoteStream[] }
  return body.streams
}

/** Import the seeded remote campaign through the UI and land in its editor. */
export async function importCampaignThroughUi(page: Page): Promise<void> {
  await page.goto('/')
  await page.getByLabel('Open by Keitaro campaign ID').fill(String(IMPORT_KEITARO_ID))
  await page.getByRole('button', { name: 'Open' }).click()
  await expect(page).toHaveURL(new RegExp(`#/campaigns/[0-9a-f-]{36}$`))
  await expect(page.getByRole('heading', { name: 'Imported Remote Campaign' })).toBeVisible()
}

/** The editor's primary synchronization button. */
export function primaryButton(page: Page) {
  return page.getByRole('button', {
    name: /Get streams from Keitaro|Upload streams to Keitaro|Uploading…|Fetching…/,
  })
}

/** Row of the streams table for one stream name. */
export function streamRow(page: Page, name: string) {
  return page.locator('tr', { hasText: name })
}

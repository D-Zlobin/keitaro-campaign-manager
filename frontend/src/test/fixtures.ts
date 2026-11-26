import type { CampaignDetail, CampaignSummary, DraftStream } from '../api/types'

export const CAMPAIGN_ID = '7f3c9a2e-4c1b-4f6a-9c2d-8e5b7a9d1c3f'

export function makeSummary(overrides: Partial<CampaignSummary> = {}): CampaignSummary {
  return {
    id: CAMPAIGN_ID,
    keitaro_id: 500,
    name: 'Example DE',
    country_code: 'DE',
    sync_state: 'clean',
    dirty: false,
    revision: 1,
    last_fetch_at: '2026-11-26T12:00:00',
    last_upload_at: null,
    created_at: '2026-11-26T11:00:00',
    ...overrides,
  }
}

export function makeGoogleStream(overrides: Partial<DraftStream> = {}): DraftStream {
  return {
    keitaro_id: 9101,
    name: 'Google — DE',
    type: 'regular',
    schema: 'redirect',
    position: 0,
    state: 'active',
    share: 50,
    pinned: false,
    offer_capable: false,
    offers: [],
    ...overrides,
  }
}

export function makeOfferStream(overrides: Partial<DraftStream> = {}): DraftStream {
  return {
    keitaro_id: 9102,
    name: 'Offer — 18001',
    type: 'regular',
    schema: 'landings',
    position: 1,
    state: 'active',
    share: 50,
    pinned: false,
    offer_capable: true,
    offers: [
      {
        offer_id: 18001,
        offer_name: 'Alpha Diet Offer',
        keitaro_association_id: 7001,
        state: 'active',
        share: 100,
      },
    ],
    ...overrides,
  }
}

export function makeDetail(overrides: Partial<CampaignDetail> = {}): CampaignDetail {
  return {
    campaign: makeSummary(),
    streams: [makeGoogleStream(), makeOfferStream()],
    notices: [],
    upload_blocked: false,
    ...overrides,
  }
}

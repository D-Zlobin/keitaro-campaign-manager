/** Typed endpoint functions for the application API (spec section 8). */

import { apiRequest } from './client'
import type {
  CampaignDetail,
  CampaignOperationResult,
  CampaignSummary,
  DraftCommand,
  GeoOption,
  OfferOption,
} from './types'

export function listCampaigns(signal?: AbortSignal): Promise<{ campaigns: CampaignSummary[] }> {
  return apiRequest('/campaigns', { signal })
}

export function createCampaign(
  input: { name: string; country_code: string; offer_id: number },
  idempotencyKey: string,
): Promise<CampaignOperationResult> {
  return apiRequest('/campaigns', {
    method: 'POST',
    body: input,
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

export function importCampaign(keitaroCampaignId: number): Promise<CampaignOperationResult> {
  return apiRequest('/campaigns/import', {
    method: 'POST',
    body: { keitaro_campaign_id: keitaroCampaignId },
  })
}

export function getCampaign(id: string, signal?: AbortSignal): Promise<CampaignDetail> {
  return apiRequest(`/campaigns/${id}`, { signal })
}

export function fetchCampaign(
  id: string,
  confirmDiscard: boolean,
): Promise<CampaignOperationResult> {
  return apiRequest(`/campaigns/${id}/fetch`, {
    method: 'POST',
    body: { confirm_discard: confirmDiscard },
  })
}

export function updateCampaignDraft(
  id: string,
  expectedRevision: number,
  command: DraftCommand,
): Promise<CampaignDetail> {
  return apiRequest(`/campaigns/${id}/draft`, {
    method: 'PUT',
    body: { expected_revision: expectedRevision, command },
  })
}

export function uploadCampaign(id: string): Promise<CampaignOperationResult> {
  return apiRequest(`/campaigns/${id}/upload`, { method: 'POST' })
}

export function listGeos(signal?: AbortSignal): Promise<{ geos: GeoOption[] }> {
  return apiRequest('/geos', { signal })
}

export interface OfferSearchContext {
  campaignId: string
  streamId: number
}

export function searchOffers(
  search: string,
  context?: OfferSearchContext,
  signal?: AbortSignal,
): Promise<{ offers: OfferOption[] }> {
  const params = new URLSearchParams({ search })
  if (context) {
    params.set('campaign_id', context.campaignId)
    params.set('stream_id', String(context.streamId))
  }
  return apiRequest(`/offers?${params.toString()}`, { signal })
}

/**
 * TypeScript mirrors of the backend API contracts (backend/app/api/schemas.py).
 * The browser only talks to this application's backend; Keitaro and the Five
 * API are reached exclusively through these endpoints.
 */

export type SyncState = 'clean' | 'dirty' | 'syncing' | 'needs_attention'
export type StreamState = 'active' | 'disabled'
export type OfferAssociationState = 'active' | 'disabled'

export interface CampaignSummary {
  id: string
  keitaro_id: number
  name: string
  country_code: string | null
  sync_state: SyncState
  dirty: boolean
  revision: number
  last_fetch_at: string | null
  last_upload_at: string | null
  created_at: string
}

export interface DraftOffer {
  offer_id: number
  offer_name: string
  keitaro_association_id: number | null
  state: OfferAssociationState
  share: number
  pinned: boolean
}

export interface DraftStream {
  keitaro_id: number
  name: string
  type: string
  schema: string
  position: number
  state: StreamState
  share: number
  offer_capable: boolean
  offers: DraftOffer[]
}

export interface CampaignDetail {
  campaign: CampaignSummary
  streams: DraftStream[]
  notices: string[]
  upload_blocked: boolean
}

export interface CampaignOperationResult {
  campaign: CampaignSummary
  notices: string[]
}

export interface GeoOption {
  code: string
  name: string
}

export interface OfferOption {
  offer_id: number
  name: string
}

/** The only editable draft operations; percentages and renames are not expressible. */
export type DraftCommand =
  | { type: 'delete_stream'; stream_id: number }
  | { type: 'restore_stream'; stream_id: number }
  | { type: 'pin_offer'; stream_id: number; offer_id: number }
  | { type: 'unpin_offer'; stream_id: number; offer_id: number }
  | { type: 'add_offer'; stream_id: number; offer_id: number }
  | { type: 'remove_offer'; stream_id: number; offer_id: number }

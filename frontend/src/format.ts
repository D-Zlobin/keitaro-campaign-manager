import type { CampaignSummary, SyncState } from './api/types'

/** Deterministic `YYYY-MM-DD HH:MM` in local time; falls back to the raw value. */
export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) {
    return iso
  }
  const pad = (value: number) => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/** Last successful synchronization (fetch or upload), whichever is newer. */
export function lastSyncAt(campaign: CampaignSummary): string | null {
  const timestamps = [campaign.last_fetch_at, campaign.last_upload_at]
    .filter((value): value is string => value !== null)
    .map((value) => ({ value, parsed: Date.parse(value) }))
    .filter((entry) => !Number.isNaN(entry.parsed))
  if (timestamps.length === 0) {
    return null
  }
  timestamps.sort((a, b) => a.parsed - b.parsed)
  return timestamps[timestamps.length - 1].value
}

export const SYNC_STATE_LABELS: Record<SyncState, string> = {
  clean: 'Clean',
  dirty: 'Dirty',
  syncing: 'Syncing',
  needs_attention: 'Needs attention',
}

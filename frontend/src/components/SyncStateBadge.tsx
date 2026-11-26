import type { SyncState } from '../api/types'
import { SYNC_STATE_LABELS } from '../format'

export function SyncStateBadge({ state }: { state: SyncState }) {
  return <span className={`badge sync-badge sync-${state}`}>{SYNC_STATE_LABELS[state]}</span>
}

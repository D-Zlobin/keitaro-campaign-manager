import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { fetchCampaign, getCampaign, updateCampaignDraft, uploadCampaign } from '../api/campaigns'
import type { CampaignDetail, DraftCommand } from '../api/types'
import { ConfirmButton } from '../components/ConfirmButton'
import { StreamsTable } from '../components/StreamsTable'
import { SyncStateBadge } from '../components/SyncStateBadge'
import { formatDateTime, lastSyncAt } from '../format'

interface CampaignEditorPageProps {
  campaignId: string
}

type LoadState = 'loading' | 'error' | 'ready'
type Operation = 'fetch' | 'upload' | 'draft'

interface OperationError {
  kind: 'generic' | 'remote_conflict' | 'dirty_draft' | 'blocked' | 'validation' | 'info'
  message: string
}

/**
 * Edit Campaign screen. All editing goes through the draft API; server
 * responses are authoritative. Synchronization is never implicit: fetching
 * and uploading happen only from explicit operator actions.
 */
export function CampaignEditorPage({ campaignId }: CampaignEditorPageProps) {
  const [detail, setDetail] = useState<CampaignDetail | null>(null)
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [loadError, setLoadError] = useState('')
  const [operation, setOperation] = useState<Operation | null>(null)
  const [opError, setOpError] = useState<OperationError | null>(null)
  const [opNotices, setOpNotices] = useState<string[]>([])
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({})
  const [staleDraft, setStaleDraft] = useState(false)

  const reload = useCallback(async () => {
    const fresh = await getCampaign(campaignId)
    setDetail(fresh)
    return fresh
  }, [campaignId])

  useEffect(() => {
    const controller = new AbortController()
    getCampaign(campaignId, controller.signal)
      .then((fresh) => {
        setDetail(fresh)
        setLoadState('ready')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') {
          return
        }
        setLoadError(
          error instanceof ApiError ? error.message : 'The campaign could not be loaded.',
        )
        setLoadState('error')
      })
    return () => controller.abort()
  }, [campaignId])

  const sendCommand = useCallback(
    async (command: DraftCommand): Promise<boolean> => {
      if (!detail || operation) {
        return false
      }
      setOperation('draft')
      setOpError(null)
      setRowErrors((current) => {
        const next = { ...current }
        delete next[command.stream_id]
        return next
      })
      try {
        const updated = await updateCampaignDraft(campaignId, detail.campaign.revision, command)
        setDetail(updated)
        setStaleDraft(false)
        return true
      } catch (error) {
        if (error instanceof ApiError) {
          if (error.code === 'stale_revision') {
            setStaleDraft(true)
          } else if (error.status === 422 || error.status === 404) {
            setRowErrors((current) => ({ ...current, [command.stream_id]: error.message }))
          } else if (error.code === 'needs_attention' || error.code === 'operation_in_progress') {
            setOpError({ kind: 'blocked', message: error.message })
            await reload().catch(() => undefined)
          } else {
            setOpError({ kind: 'generic', message: error.message })
          }
        } else {
          setOpError({ kind: 'generic', message: 'The change could not be saved. Try again.' })
        }
        return false
      } finally {
        setOperation(null)
      }
    },
    [campaignId, detail, operation, reload],
  )

  const doFetch = useCallback(
    async (confirmDiscard: boolean) => {
      if (operation) {
        return
      }
      setOperation('fetch')
      setOpError(null)
      setOpNotices([])
      try {
        const result = await fetchCampaign(campaignId, confirmDiscard)
        setOpNotices(result.notices)
        await reload()
        setStaleDraft(false)
      } catch (error) {
        if (error instanceof ApiError) {
          if (error.code === 'dirty_draft') {
            setOpError({ kind: 'dirty_draft', message: error.message })
          } else {
            setOpError({ kind: 'generic', message: error.message })
          }
        } else {
          setOpError({ kind: 'generic', message: 'The campaign could not be fetched. Try again.' })
        }
      } finally {
        setOperation(null)
      }
    },
    [campaignId, operation, reload],
  )

  const doUpload = useCallback(async () => {
    if (operation) {
      return
    }
    setOperation('upload')
    setOpError(null)
    setOpNotices([])
    try {
      const result = await uploadCampaign(campaignId)
      setOpNotices(result.notices)
      await reload()
    } catch (error) {
      if (error instanceof ApiError) {
        switch (error.code) {
          case 'remote_conflict':
            setOpError({ kind: 'remote_conflict', message: error.message })
            break
          case 'clean_draft':
            setOpError({ kind: 'info', message: error.message })
            await reload().catch(() => undefined)
            break
          case 'invalid_draft':
            setOpError({ kind: 'validation', message: error.message })
            break
          case 'upload_failed':
            setOpError({
              kind: 'generic',
              message: `${error.message} Your changes are still saved locally.`,
            })
            break
          case 'upload_needs_attention':
          case 'needs_attention':
            setOpError({ kind: 'blocked', message: error.message })
            await reload().catch(() => undefined)
            break
          default:
            setOpError({ kind: 'generic', message: error.message })
        }
      } else {
        setOpError({ kind: 'generic', message: 'The upload could not be completed. Try again.' })
      }
    } finally {
      setOperation(null)
    }
  }, [campaignId, operation, reload])

  if (loadState === 'loading') {
    return <p role="status">Loading campaign…</p>
  }
  if (loadState === 'error' || !detail) {
    return (
      <div className="banner banner-error" role="alert">
        {loadError || 'The campaign could not be loaded.'}{' '}
        <a href="#/">Back to the campaign list</a>
      </div>
    )
  }

  const { campaign } = detail
  const busy = operation !== null
  const blocked = detail.upload_blocked
  const dirty = campaign.dirty
  const editingDisabled = busy || blocked || staleDraft
  const needsAttention = campaign.sync_state === 'needs_attention'
  const syncing = campaign.sync_state === 'syncing'
  const lastSync = lastSyncAt(campaign)
  const notices = [...new Set([...detail.notices, ...opNotices])]

  const primaryLabel =
    operation === 'upload'
      ? 'Uploading…'
      : operation === 'fetch'
        ? 'Fetching…'
        : dirty
          ? 'Upload streams to Keitaro'
          : 'Get streams from Keitaro'

  return (
    <section aria-labelledby="editor-heading">
      <p className="breadcrumb">
        <a href="#/">← Campaigns</a>
      </p>
      <div className="editor-header">
        <div>
          <h2 id="editor-heading">{campaign.name}</h2>
          <p className="editor-meta">
            Keitaro ID {campaign.keitaro_id}
            {campaign.country_code ? ` · ${campaign.country_code}` : ''} ·{' '}
            <SyncStateBadge state={campaign.sync_state} /> · Last synchronization:{' '}
            {lastSync ? formatDateTime(lastSync) : 'Never'}
          </p>
        </div>
        <div className="editor-actions">
          <button
            type="button"
            className="primary"
            disabled={busy || blocked}
            onClick={dirty ? doUpload : () => doFetch(false)}
          >
            {primaryLabel}
          </button>
          {(!dirty || blocked) && (
            <ConfirmButton
              label="Hard reset from Kitaro"
              question="Discard all local changes and replace them with streams from Keitaro?"
              confirmLabel="Confirm hard reset"
              className="danger"
              disabled={busy}
              onConfirm={() => doFetch(true)}
            />
          )}
          {dirty && !blocked && (
            <ConfirmButton
              label="Discard changes and reload"
              question="Discard all local changes and reload from Keitaro?"
              confirmLabel="Discard and reload"
              className="danger"
              disabled={busy}
              onConfirm={() => doFetch(true)}
            />
          )}
        </div>
      </div>

      {staleDraft && (
        <div className="banner banner-warning" role="alert">
          This draft changed in another window or tab. Reload the latest draft before editing — your
          last change was not saved.{' '}
          <button
            type="button"
            onClick={() => {
              void reload()
                .then(() => setStaleDraft(false))
                .catch(() =>
                  setOpError({ kind: 'generic', message: 'The draft could not be reloaded.' }),
                )
            }}
          >
            Reload draft
          </button>
        </div>
      )}

      {needsAttention && (
        <div className="banner banner-error" role="alert">
          <span>
            A previous synchronization could not be completed safely. This campaign is blocked until
            it is reloaded from Keitaro, which replaces the local draft.
          </span>{' '}
          <ConfirmButton
            label="Reload from Keitaro"
            question="Replace the local draft with the current Keitaro state?"
            confirmLabel="Reload and replace draft"
            disabled={busy}
            onConfirm={() => doFetch(true)}
          />
        </div>
      )}

      {syncing && (
        <div className="banner banner-info" role="status">
          A synchronization operation is in progress for this campaign.{' '}
          <button type="button" disabled={busy} onClick={() => void reload()}>
            Refresh
          </button>
        </div>
      )}

      {opError && opError.kind === 'remote_conflict' && (
        <div className="banner banner-error" role="alert">
          <span>{opError.message} Your local changes are preserved.</span>{' '}
          <ConfirmButton
            label="Reload from Keitaro"
            question="Replace the local draft with the current Keitaro state? Local changes will be lost."
            confirmLabel="Reload and replace draft"
            disabled={busy}
            onConfirm={() => doFetch(true)}
          />
        </div>
      )}
      {opError && opError.kind === 'dirty_draft' && (
        <div className="banner banner-warning" role="alert">
          <span>{opError.message}</span>{' '}
          <ConfirmButton
            label="Discard changes and reload"
            question="Discard all local changes and reload from Keitaro?"
            confirmLabel="Discard and reload"
            disabled={busy}
            onConfirm={() => doFetch(true)}
          />
        </div>
      )}
      {opError && opError.kind !== 'remote_conflict' && opError.kind !== 'dirty_draft' && (
        <div
          className={opError.kind === 'info' ? 'banner banner-info' : 'banner banner-error'}
          role="alert"
        >
          {opError.message}
        </div>
      )}

      {notices.length > 0 && (
        <div className="banner banner-info notices">
          <strong>Notices</strong>
          <ul>
            {notices.map((notice) => (
              <li key={notice}>{notice}</li>
            ))}
          </ul>
        </div>
      )}

      <StreamsTable
        campaignId={campaignId}
        streams={detail.streams}
        disabled={editingDisabled}
        rowErrors={rowErrors}
        onCommand={sendCommand}
      />
    </section>
  )
}

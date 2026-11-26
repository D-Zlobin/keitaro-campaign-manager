import { useCallback, useEffect, useState } from 'react'
import { listCampaigns } from '../api/campaigns'
import type { CampaignSummary } from '../api/types'
import { ImportCampaignForm } from '../components/ImportCampaignForm'
import { NewCampaignDialog } from '../components/NewCampaignDialog'
import { SyncStateBadge } from '../components/SyncStateBadge'
import { formatDateTime, lastSyncAt } from '../format'
import { campaignEditorHref, navigateToEditor } from '../router'

type LoadState = 'loading' | 'error' | 'ready'

/** Main screen: locally known campaigns plus creation and import entries. */
export function CampaignListPage() {
  const [campaigns, setCampaigns] = useState<CampaignSummary[]>([])
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [dialogOpen, setDialogOpen] = useState(false)

  const load = useCallback((signal?: AbortSignal) => {
    listCampaigns(signal)
      .then(({ campaigns: list }) => {
        setCampaigns(list)
        setLoadState('ready')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') {
          return
        }
        setLoadState('error')
      })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  const openEditor = (campaign: CampaignSummary) => navigateToEditor(campaign.id)

  return (
    <section aria-labelledby="campaign-list-heading">
      <div className="page-actions">
        <h2 id="campaign-list-heading">Campaigns</h2>
        <button type="button" className="primary" onClick={() => setDialogOpen(true)}>
          New Campaign
        </button>
      </div>
      <ImportCampaignForm onImported={openEditor} />
      {loadState === 'loading' && <p role="status">Loading campaigns…</p>}
      {loadState === 'error' && (
        <div className="banner banner-error" role="alert">
          The campaign list could not be loaded.{' '}
          <button
            type="button"
            onClick={() => {
              setLoadState('loading')
              load()
            }}
          >
            Retry
          </button>
        </div>
      )}
      {loadState === 'ready' && campaigns.length === 0 && (
        <p className="empty-state">
          No campaigns yet. Create one or open an existing Keitaro campaign.
        </p>
      )}
      {loadState === 'ready' && campaigns.length > 0 && (
        <table className="campaign-table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Keitaro ID</th>
              <th scope="col">State</th>
              <th scope="col">Last synchronization</th>
            </tr>
          </thead>
          <tbody>
            {campaigns.map((campaign) => {
              const lastSync = lastSyncAt(campaign)
              return (
                <tr key={campaign.id} className="campaign-row" onClick={() => openEditor(campaign)}>
                  <td>
                    <a
                      href={campaignEditorHref(campaign.id)}
                      onClick={(event) => event.stopPropagation()}
                    >
                      {campaign.name}
                    </a>
                  </td>
                  <td>{campaign.keitaro_id}</td>
                  <td>
                    <SyncStateBadge state={campaign.sync_state} />
                  </td>
                  <td>{lastSync ? formatDateTime(lastSync) : 'Never'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {dialogOpen && (
        <NewCampaignDialog onClose={() => setDialogOpen(false)} onCreated={openEditor} />
      )}
    </section>
  )
}

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { errorResponse, jsonResponse, mockApi } from '../test/mockApi'
import { CAMPAIGN_ID, makeSummary } from '../test/fixtures'
import { CampaignListPage } from './CampaignListPage'

afterEach(() => {
  vi.unstubAllGlobals()
  window.location.hash = ''
})

describe('CampaignListPage', () => {
  it('lists campaigns with name, Keitaro ID, state, and last synchronization time', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/campaigns',
        handler: () =>
          jsonResponse({
            campaigns: [
              makeSummary({
                sync_state: 'dirty',
                dirty: true,
                last_upload_at: '2026-11-26T14:30:00',
              }),
            ],
          }),
      },
    ])
    render(<CampaignListPage />)

    expect(await screen.findByRole('link', { name: 'Example DE' })).toBeInTheDocument()
    expect(screen.getByText('500')).toBeInTheDocument()
    expect(screen.getByText('Dirty')).toBeInTheDocument()
    expect(screen.getByText('2026-11-26 14:30')).toBeInTheDocument()
  })

  it('shows an empty state when no campaigns are known', async () => {
    mockApi([
      { method: 'GET', path: '/api/v1/campaigns', handler: () => jsonResponse({ campaigns: [] }) },
    ])
    render(<CampaignListPage />)
    expect(await screen.findByText(/No campaigns yet/)).toBeInTheDocument()
  })

  it('shows a failure state with Retry when the list cannot be loaded', async () => {
    let calls = 0
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/campaigns',
        handler: () => {
          calls += 1
          return calls === 1
            ? errorResponse(500, 'internal_error', 'Internal server error.')
            : jsonResponse({ campaigns: [makeSummary()] })
        },
      },
    ])
    const user = userEvent.setup()
    render(<CampaignListPage />)

    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('link', { name: 'Example DE' })).toBeInTheDocument()
  })

  it('navigates to the editor when a campaign row is clicked', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/campaigns',
        handler: () => jsonResponse({ campaigns: [makeSummary()] }),
      },
    ])
    const user = userEvent.setup()
    render(<CampaignListPage />)

    await user.click(await screen.findByRole('link', { name: 'Example DE' }))
    expect(window.location.hash).toBe(`#/campaigns/${CAMPAIGN_ID}`)
  })

  it('imports a campaign by Keitaro ID and opens its editor', async () => {
    mockApi([
      { method: 'GET', path: '/api/v1/campaigns', handler: () => jsonResponse({ campaigns: [] }) },
      {
        method: 'POST',
        path: '/api/v1/campaigns/import',
        handler: ({ body }) => {
          expect(body).toEqual({ keitaro_campaign_id: 4242 })
          return jsonResponse({ campaign: makeSummary(), notices: [] }, 201)
        },
      },
    ])
    const user = userEvent.setup()
    render(<CampaignListPage />)

    await user.type(screen.getByLabelText('Open by Keitaro campaign ID'), '4242')
    await user.click(screen.getByRole('button', { name: 'Open' }))

    await vi.waitFor(() => expect(window.location.hash).toBe(`#/campaigns/${CAMPAIGN_ID}`))
  })

  it('validates the import ID locally before calling the API', async () => {
    const mock = mockApi([
      { method: 'GET', path: '/api/v1/campaigns', handler: () => jsonResponse({ campaigns: [] }) },
    ])
    const user = userEvent.setup()
    render(<CampaignListPage />)

    await user.type(screen.getByLabelText('Open by Keitaro campaign ID'), 'abc')
    await user.click(screen.getByRole('button', { name: 'Open' }))

    expect(await screen.findByText(/positive numeric Keitaro campaign ID/)).toBeInTheDocument()
    expect(
      mock.mock.calls.filter((call) => (call[1] as RequestInit)?.method === 'POST'),
    ).toHaveLength(0)
  })

  it('shows the server error when the import is rejected (e.g. duplicate)', async () => {
    mockApi([
      { method: 'GET', path: '/api/v1/campaigns', handler: () => jsonResponse({ campaigns: [] }) },
      {
        method: 'POST',
        path: '/api/v1/campaigns/import',
        handler: () =>
          errorResponse(409, 'campaign_exists', 'This Keitaro campaign is already imported.'),
      },
    ])
    const user = userEvent.setup()
    render(<CampaignListPage />)

    await user.type(screen.getByLabelText('Open by Keitaro campaign ID'), '4242')
    await user.click(screen.getByRole('button', { name: 'Open' }))

    expect(
      await screen.findByText('This Keitaro campaign is already imported.'),
    ).toBeInTheDocument()
    expect(window.location.hash).toBe('')
  })
})

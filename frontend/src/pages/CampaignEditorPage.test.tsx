import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CampaignDetail } from '../api/types'
import { deferred, errorResponse, jsonResponse, mockApi } from '../test/mockApi'
import type { MockRoute } from '../test/mockApi'
import {
  CAMPAIGN_ID,
  makeDetail,
  makeGoogleStream,
  makeOfferStream,
  makeSummary,
} from '../test/fixtures'
import { CampaignEditorPage } from './CampaignEditorPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

function detailRoute(detail: CampaignDetail): MockRoute {
  return { method: 'GET', path: '/api/v1/campaigns/:id', handler: () => jsonResponse(detail) }
}

function renderEditor(detail: CampaignDetail, extraRoutes: MockRoute[] = []) {
  const mock = mockApi([detailRoute(detail), ...extraRoutes])
  render(<CampaignEditorPage campaignId={CAMPAIGN_ID} />)
  return mock
}

async function streamRow(name: string): Promise<HTMLElement> {
  const cell = await screen.findByText(name)
  const row = cell.closest('tr')
  expect(row).not.toBeNull()
  return row as HTMLElement
}

const dirtyDetail = makeDetail({
  campaign: makeSummary({ sync_state: 'dirty', dirty: true, revision: 2 }),
  streams: [makeGoogleStream({ state: 'disabled', share: 0 }), makeOfferStream({ share: 100 })],
})

describe('CampaignEditorPage — primary synchronization button', () => {
  it('shows "Get streams from Keitaro" for a clean draft', async () => {
    renderEditor(makeDetail())
    expect(await screen.findByRole('button', { name: 'Get streams from Keitaro' })).toBeEnabled()
  })

  it('shows "Upload streams to Keitaro" for a dirty draft', async () => {
    renderEditor(dirtyDetail)
    expect(await screen.findByRole('button', { name: 'Upload streams to Keitaro' })).toBeEnabled()
  })

  it('disables the button with a progress label while an operation runs', async () => {
    const pending = deferred<Response>()
    renderEditor(makeDetail(), [
      { method: 'POST', path: '/api/v1/campaigns/:id/fetch', handler: () => pending.promise },
    ])
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Get streams from Keitaro' }))
    expect(screen.getByRole('button', { name: 'Fetching…' })).toBeDisabled()

    pending.resolve(jsonResponse({ campaign: makeSummary(), notices: [] }))
    expect(await screen.findByRole('button', { name: 'Get streams from Keitaro' })).toBeEnabled()
  })

  it('disables upload and editing when synchronization needs attention', async () => {
    renderEditor(
      makeDetail({
        campaign: makeSummary({ sync_state: 'needs_attention', dirty: true }),
        upload_blocked: true,
      }),
    )

    expect(await screen.findByRole('button', { name: 'Upload streams to Keitaro' })).toBeDisabled()
    expect(screen.getByText(/blocked until it is reloaded from Keitaro/)).toBeInTheDocument()
    expect(
      within(await streamRow('Google — DE')).getByRole('button', { name: 'Delete' }),
    ).toBeDisabled()
    expect(
      within(await streamRow('Google — DE')).getByRole('button', { name: 'Pin' }),
    ).toBeDisabled()
  })
})

describe('CampaignEditorPage — streams table', () => {
  it('renders disabled rows distinctly with a badge, zero share, and Restore', async () => {
    renderEditor(dirtyDetail)

    const row = await streamRow('Google — DE')
    await within(row).findByText('Disabled')
    expect(within(row).getByText('0%')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Restore' })).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: 'Pin' })).not.toBeInTheDocument()
    expect(row.className).toContain('stream-row-disabled')
  })

  it('marks an active zero-share stream as receiving no traffic', async () => {
    renderEditor(
      makeDetail({
        streams: [makeGoogleStream({ share: 0 }), makeOfferStream({ share: 100 })],
      }),
    )
    expect(
      await within(await streamRow('Google — DE')).findByText('no traffic'),
    ).toBeInTheDocument()
  })

  it('sends pin commands with the current revision; pin-only changes stay clean', async () => {
    const pinned = makeDetail({
      streams: [makeGoogleStream({ pinned: true }), makeOfferStream()],
    })
    const mock = renderEditor(makeDetail(), [
      {
        method: 'PUT',
        path: '/api/v1/campaigns/:id/draft',
        handler: ({ body }) => {
          expect(body).toEqual({
            expected_revision: 1,
            command: { type: 'pin_stream', stream_id: 9101 },
          })
          return jsonResponse(pinned)
        },
      },
    ])
    const user = userEvent.setup()

    await user.click(within(await streamRow('Google — DE')).getByRole('button', { name: 'Pin' }))

    expect(await within(await streamRow('Google — DE')).findByText('Pinned')).toBeInTheDocument()
    expect(
      within(await streamRow('Google — DE')).getByRole('button', { name: 'Unpin' }),
    ).toBeInTheDocument()
    // Pin metadata alone is not Keitaro-managed: the draft stays clean.
    expect(screen.getByRole('button', { name: 'Get streams from Keitaro' })).toBeInTheDocument()
    expect(mock.mock.calls.some((call) => (call[1] as RequestInit)?.method === 'PUT')).toBe(true)
  })

  it('deletes a stream through the draft API and renders the authoritative result', async () => {
    renderEditor(makeDetail(), [
      {
        method: 'PUT',
        path: '/api/v1/campaigns/:id/draft',
        handler: ({ body }) => {
          expect(body).toEqual({
            expected_revision: 1,
            command: { type: 'delete_stream', stream_id: 9101 },
          })
          return jsonResponse(dirtyDetail)
        },
      },
    ])
    const user = userEvent.setup()

    await user.click(within(await streamRow('Google — DE')).getByRole('button', { name: 'Delete' }))

    expect(
      await screen.findByRole('button', { name: 'Upload streams to Keitaro' }),
    ).toBeInTheDocument()
    expect(within(await streamRow('Google — DE')).getByText('Disabled')).toBeInTheDocument()
    expect(within(await streamRow('Offer — 18001')).getByText('100%')).toBeInTheDocument()
  })

  it('shows rule violations next to the affected stream', async () => {
    renderEditor(
      makeDetail({
        streams: [makeGoogleStream({ state: 'disabled', share: 0 }), makeOfferStream()],
      }),
      [
        {
          method: 'PUT',
          path: '/api/v1/campaigns/:id/draft',
          handler: () =>
            errorResponse(
              422,
              'stream_rule_violation',
              'Cannot restore the stream: the shares of pinned streams leave no room.',
            ),
        },
      ],
    )
    const user = userEvent.setup()

    await user.click(
      within(await streamRow('Google — DE')).getByRole('button', { name: 'Restore' }),
    )

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/Cannot restore the stream/)
  })
})

describe('CampaignEditorPage — offer editing', () => {
  it('expands the offer list only for offer-capable streams', async () => {
    renderEditor(makeDetail())
    const user = userEvent.setup()

    // The redirect (Google) stream exposes no offer editing.
    expect(within(await streamRow('Google — DE')).getByText('—')).toBeInTheDocument()

    await user.click(
      within(await streamRow('Offer — 18001')).getByRole('button', { name: '1 offer' }),
    )
    expect(await screen.findByText('Alpha Diet Offer')).toBeInTheDocument()
    expect(screen.getByText('100%')).toBeInTheDocument()
  })

  it('adds an offer selected from the exclusion-scoped autocomplete', async () => {
    const beta = { offer_id: 18002, name: 'Beta Finance Offer' }
    const after = makeDetail({
      campaign: makeSummary({ sync_state: 'dirty', dirty: true, revision: 2 }),
      streams: [
        makeGoogleStream(),
        makeOfferStream({
          offers: [
            {
              offer_id: 18001,
              offer_name: 'Alpha Diet Offer',
              keitaro_association_id: 7001,
              state: 'active',
              share: 50,
            },
            {
              offer_id: 18002,
              offer_name: 'Beta Finance Offer',
              keitaro_association_id: null,
              state: 'active',
              share: 50,
            },
          ],
        }),
      ],
    })
    const mock = renderEditor(makeDetail(), [
      { method: 'GET', path: '/api/v1/offers', handler: () => jsonResponse({ offers: [beta] }) },
      {
        method: 'PUT',
        path: '/api/v1/campaigns/:id/draft',
        handler: ({ body }) => {
          expect(body).toEqual({
            expected_revision: 1,
            command: { type: 'add_offer', stream_id: 9102, offer_id: 18002 },
          })
          return jsonResponse(after)
        },
      },
    ])
    const user = userEvent.setup()

    await user.click(
      within(await streamRow('Offer — 18001')).getByRole('button', { name: '1 offer' }),
    )
    await user.type(
      screen.getByRole('combobox', { name: 'Search offers to add to Offer — 18001' }),
      'beta',
    )
    await user.click(await screen.findByRole('option', { name: /Beta Finance Offer/ }))
    await user.click(screen.getByRole('button', { name: 'Add offer' }))

    expect(await screen.findByText('Beta Finance Offer')).toBeInTheDocument()
    expect(
      within(await streamRow('Offer — 18001')).getByRole('button', { name: '2 offers' }),
    ).toBeInTheDocument()

    const search = mock.mock.calls.find((call) => String(call[0]).includes('/api/v1/offers'))
    const url = new URL(String(search![0]), 'http://localhost')
    expect(url.searchParams.get('campaign_id')).toBe(CAMPAIGN_ID)
    expect(url.searchParams.get('stream_id')).toBe('9102')
  })

  it('removes an offer through the draft API', async () => {
    const after = makeDetail({
      campaign: makeSummary({ sync_state: 'dirty', dirty: true, revision: 2 }),
      streams: [makeGoogleStream(), makeOfferStream({ offers: [] })],
    })
    renderEditor(makeDetail(), [
      {
        method: 'PUT',
        path: '/api/v1/campaigns/:id/draft',
        handler: ({ body }) => {
          expect(body).toEqual({
            expected_revision: 1,
            command: { type: 'remove_offer', stream_id: 9102, offer_id: 18001 },
          })
          return jsonResponse(after)
        },
      },
    ])
    const user = userEvent.setup()

    await user.click(
      within(await streamRow('Offer — 18001')).getByRole('button', { name: '1 offer' }),
    )
    await user.click(await screen.findByRole('button', { name: 'Remove' }))

    expect(await screen.findByText('No active offers in this stream.')).toBeInTheDocument()
  })
})

describe('CampaignEditorPage — fetch, discard, and conflicts', () => {
  it('fetches from Keitaro and shows normalization notices', async () => {
    let gets = 0
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/campaigns/:id',
        handler: () => {
          gets += 1
          return jsonResponse(
            gets === 1
              ? makeDetail()
              : makeDetail({ notices: ['Stream 9103 was deleted remotely and was not restored.'] }),
          )
        },
      },
      {
        method: 'POST',
        path: '/api/v1/campaigns/:id/fetch',
        handler: ({ body }) => {
          expect(body).toEqual({ confirm_discard: false })
          return jsonResponse({
            campaign: makeSummary(),
            notices: ['Stream 9103 was deleted remotely and was not restored.'],
          })
        },
      },
    ])
    render(<CampaignEditorPage campaignId={CAMPAIGN_ID} />)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Get streams from Keitaro' }))
    expect(
      await screen.findByText('Stream 9103 was deleted remotely and was not restored.'),
    ).toBeInTheDocument()
  })

  it('handles a dirty-draft fetch conflict with an explicit discard confirmation', async () => {
    let fetches = 0
    renderEditor(makeDetail(), [
      {
        method: 'POST',
        path: '/api/v1/campaigns/:id/fetch',
        handler: ({ body }) => {
          fetches += 1
          const confirm = (body as { confirm_discard: boolean }).confirm_discard
          if (fetches === 1) {
            expect(confirm).toBe(false)
            return errorResponse(409, 'dirty_draft', 'The local draft has unsynchronized changes.')
          }
          expect(confirm).toBe(true)
          return jsonResponse({ campaign: makeSummary(), notices: [] })
        },
      },
    ])
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Get streams from Keitaro' }))
    expect(
      await screen.findByText('The local draft has unsynchronized changes.'),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Discard changes and reload' }))
    await user.click(screen.getByRole('button', { name: 'Discard and reload' }))
    await vi.waitFor(() => expect(fetches).toBe(2))
  })

  it('asks for a reload instead of retrying over another tab’s edits (stale revision)', async () => {
    let gets = 0
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/campaigns/:id',
        handler: () => {
          gets += 1
          return jsonResponse(gets === 1 ? makeDetail() : dirtyDetail)
        },
      },
      {
        method: 'PUT',
        path: '/api/v1/campaigns/:id/draft',
        handler: () =>
          errorResponse(409, 'stale_revision', 'The local draft changed in another request.', {
            expected_revision: 1,
            actual_revision: 2,
          }),
      },
    ])
    render(<CampaignEditorPage campaignId={CAMPAIGN_ID} />)
    const user = userEvent.setup()

    await user.click(within(await streamRow('Google — DE')).getByRole('button', { name: 'Delete' }))

    expect(await screen.findByText(/changed in another window or tab/)).toBeInTheDocument()
    // The rejected command changed nothing locally; the draft stays as loaded.
    expect(screen.getByRole('button', { name: 'Get streams from Keitaro' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Reload draft' }))
    await vi.waitFor(() =>
      expect(screen.queryByText(/changed in another window or tab/)).not.toBeInTheDocument(),
    )
    // The reloaded draft is the other tab's dirty state.
    expect(
      await screen.findByRole('button', { name: 'Upload streams to Keitaro' }),
    ).toBeInTheDocument()
    expect(gets).toBe(2)
  })

  it('shows a remote conflict on upload, preserves the draft, and reloads only after confirmation', async () => {
    let fetches = 0
    renderEditor(dirtyDetail, [
      {
        method: 'POST',
        path: '/api/v1/campaigns/:id/upload',
        handler: () =>
          errorResponse(
            409,
            'remote_conflict',
            'The campaign changed in Keitaro. Reload it before uploading again.',
          ),
      },
      {
        method: 'POST',
        path: '/api/v1/campaigns/:id/fetch',
        handler: ({ body }) => {
          fetches += 1
          expect((body as { confirm_discard: boolean }).confirm_discard).toBe(true)
          return jsonResponse({ campaign: makeSummary(), notices: [] })
        },
      },
    ])
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Upload streams to Keitaro' }))

    expect(await screen.findByText(/changed in Keitaro/)).toBeInTheDocument()
    expect(screen.getByText(/local changes are preserved/)).toBeInTheDocument()
    // The draft is untouched: the primary action is still Upload.
    expect(screen.getByRole('button', { name: 'Upload streams to Keitaro' })).toBeInTheDocument()
    expect(fetches).toBe(0)

    await user.click(screen.getByRole('button', { name: 'Reload from Keitaro' }))
    await user.click(screen.getByRole('button', { name: 'Reload and replace draft' }))
    await vi.waitFor(() => expect(fetches).toBe(1))
  })

  it('recovers from needs_attention only through a confirmed reload', async () => {
    let fetches = 0
    renderEditor(
      makeDetail({
        campaign: makeSummary({ sync_state: 'needs_attention', dirty: true }),
        upload_blocked: true,
      }),
      [
        {
          method: 'POST',
          path: '/api/v1/campaigns/:id/fetch',
          handler: ({ body }) => {
            fetches += 1
            expect((body as { confirm_discard: boolean }).confirm_discard).toBe(true)
            return jsonResponse({ campaign: makeSummary(), notices: [] })
          },
        },
      ],
    )
    const user = userEvent.setup()

    await screen.findByText(/blocked until it is reloaded from Keitaro/)
    await user.click(screen.getByRole('button', { name: 'Reload from Keitaro' }))
    expect(fetches).toBe(0)
    await user.click(screen.getByRole('button', { name: 'Reload and replace draft' }))
    await vi.waitFor(() => expect(fetches).toBe(1))
  })

  it('reports a failed upload whose rollback succeeded and keeps the draft dirty', async () => {
    renderEditor(dirtyDetail, [
      {
        method: 'POST',
        path: '/api/v1/campaigns/:id/upload',
        handler: () =>
          errorResponse(
            502,
            'upload_failed',
            'Keitaro rejected the stream update. The rollback succeeded.',
          ),
      },
    ])
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Upload streams to Keitaro' }))

    expect(await screen.findByText(/rollback succeeded/)).toBeInTheDocument()
    expect(screen.getByText(/still saved locally/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Upload streams to Keitaro' })).toBeEnabled()
  })
})

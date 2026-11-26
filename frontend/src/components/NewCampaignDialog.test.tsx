import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { deferred, errorResponse, headerValue, jsonResponse, mockApi } from '../test/mockApi'
import { makeSummary } from '../test/fixtures'
import { NewCampaignDialog } from './NewCampaignDialog'

const GEOS = [
  { code: 'DE', name: 'Germany' },
  { code: 'CL', name: 'Chile' },
]
const ALPHA = { offer_id: 18001, name: 'Alpha Diet Offer' }

afterEach(() => {
  vi.unstubAllGlobals()
})

function geoRoute(body: unknown = { geos: GEOS }) {
  return { method: 'GET' as const, path: '/api/v1/geos', handler: () => jsonResponse(body) }
}

function offerRoute(body: unknown = { offers: [ALPHA] }) {
  return { method: 'GET' as const, path: '/api/v1/offers', handler: () => jsonResponse(body) }
}

function setup() {
  const onClose = vi.fn()
  const onCreated = vi.fn()
  render(<NewCampaignDialog onClose={onClose} onCreated={onCreated} />)
  return { onClose, onCreated }
}

async function fillValidForm(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Name'), 'Example DE')
  await screen.findByRole('option', { name: 'Germany (DE)' })
  await user.selectOptions(screen.getByLabelText('Geo'), 'DE')
  await user.type(screen.getByLabelText('Offer'), 'alpha')
  await user.click(await screen.findByRole('option', { name: /Alpha Diet Offer/ }))
}

describe('NewCampaignDialog', () => {
  it('loads Geo options from the API with a loading state', async () => {
    const pending = deferred<Response>()
    mockApi([{ method: 'GET', path: '/api/v1/geos', handler: () => pending.promise }, offerRoute()])
    setup()

    const geoSelect = screen.getByLabelText('Geo')
    expect(geoSelect).toBeDisabled()
    expect(within(geoSelect).getByText('Loading countries…')).toBeInTheDocument()

    pending.resolve(jsonResponse({ geos: GEOS }))
    expect(await screen.findByRole('option', { name: 'Germany (DE)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Chile (CL)' })).toBeInTheDocument()
    expect(geoSelect).toBeEnabled()
  })

  it('shows a Geo failure state with a working Retry for transient errors', async () => {
    let calls = 0
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/geos',
        handler: () => {
          calls += 1
          return calls === 1
            ? errorResponse(502, 'upstream_error', 'The Geo list service returned an error.')
            : jsonResponse({ geos: GEOS })
        },
      },
      offerRoute(),
    ])
    const user = userEvent.setup()
    setup()

    expect(await screen.findByText(/country list could not be loaded/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('option', { name: 'Germany (DE)' })).toBeInTheDocument()
  })

  it('shows the server message without Retry when the catalog is not configured (503)', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/geos',
        handler: () =>
          errorResponse(
            503,
            'service_unavailable',
            'The offer and Geo catalog integration is not configured on this backend.',
          ),
      },
      offerRoute(),
    ])
    setup()

    expect(await screen.findByText(/catalog integration is not configured/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('shows an empty state when no countries are available', async () => {
    mockApi([geoRoute({ geos: [] }), offerRoute()])
    setup()
    expect(await screen.findByText('No countries available')).toBeInTheDocument()
    expect(screen.getByLabelText('Geo')).toBeDisabled()
  })

  it('validates all fields before submitting and creates nothing remotely', async () => {
    const mock = mockApi([geoRoute(), offerRoute()])
    const user = userEvent.setup()
    setup()

    await screen.findByRole('option', { name: 'Germany (DE)' })
    await user.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText('Enter a campaign name.')).toBeInTheDocument()
    expect(screen.getByText('Select a country.')).toBeInTheDocument()
    expect(screen.getByText('Select an offer from the search results.')).toBeInTheDocument()
    expect(
      mock.mock.calls.filter((call) => (call[1] as RequestInit)?.method === 'POST'),
    ).toHaveLength(0)
  })

  it('submits the normalized code and the selected Keitaro offer ID with an idempotency key', async () => {
    const mock = mockApi([
      geoRoute(),
      offerRoute(),
      {
        method: 'POST',
        path: '/api/v1/campaigns',
        handler: ({ init, body }) => {
          expect(body).toEqual({ name: 'Example DE', country_code: 'DE', offer_id: 18001 })
          expect(headerValue(init, 'Idempotency-Key')).toBeTruthy()
          return jsonResponse({ campaign: makeSummary(), notices: [] }, 201)
        },
      },
    ])
    const user = userEvent.setup()
    const { onCreated } = setup()

    await fillValidForm(user)
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith(makeSummary()))
    const post = mock.mock.calls.find((call) => (call[1] as RequestInit)?.method === 'POST')
    expect(headerValue(post![1] as RequestInit, 'Idempotency-Key')).toBeTruthy()
  })

  it('reuses the same idempotency key when a failed submission is retried', async () => {
    const keys: (string | undefined)[] = []
    let attempts = 0
    mockApi([
      geoRoute(),
      offerRoute(),
      {
        method: 'POST',
        path: '/api/v1/campaigns',
        handler: ({ init }) => {
          attempts += 1
          keys.push(headerValue(init, 'Idempotency-Key'))
          return attempts === 1
            ? errorResponse(504, 'upstream_timeout', 'Keitaro did not respond in time.')
            : jsonResponse({ campaign: makeSummary(), notices: [] }, 201)
        },
      },
    ])
    const user = userEvent.setup()
    const { onCreated } = setup()

    await fillValidForm(user)
    await user.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText('Keitaro did not respond in time.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Create' }))
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalled())

    expect(attempts).toBe(2)
    expect(keys[0]).toBeTruthy()
    expect(keys[0]).toBe(keys[1])
  })

  it('keeps the dialog open and shows the server message when creation fails', async () => {
    mockApi([
      geoRoute(),
      offerRoute(),
      {
        method: 'POST',
        path: '/api/v1/campaigns',
        handler: () =>
          errorResponse(502, 'upstream_error', 'The offer directory returned an error.'),
      },
    ])
    const user = userEvent.setup()
    const { onCreated, onClose } = setup()

    await fillValidForm(user)
    await user.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText('The offer directory returned an error.')).toBeInTheDocument()
    expect(onCreated).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })
})

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { OfferOption } from '../api/types'
import { deferred, errorResponse, jsonResponse, mockApi } from '../test/mockApi'
import { OfferAutocomplete } from './OfferAutocomplete'

const ALPHA: OfferOption = { offer_id: 18001, name: 'Alpha Diet Offer' }
const BETA: OfferOption = { offer_id: 18002, name: 'Beta Finance Offer' }

afterEach(() => {
  vi.unstubAllGlobals()
})

function setup(props: Partial<Parameters<typeof OfferAutocomplete>[0]> = {}) {
  const onSelect = vi.fn()
  render(<OfferAutocomplete inputId="offer" onSelect={onSelect} {...props} />)
  return { onSelect, input: screen.getByRole('combobox') }
}

describe('OfferAutocomplete', () => {
  it('shows a loading state while searching, then the results', async () => {
    const pending = deferred<Response>()
    mockApi([{ method: 'GET', path: '/api/v1/offers', handler: () => pending.promise }])
    const user = userEvent.setup()
    const { input } = setup()

    await user.type(input, 'alpha')
    expect(await screen.findByText('Searching offers…')).toBeInTheDocument()

    pending.resolve(jsonResponse({ offers: [ALPHA] }))
    expect(await screen.findByRole('option', { name: /Alpha Diet Offer/ })).toBeInTheDocument()
  })

  it('requires selecting a result: free text alone never produces a selection', async () => {
    mockApi([
      { method: 'GET', path: '/api/v1/offers', handler: () => jsonResponse({ offers: [ALPHA] }) },
    ])
    const user = userEvent.setup()
    const { onSelect, input } = setup()

    await user.type(input, 'alpha')
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('submits the resolved offer (with its Keitaro ID) on selection', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/offers',
        handler: () => jsonResponse({ offers: [ALPHA, BETA] }),
      },
    ])
    const user = userEvent.setup()
    const { onSelect, input } = setup()

    await user.type(input, 'offer')
    await user.click(await screen.findByRole('option', { name: /Beta Finance Offer/ }))

    expect(onSelect).toHaveBeenCalledWith(BETA)
    expect(input).toHaveValue('Beta Finance Offer')
  })

  it('supports keyboard selection with ArrowDown and Enter', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/offers',
        handler: () => jsonResponse({ offers: [ALPHA, BETA] }),
      },
    ])
    const user = userEvent.setup()
    const { onSelect, input } = setup()

    await user.type(input, 'offer')
    await screen.findByRole('listbox')
    await user.keyboard('{ArrowDown}{Enter}')

    expect(onSelect).toHaveBeenCalledWith(BETA)
  })

  it('shows a no-results state for empty searches', async () => {
    mockApi([
      { method: 'GET', path: '/api/v1/offers', handler: () => jsonResponse({ offers: [] }) },
    ])
    const user = userEvent.setup()
    const { input } = setup()

    await user.type(input, 'zzz')
    expect(await screen.findByText('No offers match your search.')).toBeInTheDocument()
  })

  it('shows a failure state and recovers via Retry', async () => {
    let calls = 0
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/offers',
        handler: () => {
          calls += 1
          return calls === 1
            ? errorResponse(
                502,
                'upstream_error',
                'The offer autocomplete service returned an error.',
              )
            : jsonResponse({ offers: [ALPHA] })
        },
      },
    ])
    const user = userEvent.setup()
    const { input } = setup()

    await user.type(input, 'alpha')
    expect(await screen.findByText(/Offer search failed\./)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('option', { name: /Alpha Diet Offer/ })).toBeInTheDocument()
  })

  it('shows the server message without Retry when the catalog is not configured (503)', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/offers',
        handler: () =>
          errorResponse(
            503,
            'service_unavailable',
            'The offer and Geo catalog integration is not configured on this backend.',
          ),
      },
    ])
    const user = userEvent.setup()
    const { input } = setup()

    await user.type(input, 'alpha')
    expect(await screen.findByText(/catalog integration is not configured/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('passes the editing-stream context so active offers are excluded server-side', async () => {
    const mock = mockApi([
      { method: 'GET', path: '/api/v1/offers', handler: () => jsonResponse({ offers: [] }) },
    ])
    const user = userEvent.setup()
    const { input } = setup({ context: { campaignId: 'abc-123', streamId: 9102 } })

    await user.type(input, 'alpha')
    await screen.findByText('No offers match your search.')

    const url = new URL(String(mock.mock.calls[0][0]), 'http://localhost')
    expect(url.searchParams.get('campaign_id')).toBe('abc-123')
    expect(url.searchParams.get('stream_id')).toBe('9102')
  })

  it('clears the selection when the text is edited afterwards', async () => {
    mockApi([
      { method: 'GET', path: '/api/v1/offers', handler: () => jsonResponse({ offers: [ALPHA] }) },
    ])
    const user = userEvent.setup()
    const { onSelect, input } = setup()

    await user.type(input, 'alpha')
    await user.click(await screen.findByRole('option', { name: /Alpha Diet Offer/ }))
    expect(onSelect).toHaveBeenLastCalledWith(ALPHA)

    await user.type(input, 'x')
    expect(onSelect).toHaveBeenLastCalledWith(null)
  })
})

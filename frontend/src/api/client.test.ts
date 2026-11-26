import { afterEach, describe, expect, it, vi } from 'vitest'
import { errorResponse, headerValue, jsonResponse, mockApi } from '../test/mockApi'
import { makeDetail, makeSummary } from '../test/fixtures'
import { createCampaign, fetchCampaign, searchOffers, updateCampaignDraft } from './campaigns'
import { apiRequest, ApiError } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
})

async function captureError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError)
    return error as ApiError
  }
  throw new Error('Expected the request to fail')
}

describe('apiRequest', () => {
  it('parses the error envelope into a typed ApiError', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/campaigns',
        handler: () =>
          errorResponse(409, 'remote_conflict', 'The campaign changed in Keitaro.', {
            keitaro_id: 500,
          }),
      },
    ])
    const error = await captureError(apiRequest('/campaigns'))
    expect(error.status).toBe(409)
    expect(error.code).toBe('remote_conflict')
    expect(error.message).toBe('The campaign changed in Keitaro.')
    expect(error.details).toEqual({ keitaro_id: 500 })
  })

  it('maps network failures to a safe error without internals', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('ECONNREFUSED 10.0.0.1')))
    const error = await captureError(apiRequest('/campaigns'))
    expect(error.code).toBe('network_error')
    expect(error.status).toBe(0)
    expect(error.message).not.toContain('ECONNREFUSED')
  })

  it('handles non-JSON error bodies with a generic envelope', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        json: () => Promise.reject(new SyntaxError('not json')),
      }),
    )
    const error = await captureError(apiRequest('/campaigns'))
    expect(error.status).toBe(502)
    expect(error.code).toBe('http_error')
  })
})

describe('campaign endpoints', () => {
  it('sends the Idempotency-Key header and the creation payload', async () => {
    const mock = mockApi([
      {
        method: 'POST',
        path: '/api/v1/campaigns',
        handler: ({ init, body }) => {
          expect(headerValue(init, 'Idempotency-Key')).toBe('key-abc-123')
          expect(body).toEqual({ name: 'Example DE', country_code: 'DE', offer_id: 18001 })
          return jsonResponse({ campaign: makeSummary(), notices: [] }, 201)
        },
      },
    ])
    const result = await createCampaign(
      { name: 'Example DE', country_code: 'DE', offer_id: 18001 },
      'key-abc-123',
    )
    expect(result.campaign.keitaro_id).toBe(500)
    expect(mock).toHaveBeenCalledTimes(1)
  })

  it('sends expected_revision and the command on draft updates', async () => {
    mockApi([
      {
        method: 'PUT',
        path: '/api/v1/campaigns/:id/draft',
        handler: ({ url, body }) => {
          expect(url.pathname).toBe('/api/v1/campaigns/abc-123/draft')
          expect(body).toEqual({
            expected_revision: 7,
            command: { type: 'add_offer', stream_id: 9102, offer_id: 18002 },
          })
          return jsonResponse(makeDetail())
        },
      },
    ])
    await updateCampaignDraft('abc-123', 7, { type: 'add_offer', stream_id: 9102, offer_id: 18002 })
  })

  it('sends the discard confirmation flag on fetch', async () => {
    mockApi([
      {
        method: 'POST',
        path: '/api/v1/campaigns/:id/fetch',
        handler: ({ body }) => {
          expect(body).toEqual({ confirm_discard: true })
          return jsonResponse({ campaign: makeSummary(), notices: ['Normalized'] })
        },
      },
    ])
    const result = await fetchCampaign('abc-123', true)
    expect(result.notices).toEqual(['Normalized'])
  })

  it('scopes offer search to the stream being edited', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/offers',
        handler: ({ url }) => {
          expect(url.searchParams.get('search')).toBe('alpha')
          expect(url.searchParams.get('campaign_id')).toBe('abc-123')
          expect(url.searchParams.get('stream_id')).toBe('9102')
          return jsonResponse({ offers: [] })
        },
      },
    ])
    await searchOffers('alpha', { campaignId: 'abc-123', streamId: 9102 })
  })
})

import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { jsonResponse, mockApi } from './test/mockApi'
import { CAMPAIGN_ID, makeDetail, makeSummary } from './test/fixtures'
import App from './App'

afterEach(() => {
  vi.unstubAllGlobals()
  window.location.hash = ''
})

describe('App', () => {
  it('renders the application heading and the campaign list', async () => {
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/campaigns',
        handler: () => jsonResponse({ campaigns: [makeSummary()] }),
      },
    ])
    render(<App />)
    expect(screen.getByRole('heading', { name: 'Keitaro Campaign Manager' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Example DE' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New Campaign' })).toBeInTheDocument()
  })

  it('routes #/campaigns/{id} to the editor', async () => {
    window.location.hash = `#/campaigns/${CAMPAIGN_ID}`
    mockApi([
      {
        method: 'GET',
        path: '/api/v1/campaigns/:id',
        handler: () => jsonResponse(makeDetail()),
      },
    ])
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Example DE' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Get streams from Keitaro' })).toBeInTheDocument()
  })
})

/**
 * Minimal hash-based routing: `#/` is the campaign list, `#/campaigns/{id}`
 * is the editor. Hash routes need no server-side fallback, so the built
 * frontend can be served as plain static files.
 */

import { useEffect, useState } from 'react'

export type Route = { name: 'list' } | { name: 'editor'; campaignId: string }

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, '')
  const match = /^\/campaigns\/([0-9a-fA-F-]{36})$/.exec(path)
  if (match) {
    return { name: 'editor', campaignId: match[1] }
  }
  return { name: 'list' }
}

export function campaignEditorHref(campaignId: string): string {
  return `#/campaigns/${campaignId}`
}

export function navigateToEditor(campaignId: string): void {
  window.location.hash = campaignEditorHref(campaignId)
}

export function useHashRoute(): Route {
  const [hash, setHash] = useState(() => window.location.hash)
  useEffect(() => {
    const onHashChange = () => setHash(window.location.hash)
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])
  return parseHash(hash)
}

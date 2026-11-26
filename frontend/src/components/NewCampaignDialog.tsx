import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { createCampaign, listGeos } from '../api/campaigns'
import type { CampaignSummary, GeoOption, OfferOption } from '../api/types'
import { Dialog } from './Dialog'
import { OfferAutocomplete } from './OfferAutocomplete'

interface NewCampaignDialogProps {
  onClose: () => void
  onCreated: (campaign: CampaignSummary) => void
}

type GeoLoadState = 'loading' | 'error' | 'ready'

interface FieldErrors {
  name?: string
  geo?: string
  offer?: string
}

/**
 * Compact creation dialog: exactly Name, Geo, and Offer plus a Create
 * action. Geo options come from `GET /geos`; the offer must be picked from
 * the autocomplete so a resolved Keitaro offer ID is submitted. The
 * idempotency key is stable for the dialog's lifetime, so retrying a failed
 * submission replays instead of duplicating.
 */
export function NewCampaignDialog({ onClose, onCreated }: NewCampaignDialogProps) {
  const [name, setName] = useState('')
  const [geoCode, setGeoCode] = useState('')
  const [offer, setOffer] = useState<OfferOption | null>(null)
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const [geos, setGeos] = useState<GeoOption[]>([])
  const [geoState, setGeoState] = useState<GeoLoadState>('loading')
  const [geoReload, setGeoReload] = useState(0)

  const idempotencyKey = useRef<string>(crypto.randomUUID())

  useEffect(() => {
    const controller = new AbortController()
    listGeos(controller.signal)
      .then(({ geos: options }) => {
        setGeos(options)
        setGeoState('ready')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') {
          return
        }
        setGeos([])
        setGeoState('error')
      })
    return () => controller.abort()
  }, [geoReload])

  const validate = (): FieldErrors => {
    const errors: FieldErrors = {}
    const trimmed = name.trim()
    if (!trimmed) {
      errors.name = 'Enter a campaign name.'
    } else if (trimmed.length > 255) {
      errors.name = 'The name must be 255 characters or fewer.'
    }
    if (!geoCode) {
      errors.geo = 'Select a country.'
    }
    if (!offer) {
      errors.offer = 'Select an offer from the search results.'
    }
    return errors
  }

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setSubmitError(null)
    const errors = validate()
    setFieldErrors(errors)
    if (Object.keys(errors).length > 0 || !offer) {
      return
    }
    setSubmitting(true)
    try {
      const result = await createCampaign(
        { name: name.trim(), country_code: geoCode, offer_id: offer.offer_id },
        idempotencyKey.current,
      )
      onCreated(result.campaign)
    } catch (error) {
      setSubmitError(
        error instanceof ApiError ? error.message : 'The campaign could not be created. Try again.',
      )
      setSubmitting(false)
    }
  }

  return (
    <Dialog title="New Campaign" onClose={onClose}>
      <form className="new-campaign-form" onSubmit={onSubmit} noValidate>
        {submitError && (
          <div className="banner banner-error" role="alert">
            {submitError}
          </div>
        )}
        <div className="field">
          <label htmlFor="new-campaign-name">Name</label>
          <input
            id="new-campaign-name"
            type="text"
            value={name}
            maxLength={255}
            disabled={submitting}
            aria-invalid={fieldErrors.name ? true : undefined}
            aria-describedby={fieldErrors.name ? 'new-campaign-name-error' : undefined}
            onChange={(event) => setName(event.target.value)}
          />
          {fieldErrors.name && (
            <p className="field-error" id="new-campaign-name-error" role="alert">
              {fieldErrors.name}
            </p>
          )}
        </div>
        <div className="field">
          <label htmlFor="new-campaign-geo">Geo</label>
          <select
            id="new-campaign-geo"
            value={geoCode}
            disabled={submitting || geoState !== 'ready' || geos.length === 0}
            aria-invalid={fieldErrors.geo ? true : undefined}
            aria-describedby={fieldErrors.geo ? 'new-campaign-geo-error' : undefined}
            onChange={(event) => setGeoCode(event.target.value)}
          >
            <option value="">
              {geoState === 'loading'
                ? 'Loading countries…'
                : geoState === 'error'
                  ? 'Country list unavailable'
                  : geos.length === 0
                    ? 'No countries available'
                    : 'Select a country…'}
            </option>
            {geos.map((geo) => (
              <option key={geo.code} value={geo.code}>
                {geo.name} ({geo.code})
              </option>
            ))}
          </select>
          {geoState === 'error' && (
            <p className="field-error" role="alert">
              The country list could not be loaded.{' '}
              <button
                type="button"
                onClick={() => {
                  setGeoState('loading')
                  setGeoReload((token) => token + 1)
                }}
              >
                Retry
              </button>
            </p>
          )}
          {fieldErrors.geo && (
            <p className="field-error" id="new-campaign-geo-error" role="alert">
              {fieldErrors.geo}
            </p>
          )}
        </div>
        <div className="field">
          <label htmlFor="new-campaign-offer">Offer</label>
          <OfferAutocomplete
            inputId="new-campaign-offer"
            disabled={submitting}
            invalid={Boolean(fieldErrors.offer)}
            ariaDescribedBy={fieldErrors.offer ? 'new-campaign-offer-error' : undefined}
            onSelect={(selected) => setOffer(selected)}
          />
          {fieldErrors.offer && (
            <p className="field-error" id="new-campaign-offer-error" role="alert">
              {fieldErrors.offer}
            </p>
          )}
        </div>
        <div className="dialog-actions">
          <button type="button" onClick={onClose} disabled={submitting}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={submitting}>
            {submitting ? 'Creating…' : 'Create'}
          </button>
        </div>
      </form>
    </Dialog>
  )
}

import { useState } from 'react'
import { ApiError } from '../api/client'
import { importCampaign } from '../api/campaigns'
import type { CampaignSummary } from '../api/types'

interface ImportCampaignFormProps {
  onImported: (campaign: CampaignSummary) => void
}

/** Import an existing Keitaro campaign by its numeric ID. */
export function ImportCampaignForm({ onImported }: ImportCampaignFormProps) {
  const [value, setValue] = useState('')
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setFieldError(null)
    setSubmitError(null)
    const parsed = Number(value.trim())
    if (!Number.isInteger(parsed) || parsed <= 0) {
      setFieldError('Enter a positive numeric Keitaro campaign ID.')
      return
    }
    setSubmitting(true)
    try {
      const result = await importCampaign(parsed)
      onImported(result.campaign)
    } catch (error) {
      setSubmitError(
        error instanceof ApiError
          ? error.message
          : 'The campaign could not be imported. Try again.',
      )
      setSubmitting(false)
    }
  }

  return (
    <form className="import-form" onSubmit={onSubmit} noValidate>
      <label htmlFor="import-campaign-id">Open by Keitaro campaign ID</label>
      <div className="import-row">
        <input
          id="import-campaign-id"
          type="text"
          inputMode="numeric"
          placeholder="e.g. 4242"
          value={value}
          disabled={submitting}
          aria-invalid={fieldError ? true : undefined}
          aria-describedby={fieldError ? 'import-campaign-id-error' : undefined}
          onChange={(event) => setValue(event.target.value)}
        />
        <button type="submit" disabled={submitting}>
          {submitting ? 'Opening…' : 'Open'}
        </button>
      </div>
      {fieldError && (
        <p className="field-error" id="import-campaign-id-error" role="alert">
          {fieldError}
        </p>
      )}
      {submitError && (
        <div className="banner banner-error" role="alert">
          {submitError}
        </div>
      )}
    </form>
  )
}

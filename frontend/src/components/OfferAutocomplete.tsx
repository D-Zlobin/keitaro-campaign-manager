import { useEffect, useId, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { searchOffers } from '../api/campaigns'
import type { OfferSearchContext } from '../api/campaigns'
import type { OfferOption } from '../api/types'

const SEARCH_DEBOUNCE_MS = 300

interface OfferAutocompleteProps {
  inputId: string
  placeholder?: string
  /** Accessible name when no visible <label> is associated with the input. */
  ariaLabel?: string
  /** When set, offers already active in that stream are excluded server-side. */
  context?: OfferSearchContext
  disabled?: boolean
  invalid?: boolean
  ariaDescribedBy?: string
  /** Called with the chosen offer, or null when the selection is cleared. */
  onSelect: (offer: OfferOption | null) => void
}

type SearchStatus = 'idle' | 'loading' | 'ready' | 'error'

interface SearchState {
  term: string
  status: 'loading' | 'ready' | 'error'
  results: OfferOption[]
  /** Server-provided explanation for non-retryable failures (e.g. 503). */
  errorMessage?: string
  errorRetryable?: boolean
}

/**
 * Search-as-you-type selector over `GET /offers` (Five API-backed). The
 * parent only ever receives a resolved offer (with its Keitaro offer ID) —
 * free text is never a valid selection.
 */
export function OfferAutocomplete({
  inputId,
  placeholder = 'Search offers…',
  ariaLabel,
  context,
  disabled = false,
  invalid = false,
  ariaDescribedBy,
  onSelect,
}: OfferAutocompleteProps) {
  const listboxId = useId()
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<OfferOption | null>(null)
  const [search, setSearch] = useState<SearchState | null>(null)
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [retryToken, setRetryToken] = useState(0)

  const contextRef = useRef(context)
  useEffect(() => {
    contextRef.current = context
  })
  const contextKey = context ? `${context.campaignId}:${context.streamId}` : ''

  // The searched term is empty while the input still shows the selection.
  const term = selected && query === selected.name ? '' : query.trim()
  const searching = term !== ''
  const current = search !== null && search.term === term ? search : null
  // A term with no settled search yet is loading — derived, so typing shows
  // immediate feedback without synchronous state updates in the effect.
  const status: SearchStatus = !searching ? 'idle' : current === null ? 'loading' : current.status
  const results = current !== null && current.status === 'ready' ? current.results : []

  useEffect(() => {
    if (!term) {
      return
    }
    const controller = new AbortController()
    const timer = setTimeout(() => {
      setSearch({ term, status: 'loading', results: [] })
      setActiveIndex(-1)
      searchOffers(term, contextRef.current, controller.signal)
        .then(({ offers }) => {
          setSearch({ term, status: 'ready', results: offers })
          setActiveIndex(offers.length > 0 ? 0 : -1)
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === 'AbortError') {
            return
          }
          // A 503 means the catalog integration is not configured — show the
          // server's explanation instead of a retry that cannot succeed.
          const unavailable = error instanceof ApiError && error.status === 503
          setSearch({
            term,
            status: 'error',
            results: [],
            errorMessage: unavailable ? error.message : undefined,
            errorRetryable: !unavailable,
          })
          setActiveIndex(-1)
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [term, contextKey, retryToken])

  const select = (offer: OfferOption) => {
    setSelected(offer)
    setQuery(offer.name)
    setOpen(false)
    setActiveIndex(-1)
    onSelect(offer)
  }

  const clearSelection = () => {
    setSelected(null)
    setQuery('')
    setOpen(false)
    onSelect(null)
  }

  const onChange = (value: string) => {
    setQuery(value)
    setOpen(true)
    if (selected && value !== selected.name) {
      setSelected(null)
      onSelect(null)
    }
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (status !== 'ready' || results.length === 0) {
        return
      }
      event.preventDefault()
      setOpen(true)
      const delta = event.key === 'ArrowDown' ? 1 : -1
      setActiveIndex((index) => (index + delta + results.length) % results.length)
    } else if (event.key === 'Enter') {
      if (open && status === 'ready' && activeIndex >= 0 && activeIndex < results.length) {
        event.preventDefault()
        select(results[activeIndex])
      }
    } else if (event.key === 'Escape') {
      setOpen(false)
    }
  }

  const showDropdown = open && searching

  return (
    <div className="offer-autocomplete">
      <div className="offer-input-row">
        <input
          id={inputId}
          type="text"
          role="combobox"
          aria-label={ariaLabel}
          aria-expanded={showDropdown}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={
            showDropdown && activeIndex >= 0 && activeIndex < results.length
              ? `${listboxId}-option-${results[activeIndex].offer_id}`
              : undefined
          }
          aria-invalid={invalid || undefined}
          aria-describedby={ariaDescribedBy}
          placeholder={placeholder}
          value={query}
          disabled={disabled}
          autoComplete="off"
          onChange={(event) => onChange(event.target.value)}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onKeyDown={onKeyDown}
        />
        {selected && !disabled && (
          <button
            type="button"
            className="offer-clear"
            aria-label="Clear selected offer"
            onClick={clearSelection}
          >
            ×
          </button>
        )}
      </div>
      {showDropdown && (
        <div className="offer-dropdown" onMouseDown={(event) => event.preventDefault()}>
          {status === 'loading' && (
            <div className="offer-status" role="status">
              Searching offers…
            </div>
          )}
          {status === 'error' && (
            <div className="offer-status offer-status-error" role="alert">
              {current?.errorMessage ?? 'Offer search failed.'}{' '}
              {current?.errorRetryable !== false && (
                <button type="button" onClick={() => setRetryToken((token) => token + 1)}>
                  Retry
                </button>
              )}
            </div>
          )}
          {status === 'ready' && results.length === 0 && (
            <div className="offer-status" role="status">
              No offers match your search.
            </div>
          )}
          {status === 'ready' && results.length > 0 && (
            <ul className="offer-results" role="listbox" id={listboxId} aria-label="Offer results">
              {results.map((offer, index) => (
                <li
                  key={offer.offer_id}
                  id={`${listboxId}-option-${offer.offer_id}`}
                  role="option"
                  aria-selected={index === activeIndex}
                  className={
                    index === activeIndex ? 'offer-option offer-option-active' : 'offer-option'
                  }
                  onMouseDown={(event) => {
                    event.preventDefault()
                    select(offer)
                  }}
                  onMouseEnter={() => setActiveIndex(index)}
                >
                  <span className="offer-option-name">{offer.name}</span>
                  <span className="offer-option-id">ID {offer.offer_id}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

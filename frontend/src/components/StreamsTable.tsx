import { Fragment, useState } from 'react'
import type { DraftCommand, DraftStream, OfferOption } from '../api/types'
import { OfferAutocomplete } from './OfferAutocomplete'

interface StreamsTableProps {
  campaignId: string
  streams: DraftStream[]
  /** True while an operation runs, synchronization is blocked, or the draft is stale. */
  disabled: boolean
  rowErrors: Record<number, string>
  /** Applies one draft command; resolves true when the server accepted it. */
  onCommand: (command: DraftCommand) => Promise<boolean>
}

function sortedStreams(streams: DraftStream[]): DraftStream[] {
  return [...streams].sort((a, b) => a.position - b.position || a.keitaro_id - b.keitaro_id)
}

function ShareValue({ share, active }: { share: number; active: boolean }) {
  return (
    <span className="share-value">
      <span className="share-number">{share}%</span>
      {active && share === 0 && (
        <span
          className="badge badge-zero"
          title="This active stream receives no weighted rotation traffic."
        >
          no traffic
        </span>
      )}
    </span>
  )
}

/** Streams of the draft with shares, pins, delete/restore, and offer editing. */
export function StreamsTable({
  campaignId,
  streams,
  disabled,
  rowErrors,
  onCommand,
}: StreamsTableProps) {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})

  const toggleExpanded = (streamId: number) =>
    setExpanded((current) => ({ ...current, [streamId]: !current[streamId] }))

  return (
    <table className="streams-table">
      <thead>
        <tr>
          <th scope="col">Stream</th>
          <th scope="col">Share</th>
          <th scope="col">Offers</th>
          <th scope="col">Actions</th>
        </tr>
      </thead>
      <tbody>
        {sortedStreams(streams).map((stream) => {
          const active = stream.state === 'active'
          const activeOffers = stream.offers.filter((offer) => offer.state === 'active')
          const isExpanded = Boolean(expanded[stream.keitaro_id])
          return (
            <Fragment key={stream.keitaro_id}>
              <tr className={active ? 'stream-row' : 'stream-row stream-row-disabled'}>
                <td>
                  <span className="stream-name">{stream.name}</span>
                  {!active && <span className="badge badge-disabled">Disabled</span>}
                </td>
                <td>
                  <ShareValue share={stream.share} active={active} />
                </td>
                <td>
                  {stream.offer_capable ? (
                    <button
                      type="button"
                      className="offers-toggle"
                      aria-expanded={isExpanded}
                      aria-controls={`offers-panel-${stream.keitaro_id}`}
                      onClick={() => toggleExpanded(stream.keitaro_id)}
                    >
                      {activeOffers.length} offer{activeOffers.length === 1 ? '' : 's'}
                    </button>
                  ) : (
                    <span className="offers-none">—</span>
                  )}
                </td>
                <td>
                  {active ? (
                    <button
                      type="button"
                      className="danger"
                      disabled={disabled}
                      onClick={() =>
                        onCommand({ type: 'delete_stream', stream_id: stream.keitaro_id })
                      }
                    >
                      Delete
                    </button>
                  ) : (
                    <button
                      type="button"
                      disabled={disabled}
                      onClick={() =>
                        onCommand({ type: 'restore_stream', stream_id: stream.keitaro_id })
                      }
                    >
                      Restore
                    </button>
                  )}
                </td>
              </tr>
              {rowErrors[stream.keitaro_id] && (
                <tr className="stream-error-row">
                  <td colSpan={4} role="alert">
                    {rowErrors[stream.keitaro_id]}
                  </td>
                </tr>
              )}
              {stream.offer_capable && isExpanded && (
                <tr className="offers-panel-row">
                  <td colSpan={4}>
                    <OfferPanel
                      campaignId={campaignId}
                      stream={stream}
                      disabled={disabled}
                      onCommand={onCommand}
                    />
                  </td>
                </tr>
              )}
            </Fragment>
          )
        })}
      </tbody>
    </table>
  )
}

interface OfferPanelProps {
  campaignId: string
  stream: DraftStream
  disabled: boolean
  onCommand: (command: DraftCommand) => Promise<boolean>
}

/** Expandable offer list of one offer-capable stream (spec section 6.8). */
function OfferPanel({ campaignId, stream, disabled, onCommand }: OfferPanelProps) {
  const [pendingOffer, setPendingOffer] = useState<OfferOption | null>(null)
  const [autocompleteKey, setAutocompleteKey] = useState(0)

  const addOffer = async () => {
    if (!pendingOffer) {
      return
    }
    const accepted = await onCommand({
      type: 'add_offer',
      stream_id: stream.keitaro_id,
      offer_id: pendingOffer.offer_id,
    })
    if (accepted) {
      setPendingOffer(null)
      setAutocompleteKey((key) => key + 1)
    }
  }

  return (
    <div className="offers-panel" id={`offers-panel-${stream.keitaro_id}`}>
      {stream.offers.length === 0 ? (
        <p className="offers-empty">No active offers in this stream.</p>
      ) : (
        <ul className="offer-list">
          {stream.offers.map((offer) => {
            const active = offer.state === 'active'
            return (
              <li
                key={offer.offer_id}
                className={active ? 'offer-list-item' : 'offer-list-item offer-list-item-disabled'}
              >
                <span className="offer-name">
                  {offer.offer_name}
                  {offer.pinned && <span className="badge badge-pinned">Pinned</span>}
                  {!active && <span className="badge badge-disabled">Disabled</span>}
                </span>
                <ShareValue share={offer.share} active={active} />
                {active ? (
                  <>
                    <button
                      type="button"
                      className="pin-button"
                      aria-pressed={offer.pinned}
                      disabled={disabled}
                      onClick={() =>
                        onCommand({
                          type: offer.pinned ? 'unpin_offer' : 'pin_offer',
                          stream_id: stream.keitaro_id,
                          offer_id: offer.offer_id,
                        })
                      }
                    >
                      {offer.pinned ? 'Unpin' : 'Pin'}
                    </button>
                    <button
                      type="button"
                      className="danger"
                      disabled={disabled}
                      onClick={() =>
                        onCommand({
                          type: 'remove_offer',
                          stream_id: stream.keitaro_id,
                          offer_id: offer.offer_id,
                        })
                      }
                    >
                      Remove
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() =>
                      onCommand({
                        type: 'add_offer',
                        stream_id: stream.keitaro_id,
                        offer_id: offer.offer_id,
                      })
                    }
                  >
                    Restore
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
      <div className="offer-add">
        <OfferAutocomplete
          key={autocompleteKey}
          inputId={`add-offer-${stream.keitaro_id}`}
          ariaLabel={`Search offers to add to ${stream.name}`}
          context={{ campaignId, streamId: stream.keitaro_id }}
          disabled={disabled}
          onSelect={setPendingOffer}
        />
        <button type="button" disabled={disabled || !pendingOffer} onClick={addOffer}>
          Add offer
        </button>
      </div>
    </div>
  )
}

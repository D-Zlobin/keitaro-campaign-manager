"""Typed domain errors (plan stage 2).

API error handlers (``app.errors``) translate these into the response
envelope: ``StreamNotFoundError``/``OfferNotFoundError`` map to 404, the rule
violations map to 422, and local revision conflicts (raised by the
repositories) map to 409. Domain code never raises HTTP-layer exceptions.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for campaign-rule failures."""


class StreamNotFoundError(DomainError):
    """A draft command referenced an unknown Keitaro stream ID."""

    def __init__(self, keitaro_id: int) -> None:
        super().__init__(f"Stream {keitaro_id} is not part of this draft")
        self.keitaro_id = keitaro_id


class OfferNotFoundError(DomainError):
    """A draft command referenced an unknown offer ID."""

    def __init__(self, offer_id: int) -> None:
        super().__init__(f"Offer {offer_id} is not associated with this stream")
        self.offer_id = offer_id


class StreamRuleViolationError(DomainError):
    """An operation would violate the stream share rules (spec section 6.7)."""


class OfferRuleViolationError(DomainError):
    """An operation would violate the offer editing rules (spec section 6.8)."""


class UnsupportedStreamEditError(DomainError):
    """Offer editing was attempted on a stream that is not offer-capable."""

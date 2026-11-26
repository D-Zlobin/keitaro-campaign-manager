"""Pure domain functions: share calculation, offer editing, canonicalization,
and update planning (plan stage 2). No I/O, no ORM, no HTTP — every function
operates on plain data and is independently testable.
"""

from app.domain.canonical import (
    SHA256_HEX_LENGTH,
    build_canonical_document,
    canonical_json,
    fingerprint_canonical,
    fingerprint_remote,
)
from app.domain.distribution import distribute_equal_shares
from app.domain.entries import (
    DraftAssociationState,
    DraftStreamState,
    OfferEntry,
    StreamEntry,
)
from app.domain.errors import (
    DomainError,
    OfferNotFoundError,
    OfferRuleViolationError,
    StreamNotFoundError,
    StreamRuleViolationError,
    UnsupportedStreamEditError,
)
from app.domain.offers import (
    add_offer,
    pin_offer,
    redistribute_offer_shares,
    remove_offer,
    unpin_offer,
    validate_active_stream_offers,
    validate_offer_share_invariants,
)
from app.domain.shares import (
    delete_stream,
    recalculate_shares,
    restore_stream,
    validate_share_invariants,
)
from app.domain.states import (
    BaselineSource,
    IdempotencyState,
    OfferAssociationState,
    StreamState,
    SyncState,
)
from app.domain.update_plan import (
    AssociationUpdate,
    StreamUpdate,
    is_dirty,
    plan_updates,
)

__all__ = [
    "AssociationUpdate",
    "BaselineSource",
    "DomainError",
    "DraftAssociationState",
    "DraftStreamState",
    "IdempotencyState",
    "OfferAssociationState",
    "OfferEntry",
    "OfferNotFoundError",
    "OfferRuleViolationError",
    "SHA256_HEX_LENGTH",
    "StreamEntry",
    "StreamNotFoundError",
    "StreamRuleViolationError",
    "StreamState",
    "StreamUpdate",
    "SyncState",
    "UnsupportedStreamEditError",
    "add_offer",
    "build_canonical_document",
    "canonical_json",
    "delete_stream",
    "distribute_equal_shares",
    "fingerprint_canonical",
    "fingerprint_remote",
    "is_dirty",
    "pin_offer",
    "plan_updates",
    "recalculate_shares",
    "redistribute_offer_shares",
    "remove_offer",
    "restore_stream",
    "unpin_offer",
    "validate_active_stream_offers",
    "validate_offer_share_invariants",
    "validate_share_invariants",
]

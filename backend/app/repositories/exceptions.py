"""Repository-layer exceptions, independent of the API error envelope."""


class RepositoryError(Exception):
    """Base class for persistence-layer failures."""


class CampaignNotFoundError(RepositoryError):
    def __init__(self, campaign_id: object) -> None:
        super().__init__(f"Campaign not found: {campaign_id!r}")
        self.campaign_id = campaign_id


class StaleRevisionError(RepositoryError):
    """The caller's ``expected_revision`` does not match the stored revision."""

    def __init__(self, campaign_id: object, expected: int, actual: int) -> None:
        super().__init__(
            f"Stale local revision for campaign {campaign_id!r}: "
            f"expected {expected}, actual {actual}"
        )
        self.campaign_id = campaign_id
        self.expected = expected
        self.actual = actual


class InvalidSyncTransitionError(RepositoryError):
    """Disallowed synchronization-state transition."""

    def __init__(self, campaign_id: object, from_state: object, to_state: object) -> None:
        super().__init__(
            f"Invalid sync-state transition for campaign {campaign_id!r}: "
            f"{from_state!r} -> {to_state!r}"
        )
        self.campaign_id = campaign_id
        self.from_state = from_state
        self.to_state = to_state

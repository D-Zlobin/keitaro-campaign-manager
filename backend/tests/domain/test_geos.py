"""ISO 3166-1 alpha-2 validation for campaign creation."""

from app.domain.geos import ISO_3166_1_ALPHA2, is_valid_country_code


def test_assigned_codes_are_valid() -> None:
    for code in ("DE", "US", "JP", "BR", "ZA", "AX", "SS", "GB"):
        assert is_valid_country_code(code), code


def test_unassigned_and_malformed_codes_are_rejected() -> None:
    # ZZ/XX are unassigned; UK is exceptionally reserved, not assigned.
    for code in ("ZZ", "XX", "UK", "AN", "D1", "1D", "", "DEU", "de"):
        assert not is_valid_country_code(code), code


def test_code_set_matches_iso_count() -> None:
    assert len(ISO_3166_1_ALPHA2) == 249
    assert all(len(code) == 2 and code.isupper() for code in ISO_3166_1_ALPHA2)

from __future__ import annotations

from datetime import date, datetime

import pytest

from price_parser import (
    CurrencyDeclaration,
    CurrencyResolver,
    EvidenceLevel,
    Price,
    ResolutionStatus,
    resolve_currency,
)

SITE_CA = "site-ca"
SITE_AU = "site-au"
SITE_US = "site-us"
STORE_CA = "store-ca"
STORE_AU = "store-au"
STORE_US = "store-us"
ITEM_1 = "item-1"
ITEM_2 = "item-2"


def declaration(
    declaration_id: str,
    currency: str,
    level: EvidenceLevel,
    **kwargs: object,
) -> CurrencyDeclaration:
    return CurrencyDeclaration(
        declaration_id=declaration_id,
        currency=currency,
        level=level,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def merchant_resolver() -> CurrencyResolver:
    return CurrencyResolver(
        [
            declaration(
                "site-default-ca",
                "CAD",
                EvidenceLevel.SITE,
                site_id=SITE_CA,
            ),
            declaration(
                "site-default-au",
                "AUD",
                EvidenceLevel.SITE,
                site_id=SITE_AU,
            ),
            declaration(
                "site-default-us",
                "USD",
                EvidenceLevel.SITE,
                site_id=SITE_US,
            ),
            declaration(
                "store-ca-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            ),
            declaration(
                "store-au-aud",
                "AUD",
                EvidenceLevel.STORE,
                store_id=STORE_AU,
            ),
            declaration(
                "store-us-usd",
                "USD",
                EvidenceLevel.STORE,
                store_id=STORE_US,
            ),
        ]
    )


def test_dollar_symbol_uses_store_declaration(
    merchant_resolver: CurrencyResolver,
) -> None:
    cases = [
        (STORE_CA, SITE_CA, "CAD", "store-ca-cad"),
        (STORE_AU, SITE_AU, "AUD", "store-au-aud"),
        (STORE_US, SITE_US, "USD", "store-us-usd"),
    ]
    for store_id, site_id, expected_currency, expected_id in cases:
        result = merchant_resolver.resolve("$", store_id=store_id, site_id=site_id)
        assert result.status is ResolutionStatus.RESOLVED
        assert result.currency == expected_currency
        assert result.evidence is not None
        assert result.evidence.declaration_id == expected_id
        assert result.is_suspended is False


def test_site_default_used_without_store_declaration() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "site-default-us",
                "USD",
                EvidenceLevel.SITE,
                site_id=SITE_US,
            )
        ]
    )
    result = resolver.resolve("$", site_id=SITE_US)
    assert result.status is ResolutionStatus.RESOLVED
    assert result.currency == "USD"
    assert result.evidence is not None
    assert result.evidence.declaration_id == "site-default-us"


def test_item_declaration_overrides_store_and_site() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "item-usd",
                "USD",
                EvidenceLevel.ITEM,
                item_id=ITEM_1,
            ),
            declaration(
                "store-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            ),
            declaration(
                "site-cad",
                "CAD",
                EvidenceLevel.SITE,
                site_id=SITE_CA,
            ),
        ]
    )
    result = resolver.resolve("$", item_id=ITEM_1, store_id=STORE_CA, site_id=SITE_CA)
    assert result.status is ResolutionStatus.RESOLVED
    assert result.currency == "USD"
    assert result.evidence is not None
    assert result.evidence.declaration_id == "item-usd"


def test_item_declaration_is_scoped_to_one_product() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "item-1-usd",
                "USD",
                EvidenceLevel.ITEM,
                item_id=ITEM_1,
            ),
            declaration(
                "store-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            ),
        ]
    )
    other = resolver.resolve("$", item_id=ITEM_2, store_id=STORE_CA, site_id=SITE_CA)
    assert other.currency == "CAD"
    assert other.evidence is not None
    assert other.evidence.declaration_id == "store-cad"


def test_store_declaration_does_not_leak_between_stores() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "store-ca-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            ),
        ]
    )
    result = resolver.resolve("$", store_id=STORE_US, site_id=SITE_US)
    assert result.status is ResolutionStatus.UNKNOWN
    assert result.currency is None
    assert result.evidence is None


def test_same_level_conflict_suspends_row() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "store-says-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            ),
            declaration(
                "store-says-usd",
                "USD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            ),
            declaration(
                "site-default",
                "USD",
                EvidenceLevel.SITE,
                site_id=SITE_CA,
            ),
        ]
    )
    result = resolver.resolve("$", store_id=STORE_CA, site_id=SITE_CA)
    assert result.status is ResolutionStatus.SUSPENDED
    assert result.is_suspended is True
    assert result.currency is None
    assert result.evidence is None
    assert {d.declaration_id for d in result.conflicts} == {
        "store-says-cad",
        "store-says-usd",
    }


def test_item_conflict_suspends_even_when_store_is_clear() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "item-a",
                "CAD",
                EvidenceLevel.ITEM,
                item_id=ITEM_1,
            ),
            declaration(
                "item-b",
                "USD",
                EvidenceLevel.ITEM,
                item_id=ITEM_1,
            ),
            declaration(
                "store-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            ),
        ]
    )
    result = resolver.resolve("$", item_id=ITEM_1, store_id=STORE_CA)
    assert result.status is ResolutionStatus.SUSPENDED
    assert result.evidence is None


def test_timed_store_declaration_respects_effective_window() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "store-old-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_from=date(2025, 1, 1),
                effective_until=date(2026, 6, 30),
            ),
            declaration(
                "store-new-usd",
                "USD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_from=date(2026, 7, 1),
            ),
        ]
    )
    old_quote = resolver.resolve("$", store_id=STORE_CA, as_of=date(2026, 5, 1))
    assert old_quote.currency == "CAD"
    assert old_quote.evidence is not None
    assert old_quote.evidence.declaration_id == "store-old-cad"

    new_quote = resolver.resolve("$", store_id=STORE_CA, as_of=date(2026, 11, 14))
    assert new_quote.currency == "USD"
    assert new_quote.evidence is not None
    assert new_quote.evidence.declaration_id == "store-new-usd"


def test_historical_page_ignores_todays_store_setting() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "store-old-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_until=date(2026, 10, 31),
            ),
            declaration(
                "store-today-usd",
                "USD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_from=date(2026, 11, 1),
            ),
        ]
    )
    historical = resolver.resolve("$", store_id=STORE_CA, as_of=date(2026, 9, 15))
    assert historical.currency == "CAD"
    assert historical.evidence is not None
    assert historical.evidence.declaration_id == "store-old-cad"

    boundary = resolver.resolve("$", store_id=STORE_CA, as_of=date(2026, 10, 31))
    assert boundary.currency == "CAD"

    switched = resolver.resolve("$", store_id=STORE_CA, as_of=date(2026, 11, 1))
    assert switched.currency == "USD"


def test_undated_lookup_ignores_timed_declarations() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "timed-usd",
                "USD",
                EvidenceLevel.STORE,
                store_id=STORE_US,
                effective_from=date(2026, 1, 1),
            ),
            declaration(
                "site-usd",
                "USD",
                EvidenceLevel.SITE,
                site_id=SITE_US,
            ),
        ]
    )
    result = resolver.resolve("$", store_id=STORE_US, site_id=SITE_US)
    assert result.currency == "USD"
    assert result.evidence is not None
    assert result.evidence.declaration_id == "site-usd"


def test_datetime_as_of_is_supported() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "store-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_from=date(2026, 1, 1),
            ),
        ]
    )
    result = resolver.resolve(
        "$",
        store_id=STORE_CA,
        as_of=datetime(2026, 5, 1, 12, 30),
    )
    assert result.currency == "CAD"


def test_recompute_shows_which_raw_statement_changed_conclusion() -> None:
    historical_registry = CurrencyResolver(
        [
            declaration(
                "store-declaration-v1",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_until=date(2026, 10, 31),
            )
        ]
    )
    current_registry = CurrencyResolver(
        [
            declaration(
                "store-declaration-v1",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_until=date(2026, 10, 31),
            ),
            declaration(
                "store-declaration-v2",
                "USD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_from=date(2026, 11, 1),
            ),
        ]
    )
    kwargs = {"symbol": "$", "store_id": STORE_CA, "as_of": date(2026, 11, 14)}
    before = historical_registry.resolve(**kwargs)  # type: ignore[arg-type]
    after = current_registry.resolve(**kwargs)  # type: ignore[arg-type]
    assert before.status is ResolutionStatus.UNKNOWN
    assert after.status is ResolutionStatus.RESOLVED
    assert after.evidence is not None
    assert after.evidence.declaration_id == "store-declaration-v2"

    historical_quote = current_registry.resolve(
        "$", store_id=STORE_CA, as_of=date(2026, 10, 1)
    )
    assert historical_quote.evidence is not None
    assert historical_quote.evidence.declaration_id == "store-declaration-v1"


def test_unambiguous_symbol_resolves_without_declarations() -> None:
    result = CurrencyResolver().resolve("€", store_id=STORE_CA)
    assert result.status is ResolutionStatus.RESOLVED
    assert result.currency == "EUR"
    assert result.evidence is None


def test_ambiguous_symbol_without_evidence_is_unknown() -> None:
    result = CurrencyResolver().resolve("$", store_id=STORE_CA)
    assert result.status is ResolutionStatus.UNKNOWN
    assert result.currency is None
    assert result.reason is not None


def test_missing_symbol_is_unknown() -> None:
    result = CurrencyResolver(
        [
            declaration(
                "store-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            )
        ]
    ).resolve(None, store_id=STORE_CA)
    assert result.status is ResolutionStatus.UNKNOWN
    assert result.symbol is None
    assert result.currency is None


def test_symbol_whitespace_is_normalized() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "store-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            )
        ]
    )
    result = resolver.resolve("  $  ", store_id=STORE_CA)
    assert result.symbol == "$"
    assert result.currency == "CAD"


def test_considered_lists_effective_in_scope_declarations() -> None:
    resolver = CurrencyResolver(
        [
            declaration(
                "store-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            ),
            declaration(
                "other-store",
                "USD",
                EvidenceLevel.STORE,
                store_id=STORE_US,
            ),
            declaration(
                "expired",
                "USD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
                effective_until=date(2020, 1, 1),
            ),
        ]
    )
    result = resolver.resolve("$", store_id=STORE_CA, as_of=date(2026, 11, 14))
    assert [d.declaration_id for d in result.considered] == ["store-cad"]


def test_resolve_price_bridge() -> None:
    parsed = Price.fromstring("$19.99")
    resolver = CurrencyResolver(
        [
            declaration(
                "store-au-aud",
                "AUD",
                EvidenceLevel.STORE,
                store_id=STORE_AU,
            )
        ]
    )
    result = resolver.resolve_price(parsed, store_id=STORE_AU)
    assert result.status is ResolutionStatus.RESOLVED
    assert result.currency == "AUD"
    assert result.symbol == "$"


def test_module_level_helper() -> None:
    result = resolve_currency(
        "$",
        [
            declaration(
                "store-ca-cad",
                "CAD",
                EvidenceLevel.STORE,
                store_id=STORE_CA,
            )
        ],
        store_id=STORE_CA,
    )
    assert result.currency == "CAD"


def test_duplicate_declaration_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate declaration_id"):
        CurrencyResolver(
            [
                declaration(
                    "same-id",
                    "CAD",
                    EvidenceLevel.STORE,
                    store_id=STORE_CA,
                ),
                declaration(
                    "same-id",
                    "USD",
                    EvidenceLevel.STORE,
                    store_id=STORE_US,
                ),
            ]
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"declaration_id": "  ", "currency": "CAD", "level": EvidenceLevel.SITE},
            "declaration_id",
        ),
        (
            {"declaration_id": "d1", "currency": " ", "level": EvidenceLevel.SITE},
            "currency",
        ),
    ],
)
def test_blank_declaration_fields_are_rejected(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CurrencyDeclaration(**kwargs)  # type: ignore[arg-type]


def test_inverted_time_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="effective_until"):
        CurrencyDeclaration(
            declaration_id="d1",
            currency="CAD",
            level=EvidenceLevel.STORE,
            effective_from=date(2026, 12, 1),
            effective_until=date(2026, 1, 1),
        )

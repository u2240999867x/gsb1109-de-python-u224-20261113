"""
Currency resolution with hierarchical, time-scoped evidence.

``price_parser.parser`` extracts the currency *symbol* as it appears on a
page, but many symbols are ambiguous on their own. For example, ``$`` is used
by dozens of currencies, so the same merchant can mean CAD on its Canadian
store, AUD on its Australian store and USD on its American store.

This module resolves an observed symbol to a single currency code from
explicit declarations, which carry their own scope and validity period:

* :class:`EvidenceLevel.ITEM` -- a currency declared for one product;
* :class:`EvidenceLevel.STORE` -- a currency declared for a store, optionally
  limited by an effective time window;
* :class:`EvidenceLevel.SITE` -- the default currency of a whole site.

Levels are tried from the narrowest (item) to the broadest (site). The first
level that provides in-scope, currently-effective evidence wins. When several
declarations at the *same* level support different currencies, the row is
suspended instead of guessing. Every resolution keeps the declaration it is
based on, so recomputing an old quote shows exactly which raw statement
changed the conclusion.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING

import attr

from ._currencies import CURRENCIES

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .parser import Price


class EvidenceLevel(enum.Enum):
    """Scope of a currency declaration, from narrow to broad."""

    ITEM = "item"
    STORE = "store"
    SITE = "site"


# Narrower scopes are consulted first.
_LEVEL_ORDER: tuple[EvidenceLevel, ...] = (
    EvidenceLevel.ITEM,
    EvidenceLevel.STORE,
    EvidenceLevel.SITE,
)


class ResolutionStatus(enum.Enum):
    """Outcome of a currency resolution attempt."""

    RESOLVED = "resolved"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


@attr.s(auto_attribs=True, frozen=True, slots=True, kw_only=True)
class CurrencyDeclaration:
    """A raw statement saying which currency a scope uses.

    ``effective_from`` and ``effective_until`` are both inclusive. A
    declaration without either bound is considered always effective. Timed
    declarations are only used when the resolution date (``as_of``) falls
    inside their window, which keeps today's store settings from being
    applied to historical pages.
    """

    declaration_id: str
    currency: str
    level: EvidenceLevel
    effective_from: date | None = None
    effective_until: date | None = None
    item_id: str | None = None
    store_id: str | None = None
    site_id: str | None = None

    def __attrs_post_init__(self) -> None:
        if not self.declaration_id.strip():
            raise ValueError("declaration_id must not be empty")
        if not self.currency.strip():
            raise ValueError("currency must not be empty")
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until < self.effective_from
        ):
            raise ValueError("effective_until must not be before effective_from")

    def is_effective_on(self, as_of: date | None) -> bool:
        """Return whether the declaration is in effect on ``as_of``.

        When ``as_of`` is ``None`` only declarations without a time window
        apply, so undated lookups never pick up time-scoped store settings.
        """
        if as_of is None:
            return self.effective_from is None and self.effective_until is None
        if self.effective_from is not None and as_of < self.effective_from:
            return False
        return not (self.effective_until is not None and as_of > self.effective_until)


@attr.s(auto_attribs=True, frozen=True, slots=True, kw_only=True)
class CurrencyResolution:
    """The result of resolving one observed currency symbol."""

    status: ResolutionStatus
    symbol: str | None
    currency: str | None
    evidence: CurrencyDeclaration | None
    reason: str | None = None
    considered: tuple[CurrencyDeclaration, ...] = ()
    conflicts: tuple[CurrencyDeclaration, ...] = ()

    @property
    def is_suspended(self) -> bool:
        """Return whether the row must be held rather than imported."""
        return self.status is ResolutionStatus.SUSPENDED


@attr.s(auto_attribs=True, frozen=True, slots=True, kw_only=True)
class _Scope:
    item_id: str | None = None
    store_id: str | None = None
    site_id: str | None = None


# Symbols the parser recognises but which are not present, or not unique,
# in the CURRENCIES table under that exact spelling.
_SYMBOL_ALIASES: dict[str, frozenset[str]] = {
    "US$": frozenset({"USD"}),
    "A$": frozenset({"AUD"}),
}


def _build_symbol_index() -> dict[str, frozenset[str]]:
    index: dict[str, set[str]] = {}
    for code, info in CURRENCIES.items():
        symbols = [info["s"], info["sn"], *info.get("sn2", [])]
        for symbol in symbols:
            if symbol:
                index.setdefault(symbol, set()).add(code)
    merged: dict[str, frozenset[str]] = {
        symbol: frozenset(codes) for symbol, codes in index.items()
    }
    merged.update(_SYMBOL_ALIASES)
    return merged


_SYMBOL_INDEX = _build_symbol_index()


def _as_date(value: date | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


def _normalize_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    normalized = symbol.strip()
    return normalized or None


class CurrencyResolver:
    """Resolve ambiguous currency symbols from scoped declarations.

    A resolver is an immutable registry of :class:`CurrencyDeclaration`
    objects. Keep historical registries around (or build one per import run)
    to recompute old quotes against the statements that were effective at the
    time instead of the current store configuration.
    """

    def __init__(self, declarations: Iterable[CurrencyDeclaration] = ()) -> None:
        registry = tuple(declarations)
        seen: set[str] = set()
        duplicates: list[str] = []
        for declaration in registry:
            if declaration.declaration_id in seen:
                duplicates.append(declaration.declaration_id)
            seen.add(declaration.declaration_id)
        if duplicates:
            raise ValueError(
                "duplicate declaration_id values: " + ", ".join(sorted(duplicates))
            )
        self._declarations = registry

    @property
    def declarations(self) -> tuple[CurrencyDeclaration, ...]:
        """All declarations known to this resolver."""
        return self._declarations

    def resolve(
        self,
        symbol: str | None,
        *,
        item_id: str | None = None,
        store_id: str | None = None,
        site_id: str | None = None,
        as_of: date | None = None,
    ) -> CurrencyResolution:
        """Resolve ``symbol`` for one product row.

        ``item_id``, ``store_id`` and ``site_id`` identify where the row was
        observed and select declarations by scope. ``as_of`` is the date the
        row was captured; only declarations effective on that date are used.
        """
        normalized = _normalize_symbol(symbol)
        resolution_date = _as_date(as_of)
        scope = _Scope(item_id=item_id, store_id=store_id, site_id=site_id)

        if normalized is None:
            return CurrencyResolution(
                status=ResolutionStatus.UNKNOWN,
                symbol=None,
                currency=None,
                evidence=None,
                reason="no currency symbol was observed on the row",
            )

        unambiguous = _SYMBOL_INDEX.get(normalized)
        if unambiguous is not None and len(unambiguous) == 1:
            return CurrencyResolution(
                status=ResolutionStatus.RESOLVED,
                symbol=normalized,
                currency=next(iter(unambiguous)),
                evidence=None,
                reason="symbol maps to a single currency without declarations",
            )

        eligible = tuple(
            declaration
            for declaration in self._declarations
            if self._in_scope(declaration, scope)
            and declaration.is_effective_on(resolution_date)
        )

        for level in _LEVEL_ORDER:
            candidates = tuple(
                declaration for declaration in eligible if declaration.level is level
            )
            if not candidates:
                continue
            by_currency: dict[str, CurrencyDeclaration] = {}
            for declaration in candidates:
                by_currency.setdefault(declaration.currency, declaration)
            if len(by_currency) > 1:
                return CurrencyResolution(
                    status=ResolutionStatus.SUSPENDED,
                    symbol=normalized,
                    currency=None,
                    evidence=None,
                    reason=(
                        f"conflicting {level.value}-level declarations: "
                        + ", ".join(
                            f"{declaration.declaration_id}->{declaration.currency}"
                            for declaration in by_currency.values()
                        )
                    ),
                    considered=eligible,
                    conflicts=candidates,
                )
            evidence = next(iter(by_currency.values()))
            return CurrencyResolution(
                status=ResolutionStatus.RESOLVED,
                symbol=normalized,
                currency=evidence.currency,
                evidence=evidence,
                considered=eligible,
            )

        return CurrencyResolution(
            status=ResolutionStatus.UNKNOWN,
            symbol=normalized,
            currency=None,
            evidence=None,
            reason="no in-scope declaration is effective for the given date",
            considered=eligible,
        )

    def resolve_price(
        self,
        price: Price,
        *,
        item_id: str | None = None,
        store_id: str | None = None,
        site_id: str | None = None,
        as_of: date | None = None,
    ) -> CurrencyResolution:
        """Resolve the currency symbol already parsed into ``price``."""
        return self.resolve(
            price.currency,
            item_id=item_id,
            store_id=store_id,
            site_id=site_id,
            as_of=as_of,
        )

    @staticmethod
    def _in_scope(declaration: CurrencyDeclaration, scope: _Scope) -> bool:
        if declaration.level is EvidenceLevel.ITEM:
            return scope.item_id is not None and declaration.item_id == scope.item_id
        if declaration.level is EvidenceLevel.STORE:
            return scope.store_id is not None and declaration.store_id == scope.store_id
        return scope.site_id is not None and declaration.site_id == scope.site_id


def resolve_currency(
    symbol: str | None,
    declarations: Iterable[CurrencyDeclaration],
    *,
    item_id: str | None = None,
    store_id: str | None = None,
    site_id: str | None = None,
    as_of: date | None = None,
) -> CurrencyResolution:
    """Convenience wrapper building a :class:`CurrencyResolver` per call."""
    return CurrencyResolver(declarations).resolve(
        symbol,
        item_id=item_id,
        store_id=store_id,
        site_id=site_id,
        as_of=as_of,
    )

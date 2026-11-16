"""Credits: what a model call costs on a connection (M7, M8).

One pure calculation, used everywhere a credit is counted (the usage row each
model call records, and the connections screen's worked example)::

    weighted = input x w_in + output x w_out + cache read x w_cr + cache write x w_cw
    credits  = weighted / base rate x tier multiplier

``input`` is uncached input. The weights (1, 5, 0.1 and 1.25 by default), the base
rate (weighted tokens per credit on a standard model, 1,000 by default) and the
higher and lower tier multipliers (2 and 0.5 by default; standard is always 1) are
the connection's own (``connection_metering``). Each model on a connection is of the
lower, standard or higher tier (``connection_models``).

Arithmetic is exact (:class:`decimal.Decimal`; weights and multipliers stored as
floating point are read as the decimals they were entered as). A call's credits are
rounded once, half up, to six decimal places (:data:`PLACES`), and stored as whole
micro-credits (:func:`to_micro`), so sums are exact on SQLite and PostgreSQL alike.
Every usage row keeps the inputs it was charged with (:meth:`Rates.as_record`), so a
later change of a connection's rates never rewrites history.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Literal

__all__ = [
    "MICRO",
    "PLACES",
    "TIERS",
    "Estimate",
    "Rates",
    "Tier",
    "TokenCounts",
    "credits",
    "estimate",
    "from_micro",
    "to_micro",
    "weighted_tokens",
]

Tier = Literal["lower", "standard", "higher"]
#: The tiers, cheapest first.
TIERS: Final[tuple[Tier, ...]] = ("lower", "standard", "higher")
#: Micro-credits in a credit: credits are kept to six decimal places.
MICRO: Final = 1_000_000
#: The precision of a call's credits.
PLACES: Final = Decimal("0.000001")


def _decimal(value: object, name: str) -> Decimal:
    """``value`` as the decimal it was written as (a float's shortest form)."""
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ValueError(f"{name} must be a number")
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite number")
    # One spelling per value, so a record reads "2" whether entered as 2 or 2.0.
    if number == number.to_integral_value():
        return number.quantize(Decimal(1))
    return number.normalize()


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number of tokens")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """One call's tokens: uncached input, output, cache read and cache write."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        for f in fields(self):
            _count(getattr(self, f.name), f.name)

    @classmethod
    def of(cls, value: TokenCounts | Mapping[str, int]) -> TokenCounts:
        """Counts from a mapping of the four names (missing ones are zero)."""
        if isinstance(value, TokenCounts):
            return value
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError("unknown token counts: " + ", ".join(unknown))
        return cls(**{name: value[name] for name in known if name in value})

    def as_dict(self) -> dict[str, int]:
        return {f.name: int(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True, slots=True)
class Rates:
    """A connection's credit rates. The defaults are M7's and M8's."""

    base_rate_tokens: int = 1000
    higher_multiplier: Decimal = Decimal(2)
    lower_multiplier: Decimal = Decimal("0.5")
    input_weight: Decimal = Decimal(1)
    output_weight: Decimal = Decimal(5)
    cache_read_weight: Decimal = Decimal("0.1")
    cache_write_weight: Decimal = Decimal("1.25")

    def __post_init__(self) -> None:
        base = _count(self.base_rate_tokens, "base_rate_tokens")
        if base < 1:
            raise ValueError("a base rate is at least one weighted token per credit")
        for name in ("higher_multiplier", "lower_multiplier"):
            value = _decimal(getattr(self, name), name)
            if value <= 0:
                raise ValueError("a tier multiplier must be positive")
            object.__setattr__(self, name, value)
        for name in ("input_weight", "output_weight", "cache_read_weight", "cache_write_weight"):
            value = _decimal(getattr(self, name), name)
            if value < 0:
                raise ValueError("a token weight cannot be negative")
            object.__setattr__(self, name, value)

    @classmethod
    def from_metering(cls, metering: Any) -> Rates:
        """The rates of a ``spark.database.connections.Metering`` (or anything shaped so)."""
        return cls(
            base_rate_tokens=int(metering.base_rate_tokens),
            higher_multiplier=_decimal(metering.higher_multiplier, "higher_multiplier"),
            lower_multiplier=_decimal(metering.lower_multiplier, "lower_multiplier"),
            input_weight=_decimal(metering.input_weight, "input_weight"),
            output_weight=_decimal(metering.output_weight, "output_weight"),
            cache_read_weight=_decimal(metering.cache_read_weight, "cache_read_weight"),
            cache_write_weight=_decimal(metering.cache_write_weight, "cache_write_weight"),
        )

    def multiplier(self, tier: str) -> Decimal:
        """The multiplier of ``tier``: standard is always 1."""
        if tier == "standard":
            return Decimal(1)
        if tier == "higher":
            return self.higher_multiplier
        if tier == "lower":
            return self.lower_multiplier
        raise ValueError("a tier is lower, standard or higher")

    def as_record(self, tier: str, *, metered: bool) -> dict[str, Any]:
        """The inputs a call was charged with, as a usage row keeps them (JSON-ready)."""
        return {
            "metered": metered,
            "tier": tier,
            "multiplier": str(self.multiplier(tier)),
            "base_rate_tokens": self.base_rate_tokens,
            "weights": {
                "input": str(self.input_weight),
                "output": str(self.output_weight),
                "cache_read": str(self.cache_read_weight),
                "cache_write": str(self.cache_write_weight),
            },
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> tuple[Rates, Tier]:
        """The rates and tier a usage row's record names (its tier's multiplier included)."""
        name = str(record["tier"])
        if name not in TIERS:
            raise ValueError("a tier is lower, standard or higher")
        tier: Tier = name
        weights = record["weights"]
        multiplier = _decimal(record["multiplier"], "multiplier")
        defaults = cls()
        rates = cls(
            base_rate_tokens=int(record["base_rate_tokens"]),
            higher_multiplier=multiplier if tier == "higher" else defaults.higher_multiplier,
            lower_multiplier=multiplier if tier == "lower" else defaults.lower_multiplier,
            input_weight=_decimal(weights["input"], "input"),
            output_weight=_decimal(weights["output"], "output"),
            cache_read_weight=_decimal(weights["cache_read"], "cache_read"),
            cache_write_weight=_decimal(weights["cache_write"], "cache_write"),
        )
        return rates, tier


def weighted_tokens(rates: Rates, counts: TokenCounts) -> Decimal:
    """The call's weighted tokens at the connection's weights."""
    total = (
        counts.input_tokens * rates.input_weight
        + counts.output_tokens * rates.output_weight
        + counts.cache_read_tokens * rates.cache_read_weight
        + counts.cache_write_tokens * rates.cache_write_weight
    )
    return _decimal(total, "weighted tokens")


def credits(rates: Rates, counts: TokenCounts, tier: str = "standard") -> Decimal:
    """The call's credits: weighted tokens / base rate x tier multiplier, to six places."""
    raw = weighted_tokens(rates, counts) / Decimal(rates.base_rate_tokens) * rates.multiplier(tier)
    return raw.quantize(PLACES, rounding=ROUND_HALF_UP)


def to_micro(amount: Decimal) -> int:
    """Credits as whole micro-credits (rounded half up), as usage rows store them."""
    return int((amount * MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_micro(micro: int) -> Decimal:
    """Whole micro-credits as credits, to six places."""
    return (Decimal(int(micro)) / MICRO).quantize(PLACES)


@dataclass(frozen=True, slots=True)
class Estimate:
    """What sample counts would cost on a connection, at each tier."""

    counts: TokenCounts
    weighted_tokens: Decimal
    credits: dict[str, Decimal]

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready: numbers as decimal strings, so nothing is lost to floating point."""
        return {
            "counts": self.counts.as_dict(),
            "weighted_tokens": str(self.weighted_tokens),
            "credits": {tier: str(amount) for tier, amount in self.credits.items()},
        }


def estimate(connection: Any, counts: TokenCounts | Mapping[str, int]) -> Estimate:
    """What ``counts`` would cost on ``connection`` at each tier (the worked example).

    ``connection`` is the connection's :class:`Rates`, or its metering settings
    (``spark.database.connections.Metering``). ``counts`` is a :class:`TokenCounts`
    or a mapping of ``input_tokens``, ``output_tokens``, ``cache_read_tokens`` and
    ``cache_write_tokens`` (missing ones are zero).
    """
    rates = connection if isinstance(connection, Rates) else Rates.from_metering(connection)
    sample = TokenCounts.of(counts)
    return Estimate(
        counts=sample,
        weighted_tokens=weighted_tokens(rates, sample),
        credits={tier: credits(rates, sample, tier) for tier in TIERS},
    )

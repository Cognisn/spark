"""The credit calculation (M7, M8), proven on worked examples.

weighted = input x w_in + output x w_out + cache read x w_cr + cache write x w_cw
credits  = weighted / base rate x tier multiplier, kept to six decimal places
(rounded half up, per call).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from spark.usage import credits
from spark.usage.credits import Estimate, Rates, TokenCounts


@dataclass(frozen=True)
class MeteringLike:
    """The fields of spark.database.connections.Metering, floats as stored."""

    connection_id: str = "c"
    metered: bool = True
    base_rate_tokens: int = 1000
    higher_multiplier: float = 2.0
    lower_multiplier: float = 0.5
    input_weight: float = 1.0
    output_weight: float = 5.0
    cache_read_weight: float = 0.1
    cache_write_weight: float = 1.25


DEFAULT = Rates()


class TestWorkedExamples:
    def test_input_and_output_on_a_standard_model(self) -> None:
        # 1,000 in x 1 + 200 out x 5 = 2,000 weighted; / 1,000 = 2 credits.
        counts = TokenCounts(input_tokens=1000, output_tokens=200)
        assert credits.weighted_tokens(DEFAULT, counts) == Decimal("2000")
        assert credits.credits(DEFAULT, counts) == Decimal("2.000000")

    def test_cache_tokens_count_at_their_weights(self) -> None:
        # 500 x 1 + 100 x 5 + 10,000 x 0.1 + 2,000 x 1.25
        # = 500 + 500 + 1,000 + 2,500 = 4,500 weighted; 4.5 credits.
        counts = TokenCounts(500, 100, 10_000, 2_000)
        assert credits.weighted_tokens(DEFAULT, counts) == Decimal("4500")
        assert credits.credits(DEFAULT, counts) == Decimal("4.500000")

    @pytest.mark.parametrize(
        ("tier", "expected"),
        [("lower", "2.250000"), ("standard", "4.500000"), ("higher", "9.000000")],
    )
    def test_each_tier(self, tier: str, expected: str) -> None:
        counts = TokenCounts(500, 100, 10_000, 2_000)
        assert credits.credits(DEFAULT, counts, tier) == Decimal(expected)

    def test_the_connections_own_weights_base_rate_and_multipliers(self) -> None:
        rates = Rates(
            base_rate_tokens=400,
            higher_multiplier=Decimal("3"),
            lower_multiplier=Decimal("0.25"),
            input_weight=Decimal("2"),
            output_weight=Decimal("8"),
            cache_read_weight=Decimal("0.5"),
            cache_write_weight=Decimal("2"),
        )
        # 100 x 2 + 50 x 8 + 200 x 0.5 + 100 x 2 = 200 + 400 + 100 + 200 = 900.
        counts = TokenCounts(100, 50, 200, 100)
        assert credits.weighted_tokens(rates, counts) == Decimal("900")
        # 900 / 400 = 2.25; x 3 = 6.75; x 0.25 = 0.5625.
        assert credits.credits(rates, counts, "higher") == Decimal("6.750000")
        assert credits.credits(rates, counts, "lower") == Decimal("0.562500")

    def test_rounded_half_up_to_six_places(self) -> None:
        # 1 weighted token / 3 base rate = 0.333333..., one token of 1/1,000,000 = 0.0000005.
        third = Rates(base_rate_tokens=3)
        assert credits.credits(third, TokenCounts(1)) == Decimal("0.333333")
        million = Rates(base_rate_tokens=2_000_000)
        assert credits.credits(million, TokenCounts(1)) == Decimal("0.000001")  # 0.0000005 up

    def test_no_tokens_cost_nothing(self) -> None:
        assert credits.credits(DEFAULT, TokenCounts()) == Decimal("0.000000")

    def test_floats_as_stored_are_read_as_their_decimals(self) -> None:
        rates = Rates.from_metering(MeteringLike())
        assert rates == DEFAULT
        assert rates.cache_read_weight == Decimal("0.1")


class TestMicroCredits:
    def test_round_trip(self) -> None:
        assert credits.to_micro(Decimal("4.5")) == 4_500_000
        assert credits.from_micro(4_500_000) == Decimal("4.500000")

    def test_to_micro_rounds_half_up(self) -> None:
        assert credits.to_micro(Decimal("0.0000005")) == 1


class TestValidation:
    def test_counts_are_whole_and_not_negative(self) -> None:
        with pytest.raises(ValueError):
            TokenCounts(-1)
        with pytest.raises(ValueError):
            TokenCounts(True)  # type: ignore[arg-type]

    def test_rates(self) -> None:
        with pytest.raises(ValueError):
            Rates(base_rate_tokens=0)
        with pytest.raises(ValueError):
            Rates(higher_multiplier=Decimal("0"))
        with pytest.raises(ValueError):
            Rates(output_weight=Decimal("-1"))

    def test_unknown_tier(self) -> None:
        with pytest.raises(ValueError):
            credits.credits(DEFAULT, TokenCounts(1), "premium")


class TestRecord:
    def test_the_inputs_used_round_trip(self) -> None:
        record = DEFAULT.as_record("higher", metered=True)
        assert record == {
            "metered": True,
            "tier": "higher",
            "multiplier": "2",
            "base_rate_tokens": 1000,
            "weights": {"input": "1", "output": "5", "cache_read": "0.1", "cache_write": "1.25"},
        }
        rates, tier = Rates.from_record(record)
        assert (rates, tier) == (DEFAULT, "higher")


class TestEstimate:
    """credits.estimate(connection, sample counts): the connections screen's worked example."""

    def test_every_tier_from_a_metering_row(self) -> None:
        result = credits.estimate(
            MeteringLike(),
            {
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_read_tokens": 10_000,
                "cache_write_tokens": 2_000,
            },
        )
        assert isinstance(result, Estimate)
        assert result.weighted_tokens == Decimal("4500")
        assert result.credits == {
            "lower": Decimal("2.250000"),
            "standard": Decimal("4.500000"),
            "higher": Decimal("9.000000"),
        }

    def test_as_dict_is_json_ready(self) -> None:
        result = credits.estimate(DEFAULT, TokenCounts(1000, 200))
        assert result.as_dict() == {
            "counts": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
            "weighted_tokens": "2000",
            "credits": {"lower": "1.000000", "standard": "2.000000", "higher": "4.000000"},
        }

    def test_missing_counts_are_zero_and_unknown_ones_refused(self) -> None:
        assert credits.estimate(DEFAULT, {"output_tokens": 200}).weighted_tokens == Decimal("1000")
        with pytest.raises(ValueError):
            credits.estimate(DEFAULT, {"thinking_tokens": 5})

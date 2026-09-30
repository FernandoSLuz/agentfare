import decimal
from decimal import Decimal

import pytest

from agentfare.money import (
    UCENTS_PER_CENT,
    UCENTS_PER_USD,
    cents_to_ucents,
    format_usd,
    format_usd_compact,
    ratio_str,
    to_cents,
    usd_to_ucents,
)


class TestToCents:
    @pytest.mark.parametrize(
        ("ucents", "cents"),
        [
            (0, 0),
            (499_999, 0),
            (500_000, 1),  # exact half rounds up
            (1_499_999, 1),
            (1_500_000, 2),
            (2_500_000, 3),  # not banker's rounding
            (1_000_000, 1),
            (123_456_789, 123),
        ],
    )
    def test_half_up(self, ucents: int, cents: int) -> None:
        assert to_cents(ucents) == cents

    @pytest.mark.parametrize(
        ("ucents", "cents"),
        [
            (-499_999, 0),
            (-500_000, -1),  # half rounds away from zero, mirroring the positive side
            (-1_500_000, -2),
            (-2_500_000, -3),
            (-1_000_000, -1),
        ],
    )
    def test_negative_mirrors_positive(self, ucents: int, cents: int) -> None:
        assert to_cents(ucents) == cents
        assert to_cents(-ucents) == -cents

    def test_cents_round_trip(self) -> None:
        assert cents_to_ucents(7) == 7 * UCENTS_PER_CENT
        assert to_cents(cents_to_ucents(-7)) == -7


class TestFormatUsdCompact:
    """Whole cents from $1 up, where a fraction of a cent no longer matters; below $1 the default rule of format_usd."""

    @pytest.mark.parametrize(
        ("ucents", "shown"),
        [
            (0, "$0.00"),
            (42_000, "$0.0004"),  # below $1: four places for a sub-cent amount, as format_usd
            (76_000_000, "$0.76"),  # ... two for a whole cent
            (5_920_000, "$0.0592"),
            (99_999_999, "$1.00"),  # rounds up to a dollar: from $1 up two places, so no $1.0000
            (UCENTS_PER_USD, "$1.00"),
            (UCENTS_PER_USD + 234_999, "$1.00"),  # a fraction of a cent above $1 is not shown
            (265_100_000, "$2.65"),
            (12_345_678_901, "$123.46"),
        ],
    )
    def test_the_compact_rule(self, ucents: int, shown: str) -> None:
        assert format_usd_compact(ucents) == shown

    def test_negative_amounts_follow_the_size_of_the_amount(self) -> None:
        assert format_usd_compact(-UCENTS_PER_USD - 1) == "-$1.00"
        assert format_usd_compact(-4_200_000) == "-$0.0420"

    def test_below_a_dollar_it_is_format_usd_and_from_a_dollar_it_forces_two_places(self) -> None:
        for ucents in (1, 4_999, 5_000, 49_999, 4_200_000, 82_714_980, UCENTS_PER_USD - 1):
            assert format_usd_compact(ucents) == format_usd(ucents)
        for ucents in (UCENTS_PER_USD, UCENTS_PER_USD + 1_234_567, 5 * UCENTS_PER_USD + 4_999):
            assert format_usd_compact(ucents) == format_usd(ucents, 2)


class TestFormatUsd:
    def test_whole_cent_amounts_use_two_places_by_default(self) -> None:
        assert format_usd(0) == "$0.00"
        assert format_usd(420_000_000) == "$4.20"
        assert format_usd(UCENTS_PER_USD) == "$1.00"
        assert format_usd(123_456_000_000) == "$1,234.56"

    def test_sub_cent_amounts_use_four_places_by_default(self) -> None:
        assert format_usd(42_000) == "$0.0004"
        assert format_usd(4_200_000) == "$0.0420"
        assert format_usd(82_714_980) == "$0.8271"

    def test_default_decision_is_made_after_rounding_to_four_places(self) -> None:
        # 4.999999 cents rounds to 5.00 cents at 4dp, so it is shown as a whole cent
        assert format_usd(4_999_999) == "$0.05"
        assert format_usd(49_999) == "$0.0005"
        # below half of the fourth place it is zero, and zero is a whole cent
        assert format_usd(4_999) == "$0.00"

    def test_forced_two_places_rounds_half_up_to_a_cent(self) -> None:
        assert format_usd(1_500_000, 2) == "$0.02"
        assert format_usd(1_499_999, 2) == "$0.01"
        assert format_usd(82_714_980, 2) == "$0.83"

    def test_forced_four_places_rounds_half_up_to_a_hundredth_of_a_cent(self) -> None:
        assert format_usd(420_000_000, 4) == "$4.2000"
        assert format_usd(5_000, 4) == "$0.0001"  # exactly half of the last place
        assert format_usd(4_999, 4) == "$0.0000"

    def test_thousands_separator(self) -> None:
        assert format_usd(1_234_567 * UCENTS_PER_USD, 2) == "$1,234,567.00"

    def test_negative_amounts(self) -> None:
        assert format_usd(-420_000_000) == "-$4.20"
        assert format_usd(-4_200_000) == "-$0.0420"
        assert format_usd(-1_500_000, 2) == "-$0.02"

    @pytest.mark.parametrize("places", [None, 2, 4])
    def test_amounts_that_round_to_zero_are_never_negative_zero(self, places: int | None) -> None:
        assert format_usd(-100, places) == format_usd(0, places)
        assert "-" not in format_usd(-100, places)

    def test_only_zero_two_four_places_are_supported(self) -> None:
        for bad in (0, 1, 3, 6):
            with pytest.raises(ValueError):
                format_usd(1, bad)

    def test_returns_str_without_touching_float(self) -> None:
        assert format_usd(10**18) == "$10,000,000,000.00"  # a float would lose the low digits


class TestUsdToUcents:
    def test_exact_for_decimal_str_and_int(self) -> None:
        assert usd_to_ucents(Decimal("0.1")) == 10_000_000
        assert usd_to_ucents("0.1") == 10_000_000
        assert usd_to_ucents("4.2") == 420_000_000
        assert usd_to_ucents(3) == 300_000_000
        assert usd_to_ucents("0.00000001") == 1  # one ucent

    def test_sub_ucent_remainders_round_half_up(self) -> None:
        assert usd_to_ucents("0.000000005") == 1  # 0.5 ucent
        assert usd_to_ucents("0.0000000049") == 0

    def test_no_binary_float_artifacts(self) -> None:
        total = usd_to_ucents(Decimal("0.1")) + usd_to_ucents(Decimal("0.2"))
        assert total == usd_to_ucents(Decimal("0.3"))  # 0.1 + 0.2 != 0.3 with floats

    def test_large_values_stay_exact(self) -> None:
        assert usd_to_ucents("123456789012.34") == 12_345_678_901_234_000_000

    def test_floats_are_rejected(self) -> None:
        with pytest.raises(TypeError):
            usd_to_ucents(0.1)  # type: ignore[arg-type]

    @pytest.mark.parametrize("digits", [6, 1])
    def test_a_caller_lowering_the_decimal_precision_changes_nothing(self, digits: int) -> None:
        with decimal.localcontext(decimal.Context(prec=digits)):
            assert usd_to_ucents("1234567.89") == 123_456_789_000_000
            assert usd_to_ucents(Decimal("0.000000015")) == 2  # a half still rounds up

    def test_more_digits_than_the_default_precision_are_not_rounded_first(self) -> None:
        # 0.5 ucent minus a hair: rounds to 0. Rounding to 28 digits first would give exactly 0.5 and round up to 1.
        assert usd_to_ucents("0.0000000049999999999999999999999999999999") == 0
        assert usd_to_ucents("0.0000000050000000000000000000000000000001") == 1


class TestRatioStr:
    def test_basic(self) -> None:
        assert ratio_str(10, 2) == "5.0x"
        assert ratio_str(3, 2) == "1.5x"
        assert ratio_str(0, 7) == "0.0x"

    def test_undefined(self) -> None:
        assert ratio_str(5, 0) == "-"
        assert ratio_str(5, -1) == "-"

    def test_ties_round_half_up_like_every_other_amount(self) -> None:
        assert ratio_str(5, 4) == "1.3x"  # 1.25
        assert ratio_str(1, 4) == "0.3x"  # 0.25
        assert ratio_str(9, 8) == "1.1x"  # 1.125 is below the tie at one place
        assert ratio_str(99, 40) == "2.5x"  # 2.475

    def test_negative_numerator_keeps_its_sign_only_when_visible(self) -> None:
        assert ratio_str(-5, 4) == "-1.3x"
        assert ratio_str(-1, 100) == "0.0x"

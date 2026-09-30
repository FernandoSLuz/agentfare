import decimal
import math
import random
from collections.abc import Callable, Iterator
from fractions import Fraction

import pytest

from agentfare.cost import SessionCost, price_usage, reprice, usage_cost_ucents
from agentfare.models import Usage
from agentfare.money import format_usd, to_cents
from agentfare.pricing import ModelPrice, PriceTable

MODELS = {
    # every rate present
    "claude-x": {"input": 3, "cached_input": 0.30, "cache_write": 3.75, "cache_write_1h": 6, "output": 15},
    # cached_input, cache_write and cache_write_1h absent: all fall back to the input rate
    "plain": {"input": 1.5, "output": 6},
    # cache_write present, cached_input absent
    "cw-only": {"input": 4, "cache_write": 5, "output": 20},
    # cached_input present, cache_write absent
    "gpt-sol": {"input": 2.0, "cached_input": 0.10, "output": 10.0},
    # a price so small that one token is half a micro-cent
    "tiny": {"input": 0.005, "output": 0.005},
    "unver-in": {"input": "unverified", "output": 5},
    "unver-out": {"input": 1, "output": "unverified"},
    "partial": {"input": 2, "cached_input": "unverified", "output": 8},
    # the 1-hour price cannot be verified: 1-hour writes are billed at the 5-minute rate
    "hour-unver": {"input": 3, "cache_write": 3.75, "cache_write_1h": "unverified", "output": 15},
}


@pytest.fixture
def table(make_table: Callable[..., PriceTable]) -> PriceTable:
    return make_table(MODELS, aliases={"sol": "gpt-sol"})


def price(table: PriceTable, model: str) -> ModelPrice:
    p = table.get(model)
    assert p is not None
    return p


class TestUsageCost:
    def test_every_bucket_at_its_own_rate(self, table: PriceTable) -> None:
        # 10_000*3 + 500_000*0.30 + 40_000*3.75 + 8_000*15 = 450_000 dollar-tokens -> $0.45
        u = Usage(input=10_000, cache_read=500_000, cache_write=40_000, output=8_000)
        assert usage_cost_ucents(u, price(table, "claude-x")) == 45_000_000
        assert format_usd(45_000_000) == "$0.45"

    def test_five_minute_and_one_hour_writes_each_at_their_own_rate(self, table: PriceTable) -> None:
        p = price(table, "claude-x")
        assert usage_cost_ucents(Usage(cache_write=1_000_000), p) == 375_000_000  # $3.75
        assert usage_cost_ucents(Usage(cache_write_1h=1_000_000), p) == 600_000_000  # $6.00
        # 10_000*3 + 500_000*0.30 + 40_000*3.75 + 25_000*6 + 8_000*15 = 600_000 dollar-tokens -> $0.60
        u = Usage(input=10_000, cache_read=500_000, cache_write=40_000, output=8_000, cache_write_1h=25_000)
        assert usage_cost_ucents(u, p) == 60_000_000

    def test_the_first_four_fields_stay_positional_and_1h_defaults_to_zero(self, table: PriceTable) -> None:
        assert usage_cost_ucents(Usage(10_000, 500_000, 40_000, 8_000), price(table, "claude-x")) == 45_000_000

    def test_missing_one_hour_price_falls_back_to_the_cache_write_price_then_the_input_price(self, table: PriceTable) -> None:
        assert usage_cost_ucents(Usage(cache_write_1h=1_000_000), price(table, "cw-only")) == 500_000_000  # its 5-minute rate 5
        assert usage_cost_ucents(Usage(cache_write_1h=1_000_000), price(table, "plain")) == 150_000_000  # the input rate 1.5
        assert usage_cost_ucents(Usage(cache_write_1h=1_000_000), price(table, "gpt-sol")) == 200_000_000

    def test_unverified_one_hour_price_bills_1h_writes_at_the_5_minute_rate_and_stays_priced(self, table: PriceTable) -> None:
        p = price(table, "hour-unver")
        assert p.priced
        assert usage_cost_ucents(Usage(cache_write_1h=1_000_000), p) == usage_cost_ucents(Usage(cache_write=1_000_000), p) == 375_000_000

    def test_one_million_tokens_cost_the_listed_price(self, table: PriceTable) -> None:
        p = price(table, "gpt-sol")
        assert usage_cost_ucents(Usage(input=1_000_000), p) == 200_000_000  # $2.00
        assert usage_cost_ucents(Usage(cache_read=1_000_000), p) == 10_000_000  # $0.10
        assert usage_cost_ucents(Usage(output=1_000_000), p) == 1_000_000_000  # $10.00
        assert usage_cost_ucents(Usage(cache_write=1_000_000), p) == 200_000_000  # no cache_write price: input rate

    def test_realistic_mix_is_exact(self, table: PriceTable) -> None:
        # 123_456*2.0 + 2_345_678*0.10 + 34_567*10 = 827_149.8 dollar-tokens -> $0.8271498
        u = Usage(input=123_456, cache_read=2_345_678, output=34_567)
        cost = usage_cost_ucents(u, price(table, "gpt-sol"))
        assert cost == 82_714_980
        assert format_usd(cost) == "$0.8271"
        assert to_cents(cost) == 83

    def test_missing_cached_input_falls_back_to_the_input_rate(self, table: PriceTable) -> None:
        # 100_000*1.5 + 200_000*1.5 (read) + 300_000*1.5 (write) + 50_000*6 = 1_200_000 -> $1.20
        u = Usage(input=100_000, cache_read=200_000, cache_write=300_000, output=50_000)
        assert usage_cost_ucents(u, price(table, "plain")) == 120_000_000
        assert usage_cost_ucents(Usage(cache_read=1_000_000), price(table, "plain")) == usage_cost_ucents(
            Usage(input=1_000_000), price(table, "plain")
        )

    def test_missing_cache_write_falls_back_to_the_input_rate_only_for_writes(self, table: PriceTable) -> None:
        p = price(table, "gpt-sol")
        assert usage_cost_ucents(Usage(cache_write=500_000), p) == usage_cost_ucents(Usage(input=500_000), p)
        assert usage_cost_ucents(Usage(cache_read=500_000), p) == 5_000_000  # still the cheap cached rate

    def test_cache_write_present_without_cached_input(self, table: PriceTable) -> None:
        # read at the input rate 4, write at 5: 1M*4 + 1M*5 = 9_000_000 dollar-tokens -> $9.00
        u = Usage(cache_read=1_000_000, cache_write=1_000_000)
        assert usage_cost_ucents(u, price(table, "cw-only")) == 900_000_000

    def test_partially_unverified_cached_price_bills_reads_at_the_input_rate(self, table: PriceTable) -> None:
        p = price(table, "partial")
        assert p.priced
        assert usage_cost_ucents(Usage(cache_read=1_000_000), p) == 200_000_000

    def test_empty_usage_costs_nothing(self, table: PriceTable) -> None:
        assert usage_cost_ucents(Usage(), price(table, "claude-x")) == 0

    def test_a_zero_price_is_still_a_price(self, make_table: Callable[..., PriceTable]) -> None:
        free = make_table({"free": {"input": 0, "output": 0}})
        assert usage_cost_ucents(Usage(input=10**9, output=10**9), price(free, "free")) == 0

    def test_huge_token_counts_stay_exact(self, table: PriceTable) -> None:
        u = Usage(input=10**12, output=10**12)  # a trillion tokens each: 2_000_000 + 10_000_000 dollars
        assert usage_cost_ucents(u, price(table, "gpt-sol")) == 12_000_000 * 100 * 1_000_000

    def test_unpriced_models_are_refused(self, table: PriceTable) -> None:
        for model in ("unver-in", "unver-out"):
            with pytest.raises(ValueError, match="no verified price"):
                usage_cost_ucents(Usage(input=1), price(table, model))


class TestRounding:
    def test_half_a_micro_cent_rounds_up(self, table: PriceTable) -> None:
        p = price(table, "tiny")  # $0.005 per 1M tokens: 1 token = 0.5 ucent
        assert usage_cost_ucents(Usage(input=1), p) == 1
        assert usage_cost_ucents(Usage(input=3), p) == 2  # 1.5
        assert usage_cost_ucents(Usage(input=5), p) == 3  # 2.5: not banker's rounding

    def test_just_below_half_rounds_down(self, make_table: Callable[..., PriceTable]) -> None:
        t = make_table({"m": {"input": "0.0049", "output": 1}})
        assert usage_cost_ucents(Usage(input=1), price(t, "m")) == 0  # 0.49 ucent

    def test_rounding_happens_once_over_the_whole_usage(self, table: PriceTable) -> None:
        # 0.5 + 0.5 = 1.0 ucent. Rounding each bucket first would give 1 + 1 = 2.
        assert usage_cost_ucents(Usage(input=1, output=1), price(table, "tiny")) == 1

    def test_sub_cent_totals_round_half_up_when_shown_in_cents(self) -> None:
        # 0.5 cent = 500_000 ucents is the boundary between 0 and 1 cent
        assert to_cents(499_999) == 0 and to_cents(500_000) == 1


# Contexts a caller (or a test harness) could have installed. Money math must not notice any of them.
AMBIENT_CONTEXTS = {
    "six digits": decimal.Context(prec=6),
    "one digit": decimal.Context(prec=1),
    "small exponent range": decimal.Context(prec=28, Emax=9, Emin=-9),
    "trapping every rounding": decimal.Context(prec=28, traps=[decimal.Inexact, decimal.Rounded]),
    "round half even": decimal.Context(prec=28, rounding=decimal.ROUND_HALF_EVEN),
}


class TestIndependentOfTheDecimalContext:
    @pytest.fixture(params=AMBIENT_CONTEXTS)
    def ambient(self, request: pytest.FixtureRequest) -> Iterator[decimal.Context]:
        with decimal.localcontext(AMBIENT_CONTEXTS[request.param]) as ctx:
            yield ctx

    def test_usage_cost(self, table: PriceTable, ambient: decimal.Context) -> None:
        # 123_456_789 * 2.0 + 987_654_321 * 10 = 10_123_456_788 dollar-tokens
        u = Usage(input=123_456_789, output=987_654_321)
        assert usage_cost_ucents(u, price(table, "gpt-sol")) == 10_123_456_788 * 100

    def test_halves_still_round_up(self, table: PriceTable, ambient: decimal.Context) -> None:
        assert usage_cost_ucents(Usage(input=5), price(table, "tiny")) == 3  # 2.5, not banker's rounding

    def test_price_usage_and_reprice(self, table: PriceTable, ambient: decimal.Context) -> None:
        usage = {"gpt-sol": Usage(input=123_456_789, output=987_654_321)}
        assert price_usage(usage, table).ucents == 10_123_456_788 * 100
        assert reprice(usage, "gpt-sol", table) == 10_123_456_788 * 100


class TestAgainstRationalArithmetic:
    def test_random_usage_and_prices_round_half_up_exactly_once(self, make_table: Callable[..., PriceTable]) -> None:
        rng = random.Random(2026)
        prices = ["0", "0.005", "0.075", "0.10", "1.25", "2.5", "3.75", "15", "0.0049", "1e-7", "12.345678901234567890123456789012345"]
        counts = [0, 1, 2, 3, 999, 10**6 + 7, 10**9 + 1, 10**12 + 5]
        for _ in range(3000):
            entry = {"input": rng.choice(prices), "output": rng.choice(prices)}
            for optional in ("cached_input", "cache_write", "cache_write_1h"):
                if rng.random() < 0.6:
                    entry[optional] = rng.choice(prices)
            p = price(make_table({"m": entry}), "m")
            u = Usage(*(rng.choice(counts) for _ in range(5)))
            exact = (
                Fraction(u.input) * Fraction(p.input) + Fraction(u.cache_read) * Fraction(p.cached_rate)
                + Fraction(u.cache_write) * Fraction(p.cache_write_rate) + Fraction(u.output) * Fraction(p.output)
                + Fraction(u.cache_write_1h) * Fraction(p.cache_write_1h_rate)
            ) * 100
            assert usage_cost_ucents(u, p) == math.floor(exact + Fraction(1, 2)), (u, entry)


class TestPricesWithManyDigits:
    def test_a_price_longer_than_28_digits_is_not_rounded_before_the_sum(self, make_table: Callable[..., PriceTable]) -> None:
        # 0.5 ucent minus a hair rounds down. Rounding the product to 28 digits first turns it into exactly 0.5, which rounds up.
        t = make_table({"m": {"input": "0.0049999999999999999999999999999999999999", "output": 1}})
        assert usage_cost_ucents(Usage(input=1), price(t, "m")) == 0
        t = make_table({"m": {"input": "0.0050000000000000000000000000000000000001", "output": 1}})
        assert usage_cost_ucents(Usage(input=1), price(t, "m")) == 1


class TestPriceUsage:
    def test_all_priced(self, table: PriceTable) -> None:
        usage = {
            "claude-x": Usage(input=10_000, cache_read=500_000, cache_write=40_000, output=8_000),
            "gpt-sol": Usage(input=1_000_000),
        }
        out = price_usage(usage, table)
        assert out.by_model == {"claude-x": 45_000_000, "gpt-sol": 200_000_000}
        assert out.ucents == 245_000_000
        assert out.unpriced == {} and out.complete

    def test_one_hour_writes_are_part_of_a_session_cost(self, table: PriceTable) -> None:
        usage = {"claude-x": Usage(cache_write=1_000_000, cache_write_1h=1_000_000), "hour-unver": Usage(cache_write_1h=1_000_000)}
        out = price_usage(usage, table)
        assert out.by_model == {"claude-x": 975_000_000, "hour-unver": 375_000_000}
        assert out.complete

    def test_unverified_and_unknown_models_are_filled_in_and_left_out_of_the_total(self, table: PriceTable) -> None:
        unpriced_usage = Usage(input=7, cache_read=8, cache_write=9, output=10, cache_write_1h=11)
        usage = {
            "gpt-sol": Usage(input=1_000_000),
            "unver-in": unpriced_usage,
            "mystery-9000": Usage(output=5),
            "unver-out": Usage(input=1),
        }
        out = price_usage(usage, table)
        assert out.ucents == 200_000_000
        assert out.by_model == {"gpt-sol": 200_000_000}
        assert out.unpriced == {"unver-in": unpriced_usage, "mystery-9000": Usage(output=5), "unver-out": Usage(input=1)}
        assert not out.complete

    def test_by_model_uses_canonical_ids_and_merges_spellings(self, table: PriceTable) -> None:
        usage = {
            "gpt-sol": Usage(input=1_000_000),
            "SOL": Usage(input=1_000_000),
            "openai/gpt-sol-20260101": Usage(input=1_000_000),
        }
        out = price_usage(usage, table)
        assert out.by_model == {"gpt-sol": 600_000_000}
        assert out.ucents == 600_000_000

    def test_unpriced_keeps_the_logged_spelling(self, table: PriceTable) -> None:
        out = price_usage({"Unver-In-20260101": Usage(input=4)}, table)
        assert list(out.unpriced) == ["Unver-In-20260101"]

    def test_each_model_is_rounded_on_its_own(self, table: PriceTable) -> None:
        # per-model totals are integers; the session total is their exact sum
        out = price_usage({"tiny": Usage(input=1), "tiny-20260101": Usage(input=1)}, table)
        assert out.by_model == {"tiny": 2}  # two 0.5-ucent lines, each rounded up, merged under one key
        assert out.ucents == 2

    def test_empty_session(self, table: PriceTable) -> None:
        out = price_usage({}, table)
        assert out == SessionCost() and out.complete and out.ucents == 0

    def test_partial_cached_price_still_counts_toward_the_total(self, table: PriceTable) -> None:
        out = price_usage({"partial": Usage(cache_read=1_000_000)}, table)
        assert out.ucents == 200_000_000 and out.complete


class TestReprice:
    def test_all_usage_is_repriced_on_the_target_model(self, table: PriceTable) -> None:
        usage = {"claude-x": Usage(input=1_000_000, output=1_000_000), "gpt-sol": Usage(cache_read=2_000_000)}
        # everything on gpt-sol: 1M*2.0 + 1M*10 + 2M*0.10 = 12_200_000 dollar-tokens -> $12.20
        assert reprice(usage, "gpt-sol", table) == 1_220_000_000

    def test_target_model_is_resolved_like_any_logged_name(self, table: PriceTable) -> None:
        usage = {"claude-x": Usage(input=1_000_000)}
        assert reprice(usage, "SOL", table) == reprice(usage, "gpt-sol", table) == 200_000_000

    def test_rounds_once_over_the_summed_usage(self, table: PriceTable) -> None:
        usage = {"a": Usage(input=1), "b": Usage(output=1)}
        assert reprice(usage, "tiny", table) == 1  # 0.5 + 0.5, not 1 + 1

    def test_no_verified_price_means_none(self, table: PriceTable) -> None:
        usage = {"claude-x": Usage(input=1_000)}
        assert reprice(usage, "unver-in", table) is None
        assert reprice(usage, "unver-out", table) is None
        assert reprice(usage, "not-in-the-table", table) is None

    def test_repricing_on_the_same_model_matches_price_usage(self, table: PriceTable) -> None:
        usage = {"claude-x": Usage(input=10_000, cache_read=500_000, cache_write=40_000, output=8_000)}
        assert reprice(usage, "claude-x", table) == price_usage(usage, table).ucents

    def test_empty_usage_costs_zero(self, table: PriceTable) -> None:
        assert reprice({}, "gpt-sol", table) == 0

    def test_one_hour_writes_are_summed_with_the_rest_and_repriced_at_the_targets_rate(self, table: PriceTable) -> None:
        usage = {"claude-x": Usage(cache_write=1_000_000, cache_write_1h=1_000_000), "plain": Usage(cache_write_1h=1_000_000)}
        assert reprice(usage, "claude-x", table) == 375_000_000 + 2 * 600_000_000
        assert reprice(usage, "cw-only", table) == 3 * 500_000_000  # its 5-minute rate covers both kinds
        assert reprice(usage, "gpt-sol", table) == 3 * 200_000_000  # no cache-write prices at all: the input rate

    def test_target_uses_its_own_cache_fallbacks(self, table: PriceTable) -> None:
        usage = {"claude-x": Usage(cache_write=1_000_000, cache_read=1_000_000)}
        # plain has neither cached_input nor cache_write: both buckets at its input rate 1.5
        assert reprice(usage, "plain", table) == 300_000_000
        assert reprice(usage, "claude-x", table) == 405_000_000  # 3.75 + 0.30 -> $4.05

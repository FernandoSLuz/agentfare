"""Router: scoring, advice, weekly savings and the pre-flight pick for `agentfare run`."""

from __future__ import annotations

import ast
import builtins
import dataclasses
import io
import os
import zoneinfo
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agentfare import router as router_module
from agentfare.config import DEFAULT_CONFIG, DEFAULT_CONFIG_YAML, Config, ConfigError
from agentfare.db import Store, StoredSession
from agentfare.models import SessionRecord, Usage
from agentfare.money import cents_to_ucents, usd_to_ucents
from agentfare.pricing import PriceTable, load_pricing
from agentfare.router import (
    MAX_SCORE,
    Advice,
    WeekSavings,
    advise_sessions,
    count_files,
    format_advice_line,
    min_cost_ucents,
    pick_for_run,
    saving_ratio,
    savings_split,
    score_session,
    total_savings,
    weekly_savings,
)

Make = Callable[..., SessionRecord]
MakeConfig = Callable[..., Config]
Stored = Callable[..., StoredSession]

# Round thresholds so every boundary is easy to read; the shipped defaults are checked separately.
SIGNALS = {
    "input_tokens": {"medium": 1_000, "heavy": 5_000},
    "tool_calls": {"medium": 10, "heavy": 50},
    "files_touched": {"medium": 2, "heavy": 6},
    "error_retries": {"medium": 3, "heavy": 9},
    "active_minutes": {"medium": 10, "heavy": 60},
}
ROUTER = {
    "signals": SIGNALS,
    "tiers": {"light_max": 2, "medium_max": 5},
    "recommend": {"light": "small", "medium": "small", "heavy": None},
    "min_saving_ratio": 1.2,
}
PRICES = {
    "big": {"input": 10, "cached_input": 1, "output": 50, "cache_write": 12.5},
    "small": {"input": 2, "cached_input": 0.10, "output": 10},
    "twin": {"input": 2, "cached_input": 0.10, "output": 10},
    "tiny": {"input": 1, "output": 5},
    "unver": {"input": "unverified", "output": 5},
}
MILLION_OUT = Usage(output=1_000_000)
HEAVY = {"tool_calls": 50, "files_touched": 6, "error_retries": 9}  # 6 points with the ROUTER config: a heavy session
MEDIUM = {"tool_calls": 10, "files_touched": 2, "error_retries": 3, "active_seconds": 600}  # 4 points: medium


def usd(amount: str) -> int:
    """Exact ucents of a dollar amount written as text (no floats in expected values either)."""
    return usd_to_ucents(amount)


@pytest.fixture
def cfg(make_config: MakeConfig) -> Config:
    return make_config(timezone="UTC", router=ROUTER)


@pytest.fixture
def table(make_table: Callable[..., PriceTable]) -> PriceTable:
    return make_table(PRICES)


@pytest.fixture
def stored(make_record: Make) -> Stored:
    """``stored(tool_calls=12, usage={...})``: a session as the store returns it. Nothing scores unless asked to."""

    def make(**kw: Any) -> StoredSession:
        with Store(":memory:") as store:
            store.upsert_session(make_record(**({"usage": {}, "active_seconds": 0} | kw)))
            return store.sessions()[0]

    return make


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as s:
        yield s


def only(a: list[Advice]) -> Advice:
    assert len(a) == 1
    return a[0]


def on(stored: Stored, model: str, usage: Usage = MILLION_OUT, **kw: Any) -> StoredSession:
    """A session that used ``usage`` on ``model`` (1M output tokens by default)."""
    return stored(usage={model: usage}, **kw)


# ---- score_session ------------------------------------------------------------------------
class TestSignals:
    @pytest.mark.parametrize(
        ("signal", "below_medium", "medium", "below_heavy", "heavy"),
        [("tool_calls", 9, 10, 49, 50), ("files_touched", 1, 2, 5, 6), ("error_retries", 2, 3, 8, 9)],
    )
    def test_count_signal_boundaries(self, cfg: Config, stored: Stored, signal: str, below_medium: int, medium: int, below_heavy: int, heavy: int) -> None:
        points = [score_session(stored(**{signal: n}), cfg).points[signal] for n in (below_medium, medium, below_heavy, heavy)]
        assert points == [0, 1, 1, 2]

    def test_input_tokens_boundaries_use_every_input_bucket_and_no_output(self, cfg: Config, stored: Stored) -> None:
        def points(usage: dict[str, Usage]) -> int:
            return score_session(stored(usage=usage), cfg).points["input_tokens"]

        assert [points({"a": Usage(input=n)}) for n in (999, 1_000, 4_999, 5_000)] == [0, 1, 1, 2]
        assert points({"a": Usage(output=9_000_000)}) == 0  # output is not input
        mixed = {"a": Usage(cache_read=400), "b": Usage(cache_write=300, cache_write_1h=300)}  # models add up; reads and both writes are input
        assert points(mixed) == 1
        assert score_session(stored(usage=mixed), cfg).signals["input_tokens"] == 1_000

    def test_active_minutes_come_from_active_seconds(self, cfg: Config, stored: Stored) -> None:
        def points(seconds: int) -> int:
            return score_session(stored(active_seconds=seconds, wall_seconds=100_000), cfg).points["active_minutes"]

        assert [points(s) for s in (599, 600, 3_599, 3_600)] == [0, 1, 1, 2]

    def test_wall_time_does_not_count_only_active_time(self, cfg: Config, stored: Stored) -> None:
        scored = score_session(stored(wall_seconds=36_000, active_seconds=60), cfg)
        assert (scored.points["active_minutes"], scored.signals["active_minutes"]) == (0, 1.0)

    def test_a_fractional_threshold_compares_exactly(self, make_config: MakeConfig, stored: Stored) -> None:
        """1.15 minutes is exactly 69 seconds: the threshold is the decimal that was written, not a binary float."""
        signals = {**SIGNALS, "active_minutes": {"medium": 1.15, "heavy": 2.3}}
        cfg = make_config(timezone="UTC", router={**ROUTER, "signals": signals})
        assert [score_session(stored(active_seconds=s), cfg).points["active_minutes"] for s in (68, 69, 137, 138)] == [0, 1, 1, 2]

    def test_measured_values_are_reported(self, cfg: Config, stored: Stored) -> None:
        s = stored(usage={"a": Usage(input=100, cache_read=200)}, tool_calls=7, files_touched=3, error_retries=1, active_seconds=90)
        assert score_session(s, cfg).signals == {"input_tokens": 300, "tool_calls": 7, "files_touched": 3, "error_retries": 1, "active_minutes": 1.5}

    def test_an_empty_session_scores_zero(self, cfg: Config, stored: Stored) -> None:
        scored = score_session(stored(), cfg)
        assert (scored.score, scored.tier, scored.points) == (0, "light", dict.fromkeys(SIGNALS, 0))


STEPS = [  # (medium, heavy) values of the five signals, in the order the points are handed out
    ({"tool_calls": 10}, {"tool_calls": 50}),
    ({"files_touched": 2}, {"files_touched": 6}),
    ({"error_retries": 3}, {"error_retries": 9}),
    ({"active_seconds": 600}, {"active_seconds": 3_600}),
    ({"usage": {"a": Usage(input=1_000)}}, {"usage": {"a": Usage(input=5_000)}}),
]


def session_worth(stored: Stored, score: int) -> StoredSession:
    """A session that scores exactly ``score`` (0-10) with the ROUTER thresholds: 2 points per heavy signal, then 1 for a medium one."""
    kw: dict[str, Any] = {}
    for i, (medium, heavy) in enumerate(STEPS):
        if i < score // 2:
            kw |= heavy
        elif i == score // 2 and score % 2:
            kw |= medium
    return stored(**kw)


class TestTiers:
    @pytest.mark.parametrize(
        ("score", "tier"),
        [(0, "light"), (1, "light"), (2, "light"), (3, "medium"), (5, "medium"), (6, "heavy"), (10, "heavy")],
    )
    def test_tier_cut_points(self, cfg: Config, stored: Stored, score: int, tier: str) -> None:
        scored = score_session(session_worth(stored, score), cfg)
        assert (scored.score, scored.tier) == (score, tier)

    def test_the_cut_points_are_configurable(self, make_config: MakeConfig, stored: Stored) -> None:
        tight = make_config(timezone="UTC", router={**ROUTER, "tiers": {"light_max": 0, "medium_max": 1}})
        assert [score_session(session_worth(stored, n), tight).tier for n in (0, 1, 2)] == ["light", "medium", "heavy"]

    def test_max_score_is_two_points_per_signal(self) -> None:
        assert MAX_SCORE == 10


class TestExplain:
    def test_lists_the_signals_that_scored_in_a_fixed_order(self, cfg: Config, stored: Stored) -> None:
        s = stored(usage={"a": Usage(input=3_416_900)}, tool_calls=45, files_touched=20, error_retries=1, active_seconds=5_930)
        assert score_session(s, cfg).explain() == "score 7/10 (heavy): input_tokens 3.4M +2, tool_calls 45 +1, files_touched 20 +2, active_minutes 98 +2"

    def test_says_so_when_nothing_scored(self, cfg: Config, stored: Stored) -> None:
        assert score_session(stored(), cfg).explain() == "score 0/10 (light): no signal at or above medium"

    @pytest.mark.parametrize(
        ("tokens", "shown"),
        [(999, "999"), (1_000, "1k"), (312_499, "312k"), (999_999, "999k"), (1_000_000, "1.0M"), (24_649_999, "24.6M")],
    )
    def test_compact_token_counts(self, make_config: MakeConfig, stored: Stored, tokens: int, shown: str) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "signals": {**SIGNALS, "input_tokens": {"medium": 1, "heavy": 10**9}}})
        assert f"input_tokens {shown} +1" in score_session(stored(usage={"a": Usage(input=tokens)}), cfg).explain()

    def test_minutes_round_down_so_the_shown_value_never_overstates_a_threshold(self, cfg: Config, stored: Stored) -> None:
        assert "active_minutes 10 +1" in score_session(stored(active_seconds=659), cfg).explain()
        assert "active_minutes" not in score_session(stored(active_seconds=599), cfg).explain()


class TestConfigErrors:
    @pytest.mark.parametrize(
        ("router", "message"),
        [
            ({"signals": {"tool_calls": {"medium": 9, "heavy": 3}}}, r"medium \(9\) must not be above heavy \(3\)"),
            ({"signals": {"tool_calls": {"medium": "lots", "heavy": 3}}}, r"router.signals.tool_calls.medium must be a number"),
            ({"signals": {"tool_calls": {"medium": True, "heavy": 3}}}, r"router.signals.tool_calls.medium must be a number"),
            ({"signals": {"tool_calls": {"medium": -1, "heavy": 3}}}, r"router.signals.tool_calls.medium must be a number of at least 0"),
            ({"tiers": {"light_max": 4, "medium_max": 3}}, r"light_max \(4\) must not be above medium_max \(3\)"),
            ({"tiers": {"light_max": 1.5, "medium_max": 3}}, r"router.tiers.light_max must be a whole number"),
            ({"tiers": {"light_max": None, "medium_max": 3}}, r"router.tiers.light_max must be a number"),
            ({"recommend": {"fast": "small"}}, r"router.recommend.fast: unknown tier"),
            ({"recommend": {"light": 3}}, r"router.recommend.light must be a model id or null"),
            ({"min_saving_ratio": 0.9}, r"router.min_saving_ratio must be at least 1"),
            ({"min_saving_ratio": "1.2"}, r"router.min_saving_ratio must be a number"),
            ({"advise": {"min_cost_cents": -5}}, r"router.advise.min_cost_cents must be a number of at least 0"),
        ],
    )
    def test_a_bad_value_names_its_key(self, make_config: MakeConfig, stored: Stored, table: PriceTable, router: dict[str, Any], message: str) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, **router})

        def use() -> None:
            advise_sessions([stored()], cfg, table)
            min_cost_ucents(cfg)

        with pytest.raises(ConfigError, match=message):
            use()

    def test_a_signal_that_does_not_exist_is_ignored_and_the_known_ones_are_still_checked(
        self, make_config: MakeConfig, stored: Stored, table: PriceTable
    ) -> None:
        """Deliberate change: ``router.signals.tool_call`` used to stop ``advise``, ``report`` and ``run`` with an error. The config
        already warns about it once, with a did-you-mean, so the score just does not use it."""
        typo = {**SIGNALS, "tool_call": {"medium": 1, "heavy": 2}, "files": "not even a mapping"}
        cfg = make_config(timezone="UTC", router={**ROUTER, "signals": typo})
        s = stored(tool_calls=1)
        assert score_session(s, cfg) == score_session(s, make_config(timezone="UTC", router=ROUTER))  # tool_call did not count
        assert only(advise_sessions([s], cfg, table)).scored.points["tool_calls"] == 0
        bad = make_config(timezone="UTC", router={**ROUTER, "signals": {**typo, "tool_calls": {"medium": 9, "heavy": 3}}})
        with pytest.raises(ConfigError, match=r"router.signals.tool_calls: medium \(9\) must not be above heavy \(3\)"):
            score_session(s, bad)

    def test_score_session_validates_too(self, make_config: MakeConfig, stored: Stored) -> None:
        with pytest.raises(ConfigError, match="router.tiers"):
            score_session(stored(), make_config(router={"tiers": {"light_max": 9, "medium_max": 1}}))


# ---- advise_sessions ----------------------------------------------------------------------
class TestAdvice:
    def test_a_light_session_on_a_pricey_model_is_switched(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([on(stored, "big")], cfg, table))
        assert (a.scored.tier, a.model, a.actual_complete) == ("light", "big", True)
        assert a.actual_ucents == usd("50")  # 1M output tokens at $50/M
        assert (a.recommended_model, a.recommended_ucents) == ("small", usd("10"))
        assert a.switch and a.saved_ucents == usd("40")
        assert (a.ratio, a.note) == (5.0, "")

    def test_the_whole_usage_is_repriced_on_the_recommended_model(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        usage = {
            "big": Usage(input=1_000_000, cache_read=1_000_000, cache_write=1_000_000, output=100_000),
            "tiny": Usage(input=1_000_000),
        }
        a = only(advise_sessions([stored(usage=usage)], cfg, table))
        assert a.actual_ucents == usd("29.5")  # big: 10 + 1 + 12.5 + 5, tiny: 1
        # 2M input at $2, 1M cached at $0.10, 1M written at the input rate ($2: small has no cache-write price), 0.1M output at $10
        assert a.recommended_ucents == usd("7.1")
        assert a.model == "big"  # the primary model is the one with the most output

    def test_the_tier_picks_the_model(self, make_config: MakeConfig, table: PriceTable, stored: Stored) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "recommend": {"light": "tiny", "medium": "small", "heavy": "twin"}})
        sessions = [on(stored, "big"), on(stored, "big", **MEDIUM), on(stored, "big", **HEAVY)]
        got = advise_sessions(sessions, cfg, table)
        assert [(a.scored.tier, a.recommended_model) for a in got] == [("light", "tiny"), ("medium", "small"), ("heavy", "twin")]

    def test_heavy_is_kept_when_no_model_is_configured_for_it(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([on(stored, "big", **HEAVY)], cfg, table))
        assert (a.scored.tier, a.switch, a.recommended_model, a.recommended_ucents, a.ratio, a.saved_ucents) == ("heavy", False, None, None, None, 0)
        assert (a.note, a.actual_ucents) == ("heavy: keep", usd("50"))

    def test_a_missing_tier_entry_means_keep(self, make_config: MakeConfig, table: PriceTable, stored: Stored) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "recommend": {"light": None}})
        a = only(advise_sessions([on(stored, "big")], cfg, table))
        assert (a.switch, a.note) == (False, "light: keep")

    def test_already_cheaper_than_the_recommendation(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([on(stored, "tiny")], cfg, table))  # $5 actual, $10 on small
        assert (a.switch, a.note, a.actual_ucents) == (False, "already cheaper than small", usd("5"))
        assert (a.recommended_model, a.recommended_ucents) == (None, None)

    def test_equal_cost_is_not_a_switch_even_at_a_ratio_of_one(self, make_config: MakeConfig, table: PriceTable, stored: Stored) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "min_saving_ratio": 1})
        a = only(advise_sessions([on(stored, "twin")], cfg, table))  # the same price as small
        assert (a.switch, a.note) == (False, "already cheaper than small")

    def test_already_on_the_recommended_model(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([on(stored, "small")], cfg, table))
        assert (a.switch, a.note) == (False, "already on small")

    def test_an_alias_of_the_recommended_model_counts_as_already_on_it(self, cfg: Config, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        table = make_table(PRICES, aliases={"small-2026": "small"})
        a = only(advise_sessions([on(stored, "small-2026")], cfg, table))
        assert (a.switch, a.note) == (False, "already on small")

    def test_a_mix_of_the_recommended_model_and_another_is_repriced_not_skipped(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([stored(usage={"small": MILLION_OUT, "big": MILLION_OUT})], cfg, table))
        assert a.switch and (a.actual_ucents, a.recommended_ucents) == (usd("60"), usd("20"))

    def test_zero_usage_is_reported_as_no_cost(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([stored()], cfg, table))
        assert (a.actual_ucents, a.actual_complete, a.switch, a.note) == (0, True, False, "no cost")

    def test_a_free_recommended_model_does_not_divide_by_zero(self, cfg: Config, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        table = make_table({"big": PRICES["big"], "small": {"input": 0, "output": 0}})
        a = only(advise_sessions([on(stored, "big", session_id="free")], cfg, table))
        assert a.switch and a.recommended_ucents == 0 and a.ratio is None
        assert format_advice_line(a) == "session free: you spent $50.00 on big → small would likely have handled this for ~$0.00 (free)"

    def test_advice_keeps_the_input_order(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        sessions = [on(stored, "tiny", session_id="a"), on(stored, "big", session_id="b")]
        assert [a.session.session_id for a in advise_sessions(sessions, cfg, table)] == ["a", "b"]


SUNDAY, MONDAY = "2026-09-27T23:45:00Z", "2026-09-28T00:00:00Z"  # the last slot of ISO week 2026-W39 and the first of W40, in UTC


def spread(stored: Stored, slots: dict[tuple[str, str], Usage], **kw: Any) -> StoredSession:
    """A session as the store returns it for a period: ``slots`` is what was spent when, ``usage`` their sum per model."""
    usage: dict[str, Usage] = {}
    for (_, model), used in slots.items():
        usage[model] = usage.get(model, Usage()) + used
    return dataclasses.replace(stored(**kw), usage=usage, slots=slots)


class TestSlots:
    """Money is attributed to when it was spent: advice is priced slot by slot on the usage inside the period."""

    def test_the_spend_of_a_session_is_kept_per_slot(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = spread(stored, {(SUNDAY, "big"): MILLION_OUT, (MONDAY, "big"): Usage(output=500_000)})
        a = only(advise_sessions([s], cfg, table))
        assert [(x.slot, x.actual_ucents, x.recommended_ucents) for x in a.spend] == [(SUNDAY, usd("50"), usd("10")), (MONDAY, usd("25"), usd("5"))]
        assert (a.actual_ucents, a.recommended_ucents) == (usd("75"), usd("15"))  # the sums of the slots, to the micro-cent

    def test_models_of_one_slot_are_repriced_together_and_a_kept_session_has_equal_columns(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = spread(stored, {(SUNDAY, "big"): MILLION_OUT, (SUNDAY, "tiny"): MILLION_OUT, (MONDAY, "tiny"): MILLION_OUT})
        a = only(advise_sessions([s], cfg, table))
        assert [(x.slot, x.actual_ucents, x.recommended_ucents) for x in a.spend] == [(SUNDAY, usd("55"), usd("20")), (MONDAY, usd("5"), usd("10"))]
        kept = only(advise_sessions([spread(stored, {(SUNDAY, "big"): MILLION_OUT}, **HEAVY)], cfg, table))
        assert [(x.actual_ucents, x.recommended_ucents) for x in kept.spend] == [(usd("50"), usd("50"))]

    def test_a_session_with_no_slots_is_all_at_its_start_slot(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([on(stored, "big", started_at="2026-09-30T10:07:31Z")], cfg, table))
        assert [(x.slot, x.actual_ucents) for x in a.spend] == [("2026-09-30T10:00:00Z", usd("50"))]

    def test_a_session_without_usage_still_has_its_start_slot_at_zero(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([stored(started_at="2026-09-30T10:20:00Z")], cfg, table))
        assert [(x.slot, x.actual_ucents, x.recommended_ucents) for x in a.spend] == [("2026-09-30T10:15:00Z", 0, 0)]

    def test_the_slots_are_rounded_one_by_one_like_the_rows_of_summary(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        """1 token at $1.005/M is 100.5 micro-cents: each slot rounds half up to 101, where the two together would be 201."""
        table = make_table({"a": {"input": "1.005", "output": 1}, "b": {"input": "0.1005", "output": 1}})
        cfg = make_config(timezone="UTC", router={**ROUTER, "recommend": {"light": "b", "medium": "b", "heavy": "b"}})
        a = only(advise_sessions([spread(stored, {(SUNDAY, "a"): Usage(input=1_000_000), (MONDAY, "a"): Usage(input=1_000_000)})], cfg, table))
        assert [x.actual_ucents for x in a.spend] == [usd("1.005")] * 2 and a.actual_ucents == usd("2.01")
        tiny = only(advise_sessions([spread(stored, {(SUNDAY, "a"): Usage(input=1), (MONDAY, "a"): Usage(input=1)})], cfg, table))
        assert [x.actual_ucents for x in tiny.spend] == [101, 101] and tiny.actual_ucents == 202  # not 201

    def test_the_ratio_test_uses_the_money_inside_the_period(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        clipped = dataclasses.replace(spread(stored, {(MONDAY, "big"): MILLION_OUT}), clipped=True)
        a = only(advise_sessions([clipped], cfg, table))
        assert (a.actual_ucents, a.recommended_ucents, a.saved_ucents) == (usd("50"), usd("10"), usd("40"))

    def test_an_unpriced_model_in_one_slot_leaves_the_whole_session_unadvised(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([spread(stored, {(SUNDAY, "big"): MILLION_OUT, (MONDAY, "unver"): MILLION_OUT})], cfg, table))
        assert (a.actual_complete, a.switch, a.note, a.actual_ucents) == (False, False, "no verified price", usd("50"))
        assert [x.slot for x in a.spend] == [SUNDAY, MONDAY]  # still placed in both weeks, to be counted as skipped there


class TestWholeSessionDecision:
    """A period shows the money of its own slots, but the decision (model, ratio gate, label) does not depend on the period."""

    WEEK_2 = "2026-09-28T00:00:00Z"  # Monday 00:00 UTC: where ISO week 2026-W40 starts

    def put(self, store: Store, make_record: Make, slots: dict[tuple[str, str], Usage], **kw: Any) -> None:
        usage: dict[str, Usage] = {}
        for (_, model), used in slots.items():
            usage[model] = usage.get(model, Usage()) + used
        store.upsert_session(make_record(usage=usage, slots=slots, active_seconds=0, **kw))

    def view(self, store: Store, cfg: Config, table: PriceTable, since: str | None = None, until: str | None = None) -> list[Advice]:
        sessions = store.sessions(since=since, until=until)
        return advise_sessions(sessions, cfg, table, store.whole_usage(s.key for s in sessions if s.clipped))

    def test_a_slice_is_named_after_the_model_it_used_inside_the_period_not_after_the_rest_of_the_session(
        self, cfg: Config, table: PriceTable, store: Store, make_record: Make
    ) -> None:
        """A session with ``tiny`` on Sunday (the most output of the session) and ``big`` on Monday, looked at on Monday, is ``big``."""
        self.put(store, make_record, {(SUNDAY, "tiny"): Usage(output=2_000_000), (MONDAY, "big"): MILLION_OUT}, session_id="span")
        monday = only(self.view(store, cfg, table, since=self.WEEK_2))
        assert monday.session.clipped and (monday.model, monday.other_models) == ("big", 0)
        assert format_advice_line(monday).startswith("session span: you spent $50.00 on big →")  # no "(+1 more model)" for a model that is not in the view
        sunday = only(self.view(store, cfg, table, until=self.WEEK_2))
        assert (sunday.model, sunday.other_models) == ("tiny", 0)
        whole = only(self.view(store, cfg, table))
        assert (whole.model, whole.other_models) == ("tiny", 1)  # the most output of the two, and the other one counted
        assert "on tiny (+1 more model) →" in format_advice_line(whole)

    def test_the_weekly_rows_of_an_all_time_view_are_the_rows_of_each_single_week(
        self, cfg: Config, table: PriceTable, store: Store, make_record: Make
    ) -> None:
        """``big`` ($50) in week 1 and ``tiny`` ($5, cheaper than the $10 ``small`` would cost) in week 2: a light session the router
        moves as a whole. Week 2 used to read 0 in its own view and minus $5 in the all-time one."""
        self.put(store, make_record, {(SUNDAY, "big"): MILLION_OUT, (MONDAY, "tiny"): MILLION_OUT}, session_id="span")
        everything = weekly_savings(self.view(store, cfg, table), cfg)
        single = [
            only(weekly_savings(self.view(store, cfg, table, since=since, until=until), cfg))
            for since, until in ((None, self.WEEK_2), (self.WEEK_2, None))
        ]
        assert [w.week for w in everything] == ["2026-W39", "2026-W40"]
        assert everything == single  # WeekSavings compare on money and counts
        assert [(w.actual_ucents, w.recommended_ucents, w.saved_ucents) for w in everything] == [
            (usd("50"), usd("10"), usd("40")), (usd("5"), usd("10"), -usd("5")),
        ]
        assert total_savings(everything).saved_ucents == usd("35")  # the whole session: $55 -> $20, saved $35 less what week 2 gave back

    def test_the_ratio_gate_and_the_target_come_from_the_whole_session(
        self, make_config: MakeConfig, make_table: Callable[..., PriceTable], store: Store, make_record: Make
    ) -> None:
        """``a`` costs 1.1x what ``b`` would (below the minimum of 1.2x) in week 1 and 10x in week 2: 5.5x over the whole session,
        so the week 1 slice, which would not be advised on its own, is part of an advised switch."""
        table = make_table({"a": {"input": 1, "output": Decimal("11")}, "hot": {"input": 1, "output": 100}, "b": {"input": 1, "output": 10}})
        cfg = make_config(timezone="UTC", router={**ROUTER, "recommend": {"light": "b", "medium": "b", "heavy": "b"}})
        self.put(store, make_record, {(SUNDAY, "a"): MILLION_OUT, (MONDAY, "hot"): MILLION_OUT}, session_id="span")
        week_1 = only(self.view(store, cfg, table, until=self.WEEK_2))
        assert (week_1.switch, week_1.recommended_model, week_1.note) == (True, "b", "")
        assert (week_1.actual_ucents, week_1.recommended_ucents) == (usd("11"), usd("10"))  # only 1.1x on its own
        assert only(self.view(store, cfg, table, since=self.WEEK_2)).saved_ucents == usd("90")

    def test_a_session_already_cheaper_as_a_whole_is_kept_in_every_period(
        self, cfg: Config, table: PriceTable, store: Store, make_record: Make
    ) -> None:
        """4M output tokens on ``tiny`` in week 1 and a little ``big`` in week 2: $25 against the $41 the same tokens cost on ``small``,
        so the router saves nothing over the whole session and no period reads as a saving, whatever its own slice would say."""
        self.put(store, make_record, {(SUNDAY, "tiny"): Usage(output=4_000_000), (MONDAY, "big"): Usage(output=100_000)}, session_id="span")
        # $20 + $5 = $25 actual; on ``small``: 4.1M output tokens = $41, so it is cheaper already
        for since, until in ((None, None), (None, self.WEEK_2), (self.WEEK_2, None)):
            a = only(self.view(store, cfg, table, since=since, until=until))
            assert (a.switch, a.note, a.saved_ucents) == (False, "already cheaper than small", 0), (since, until)

    def test_a_slice_that_costs_no_less_on_the_advised_model_says_so(
        self, cfg: Config, table: PriceTable, store: Store, make_record: Make
    ) -> None:
        self.put(store, make_record, {(SUNDAY, "big"): MILLION_OUT, (MONDAY, "tiny"): MILLION_OUT}, session_id="span")
        line = format_advice_line(only(self.view(store, cfg, table, since=self.WEEK_2)))
        assert line == "session span: you spent $5.00 on tiny → small would likely have handled this for ~$10.00 (not cheaper for the part inside this period)"

    def test_an_unpriced_model_outside_the_period_decides_on_what_is_inside_it(
        self, cfg: Config, table: PriceTable, store: Store, make_record: Make
    ) -> None:
        self.put(store, make_record, {(SUNDAY, "unver"): MILLION_OUT, (MONDAY, "big"): MILLION_OUT}, session_id="span")
        a = only(self.view(store, cfg, table, since=self.WEEK_2))
        assert (a.actual_complete, a.switch, a.actual_ucents, a.recommended_ucents) == (True, True, usd("50"), usd("10"))
        assert only(self.view(store, cfg, table)).note == "no verified price"


class TestSavingRatio:
    """A switch is advised only when it is at least ``min_saving_ratio`` times cheaper, in integer arithmetic."""

    def advise(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored, ratio: Any, actual: str, target: str) -> Advice:
        table = make_table({"a": {"input": 1, "output": Decimal(actual)}, "b": {"input": 1, "output": Decimal(target)}})
        cfg = make_config(timezone="UTC", router={**ROUTER, "min_saving_ratio": ratio, "recommend": {"light": "b", "medium": "b", "heavy": "b"}})
        return only(advise_sessions([on(stored, "a")], cfg, table))

    def test_exactly_at_the_ratio_switches(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        assert self.advise(make_config, make_table, stored, 1.2, "6", "5").switch

    def test_a_hair_below_the_ratio_does_not(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        a = self.advise(make_config, make_table, stored, 1.2, "5.99", "5")
        assert not a.switch and a.note == "b is only 1.1x cheaper (minimum 1.2x)"

    @pytest.mark.parametrize(("ratio", "actual", "target", "shown"), [(1.2, "5.99", "5", "1.1x"), (1.25, "6.24", "5", "1.2x"), (2, "9.99", "5", "1.9x"), (1.05, "5.2", "5", "1.0x")])
    def test_the_ratio_in_the_note_is_rounded_down_so_it_never_reads_as_the_minimum(
        self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored, ratio: Any, actual: str, target: str, shown: str
    ) -> None:
        note = self.advise(make_config, make_table, stored, ratio, actual, target).note
        assert note == f"b is only {shown} cheaper (minimum {ratio}x)"

    def test_the_note_shows_the_ratio_rounded_and_the_minimum_as_configured(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        assert self.advise(make_config, make_table, stored, 2, "3", "2").note == "b is only 1.5x cheaper (minimum 2x)"

    def test_a_whole_number_minimum_keeps_its_zeros(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        assert self.advise(make_config, make_table, stored, 10, "30", "5").note == "b is only 6.0x cheaper (minimum 10x)"

    def test_the_float_in_the_config_is_converted_exactly(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        """3.3 / 3.0 is exactly 1.1, but 3.0 * 1.1 in binary floating point is 3.3000000000000003, which would say no."""
        assert 3.0 * 1.1 > 3.3
        assert self.advise(make_config, make_table, stored, 1.1, "3.3", "3.0").switch

    def test_the_ratio_is_an_exact_fraction(self, make_config: MakeConfig) -> None:
        assert saving_ratio(make_config(router={"min_saving_ratio": 1.2})) == (6, 5)
        assert saving_ratio(make_config(router={"min_saving_ratio": 3})) == (3, 1)

    def test_the_advised_multiple_is_reported(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        assert self.advise(make_config, make_table, stored, 1.2, "42", "8.4").ratio == 5.0


class TestUnverifiedPrices:
    def test_unpriced_usage_is_not_advised_on(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([on(stored, "unver")], cfg, table))
        assert (a.actual_complete, a.actual_ucents, a.switch, a.recommended_model, a.note) == (False, 0, False, None, "no verified price")

    def test_a_model_missing_from_the_table_is_the_same(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        assert not only(advise_sessions([on(stored, "mystery")], cfg, table)).actual_complete

    def test_a_partly_priced_session_keeps_the_priced_part_but_gets_no_advice(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        a = only(advise_sessions([stored(usage={"big": MILLION_OUT, "unver": MILLION_OUT})], cfg, table))
        assert (a.actual_complete, a.actual_ucents, a.switch, a.saved_ucents) == (False, usd("50"), False, 0)

    def test_a_recommended_model_without_a_price_gives_a_note(self, make_config: MakeConfig, table: PriceTable, stored: Stored) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "recommend": {"light": "unver", "medium": "nope", "heavy": None}})
        a = only(advise_sessions([on(stored, "big")], cfg, table))
        assert (a.switch, a.note, a.actual_complete) == (False, "unver has no verified price", True)


# ---- lines --------------------------------------------------------------------------------
class TestAdviceLine:
    def test_switch_line(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = on(stored, "big", session_id="0193a5f2-77c1-7abc-9def-0123456789ab")
        assert format_advice_line(only(advise_sessions([s], cfg, table))) == (
            "session 456789ab: you spent $50.00 on big → small would likely have handled this for ~$10.00 (5.0x cheaper)"
        )

    def test_sub_cent_amounts_keep_four_decimals(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = on(stored, "big", Usage(output=421), session_id="abcdef0123456789")
        assert format_advice_line(only(advise_sessions([s], cfg, table))) == (
            "session 23456789: you spent $0.0211 on big → small would likely have handled this for ~$0.0042 (5.0x cheaper)"
        )

    @pytest.mark.parametrize(("encoding", "arrow"), [("utf-8", "→"), ("UTF-16", "→"), ("ascii", "->"), ("latin-1", "->"), ("cp437", "->"), ("no-such-codec", "->")])
    def test_the_arrow_is_ascii_when_the_console_cannot_write_it(self, cfg: Config, table: PriceTable, stored: Stored, encoding: str, arrow: str) -> None:
        line = format_advice_line(only(advise_sessions([on(stored, "big", session_id="arrow")], cfg, table)), encoding)
        assert line == f"session arrow: you spent $50.00 on big {arrow} small would likely have handled this for ~$10.00 (5.0x cheaper)"

    def test_keep_line(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = on(stored, "big", session_id="s-heavy", **HEAVY)
        assert format_advice_line(only(advise_sessions([s], cfg, table))) == "session s-heavy: you spent $50.00 on big: no switch advised, heavy: keep"

    def test_already_cheaper_line(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        assert format_advice_line(only(advise_sessions([on(stored, "tiny", session_id="cheap")], cfg, table))) == (
            "session cheap: you spent $5.00 on tiny: no switch advised, already cheaper than small"
        )

    def test_unpriced_line_says_priced_part_only(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = stored(usage={"big": MILLION_OUT, "unver": Usage(output=500_000)}, session_id="mixed")
        assert format_advice_line(only(advise_sessions([s], cfg, table))) == (
            "session mixed: you spent $50.00 (priced part only) on big (+1 more model): no advice, no verified price"
        )

    def test_a_session_without_any_tokens_names_no_model(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        assert format_advice_line(only(advise_sessions([stored(session_id="idle")], cfg, table))) == (
            "session idle: you spent $0.00 (no token usage recorded): no switch advised, no cost"
        )

    def test_a_session_with_nothing_priced_has_an_unknown_amount(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        assert format_advice_line(only(advise_sessions([on(stored, "unver", session_id="ghost")], cfg, table))) == (
            "session ghost: you spent an unknown amount on unver: no advice, no verified price"
        )

    def test_more_models_are_counted(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = stored(usage={"big": MILLION_OUT, "tiny": Usage(output=1), "small": Usage(output=1)}, session_id="three")
        assert "on big (+2 more models) →" in format_advice_line(only(advise_sessions([s], cfg, table)))

    def test_the_router_needs_no_rendering_code_for_its_amounts_or_ids(self) -> None:
        """Amounts and ids come from ``money`` and ``models``; ``render`` (Rich, Typer) is for the commands that print."""
        tree = ast.parse(Path(router_module.__file__).read_text())
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not [m for m in imported if m.split(".")[-1] == "render"]
        assert not hasattr(router_module, "_usd") and not hasattr(router_module, "render")

    def test_amounts_from_a_dollar_up_have_two_decimals_below_it_the_usual_rule(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        line = format_advice_line(only(advise_sessions([on(stored, "big", Usage(output=20_000), session_id="mid")], cfg, table)))  # $1.00 -> $0.20
        assert line == "session mid: you spent $1.00 on big → small would likely have handled this for ~$0.20 (5.0x cheaper)"
        line = format_advice_line(only(advise_sessions([on(stored, "big", Usage(output=25_337), session_id="odd")], cfg, table)))  # $1.26685 -> $0.25337
        assert line == "session odd: you spent $1.27 on big → small would likely have handled this for ~$0.2534 (5.0x cheaper)"


# ---- weekly savings -----------------------------------------------------------------------
class TestWeeklySavings:
    def rows(self, cfg: Config, table: PriceTable, stored: Stored, *specs: tuple[str, str]) -> list[WeekSavings]:
        sessions = [on(stored, model, session_id=f"s{i}", started_at=started) for i, (started, model) in enumerate(specs)]
        return weekly_savings(advise_sessions(sessions, cfg, table), cfg)

    def test_one_row_per_iso_week_oldest_first(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        rows = self.rows(cfg, table, stored, ("2026-10-05T10:00:00Z", "big"), ("2026-09-28T10:00:00Z", "big"), ("2026-09-27T23:59:00Z", "tiny"))
        assert [r.week for r in rows] == ["2026-W39", "2026-W40", "2026-W41"]

    def test_totals_add_up(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        [row] = self.rows(cfg, table, stored, ("2026-09-28T10:00:00Z", "big"), ("2026-09-29T10:00:00Z", "tiny"), ("2026-09-30T10:00:00Z", "big"))
        assert (row.sessions, row.switched, row.skipped) == (3, 2, 0)
        assert row.actual_ucents == usd("105")
        assert row.recommended_ucents == usd("25")  # 10 + 5 (the session that was not switched counts as it was) + 10
        assert row.saved_ucents == usd("80")

    def test_sessions_with_unverified_prices_are_skipped_not_summed(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        sessions = [on(stored, "big", session_id="a"), stored(usage={"big": MILLION_OUT, "unver": MILLION_OUT}, session_id="b"), on(stored, "unver", session_id="c")]
        [row] = weekly_savings(advise_sessions(sessions, cfg, table), cfg)
        assert (row.sessions, row.switched, row.skipped) == (1, 1, 2)
        assert (row.actual_ucents, row.recommended_ucents) == (usd("50"), usd("10"))

    def test_a_week_with_only_skipped_sessions_still_shows_them(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        [row] = weekly_savings(advise_sessions([on(stored, "unver")], cfg, table), cfg)
        assert (row.sessions, row.skipped, row.actual_ucents, row.saved_ucents) == (0, 1, 0, 0)

    def test_weeks_follow_the_configured_timezone(self, make_config: MakeConfig, table: PriceTable, stored: Stored) -> None:
        try:
            zoneinfo.ZoneInfo("Asia/Tokyo")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database")
        session = on(stored, "big", started_at="2026-09-27T23:30:00Z")  # Sunday in UTC, Monday morning in Tokyo
        for zone, week in (("UTC", "2026-W39"), ("Asia/Tokyo", "2026-W40")):
            cfg = make_config(timezone=zone, router=ROUTER)
            assert [w.week for w in weekly_savings(advise_sessions([session], cfg, table), cfg)] == [week]

    def test_no_sessions_no_rows(self, cfg: Config) -> None:
        assert weekly_savings([], cfg) == []

    def test_a_session_that_spans_two_weeks_adds_to_both_what_it_spent_in_each(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = spread(stored, {(SUNDAY, "big"): MILLION_OUT, (MONDAY, "big"): Usage(output=500_000)}, started_at=SUNDAY)
        first, second = weekly_savings(advise_sessions([s], cfg, table), cfg)
        assert (first.week, first.sessions, first.switched, first.actual_ucents, first.recommended_ucents) == ("2026-W39", 1, 1, usd("50"), usd("10"))
        assert (second.week, second.sessions, second.switched, second.actual_ucents, second.recommended_ucents) == ("2026-W40", 1, 1, usd("25"), usd("5"))

    def test_the_total_counts_a_session_that_spans_weeks_once_and_adds_the_money_of_both(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        span = spread(stored, {(SUNDAY, "big"): MILLION_OUT, (MONDAY, "big"): MILLION_OUT}, session_id="span")
        other = spread(stored, {(MONDAY, "tiny"): MILLION_OUT}, session_id="other")
        weeks = weekly_savings(advise_sessions([span, other], cfg, table), cfg)
        assert [(w.week, w.sessions, w.switched) for w in weeks] == [("2026-W39", 1, 1), ("2026-W40", 2, 1)]
        total = total_savings(weeks)
        assert (total.sessions, total.switched, total.skipped) == (2, 1, 0)
        assert (total.actual_ucents, total.recommended_ucents, total.saved_ucents) == (usd("105"), usd("25"), usd("80"))

    def test_a_skipped_session_is_skipped_in_each_week_it_has_usage_in_and_once_in_the_total(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        s = spread(stored, {(SUNDAY, "unver"): MILLION_OUT, (MONDAY, "big"): MILLION_OUT})
        weeks = weekly_savings(advise_sessions([s], cfg, table), cfg)
        assert [(w.week, w.sessions, w.skipped, w.actual_ucents) for w in weeks] == [("2026-W39", 0, 1, 0), ("2026-W40", 0, 1, 0)]
        assert (total_savings(weeks).sessions, total_savings(weeks).skipped) == (0, 1)

    def test_the_week_of_a_slot_follows_the_configured_timezone(self, make_config: MakeConfig, table: PriceTable, stored: Stored) -> None:
        try:
            zoneinfo.ZoneInfo("Asia/Tokyo")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database")
        s = spread(stored, {("2026-09-27T14:45:00Z", "big"): MILLION_OUT, ("2026-09-27T15:00:00Z", "big"): MILLION_OUT})  # 23:45 Sunday and 00:00 Monday in Tokyo
        for zone, weeks in (("UTC", ["2026-W39"]), ("Asia/Tokyo", ["2026-W39", "2026-W40"])):
            cfg = make_config(timezone=zone, router=ROUTER)
            assert [w.week for w in weekly_savings(advise_sessions([s], cfg, table), cfg)] == weeks

    def test_the_weeks_add_up_to_the_sum_of_the_advice_to_the_micro_cent(self, make_config: MakeConfig, make_table: Callable[..., PriceTable], stored: Stored) -> None:
        table = make_table({"a": {"input": "1.005", "output": 1}, "b": {"input": "0.1005", "output": 1}})
        cfg = make_config(timezone="UTC", router={**ROUTER, "recommend": {"light": "b", "medium": "b", "heavy": "b"}})
        sessions = [spread(stored, {(SUNDAY, "a"): Usage(input=n), (MONDAY, "a"): Usage(input=n + 1)}, session_id=f"s{n}") for n in (1, 7, 33, 500_001)]
        advice = advise_sessions(sessions, cfg, table)
        total = total_savings(weekly_savings(advice, cfg))
        assert total.actual_ucents == sum(a.actual_ucents for a in advice)
        assert total.recommended_ucents == sum(a.recommended_ucents for a in advice if a.switch)
        assert total.saved_ucents == sum(a.saved_ucents for a in advice)


    def test_the_grand_total_sums_the_weeks(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        total = total_savings(self.rows(cfg, table, stored, ("2026-09-21T10:00:00Z", "big"), ("2026-09-28T10:00:00Z", "big")))
        assert (total.week, total.sessions, total.switched, total.actual_ucents, total.recommended_ucents) == ("total", 2, 2, usd("100"), usd("20"))

    def test_an_empty_grand_total_is_zero(self) -> None:
        assert total_savings([]) == WeekSavings("total", 0, 0, 0, 0)


class TestSavingsSplit:
    def test_the_saving_is_split_by_whether_a_person_drove_the_session(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        sessions = [on(stored, "big", session_id="driven", prompts=2), on(stored, "big", session_id="thread", prompts=0), on(stored, "tiny", session_id="cheap", prompts=1)]
        advice = advise_sessions(sessions, cfg, table)
        assert savings_split(advice) == (usd("40"), usd("40"))  # the session that is already cheaper saves nothing

    def test_the_two_parts_add_up_to_the_saved_of_the_weekly_total(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        sessions = [
            spread(stored, {(SUNDAY, "big"): MILLION_OUT, (MONDAY, "big"): Usage(output=300_001)}, session_id="a", prompts=1),
            spread(stored, {(MONDAY, "big"): Usage(output=123_457)}, session_id="b", prompts=0),
            on(stored, "unver", session_id="c", prompts=0),
        ]
        advice = advise_sessions(sessions, cfg, table)
        assert sum(savings_split(advice)) == total_savings(weekly_savings(advice, cfg)).saved_ucents

    def test_nothing_saved_is_zero_and_zero(self, cfg: Config, table: PriceTable, stored: Stored) -> None:
        assert savings_split([]) == (0, 0)
        assert savings_split(advise_sessions([on(stored, "big", **HEAVY)], cfg, table)) == (0, 0)


# ---- shipped defaults ---------------------------------------------------------------------
def session_worth_defaults(stored: Stored, score: int) -> StoredSession:
    """A session that scores exactly ``score`` (0-10) with the shipped thresholds: 2 points per heavy signal, then 1 for a medium one."""
    heavy = [{"tool_calls": 180}, {"files_touched": 6}, {"error_retries": 8}, {"active_seconds": 4_800}, {"usage": {"a": Usage(input=25_000_000)}}]
    medium = [{"tool_calls": 25}, {"files_touched": 1}, {"error_retries": 2}, {"active_seconds": 600}, {"usage": {"a": Usage(input=2_000_000)}}]
    kw: dict[str, Any] = {}
    for i in range(len(heavy)):
        if i < score // 2:
            kw |= heavy[i]
        elif i == score // 2 and score % 2:
            kw |= medium[i]
    return stored(**kw)



class TestShippedDefaults:
    def test_every_model_the_router_names_has_a_verified_price(self) -> None:
        table = load_pricing()
        router = DEFAULT_CONFIG["router"]
        names = {m for m in router["recommend"].values() if m} | set(router["run"]["model_aliases"].values())
        for tiers in router["run"]["models"].values():
            names |= {m for m in tiers.values() if m}
        assert names
        assert [m for m in sorted(names) if table.priced(m) is None] == []

    @pytest.mark.parametrize(
        ("signal", "medium", "heavy"),
        [("tool_calls", 25, 180), ("files_touched", 1, 6), ("error_retries", 2, 8)],
    )
    def test_the_shipped_count_thresholds_are_the_documented_ones(self, make_config: MakeConfig, stored: Stored, signal: str, medium: int, heavy: int) -> None:
        """docs/ROUTING.md section 1 lists these numbers: change them together."""
        cfg = make_config(timezone="UTC")
        points = [score_session(stored(**{signal: n}), cfg).points[signal] for n in (medium - 1, medium, heavy - 1, heavy)]
        assert points == [0, 1, 1, 2]

    def test_the_shipped_input_and_time_thresholds_are_the_documented_ones(self, make_config: MakeConfig, stored: Stored) -> None:
        cfg = make_config(timezone="UTC")
        tokens = [score_session(stored(usage={"a": Usage(input=n)}), cfg).points["input_tokens"] for n in (1_999_999, 2_000_000, 24_999_999, 25_000_000)]
        minutes = [score_session(stored(active_seconds=s), cfg).points["active_minutes"] for s in (599, 600, 4_799, 4_800)]
        assert (tokens, minutes) == ([0, 1, 1, 2], [0, 1, 1, 2])

    def test_the_shipped_tiers_and_recommendations_are_the_documented_ones(self, make_config: MakeConfig, stored: Stored) -> None:
        cfg = make_config(timezone="UTC")
        assert [score_session(session_worth_defaults(stored, n), cfg).tier for n in (2, 3, 6, 7)] == ["light", "medium", "medium", "heavy"]
        assert cfg.get("router.recommend") == {"light": "gpt-6.1-sol", "medium": "gpt-6.1-sol", "heavy": None}

    def test_the_shipped_run_settings_are_the_documented_ones(self) -> None:
        """docs/ROUTING.md sections 4 and 5 list these: change them together."""
        run = DEFAULT_CONFIG["router"]["run"]
        assert (run["tiers"], run["prompt_chars"], run["repo_files"], run["keyword_points"]) == (
            {"light_max": 0, "medium_max": 2}, {"medium": 400, "heavy": 1500}, {"medium": 200, "heavy": 1200}, 2,
        )
        assert run["heavy_keywords"] == [
            "refactor", "architect", "migrat", "redesign", "rewrit", "overhaul", "security", "concurrency",
            "refator", "arquitet", "migr", "reescrev", "reescrit", "seguranc", "concorrenc",
        ]
        assert run["light_keywords"] == [
            "typo", "renam", "docstring", "comment", "format", "lint", "bump", "readme", "changelog",
            "renomear", "renome", "comentar", "comentario", "formatar", "erro de digitacao",
        ]
        assert run["medium_keywords"] == [
            "implement", "feature", "debug", "bug", "fix", "test", "optimi", "perform", "investigat", "design", "integrat", "endpoint", "api",
            "schema", "upgrade", "funcionalidade", "depur", "teste", "otimiz", "desempenho", "investig", "projeto", "integr", "corrig",
        ]
        assert run["model_aliases"] == {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-4-5", "fable": "claude-fable-5-1"}
        assert run["models"] == {
            "claude": {"light": "claude-sonnet-5-5", "medium": "claude-sonnet-5-5", "heavy": "claude-opus-5-5"},
            "codex": {"light": "gpt-6.1-sol", "medium": "gpt-6.1-sol", "heavy": "gpt-6-astra"},
            "gemini": {"light": "gemini-3.1-flash-lite", "medium": "gemini-3-flash-preview", "heavy": "gemini-3.1-pro-preview"},
        }
        assert run["default_tokens"] == {
            "light": {"input": 30_000, "cache_read": 600_000, "cache_write": 25_000, "output": 7_000},
            "medium": {"input": 135_000, "cache_read": 8_800_000, "cache_write": 130_000, "output": 65_000},
            "heavy": {"input": 770_000, "cache_read": 118_000_000, "cache_write": 1_500_000, "output": 480_000},
        }

    def test_defaults_are_valid_and_the_advise_knob_exists(self, make_config: MakeConfig, stored: Stored) -> None:
        cfg = make_config(timezone="UTC")
        score_session(stored(), cfg)
        assert min_cost_ucents(cfg) == cents_to_ucents(10)
        assert saving_ratio(cfg) == (6, 5)


# ---- pick_for_run -------------------------------------------------------------------------
RUN = {
    "tiers": {"light_max": 1, "medium_max": 3},
    "prompt_chars": {"medium": 100, "heavy": 200},
    "repo_files": {"medium": 3, "heavy": 6},
    "heavy_keywords": ["refactor", "c++"],
    "light_keywords": ["typo", "rename"],
    "medium_keywords": ["implement", "bug"],
    "keyword_points": 2,
    "model_aliases": {"opus": "big", "sonnet": "small"},
    "models": {
        "claude": {"light": "tiny", "medium": "small", "heavy": "big"},
        "nomodel": {"light": None, "medium": None, "heavy": None},
        "shaky": {"light": "unver", "medium": "unver", "heavy": "big"},
    },
    "default_tokens": {  # every bucket spelled out: a partial entry would be merged with the shipped defaults
        "light": {"input": 1_000_000, "cache_read": 0, "cache_write": 0, "output": 0},
        "medium": {"input": 1_000_000, "cache_read": 0, "cache_write": 0, "output": 1_000_000},
        "heavy": {"input": 1_000_000, "cache_read": 1_000_000, "cache_write": 1_000_000, "output": 1_000_000},
    },
}
# Past sessions are sized with millions of tokens: keep input from scoring so their tier is decided by the other signals.
QUIET = {**SIGNALS, "input_tokens": {"medium": 10**12, "heavy": 10**13}}


@pytest.fixture
def run_cfg(make_config: MakeConfig) -> Config:
    return make_config(timezone="UTC", router={**ROUTER, "signals": QUIET, "run": RUN})


def pick(cfg: Config, table: PriceTable, prompt: str = "", agent: str = "claude", store: Store | None = None, **kw: Any) -> Any:
    return pick_for_run(cfg, table, store, agent=agent, prompt=prompt, **kw)


def repo(tmp_path: Path, files: int, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    for i in range(files):
        (root / f"f{i}.txt").write_text("x")
    return root


NOTHING = ["no rule fired: score 0"]


class TestRunPromptRule:
    @pytest.mark.parametrize(
        ("chars", "score", "reasons"),
        [
            (99, 0, NOTHING),
            (100, 1, ["prompt is 100 chars (>= 100): +1"]),
            (199, 1, ["prompt is 199 chars (>= 100): +1"]),
            (200, 2, ["prompt is 200 chars (>= 200): +2"]),
            (5_000, 2, ["prompt is 5000 chars (>= 200): +2"]),
        ],
    )
    def test_boundaries(self, run_cfg: Config, table: PriceTable, chars: int, score: int, reasons: list[str]) -> None:
        got = pick(run_cfg, table, "x" * chars)
        assert (got.score, got.reasons) == (score, reasons)

    def test_only_the_length_matters_never_the_content(self, run_cfg: Config, table: PriceTable) -> None:
        secret = "PRIVACY-CANARY-run-prompt"
        got = pick(run_cfg, table, secret + " " * 100)
        assert got.reasons == ["prompt is 125 chars (>= 100): +1"] and secret not in repr(got)


class TestRunRepoRule:
    @pytest.mark.parametrize(
        ("files", "score", "reasons"),
        [
            (2, 0, NOTHING),
            (3, 1, ["repo has 3 files (>= 3): +1"]),
            (5, 1, ["repo has 5 files (>= 3): +1"]),
            (6, 2, ["repo has 6+ files (>= 6): +2"]),
            (40, 2, ["repo has 6+ files (>= 6): +2"]),
        ],
    )
    def test_boundaries(self, run_cfg: Config, table: PriceTable, tmp_path: Path, files: int, score: int, reasons: list[str]) -> None:
        got = pick(run_cfg, table, repo_path=repo(tmp_path, files))
        assert (got.score, got.reasons) == (score, reasons)

    def test_skips_dependency_build_and_vcs_directories_at_any_depth(self, tmp_path: Path) -> None:
        root = repo(tmp_path, 2)
        for skipped in (
            ".git", "node_modules", ".venv", "venv", "build", "dist", "target", "__pycache__", ".hg", ".svn",
            ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".nuxt", ".svelte-kit", ".turbo", ".gradle", ".terraform", ".pnpm-store",
        ):
            (root / skipped).mkdir()
            (root / skipped / "a.txt").write_text("x")
        (root / "src" / "deep").mkdir(parents=True)
        (root / "src" / "deep" / "b.txt").write_text("x")
        (root / "src" / "node_modules").mkdir()
        (root / "src" / "node_modules" / "c.txt").write_text("x")
        assert count_files(root, 100) == 3

    def test_stops_at_the_cap(self, tmp_path: Path) -> None:
        assert count_files(repo(tmp_path, 50), 7) == 7

    def test_does_not_follow_symlinks_or_count_them(self, tmp_path: Path) -> None:
        root = repo(tmp_path, 1)
        outside = repo(tmp_path, 5, "outside")
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (root / "link.txt").symlink_to(outside / "f0.txt")
        assert count_files(root, 100) == 1

    def test_never_opens_a_file(self, tmp_path: Path) -> None:
        root = repo(tmp_path, 3)
        for f in root.iterdir():
            f.chmod(0)
        try:
            assert count_files(root, 100) == 3
        finally:
            for f in root.iterdir():
                f.chmod(0o644)

    def test_nothing_is_opened_at_all_not_even_by_root(self, run_cfg: Config, table: PriceTable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A chmod-0 file only proves that for an ordinary user: this fails whoever runs it."""
        root = repo(tmp_path, 3)
        (root / "sub").mkdir()
        (root / "sub" / "deep.txt").write_text("x")

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("the repo walk opened a file")

        with monkeypatch.context() as patched:
            for module in (builtins, io, os):
                patched.setattr(module, "open", refuse, raising=False)
            assert count_files(root, 100) == 4
            assert pick(run_cfg, table, "refactor", repo_path=root).reasons == ["repo has 4 files (>= 3): +1", "heavy keyword refactor: +2"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can list any directory")
    def test_an_unreadable_directory_counts_as_empty(self, tmp_path: Path) -> None:
        root = repo(tmp_path, 2)
        locked = root / "locked"
        locked.mkdir()
        (locked / "a.txt").write_text("x")
        locked.chmod(0)
        try:
            assert count_files(root, 100) == 2
        finally:
            locked.chmod(0o755)

    def test_a_missing_directory_counts_zero(self, tmp_path: Path) -> None:
        assert count_files(tmp_path / "nope", 10) == 0

    def test_no_repo_path_means_no_rule(self, run_cfg: Config, table: PriceTable) -> None:
        assert pick(run_cfg, table).reasons == NOTHING

    def test_a_path_that_is_not_a_directory_means_no_rule(self, run_cfg: Config, table: PriceTable, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        assert pick(run_cfg, table, repo_path=tmp_path / "a.txt").reasons == NOTHING
        assert pick(run_cfg, table, repo_path=tmp_path / "missing").reasons == NOTHING


class TestRunKeywords:
    def test_heavy_keyword_adds_the_configured_points(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, "refactor it")
        assert (got.score, got.reasons) == (2, ["heavy keyword refactor: +2"])

    def test_light_keyword_subtracts(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, "x" * 200 + " typo")
        assert (got.score, got.reasons) == (0, ["prompt is 205 chars (>= 200): +2", "light keyword typo: -2"])

    def test_both_families_cancel_out(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, "typo refactor")
        assert (got.score, got.reasons) == (0, ["heavy keyword refactor: +2", "light keyword typo: -2"])

    @pytest.mark.parametrize("prompt", ["REFACTOR", "Refactor", "please ReFaCtOr this", "(refactor)", "refactor.", "pre-refactor", "a\nrefactor\tb"])
    def test_case_insensitive_and_delimited_by_anything_that_is_not_a_word_character(self, run_cfg: Config, table: PriceTable, prompt: str) -> None:
        assert pick(run_cfg, table, prompt).reasons == ["heavy keyword refactor: +2"]

    @pytest.mark.parametrize("prompt", ["refactoring", "Refactored", "refactors", "refactor_it", "REFACTORING the module", "(refactoring)"])
    def test_a_keyword_matches_the_words_that_start_with_it(self, run_cfg: Config, table: PriceTable, prompt: str) -> None:
        assert pick(run_cfg, table, prompt).reasons == ["heavy keyword refactor: +2"]

    @pytest.mark.parametrize("prompt", ["prerefactor", "xrefactor", "unrefactored", "a_refactor", "9refactor", "efactor"])
    def test_a_word_that_only_contains_the_keyword_does_not_match(self, run_cfg: Config, table: PriceTable, prompt: str) -> None:
        assert pick(run_cfg, table, prompt).reasons == NOTHING

    @pytest.mark.parametrize(
        ("prompt", "reasons"),
        [
            ("refactoring the billing module", ["heavy keyword refactor: +2"]),
            ("we refactored it", ["heavy keyword refactor: +2"]),
            ("review the architecture", ["heavy keyword architect: +2"]),
            ("an architectural decision", ["heavy keyword architect: +2"]),
            ("the tables were migrated", ["heavy keyword migrat: +2"]),
            ("she migrates data", ["heavy keyword migrat: +2"]),
            ("we are migrating the tables", ["heavy keyword migrat: +2"]),  # a stem: "migrate" is not a prefix of "migrating"
            ("a migration of the tables", ["heavy keyword migrat: +2"]),
            ("I am rewriting the parser", ["heavy keyword rewrit: +2"]),
            ("it has been rewritten", ["heavy keyword rewrit: +2"]),
            ("rewrite the parser", ["heavy keyword rewrit: +2"]),
            ("renaming the variable", ["light keyword renam: -2"]),
            ("rename the variable", ["light keyword renam: -2"]),
            ("formatting and linting", ["light keywords format, lint: -3"]),
            ("more information about it", NOTHING),  # "format" is not a prefix of "information"
            ("the commentary", ["light keyword comment: -2"]),  # the known cost of a prefix: docs/ROUTING.md lists these three
            ("its typography", ["light keyword typo: -2"]),
            ("the formation", ["light keyword format: -2"]),
            ("lint-staged, do_refactor", ["light keyword lint: -2"]),  # a hyphen ends a word, an underscore does not
            ("performance", ["medium keyword perform: +1"]),
            ("we use a schema", ["medium keyword schema: +1"]),
            ("a fixture", ["medium keyword fix: +1"]),  # a prefix is coarse: the docs list this one too
        ],
    )
    def test_the_shipped_keywords_read_as_word_prefixes(self, make_config: MakeConfig, table: PriceTable, prompt: str, reasons: list[str]) -> None:
        got = pick(make_config(timezone="UTC"), table, prompt)
        assert got.reasons == reasons

    def test_a_verb_and_its_noun_are_one_keyword_not_two(self, make_config: MakeConfig, table: PriceTable) -> None:
        """One idea in a prompt must not add up to a second reason: heavy takes two."""
        cfg = make_config(timezone="UTC")
        assert pick(cfg, table, "migrate the tables, the migration is risky").reasons == ["heavy keyword migrat: +2"]
        assert pick(cfg, table, "rewrite it: the rewriting is big").reasons == ["heavy keyword rewrit: +2"]
        assert pick(cfg, table, "rename it, renaming is easy").reasons == ["light keyword renam: -2"]

    def test_two_keywords_that_start_the_same_word_are_one_keyword(self, make_config: MakeConfig, table: PriceTable) -> None:
        """The shipped lists hold ``migrat`` and ``migr`` (Portuguese): ``migrate`` must not be two reasons, and heavy takes two."""
        cfg = make_config(timezone="UTC")
        assert pick(cfg, table, "migrate the tables").reasons == ["heavy keyword migrat: +2"]
        assert pick(cfg, table, "renomear a variavel").reasons == ["light keyword renomear: -2"]  # not renomear and renome: -3
        assert pick(cfg, table, "renome a variavel").reasons == ["light keyword renome: -2"]
        assert pick(cfg, table, "formatar").reasons == ["light keyword format: -2"]
        assert pick(cfg, table, "migrate or migrar").reasons == ["heavy keywords migrat, migr: +3"]  # two different words, two keywords

    @pytest.mark.parametrize(
        ("prompt", "reasons"),
        [
            ("MIGRAÇÃO do banco", ["heavy keyword migr: +2"]),
            ("migracao do banco", ["heavy keyword migr: +2"]),  # no accent on the prompt side
            ("Segurança primeiro", ["heavy keyword seguranc: +2"]),
            ("um seguro de viagem", NOTHING),  # the stem is seguranc: "seguro" (insurance, safe) is another word
            ("reescrever tudo", ["heavy keyword reescrev: +2"]),
            ("foi reescrito", ["heavy keyword reescrit: +2"]),
            ("concorrência", ["heavy keyword concorrenc: +2"]),
            ("concorrências", ["heavy keyword concorrenc: +2"]),
            ("o concorrente", NOTHING),  # a competitor is not concurrency
            ("a concorrer ao prêmio", NOTHING),  # a verb of another meaning, which the old stem ``concorr`` matched
            ("arquitetura", ["heavy keyword arquitet: +2"]),
            ("refatoração", ["heavy keyword refator: +2"]),
            ("comentário inútil", ["light keyword comentar: -2"]),
            ("corrigir um erro de digitação", ["light keyword erro de digitacao: -2", "medium keyword corrig: +1"]),  # the verb to fix is ordinary work
            ("corrigir o bug", ["medium keywords bug, corrig: +1"]),
            ("foi corrigido", ["medium keyword corrig: +1"]),
            ("erro   de\tdigitacao", ["light keyword erro de digitacao: -2"]),  # the phrase reads across any white space
            ("um erro de lógica", NOTHING),  # only the whole phrase is a keyword
            ("implementar a funcionalidade", ["medium keywords implement, funcionalidade: +1"]),
            ("depurar o teste", ["medium keywords test, depur: +1"]),  # the English stem test comes first in the list, so teste adds nothing
            ("otimização de desempenho", ["medium keywords otimiz, desempenho: +1"]),
            ("projeto novo", ["medium keyword projeto: +1"]),
            ("os projetos", ["medium keyword projeto: +1"]),
            ("a projeção de custos", NOTHING),  # not the noun: the old stem ``projet`` was only three letters away from it
        ],
    )
    def test_portuguese_stems_match_with_or_without_accents(self, make_config: MakeConfig, table: PriceTable, prompt: str, reasons: list[str]) -> None:
        assert pick(make_config(timezone="UTC"), table, prompt).reasons == reasons

    def test_an_accent_on_either_side_makes_no_difference(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "heavy_keywords": ["migração", "Ünïcode"]}})
        assert pick(cfg, table, "migracao e UNICODE").reasons == ["heavy keywords migração, Ünïcode: +3"]
        assert pick(cfg, table, "MIGRAÇÃO e ünicode").reasons == ["heavy keywords migração, Ünïcode: +3"]
        assert pick(cfg, table, "a-migração").reasons == ["heavy keyword migração: +2"]

    def test_the_decomposed_form_of_an_accent_is_the_same_letter(self, make_config: MakeConfig, table: PriceTable) -> None:
        composed, decomposed = "caf\u00e9", "cafe\u0301"
        for configured, typed in ((composed, decomposed), (decomposed, composed), (composed, "cafe")):
            cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "heavy_keywords": [configured]}})
            assert pick(cfg, table, f"x {typed}").reasons == [f"heavy keyword {configured}: +2"]

    def test_regex_characters_in_a_keyword_are_literal(self, run_cfg: Config, table: PriceTable) -> None:
        assert pick(run_cfg, table, "port it to c++ today").reasons == ["heavy keyword c++: +2"]
        assert pick(run_cfg, table, "c and cc plus").reasons == NOTHING

    def test_a_keyword_that_ends_in_punctuation_still_matches_the_words_that_go_on(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "heavy_keywords": ["c++", "c#"]}})
        assert pick(cfg, table, "c++20 and c#").reasons == ["heavy keywords c++, c#: +3"]

    def test_only_the_configured_word_is_ever_echoed(self, run_cfg: Config, table: PriceTable) -> None:
        secret = "PRIVACY-CANARY-keyword"
        got = pick(run_cfg, table, f"{secret} refactoring")
        assert got.reasons == ["heavy keyword refactor: +2"] and secret not in repr(got)

    @pytest.mark.parametrize("bad", [None, "refactor", [1, 2], [""]])
    def test_bad_keyword_lists_are_rejected(self, make_config: MakeConfig, table: PriceTable, bad: Any) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "heavy_keywords": bad}})
        with pytest.raises(ConfigError, match="router.run.heavy_keywords must be a list of words"):
            pick(cfg, table, "x")


class TestRunKeywordPoints:
    """A family scores ``keyword_points`` for its first distinct keyword and 1 more for each further one, at most 2 more."""

    def cfg(self, make_config: MakeConfig, **run: Any) -> Config:
        heavy = ["refactor", "architect", "migrate", "redesign", "rewrite"]
        light = ["typo", "rename", "docstring", "comment", "format"]
        return make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "heavy_keywords": heavy, "light_keywords": light, **run}})

    @pytest.mark.parametrize(
        ("prompt", "score", "reason"),
        [
            ("refactor", 2, "heavy keyword refactor: +2"),
            ("refactor architect", 3, "heavy keywords refactor, architect: +3"),
            ("refactor architect migrate", 4, "heavy keywords refactor, architect, migrate: +4"),
            ("refactor architect migrate redesign", 4, "heavy keywords refactor, architect, migrate, redesign: +4"),  # the extras stop at 2
            ("refactor architect migrate redesign rewrite", 4, "heavy keywords refactor, architect, migrate, redesign, rewrite: +4"),
        ],
    )
    def test_heavy_keywords_add_up_to_two_extra_points(self, make_config: MakeConfig, table: PriceTable, prompt: str, score: int, reason: str) -> None:
        got = pick(self.cfg(make_config), table, prompt)
        assert (got.score, got.reasons) == (score, [reason])

    @pytest.mark.parametrize(
        ("prompt", "reason"),
        [
            ("typo", "light keyword typo: -2"),
            ("typo rename", "light keywords typo, rename: -3"),
            ("typo rename docstring", "light keywords typo, rename, docstring: -4"),
            ("typo rename docstring comment format", "light keywords typo, rename, docstring, comment, format: -4"),
        ],
    )
    def test_light_keywords_subtract_the_same_way(self, make_config: MakeConfig, table: PriceTable, prompt: str, reason: str) -> None:
        got = pick(self.cfg(make_config), table, "x" * 200 + " " + prompt)  # +2 from the prompt keeps the score above the floor
        assert got.reasons == ["prompt is %d chars (>= 200): +2" % (len(prompt) + 201), reason]
        assert got.score == max(0, 2 + int(reason.rsplit(" ", 1)[1]))

    def test_the_score_never_goes_below_zero(self, make_config: MakeConfig, table: PriceTable) -> None:
        got = pick(self.cfg(make_config), table, "typo rename docstring comment")
        assert (got.score, got.reasons) == (0, ["light keywords typo, rename, docstring, comment: -4"])

    def test_the_two_families_add_up_before_the_floor(self, make_config: MakeConfig, table: PriceTable) -> None:
        got = pick(self.cfg(make_config), table, "refactor architect typo")  # +3 - 2
        assert (got.score, got.reasons) == (1, ["heavy keywords refactor, architect: +3", "light keyword typo: -2"])

    def test_the_forms_of_one_keyword_are_one_keyword(self, make_config: MakeConfig, table: PriceTable) -> None:
        got = pick(self.cfg(make_config), table, "refactor refactoring Refactored REFACTORS")
        assert (got.score, got.reasons) == (2, ["heavy keyword refactor: +2"])

    def test_a_keyword_listed_twice_counts_once_and_keeps_its_first_spelling(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = self.cfg(make_config, heavy_keywords=["Refactor", "refactor", "REFACTOR", "architect"])
        assert pick(cfg, table, "refactor").reasons == ["heavy keyword Refactor: +2"]
        assert pick(cfg, table, "refactor architect").reasons == ["heavy keywords Refactor, architect: +3"]

    def test_the_first_keyword_is_worth_keyword_points_and_the_rest_one_each(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = self.cfg(make_config, keyword_points=3)
        assert [pick(cfg, table, p).score for p in ("refactor", "refactor architect", "refactor architect migrate", "refactor architect migrate redesign")] == [3, 4, 5, 5]
        cfg = self.cfg(make_config, keyword_points=1)
        assert [pick(cfg, table, p).score for p in ("refactor", "refactor architect", "refactor architect migrate")] == [1, 2, 3]

    def test_zero_keyword_points_turn_keywords_off(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = self.cfg(make_config, keyword_points=0)
        assert pick(cfg, table, "refactor architect migrate typo rename").reasons == NOTHING

    def test_a_negative_total_is_clamped_to_zero(self, make_config: MakeConfig, table: PriceTable) -> None:
        got = pick(self.cfg(make_config, keyword_points=5), table, "typo rename")
        assert (got.score, got.reasons) == (0, ["light keywords typo, rename: -6"])

    @pytest.mark.parametrize(
        ("prompt", "reason"),
        [
            ("implement", "medium keyword implement: +1"),
            ("implement a bug fix", "medium keywords implement, bug: +1"),  # one point for the family, however many match
            ("implementing bugs, bug, BUG", "medium keywords implement, bug: +1"),
        ],
    )
    def test_the_medium_family_adds_one_point_once(self, make_config: MakeConfig, table: PriceTable, prompt: str, reason: str) -> None:
        got = pick(self.cfg(make_config), table, prompt)
        assert (got.score, got.reasons) == (1, [reason])

    def test_the_medium_family_adds_to_the_heavy_one_and_is_cancelled_by_the_light_one(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = self.cfg(make_config)
        assert pick(cfg, table, "refactor and implement").reasons == ["heavy keyword refactor: +2", "medium keyword implement: +1"]
        assert pick(cfg, table, "refactor and implement").score == 3
        got = pick(cfg, table, "implement typo rename")
        assert (got.score, got.reasons) == (0, ["light keywords typo, rename: -3", "medium keyword implement: +1"])

    def test_keyword_points_zero_turns_the_medium_family_off_too(self, make_config: MakeConfig, table: PriceTable) -> None:
        assert pick(self.cfg(make_config, keyword_points=0), table, "implement bug").reasons == NOTHING

    def test_the_medium_points_do_not_depend_on_keyword_points(self, make_config: MakeConfig, table: PriceTable) -> None:
        assert pick(self.cfg(make_config, keyword_points=4), table, "implement bug").score == 1

    def test_a_bad_medium_list_is_rejected(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "medium_keywords": "implement"}})
        with pytest.raises(ConfigError, match="router.run.medium_keywords must be a list of words"):
            pick(cfg, table, "x")


class TestRunShippedTiers:
    """What the shipped defaults do (docs/ROUTING.md, section 4): heavy needs a second reason, not one keyword alone."""

    @pytest.fixture
    def defaults(self, make_config: MakeConfig) -> Config:
        return make_config(timezone="UTC")

    def outcome(self, cfg: Config, table: PriceTable, prompt: str, repo_path: Path | None = None) -> tuple[str, int]:
        got = pick(cfg, table, prompt, repo_path=repo_path)
        return got.tier, got.score

    def test_the_cut_points_are_the_documented_ones(self, defaults: Config) -> None:
        assert defaults.get("router.run.tiers") == {"light_max": 0, "medium_max": 2}

    def test_one_heavy_keyword_alone_stays_medium(self, defaults: Config, table: PriceTable) -> None:
        assert self.outcome(defaults, table, "refactor the billing module") == ("medium", 2)

    def test_two_heavy_keywords_reach_heavy(self, defaults: Config, table: PriceTable) -> None:
        assert self.outcome(defaults, table, "refactor the billing module and migrate it") == ("heavy", 3)

    def test_one_heavy_keyword_and_a_prompt_of_400_chars_reach_heavy(self, defaults: Config, table: PriceTable) -> None:
        assert self.outcome(defaults, table, "refactor " + "x" * 391) == ("heavy", 3)  # 400 characters
        assert self.outcome(defaults, table, "refactor " + "x" * 390) == ("medium", 2)  # 399: one short

    def test_one_heavy_keyword_and_a_repo_of_200_files_reach_heavy(self, defaults: Config, table: PriceTable, tmp_path: Path) -> None:
        assert self.outcome(defaults, table, "refactor it", repo(tmp_path, 200)) == ("heavy", 3)
        assert self.outcome(defaults, table, "refactor it", repo(tmp_path, 199, "small")) == ("medium", 2)

    def test_a_big_prompt_or_a_big_repo_alone_stays_medium_and_both_together_are_heavy(self, defaults: Config, table: PriceTable, tmp_path: Path) -> None:
        big = repo(tmp_path, 1200)
        assert self.outcome(defaults, table, "x" * 1500) == ("medium", 2)
        assert self.outcome(defaults, table, "", big) == ("medium", 2)
        assert self.outcome(defaults, table, "x" * 1500, big) == ("heavy", 4)

    def test_a_big_size_on_top_of_a_medium_one_is_heavy(self, defaults: Config, table: PriceTable, tmp_path: Path) -> None:
        assert self.outcome(defaults, table, "x" * 1500, repo(tmp_path, 200)) == ("heavy", 3)
        assert self.outcome(defaults, table, "x" * 400, repo(tmp_path, 1200, "big")) == ("heavy", 3)
        assert self.outcome(defaults, table, "x" * 400, repo(tmp_path, 200, "medium")) == ("medium", 2)

    def test_no_keyword_and_no_size_is_light(self, defaults: Config, table: PriceTable) -> None:
        assert self.outcome(defaults, table, "fix the typo in the README") == ("light", 0)
        assert self.outcome(defaults, table, "explain this function") == ("light", 0)

    def test_a_reason_worth_one_point_is_medium(self, defaults: Config, table: PriceTable, tmp_path: Path) -> None:
        assert self.outcome(defaults, table, "x" * 400) == ("medium", 1)  # a prompt of 400+ characters
        assert self.outcome(defaults, table, "", repo(tmp_path, 200)) == ("medium", 1)  # a repo of 200+ files
        assert self.outcome(defaults, table, "implement it") == ("medium", 1)  # a medium keyword

    def test_a_heavy_keyword_with_a_medium_keyword_is_heavy(self, defaults: Config, table: PriceTable) -> None:
        assert self.outcome(defaults, table, "refactor the api") == ("heavy", 3)

    def test_the_medium_words_alone_never_reach_heavy(self, defaults: Config, table: PriceTable) -> None:
        assert self.outcome(defaults, table, "implement a feature to debug the api, test the schema and upgrade it") == ("medium", 1)

    @pytest.mark.parametrize(
        ("prompt", "tier", "score", "reasons"),
        [
            ("fix a typo in the README", "light", 0, ["light keywords typo, readme: -3", "medium keyword fix: +1"]),
            ("add a unit test for parse()", "medium", 1, ["medium keyword test: +1"]),
            ("implement a feature to export CSV", "medium", 1, ["medium keywords implement, feature: +1"]),
            ("rewrite the auth layer", "medium", 2, ["heavy keyword rewrit: +2"]),
            ("refactor the architecture and migrate the database", "heavy", 4, ["heavy keywords refactor, architect, migrat: +4"]),
            ("refatorar a arquitetura e migrar o banco de dados", "heavy", 4, ["heavy keywords refator, arquitet, migr: +4"]),
            ("explain what this function does", "light", 0, ["no rule fired: score 0"]),
        ],
    )
    def test_the_documented_prompts_get_the_documented_tier(self, defaults: Config, table: PriceTable, prompt: str, tier: str, score: int, reasons: list[str]) -> None:
        """The table in docs/ROUTING.md section 4: the default config on prompts of a few words, in a repo of a few files."""
        got = pick(defaults, table, prompt)
        assert (got.tier, got.score, got.reasons) == (tier, score, reasons)
        assert prompt not in repr(got)  # only counts and the configured words

    def test_the_highest_possible_score_is_9(self, defaults: Config, table: PriceTable, tmp_path: Path) -> None:
        """Prompt +2, repo +2, a heavy family of three or more keywords (2 for the first, 1 for each of the next two) and the medium family +1."""
        prompt = "refactor architect migrate redesign implement test " + "x" * 1500
        got = pick(defaults, table, prompt, repo_path=repo(tmp_path, 1200))
        assert (got.tier, got.score) == ("heavy", 9)
        assert [r.rsplit(" ", 1)[1] for r in got.reasons] == ["+2", "+2", "+4", "+1"]
        keyword_points = DEFAULT_CONFIG["router"]["run"]["keyword_points"]
        assert got.score == 7 + keyword_points  # the general formula of docs/ROUTING.md

    def test_the_documents_say_the_same(self) -> None:
        """The comment of the shipped config and docs/ROUTING.md quote these numbers: change them together."""
        routing = (Path(__file__).parents[1] / "docs" / "ROUTING.md").read_text()
        assert "0-9" in DEFAULT_CONFIG_YAML and "0-8" not in DEFAULT_CONFIG_YAML + routing and "0-6" not in DEFAULT_CONFIG_YAML + routing
        assert "the maximum is 9" in routing and "`7 + keyword_points`" in routing and "{light_max: 0, medium_max: 2}" in routing
        for family in ("heavy", "light", "medium"):
            assert f"`{', '.join(DEFAULT_CONFIG['router']['run'][f'{family}_keywords'])}`" in routing
        assert "router.run.model_aliases" in routing and "savings_split" in routing


class TestRunTier:
    @pytest.mark.parametrize(
        ("prompt", "files", "tier", "score"),
        [
            ("", 0, "light", 0),
            ("x" * 100, 0, "light", 1),  # light_max is 1
            ("x" * 200, 0, "medium", 2),
            ("x" * 200, 3, "medium", 3),  # medium_max is 3
            ("x" * 200, 6, "heavy", 4),
            ("x" * 200 + " refactor", 6, "heavy", 6),
        ],
    )
    def test_score_to_tier(self, run_cfg: Config, table: PriceTable, tmp_path: Path, prompt: str, files: int, tier: str, score: int) -> None:
        got = pick(run_cfg, table, prompt, repo_path=repo(tmp_path, files) if files else None)
        assert (got.tier, got.score) == (tier, score)

    def test_the_cut_points_are_configurable(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "tiers": {"light_max": 0, "medium_max": 1}}})
        assert [pick(cfg, table, "x" * n).tier for n in (0, 100, 200)] == ["light", "medium", "heavy"]

    def test_every_rule_that_fired_has_its_own_line_in_a_fixed_order(self, run_cfg: Config, table: PriceTable, tmp_path: Path) -> None:
        got = pick(run_cfg, table, "x" * 186 + " refactor typo", repo_path=repo(tmp_path, 4))
        assert got.reasons == ["prompt is 200 chars (>= 200): +2", "repo has 4 files (>= 3): +1", "heavy keyword refactor: +2", "light keyword typo: -2"]
        assert (got.score, got.tier) == (3, "medium")


class TestRunModelAndCost:
    def test_model_comes_from_the_agent_and_tier(self, run_cfg: Config, table: PriceTable) -> None:
        assert [pick(run_cfg, table, "x" * n).model for n in (0, 200, 1_000)] == ["tiny", "small", "small"]
        assert pick(run_cfg, table, "x" * 200 + " refactor").model == "big"

    def test_the_estimate_prices_default_tokens_on_the_picked_model(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table)  # light: 1M input tokens on tiny ($1/M); baseline: the same tokens on big ($10/M)
        assert (got.model, got.est_ucents, got.baseline_model, got.baseline_ucents) == ("tiny", usd("1"), "big", usd("10"))
        assert got.warnings == [] and not got.forced

    def test_heavy_tier_is_its_own_baseline(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, "x" * 200 + " refactor")  # 1M of each bucket on big: 10 + 1 + 12.5 + 50
        assert got.est_ucents == got.baseline_ucents == usd("73.5")

    def test_an_agent_without_an_entry_gets_no_model_and_a_warning(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, "x" * 200, agent="my-agent")
        assert (got.model, got.est_ucents, got.baseline_model, got.baseline_ucents, got.tier) == (None, None, None, None, "medium")
        assert got.warnings == ["no model configured for agent 'my-agent' (router.run.models): run it with its own default"]

    def test_a_null_tier_entry_gets_no_model_and_a_warning(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, agent="nomodel")
        assert (got.model, got.est_ucents, got.baseline_model) == (None, None, None)
        assert got.warnings == ["no light-tier model configured for nomodel (router.run.models): run it with its own default"]

    def test_an_unverified_model_has_no_estimate(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, agent="shaky")
        assert (got.model, got.est_ucents, got.baseline_model, got.baseline_ucents) == ("unver", None, "big", usd("10"))
        assert got.warnings == ["no verified price for unver: cost estimate unavailable"]

    def test_an_unpriced_baseline_is_reported_too(self, make_config: MakeConfig, table: PriceTable) -> None:
        models = {"claude": {"light": "tiny", "medium": "tiny", "heavy": "unver"}}
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "models": models}})
        got = pick(cfg, table)
        assert (got.est_ucents, got.baseline_ucents) == (usd("1"), None)
        assert got.warnings == ["no verified price for unver: baseline cost unavailable"]

    def test_every_warning_is_its_own_entry_and_text_is_never_split(self, make_config: MakeConfig, table: PriceTable) -> None:
        """Two problems at once give two entries; a model id that happens to hold '; ' stays in one piece."""
        models = {"claude": {"light": "unver", "medium": "unver", "heavy": "unver"}}
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "models": models}})
        got = pick(cfg, table)
        assert got.warnings == ["no verified price for unver: cost estimate unavailable", "no verified price for unver: baseline cost unavailable"]
        odd = pick(cfg, table, force_model="a; b")
        assert odd.warnings == ["no verified price for a; b: cost estimate unavailable", "no verified price for unver: baseline cost unavailable"]

    def test_force_model_overrides_the_router(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, "x" * 200 + " refactor", force_model="tiny")
        assert (got.forced, got.model, got.tier, got.baseline_model) == (True, "tiny", "heavy", "big")  # the score is still computed and explained
        assert got.reasons == ["prompt is 209 chars (>= 200): +2", "heavy keyword refactor: +2"]
        assert got.est_ucents == usd("8")  # heavy default tokens on tiny: 1 + 1 + 1 (no cache prices: input rate) + 5

    def test_the_forced_estimate_uses_the_forced_models_price(self, run_cfg: Config, table: PriceTable) -> None:
        assert pick(run_cfg, table, force_model="small").est_ucents == usd("2")  # light: 1M input tokens at $2/M

    def test_forcing_an_unknown_model_warns_and_has_no_estimate(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, force_model="mystery")
        assert (got.model, got.est_ucents, got.forced) == ("mystery", None, True)
        assert got.warnings == ["no verified price for mystery: cost estimate unavailable"]

    def test_forcing_a_model_on_a_custom_agent_is_fine(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, agent="my-agent", force_model="small")
        assert (got.model, got.baseline_model, got.baseline_ucents, got.warnings) == ("small", None, None, [])
        assert got.est_ucents == usd("2")

    def test_an_alias_is_priced_as_the_model_it_stands_for(self, run_cfg: Config, table: PriceTable) -> None:
        got = pick(run_cfg, table, force_model="sonnet")  # RUN maps sonnet to small: light task, 1M input tokens at $2/M
        assert (got.model, got.alias_for, got.priced_model, got.est_ucents, got.forced, got.warnings) == ("sonnet", "small", "small", usd("2"), True, [])

    def test_an_alias_is_found_in_any_case_and_a_real_model_is_not_one(self, run_cfg: Config, table: PriceTable) -> None:
        assert [pick(run_cfg, table, force_model=m).alias_for for m in ("Opus", " OPUS ", "big", "opus-2")] == ["big", "big", None, None]
        assert pick(run_cfg, table).alias_for is None and pick(run_cfg, table).priced_model == "tiny"

    def test_an_alias_of_a_model_without_a_price_is_named_by_what_it_stands_for(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "model_aliases": {"opus": "unver"}}})
        got = pick(cfg, table, force_model="opus")
        assert (got.est_ucents, got.warnings) == (None, ["no verified price for unver: cost estimate unavailable"])

    def test_the_aliases_are_configurable_and_checked(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "model_aliases": {"fast": "tiny"}}})
        assert pick(cfg, table, force_model="fast").est_ucents == usd("1")
        assert pick(cfg, table, force_model="opus").alias_for == "claude-opus-5-5"  # a mapping is merged over the shipped one: the rest stay
        for bad in ({"opus": ""}, {"opus": 5}, {"opus": None}):
            cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "model_aliases": bad}})
            with pytest.raises(ConfigError, match="router.run.model_aliases.opus must be a model id"):
                pick(cfg, table, force_model="opus")

    def test_the_shipped_aliases_are_priced_by_the_builtin_table(self, make_config: MakeConfig) -> None:
        cfg, builtin = make_config(timezone="UTC"), load_pricing()
        for alias in ("opus", "sonnet", "haiku", "fable"):
            got = pick(cfg, builtin, force_model=alias)
            assert got.alias_for and got.est_ucents is not None and got.warnings == [], alias

    def test_bad_default_tokens_are_rejected(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "default_tokens": {"light": {"inptu": 5}}}})
        with pytest.raises(ConfigError, match="router.run.default_tokens.light"):
            pick(cfg, table)

    def test_a_bad_models_entry_is_rejected(self, make_config: MakeConfig, table: PriceTable) -> None:
        cfg = make_config(timezone="UTC", router={**ROUTER, "run": {**RUN, "models": {"claude": {"fast": "tiny"}}}})
        with pytest.raises(ConfigError, match=r"router.run.models.claude.fast: unknown tier"):
            pick(cfg, table)


class TestRunHistory:
    """The estimate uses the median of past sessions of the same agent and tier once there are at least 5."""

    def fill(self, store: Store, make_record: Make, sizes: list[int], **kw: Any) -> None:
        for i, n in enumerate(sizes):
            fields: dict[str, Any] = {"prompts": 1, "active_seconds": 0, "usage": {"big": Usage(input=n, output=n * 2)}}
            store.upsert_session(make_record(f"h{i}-{n}-{kw.get('agent', 'claude')}", **(fields | kw)))

    def test_the_baseline_is_the_model_you_use_most_not_the_heavy_one(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        store.upsert_session(make_record("a", prompts=1, usage={"small": Usage(output=1_000_000), "tiny": Usage(output=1_000_000)}))  # $10 and $5
        store.upsert_session(make_record("b", prompts=0, usage={"tiny": Usage(output=1_000_000)}))  # $5 more on tiny: $10 each, the name breaks the tie
        got = pick(run_cfg, table, store=store)
        assert (got.baseline_model, got.baseline_most_used, got.baseline_ucents) == ("small", True, usd("2"))  # light: 1M input tokens at $2/M, not big's $10
        assert pick(run_cfg, table, store=None).baseline_model == "big" and not pick(run_cfg, table, store=None).baseline_most_used

    def test_the_most_used_model_is_counted_by_its_canonical_name_and_only_when_priced(self, run_cfg: Config, make_table: Callable[..., PriceTable], make_record: Make, store: Store) -> None:
        table = make_table(PRICES, aliases={"small-2026": "small"})
        store.upsert_session(make_record("a", usage={"unver": Usage(output=9_000_000)}))  # no verified price: cannot win
        store.upsert_session(make_record("b", usage={"small-2026": Usage(output=300_000)}))  # $3 under an alias
        store.upsert_session(make_record("c", usage={"small": Usage(output=300_000), "big": Usage(output=100_000)}))  # $3 + $5: small totals $6, big $5
        assert pick(run_cfg, table, store=store).baseline_model == "small"

    def test_the_baseline_is_the_model_of_the_last_30_days_counted_from_the_newest_session(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        """``big`` ($50 of output) in June, ``tiny`` ($5) in July, ``small`` ($10) on July 15, the newest session: the window starts on
        June 15, so June 10 is out and ``small`` leads. The day it is run does not matter, the database decides."""
        store.upsert_session(make_record("a", started_at="2026-06-10T10:00:00Z", usage={"big": MILLION_OUT}))
        store.upsert_session(make_record("b", started_at="2026-07-01T10:00:00Z", usage={"tiny": MILLION_OUT}))
        store.upsert_session(make_record("c", started_at="2026-07-15T10:00:00Z", usage={"small": MILLION_OUT}))
        got = pick(run_cfg, table, store=store)
        assert (got.baseline_model, got.baseline_basis, got.baseline_since, got.baseline_most_used) == ("small", "recent", "2026-06-15", True)
        store.upsert_session(make_record("d", started_at="2026-07-16T10:00:00Z", usage={"tiny": Usage(output=3_000_000)}))  # $15, and the window moves a day
        got = pick(run_cfg, table, store=store)
        assert (got.baseline_model, got.baseline_since) == ("tiny", "2026-06-16")

    def test_the_window_is_the_days_of_the_configured_timezone(self, make_config: MakeConfig, table: PriceTable, make_record: Make, store: Store) -> None:
        cfg = make_config(timezone="Asia/Tokyo", router={**ROUTER, "run": RUN})
        store.upsert_session(make_record("a", started_at="2026-07-14T20:00:00Z", usage={"big": MILLION_OUT}))  # July 15, 05:00 in Tokyo
        assert pick(cfg, table, store=store).baseline_since == "2026-06-15"

    def test_the_baseline_falls_back_to_all_time_and_then_to_the_configured_model(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        store.upsert_session(make_record("a", started_at="2026-06-01T10:00:00Z", usage={"big": MILLION_OUT}))
        store.upsert_session(make_record("b", agent="codex", started_at="2026-09-01T10:00:00Z", usage={"tiny": MILLION_OUT}))  # the newest: nothing of claude's is recent
        got = pick(run_cfg, table, store=store)
        assert (got.baseline_model, got.baseline_basis, got.baseline_since, got.baseline_most_used) == ("big", "all_time", None, True)
        nothing = pick(run_cfg, table, agent="gemini", store=store)  # no history at all for this agent
        assert (nothing.baseline_basis, nothing.baseline_since) == ("configured" if nothing.baseline_model else None, None)
        assert pick(run_cfg, table, store=None).baseline_basis == "configured"

    def test_a_pick_that_is_the_baseline_says_so_even_under_an_alias_or_another_spelling(self, run_cfg: Config, make_table: Callable[..., PriceTable], make_record: Make, store: Store) -> None:
        table = make_table(PRICES, aliases={"tiny-2026": "tiny"})
        store.upsert_session(make_record("a", usage={"tiny-2026": MILLION_OUT}))
        assert pick(run_cfg, table, store=store).same_as_baseline  # light picks ``tiny``: the baseline under its alias
        assert not pick(run_cfg, table, force_model="small", store=store).same_as_baseline
        assert not pick(run_cfg, table, store=None).same_as_baseline  # light -> tiny, the configured baseline is big

    def test_without_priced_history_the_baseline_is_the_configured_heavy_model(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        store.upsert_session(make_record("a", usage={"unver": Usage(output=1_000_000)}))
        store.upsert_session(make_record("b", agent="codex", usage={"tiny": Usage(output=1_000_000)}))  # another agent's history
        got = pick(run_cfg, table, store=store)
        assert (got.baseline_model, got.baseline_most_used) == ("big", False)

    def test_a_custom_agent_with_history_gets_a_baseline_too(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        store.upsert_session(make_record("a", agent="my-agent", usage={"tiny": Usage(output=1_000_000)}))
        got = pick(run_cfg, table, agent="my-agent", force_model="small", store=store)
        assert (got.baseline_model, got.baseline_most_used, got.baseline_ucents) == ("tiny", True, usd("1"))

    def test_five_light_sessions_replace_the_default(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        self.fill(store, make_record, [100_000, 200_000, 300_000, 400_000, 500_000])
        got = pick(run_cfg, table, store=store)
        assert got.est_ucents == usd("3.3")  # median 300k input and 600k output on tiny ($1 in, $5 out)
        assert got.baseline_ucents == usd("33")  # the same tokens on big ($10 in, $50 out)
        assert got.history_sessions == 5

    def test_the_number_of_sessions_behind_the_median_is_reported(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        self.fill(store, make_record, [100_000 * n for n in range(1, 9)])
        assert pick(run_cfg, table, store=store).history_sessions == 8
        assert pick(run_cfg, table, "x" * 200, store=store).history_sessions == 0  # medium: no history of that tier

    def test_the_default_budget_is_zero_sessions(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        assert pick(run_cfg, table, store=store).history_sessions == 0  # nothing stored
        assert pick(run_cfg, table, store=None).history_sessions == 0  # no database at all
        self.fill(store, make_record, [100_000, 200_000, 300_000, 400_000])
        assert pick(run_cfg, table, store=store).history_sessions == 0  # four are not enough
        assert pick(run_cfg, table, force_model="small", store=store).history_sessions == 0

    def test_only_the_sessions_that_count_are_counted(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        self.fill(store, make_record, [100_000] * 5)
        self.fill(store, make_record, [100_000] * 3, agent="codex")  # another agent
        self.fill(store, make_record, [100_001] * 3, prompts=0)  # nobody drove them
        assert pick(run_cfg, table, store=store).history_sessions == 5
        assert pick(run_cfg, table, agent="codex", store=store).history_sessions == 0

    def test_four_are_not_enough(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        self.fill(store, make_record, [100_000, 200_000, 300_000, 400_000])
        assert pick(run_cfg, table, store=store).est_ucents == usd("1")  # default_tokens.light

    def test_no_store_means_the_defaults(self, run_cfg: Config, table: PriceTable) -> None:
        assert pick(run_cfg, table, store=None).est_ucents == usd("1")

    def test_an_empty_store_means_the_defaults_for_every_tier(self, run_cfg: Config, table: PriceTable, store: Store) -> None:
        got = [pick(run_cfg, table, prompt, store=store) for prompt in ("", "x" * 200, "x" * 200 + " refactor")]
        assert [(p.tier, p.est_ucents) for p in got] == [("light", usd("1")), ("medium", usd("12")), ("heavy", usd("73.5"))]

    def test_the_median_of_an_even_count_is_the_integer_midpoint(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        self.fill(store, make_record, [100_000, 200_000, 300_000, 400_000, 500_000, 600_000])
        assert pick(run_cfg, table, store=store).est_ucents == usd("3.85")  # input 350k, output 700k

    def test_every_token_bucket_has_its_own_median(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        for i in range(5):
            usage = {"big": Usage(input=1_000_000 * (5 - i), cache_read=1_000_000 * i, cache_write=1_000_000, output=0)}
            store.upsert_session(make_record(f"h{i}", prompts=1, active_seconds=0, usage=usage))
        # medians: input 3M, cache_read 2M, cache_write 1M; on tiny the cache rates fall back to the input rate ($1/M)
        assert pick(run_cfg, table, store=store).est_ucents == usd("6")

    def test_other_agents_tiers_and_promptless_threads_are_left_out(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        self.fill(store, make_record, [100_000] * 4)  # four usable light Claude sessions: one short
        big = {"big": Usage(input=9_000_000)}
        for i in range(10):
            store.upsert_session(make_record(f"codex{i}", agent="codex", prompts=1, active_seconds=0, usage=big))  # another agent
            store.upsert_session(make_record(f"sub{i}", prompts=0, active_seconds=0, usage=big))  # a sub-agent thread: no person drove it
            store.upsert_session(make_record(f"heavy{i}", prompts=1, active_seconds=3_600, usage=big, **HEAVY))  # another tier
        assert pick(run_cfg, table, store=store).est_ucents == usd("1")  # still the default

    def test_history_is_looked_up_for_the_tier_that_was_picked(self, run_cfg: Config, table: PriceTable, make_record: Make, store: Store) -> None:
        for i in range(5):
            store.upsert_session(make_record(f"m{i}", prompts=1, usage={"big": Usage(input=2_000_000)}, **MEDIUM))  # 4 points: medium
        medium = pick(run_cfg, table, "x" * 200, store=store)  # the prompt rule gives 2 points: the medium run tier
        assert (medium.tier, medium.est_ucents) == ("medium", usd("4"))  # 2M input on small at $2/M
        assert pick(run_cfg, table, store=store).est_ucents == usd("1")  # light has no history

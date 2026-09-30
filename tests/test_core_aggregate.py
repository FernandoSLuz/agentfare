import random
import zoneinfo
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import pytest

from agentfare.aggregate import (
    Group,
    Period,
    Row,
    group_rows,
    load_rows,
    model_warnings,
    period_for,
    priced_rows,
    total_ucents,
    unpriced_models,
)
from agentfare.config import Config, ConfigError, day_of, fmt_iso, iso_week_of, parse_iso
from agentfare.cost import price_usage, usage_cost_ucents
from agentfare.db import Store
from agentfare.models import SessionRecord, Usage, slot_of
from agentfare.pricing import PriceTable

UTC = timezone.utc
Make = Callable[..., SessionRecord]

MODELS = {
    "gpt-sol": {"input": 2.0, "cached_input": 0.10, "output": 10.0},
    "claude-x": {"input": 3, "cached_input": 0.30, "cache_write": 3.75, "cache_write_1h": 6, "output": 15},
    "unver": {"input": "unverified", "output": 5},
    "partial": {"input": 2, "cached_input": "unverified", "output": 8},
}
CLAUDE_X_USAGE = Usage(input=10_000, cache_read=500_000, cache_write=40_000, output=8_000)  # $0.45


def toronto() -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo("America/Toronto")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone database")


@pytest.fixture
def table(make_table: Callable[..., PriceTable]) -> PriceTable:
    return make_table(MODELS, aliases={"sol": "gpt-sol"})


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as s:
        yield s


@pytest.fixture
def filled(store: Store, make_record: Make) -> Store:
    """Four sessions across two ISO weeks (2026-W40 and W41), two projects and every price status."""
    for rec in (
        make_record("s1", agent="claude", project="alpha", started_at="2026-09-28T10:00:00Z",
                    usage={"gpt-sol": Usage(input=1_000_000)}),
        make_record("s2", agent="codex", project="alpha", started_at="2026-09-29T10:00:00Z",
                    usage={"SOL": Usage(output=1_000_000), "unver": Usage(input=10, output=20)}),
        make_record("s3", agent="claude", project="beta", started_at="2026-10-06T10:00:00Z",
                    usage={"claude-x-20260101": CLAUDE_X_USAGE, "mystery": Usage(output=5)}),
        make_record("s4", agent="gemini", project="beta", started_at="2026-10-07T10:00:00Z",
                    usage={"partial": Usage(cache_read=1_000_000)}),
    ):
        store.upsert_session(rec)
    return store


@pytest.fixture
def rows(filled: Store, table: PriceTable) -> list[Row]:
    return priced_rows(filled.sessions(), table, UTC)


class TestPricedRows:
    def test_one_row_per_session_and_model(self, rows: list[Row]) -> None:
        assert sorted((r.session.session_id, r.model) for r in rows) == [
            ("s1", "gpt-sol"), ("s2", "SOL"), ("s2", "unver"), ("s3", "claude-x-20260101"), ("s3", "mystery"), ("s4", "partial"),
        ]

    def test_priced_rows_carry_exact_costs(self, rows: list[Row]) -> None:
        by = {(r.session.session_id, r.model): r for r in rows}
        assert by["s1", "gpt-sol"].ucents == 200_000_000
        assert by["s2", "SOL"].ucents == 1_000_000_000
        assert by["s3", "claude-x-20260101"].ucents == 45_000_000
        assert by["s4", "partial"].ucents == 200_000_000  # cached price unverified: reads at the input rate

    def test_one_hour_cache_writes_have_their_own_rate_through_the_store(self, store: Store, make_record: Make, table: PriceTable) -> None:
        both = Usage(cache_write=1_000_000, cache_write_1h=1_000_000)
        store.upsert_session(make_record("a", usage={"claude-x": both, "gpt-sol": Usage(cache_write_1h=1_000_000)}))
        by = {r.model: r for r in priced_rows(store.sessions(), table, UTC)}
        assert by["claude-x"].usage == both and by["claude-x"].ucents == 975_000_000  # $3.75 + $6.00
        assert by["gpt-sol"].ucents == 200_000_000  # no 1-hour or 5-minute price: the input rate

    def test_unverified_and_unknown_models_have_no_cost(self, rows: list[Row]) -> None:
        by = {(r.session.session_id, r.model): r for r in rows}
        assert by["s2", "unver"].ucents is None
        assert by["s3", "mystery"].ucents is None

    def test_canonical_id_follows_aliases_and_date_suffixes(self, rows: list[Row]) -> None:
        by = {r.model: r.canonical for r in rows}
        assert by == {
            "gpt-sol": "gpt-sol", "SOL": "gpt-sol", "claude-x-20260101": "claude-x",
            "unver": "unver",  # in the table but unverified: still its canonical id
            "mystery": "mystery",  # not in the table: the logged id
            "partial": "partial",
        }

    def test_usage_is_carried_over(self, rows: list[Row]) -> None:
        assert {r.model: r.usage for r in rows}["claude-x-20260101"] == CLAUDE_X_USAGE

    def test_a_session_without_slots_has_the_day_and_week_of_its_start(self, rows: list[Row]) -> None:
        by = {r.session.session_id: (r.day, r.week) for r in rows}
        assert by == {
            "s1": ("2026-09-28", "2026-W40"), "s2": ("2026-09-29", "2026-W40"),
            "s3": ("2026-10-06", "2026-W41"), "s4": ("2026-10-07", "2026-W41"),
        }

    def test_the_timezone_moves_the_day_and_the_week(self, store: Store, make_record: Make, table: PriceTable) -> None:
        store.upsert_session(make_record("late", started_at="2027-01-04T03:00:00Z"))
        [row] = priced_rows(store.sessions(), table, UTC)
        assert (row.day, row.week) == ("2027-01-04", "2027-W01")
        [row] = priced_rows(store.sessions(), table, toronto())
        assert (row.day, row.week) == ("2027-01-03", "2026-W53")  # Sunday evening, previous ISO year

    def test_session_with_no_usage_yields_no_rows(self, store: Store, make_record: Make, table: PriceTable) -> None:
        store.upsert_session(make_record("empty", usage={}))
        assert priced_rows(store.sessions(), table, UTC) == []


class TestGroupRows:
    def test_by_model_orders_by_cost_then_key_and_never_counts_unpriced_tokens_as_money(self, rows: list[Row]) -> None:
        groups = group_rows(rows, lambda r: r.canonical)
        assert [g.key for g in groups] == ["gpt-sol", "partial", "claude-x", "mystery", "unver"]
        by = {g.key: g for g in groups}
        assert by["gpt-sol"].ucents == 1_200_000_000 and by["gpt-sol"].unpriced_tokens == 0
        assert by["partial"].ucents == 200_000_000
        assert by["claude-x"].ucents == 45_000_000
        assert (by["unver"].ucents, by["unver"].unpriced_tokens) == (0, 30)
        assert (by["mystery"].ucents, by["mystery"].unpriced_tokens) == (0, 5)

    def test_usage_and_sessions_are_summed(self, rows: list[Row]) -> None:
        by = {g.key: g for g in group_rows(rows, lambda r: r.canonical)}
        sol = by["gpt-sol"]
        assert sol.usage == Usage(input=1_000_000, output=1_000_000)
        assert sol.sessions == {"claude:s1", "codex:s2"} and sol.session_count == 2
        assert by["unver"].usage == Usage(input=10, output=20)

    def test_unpriced_tokens_count_every_bucket(self, store: Store, make_record: Make, table: PriceTable) -> None:
        store.upsert_session(make_record("a", usage={"unver": Usage(input=1, cache_read=2, cache_write=3, output=4, cache_write_1h=5)}))
        [g] = group_rows(priced_rows(store.sessions(), table, UTC), lambda r: r.canonical)
        assert g.unpriced_tokens == 15 and g.ucents == 0

    def test_a_session_counts_once_per_group_even_with_several_rows(self, store: Store, make_record: Make, table: PriceTable) -> None:
        store.upsert_session(make_record("a", usage={"gpt-sol": Usage(input=1), "SOL": Usage(input=1), "sol-20260101": Usage(input=1)}))
        [g] = group_rows(priced_rows(store.sessions(), table, UTC), lambda r: r.canonical)
        assert g.session_count == 1 and g.usage == Usage(input=3)

    def test_by_project_day_and_week(self, rows: list[Row]) -> None:
        by_project = group_rows(rows, lambda r: r.session.project)
        assert [(g.key, g.ucents, g.unpriced_tokens, g.session_count) for g in by_project] == [
            ("alpha", 1_200_000_000, 30, 2),
            ("beta", 245_000_000, 5, 2),
        ]
        by_week = group_rows(rows, lambda r: r.week)
        assert [(g.key, g.ucents) for g in by_week] == [("2026-W40", 1_200_000_000), ("2026-W41", 245_000_000)]
        by_day = group_rows(rows, lambda r: r.day)
        # 2026-09-28 and 2026-10-07 tie at $2.00 and fall back to key order
        assert [(g.key, g.ucents) for g in by_day] == [
            ("2026-09-29", 1_000_000_000), ("2026-09-28", 200_000_000), ("2026-10-07", 200_000_000), ("2026-10-06", 45_000_000),
        ]

    def test_ties_are_broken_by_key_including_all_zero_groups(self, store: Store, make_record: Make, table: PriceTable) -> None:
        for name in ("zeta", "alpha", "mid"):
            store.upsert_session(make_record(name, project=name, usage={"unver": Usage(input=1)}))
        groups = group_rows(priced_rows(store.sessions(), table, UTC), lambda r: r.session.project)
        assert [g.key for g in groups] == ["alpha", "mid", "zeta"]

    def test_totals_ignore_unpriced_rows(self, rows: list[Row]) -> None:
        assert total_ucents(rows) == 1_445_000_000
        assert total_ucents([]) == 0
        assert total_ucents(r for r in rows if r.ucents is None) == 0
        assert total_ucents(rows) == sum(g.ucents for g in group_rows(rows, lambda r: r.canonical))

    def test_empty_input(self) -> None:
        assert group_rows([], lambda r: r.day) == []

    def test_group_defaults(self) -> None:
        g = Group(key="k")
        assert (g.ucents, g.unpriced_tokens, g.session_count, g.usage) == (0, 0, 0, Usage())


class TestUnpricedModels:
    def test_every_model_whose_spend_is_missing_or_approximate_is_named(self, rows: list[Row], table: PriceTable) -> None:
        warnings = unpriced_models(rows, table)
        assert list(warnings) == ["mystery", "partial", "unver"]  # sorted by the logged id
        assert "not in the price table" in warnings["mystery"]
        assert "unverified" in warnings["unver"] and "not counted" in warnings["unver"]
        assert "cached-input price is unverified" in warnings["partial"]

    def test_clean_models_produce_no_warning(self, filled: Store, table: PriceTable) -> None:
        clean = priced_rows(filled.sessions(project="alpha", agent="claude"), table, UTC)
        assert unpriced_models(clean, table) == {}
        assert unpriced_models([], table) == {}

    def test_the_logged_spelling_is_used_as_the_key(self, store: Store, make_record: Make, table: PriceTable) -> None:
        store.upsert_session(make_record("a", usage={"Unver-20260101": Usage(input=1)}))
        warnings = unpriced_models(priced_rows(store.sessions(), table, UTC), table)
        assert list(warnings) == ["Unver-20260101"]

    def test_an_unverified_one_hour_price_is_named_although_the_model_is_priced(self, store: Store, make_record: Make, make_table: Callable[..., PriceTable]) -> None:
        table = make_table({"hour": {"input": 3, "cache_write": 3.75, "cache_write_1h": "unverified", "output": 15}})
        store.upsert_session(make_record("a", usage={"hour": Usage(cache_write_1h=1_000_000)}))
        rows = priced_rows(store.sessions(), table, UTC)
        assert rows[0].ucents == 375_000_000  # counted, at the 5-minute rate
        assert list(unpriced_models(rows, table).values()) == [
            "hour: 1-hour cache-write price is unverified, 1-hour cache writes are billed at the 5-minute cache-write rate"
        ]

    def test_one_warning_per_model_however_many_rows(self, store: Store, make_record: Make, table: PriceTable) -> None:
        for i in range(3):
            store.upsert_session(make_record(f"s{i}", usage={"mystery": Usage(input=1)}))
        assert list(unpriced_models(priced_rows(store.sessions(), table, UTC), table)) == ["mystery"]


class TestModelWarnings:
    """One warning per model that has no verified price or only a partly verified one: what ``run`` names, from ids alone."""

    def test_every_model_with_a_price_problem_is_named_in_sorted_order(self, table: PriceTable) -> None:
        warnings = model_warnings(["unver", "gpt-sol", "mystery", "partial"], table)
        assert list(warnings) == ["mystery", "partial", "unver"]
        assert "not in the price table" in warnings["mystery"] and "add it to pricing.yaml" in warnings["mystery"]
        assert "cached-input price is unverified" in warnings["partial"]
        assert "price is unverified, its spend is not counted" in warnings["unver"]

    def test_clean_models_and_no_models_give_nothing(self, table: PriceTable) -> None:
        assert model_warnings(["gpt-sol", "sol", "claude-x-20260101"], table) == {}  # priced, aliased, dated: nothing to say
        assert model_warnings([], table) == {}

    def test_a_model_given_twice_is_named_once_under_the_spelling_it_was_given(self, table: PriceTable) -> None:
        assert list(model_warnings(iter(["Mystery-9", "Mystery-9", "mystery"]), table)) == ["Mystery-9", "mystery"]

    def test_it_is_what_unpriced_models_says_about_the_models_of_the_rows(self, rows: list[Row], table: PriceTable) -> None:
        assert unpriced_models(rows, table) == model_warnings({r.model for r in rows}, table)


class TestPeriodFor:
    def test_no_arguments_means_all_time(self, make_config: Callable[..., Config]) -> None:
        assert period_for(make_config(timezone="UTC")) == Period(None, None, "all time")

    def test_since_and_until_are_inclusive_local_days_in_utc(self, make_config: Callable[..., Config]) -> None:
        p = period_for(make_config(timezone="UTC"), since="2026-09-21", until="2026-09-27")
        assert p == Period("2026-09-21T00:00:00Z", "2026-09-28T00:00:00Z", "2026-09-21 .. 2026-09-27")

    def test_open_ended_periods(self, make_config: Callable[..., Config]) -> None:
        cfg = make_config(timezone="UTC")
        assert period_for(cfg, since="2026-09-21") == Period("2026-09-21T00:00:00Z", None, "from 2026-09-21")
        assert period_for(cfg, until="2026-09-27") == Period(None, "2026-09-28T00:00:00Z", "up to 2026-09-27")

    def test_until_rolls_over_month_and_year_ends(self, make_config: Callable[..., Config]) -> None:
        cfg = make_config(timezone="UTC")
        assert period_for(cfg, until="2026-09-30").until == "2026-10-01T00:00:00Z"
        assert period_for(cfg, until="2026-12-31").until == "2027-01-01T00:00:00Z"
        assert period_for(cfg, until="2028-02-28").until == "2028-02-29T00:00:00Z"  # leap year

    def test_days_are_local_days_in_another_timezone(self, make_config: Callable[..., Config]) -> None:
        toronto()
        cfg = make_config(timezone="America/Toronto")
        p = period_for(cfg, since="2026-09-21", until="2026-09-27")
        assert (p.since, p.until) == ("2026-09-21T04:00:00Z", "2026-09-28T04:00:00Z")  # EDT is UTC-4
        winter = period_for(cfg, since="2026-01-15", until="2026-01-15")
        assert (winter.since, winter.until) == ("2026-01-15T05:00:00Z", "2026-01-16T05:00:00Z")  # EST is UTC-5

    def test_a_dst_change_inside_the_period_is_respected(self, make_config: Callable[..., Config]) -> None:
        toronto()  # clocks go back on 2026-11-01
        cfg = make_config(timezone="America/Toronto")
        p = period_for(cfg, since="2026-11-01", until="2026-11-01")
        assert (p.since, p.until) == ("2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z")  # a 25 hour day

    def test_week_true_is_the_iso_week_containing_now(self, make_config: Callable[..., Config], frozen_now: Callable[..., object]) -> None:
        frozen_now("2026-09-30T12:00:00Z")
        p = period_for(make_config(timezone="UTC"), week=True)
        assert p == Period("2026-09-28T00:00:00Z", "2026-10-05T00:00:00Z", "week 2026-09-28 .. 2026-10-04")

    def test_week_true_uses_the_local_calendar(self, make_config: Callable[..., Config], frozen_now: Callable[..., object]) -> None:
        toronto()
        frozen_now("2026-09-28T02:00:00Z")  # Monday in UTC, Sunday evening in Toronto
        assert period_for(make_config(timezone="UTC"), week=True).label == "week 2026-09-28 .. 2026-10-04"
        p = period_for(make_config(timezone="America/Toronto"), week=True)
        assert p == Period("2026-09-21T04:00:00Z", "2026-09-28T04:00:00Z", "week 2026-09-21 .. 2026-09-27")

    def test_week_label(self, make_config: Callable[..., Config]) -> None:
        p = period_for(make_config(timezone="UTC"), week="2026-W39")
        assert p == Period("2026-09-21T00:00:00Z", "2026-09-28T00:00:00Z", "week 2026-09-21 .. 2026-09-27")

    def test_week_label_in_another_timezone(self, make_config: Callable[..., Config]) -> None:
        try:
            zoneinfo.ZoneInfo("Asia/Tokyo")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database")
        p = period_for(make_config(timezone="Asia/Tokyo"), week="2026-W39")
        assert (p.since, p.until) == ("2026-09-20T15:00:00Z", "2026-09-27T15:00:00Z")  # JST is UTC+9

    def test_week_label_across_the_dst_change(self, make_config: Callable[..., Config]) -> None:
        toronto()
        p = period_for(make_config(timezone="America/Toronto"), week="2026-W44")
        assert (p.since, p.until) == ("2026-10-26T04:00:00Z", "2026-11-02T05:00:00Z")  # 169 hours

    def test_week_53_ends_in_the_next_calendar_year(self, make_config: Callable[..., Config]) -> None:
        p = period_for(make_config(timezone="UTC"), week="2026-W53")
        assert p == Period("2026-12-28T00:00:00Z", "2027-01-04T00:00:00Z", "week 2026-12-28 .. 2027-01-03")

    def test_week_wins_over_since_and_until(self, make_config: Callable[..., Config]) -> None:
        p = period_for(make_config(timezone="UTC"), week="2026-W39", since="2020-01-01", until="2020-01-02")
        assert p.since == "2026-09-21T00:00:00Z" and p.until == "2026-09-28T00:00:00Z"

    @pytest.mark.parametrize(("since", "until"), [("2026-09-30", "2026-09-29"), ("2026-09-30", "2026-09-01"), ("2027-01-01", "2026-12-31")])
    def test_since_later_than_until_is_a_config_error(self, make_config: Callable[..., Config], since: str, until: str) -> None:
        with pytest.raises(ConfigError, match=f"since {since} is later than until {until}"):
            period_for(make_config(timezone="UTC"), since=since, until=until)

    def test_since_later_than_until_is_judged_on_local_days(self, make_config: Callable[..., Config]) -> None:
        with pytest.raises(ConfigError, match="later than"):
            period_for(make_config(timezone="Asia/Tokyo"), since="2026-03-08", until="2026-03-07")
        assert period_for(make_config(timezone="Asia/Tokyo"), since="2026-03-08", until="2026-03-08").until == "2026-03-08T15:00:00Z"

    def test_a_single_day_is_not_an_empty_period(self, make_config: Callable[..., Config]) -> None:
        p = period_for(make_config(timezone="UTC"), since="2026-09-30", until="2026-09-30")
        assert (p.since, p.until) == ("2026-09-30T00:00:00Z", "2026-10-01T00:00:00Z")

    def test_bad_input_is_a_config_error(self, make_config: Callable[..., Config]) -> None:
        cfg = make_config(timezone="UTC")
        for kwargs in ({"week": "2026-39"}, {"week": "2027-W53"}, {"since": "yesterday"}, {"until": "2026-13-01"}, {"since": "2026-9-1x"}):
            with pytest.raises(ConfigError):
                period_for(cfg, **kwargs)


class TestLoadRows:
    def test_week_boundaries_follow_the_local_timezone(
        self, store: Store, make_record: Make, make_config: Callable[..., Config], table: PriceTable
    ) -> None:
        toronto()
        cfg = make_config(timezone="America/Toronto")
        for sid, started in (
            ("before", "2026-09-21T03:59:59Z"),  # Sunday 23:59:59 EDT, previous week
            ("first", "2026-09-21T04:00:00Z"),  # Monday 00:00:00 EDT
            ("last", "2026-09-28T03:59:59Z"),  # Sunday 23:59:59 EDT
            ("after", "2026-09-28T04:00:00Z"),  # next Monday 00:00:00 EDT
        ):
            store.upsert_session(make_record(sid, started_at=started))
        got = load_rows(cfg, store, table, period=period_for(cfg, week="2026-W39"))
        assert sorted(r.session.session_id for r in got) == ["first", "last"]
        assert {r.week for r in got} == {"2026-W39"}

    def test_since_is_inclusive_and_until_exclusive_at_the_instant(
        self, store: Store, make_record: Make, make_config: Callable[..., Config], table: PriceTable
    ) -> None:
        cfg = make_config(timezone="UTC")
        for sid, started in (("a", "2026-09-20T23:59:59Z"), ("b", "2026-09-21T00:00:00Z"), ("c", "2026-09-27T23:59:59Z"), ("d", "2026-09-28T00:00:00Z")):
            store.upsert_session(make_record(sid, started_at=started))
        got = load_rows(cfg, store, table, period=period_for(cfg, since="2026-09-21", until="2026-09-27"))
        assert sorted(r.session.session_id for r in got) == ["b", "c"]

    def test_no_period_loads_everything(self, filled: Store, make_config: Callable[..., Config], table: PriceTable) -> None:
        assert len(load_rows(make_config(timezone="UTC"), filled, table)) == 6

    def test_agent_and_project_filters(self, filled: Store, make_config: Callable[..., Config], table: PriceTable) -> None:
        cfg = make_config(timezone="UTC")
        assert {r.session.session_id for r in load_rows(cfg, filled, table, agent="claude")} == {"s1", "s3"}
        assert {r.session.session_id for r in load_rows(cfg, filled, table, project="BETA")} == {"s3", "s4"}
        assert {r.session.session_id for r in load_rows(cfg, filled, table, agent="claude", project="beta")} == {"s3"}

    def test_rows_use_the_configured_timezone_for_day_and_week(
        self, store: Store, make_record: Make, make_config: Callable[..., Config], table: PriceTable
    ) -> None:
        store.upsert_session(make_record("late", started_at="2027-01-04T03:00:00Z"))
        [utc] = load_rows(make_config(timezone="UTC"), store, table)
        assert (utc.day, utc.week) == ("2027-01-04", "2027-W01")
        toronto()
        [local] = load_rows(make_config(timezone="America/Toronto"), store, table)
        assert (local.day, local.week) == ("2027-01-03", "2026-W53")


class TestBoundsMatchBucketing:
    """A period must contain exactly the instants whose local day (or ISO week) is the one asked for."""

    @pytest.mark.parametrize(
        ("name", "step_minutes", "start"),
        [
            ("America/Toronto", 60, "2025-12-25"),  # DST changes at 02:00
            ("Asia/Tokyo", 60, "2025-12-25"),  # no DST at all
            ("America/Havana", 60, "2025-12-25"),  # spring forward at 00:00: that midnight does not exist
            ("Asia/Beirut", 60, "2025-12-25"),  # fall back at 00:00: 23:00-24:00 happens twice
            ("America/Santiago", 60, "2025-12-25"),  # changes at 24:00 local, so the day after starts at the new offset
            ("Africa/Cairo", 60, "2025-12-25"),  # DST that came back in 2023, changing at 00:00 and 24:00
            ("Africa/Casablanca", 60, "2025-12-25"),  # negative DST around Ramadan
            ("Europe/Dublin", 60, "2025-12-25"),  # negative DST: winter time is the "daylight" one
            ("Australia/Lord_Howe", 30, "2025-12-25"),  # a 30 minute DST shift
            ("Pacific/Chatham", 60, "2025-12-25"),  # +12:45 / +13:45
            ("Asia/Kolkata", 60, "2025-12-25"),  # a half-hour offset, no DST
            ("Pacific/Auckland", 60, "2025-12-25"),  # southern hemisphere, +12/+13
            ("Pacific/Apia", 60, "2011-06-01"),  # 2011-12-30 never happened: the date line moved
        ],
    )
    def test_every_instant_of_a_year_falls_in_its_own_day_and_week(
        self, make_config: Callable[..., Config], name: str, step_minutes: int, start: str
    ) -> None:
        try:
            zoneinfo.ZoneInfo(name)
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip(f"no time zone database for {name}")
        cfg = make_config(timezone=name)
        tz = cfg.tz
        days: dict[str, Period] = {}
        weeks: dict[str, Period] = {}
        instant = datetime.fromisoformat(start).replace(tzinfo=UTC)
        for _ in range(60 * 24 * 375 // step_minutes):
            ts = fmt_iso(instant)
            day, week = day_of(ts, tz), iso_week_of(ts, tz)
            by_day = days.setdefault(day, period_for(cfg, since=day, until=day))
            by_week = weeks.setdefault(week, period_for(cfg, week=week))
            assert by_day.since <= ts < by_day.until, (ts, day, by_day)
            assert by_week.since <= ts < by_week.until, (ts, week, by_week)
            instant += timedelta(minutes=step_minutes)

    def test_a_day_whose_midnight_does_not_exist_starts_when_the_day_first_exists(self, make_config: Callable[..., Config]) -> None:
        try:
            zoneinfo.ZoneInfo("America/Havana")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database")
        cfg = make_config(timezone="America/Havana")  # 2026-03-08: 00:00 jumps straight to 01:00
        p = period_for(cfg, since="2026-03-08", until="2026-03-08")
        assert (p.since, p.until) == ("2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z")  # a 23 hour day
        assert day_of("2026-03-08T04:59:59Z", cfg.tz) == "2026-03-07"
        assert day_of("2026-03-08T05:00:00Z", cfg.tz) == "2026-03-08"


class TestEdgeOfTime:
    def test_typed_dates_that_cannot_be_represented_are_a_config_error(self, make_config: Callable[..., Config]) -> None:
        try:
            zoneinfo.ZoneInfo("Asia/Tokyo")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database")
        for timezone_name, kwargs in (
            ("Asia/Tokyo", {"since": "0001-01-01"}),  # midnight in Tokyo is before year 1 in UTC
            ("UTC", {"until": "9999-12-31"}),  # the day after does not exist
            ("UTC", {"week": "9999-W52"}),
        ):
            with pytest.raises(ConfigError):
                period_for(make_config(timezone=timezone_name), **kwargs)

    def test_a_session_stored_at_the_edge_of_time_does_not_break_every_report(
        self, store: Store, make_record: Make, table: PriceTable
    ) -> None:
        store.upsert_session(make_record("zero-date", started_at="0001-01-01T00:00:00Z"))
        store.upsert_session(make_record("normal", started_at="2026-09-30T10:00:00Z"))
        rows = priced_rows(store.sessions(), table, toronto())
        assert {r.session.session_id: (r.day, r.week) for r in rows} == {
            "zero-date": ("0001-01-01", "0001-W01"),
            "normal": ("2026-09-30", "2026-W40"),
        }


def zone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip(f"no time zone database for {name}")


def slot_cost(i: int) -> int:
    """What ``spanning_session`` spent in its i-th slot, in micro-cents: claude-x, 1000 x (i+1) in at $3 and 100 x (i+1) out at $15."""
    return (i + 1) * (1000 * 300 + 100 * 1500)


# The slots of one session that starts at 18:05 UTC on Sunday 2026-09-27 and runs into Monday, with where each one falls
# on the local calendar: (slot, {zone: (day, ISO week)}). Monday 2026-09-28 is the first day of 2026-W40.
SPANNING_SLOTS = [
    ("2026-09-27T18:00:00Z", {"UTC": ("2026-09-27", "2026-W39"), "America/Toronto": ("2026-09-27", "2026-W39"), "Asia/Kolkata": ("2026-09-27", "2026-W39"), "Asia/Kathmandu": ("2026-09-27", "2026-W39")}),
    ("2026-09-27T18:15:00Z", {"UTC": ("2026-09-27", "2026-W39"), "America/Toronto": ("2026-09-27", "2026-W39"), "Asia/Kolkata": ("2026-09-27", "2026-W39"), "Asia/Kathmandu": ("2026-09-28", "2026-W40")}),
    ("2026-09-27T18:30:00Z", {"UTC": ("2026-09-27", "2026-W39"), "America/Toronto": ("2026-09-27", "2026-W39"), "Asia/Kolkata": ("2026-09-28", "2026-W40"), "Asia/Kathmandu": ("2026-09-28", "2026-W40")}),
    ("2026-09-28T00:00:00Z", {"UTC": ("2026-09-28", "2026-W40"), "America/Toronto": ("2026-09-27", "2026-W39"), "Asia/Kolkata": ("2026-09-28", "2026-W40"), "Asia/Kathmandu": ("2026-09-28", "2026-W40")}),
    ("2026-09-28T03:45:00Z", {"UTC": ("2026-09-28", "2026-W40"), "America/Toronto": ("2026-09-27", "2026-W39"), "Asia/Kolkata": ("2026-09-28", "2026-W40"), "Asia/Kathmandu": ("2026-09-28", "2026-W40")}),
    ("2026-09-28T04:00:00Z", {"UTC": ("2026-09-28", "2026-W40"), "America/Toronto": ("2026-09-28", "2026-W40"), "Asia/Kolkata": ("2026-09-28", "2026-W40"), "Asia/Kathmandu": ("2026-09-28", "2026-W40")}),
]


@pytest.fixture
def spanning_session(store: Store, make_record: Make) -> SessionRecord:
    slots = {(slot, "claude-x"): Usage(input=1000 * (i + 1), output=100 * (i + 1)) for i, (slot, _) in enumerate(SPANNING_SLOTS)}
    whole = sum((u for u in slots.values()), Usage())
    rec = make_record("span", started_at="2026-09-27T18:05:00Z", wall_seconds=10 * 3600, usage={"claude-x": whole}, slots=slots)
    store.upsert_session(rec)
    return rec


class TestEachSlotLandsOnItsLocalDayAndWeek:
    """Money belongs to when it was spent: a session that runs over midnight, over an ISO week, in any time zone."""

    ZONES = ["UTC", "America/Toronto", "Asia/Kolkata", "Asia/Kathmandu"]

    @pytest.mark.parametrize("name", ZONES)
    def test_every_row_has_the_day_and_week_of_its_own_slot(self, store: Store, table: PriceTable, spanning_session: SessionRecord, name: str) -> None:
        rows = priced_rows(store.sessions(), table, zone(name))
        assert [(r.slot, (r.day, r.week)) for r in rows] == [(slot, where[name]) for slot, where in SPANNING_SLOTS]
        assert [r.ucents for r in rows] == [slot_cost(i) for i in range(len(SPANNING_SLOTS))]

    @pytest.mark.parametrize("name", ZONES)
    def test_the_money_of_each_day_and_week_is_what_was_spent_in_it(self, store: Store, table: PriceTable, spanning_session: SessionRecord, name: str) -> None:
        rows = priced_rows(store.sessions(), table, zone(name))
        for attribute, index in (("day", 0), ("week", 1)):
            want: dict[str, int] = {}
            for i, (_, where) in enumerate(SPANNING_SLOTS):
                want[where[name][index]] = want.get(where[name][index], 0) + slot_cost(i)
            got = {g.key: g.ucents for g in group_rows(rows, lambda r: getattr(r, attribute))}
            assert got == want, (name, attribute)
            assert all(g.sessions == {"claude:span"} and g.session_count == 1 for g in group_rows(rows, lambda r: getattr(r, attribute)))

    @pytest.mark.parametrize("name", ZONES)
    def test_the_days_add_up_to_the_cost_of_the_whole_session(self, store: Store, table: PriceTable, spanning_session: SessionRecord, name: str) -> None:
        rows = priced_rows(store.sessions(), table, zone(name))
        whole = usage_cost_ucents(spanning_session.usage["claude-x"], table.priced("claude-x"))  # type: ignore[arg-type]
        assert total_ucents(rows) == whole == sum(slot_cost(i) for i in range(len(SPANNING_SLOTS)))
        assert sum(g.ucents for g in group_rows(rows, lambda r: r.day)) == whole
        assert sum(g.ucents for g in group_rows(rows, lambda r: r.week)) == whole

    def test_it_really_differs_from_putting_everything_at_the_start(self, store: Store, table: PriceTable, spanning_session: SessionRecord) -> None:
        by_day = {g.key: g.ucents for g in group_rows(priced_rows(store.sessions(), table, UTC), lambda r: r.day)}
        assert by_day["2026-09-27"] == sum(slot_cost(i) for i in range(3)) and by_day["2026-09-28"] == sum(slot_cost(i) for i in range(3, 6))

    @pytest.mark.parametrize("name", ZONES)
    def test_a_period_of_local_days_holds_what_was_spent_in_those_days(
        self, store: Store, make_config: Callable[..., Config], table: PriceTable, spanning_session: SessionRecord, name: str
    ) -> None:
        cfg = make_config(timezone=name)
        seen = 0
        for day in ("2026-09-27", "2026-09-28"):
            rows = load_rows(cfg, store, table, period=period_for(cfg, since=day, until=day))
            want = sum(slot_cost(i) for i, (_, where) in enumerate(SPANNING_SLOTS) if where[name][0] == day)
            assert total_ucents(rows) == want and {r.day for r in rows} <= {day}, (name, day)
            assert {r.session.key for r in rows} == {"claude:span"}  # one session, in both days
            seen += total_ucents(rows)
        assert seen == sum(slot_cost(i) for i in range(len(SPANNING_SLOTS)))

    def test_a_week_period_shows_only_the_week_s_share(
        self, store: Store, make_config: Callable[..., Config], table: PriceTable, spanning_session: SessionRecord
    ) -> None:
        cfg = make_config(timezone="Asia/Kathmandu")
        w39 = load_rows(cfg, store, table, period=period_for(cfg, week="2026-W39"))
        w40 = load_rows(cfg, store, table, period=period_for(cfg, week="2026-W40"))
        assert total_ucents(w39) == slot_cost(0) and total_ucents(w40) == sum(slot_cost(i) for i in range(1, 6))
        assert [r.session.clipped for r in w39 + w40] == [True] * (len(w39) + len(w40))

    def test_a_session_with_several_models_and_slots_is_one_session_in_a_group(self, store: Store, make_record: Make, table: PriceTable) -> None:
        store.upsert_session(make_record(
            "many", usage={"claude-x": Usage(input=3), "gpt-sol": Usage(input=3)},
            slots={("2026-09-30T10:00:00Z", "claude-x"): Usage(input=1), ("2026-09-30T10:15:00Z", "claude-x"): Usage(input=2),
                   ("2026-09-30T10:15:00Z", "gpt-sol"): Usage(input=3)},
        ))
        rows = priced_rows(store.sessions(), table, UTC)
        assert len(rows) == 3
        [group] = group_rows(rows, lambda r: r.day)
        assert group.sessions == {"claude:many"} and group.session_count == 1 and group.usage == Usage(input=6)

    def test_rows_come_in_a_fixed_order(self, store: Store, make_record: Make, table: PriceTable) -> None:
        store.upsert_session(make_record(
            "o", usage={"gpt-sol": Usage(input=3), "claude-x": Usage(input=3)},
            slots={("2026-09-30T10:15:00Z", "gpt-sol"): Usage(input=1), ("2026-09-30T10:00:00Z", "gpt-sol"): Usage(input=2),
                   ("2026-09-30T10:15:00Z", "claude-x"): Usage(input=3)},
        ))
        assert [(r.model, r.slot) for r in priced_rows(store.sessions(), table, UTC)] == [
            ("claude-x", "2026-09-30T10:15:00Z"), ("gpt-sol", "2026-09-30T10:00:00Z"), ("gpt-sol", "2026-09-30T10:15:00Z")
        ]

    def test_fractional_prices_may_differ_from_the_whole_by_half_a_micro_cent_per_slot_at_most(self, store: Store, make_record: Make, make_table: Callable[..., PriceTable]) -> None:
        table = make_table({"cheap": {"input": 0.075, "output": 0.3}})  # 7.5 micro-cents a token: every slot rounds on its own
        slots = {(slot, "cheap"): Usage(input=1 + i, output=1) for i, (slot, _) in enumerate(SPANNING_SLOTS)}
        store.upsert_session(make_record("f", usage={"cheap": sum(slots.values(), Usage())}, slots=slots))
        total = total_ucents(priced_rows(store.sessions(), table, UTC))
        whole = usage_cost_ucents(sum(slots.values(), Usage()), table.priced("cheap"))  # type: ignore[arg-type]
        assert abs(total - whole) <= len(slots) / 2


class TestMoneyByPeriodProperties:
    """For random sessions and random periods, the money of a period is exactly what was spent in it, whatever the slicing."""

    # every price is a whole number of micro-cents per token, so a slot's cost and a whole session's cost add up exactly
    PRICES = {
        "claude-x": {"input": 3, "cached_input": 0.30, "cache_write": 3.75, "cache_write_1h": 6, "output": 15},
        "gpt-sol": {"input": 2.0, "cached_input": 0.10, "output": 10.0},
        "unver": {"input": "unverified", "output": 5},
    }
    MODELS = tuple(PRICES)

    def random_records(self, rnd: random.Random, how_many: int) -> list[SessionRecord]:
        origin = datetime(2026, 9, 25, tzinfo=UTC)
        records = []
        for i in range(how_many):
            first = origin + timedelta(seconds=rnd.randrange(0, 6 * 86400))
            slots: dict[tuple[str, str], Usage] = {}
            for _ in range(rnd.choice([0, 1, 1, 3, 8, 30])):
                model, at = rnd.choice(self.MODELS), first + timedelta(seconds=rnd.randrange(0, 3 * 86400))
                u = Usage(rnd.randrange(0, 5000), rnd.randrange(0, 90_000), rnd.randrange(0, 3000), rnd.randrange(0, 900), rnd.randrange(0, 400))
                key = (slot_of(at), model)
                slots[key] = slots[key] + u if key in slots else u
            usage: dict[str, Usage] = {}
            for (_, model), u in slots.items():
                usage[model] = usage[model] + u if model in usage else u
            if not slots:  # a parser that recorded no timing: everything counts at the start
                usage = {rnd.choice(self.MODELS): Usage(rnd.randrange(1, 5000), 0, 0, rnd.randrange(1, 900))}
            started = min((parse_iso(slot) for slot, _ in slots), default=first)
            records.append(SessionRecord(
                agent=rnd.choice(["claude", "codex", "gemini"]), session_id=f"r{i}", source_path="/x", project=f"p{i % 5}",
                started_at=fmt_iso(started), ended_at=fmt_iso(started), wall_seconds=0, active_seconds=0, usage=usage, slots=slots,
            ))
        return records

    def random_bound(self, rnd: random.Random) -> datetime:
        at = datetime(2026, 9, 24, tzinfo=UTC) + timedelta(seconds=rnd.randrange(0, 11 * 86400))
        return at if rnd.random() < 0.5 else at.replace(minute=at.minute - at.minute % 15, second=0)  # a slot edge or anywhere

    def direct_sum(self, records: list[SessionRecord], table: PriceTable, lo: datetime | None, hi: datetime | None) -> int:
        """Straight from the records: each slot in [lo, hi) priced on its own, no database and no clipping involved."""
        total = 0
        for rec in records:
            for (slot, model), u in rec.effective_slots().items():
                price = table.priced(model)
                at = parse_iso(slot)
                if price and (lo is None or at >= lo) and (hi is None or at < hi):
                    total += usage_cost_ucents(u, price)
        return total

    @pytest.fixture
    def prices(self, make_table: Callable[..., PriceTable]) -> PriceTable:
        return make_table(self.PRICES)

    @pytest.mark.parametrize("seed", range(12))
    def test_the_money_of_a_period_is_the_sum_of_its_slots(self, seed: int, prices: PriceTable) -> None:
        rnd = random.Random(seed)
        records = self.random_records(rnd, 60)
        with Store(":memory:") as store:
            for rec in records:
                store.upsert_session(rec)
            for _ in range(25):
                lo, hi = sorted((self.random_bound(rnd), self.random_bound(rnd)))
                lo_arg, hi_arg = (fmt_iso(lo), fmt_iso(hi)) if rnd.random() < 0.7 else (fmt_iso(lo), None)
                clipped = store.sessions(since=lo_arg, until=hi_arg)
                want = self.direct_sum(records, prices, parse_iso(lo_arg), parse_iso(hi_arg) if hi_arg else None)
                assert total_ucents(priced_rows(clipped, prices, UTC)) == want
                # a clipped session priced as a whole is the same money: its usage is what was spent inside the period
                assert sum(price_usage(s.usage, prices).ucents for s in clipped) == want

    @pytest.mark.parametrize("seed", range(12))
    def test_the_cost_of_a_whole_session_is_the_sum_of_all_its_slots(self, seed: int, prices: PriceTable) -> None:
        records = self.random_records(random.Random(1000 + seed), 60)
        with Store(":memory:") as store:
            for rec in records:
                store.upsert_session(rec)
            stored = {s.key: s for s in store.sessions()}
            assert set(stored) == {r.key for r in records}
            for rec in records:
                by_slots = sum(usage_cost_ucents(u, p) for (_, m), u in rec.effective_slots().items() if (p := prices.priced(m)))
                assert total_ucents(priced_rows([stored[rec.key]], prices, UTC)) == by_slots == price_usage(rec.usage, prices).ucents
                assert stored[rec.key].usage == rec.usage and not stored[rec.key].clipped

    @pytest.mark.parametrize("seed", range(12))
    def test_periods_that_tile_the_timeline_add_up_to_everything(self, seed: int, prices: PriceTable) -> None:
        rnd = random.Random(2000 + seed)
        records = self.random_records(rnd, 60)
        with Store(":memory:") as store:
            for rec in records:
                store.upsert_session(rec)
            everything = total_ucents(priced_rows(store.sessions(), prices, UTC))
            assert everything == self.direct_sum(records, prices, None, None)
            cuts = sorted({fmt_iso(self.random_bound(rnd)) for _ in range(6)})
            edges: list[str | None] = [None, *cuts, None]
            tiles = [store.sessions(since=a, until=b) for a, b in zip(edges, edges[1:])]
            assert sum(total_ucents(priced_rows(t, prices, UTC)) for t in tiles) == everything
            assert sum(s.total_usage.total for t in tiles for s in t) == sum(r.total_usage.total for r in records)
            # a session can be in several tiles, and is whole in exactly the ones that hold all of its slots
            for tile in tiles:
                for s in tile:
                    assert s.clipped == (s.total_usage != next(r for r in records if r.key == s.key).total_usage)

    def test_periods_in_another_time_zone_tile_too(self, make_config: Callable[..., Config], prices: PriceTable) -> None:
        cfg = make_config(timezone="Asia/Kathmandu")
        records = self.random_records(random.Random(99), 80)
        with Store(":memory:") as store:
            for rec in records:
                store.upsert_session(rec)
            days = [f"2026-09-{d:02d}" for d in range(24, 31)] + [f"2026-10-{d:02d}" for d in range(1, 8)]
            per_day = {d: total_ucents(load_rows(cfg, store, prices, period=period_for(cfg, since=d, until=d))) for d in days}
            by_row = {g.key: g.ucents for g in group_rows(load_rows(cfg, store, prices), lambda r: r.day)}
            assert {d: v for d, v in per_day.items() if v} == {d: v for d, v in by_row.items() if d in days and v}
            assert sum(by_row.values()) == self.direct_sum(records, prices, None, None)

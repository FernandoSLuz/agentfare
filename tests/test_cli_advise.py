"""`agentfare advise` end to end: CliRunner on the synthetic fixtures and on small hand-built worlds."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zoneinfo
from collections.abc import Callable
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from agentfare.cli import app
from agentfare.config import Config, fmt_iso, parse_iso
from agentfare.cost import price_usage
from agentfare.db import Store
from agentfare.ingest import ingest
from agentfare.models import SessionRecord, Usage
from agentfare.money import format_usd
from agentfare.pricing import load_pricing
from agentfare.render import percent, share_bp, usd

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config.yaml"
TITLE = "Router savings (per ISO week)"
LEGEND = [
    "Sess fully priced sessions, Switched sessions with an advised switch, Router the",
    "same spend with every advised switch applied",
]
CAVEAT = [
    "Note: Each estimate re-prices the same token counts on the recommended model.",
    "Tokenizers and caching differ and answer quality is not measured, so read it as",
    "a hint, not a promise.",
]
World = Callable[..., tuple[str, ...]]


@pytest.fixture(autouse=True)
def frozen(frozen_now: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch) -> None:
    frozen_now("2026-09-30T12:00:00Z")
    monkeypatch.setenv("COLUMNS", "80")  # Rich reads it: the layout is the 80-column one wherever the tests run


def advise(*args: str, prefix: tuple[str, ...] = ("--config", str(FIXTURE_CONFIG))) -> Result:
    return CliRunner().invoke(app, [*prefix, "advise", *args])


def lines(result: Result) -> list[str]:
    assert result.exit_code == 0, result.output
    return [ln.rstrip() for ln in result.stdout.splitlines()]


def flat(out: list[str]) -> str:
    """The output as one line, so an assertion does not depend on where the 80-column wrap fell."""
    return " ".join(ln.strip() for ln in out)


def is_rule(line: str) -> bool:
    return bool(line) and set(line) == {"─"}


def ranked(out: list[str]) -> list[str]:
    """The first line of each listed session: ``1. session abc: ...``."""
    return [ln for ln in out if re.match(r"^ ?\d+\. session ", ln)]


# ---- the synthetic fixtures: one heavy Claude, one light Codex, one light Gemini session --------------
CODEX_UCENTS, CODEX_ROUTED_UCENTS, GEMINI_UCENTS = 76_000_000, 14_600_000, 22_000_000  # $0.76 -> $0.146, $0.22 (hand-computed)


@lru_cache(maxsize=1)
def claude_ucents() -> int:
    """The Claude fixture's cost from the price engine: how it prices cache writes is not this file's business."""
    with Store(":memory:") as store:
        cfg = Config.load(FIXTURE_CONFIG)
        ingest(cfg, store)
        [session] = store.sessions(agent="claude")
        return price_usage(session.usage, load_pricing(cfg)).ucents


class TestFixtures:
    def totals(self) -> tuple[int, int, int]:
        actual = CODEX_UCENTS + GEMINI_UCENTS + claude_ucents()
        saved = CODEX_UCENTS - CODEX_ROUTED_UCENTS
        return actual, actual - saved, saved

    def test_default_view_lists_only_the_switch(self) -> None:
        out = lines(advise())
        at = out.index(TITLE)
        assert out[:at] == [
            "AgentFare advise: all time",
            "3 sessions, 1 with a switch advised",
            "",
            "1. session 7c8d9e0f: you spent $0.76 on gpt-6-astra → gpt-6.1-sol would likely",
            "   have handled this for ~$0.1460 (5.2x cheaper)",
            "   codex, fake-repo, 2026-09-29: score 2/10 (light): files_touched 2 +1,",
            "   active_minutes 10 +1",
            "2 more sessions where no switch is advised (heavy, already cheaper, no verified",
            f"price): {usd(GEMINI_UCENTS + claude_ucents())} spent; --all lists them.",
            "",
        ]
        actual, routed, saved = self.totals()
        money = [format_usd(amount, 4) for amount in (actual, routed, saved)]  # the row has a sub-cent amount: all three show four decimals
        assert money == ["$3.6310", "$3.0170", "$0.6140"]
        week = ["2026-W40", "3", "1", *money, percent(share_bp(saved, actual))]
        assert out[at + 1].split() == ["Week", "Sess", "Switched", "Actual", "Router", "Saved", "Saved", "%"]
        assert is_rule(out[at + 2]) and is_rule(out[at + 4])
        assert out[at + 3].split() == week and out[at + 5].split() == ["Total", *week[1:]]
        assert out[at + 6 :] == [
            "Of the saving, $0.6140 (100%) comes from sessions with prompts and $0.00 (0.0%)",
            "from sub-agent or background threads with 0 prompts.",
            *LEGEND, "", *CAVEAT,
        ]

    def test_all_shows_the_three_outcomes(self) -> None:
        out = lines(advise("--all"))
        assert out[3:15] == [
            "1. session 7c8d9e0f: you spent $0.76 on gpt-6-astra → gpt-6.1-sol would likely",
            "   have handled this for ~$0.1460 (5.2x cheaper)",
            "   codex, fake-repo, 2026-09-29: score 2/10 (light): files_touched 2 +1,",
            "   active_minutes 10 +1",
            f"2. session a1c7e402: you spent {usd(claude_ucents())} on claude-opus-5-5 (+1 more model): no",
            "   switch advised, heavy: keep",
            "   claude, acme-shop, 2026-09-28: score 7/10 (heavy): input_tokens 3.4M +1,",
            "   tool_calls 46 +1, files_touched 10 +2, error_retries 5 +1, active_minutes 98",
            "   +2",
            "3. session 4a6e8f10: you spent $0.22 on gemini-2.5-pro: no switch advised,",
            "   already cheaper than gpt-6.1-sol",
            "   gemini, demo-notes-app, 2026-09-30: score 1/10 (light): files_touched 2 +1",
        ]
        assert out[15] == "" and out[16] == TITLE
        assert not any("more sessions where" in ln for ln in out)

    def test_the_json_view_of_the_same_run(self) -> None:
        data = json.loads(advise("--all", "--json").stdout)
        assert list(data) == ["period", "filters", "counts", "sessions", "not_listed", "weeks", "total", "saved_split", "warnings", "caveat"]
        assert data["saved_split"] == {"prompted": {"cents": 61, "ucents": 61_400_000}, "background": {"cents": 0, "ucents": 0}}
        assert data["period"] == {"label": "all time", "since": None, "until": None}
        assert data["filters"] == {"agent": None, "project": None}
        assert data["counts"] == {"sessions": 3, "listed": 3, "candidates": 3}
        by_agent = {s["agent"]: s for s in data["sessions"]}
        assert {a: (s["tier"], s["switch"], s["recommended_model"]) for a, s in by_agent.items()} == {
            "claude": ("heavy", False, None),
            "codex": ("light", True, "gpt-6.1-sol"),
            "gemini": ("light", False, None),
        }
        assert by_agent["codex"]["actual"] == {"cents": 76, "ucents": CODEX_UCENTS}
        assert by_agent["codex"]["recommended"] == {"cents": 15, "ucents": CODEX_ROUTED_UCENTS}
        assert by_agent["codex"]["saved"] == {"cents": 61, "ucents": 61_400_000}
        assert by_agent["codex"]["ratio"] == 5.21
        assert by_agent["claude"]["recommended"] is None and by_agent["claude"]["saved"] == {"cents": 0, "ucents": 0}
        actual, routed, saved = self.totals()
        assert data["total"]["actual"]["ucents"] == actual and data["total"]["recommended"]["ucents"] == routed
        assert data["total"]["saved_bp"] == share_bp(saved, actual)
        assert data["warnings"] == [] and data["caveat"].startswith("Each estimate re-prices the same token counts")

    def test_session_keys_are_stable(self) -> None:
        [codex] = json.loads(advise("--json").stdout)["sessions"]
        assert list(codex) == [
            "key", "agent", "session_id", "short_id", "project", "started_at", "day", "prompts", "clipped", "model", "models",
            "tier", "score", "points", "signals", "explanation", "actual_complete", "actual",
            "switch", "recommended_model", "recommended", "saved", "ratio", "note", "other_provider", "line", "other_models",
        ]
        assert list(codex["points"]) == ["input_tokens", "tool_calls", "files_touched", "error_retries", "active_minutes"]
        assert list(codex["signals"]) == list(codex["points"])
        assert (codex["short_id"], codex["day"], codex["models"], codex["prompts"]) == ("7c8d9e0f", "2026-09-29", ["gpt-6-astra"], 1)
        assert (codex["clipped"], codex["other_provider"]) == (False, False)  # openai to openai
        assert codex["line"] == (
            "session 7c8d9e0f: you spent $0.76 on gpt-6-astra → gpt-6.1-sol would likely have handled this for ~$0.1460 (5.2x cheaper)"
        )

    def test_week_and_total_keys_are_stable(self) -> None:
        data = json.loads(advise("--json").stdout)
        keys = ["week", "sessions", "switched", "skipped", "actual", "recommended", "saved", "saved_bp"]
        assert list(data["weeks"][0]) == keys and list(data["total"]) == keys
        assert data["weeks"][0]["week"] == "2026-W40" and data["total"]["week"] == "total"
        for name, group in data["not_listed"].items():
            assert list(group) == ["sessions", "switchable", "actual", "saved", *(["below"] if name == "small" else [])]

    def test_money_is_integer_cents_and_ucents_rounded_half_up(self) -> None:
        data = json.loads(advise("--all", "--json").stdout)
        amounts = [s[k] for s in data["sessions"] for k in ("actual", "recommended", "saved") if s[k] is not None]
        amounts += [data["total"][k] for k in ("actual", "recommended", "saved")]
        assert amounts
        for money in amounts:
            assert list(money) == ["cents", "ucents"] and isinstance(money["cents"], int) and isinstance(money["ucents"], int)
            assert money["cents"] == (money["ucents"] + 500_000) // 1_000_000

    def test_output_is_deterministic(self) -> None:
        assert advise("--all").stdout == advise("--all").stdout
        assert advise("--json").stdout == advise("--json").stdout

    def test_no_prompt_content_no_ansi_and_within_80_columns(self) -> None:
        for args in ((), ("--all",), ("--all", "--json")):
            out = advise(*args).stdout
            assert "PRIVACY-CANARY" not in out
            assert "\x1b" not in out
            assert args[-1:] == ("--json",) or max(len(line) for line in out.splitlines()) <= 80


# ---- filters and arguments --------------------------------------------------------------------------
class TestFilters:
    def test_week_of_and_week(self) -> None:
        assert lines(advise("--week-of", "2026-W40"))[:2] == ["AgentFare advise: week 2026-09-28 .. 2026-10-04", "3 sessions, 1 with a switch advised"]
        assert lines(advise("--week"))[:2] == ["AgentFare advise: week 2026-09-28 .. 2026-10-04", "3 sessions, 1 with a switch advised"]  # now is 2026-09-30
        assert lines(advise("--week-of", "2026-W39")) == [
            "no sessions in this period (week 2026-09-21 .. 2026-09-27)", "data exists from 2026-09-28 to 2026-09-30",  # the hint of render.empty_hints
        ]

    def test_the_week_filter_and_the_weekly_table_use_the_same_calendar_in_any_timezone(self, world: World) -> None:
        """Sunday 23:30 in Toronto is Monday in UTC: the filter and the table row must agree on the ISO week."""
        try:
            zoneinfo.ZoneInfo("America/Toronto")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database")
        sessions = [
            record("sun", "claude", "p", "2026-09-28T03:30:00Z", 1, out_usage("big", 100_000)),  # Sunday 2026-09-27, 23:30 EDT: W39
            record("mon", "claude", "p", "2026-09-28T04:10:00Z", 1, out_usage("big", 100_000)),  # Monday 2026-09-28, 00:10 EDT: W40
        ]
        prefix = world(sessions)
        config = Path(prefix[1]) / "config.yaml"
        config.write_text(config.read_text().replace("timezone: UTC", "timezone: America/Toronto"))
        for week, expected in (("2026-W39", "sun"), ("2026-W40", "mon")):
            data = json.loads(advise("--week-of", week, "--json", prefix=prefix).stdout)
            assert [s["session_id"] for s in data["sessions"]] == [expected]
            assert [(w["week"], w["sessions"]) for w in data["weeks"]] == [(week, 1)]
        assert [w["week"] for w in json.loads(advise("--json", prefix=prefix).stdout)["weeks"]] == ["2026-W39", "2026-W40"]

    def test_since_and_until_are_inclusive_days(self) -> None:
        out = lines(advise("--all", "--since", "2026-09-29", "--until", "2026-09-29"))
        assert out[:2] == ["AgentFare advise: 2026-09-29 .. 2026-09-29", "1 session, 1 with a switch advised"]
        assert out[3].startswith("1. session 7c8d9e0f")

    def test_agent_and_project_filters(self) -> None:
        out = lines(advise("--all", "--agent", "claude"))
        assert out[:2] == ["AgentFare advise: all time, agent claude", "1 session, 0 with a switch advised"]
        out = lines(advise("--all", "--project", "DEMO-notes-APP"))  # case-insensitive
        assert out[:2] == ["AgentFare advise: all time, project DEMO-notes-APP", "1 session, 0 with a switch advised"]
        assert lines(advise("--agent", "gemini", "--project", "acme-shop")) == ["no sessions in this period (all time, project acme-shop, agent gemini)"]

    def test_all_lists_everything_and_overrides_limit(self) -> None:
        out = lines(advise("--all", "--limit", "1"))
        assert [ln[:3] for ln in ranked(out)] == ["1. ", "2. ", "3. "]
        assert not any(ln.startswith("Showing") for ln in out)

    def test_limit_caps_the_default_list_and_says_so(self, world: World) -> None:
        out = lines(advise("--limit", "1", prefix=world()))
        assert [ln[:3] for ln in ranked(out)] == ["1. "]
        assert "Showing 1 of 3; --limit N shows more, --all lists everything." in out
        assert json.loads(advise("--limit", "1", "--json", prefix=world()).stdout)["counts"] == {"sessions": 9, "listed": 1, "candidates": 3}

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("--week", "--week-of", "2026-W40"), "--week and --week-of cannot be combined"),
            (("--week", "--since", "2026-09-01"), "--week and --week-of cannot be combined with --since or --until"),
            (("--week-of", "2026-W40", "--until", "2026-10-01"), "--week and --week-of cannot be combined with --since or --until"),
            (("--week-of", "2026-40"), "invalid ISO week '2026-40'"),
            (("--since", "yesterday"), "invalid date 'yesterday'"),
            (("--agent", "gpt"), "unknown agent 'gpt': expected one of claude, codex, gemini"),
            (("--limit", "0"), "--limit must be at least 1, got 0"),
        ],
    )
    def test_bad_arguments_are_clean_errors(self, args: tuple[str, ...], message: str) -> None:
        result = advise(*args)
        assert result.exit_code == 2
        assert message in result.stderr and result.stderr.startswith("error: ")
        assert "Traceback" not in result.output

    def test_no_sessions_at_all(self, tmp_path: Path) -> None:
        empty = ("--home", str(tmp_path / "empty"), "--no-ingest")
        out = lines(advise(prefix=empty))
        assert out[0] == "no sessions in this period (all time)"
        assert "the database has no sessions at all" in flat(out)
        data = json.loads(advise("--json", prefix=empty).stdout)
        assert data["sessions"] == [] and data["weeks"] == [] and data["counts"] == {"sessions": 0, "listed": 0, "candidates": 0}
        assert (data["total"]["sessions"], data["total"]["actual"], data["total"]["saved_bp"]) == (0, {"cents": 0, "ucents": 0}, None)


# ---- a hand-built world: every kind of session the default view folds ---------------------------------
PRICES = {
    "big": {"input": 10, "cached_input": 1, "output": 50, "cache_write": 12.5},
    "small": {"input": 2, "cached_input": 0.10, "output": 10},
    "tiny": {"input": 1, "output": 5},
    "unver": {"input": "unverified", "output": 5},
}
ROUTER = {
    "signals": {
        "input_tokens": {"medium": 1_000_000, "heavy": 5_000_000},
        "tool_calls": {"medium": 10, "heavy": 50},
        "files_touched": {"medium": 2, "heavy": 6},
        "error_retries": {"medium": 3, "heavy": 9},
        "active_minutes": {"medium": 10, "heavy": 60},
    },
    "tiers": {"light_max": 2, "medium_max": 5},
    "recommend": {"light": "small", "medium": "small", "heavy": None},
}


def record(sid: str, agent: str, project: str, started: str, prompts: int, usage: dict[str, Usage], **extra: Any) -> SessionRecord:
    return SessionRecord(
        agent=agent, session_id=sid, source_path=f"/logs/{sid}.jsonl", project=project, started_at=started,
        ended_at=fmt_iso(parse_iso(started) + timedelta(seconds=600)), wall_seconds=600, active_seconds=0,
        usage=usage, prompts=prompts, **extra,
    )


def out_usage(model: str, tokens: int) -> dict[str, Usage]:
    return {model: Usage(output=tokens)}


WORLD = [
    record("alpha1", "codex", "p1", "2026-09-28T10:00:00Z", 2, out_usage("big", 1_000_000)),  # $50 -> $10
    record("alpha2", "claude", "p2", "2026-09-29T10:00:00Z", 1, out_usage("big", 100_000)),  # $5 -> $1
    record("keep1", "claude", "p2", "2026-09-29T12:00:00Z", 3, out_usage("big", 1_000_000), tool_calls=50, files_touched=6, error_retries=9),
    record("cheap1", "codex", "p1", "2026-09-30T09:00:00Z", 1, out_usage("tiny", 1_000_000)),  # $5, $10 on small
    record("tiny1", "claude", "p2", "2026-09-30T09:30:00Z", 1, out_usage("big", 1_000)),  # $0.05 -> $0.01
    record("bg1", "codex", "p1", "2026-09-30T10:00:00Z", 0, out_usage("big", 500_000)),  # a sub-agent thread: $25 -> $5
    record("bg2", "codex", "p1", "2026-09-30T10:30:00Z", 0, out_usage("tiny", 200_000)),  # $1, $2 on small
    record("unp1", "codex", "p1", "2026-09-30T11:00:00Z", 1, out_usage("unver", 1_000_000)),
    record("w39a", "claude", "p2", "2026-09-22T10:00:00Z", 1, out_usage("big", 200_000)),  # $10 -> $2, the week before
]


@pytest.fixture
def world(tmp_path: Path) -> World:
    """``world(sessions, router=..., prices=...)``: a data directory with these sessions, prices and router
    settings (the WORLD by default); returns the command-line arguments that use it."""

    def build(sessions: list[SessionRecord] | None = None, router: dict[str, Any] | None = None, prices: dict[str, Any] | None = None) -> tuple[str, ...]:
        home = tmp_path / "world"
        home.mkdir(exist_ok=True)
        (home / "pricing.yaml").write_text(yaml.safe_dump({"schema": 1, "models": prices or PRICES}))
        (home / "config.yaml").write_text(yaml.safe_dump({"timezone": "UTC", "router": {**ROUTER, **(router or {})}}))
        with Store(home / "agentfare.db") as store:
            for rec in WORLD if sessions is None else sessions:
                store.upsert_session(rec)
        return ("--home", str(home), "--no-ingest")

    return build


class TestDefaultView:
    def test_lists_the_biggest_savings_first_and_folds_the_rest(self, world: World) -> None:
        out = lines(advise(prefix=world()))
        at = out.index(TITLE)
        assert out[:at] == [
            "AgentFare advise: all time",
            "9 sessions, 5 with a switch advised",
            "",
            "1. session alpha1: you spent $50.00 on big → small would likely have handled",
            "   this for ~$10.00 (5.0x cheaper)",
            "   codex, p1, 2026-09-28: score 0/10 (light): no signal at or above medium",
            "2. session w39a: you spent $10.00 on big → small would likely have handled this",
            "   for ~$2.00 (5.0x cheaper)",
            "   claude, p2, 2026-09-22: score 0/10 (light): no signal at or above medium",
            "3. session alpha2: you spent $5.00 on big → small would likely have handled this",
            "   for ~$1.00 (5.0x cheaper)",
            "   claude, p2, 2026-09-29: score 0/10 (light): no signal at or above medium",
            "2 sub-agent or background sessions with 0 prompts: $26.00 spent, 1 switchable",
            "for $20.00; included in the totals, --all lists them.",
            "1 session under $0.10: $0.05 spent, 1 switchable for $0.04; included in the",
            "totals, --all lists them.",
            "3 more sessions where no switch is advised (heavy, already cheaper, no verified",
            "price): $55.00 spent; --all lists them.",
            "",
        ]
        assert out[at + 1] == "Week      Sess  Switched   Actual  Router   Saved  Saved %"
        assert is_rule(out[at + 2]) and is_rule(out[at + 5]) and len(out[at + 2]) == len(out[at + 1])
        assert out[at + 3 : at + 5] == [
            "2026-W39     1         1   $10.00   $2.00   $8.00    80.0%",
            "2026-W40     7         4  $136.05  $72.01  $64.04    47.1%",
        ]
        assert out[at + 6 : at + 12] == [
            "Total        8         5  $146.05  $74.01  $72.04    49.3%",
            "Of the saving, $52.04 (72.2%) comes from sessions with prompts and $20.00",
            "(27.8%) from sub-agent or background threads with 0 prompts.",
            *LEGEND,
            "1 session with an unverified price left out of these totals and got no advice.",
        ]
        assert "warning: 1 model without a verified price" in flat(out[at + 12 :])  # the wrapping of the warning block is render.py's
        assert "unver: price is unverified, its spend is not counted (see pricing.yaml)" in flat(out[at + 12 :])
        assert out[-3:] == CAVEAT

    def test_all_lists_every_session_switches_first_then_by_spend(self, world: World) -> None:
        out = lines(advise("--all", prefix=world()))
        assert [ln.split(":")[0] for ln in ranked(out)] == [
            "1. session alpha1", "2. session bg1", "3. session w39a", "4. session alpha2", "5. session tiny1",
            "6. session keep1", "7. session cheap1", "8. session bg2", "9. session unp1",
        ]
        text = flat(out)
        assert "6. session keep1: you spent $50.00 on big: no switch advised, heavy: keep" in text
        assert "7. session cheap1: you spent $5.00 on tiny: no switch advised, already cheaper than small" in text
        assert "9. session unp1: you spent an unknown amount on unver: no advice, no verified price" in text
        assert "sub-agent or background sessions" not in text and "more sessions where" not in text  # nothing is folded

    def test_the_folding_rule_follows_the_min_cost_knob(self, world: World) -> None:
        zero = flat(lines(advise(prefix=world(router={"advise": {"min_cost_cents": 0}}))))
        assert "under $" not in zero
        assert "3. session alpha2" in zero and "4. session tiny1" in zero  # the $0.05 session is listed on its own now
        six = flat(lines(advise(prefix=world(router={"advise": {"min_cost_cents": 600}}))))  # $6: alpha2 and cheap1 ($5 each) count as small too
        assert "3 sessions under $6.00: $10.05 spent, 2 switchable for $4.04; included in the totals, --all lists them." in six

    def test_the_default_limit_is_fifteen(self, world: World) -> None:
        many = [record(f"m{i:02d}", "claude", "p", "2026-09-29T10:00:00Z", 1, out_usage("big", 100_000 + i)) for i in range(20)]
        prefix = world(many)
        assert json.loads(advise("--json", prefix=prefix).stdout)["counts"] == {"sessions": 20, "listed": 15, "candidates": 20}
        assert "Showing 15 of 20; --limit N shows more, --all lists everything." in lines(advise(prefix=prefix))
        assert json.loads(advise("--limit", "18", "--json", prefix=prefix).stdout)["counts"]["listed"] == 18
        assert json.loads(advise("--all", "--json", prefix=prefix).stdout)["counts"]["listed"] == 20

    def test_a_narrow_terminal_drops_the_counts_before_it_cuts_a_number(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COLUMNS", "60")
        pricey = [record("dear1", "codex", "p1", "2026-09-28T10:00:00Z", 1, out_usage("big", 600_000_000))]  # $30,000.00 -> $6,000.00
        out = lines(advise(prefix=world(pricey)))
        at = out.index(TITLE)
        table = out[at + 1 : at + 6]
        assert all(len(ln) <= 60 for ln in out) and not any("…" in ln for ln in table)
        assert table[0].split() == ["Week", "Switched", "Actual", "Router", "Saved", "Saved", "%"]
        assert table[4].split() == ["Total", "1", "$30,000.00", "$6,000.00", "$24,000.00", "80.0%"]
        assert "(left out to fit 60 columns: Sess; --json has everything)" in out

    def test_rank_numbers_stay_aligned_past_nine(self, world: World) -> None:
        many = [record(f"m{i:02d}", "claude", "p", "2026-09-29T10:00:00Z", 1, out_usage("big", 100_000 + i)) for i in range(12)]
        out = lines(advise(prefix=world(many)))
        first = ranked(out)
        assert first[0].startswith(" 1. session m11") and first[9].startswith("10. session ")
        assert out[out.index(first[0]) + 1].startswith("    ")  # the hanging indent is as wide as the two-digit rank

    def test_equal_savings_are_ordered_by_spend_then_key(self, world: World) -> None:
        twins = [record(k, "claude", "p", "2026-09-29T10:00:00Z", 1, out_usage("big", 100_000)) for k in ("zz", "aa", "mm")]
        data = json.loads(advise("--json", prefix=world(twins)).stdout)
        assert [s["session_id"] for s in data["sessions"]] == ["aa", "mm", "zz"]

    def test_a_view_with_no_switch_says_so(self, world: World) -> None:
        out = lines(advise(prefix=world([WORLD[2]])))  # only the heavy session
        assert out[:4] == ["AgentFare advise: all time", "1 session, 0 with a switch advised", "", "No session with an advised switch in this view."]
        assert "1 more session where no switch is advised (heavy, already cheaper, no verified price): $50.00 spent; --all lists them." in flat(out)


class TestWorldJson:
    def test_groups_and_totals(self, world: World) -> None:
        data = json.loads(advise("--json", prefix=world()).stdout)
        assert data["counts"] == {"sessions": 9, "listed": 3, "candidates": 3}

        def money(dollars: int, cents: int = 0) -> dict[str, int]:
            return {"cents": dollars * 100 + cents, "ucents": (dollars * 100 + cents) * 1_000_000}

        assert data["not_listed"] == {
            "background": {"sessions": 2, "switchable": 1, "actual": money(26), "saved": money(20)},
            "small": {"sessions": 1, "switchable": 1, "actual": money(0, 5), "saved": money(0, 4), "below": money(0, 10)},
            "no_switch": {"sessions": 3, "switchable": 0, "actual": money(55), "saved": money(0)},
        }
        assert [(w["week"], w["sessions"], w["switched"], w["skipped"]) for w in data["weeks"]] == [("2026-W39", 1, 1, 0), ("2026-W40", 7, 4, 1)]
        assert [w["saved_bp"] for w in data["weeks"]] == [8000, 4707]
        assert data["total"] == {
            "week": "total", "sessions": 8, "switched": 5, "skipped": 1,
            "actual": money(146, 5), "recommended": money(74, 1), "saved": money(72, 4), "saved_bp": 4933,
        }

    def test_the_groups_and_the_list_cover_every_session_exactly_once(self, world: World) -> None:
        data = json.loads(advise("--json", "--limit", "1000", prefix=world()).stdout)
        folded = sum(g["sessions"] for g in data["not_listed"].values())
        assert data["counts"]["listed"] + folded == data["counts"]["sessions"] == 9

    def test_an_unpriced_session_has_no_recommendation_in_the_json(self, world: World) -> None:
        data = json.loads(advise("--all", "--json", prefix=world()).stdout)
        [unp] = [s for s in data["sessions"] if s["session_id"] == "unp1"]
        assert (unp["actual_complete"], unp["switch"], unp["recommended_model"], unp["recommended"], unp["ratio"]) == (False, False, None, None, None)
        assert (unp["note"], unp["saved"]["ucents"]) == ("no verified price", 0)


class TestUnverifiedPrices:
    def test_warnings_name_the_models_in_text_and_json(self, world: World) -> None:
        text = flat(lines(advise(prefix=world())))
        assert "warning: 1 model without a verified price, 1.0M tokens are not counted in any dollar figure" in text
        assert "unver: price is unverified, its spend is not counted (see pricing.yaml)" in text
        assert json.loads(advise("--json", prefix=world()).stdout)["warnings"] == [{
            "model": "unver", "status": "unverified", "excluded_tokens": 1_000_000,
            "message": "unver: price is unverified, its spend is not counted (see pricing.yaml)",
        }]

    def test_a_model_missing_from_the_table_is_named_too(self, world: World) -> None:
        sessions = [*WORLD[:1], record("ghost", "codex", "p1", "2026-09-30T11:00:00Z", 1, out_usage("mystery-9", 5))]
        out = lines(advise(prefix=world(sessions)))
        assert "mystery-9: not in the price table, its spend is not counted (add it to pricing.yaml)" in flat(out)
        assert "1 session with an unverified price left out of these totals and got no advice." in out

    def test_a_recommended_model_without_a_price_is_named_and_gives_no_advice(self, world: World) -> None:
        out = lines(advise("--all", prefix=world([WORLD[0]], router={"recommend": {"light": "unver", "medium": "unver", "heavy": None}})))
        assert "unver: price is unverified, its spend is not counted (see pricing.yaml)" in flat(out)
        assert "1. session alpha1: you spent $50.00 on big: no switch advised, unver has no verified price" in flat(out)

    def test_no_warning_when_every_price_is_verified(self, world: World) -> None:
        text = flat(lines(advise(prefix=world(WORLD[:5]))))
        assert "warning" not in text and "unverified price" not in text
        assert json.loads(advise("--json", prefix=world(WORLD[:5])).stdout)["warnings"] == []

    def test_a_partial_price_warning_is_shown(self, world: World) -> None:
        prices = {**PRICES, "small": {"input": 2, "cached_input": "unverified", "output": 10}}
        out = lines(advise(prefix=world(WORLD[:1], prices=prices)))
        assert "small: cached-input price is unverified, cache reads are billed at the input rate" in flat(out)

    def test_a_partly_verified_recommended_model_has_the_partial_status_in_json(self, world: World) -> None:
        """The same word the other commands use for a model that is priced but has an unverified optional price."""
        prices = {**PRICES, "small": {"input": 2, "cached_input": "unverified", "output": 10}}
        warnings = json.loads(advise("--json", prefix=world(WORLD[:1], prices=prices)).stdout)["warnings"]
        assert [(w["model"], w["status"], w["excluded_tokens"]) for w in warnings] == [("small", "partial", 0)]
        prices = {**PRICES, "small": {"input": "unverified", "output": 10}}
        warnings = json.loads(advise("--json", prefix=world(WORLD[:1], prices=prices)).stdout)["warnings"]
        assert [(w["model"], w["status"], w["excluded_tokens"]) for w in warnings] == [("small", "unverified", 0)]


class TestConfigInteraction:
    def test_a_bad_router_setting_is_a_clean_error(self, world: World) -> None:
        result = advise(prefix=world(router={"tiers": {"light_max": 5, "medium_max": 1}}))
        assert result.exit_code == 2
        assert "error: router.tiers: light_max (5) must not be above medium_max (1)" in result.stderr
        assert "Traceback" not in result.output

    def test_the_recommendation_follows_the_config(self, world: World) -> None:
        router = {"recommend": {"light": "tiny", "medium": "tiny", "heavy": "small"}}
        text = flat(lines(advise("--all", prefix=world(WORLD[:3], router=router))))
        assert "1. session alpha1: you spent $50.00 on big → tiny would likely have handled this for ~$5.00 (10.0x cheaper)" in text
        assert "session keep1: you spent $50.00 on big → small would likely have handled this for ~$10.00 (5.0x cheaper)" in text

    def test_the_minimum_ratio_follows_the_config(self, world: World) -> None:
        text = flat(lines(advise("--all", prefix=world(WORLD[:1], router={"min_saving_ratio": 5.5}))))
        assert "1. session alpha1: you spent $50.00 on big: no switch advised, small is only 5.0x cheaper (minimum 5.5x)" in text
        text = flat(lines(advise("--all", prefix=world(WORLD[:1], router={"min_saving_ratio": 5}))))
        assert "1. session alpha1: you spent $50.00 on big → small would likely" in text


# ---- money is attributed to when it was spent -----------------------------------------------------------
SUNDAY, MONDAY = "2026-09-27T23:45:00Z", "2026-09-28T00:00:00Z"  # the last slot of ISO week 2026-W39 and the first of W40, in UTC


def span_record(sid: str = "span", prompts: int = 2) -> SessionRecord:
    """One session that began on Sunday night and went on after midnight: 1M output tokens on big in each week."""
    return record(
        sid, "claude", "p1", "2026-09-27T23:30:00Z", prompts, out_usage("big", 2_000_000),
        slots={(SUNDAY, "big"): Usage(output=1_000_000), (MONDAY, "big"): Usage(output=1_000_000)},
    )


class TestSessionsThatSpanPeriods:
    def test_each_week_gets_what_was_spent_in_it_and_the_total_counts_the_session_once(self, world: World) -> None:
        out = lines(advise(prefix=world([span_record()])))
        at = out.index(TITLE)
        assert out[at + 3 : at + 5] == [
            "2026-W39     1         1   $50.00  $10.00  $40.00    80.0%",
            "2026-W40     1         1   $50.00  $10.00  $40.00    80.0%",
        ]
        assert out[at + 6] == "Total        1         1  $100.00  $20.00  $80.00    80.0%"
        assert out[1] == "1 session, 1 with a switch advised"
        assert "1. session span: you spent $100.00 on big → small would likely have handled this" in flat(out)

    @pytest.mark.parametrize(("week", "weeks_label"), [("2026-W39", "2026-09-21 .. 2026-09-27"), ("2026-W40", "2026-09-28 .. 2026-10-04")])
    def test_a_period_shows_only_the_money_spent_inside_it(self, world: World, week: str, weeks_label: str) -> None:
        prefix = world([span_record()])
        out = lines(advise("--week-of", week, prefix=prefix))
        assert out[0] == f"AgentFare advise: week {weeks_label}" and out[1] == "1 session, 1 with a switch advised"
        text = flat(out)
        assert "1. session span: you spent $50.00 on big → small would likely have handled this for ~$10.00 (5.0x cheaper)" in text
        assert "claude, p1, 2026-09-27 (the part inside this period):" in text  # the session began on the 27th, in either period
        at = out.index(TITLE)
        assert out[at + 3].split() == [week, "1", "1", "$50.00", "$10.00", "$40.00", "80.0%"]
        assert out[at + 5].split() == ["Total", "1", "1", "$50.00", "$10.00", "$40.00", "80.0%"]

    def test_the_json_of_a_period_is_clipped_and_the_two_periods_add_up_to_the_whole(self, world: World) -> None:
        prefix = world([span_record(), record("solo", "codex", "p2", "2026-09-28T09:00:00Z", 1, out_usage("big", 500_000))])
        whole = json.loads(advise("--json", prefix=prefix).stdout)
        first, second = (json.loads(advise("--week-of", w, "--json", prefix=prefix).stdout) for w in ("2026-W39", "2026-W40"))
        [span] = first["sessions"]
        assert (span["key"], span["clipped"], span["actual"]["ucents"], span["tier"]) == ("claude:span", True, 5_000_000_000, "light")
        assert [s["clipped"] for s in whole["sessions"]] == [False, False]
        assert span["prompts"] == 2 and span["started_at"] == "2026-09-27T23:30:00Z"  # everything but the money stays whole-session
        for key in ("actual", "recommended", "saved"):
            assert whole["total"][key]["ucents"] == first["total"][key]["ucents"] + second["total"][key]["ucents"]
        assert [w["week"] for w in whole["weeks"]] == ["2026-W39", "2026-W40"]
        assert whole["total"]["sessions"] == 2 and first["total"]["sessions"] == 1 and second["total"]["sessions"] == 2

    def test_the_weekly_rows_of_an_all_time_view_are_the_rows_of_each_week_and_of_the_report(self, world: World) -> None:
        """A light session that ran on ``big`` ($50) in week 1 and on ``tiny`` ($5, cheaper than the $10 ``small`` costs) in week 2 is
        moved as a whole: the all-time table and the view of each week say the same, and so does the report. Week 2 used to read
        Router = Actual in its own view and Router = $10 in the all-time one."""
        span = record(
            "span", "claude", "p1", "2026-09-27T23:30:00Z", 2, {"big": Usage(output=1_000_000), "tiny": Usage(output=1_000_000)},
            slots={(SUNDAY, "big"): Usage(output=1_000_000), (MONDAY, "tiny"): Usage(output=1_000_000)},
        )
        prefix = world([span])
        whole = json.loads(advise("--json", prefix=prefix).stdout)
        assert [(w["week"], w["actual"]["ucents"], w["recommended"]["ucents"], w["saved"]["ucents"]) for w in whole["weeks"]] == [
            ("2026-W39", 5_000_000_000, 1_000_000_000, 4_000_000_000), ("2026-W40", 500_000_000, 1_000_000_000, -500_000_000),
        ]
        for week, row in zip(("2026-W39", "2026-W40"), whole["weeks"]):
            single = json.loads(advise("--week-of", week, "--json", prefix=prefix).stdout)
            assert single["weeks"] == [row] and single["total"] == {**row, "week": "total"}
            report = json.loads(CliRunner().invoke(app, [*prefix, "report", "--week-of", week, "--json"]).stdout)
            assert report["router"] == row
        assert whole["total"]["saved"]["ucents"] == 3_500_000_000  # $55 -> $20
        text = flat(lines(advise("--week-of", "2026-W40", prefix=prefix)))
        assert "you spent $5.00 on tiny → small would likely have handled this for ~$10.00 (not cheaper for the part inside this period)" in text

    def test_a_week_is_named_after_the_model_it_used_not_after_the_model_of_the_whole_session(self, world: World) -> None:
        """``tiny`` wrote the most over the whole session (2M output tokens) but only ``big`` ran in week 2: the week says ``big``."""
        span = record(
            "span", "claude", "p1", "2026-09-27T23:30:00Z", 2, {"tiny": Usage(output=2_000_000), "big": Usage(output=1_000_000)},
            slots={(SUNDAY, "tiny"): Usage(output=2_000_000), (MONDAY, "big"): Usage(output=1_000_000)},
        )
        prefix = world([span])
        week_2 = advise("--week-of", "2026-W40", prefix=prefix)
        assert "1. session span: you spent $50.00 on big →" in flat(lines(week_2))
        assert "tiny" not in week_2.stdout.split("Router savings")[0] and "more model" not in week_2.stdout
        assert "on tiny (+1 more model) →" in flat(lines(advise(prefix=prefix)))
        assert (json.loads(advise("--week-of", "2026-W40", "--json", prefix=prefix).stdout)["sessions"][0]["model"],
                json.loads(advise("--json", prefix=prefix).stdout)["sessions"][0]["model"]) == ("big", "tiny")

    def test_the_input_tokens_of_a_huge_session_use_the_units_of_the_other_views(self, world: World) -> None:
        """2,891,300,000 input tokens are ``2.9B`` everywhere (``summary`` prints ``2.9B``), not ``2891.3M`` in the score line."""
        huge = record("huge", "claude", "p1", "2026-09-28T10:00:00Z", 1, {"big": Usage(input=2_891_300_000)})
        text = flat(lines(advise("--all", prefix=world([huge]))))
        assert "input_tokens 2.9B +2" in text and "M +2" not in text
        assert json.loads(advise("--all", "--json", prefix=world([huge])).stdout)["sessions"][0]["explanation"].count("2.9B") == 1

    def test_the_tier_belongs_to_the_whole_session_not_to_the_period_asked_for(self, world: World) -> None:
        """2M input tokens in all (a medium signal) but 1M inside either week: clipped alone it would read light and switch."""
        long = record(
            "long", "claude", "p1", "2026-09-27T23:30:00Z", 1, {"big": Usage(input=2_000_000)}, tool_calls=10, files_touched=2,
            slots={(SUNDAY, "big"): Usage(input=1_000_000), (MONDAY, "big"): Usage(input=1_000_000)},
        )
        router = {"recommend": {"light": "small", "medium": None, "heavy": None}}
        prefix = world([long], router=router)
        for args in ((), ("--week-of", "2026-W39"), ("--week-of", "2026-W40")):
            [entry] = json.loads(advise("--all", "--json", *args, prefix=prefix).stdout)["sessions"]
            assert (entry["tier"], entry["score"], entry["switch"], entry["note"]) == ("medium", 3, False, "medium: keep")
            assert entry["signals"]["input_tokens"] == 2_000_000

    def test_a_session_that_is_light_whole_is_light_in_each_part(self, world: World) -> None:
        router = {"recommend": {"light": "small", "medium": None, "heavy": None}}
        light = record("light", "claude", "p1", "2026-09-27T23:30:00Z", 1, {"big": Usage(input=200_000)}, slots={(SUNDAY, "big"): Usage(input=100_000), (MONDAY, "big"): Usage(input=100_000)})
        [entry] = json.loads(advise("--json", "--week-of", "2026-W40", prefix=world([light], router=router)).stdout)["sessions"]
        assert (entry["tier"], entry["switch"], entry["signals"]["input_tokens"]) == ("light", True, 200_000)

    def test_the_weeks_follow_the_timezone_of_the_config(self, world: World, tmp_path: Path) -> None:
        try:
            zoneinfo.ZoneInfo("Asia/Tokyo")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database")
        tokyo = record(
            "tokyo", "claude", "p1", "2026-09-27T14:30:00Z", 2, out_usage("big", 2_000_000),
            slots={("2026-09-27T14:45:00Z", "big"): Usage(output=1_000_000), ("2026-09-27T15:00:00Z", "big"): Usage(output=1_000_000)},  # 23:45 and 00:00 in Tokyo
        )
        prefix = world([tokyo])
        assert [w["week"] for w in json.loads(advise("--json", prefix=prefix).stdout)["weeks"]] == ["2026-W39"]
        home = Path(prefix[1])
        (home / "config.yaml").write_text(yaml.safe_dump({"timezone": "Asia/Tokyo", "router": {**ROUTER}}))
        assert [w["week"] for w in json.loads(advise("--json", prefix=prefix).stdout)["weeks"]] == ["2026-W39", "2026-W40"]


# ---- the arrow, the provider note, one precision per row, the split ----------------------------------------
def cli_process(*args: str, encoding: str, home: Path, config: Path = FIXTURE_CONFIG) -> subprocess.CompletedProcess[str]:
    """The real command line in its own process, with stdout in ``encoding``: what a pipe or an old terminal gives it."""
    env = {**os.environ, "PYTHONIOENCODING": encoding, "COLUMNS": "80", "AGENTFARE_HOME": str(home), "AGENTFARE_NOW": "2026-09-30T12:00:00Z"}
    entry = "from agentfare.cli import _entry; _entry()"
    return subprocess.run([sys.executable, "-c", entry, "--config", str(config), *args], capture_output=True, text=True, env=env, check=False)


class TestLegacyConsoles:
    def test_the_arrow_becomes_an_ascii_one_when_stdout_cannot_write_it(self, tmp_path: Path) -> None:
        result = cli_process("advise", encoding="ascii", home=tmp_path / "home")
        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr and "→" not in result.stdout
        assert "1. session 7c8d9e0f: you spent $0.76 on gpt-6-astra -> gpt-6.1-sol would likely" in result.stdout

    def test_a_utf8_console_keeps_the_arrow(self, tmp_path: Path) -> None:
        result = cli_process("advise", encoding="utf-8", home=tmp_path / "home")
        assert result.returncode == 0 and "on gpt-6-astra → gpt-6.1-sol would likely" in result.stdout

    def test_the_json_is_the_same_on_any_console(self, tmp_path: Path) -> None:
        ascii_out = cli_process("advise", "--all", "--json", encoding="ascii", home=tmp_path / "a")
        utf8_out = cli_process("advise", "--all", "--json", encoding="utf-8", home=tmp_path / "b")
        assert ascii_out.returncode == 0 and ascii_out.stdout == utf8_out.stdout
        assert "→" in json.loads(ascii_out.stdout)["sessions"][0]["line"]

    def test_a_project_name_the_console_cannot_write_is_shown_with_a_question_mark(self, world: World, tmp_path: Path) -> None:
        prefix = world([record("cafe", "claude", "café-日本", "2026-09-29T10:00:00Z", 1, out_usage("big", 1_000_000))])
        result = cli_process(*prefix[2:], "--home", prefix[1], "advise", encoding="ascii", home=Path(prefix[1]), config=Path(prefix[1]) / "config.yaml")
        assert result.returncode == 0, result.stderr
        assert "claude, caf?-??, 2026-09-29: score 0/10" in result.stdout and "Traceback" not in result.stderr


OPENAI, ANTHROPIC = {"provider": "openai"}, {"provider": "anthropic"}
PROVIDER_PRICES = {
    "big": {**PRICES["big"], **ANTHROPIC}, "small": {**PRICES["small"], **OPENAI}, "tiny": {**PRICES["tiny"], **ANTHROPIC},
    "mid": {**PRICES["small"], **ANTHROPIC}, "loose": PRICES["small"],
}
PROVIDER_NOTE = "gpt-ish is another provider's model: a price comparison, not something this agent can run"


class TestOtherProvider:
    def prefix(self, world: World, model: str, target: str = "small") -> tuple[str, ...]:
        recommend = {"light": target, "medium": target, "heavy": None}
        return world([record("s1", "claude", "p1", "2026-09-29T10:00:00Z", 1, out_usage(model, 1_000_000))], router={"recommend": recommend}, prices=PROVIDER_PRICES)

    def test_a_model_of_another_provider_says_it_is_a_price_comparison(self, world: World) -> None:
        out = lines(advise(prefix=self.prefix(world, "big")))
        text = flat(out)
        assert "claude, p1, 2026-09-29: score 0/10 (light): no signal at or above medium. small is another provider's model: a price comparison, not something this agent can run" in text
        assert "1. session s1: you spent $50.00 on big → small would likely have handled this for ~$10.00 (5.0x cheaper)" in text  # the signature line keeps its documented format
        entry = json.loads(advise("--json", prefix=self.prefix(world, "big")).stdout)["sessions"][0]
        assert entry["other_provider"] is True and entry["line"].endswith("would likely have handled this for ~$10.00 (5.0x cheaper)")

    def test_the_same_provider_has_no_note(self, world: World) -> None:
        prefix = self.prefix(world, "big", "mid")
        assert "another provider" not in flat(lines(advise(prefix=prefix)))
        assert json.loads(advise("--json", prefix=prefix).stdout)["sessions"][0]["other_provider"] is False

    def test_a_provider_the_table_does_not_say_is_no_claim(self, world: World) -> None:
        assert "another provider" not in flat(lines(advise(prefix=self.prefix(world, "big", "loose"))))  # the recommended model has no provider
        assert "another provider" not in flat(lines(advise(prefix=self.prefix(world, "loose", "small"))))  # the session's model has none

    def test_a_session_that_also_used_the_advised_providers_model_is_not_another_provider(self, world: World) -> None:
        both = record("s1", "claude", "p1", "2026-09-29T10:00:00Z", 1, {"big": Usage(output=500_000), "small": Usage(output=500_000)})
        prefix = world([both], router={"recommend": {"light": "small", "medium": "small", "heavy": None}}, prices=PROVIDER_PRICES)
        assert json.loads(advise("--json", prefix=prefix).stdout)["sessions"][0]["other_provider"] is False

    def test_a_kept_session_has_no_note(self, world: World) -> None:
        prefix = world([record("s1", "claude", "p1", "2026-09-29T10:00:00Z", 1, out_usage("big", 1_000_000), tool_calls=50, files_touched=6, error_retries=9)], prices=PROVIDER_PRICES)
        assert "another provider" not in flat(lines(advise("--all", prefix=prefix)))


class TestWeeklyTablePrecision:
    """One precision per row: four decimals on Actual, Router and Saved when any of them is not a whole cent."""

    PRICES = {"a": {"input": 1, "output": 100}, "b": {"input": 1, "output": "12.345"}, "c": {"input": 1, "output": 10}}

    def rows(self, world: World, target: str, *sessions: SessionRecord) -> list[list[str]]:
        router = {"recommend": {"light": target, "medium": target, "heavy": None}}
        out = lines(advise(prefix=world(list(sessions), router=router, prices=self.PRICES)))
        at = out.index(TITLE)
        return [ln.split()[3:6] for ln in out[at + 1 :] if ln and not is_rule(ln) and ln.split()[0] not in ("Week", "Of", "from", "Sess", "same")][:len(sessions) + 1]

    def test_a_row_with_a_part_of_a_cent_shows_all_three_amounts_with_four_decimals(self, world: World) -> None:
        # $1.00 actual is a whole cent, but $0.12345 on b is not: the row reads $1.0000, $0.1235 and $0.8766
        [row, total] = self.rows(world, "b", record("s1", "claude", "p", "2026-09-29T10:00:00Z", 1, out_usage("a", 10_000)))
        assert row == total == ["$1.0000", "$0.1235", "$0.8766"]

    def test_a_row_of_whole_cents_keeps_the_compact_form(self, world: World) -> None:
        [row, total] = self.rows(world, "c", record("s1", "claude", "p", "2026-09-29T10:00:00Z", 1, out_usage("a", 1_000_000)))
        assert row == total == ["$100.00", "$10.00", "$90.00"]

    def test_each_row_decides_for_itself(self, world: World) -> None:
        kept = record("w39", "claude", "p", "2026-09-22T10:00:00Z", 1, out_usage("c", 1_000_000))  # already on the advised model: $10.00
        odd = record("w41", "claude", "p", "2026-10-06T10:00:00Z", 1, out_usage("a", 10_001))  # $1.0001 -> $0.10001
        w39, w41, total = self.rows(world, "c", kept, odd)
        assert w39 == ["$10.00", "$10.00", "$0.00"]  # whole cents: compact
        assert w41 == ["$1.0001", "$0.1000", "$0.9001"] and total == ["$11.0001", "$10.1000", "$0.9001"]

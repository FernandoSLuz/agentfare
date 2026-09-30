"""End-to-end tests of the slot contract: money belongs to when it was spent, not to the day a session started.

The real CLI, in a subprocess, on a copy of the Codex fixture that keeps running after the fixture ends: the same thread gets
more usage records days later. Every number is worked out by hand in the comments (tokens x USD per 1M tokens), not computed by
the product: a session that spans a day or a week must split its money between them, every command must agree on each
period, and a period that holds only part of a session must show only that part.

Prices (src/agentfare/data/pricing.yaml, all verified on 2026-09-30), USD per 1M tokens:
    gpt-6-astra   input 10.00  cached 1.00  output 50.00    (what the fixture thread ran on)
    gpt-6.1-sol   input  2.00  cached 0.10  output 10.00    (what advise compares light and medium sessions with)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_e2e_fixtures import FIXTURES, cli, fresh_home, write_config

THREAD, TURN = "01a0d8b0-1a2b-7c3d-8e4f-5a6b7c8d9e0f", "01a0d8b0-1c00-7000-8000-000000000001"

# The fixture thread alone (Tue 2026-09-29 10:00 to 10:10 UTC): 40,000 uncached + 60,000 cached + 6,000 output on gpt-6-astra
#   40,000 x 10 + 60,000 x 1 + 6,000 x 50 = 400,000 + 60,000 + 300,000 = 760,000 -> $0.76 = 76,000,000 ucents (1 USD = 100,000,000 ucents)
# re-priced on gpt-6.1-sol: 40,000 x 2 + 60,000 x 0.10 + 6,000 x 10 = 80,000 + 6,000 + 60,000 = 146,000 -> $0.146 = 14,600,000 ucents
FIXTURE_UCENTS, FIXTURE_ON_SOL = 76_000_000, 14_600_000


def usage_record(ordinal: int, when: str, *, uncached: int, output: int = 0) -> dict[str, Any]:
    """A `token_usage_record` of the fixture thread: one more response on gpt-6-astra, ``uncached`` input tokens and ``output``."""
    usage = {
        "input_tokens": uncached, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": output,
        "reasoning_output_tokens": 0, "total_tokens": uncached + output,
    }  # fmt: skip
    return {
        "timestamp": when, "ordinal": ordinal, "type": "token_usage_record",
        "payload": {"thread_id": THREAD, "turn_id": TURN, "session_id": THREAD, "root_turn_id": TURN, "response_id": f"resp_slots_{ordinal}", "usage": usage},
    }  # fmt: skip


def codex_thread(tmp_path: Path, *records: dict[str, Any], **settings: Any) -> tuple[Path, Path]:
    """(config, home): the Codex fixture copied into ``tmp_path`` with ``records`` appended, as the only source of logs."""
    (source,) = (FIXTURES / "codex" / "sessions").rglob("rollout-*.jsonl")
    folder = tmp_path / "codex" / "sessions" / "2026" / "09" / "29"
    folder.mkdir(parents=True)
    log = folder / source.name
    log.write_text(source.read_text(encoding="utf-8") + "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    config = write_config(tmp_path / "config.yaml", sources={"claude": [], "gemini": [], "codex": [str(tmp_path / "codex" / "sessions")]}, **settings)
    return config, fresh_home(tmp_path)


def by(rows: list[dict[str, Any]], key: str) -> list[tuple[str, int]]:
    return [(row[key], row["cost"]["ucents"]) for row in rows]


# ---------------------------------------------------------------------------------------------------------------------
# A thread that goes on for a week: Tue 2026-09-29 (ISO week 2026-W40) to Tue 2026-10-06 (2026-W41), UTC
# ---------------------------------------------------------------------------------------------------------------------
# One more response at 2026-10-06T10:20:00Z: 2,000,000 uncached input tokens and 100,000 output tokens on gpt-6-astra
#   2,000,000 x 10 + 100,000 x 50 = 20,000,000 + 5,000,000 = 25,000,000 -> $25.00 = 2,500,000,000 ucents
# whole session: 76,000,000 + 2,500,000,000 = 2,576,000,000 ucents = $25.76
LATE_UCENTS = 2_500_000_000
WHOLE_UCENTS = FIXTURE_UCENTS + LATE_UCENTS
# re-priced on gpt-6.1-sol: the late response is 2,000,000 x 2 + 100,000 x 10 = 4,000,000 + 1,000,000 = 5,000,000 -> $5.00 = 500,000,000 ucents
LATE_ON_SOL = 500_000_000
NOW_W41 = "2026-10-07T12:00:00Z"  # a Wednesday of week 2026-W41 (Mon 2026-10-05 .. Sun 2026-10-11)


@pytest.fixture(scope="module")
def week_long(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp_path = tmp_path_factory.mktemp("week-long")
    config, home = codex_thread(tmp_path, usage_record(50, "2026-10-06T10:20:00.000Z", uncached=2_000_000, output=100_000))
    cli(home, "ingest", config=config, now=NOW_W41)
    return config, home


def run(week_long: tuple[Path, Path], *args: str, now: str = NOW_W41) -> Any:
    config, home = week_long
    return cli(home, "--no-ingest", *args, config=config, now=now)


class TestAThreadThatSpansTwoWeeks:
    def test_the_money_is_split_by_the_day_and_the_week_it_was_spent(self, week_long: tuple[Path, Path]) -> None:
        data = run(week_long, "summary", "--json").json()
        assert data["sessions"] == 1 and data["total"]["ucents"] == WHOLE_UCENTS  # one session, its whole cost
        assert by(data["by_day"], "day") == [("2026-09-29", FIXTURE_UCENTS), ("2026-10-06", LATE_UCENTS)]  # $0.76 and $25.00
        assert by(data["by_week"], "week") == [("2026-W40", FIXTURE_UCENTS), ("2026-W41", LATE_UCENTS)]
        assert [(r["day"], r["sessions"]) for r in data["by_day"]] == [("2026-09-29", 1), ("2026-10-06", 1)]  # in both, and once in the total
        assert by(data["by_model"], "model") == [("gpt-6-astra", WHOLE_UCENTS)]

    def test_the_text_says_so(self, week_long: tuple[Path, Path]) -> None:
        out = run(week_long, "summary").stdout
        assert "total spend $25.76" in out
        assert "a session that spans several days counts once per day; the Total row counts it once" in out
        assert "a session that spans several weeks counts once per week; the Total row counts it once" in out

    def test_the_current_week_shows_only_what_was_spent_inside_it(self, week_long: tuple[Path, Path]) -> None:
        data = run(week_long, "summary", "--week", "--json").json()
        assert data["period"] == {"label": "week 2026-10-05 .. 2026-10-11", "since": "2026-10-05T00:00:00Z", "until": "2026-10-12T00:00:00Z"}
        assert (data["sessions"], data["total"]["ucents"]) == (1, LATE_UCENTS)  # $25.00
        assert by(data["by_day"], "day") == [("2026-10-06", LATE_UCENTS)]
        assert data["tokens"]["input"] == 2_000_000 and data["tokens"]["output"] == 100_000 and data["tokens"]["cache_read"] == 0  # the late response only
        assert "spent inside the period only" in run(week_long, "summary", "--week").stdout

    def test_the_week_before_shows_the_rest(self, week_long: tuple[Path, Path]) -> None:
        data = run(week_long, "summary", "--week-of", "2026-W40", "--json").json()
        assert (data["sessions"], data["total"]["ucents"]) == (1, FIXTURE_UCENTS)  # $0.76
        assert data["tokens"]["input"] == 40_000 and data["tokens"]["cache_read"] == 60_000 and data["tokens"]["output"] == 6_000

    def test_a_week_with_nothing_in_it_is_empty_however_long_the_session(self, week_long: tuple[Path, Path]) -> None:
        data = run(week_long, "summary", "--week-of", "2026-W39", "--json").json()
        assert (data["sessions"], data["total"]["ucents"]) == (0, 0)
        assert run(week_long, "summary", "--week-of", "2026-W42", "--json").json()["sessions"] == 0

    def test_since_and_until_clip_the_same_way(self, week_long: tuple[Path, Path]) -> None:
        assert run(week_long, "summary", "--until", "2026-10-05", "--json").json()["total"]["ucents"] == FIXTURE_UCENTS
        assert run(week_long, "summary", "--since", "2026-10-06", "--json").json()["total"]["ucents"] == LATE_UCENTS
        assert run(week_long, "summary", "--since", "2026-09-30", "--until", "2026-10-05", "--json").json()["sessions"] == 0

    def test_sessions_marks_the_clipped_session_and_keeps_its_counters_whole(self, week_long: tuple[Path, Path]) -> None:
        week = run(week_long, "sessions", "--week", "--json").json()
        (session,) = week["sessions"]
        assert session["clipped"] is True and session["cost"]["ucents"] == LATE_UCENTS
        assert session["date"] == "2026-10-06"  # the first day of spend inside the view, not the day the session started
        assert session["started_at"] == "2026-09-29T10:00:00Z"  # whole-session fields stay whole-session
        assert (session["prompts"], session["tool_calls"], session["files_touched"], session["error_retries"], session["active_seconds"]) == (1, 5, 2, 1, 600)
        text = run(week_long, "sessions", "--week").stdout
        assert "$25.00*" in text and "* sessions that span the period edge show only what was spent inside it" in text
        whole = run(week_long, "sessions", "--json").json()["sessions"][0]
        assert whole["clipped"] is False and whole["cost"]["ucents"] == WHOLE_UCENTS and whole["date"] == "2026-09-29"

    def test_models_and_sessions_agree_with_summary_for_every_period(self, week_long: tuple[Path, Path]) -> None:
        for period, expected in (([], WHOLE_UCENTS), (["--week"], LATE_UCENTS), (["--week-of", "2026-W40"], FIXTURE_UCENTS)):
            totals = {name: run(week_long, name, *period, "--json").json()["total"]["ucents"] for name in ("summary", "sessions", "models")}
            assert totals == dict.fromkeys(("summary", "sessions", "models"), expected), period

    def test_advise_splits_the_weeks_and_scores_the_whole_session(self, week_long: tuple[Path, Path]) -> None:
        data = run(week_long, "advise", "--json").json()
        # Whole-session score: input 40,000 + 60,000 + 2,000,000 = 2,100,000 >= 2,000,000 (+1), 5 tool calls (0), 2 files (+1),
        # 1 error (0), 10 active minutes (+1; the week-long pause is not active time): 3 of 10, medium, compared with gpt-6.1-sol.
        (session,) = data["sessions"]
        assert (session["score"], session["tier"], session["switch"], session["recommended_model"]) == (3, "medium", True, "gpt-6.1-sol")
        assert session["actual"]["ucents"] == WHOLE_UCENTS and session["recommended"]["ucents"] == FIXTURE_ON_SOL + LATE_ON_SOL
        # W40: 76,000,000 actual, 14,600,000 on gpt-6.1-sol, saved 61,400,000 = 80.8% (8079 bp of 76,000,000)
        # W41: 2,500,000,000 actual, 500,000,000 on gpt-6.1-sol, saved 2,000,000,000 = 80.0% (8000 bp)
        rows = [(w["week"], w["sessions"], w["switched"], w["actual"]["ucents"], w["recommended"]["ucents"], w["saved"]["ucents"], w["saved_bp"]) for w in data["weeks"]]
        assert rows == [
            ("2026-W40", 1, 1, FIXTURE_UCENTS, FIXTURE_ON_SOL, 61_400_000, 8079),
            ("2026-W41", 1, 1, LATE_UCENTS, LATE_ON_SOL, 2_000_000_000, 8000),
        ]
        total = data["total"]  # the session counts in both rows and once here; 2,061,400,000 / 2,576,000,000 = 80.02%
        assert (total["sessions"], total["switched"], total["actual"]["ucents"], total["recommended"]["ucents"], total["saved"]["ucents"], total["saved_bp"]) == (
            1, 1, WHOLE_UCENTS, FIXTURE_ON_SOL + LATE_ON_SOL, 2_061_400_000, 8002,
        )
        assert total["actual"]["ucents"] == run(week_long, "summary", "--json").json()["total"]["ucents"]  # advise = summary

    def test_advise_for_one_week_keeps_the_tier_of_the_whole_session(self, week_long: tuple[Path, Path]) -> None:
        """Seen through W40 alone the session is 100,000 input tokens (0 points) and would score 2, light: the tier must not move."""
        data = run(week_long, "advise", "--week-of", "2026-W40", "--json").json()
        (session,) = data["sessions"]
        assert (session["score"], session["tier"], session["clipped"]) == (3, "medium", True)
        assert session["actual"]["ucents"] == FIXTURE_UCENTS and session["recommended"]["ucents"] == FIXTURE_ON_SOL
        assert [(w["week"], w["actual"]["ucents"]) for w in data["weeks"]] == [("2026-W40", FIXTURE_UCENTS)]
        week41 = run(week_long, "advise", "--week", "--json").json()
        assert [(w["week"], w["actual"]["ucents"], w["saved"]["ucents"]) for w in week41["weeks"]] == [("2026-W41", LATE_UCENTS, 2_000_000_000)]

    def test_the_report_of_the_week_says_25_dollars(self, week_long: tuple[Path, Path]) -> None:
        data = run(week_long, "report", "--week-of", "2026-W41", "--json").json()
        assert data["week"] == "2026-W41" and data["total"]["ucents"] == LATE_UCENTS and data["sessions"] == 1
        assert (data["router"]["actual"]["ucents"], data["router"]["saved"]["ucents"], data["router"]["saved_bp"]) == (LATE_UCENTS, 2_000_000_000, 8000)
        md = run(week_long, "report", "--week-of", "2026-W41", "--format", "md", "-o", "-").stdout
        assert "# AgentFare weekly report: 2026-W41" in md
        assert (
            "**I spent $25.00 on AI coding agents this week. In sessions you prompted yourself, a cost router would have saved 80.0% ($20.00) "
            "of the $25.00 they cost; across all sessions, including sub-agent and background threads: 80.0% ($20.00).**"
        ) in md
        assert "| Actual | $25.00 |" in md and "| With the router | $5.00 |" in md and "| Saved | $20.00 (80.0%) |" in md
        assert "| Tue Oct 6 | 1 | $25.00 |" in md
        before = run(week_long, "report", "--week-of", "2026-W40", "--json").json()
        assert before["total"]["ucents"] == FIXTURE_UCENTS and before["router"]["saved"]["ucents"] == 61_400_000
        # the default week of the report is the latest week with spend: the one of the late response, not the one the session started in
        assert run(week_long, "report", "--json").json()["week"] == "2026-W41"

    def test_the_week_total_of_every_command_is_the_same_integer(self, week_long: tuple[Path, Path]) -> None:
        summary = run(week_long, "summary", "--week", "--json").json()["total"]["ucents"]
        sessions = run(week_long, "sessions", "--week", "--json").json()["total"]["ucents"]
        models = run(week_long, "models", "--week", "--json").json()["total"]["ucents"]
        advise = run(week_long, "advise", "--week", "--json").json()["total"]["actual"]["ucents"]
        report = run(week_long, "report", "--json").json()["total"]["ucents"]
        assert summary == sessions == models == advise == report == LATE_UCENTS


# ---------------------------------------------------------------------------------------------------------------------
# A thread that goes across local midnight and a local week boundary in a timezone that is not UTC
# ---------------------------------------------------------------------------------------------------------------------
# America/Toronto is UTC-4 in October (daylight saving time ends on 2026-11-01). Two more responses, 20 minutes apart:
#   2026-10-05T03:50:00Z = Sunday 2026-10-04 23:50 in Toronto (ISO week 2026-W40), 1,000,000 uncached input tokens
#   2026-10-05T04:10:00Z = Monday 2026-10-05 00:10 in Toronto (ISO week 2026-W41), 1,000,000 uncached input tokens
#   each 1,000,000 x 10 = 10,000,000 -> $10.00 = 1,000,000,000 ucents; on gpt-6.1-sol 1,000,000 x 2 = $2.00 = 200,000,000 ucents
# On the UTC clock both are Monday 2026-10-05, in week 2026-W41. The fixture part (Tue 2026-09-29 10:00 UTC = 06:00 in Toronto) is W40.
TEN_DOLLARS, TWO_DOLLARS = 1_000_000_000, 200_000_000
TORONTO_NOW = "2026-10-05T12:00:00Z"  # Monday 08:00 in Toronto; the week 2026-W41 starts Monday 00:00 local = 2026-10-05T04:00:00Z


@pytest.fixture(scope="module")
def across_midnight(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp_path = tmp_path_factory.mktemp("across-midnight")
    config, home = codex_thread(
        tmp_path,
        usage_record(50, "2026-10-05T03:50:00.000Z", uncached=1_000_000),
        usage_record(51, "2026-10-05T04:10:00.000Z", uncached=1_000_000),
        timezone="America/Toronto",
    )
    cli(home, "ingest", config=config, now=TORONTO_NOW)
    return config, home


class TestAThreadThatCrossesLocalMidnightInAnotherTimezone:
    def test_each_response_lands_on_its_local_day_and_week(self, across_midnight: tuple[Path, Path]) -> None:
        config, home = across_midnight
        data = cli(home, "--no-ingest", "summary", "--json", config=config, now=TORONTO_NOW).json()
        assert data["total"]["ucents"] == FIXTURE_UCENTS + 2 * TEN_DOLLARS  # $20.76, one session
        assert by(data["by_day"], "day") == [("2026-09-29", FIXTURE_UCENTS), ("2026-10-04", TEN_DOLLARS), ("2026-10-05", TEN_DOLLARS)]
        assert by(data["by_week"], "week") == [("2026-W40", FIXTURE_UCENTS + TEN_DOLLARS), ("2026-W41", TEN_DOLLARS)]

    def test_on_the_utc_clock_the_same_logs_fall_on_other_days(self, across_midnight: tuple[Path, Path], tmp_path: Path) -> None:
        config, _ = across_midnight
        utc = write_config(tmp_path / "utc.yaml", sources={"claude": [], "gemini": [], "codex": [str(config.parent / "codex" / "sessions")]})
        data = cli(fresh_home(tmp_path), "summary", "--json", config=utc, now=TORONTO_NOW).json()
        assert by(data["by_day"], "day") == [("2026-09-29", FIXTURE_UCENTS), ("2026-10-05", 2 * TEN_DOLLARS)]
        assert by(data["by_week"], "week") == [("2026-W40", FIXTURE_UCENTS), ("2026-W41", 2 * TEN_DOLLARS)]
        assert data["total"]["ucents"] == FIXTURE_UCENTS + 2 * TEN_DOLLARS  # the total does not depend on the timezone

    def test_the_local_week_starts_at_local_midnight(self, across_midnight: tuple[Path, Path]) -> None:
        config, home = across_midnight
        week = cli(home, "--no-ingest", "summary", "--week", "--json", config=config, now=TORONTO_NOW).json()
        assert week["period"]["since"] == "2026-10-05T04:00:00Z"  # Monday 00:00 in Toronto
        assert (week["sessions"], week["total"]["ucents"]) == (1, TEN_DOLLARS)  # only the response at 00:10 local; the one at 23:50 is W40's
        sessions = cli(home, "--no-ingest", "sessions", "--week", "--json", config=config, now=TORONTO_NOW).json()
        assert sessions["sessions"][0]["clipped"] is True and sessions["total"]["ucents"] == TEN_DOLLARS
        before = cli(home, "--no-ingest", "summary", "--week-of", "2026-W40", "--json", config=config, now=TORONTO_NOW).json()
        assert before["total"]["ucents"] == FIXTURE_UCENTS + TEN_DOLLARS  # Sunday 23:50 and the fixture part

    def test_advise_rows_follow_the_local_weeks(self, across_midnight: tuple[Path, Path]) -> None:
        config, home = across_midnight
        data = cli(home, "--no-ingest", "advise", "--json", config=config, now=TORONTO_NOW).json()
        # W40: 76,000,000 + 1,000,000,000 actual, 14,600,000 + 200,000,000 on gpt-6.1-sol, saved 861,400,000 (80.06% = 8006 bp of 1,076,000,000)
        # W41: 1,000,000,000 actual, 200,000,000 on gpt-6.1-sol, saved 800,000,000 (8000 bp)
        # (whole-session score 3: 2,100,000 input tokens, 2 files, 30 active minutes: the 23:50 and 00:10 responses are 20 minutes
        # apart, 10 + 20)
        assert [(w["week"], w["sessions"], w["actual"]["ucents"], w["recommended"]["ucents"], w["saved"]["ucents"], w["saved_bp"]) for w in data["weeks"]] == [
            ("2026-W40", 1, FIXTURE_UCENTS + TEN_DOLLARS, FIXTURE_ON_SOL + TWO_DOLLARS, 861_400_000, 8006),
            ("2026-W41", 1, TEN_DOLLARS, TWO_DOLLARS, 800_000_000, 8000),
        ]
        total = data["total"]
        assert (total["sessions"], total["actual"]["ucents"], total["saved"]["ucents"]) == (1, FIXTURE_UCENTS + 2 * TEN_DOLLARS, 1_661_400_000)


# ---------------------------------------------------------------------------------------------------------------------
# The tier is the whole session's, in `advise` and in `report` alike
# ---------------------------------------------------------------------------------------------------------------------
class TestTheReportScoresTheWholeSession:
    def test_a_week_that_holds_a_small_part_of_a_session_does_not_change_its_tier(self, tmp_path: Path) -> None:
        """With `medium` set to keep (`null`), only a light session is compared with gpt-6.1-sol. The week-long thread scores 3, medium,
        over its whole life (2,100,000 input tokens). Its first week holds 100,000 of them and would score 2, light, on its own: then
        a saving of $0.614 would be advised on the week that the whole session does not deserve. `advise` and `report` both say 0."""
        config, home = codex_thread(
            tmp_path, usage_record(50, "2026-10-06T10:20:00.000Z", uncached=2_000_000, output=100_000),
            router={"recommend": {"light": "gpt-6.1-sol", "medium": None, "heavy": None}},
        )  # fmt: skip
        week40 = ["--week-of", "2026-W40"]
        advise = cli(home, "advise", *week40, "--all", "--json", config=config, now=NOW_W41).json()
        (session,) = advise["sessions"]
        assert (session["tier"], session["score"], session["switch"], session["clipped"]) == ("medium", 3, False, True)
        assert advise["total"]["saved"]["ucents"] == 0
        report = cli(home, "--no-ingest", "report", *week40, "--json", config=config, now=NOW_W41).json()
        assert report["total"]["ucents"] == FIXTURE_UCENTS  # the money is W40's share
        assert report["router"]["saved"]["ucents"] == 0 and report["prompted"]["saved"]["ucents"] == 0
        assert report["router"]["switched"] == 0

"""`agentfare report`: exact numbers on the synthetic fixtures, hand-built weeks for the edge cases, and the promises of the
HTML file (one static page, no script, no network reference, hostile names escaped)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from agentfare.cli import app
from agentfare.commands.report import md_text
from agentfare.db import Store
from agentfare.models import SessionRecord, Usage

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config.yaml"
FIXTURE_ARGS = ("--config", str(FIXTURE_CONFIG))
MakeRecord = Callable[..., SessionRecord]


@pytest.fixture(autouse=True)
def frozen(frozen_now: Callable[[str], Any]) -> None:
    frozen_now("2026-09-30T12:00:00Z")  # a Wednesday of ISO week 2026-W40


def report(*args: str, prefix: tuple[str, ...] = FIXTURE_ARGS) -> Result:
    return CliRunner().invoke(app, [*prefix, "report", *args])


def out(*args: str, prefix: tuple[str, ...] = FIXTURE_ARGS) -> str:
    result = report(*args, prefix=prefix)
    assert result.exit_code == 0, result.output + result.stderr
    return result.stdout


def data(*args: str, prefix: tuple[str, ...] = FIXTURE_ARGS) -> dict[str, Any]:
    return json.loads(out("--json", *args, prefix=prefix))


def html_of(tmp_path: Path, *args: str, prefix: tuple[str, ...] = FIXTURE_ARGS) -> str:
    target = tmp_path / "page.html"
    out("--format", "html", "-o", str(target), *args, prefix=prefix)
    return target.read_text(encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path) -> Callable[..., tuple[str, ...]]:
    """``world(record, ...)``: a database holding just these sessions (UTC weeks); returns the global options to use it."""

    def build(*records: SessionRecord) -> tuple[str, ...]:
        home = tmp_path / "world"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text("timezone: UTC\nsources: {claude: [], codex: [], gemini: []}\n", encoding="utf-8")
        with Store(home / "agentfare.db") as store:
            for record in records:
                store.upsert_session(record)
        return ("--home", str(home), "--no-ingest")

    return build


def astra(input_tokens: int) -> dict[str, Usage]:
    """gpt-6-astra costs $10 per million uncached input tokens: 100,000 of them are exactly $1.00 (100,000,000 ucents)."""
    return {"gpt-6-astra": Usage(input=input_tokens)}


def money(cents: int) -> dict[str, int]:
    return {"cents": cents, "ucents": cents * 1_000_000}


class Tags(HTMLParser):
    """Checks that every tag opens and closes in order (void and self-closed SVG shapes aside)."""

    VOID = {"meta", "link", "br", "hr", "img", "input", "area", "base", "col", "embed", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.tags: set[str] = set()
        self.attrs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.attrs.extend(attrs)
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.attrs.extend(attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.problems.append(f"</{tag}> where {self.stack[-1:]} was open")


def parsed(page: str) -> Tags:
    tags = Tags()
    tags.feed(page)
    tags.close()
    assert tags.problems == [] and tags.stack == []
    return tags


# ---- the synthetic fixtures: one week, 3 sessions -------------------------------------------------------
# Hand-computed, per million tokens (the price table of docs/PRICING-SOURCES.md):
#   claude-opus-5-5   39,200 x $4 + 3,080,000 x $0.20 + 93,000 x $5 (5-min writes) + 62,000 x $8 (1-hour writes)
#                     + 42,900 x $20                       = $0.1568 + $0.616 + $0.465 + $0.496 + $0.858 = $2.5918
#   claude-haiku-4-5  4,700 x $1 + 120,000 x $0.10 + 6,000 x $1.25 + 12,000 x $2 + 2,200 x $5
#                                                          = $0.0047 + $0.012 + $0.0075 + $0.024 + $0.011 = $0.0592
#   gpt-6-astra       40,000 x $10 + 60,000 x $1 + 6,000 x $50                 = $0.40 + $0.06 + $0.30    = $0.76
#   gemini-2.5-pro    92,000 x $1.25 + 80,000 x $0.125 + 9,500 x $10           = $0.115 + $0.01 + $0.095  = $0.22
# Total $3.631. The router keeps the heavy Claude session and the Gemini one (already cheaper than gpt-6.1-sol: $0.287
# there) and moves the Codex one to gpt-6.1-sol: 40,000 x $2 + 60,000 x $0.10 + 6,000 x $10 = $0.146. Router $3.017,
# saved $0.614 = 16.9% (1691 basis points).
OPUS, HAIKU, ASTRA, GEMINI = 259_180_000, 5_920_000, 76_000_000, 22_000_000
TOTAL, ROUTER, SAVED = 363_100_000, 301_700_000, 61_400_000


class TestFixtureWeek:
    def test_headline_numbers(self) -> None:
        d = data()
        assert (d["week"], d["first_day"], d["last_day"], d["generated"]) == ("2026-W40", "2026-09-28", "2026-10-04", "2026-09-30")
        assert (d["state"], d["sessions"], d["agents"], d["models"], d["projects"]) == ("savings", 3, 3, 4, 3)
        assert d["total"] == {"cents": 363, "ucents": TOTAL} and d["excluded_tokens"] == 0
        assert d["router"] == {
            "week": "2026-W40", "sessions": 3, "switched": 1, "skipped": 0,
            "actual": {"cents": 363, "ucents": TOTAL}, "recommended": {"cents": 302, "ucents": ROUTER},
            "saved": {"cents": 61, "ucents": SAVED}, "saved_bp": 1691,
        }
        assert d["background"] == {"sessions": 0, "saved": money(0)} and d["previous"] is None and d["warnings"] == []
        assert d["prompted"] == {"sessions": 3, "actual": {"cents": 363, "ucents": TOTAL}, "saved": {"cents": 61, "ucents": SAVED}, "saved_bp": 1691}

    def test_breakdowns(self) -> None:
        d = data()
        assert [(m["model"], m["cost"]["ucents"], m["share_bp"]) for m in d["by_model"]] == [
            ("claude-opus-5-5", OPUS, 7138), ("gpt-6-astra", ASTRA, 2093), ("gemini-2.5-pro", GEMINI, 606), ("claude-haiku-4-5", HAIKU, 163),
        ]
        assert [(a["agent"], a["cost"]["ucents"]) for a in d["by_agent"]] == [
            ("claude", OPUS + HAIKU), ("codex", ASTRA), ("gemini", GEMINI),
        ]
        days = d["by_day"]
        assert [x["day"] for x in days] == [f"2026-09-{n}" for n in (28, 29, 30)] + [f"2026-10-0{n}" for n in (1, 2, 3, 4)]
        assert [x["cost"]["ucents"] for x in days] == [OPUS + HAIKU, ASTRA, GEMINI, 0, 0, 0, 0]
        assert [x["sessions"] for x in days] == [1, 1, 1, 0, 0, 0, 0]
        assert d["tokens"] == {"input": 175_900, "cache_read": 3_340_000, "cache_write": 173_000, "output": 60_600, "total": 3_749_500}

    def test_tier_mix_and_top_sessions(self) -> None:
        d = data()
        # Claude scores 7/10 (heavy), the Codex and Gemini sessions 2 and 1 (light): docs/ROUTING.md, section 3
        assert d["tiers"] == [
            {"tier": "light", "sessions": 2, "cost": money(98), "unpriced_sessions": 0},
            {"tier": "medium", "sessions": 0, "cost": money(0), "unpriced_sessions": 0},
            {"tier": "heavy", "sessions": 1, "cost": {"cents": 265, "ucents": OPUS + HAIKU}, "unpriced_sessions": 0},
        ]
        assert d["top_sessions"] == [{
            "short_id": "7c8d9e0f", "agent": "codex", "project": "Project B", "model": "gpt-6-astra",
            "recommended_model": "gpt-6.1-sol", "tier": "light", "actual": {"cents": 76, "ucents": ASTRA},
            "recommended": {"cents": 15, "ucents": 14_600_000}, "saved": {"cents": 61, "ucents": 61_400_000},
        }]

    def test_markdown(self) -> None:
        text = out()
        lines = text.splitlines()
        assert lines[0] == "# AgentFare weekly report: 2026-W40"
        assert (
            "**I spent $3.63 on AI coding agents this week. In sessions you prompted yourself, a cost router would have saved 16.9% ($0.61) "
            "of the $3.63 they cost; across all sessions, including sub-agent and background threads: 16.9% ($0.61).**"
        ) in lines
        assert lines[lines.index("| | Spend |") :][:5] == ["| | Spend |", "|---|---:|", "| Actual | $3.63 |", "| With the router | $3.02 |", "| Saved | $0.61 (16.9%) |"]
        headings = [ln for ln in lines if ln.startswith("## ")]
        assert headings == ["## Spend by model", "## By agent", "## By day", "## Session mix", "## Biggest savings", "## Notes"]
        assert "| claude-opus-5-5 | 1 | $2.59 | 71.4% |" in lines
        assert "| Mon Sep 28 | 1 | $2.65 |" in lines and "| Sun Oct 4 | 0 | - |" in lines
        assert "| `7c8d9e0f` | codex | Project B | gpt-6-astra | gpt-6.1-sol | $0.76 | $0.15 | $0.61 (5.2x cheaper) |" in lines
        assert any("re-prices the same tokens" in ln and "docs/ROUTING.md" in ln for ln in lines)
        assert lines[-1] == "Generated 2026-09-30 by agentfare 0.1.0. Local-first: no account, no cloud, no prompts stored."
        assert text.endswith("\n") and not any(ln != ln.rstrip() for ln in lines)

    def test_report_totals_are_the_summary_and_advise_totals(self) -> None:
        d = data()
        s = json.loads(CliRunner().invoke(app, [*FIXTURE_ARGS, "summary", "--week-of", "2026-W40", "--json"]).stdout)
        assert d["total"] == s["total"] and d["sessions"] == s["sessions"] and d["excluded_tokens"] == s["excluded_tokens"]
        assert [x["cost"] for x in d["by_day"] if x["sessions"]] == [x["cost"] for x in s["by_day"]]
        a = json.loads(CliRunner().invoke(app, [*FIXTURE_ARGS, "advise", "--week-of", "2026-W40", "--json"]).stdout)
        assert d["router"] == a["weeks"][0]

    def test_nothing_from_the_logs_leaks(self) -> None:
        for text in (out(), out("--json")):
            assert "PRIVACY-CANARY" not in text and not re.search(r"acme-shop|fake-repo|demo-notes-app", text)

    def test_week_of_names_the_week_and_is_case_insensitive(self) -> None:
        assert data("--week-of", "2026-w40")["week"] == "2026-W40"
        assert "week_of" not in out("--week-of", "2026-W40")


class TestProjects:
    def test_hidden_by_default_and_aliased_by_spend(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(
            make_record("small", project="quiet-repo", usage={"claude-opus-5-5": Usage(input=100_000, output=10_000)}, prompts=1),
            make_record("big", project="secret-repo", usage={"claude-opus-5-5": Usage(input=200_000, output=20_000)}, prompts=1),
        )
        d = data(prefix=prefix)
        assert [(s["short_id"], s["project"]) for s in d["top_sessions"]] == [("big", "Project A"), ("small", "Project B")]
        assert d["show_projects"] is False and d["projects"] == 2
        for text in (out(prefix=prefix), out("--json", prefix=prefix)):
            assert "secret-repo" not in text and "quiet-repo" not in text

    def test_shown_with_the_flag(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path) -> None:
        prefix = world(make_record("s1", project="secret-repo", usage={"claude-opus-5-5": Usage(input=100_000, output=10_000)}, prompts=1))
        assert "| secret-repo |" in out("--show-projects", prefix=prefix)
        assert "secret-repo" in html_of(tmp_path, "--show-projects", prefix=prefix)
        d = data("--show-projects", prefix=prefix)
        assert d["show_projects"] is True and d["top_sessions"][0]["project"] == "secret-repo"
        assert "secret-repo" not in html_of(tmp_path, prefix=prefix)

    def test_more_than_26_projects_never_run_out_of_aliases(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(*(make_record(f"s{n:02d}", project=f"p{n:02d}", usage=astra(100_000 + n), prompts=1) for n in range(28)))
        labels = [s["project"] for s in data(prefix=prefix)["top_sessions"]]
        assert labels[:2] == ["Project A", "Project B"] and data(prefix=prefix)["projects"] == 28
        text = out("--show-projects", prefix=prefix)
        assert "p27" in text


class TestWeeks:
    def test_defaults_to_the_latest_week_with_sessions_at_or_before_now(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(
            make_record("old", started_at="2026-09-08T10:00:00Z", usage=astra(100_000)),  # 2026-W37
            make_record("prev", started_at="2026-09-22T10:00:00Z", usage=astra(100_000)),  # 2026-W39
            make_record("later", started_at="2026-10-08T10:00:00Z", usage=astra(100_000)),  # 2026-W41: after now
        )
        d = data(prefix=prefix)
        assert d["week"] == "2026-W39" and d["total"] == money(100)
        assert data("--week-of", "2026-W37", prefix=prefix)["week"] == "2026-W37"

    def test_an_empty_database_reports_the_current_week_as_empty(self, world: Callable[..., tuple[str, ...]]) -> None:
        prefix = world()
        d = data(prefix=prefix)
        assert d["week"] == "2026-W40" and d["state"] == "empty" and d["sessions"] == 0
        assert d["total"] == money(0) and d["router"]["saved_bp"] is None

    def test_an_empty_week_says_so(self, tmp_path: Path) -> None:
        text = out("--week-of", "2026-W30")
        assert "No sessions were recorded in 2026-W30 (Jul 20 to Jul 26, 2026), so there is nothing to report." in text
        for hype in ("saved", "$0.00", "## "):
            assert hype not in text
        page = html_of(tmp_path, "--week-of", "2026-W30")
        assert "No sessions were recorded in 2026-W30" in page
        assert "<svg class=\"bar\"" not in page and "would have saved" not in page
        parsed(page)

    def test_an_empty_week_says_what_data_there_is_like_the_other_commands(self, tmp_path: Path) -> None:
        text = out("--week-of", "2026-W30")
        assert "\nData exists from 2026-09-28 to 2026-09-30.\n" in text
        assert text.index("so there is nothing to report.") < text.index("Data exists from") < text.index("Generated 2026-09-30")
        page = html_of(tmp_path, "--week-of", "2026-W30")
        assert '<p class="meta">Data exists from 2026-09-28 to 2026-09-30.</p>' in page
        parsed(page)
        assert "hints" not in data("--week-of", "2026-W30")  # the JSON keeps its keys

    def test_an_empty_database_points_at_ingest_and_config(self, world: Callable[..., tuple[str, ...]], tmp_path: Path) -> None:
        prefix = world()
        line = "The database has no sessions at all: run agentfare ingest, and check the log sources with agentfare config."
        assert f"\n{line}\n" in out(prefix=prefix)
        assert line in html_of(tmp_path, prefix=prefix)

    def test_a_week_with_data_has_no_hint(self) -> None:
        assert "Data exists from" not in out() and "no sessions at all" not in out()

    def test_a_week_over_new_year_shows_both_years(self) -> None:
        assert "Dec 28, 2026 to Jan 3, 2027" in out("--week-of", "2026-W53")

    @pytest.mark.parametrize("label", ["2026-40", "W40", "2026-W54", "2027-W53", "2026-W00", "nonsense"])
    def test_a_bad_week_is_an_error(self, label: str) -> None:
        result = report("--week-of", label)
        assert result.exit_code == 2 and result.stderr.startswith("error: invalid ISO week")

    def test_a_blank_week_is_an_error(self) -> None:
        result = report("--week-of", " ")
        assert result.exit_code == 2 and result.stderr == "error: --week-of needs a value\n"

    def test_week_over_week(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path) -> None:
        prefix = world(
            make_record("p1", started_at="2026-09-22T10:00:00Z", usage=astra(100_000)),  # 2026-W39: 2 x $1.00
            make_record("p2", started_at="2026-09-23T10:00:00Z", usage=astra(100_000)),
            make_record("c1", started_at="2026-09-29T10:00:00Z", usage=astra(100_000)),  # 2026-W40: 3 x $1.00
            make_record("c2", started_at="2026-09-30T10:00:00Z", usage=astra(100_000)),
            make_record("c3", started_at="2026-10-01T10:00:00Z", usage=astra(100_000)),
        )
        d = data(prefix=prefix)
        assert d["previous"] == {"week": "2026-W39", "sessions": 2, "total": money(200), "change_bp": 5000} and d["in_progress"] is True
        assert data("--week-of", "2026-W39", prefix=prefix)["previous"] is None  # nothing before it
        # now is a Wednesday: 3 days of W40 against the whole of W39 is no comparison, so neither page prints a percentage
        text, page = out(prefix=prefix), html_of(tmp_path, prefix=prefix)
        assert "Week in progress, so there is no comparison with the week before yet." in text
        assert '<p class="pill">Week in progress</p>' in page
        for shown in (text, page):
            assert "50.0%" not in shown and "$2.00" not in shown and "Spend is up" not in shown and "Spend up" not in shown

    def test_spend_that_falls_or_more_than_doubles(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, frozen_now: Callable[[str], Any]
    ) -> None:
        prefix = world(
            make_record("a", started_at="2026-09-15T10:00:00Z", usage=astra(500_000)),  # W38: $5.00
            make_record("b", started_at="2026-09-22T10:00:00Z", usage=astra(100_000)),  # W39: $1.00, down 80.0%
            make_record("c", started_at="2026-09-29T10:00:00Z", usage=astra(600_000)),  # W40: $6.00, up 500.0%
        )
        assert data("--week-of", "2026-W39", prefix=prefix)["previous"]["change_bp"] == -8000
        assert "Spend is down 80.0% from 2026-W38 ($5.00)." in out("--week-of", "2026-W39", prefix=prefix)
        assert "Week in progress" in out(prefix=prefix) and "500.0%" not in out(prefix=prefix)  # W40 is still running
        frozen_now("2026-10-05T00:00:00Z")  # ... and now it is over
        assert "Spend is up 500.0% from 2026-W39 ($1.00)." in out(prefix=prefix)
        assert "Week in progress" not in out(prefix=prefix)

    def test_a_week_is_in_progress_until_its_last_day_is_over(self, frozen_now: Callable[[str], Any]) -> None:
        assert data()["in_progress"] is True and "Sep 28 to Oct 4, 2026 (in progress)." in out()
        assert data("--week-of", "2026-W39")["in_progress"] is False
        frozen_now("2026-10-04T23:00:00Z")  # the Sunday: still running
        assert data()["in_progress"] is True
        frozen_now("2026-10-05T00:00:00Z")
        d = data("--week-of", "2026-W40")
        assert d["in_progress"] is False and "(in progress)" not in out("--week-of", "2026-W40")

    def test_only_the_current_week_is_called_this_week(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(make_record("old", started_at="2026-09-22T10:00:00Z", usage=astra(100_000), prompts=1))  # 2026-W39; now is W40
        text = out(prefix=prefix)
        assert (
            "**I spent $1.00 on AI coding agents in 2026-W39. In sessions you prompted yourself, a cost router would have saved 80.0% ($0.80) "
            "of the $1.00 they cost; across all sessions, including sub-agent and background threads: 80.0% ($0.80).**"
        ) in text
        assert "this week" not in text

    def test_no_week_over_week_when_the_week_before_has_no_priced_spend(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord
    ) -> None:
        prefix = world(
            make_record("p1", started_at="2026-09-22T10:00:00Z", usage={"codex-auto-review": Usage(input=1000)}),
            make_record("c1", started_at="2026-09-29T10:00:00Z", usage=astra(100_000)),
        )
        d = data(prefix=prefix)
        assert d["previous"] == {"week": "2026-W39", "sessions": 1, "total": money(0), "change_bp": None}
        assert "Spend is" not in out(prefix=prefix)


def two_weeks(make_record: MakeRecord) -> list[SessionRecord]:
    """One session that starts on Sunday 2026-09-27 at 23:30 UTC (2026-W39) and keeps spending into Monday (2026-W40):
    $1.00 before midnight and $3.00 after it, both on gpt-6-astra. A second session lives inside W40 only ($1.00)."""
    before, after = "2026-09-27T23:30:00Z", "2026-09-28T00:15:00Z"
    return [
        make_record(
            "span", started_at=before, wall_seconds=3000, usage=astra(400_000), prompts=1,
            slots={(before, "gpt-6-astra"): Usage(input=100_000), (after, "gpt-6-astra"): Usage(input=300_000)},
        ),
        make_record("solo", started_at="2026-09-29T10:00:00Z", usage=astra(100_000), prompts=1),
    ]


class TestMoneyIsAttributedToWhenItWasSpent:
    @pytest.mark.parametrize(("week", "sessions", "cents", "saved"), [("2026-W39", 1, 100, 80), ("2026-W40", 2, 400, 320)])
    def test_a_session_across_two_weeks_shows_only_what_was_spent_inside_each(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, week: str, sessions: int, cents: int, saved: int
    ) -> None:
        prefix = world(*two_weeks(make_record))
        d = data("--week-of", week, prefix=prefix)
        assert (d["week"], d["sessions"], d["total"]) == (week, sessions, money(cents))
        assert d["router"]["sessions"] == sessions and d["router"]["actual"] == money(cents) and d["router"]["saved"] == money(saved)
        assert d["prompted"]["saved"] == money(saved) and d["tokens"]["total"] == cents * 1000
        assert sum(x["cost"]["ucents"] for x in d["by_day"]) == cents * 1_000_000
        s = json.loads(CliRunner().invoke(app, [*prefix, "summary", "--week-of", week, "--json"]).stdout)
        assert d["total"] == s["total"] and d["sessions"] == s["sessions"] and d["tokens"] == s["tokens"]
        assert [x["cost"] for x in d["by_day"] if x["sessions"]] == [x["cost"] for x in s["by_day"]]
        a = json.loads(CliRunner().invoke(app, [*prefix, "advise", "--week-of", week, "--json"]).stdout)
        assert a["weeks"] == [d["router"]]  # one row, the week asked for, with the very figures of the report

    def test_the_days_are_the_days_the_money_was_spent(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(*two_weeks(make_record))
        assert [(x["day"], x["sessions"], x["cost"]["cents"]) for x in data("--week-of", "2026-W39", prefix=prefix)["by_day"] if x["sessions"]] == [("2026-09-27", 1, 100)]
        assert [(x["day"], x["cost"]["cents"]) for x in data("--week-of", "2026-W40", prefix=prefix)["by_day"] if x["sessions"]] == [("2026-09-28", 300), ("2026-09-29", 100)]

    def test_the_week_before_is_what_was_spent_in_it(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        d = data("--week-of", "2026-W40", prefix=world(*two_weeks(make_record)))
        assert d["previous"] == {"week": "2026-W39", "sessions": 1, "total": money(100), "change_bp": 30000}

    def test_the_latest_week_is_the_one_of_the_latest_spend_not_of_the_start(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, frozen_now: Callable[[str], Any]
    ) -> None:
        frozen_now("2026-10-14T12:00:00Z")  # 2026-W42, with nothing spent in it
        before, after = "2026-09-27T23:30:00Z", "2026-09-28T00:15:00Z"
        prefix = world(make_record("span", started_at=before, usage=astra(400_000), slots={(before, "gpt-6-astra"): Usage(input=100_000), (after, "gpt-6-astra"): Usage(input=300_000)}))
        d = data(prefix=prefix)
        assert (d["week"], d["total"]) == ("2026-W40", money(300))


class TestPrices:
    def test_an_unverified_model_is_left_out_named_and_marked(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path) -> None:
        prefix = world(
            make_record("ok", usage=astra(100_000), prompts=1),
            make_record("odd", started_at="2026-09-29T10:00:00Z", usage={"codex-auto-review": Usage(input=300_000, output=20_000)}),
        )
        d = data(prefix=prefix)
        assert d["total"] == money(100) and d["excluded_tokens"] == 320_000
        assert d["warnings"] == [{
            "model": "codex-auto-review", "status": "unverified", "excluded_tokens": 320_000,
            "message": "codex-auto-review: price is unverified, its spend is not counted (see pricing.yaml)",
        }]
        odd = next(m for m in d["by_model"] if m["model"] == "codex-auto-review")
        assert odd["cost"] is None and odd["share_bp"] is None and odd["unpriced_tokens"] == 320_000
        assert d["tiers"][0]["unpriced_sessions"] == 1
        text = out(prefix=prefix)
        assert "I spent $1.00+ on AI coding agents" in text
        assert "> **Warning:** 1 model without a verified price: 320k tokens are not counted in any dollar figure." in text
        assert "> - 320k tokens: codex-auto-review: price is unverified, its spend is not counted (see pricing.yaml)" in text
        assert "| codex-auto-review | 1 | no verified price | - |" in text
        assert "A + after an amount means the figure leaves out tokens that have no verified price." in text
        page = html_of(tmp_path, prefix=prefix)
        assert "Warning: 1 model without a verified price" in page and "codex-auto-review: price is unverified" in page
        assert "no verified price" in page and "$1.00+" in page

    def test_a_recommended_model_without_a_price_is_not_called_partly_verified(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path
    ) -> None:
        """No token is left out here (only the model the router would move to has no price), so the header has no token count,
        and it must not say the prices are 'partly' verified: this one is not verified at all."""
        prefix = world(make_record("ok", usage=astra(100_000), prompts=1))
        config = Path(prefix[1]) / "config.yaml"
        config.write_text(config.read_text() + "router:\n  recommend: {light: codex-auto-review, medium: codex-auto-review, heavy: null}\n")
        assert data(prefix=prefix)["warnings"] == [{
            "model": "codex-auto-review", "status": "unverified", "excluded_tokens": 0,
            "message": "codex-auto-review: price is unverified, its spend is not counted (see pricing.yaml)",
        }]
        text = out(prefix=prefix)
        assert "> **Warning:** 1 model without a verified price." in text and "partly" not in text
        page = html_of(tmp_path, prefix=prefix)
        assert "Warning: 1 model without a verified price" in page and "partly" not in page

    def test_every_price_message_starts_with_its_model_id(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        """The markdown sets the id apart from the sentence (a code span), which relies on this shape of ``PriceTable.warning_for``."""
        prefix = world(
            make_record("unknown", usage={"no: such model": Usage(input=1000)}),
            make_record("unverified", usage={"codex-auto-review": Usage(input=1000)}),
        )
        warnings = data(prefix=prefix)["warnings"]
        assert {w["status"] for w in warnings} == {"unverified", "unknown"}
        assert all(w["message"].startswith(f"{w['model']}: ") for w in warnings)
        assert "> - 1.0k tokens: `no: such model`: not in the price table" in out(prefix=prefix)

    def test_a_session_with_an_unpriced_part_is_left_out_of_the_comparison_and_says_so(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord
    ) -> None:
        prefix = world(
            make_record("mixed", started_at="2026-09-29T10:00:00Z", usage={"gpt-6-astra": Usage(input=100_000), "codex-auto-review": Usage(input=1000)}),
            make_record("plain", usage=astra(100_000), prompts=1),
        )
        d = data(prefix=prefix)
        assert d["total"] == money(200)  # summary adds the priced part of the mixed session
        assert d["router"]["skipped"] == 1 and d["router"]["actual"] == money(100)
        assert "1 session using a model without a verified price is left out of the savings comparison; any priced part is still in the total." in out(prefix=prefix)

    def test_a_week_where_nothing_is_priced_shows_no_savings_hype(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path
    ) -> None:
        prefix = world(make_record("odd", usage={"codex-auto-review": Usage(input=300_000)}, prompts=1))
        d = data(prefix=prefix)
        assert d["state"] == "unpriced" and d["total"] == money(0) and d["excluded_tokens"] == 300_000
        text = out(prefix=prefix)
        assert "None of this week's models has a verified price, so there is no dollar total and no saving to show." in text
        assert "> **Warning:** 1 model without a verified price" in text
        for hype in ("would have saved", "I spent", "$0.00", "| Actual |", "## Biggest savings", "left out of the savings comparison"):
            assert hype not in text
        page = html_of(tmp_path, prefix=prefix)
        assert "none of this week&#39;s models has a verified price" in page and "n/a" in page
        assert "would have saved" not in page and "$0.00" not in page
        parsed(page)

    def test_a_past_week_where_nothing_is_priced_does_not_say_this_week(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path
    ) -> None:
        prefix = world(make_record("odd", started_at="2026-09-22T10:00:00Z", usage={"codex-auto-review": Usage(input=300_000)}, prompts=1))
        assert "None of 2026-W39's models has a verified price, so there is no dollar total and no saving to show." in out(prefix=prefix)
        page = html_of(tmp_path, prefix=prefix)
        assert "none of 2026-W39&#39;s models has a verified price" in page and "this week" not in page

    def test_a_week_where_no_session_could_be_compared_says_so(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path
    ) -> None:
        mixed = {"gpt-6-astra": Usage(input=100_000), "codex-auto-review": Usage(input=1000)}
        prefix = world(make_record("mixed", usage=mixed, prompts=1))
        d = data(prefix=prefix)
        assert d["state"] == "none" and d["router"]["sessions"] == 0 and d["router"]["skipped"] == 1
        text = out(prefix=prefix)
        assert "I spent $1.00+ on AI coding agents this week. The router could not compare any session: each one used a model without a verified price." in text
        for nonsense in ("likely to do the job", "| Actual |", "With the router", "$0.00"):
            assert nonsense not in text
        page = html_of(tmp_path, prefix=prefix)
        assert "could not compare any session" in page and "likely to do the job" not in page
        parsed(page)

    def test_the_markdown_comparison_says_when_it_covers_fewer_sessions_than_the_headline(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord
    ) -> None:
        mixed = {"gpt-6-astra": Usage(input=100_000), "codex-auto-review": Usage(input=1000)}
        prefix = world(make_record("mixed", usage=mixed, prompts=1), make_record("plain", started_at="2026-09-30T10:00:00Z", usage=astra(200_000), prompts=1))
        lines = out(prefix=prefix).splitlines()
        assert "| Saved | $1.60 (80.0%) |" in lines
        tail = "Compared on the sessions that are fully priced; see the notes below."
        assert lines[lines.index("| Saved | $1.60 (80.0%) |") + 2] == tail
        assert tail not in out()  # the fixture week compares everything it counts

    def test_no_advised_switch_says_so_instead_of_zero_savings(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path
    ) -> None:
        prefix = world(make_record("on-sol", usage={"gpt-6.1-sol": Usage(input=100_000)}, prompts=1))  # already on the recommended model
        d = data(prefix=prefix)
        assert d["state"] == "none" and d["router"]["saved"] == money(0) and d["top_sessions"] == []
        text = out(prefix=prefix)
        assert "I spent $0.20 on AI coding agents this week. The router found no session where a cheaper model was likely to do the job." in text
        for hype in ("would have saved", "| Saved |", "## Biggest savings"):
            assert hype not in text
        page = html_of(tmp_path, prefix=prefix)
        assert "No session where a cheaper model was likely to do the job." in page and "would have saved" not in page
        parsed(page)


class TestBackgroundSessions:
    def test_sessions_nobody_prompted_count_but_are_not_listed(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(
            make_record("driven", usage=astra(100_000), prompts=2),
            make_record("thread1", started_at="2026-09-29T10:00:00Z", usage=astra(100_000), prompts=0),
            make_record("thread2", started_at="2026-09-29T11:00:00Z", usage=astra(100_000), prompts=0),
        )
        d = data(prefix=prefix)
        assert d["router"]["switched"] == 3 and d["total"] == money(300)
        assert [s["short_id"] for s in d["top_sessions"]] == ["driven"]
        # astra -> gpt-6.1-sol on 100,000 input tokens: $1.00 -> $0.20, so $0.80 saved per session
        assert d["background"] == {"sessions": 2, "saved": {"cents": 160, "ucents": 160_000_000}}
        assert "2 sub-agent or background sessions (0 prompts) are in the totals and account for $1.60 of the saving; only sessions with prompts are listed." in out(prefix=prefix)

    def test_a_saving_that_is_all_background_says_why_nothing_is_listed(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(make_record("thread", usage=astra(100_000), prompts=0))
        text = out(prefix=prefix)
        assert "the saving comes from sub-agent and background sessions, which are not listed." in text
        assert "1 sub-agent or background session (0 prompts) is in the totals and accounts for $0.80 of the saving; only sessions with prompts are listed." in text
        assert (
            "**I spent $1.00 on AI coding agents this week. A cost router would have saved 80.0% ($0.80) across all sessions, including "
            "sub-agent and background threads; no session you prompted yourself could be compared.**"
        ) in text
        assert data(prefix=prefix)["prompted"] == {"sessions": 0, "actual": money(0), "saved": money(0), "saved_bp": None}
        assert data(prefix=prefix)["top_sessions"] == []

    def test_the_headline_shows_the_saving_on_sessions_you_prompted_next_to_the_one_on_all(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path
    ) -> None:
        prefix = world(
            make_record("a", usage=astra(100_000), prompts=1),  # $1.00, router $0.20
            make_record("b", started_at="2026-09-29T10:00:00Z", usage={"gpt-6.1-sol": Usage(input=100_000)}, prompts=1),  # $0.20, already there
            make_record("c", started_at="2026-09-29T11:00:00Z", usage=astra(200_000), prompts=0),  # $2.00, router $0.40
        )
        d = data(prefix=prefix)
        assert d["router"]["saved"] == money(240) and d["router"]["saved_bp"] == 7500 and d["background"]["saved"] == money(160)
        assert d["prompted"] == {"sessions": 2, "actual": money(120), "saved": money(80), "saved_bp": 6667}  # 0.80 of the 1.20 they cost
        assert d["prompted"]["saved"]["ucents"] + d["background"]["saved"]["ucents"] == d["router"]["saved"]["ucents"]
        lines = out(prefix=prefix).splitlines()
        assert (
            "**I spent $3.20 on AI coding agents this week. In sessions you prompted yourself, a cost router would have saved 66.7% ($0.80) "
            "of the $1.20 they cost; across all sessions, including sub-agent and background threads: 75.0% ($2.40).**"
        ) in lines
        page = html_of(tmp_path, prefix=prefix)
        # the figure to quote comes first, labelled, and the one on all sessions after it
        for shown in ("In sessions you prompted yourself, a cost router would have saved", "66.7%", "$0.80</strong> of the $1.20 they cost",
                      "Across all sessions, including sub-agent and background threads", "75.0%", "$2.40</strong> by moving 2 of 3 sessions to a cheaper model"):
            assert shown in page
        assert page.index("66.7%") < page.index("75.0%") and page.index("In sessions you prompted yourself") < page.index("Across all sessions")
        parsed(page)

    def test_a_percentage_next_to_a_spend_it_is_not_of_names_its_base(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path) -> None:
        """One session with a model that has no verified price is left out of the comparison: $2.00 is spent, $1.00 could be compared."""
        prefix = world(
            make_record("mixed", started_at="2026-09-29T10:00:00Z", usage={"gpt-6-astra": Usage(input=100_000), "codex-auto-review": Usage(input=1000)}, prompts=1),
            make_record("plain", usage=astra(100_000), prompts=1),
        )
        text = out(prefix=prefix)
        assert (
            "**I spent $2.00+ on AI coding agents this week. In sessions you prompted yourself, a cost router would have saved 80.0% ($0.80) "
            "of the $1.00 that could be compared; across all sessions, including sub-agent and background threads: 80.0% ($0.80) "
            "of the $1.00 that could be compared.**"
        ) in text
        page = html_of(tmp_path, prefix=prefix)
        assert page.count("of the $1.00 that could be compared") == 2 and "they cost" not in page

    def test_a_week_with_no_partly_priced_session_does_not_repeat_the_base_of_the_total(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(make_record("a", usage=astra(100_000), prompts=1), make_record("b", started_at="2026-09-29T11:00:00Z", usage=astra(100_000), prompts=0))
        text = out(prefix=prefix)
        assert "saved 80.0% ($0.80) of the $1.00 they cost; across all sessions, including sub-agent and background threads: 80.0% ($1.60)." in text

    def test_a_slice_that_cost_less_than_the_advised_model_is_no_saving_to_list(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        """``span`` is moved as a whole ($10 on astra in week 39, then $0.25 on a flash-lite model that gpt-6.1-sol cannot beat), so in
        week 40 alone it costs $1.75 more than it did. It is not one of the biggest savings, and it does not count as one."""
        week_39, week_40 = "2026-09-27T23:45:00Z", "2026-09-28T00:00:00Z"
        span = make_record(
            "span", started_at=week_39, prompts=1, usage={"gpt-6-astra": Usage(input=1_000_000), "gemini-3.1-flash-lite": Usage(input=1_000_000)},
            slots={(week_39, "gpt-6-astra"): Usage(input=1_000_000), (week_40, "gemini-3.1-flash-lite"): Usage(input=1_000_000)},
        )
        prefix = world(span, make_record("plain", started_at="2026-09-29T10:00:00Z", usage=astra(100_000), prompts=1))
        d = data("--week-of", "2026-W40", prefix=prefix)
        assert [s["short_id"] for s in d["top_sessions"]] == ["plain"]
        assert d["router"]["switched"] == 2
        assert d["router"]["saved"]["ucents"] == 80_000_000 + (25_000_000 - 200_000_000)  # plain saves $0.80, the slice of span -$1.75
        assert d["prompted"]["saved"] == d["router"]["saved"]

    def test_nothing_is_said_about_prompted_sessions_when_there_is_no_saving(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(make_record("on-sol", usage={"gpt-6.1-sol": Usage(input=100_000)}, prompts=1))
        assert "prompted yourself" not in out(prefix=prefix)

    def test_only_the_five_biggest_savings_are_listed_in_order(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(*(make_record(f"s{n}", started_at=f"2026-09-2{n}T10:00:00Z", usage=astra(100_000 * n), prompts=1) for n in range(1, 8)))
        assert [s["short_id"] for s in data(prefix=prefix)["top_sessions"]] == ["s7", "s6", "s5", "s4", "s3"]


class TestDayLabels:
    """The labels over the bars of the by-day chart: one compact format (three significant digits, five characters at most), so that
    they fit over a column a seventh of a 320 px page, whatever the week's size."""

    @pytest.mark.parametrize(
        ("ucents", "label"),
        [
            (0, "$0.00"), (1, "<$0.01"), (499_999, "<$0.01"), (500_000, "$0.01"), (76_000_000, "$0.76"), (990_000_000, "$9.90"),
            (999_499_999, "$9.99"), (999_500_000, "$10"), (1_000_000_000, "$10"), (6_250_000_000, "$63"), (24_999_999_999, "$250"),
            (99_949_999_999, "$999"), (99_950_000_000, "$1.0k"), (310_000_000_000, "$3.1k"), (999_499_999_999, "$10k"),
            (1_250_000_000_000, "$13k"), (99_949_999_999_999, "$999k"), (99_950_000_000_000, "$1.0M"), (1_234_000_000_000_000, "$12M"),
        ],
    )
    def test_one_compact_format(self, ucents: int, label: str) -> None:
        from agentfare.commands.report import _short_usd

        assert _short_usd(ucents) == label
        assert len(label) <= 6

    def test_a_week_of_very_different_days_reads_in_one_format_and_is_described_in_full(
        self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path
    ) -> None:
        prefix = world(*(
            make_record(f"d{i}", started_at=f"2026-09-{28 + i}T10:00:00Z", usage=astra(tokens), prompts=1)
            for i, tokens in enumerate((76_000, 6_250_000, 25_000_000))  # $0.76, $62.50, $250.00
        ))
        page = html_of(tmp_path, prefix=prefix)
        assert re.findall(r'<span class="v">([^<]*)</span>', page) == ["$0.76", "$63", "$250", "", "", "", ""]
        assert 'title="Mon Sep 28: $0.76"' in page and 'title="Tue Sep 29: $62.50"' in page and 'title="Thu Oct 1: no sessions"' in page
        assert '<span class="n"><span>Sep</span><span>28</span></span>' in page  # the date stacks: "Sep 28" is wider than its column on a phone
        assert "minmax(0, 1fr)" in page  # a long label can never widen a column


class TestHtml:
    def test_one_static_self_contained_page(self, tmp_path: Path) -> None:
        page = html_of(tmp_path)
        tags = parsed(page)
        assert page.startswith("<!doctype html>")
        assert "<script" not in page.lower() and "javascript:" not in page.lower()
        for external in ("http://", "https://", "url(", "//cdn", "@import"):
            assert external not in page
        forbidden = {"script", "link", "img", "iframe", "object", "embed", "form", "video", "audio", "base"}
        assert not tags.tags & forbidden
        assert not [(k, v) for k, v in tags.attrs if k in {"src", "href", "srcset", "action"} or (k or "").startswith("on")]
        assert {"svg", "rect", "style", "main"} <= tags.tags
        assert '<meta name="viewport"' in page and "prefers-color-scheme: light" in page and "@media print" in page
        assert "font-variant-numeric: tabular-nums" in page and "max-width: 1080px" in page

    def test_shows_the_numbers(self, tmp_path: Path) -> None:
        page = html_of(tmp_path)
        for shown in ("$3.63", "16.9%", "$0.61", "$3.02", "You spent", "With the router", "2026-W40", "Sep 28 to Oct 4, 2026",
                      "claude-opus-5-5", "gpt-6.1-sol", "7c8d9e0f", "Project B", "5.2x cheaper", "docs/ROUTING.md",
                      "Generated 2026-09-30 by agentfare 0.1.0"):
            assert shown in page

    def test_the_two_hero_bars_are_to_scale(self, tmp_path: Path) -> None:
        page = html_of(tmp_path)
        # router / actual = 301,700,000 / 363,100,000 = 83.09%: drawn as 83.1% next to a full 100% bar
        assert 'class="fill " width="100%"' in page and 'class="fill routed" width="83.1%"' in page

    def test_the_session_mix_bar_says_what_it_measures_and_keeps_its_counts_on_a_phone(self, tmp_path: Path) -> None:
        page = html_of(tmp_path)
        assert "Bar: share of sessions" in page  # the bar is sized by session count; the amounts beside it are dollars
        assert '<span class="c">2 sessions</span>' in page and '<span class="c">1 session</span>' in page
        assert re.search(r"\.tiers \.c \{[^}]*display: none", page) is None  # the count is moved under the name at 560px, not dropped

    def test_long_names_wrap_instead_of_widening_the_page(self, tmp_path: Path) -> None:
        page = html_of(tmp_path)
        assert "overflow-wrap: anywhere" in page and "grid-template-columns: minmax(0, 1fr)" in page
        assert "grid-template-columns: 1fr;" not in page  # a bare 1fr track never shrinks below its longest unbroken name

    def test_a_wide_amount_is_set_smaller_so_it_fits_a_phone(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path) -> None:
        assert 'class="figure"' in html_of(tmp_path)  # $3.63
        prefix = world(make_record("s1", usage={"gpt-6-astra": Usage(input=1_200_000_000)}, prompts=1))  # $12,000.00
        assert 'class="figure long">$12,000.00<' in html_of(tmp_path, prefix=prefix)

    def test_hostile_names_are_escaped_everywhere(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path) -> None:
        evil_project = '<script>alert("p")</script>'
        evil_model = '<img src=x onerror=alert(1)>&"\''
        prefix = world(
            make_record("s1", project=evil_project, usage={"claude-opus-5-5": Usage(input=100_000, output=10_000)}, prompts=1),
            make_record("s2", started_at="2026-09-29T10:00:00Z", project="ok", usage={evil_model: Usage(input=5_000)}, prompts=1),
        )
        page = html_of(tmp_path, "--show-projects", prefix=prefix)
        assert "<script" not in page.lower() and "<img" not in page.lower()
        assert "&lt;script&gt;alert(&#34;p&#34;)&lt;/script&gt;" in page
        assert "&lt;img src=x onerror=alert(1)&gt;&amp;&#34;&#39;" in page
        tags = parsed(page)
        assert not tags.tags & {"script", "img"} and not [k for k, _ in tags.attrs if k.startswith("on")]

    def test_control_characters_in_a_name_do_not_reach_the_page(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord, tmp_path: Path) -> None:
        prefix = world(make_record("s1", project="a\x1b[31mb‮c", usage=astra(100_000), prompts=1))
        page = html_of(tmp_path, "--show-projects", prefix=prefix)
        assert "\x1b" not in page and "‮" not in page and "a?[31mb?c" in page
        assert "\x1b" not in out("--show-projects", prefix=prefix)

    def test_written_to_the_named_file_and_the_path_is_printed(self, tmp_path: Path) -> None:
        target = tmp_path / "sub.html"
        result = report("--format", "html", "-o", str(target))
        assert result.exit_code == 0 and result.stdout == f"{target.resolve()}\n"
        assert target.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_the_default_file_is_named_after_the_week_in_the_working_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = report("--format", "html", "--week-of", "2026-W40")
        assert result.exit_code == 0 and result.stdout == f"{(tmp_path / 'agentfare-report-2026-W40.html').resolve()}\n"
        assert (tmp_path / "agentfare-report-2026-W40.html").exists()

    def test_the_two_formats_agree_on_the_numbers(self, tmp_path: Path) -> None:
        page, text = html_of(tmp_path), out()
        for shown in re.findall(r"\$\d[\d,]*\.\d\d", text):
            assert shown in page


class TestCommand:
    def test_markdown_goes_to_stdout_unless_a_file_is_named(self, tmp_path: Path) -> None:
        target = tmp_path / "r.md"
        result = report("-o", str(target))
        assert result.exit_code == 0 and result.stdout == f"{target.resolve()}\n"
        assert target.read_text(encoding="utf-8") == out()

    def test_json_can_be_saved(self, tmp_path: Path) -> None:
        target = tmp_path / "r.json"
        assert report("--json", "-o", str(target)).exit_code == 0
        assert json.loads(target.read_text(encoding="utf-8")) == data()

    def test_json_with_the_html_format_is_a_usage_error_and_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "r.html"
        for args in (("--json", "--format", "html"), ("--format", "html", "--json", "-o", str(target)), ("--json", "--format", "html", "-o", "-")):
            result = report(*args)
            assert result.exit_code == 2 and "--json prints JSON, not a page" in result.stderr and result.stdout == ""
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(("fmt", "start"), [("md", "# AgentFare weekly report: 2026-W40"), ("html", "<!doctype html>")])
    def test_a_dash_as_the_output_prints_instead_of_writing_a_file_named_dash(
        self, fmt: str, start: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        result = report("--format", fmt, "-o", "-")
        assert result.exit_code == 0 and list(work.iterdir()) == []  # neither "-" nor the default agentfare-report-<week>.html
        assert result.stdout.startswith(start)
        saved = tmp_path / "saved"
        assert report("--format", fmt, "-o", str(saved)).exit_code == 0
        assert result.stdout == saved.read_text(encoding="utf-8")

    def test_json_to_a_dash_is_printed_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = report("--json", "-o", "-")
        assert result.exit_code == 0 and json.loads(result.stdout) == data() and list(tmp_path.iterdir()) == []

    def test_json_has_stable_keys_and_money_as_cents_and_ucents(self) -> None:
        d = data()
        assert list(d) == [
            "week", "first_day", "last_day", "generated", "version", "state", "in_progress", "show_projects", "sessions", "projects", "agents",
            "models", "tokens", "total", "excluded_tokens", "router", "background", "prompted", "previous", "by_model", "by_agent", "by_day",
            "tiers", "top_sessions", "warnings",
        ]
        assert set(d["by_model"][0]) == {"model", "sessions", "tokens", "cost", "share_bp", "unpriced_tokens"}
        assert set(d["by_agent"][0]) == {"agent", "sessions", "tokens", "cost", "share_bp", "unpriced_tokens"}
        assert set(d["by_day"][0]) == {"day", "sessions", "tokens", "cost", "share_bp", "unpriced_tokens"}

        def moneys(node: Any) -> list[Any]:
            if isinstance(node, dict):
                return [node] if set(node) == {"cents", "ucents"} else [m for v in node.values() for m in moneys(v)]
            return [m for v in node for m in moneys(v)] if isinstance(node, list) else []

        found = moneys(d)
        assert found and all(isinstance(m["cents"], int) and isinstance(m["ucents"], int) for m in found)
        assert not [x for x in re.findall(r'"[a-z_]*(?:cents|ucents)": ([^,}\n]+)', json.dumps(d)) if "." in x]

    def test_output_is_deterministic_and_plain(self) -> None:
        assert out() == out() and "\x1b" not in out() and out("--json") == out("--json")

    def test_an_unwritable_target_is_a_clean_error(self, tmp_path: Path) -> None:
        result = report("--format", "html", "-o", str(tmp_path / "missing-dir" / "r.html"))
        assert result.exit_code == 2 and result.stderr.splitlines()[-1].startswith("error: cannot write ")

    def test_an_unknown_format_is_a_usage_error(self) -> None:
        assert report("--format", "pdf").exit_code == 2


class TestMarkdownNames:
    """A name from the logs is data: GitHub must show it, not act on it (no mention, link, emoji, math, tag or extra cell)."""

    @pytest.mark.parametrize(
        "name",
        ["gpt-6.1-sol", "my_project", "a__b", "models/gemini-2.5-pro", "日本語-app", "модель-2", "v1.2.3", "x", "Project AB", "my project",
         "v1.2/beta", "openai/gpt-4.1", "www", "awww.x", "my.project", "example.com"],  # no scheme, no www. and no path: GitHub leaves these alone
    )
    def test_a_plain_name_is_written_as_is(self, name: str) -> None:
        assert md_text(name) == name

    @pytest.mark.parametrize(
        ("name", "shown"),
        [
            ("_lead", "`_lead`"),
            ("__init__", "`__init__`"),
            ("trail-", "`trail-`"),
            ("a\tb", "`a\tb`"),
            (" a", "` a`"),
            ("a|b", "`a|b`"),
            ("<b>x</b>", "`<b>x</b>`"),
            ("[x](http://e.x)", "`[x](http://e.x)`"),
            ("@cf/meta/llama-3-8b", "`@cf/meta/llama-3-8b`"),
            ("fix#12", "`fix#12`"),
            ("$x$", "`$x$`"),
            ("a:tada:", "`a:tada:`"),
            ("*em*", "`*em*`"),
            ("back\\slash", "`back\\slash`"),
            ("a`b", "``a`b``"),
            ("`code`", "`` `code` ``"),
            ("a``b`", "``` a``b` ```"),
        ],
    )
    def test_anything_else_is_a_code_span(self, name: str, shown: str) -> None:
        assert md_text(name) == shown

    @pytest.mark.parametrize(
        "name",
        ["www.example.com", "WWW.Example.com", "my www.site", "www.x", "example.com/path", "github.com/acme/app", "acme.io/tool",
         "a.b/c", "models/x.ai", "user@example.com", "a.b@c.de", "http://example.com", "https://x.y/z", "ftp://h/f", "gpt-www.x"],
    )
    def test_a_name_that_github_would_link_is_a_code_span(self, name: str) -> None:
        assert md_text(name) == f"`{name}`" and md_text(name, cell=True) == f"`{name}`"

    def test_a_long_plain_name_is_checked_in_linear_time(self) -> None:
        import time

        started = time.perf_counter()
        for name in ("a" * 200_000, "a." * 100_000, "a/" * 100_000, "www" * 70_000):
            md_text(name)
        assert time.perf_counter() - started < 1

    def test_a_linkable_project_name_stays_a_code_span_in_the_report(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(make_record("s1", project="www.evil.test/x", usage=astra(100_000), prompts=1),
                       make_record("s2", project="github.com/acme/app", usage=astra(100_000), prompts=1))
        text = out("--show-projects", prefix=prefix)
        assert "| `www.evil.test/x` |" in text and "| `github.com/acme/app` |" in text
        assert not re.search(r"(?<!`)www\.evil\.test", text)

    def test_a_pipe_is_escaped_only_where_it_would_split_a_table_cell(self) -> None:
        assert md_text("a|b", cell=True) == r"`a\|b`"
        assert md_text("plain", cell=True) == "plain"

    def test_a_hostile_project_name_stays_in_its_table_cell(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(make_record("s1", project="a|b <i> @x $y$", usage=astra(100_000), prompts=1))
        row = next(ln for ln in out("--show-projects", prefix=prefix).splitlines() if ln.startswith("| `s1` |"))
        assert r"| `a\|b <i> @x $y$` |" in row and len(re.split(r"(?<!\\)\|", row.strip("|"))) == 8

    def test_a_hostile_model_name_is_a_code_span_in_the_table_and_the_warning(self, world: Callable[..., tuple[str, ...]], make_record: MakeRecord) -> None:
        prefix = world(make_record("s1", usage={"@cf/meta/llama-3-8b": Usage(input=100_000)}, prompts=1))
        text = out(prefix=prefix)
        assert "| `@cf/meta/llama-3-8b` | 1 | no verified price | - |" in text
        assert "> - 100k tokens: `@cf/meta/llama-3-8b`: not in the price table, its spend is not counted (add it to pricing.yaml)" in text
        assert not re.search(r"(?<!`)@cf", text)

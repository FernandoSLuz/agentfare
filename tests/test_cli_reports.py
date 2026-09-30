"""`agentfare summary`, `sessions` and `models` driven through the CLI.

Two data sources:

* the synthetic logs under tests/fixtures (config: tests/fixtures/config.yaml, timezone UTC), ingested into a
  throwaway AGENTFARE_HOME, with "now" frozen at 2026-09-30T12:00:00Z. Every dollar figure asserted here was
  worked out by hand from the tokens in those logs and the prices in src/agentfare/data/pricing.yaml; the
  arithmetic is in the comments. The hand prices are pinned in the throwaway home as a user price table
  (PINNED_PRICES), so a price edit in pricing.yaml cannot silently change these numbers:
  ``test_pinned_prices_match_the_built_in_table`` is the one test that fails when it does.
* databases built by hand (``hand_config``) for what the fixtures do not contain: unverified and unknown
  models, several ISO weeks, a non-UTC timezone, long session lists.

Money is compared as integer micro-cents (1 cent = 1_000_000 ucents), so the numbers are exact.
"""

from __future__ import annotations

import io
import json
import os
import re
import zoneinfo
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner, Result

from agentfare.cli import app
from agentfare.db import Store
from agentfare.models import SessionRecord, Usage, short_id
from agentfare.pricing import load_pricing, parse_pricing
from agentfare.render import Col, duration, percent, print_table, tokens, usd

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config.yaml"
NOW = "2026-09-30T12:00:00Z"  # a Wednesday, in ISO week 2026-W40 (Mon 2026-09-28 .. Sun 2026-10-04)
COMMANDS = ("summary", "sessions", "models")
runner = CliRunner()

# ---- what the fixtures hold, and what it costs (per 1M tokens; hand arithmetic) ---------------------------
#
# claude-opus-5-5   in 4.00, cache read 0.20, 5m write 5.00, 1h write 8.00 (2x input), out 20.00
#   tokens: in 39,200, read 3,080,000, write 93,000 (5m) + 62,000 (1h), out 42,900
#   39,200 x 4.00 = 156,800;  3,080,000 x 0.20 = 616,000;  93,000 x 5.00 = 465,000;
#   62,000 x 8.00 = 496,000;  42,900 x 20.00 = 858,000   -> 2,591,800 / 1e6 = $2.5918
OPUS = 259_180_000
# claude-haiku-4-5 (logged as claude-haiku-4-5-20251001)   in 1.00, read 0.10, 5m 1.25, 1h 2.00, out 5.00
#   tokens: in 4,700, read 120,000, write 6,000 (5m) + 12,000 (1h), out 2,200
#   4,700 + 12,000 + 7,500 + 24,000 + 11,000 = 59,200 / 1e6 = $0.0592
HAIKU = 5_920_000
# gpt-6-astra (codex)   in 10.00, read 1.00, out 50.00
#   tokens: in 40,000, read 60,000, out 6,000:  400,000 + 60,000 + 300,000 = 760,000 / 1e6 = $0.76
ASTRA = 76_000_000
# gemini-2.5-pro (gemini)   in 1.25, read 0.125, out 10.00
#   tokens: in 92,000, read 80,000, out 9,500:  115,000 + 10,000 + 95,000 = 220,000 / 1e6 = $0.22
GEMINI = 22_000_000

CLAUDE_SESSION = OPUS + HAIKU  # $2.6510, one Claude session (2026-09-28, project acme-shop) that used both models
TOTAL = CLAUDE_SESSION + ASTRA + GEMINI  # $3.631 = 363,100,000 ucents

# tokens (input, cache read, cache write incl. 1h, output) per group
OPUS_TOKENS = {"input": 39_200, "cache_read": 3_080_000, "cache_write": 155_000, "output": 42_900, "total": 3_317_100}
HAIKU_TOKENS = {"input": 4_700, "cache_read": 120_000, "cache_write": 18_000, "output": 2_200, "total": 144_900}
ASTRA_TOKENS = {"input": 40_000, "cache_read": 60_000, "cache_write": 0, "output": 6_000, "total": 106_000}
GEMINI_TOKENS = {"input": 92_000, "cache_read": 80_000, "cache_write": 0, "output": 9_500, "total": 181_500}

# Basis-point shares of TOTAL, rounded half up: 259.18 / 363.1 = 71.38%, 5.92 / 363.1 = 1.63%,
# 76 / 363.1 = 20.93%, 22 / 363.1 = 6.06%; the claude session 265.1 / 363.1 = 73.01%
SHARE = {"opus": 7138, "haiku": 163, "astra": 2093, "gemini": 606, "claude session": 7301}


# The prices the hand arithmetic uses (USD per 1M tokens), written to <AGENTFARE_HOME>/pricing.yaml, where the
# CLI looks for the user's own price table before the built-in one.
PINNED_PRICES = """\
schema: 1
models:
  claude-opus-5-5:   {provider: anthropic, input: 4.0, cached_input: 0.20, cache_write: 5.0, cache_write_1h: 8.0, output: 20.0}
  claude-haiku-4-5:  {provider: anthropic, input: 1.0, cached_input: 0.10, cache_write: 1.25, cache_write_1h: 2.0, output: 5.0}
  claude-sonnet-5-5: {provider: anthropic, input: 2.0, cached_input: 0.20, cache_write: 2.5, cache_write_1h: 4.0, output: 10.0}
  gpt-6-astra:       {provider: openai, input: 10.0, cached_input: 1.0, cache_write: 12.5, output: 50.0}
  gpt-6.1-sol:       {provider: openai, input: 2.0, cached_input: 0.10, cache_write: 2.5, output: 10.0}
  gemini-2.5-pro:    {provider: google, input: 1.25, cached_input: 0.125, output: 10.0}
  gemini-2.5-flash:  {provider: google, input: 0.30, cached_input: 0.03, output: 2.50}
  gemini-3-flash:    {provider: google, input: unverified, cached_input: unverified, output: unverified}
aliases:
  claude-haiku-4-5-20251001: claude-haiku-4-5
"""


@pytest.fixture(autouse=True)
def pinned_prices(isolated_machine: None) -> None:
    (Path(os.environ["AGENTFARE_HOME"]) / "pricing.yaml").write_text(PINNED_PRICES)


# ---- helpers ------------------------------------------------------------------------------------------
def run(
    *args: str, columns: int = 140, now: str | None = NOW, config: Path = FIXTURE_CONFIG, ingest: bool = True,
    env: dict[str, str | None] | None = None,
) -> Result:
    """Invoke the CLI as if piped: no terminal, no colour, the given console width."""
    environment: dict[str, str | None] = {
        "COLUMNS": str(columns), "AGENTFARE_NOW": now, "FORCE_COLOR": None, "NO_COLOR": None,
        "TTY_COMPATIBLE": None, "TTY_INTERACTIVE": None, **(env or {}),
    }
    argv = ["--config", str(config), *([] if ingest else ["--no-ingest"]), *args]
    return runner.invoke(app, argv, env=environment)


def data(*args: str, **kw: Any) -> dict[str, Any]:
    """Run with ``--json``, require success, and parse stdout (stderr may carry the ingest note)."""
    result = run(*args, "--json", **kw)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def lines(result: Result) -> list[str]:
    """stdout, one entry per line, runs of blanks squeezed so a row reads ``a b c`` whatever the alignment."""
    return [" ".join(line.split()) for line in result.stdout.splitlines()]


def left_out_note(out: list[str]) -> str:
    """The dim note that names the columns left out, from its first line to the one that ends it (it may wrap over several)."""
    start = next((i for i, line in enumerate(out) if "left out to fit" in line), None)
    if start is None:
        return ""
    end = next(i for i in range(start, len(out)) if "--json has everything" in out[i])
    return " ".join(out[start:end + 1])


def ucents(entry: dict[str, Any]) -> int | None:
    """The priced spend of a report entry; None when it has no figure."""
    return entry["cost"]["ucents"] if entry["cost"] else None


@pytest.fixture
def hand_config(tmp_path: Path) -> Callable[..., Path]:
    """``hand_config(records, timezone="UTC")``: a database holding exactly these records, and a config for it."""

    def build(records: list[SessionRecord], timezone: str = "UTC") -> Path:
        db = tmp_path / "hand.db"
        with Store(db) as store:
            for rec in records:
                store.upsert_session(rec)
        cfg = tmp_path / "hand.yaml"
        cfg.write_text(f"timezone: {timezone}\ndatabase: {db}\nsources: {{claude: [], codex: [], gemini: []}}\n")
        return cfg

    return build


def hand(hand_config: Callable[..., Path], records: list[SessionRecord], timezone: str = "UTC") -> dict[str, Any]:
    return {"config": hand_config(records, timezone), "ingest": False}


# ---- the hand numbers ---------------------------------------------------------------------------------
def test_pinned_prices_match_the_built_in_table() -> None:
    """Every price the hand numbers use is what pricing.yaml says today; if not, redo the arithmetic and update both."""
    pinned, built_in = parse_pricing(yaml.safe_load(PINNED_PRICES)), load_pricing()
    for model, mine in pinned.models.items():
        theirs = built_in.get(model)
        assert theirs is not None, f"{model} left the built-in table"
        assert theirs.priced == mine.priced, model
        if mine.priced:
            fields = ("input", "cached_rate", "cache_write_rate", "output")
            assert [getattr(theirs, f) for f in fields] == [getattr(mine, f) for f in fields], model
    assert built_in.resolve("claude-haiku-4-5-20251001") == "claude-haiku-4-5"  # the alias the Claude fixture logs


def test_one_hour_cache_writes_are_priced_at_twice_input() -> None:
    """The Claude fixture (like real Claude logs) has 1-hour cache writes, which Anthropic bills at 2x the input price.

    Without ``cache_write_1h`` in pricing.yaml they are billed at the 5-minute rate and every Claude figure is low.
    """
    built_in = load_pricing()
    for model in ("claude-opus-5-5", "claude-haiku-4-5", "claude-sonnet-5-5"):
        price = built_in.priced(model)
        assert price is not None and price.cache_write_1h_rate == 2 * price.input, model


def test_fixture_tokens_are_what_the_hand_numbers_assume() -> None:
    by_model = {(m["agent"], m["model"]): m["tokens"] for m in data("summary")["by_model"]}
    assert by_model == {
        ("claude", "claude-opus-5-5"): OPUS_TOKENS, ("claude", "claude-haiku-4-5"): HAIKU_TOKENS,
        ("codex", "gpt-6-astra"): ASTRA_TOKENS, ("gemini", "gemini-2.5-pro"): GEMINI_TOKENS,
    }


# ---- summary ------------------------------------------------------------------------------------------
def test_summary_json_exact() -> None:
    s = data("summary")
    assert list(s) == [
        "period", "filters", "sessions", "projects", "models", "tokens", "total", "excluded_tokens", "by_model",
        "by_project", "by_day", "by_week", "warnings",
    ]
    assert s["period"] == {"label": "all time", "since": None, "until": None}
    assert s["filters"] == {"project": None, "agent": None}
    assert (s["sessions"], s["projects"], s["models"]) == (3, 3, 4)
    assert s["total"] == {"cents": 363, "ucents": TOTAL}  # 363.1 cents, rounded half up
    assert s["excluded_tokens"] == 0 and s["warnings"] == []
    assert s["tokens"] == {"input": 175_900, "cache_read": 3_340_000, "cache_write": 173_000, "output": 60_600,
                           "total": 3_749_500}

    # by model: ranked by spend, the alias folded into claude-haiku-4-5
    assert [(m["agent"], m["model"], m["provider"], m["status"]) for m in s["by_model"]] == [
        ("claude", "claude-opus-5-5", "anthropic", "priced"), ("codex", "gpt-6-astra", "openai", "priced"),
        ("gemini", "gemini-2.5-pro", "google", "priced"), ("claude", "claude-haiku-4-5", "anthropic", "priced"),
    ]
    assert [ucents(m) for m in s["by_model"]] == [OPUS, ASTRA, GEMINI, HAIKU]
    assert [m["share_bp"] for m in s["by_model"]] == [SHARE["opus"], SHARE["astra"], SHARE["gemini"], SHARE["haiku"]]
    assert [m["sessions"] for m in s["by_model"]] == [1, 1, 1, 1]
    assert s["by_model"][0]["cost"] == {"cents": 259, "ucents": OPUS}  # 259.18 cents

    # by project and by day: the two-model Claude session is one project and one day
    assert [(p["project"], ucents(p)) for p in s["by_project"]] == [
        ("acme-shop", CLAUDE_SESSION), ("fake-repo", ASTRA), ("demo-notes-app", GEMINI)]
    assert [(d["day"], ucents(d)) for d in s["by_day"]] == [
        ("2026-09-28", CLAUDE_SESSION), ("2026-09-29", ASTRA), ("2026-09-30", GEMINI)]
    assert [(w["week"], ucents(w), w["sessions"]) for w in s["by_week"]] == [("2026-W40", TOTAL, 3)]
    assert s["by_project"][0]["tokens"] == {"input": 43_900, "cache_read": 3_200_000, "cache_write": 173_000,
                                            "output": 45_100, "total": 3_462_000}
    assert s["by_project"][0]["share_bp"] == SHARE["claude session"]


def test_summary_tables_match_the_hand_numbers() -> None:
    out = lines(run("summary"))
    assert "AgentFare summary: all time" in out
    assert "3 sessions, 3 projects, 4 models, total spend $3.63" in out
    # Agent Model Sess Input CacheRead CacheWrite Output Cost Share
    assert "claude claude-opus-5-5 1 39.2k 3.1M 155k 42.9k $2.59 71.4%" in out
    assert "codex gpt-6-astra 1 40.0k 60.0k 0 6.0k $0.76 20.9%" in out
    assert "gemini gemini-2.5-pro 1 92.0k 80.0k 0 9.5k $0.22 6.1%" in out
    assert "claude claude-haiku-4-5 1 4.7k 120k 18.0k 2.2k $0.0592 1.6%" in out  # under $1: sub-cent precision shows
    assert "Total 3 176k 3.3M 173k 60.6k $3.63 100%" in out
    assert "acme-shop 1 43.9k 3.2M 173k 45.1k $2.65 73.0%" in out
    assert "fake-repo 1 40.0k 60.0k 0 6.0k $0.76 20.9%" in out
    assert "demo-notes-app 1 92.0k 80.0k 0 9.5k $0.22 6.1%" in out
    # by day, with the bar proportional to the dearest day (10 cells): 76/265.1 -> 3 cells, 22/265.1 -> 1
    assert "2026-09-28 1 43.9k 3.2M 173k 45.1k $2.65 73.0% ██████████" in out
    assert "2026-09-29 1 40.0k 60.0k 0 6.0k $0.76 20.9% ███" in out
    assert "2026-09-30 1 92.0k 80.0k 0 9.5k $0.22 6.1% █" in out
    assert not any("ISO week" in line for line in out)  # one week only: no rollup
    assert not any("warning" in line for line in out)
    note = "a session that used several models counts once per model; the Total row counts it once"
    assert out.index(note) == out.index("claude claude-haiku-4-5 1 4.7k 120k 18.0k 2.2k $0.0592 1.6%") + 3  # under the model table
    assert note not in lines(run("summary", "--agent", "codex"))  # one model per session: nothing to explain
    assert not any("spent inside the period" in line for line in out)  # and all time has no edge to explain


def test_summary_week_is_the_current_iso_week() -> None:
    s = data("summary", "--week")
    assert s["period"]["label"] == "week 2026-09-28 .. 2026-10-04"
    assert (s["period"]["since"], s["period"]["until"]) == ("2026-09-28T00:00:00Z", "2026-10-05T00:00:00Z")
    assert s["total"]["ucents"] == TOTAL and s["sessions"] == 3
    assert "AgentFare summary: week 2026-09-28 .. 2026-10-04" in lines(run("summary", "--week"))
    # a week later the same logs are last week's
    assert data("summary", "--week", now="2026-10-07T12:00:00Z")["sessions"] == 0
    assert data("summary", "--week-of", "2026-W40")["total"]["ucents"] == TOTAL
    assert data("summary", "--week-of", "2026-w40")["total"]["ucents"] == TOTAL  # the label is case-insensitive
    assert data("summary", "--week-of", "2026-W39")["sessions"] == 0


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--since", "2026-09-29"], ASTRA + GEMINI),  # the first day is included
        (["--until", "2026-09-29"], CLAUDE_SESSION + ASTRA),  # so is the last: the codex session starts at 10:00 that day
        (["--since", "2026-09-29", "--until", "2026-09-29"], ASTRA),
        (["--until", "2026-09-28"], CLAUDE_SESSION),
        (["--since", "2026-09-30", "--until", "2026-09-30"], GEMINI),
        (["--since", "2026-09-28", "--until", "2026-09-30"], TOTAL),
        (["--since", "2026-10-01"], 0),
        (["--until", "2026-09-27"], 0),
    ],
)
def test_summary_since_until_edge_days(args: list[str], expected: int) -> None:
    assert data("summary", *args)["total"]["ucents"] == expected


def test_summary_project_and_agent_filters() -> None:
    s = data("summary", "--project", "ACME-SHOP")  # case-insensitive
    assert (s["sessions"], s["total"]["ucents"], s["filters"]) == (1, CLAUDE_SESSION, {"project": "ACME-SHOP", "agent": None})
    assert [m["model"] for m in s["by_model"]] == ["claude-opus-5-5", "claude-haiku-4-5"]
    assert data("summary", "--agent", "codex")["total"]["ucents"] == ASTRA
    assert data("summary", "--agent", "Gemini")["total"]["ucents"] == GEMINI
    assert data("summary", "--agent", "claude", "--project", "fake-repo")["sessions"] == 0
    scope = "AgentFare summary: week 2026-09-28 .. 2026-10-04, project acme-shop, agent claude"
    assert scope in lines(run("summary", "--week", "--project", "acme-shop", "--agent", "claude"))


def test_summary_totals_never_drift_between_tables() -> None:
    s = data("summary")
    for table in ("by_model", "by_project", "by_day", "by_week"):
        assert sum(ucents(e) or 0 for e in s[table]) == s["total"]["ucents"], table
        assert sum(e["tokens"]["total"] for e in s[table]) == s["tokens"]["total"], table


# ---- sessions -----------------------------------------------------------------------------------------
def test_sessions_json_exact() -> None:
    s = data("sessions")
    assert list(s) == ["period", "filters", "count", "shown", "tokens", "total", "excluded_tokens", "sessions", "warnings"]
    assert (s["count"], s["shown"], s["total"]) == (3, 3, {"cents": 363, "ucents": TOTAL})
    assert s["tokens"] == data("summary")["tokens"]  # the tokens of every matching session, listed or not
    gemini, codex, claude = s["sessions"]  # newest first
    assert (gemini["agent"], gemini["date"], gemini["short_id"], gemini["project"]) == ("gemini", "2026-09-30", "4a6e8f10", "demo-notes-app")
    assert (codex["agent"], codex["date"], codex["short_id"], codex["project"]) == ("codex", "2026-09-29", "7c8d9e0f", "fake-repo")
    assert (claude["agent"], claude["date"], claude["short_id"], claude["project"]) == ("claude", "2026-09-28", "a1c7e402", "acme-shop")
    assert [ucents(e) for e in (gemini, codex, claude)] == [GEMINI, ASTRA, CLAUDE_SESSION]
    assert claude["session_id"] == "2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402"
    assert (claude["primary_model"], claude["other_models"]) == ("claude-opus-5-5", 1)
    assert [(m["model"], ucents(m)) for m in claude["models"]] == [
        ("claude-opus-5-5", OPUS), ("claude-haiku-4-5", HAIKU)]
    assert claude["tokens"]["total"] == OPUS_TOKENS["total"] + HAIKU_TOKENS["total"]  # 3,462,000
    assert (gemini["primary_model"], gemini["other_models"], gemini["tokens"]) == ("gemini-2.5-pro", 0, GEMINI_TOKENS)
    assert (codex["active_seconds"], codex["prompts"]) == (600, 1)
    assert (claude["active_seconds"], claude["wall_seconds"], claude["prompts"]) == (5930, 8840, 5)
    assert all(e["unpriced_tokens"] == 0 for e in s["sessions"]) and s["warnings"] == []


def test_sessions_table_matches_the_hand_numbers() -> None:
    out = lines(run("sessions"))
    gemini = next(line for line in out if line.startswith("09-30 gemini"))
    codex = next(line for line in out if line.startswith("09-29 codex"))
    claude = next(line for line in out if line.startswith("09-28 claude"))
    assert gemini.startswith("09-30 gemini 4a6e8f10 demo-notes-app gemini-2.5-pro 9m55s 3 ")  # the year is dropped: one year
    assert gemini.endswith(" 182k $0.22")  # 181,500 tokens
    assert codex.startswith("09-29 codex 7c8d9e0f fake-repo gpt-6-astra 10m00s 1 ") and codex.endswith(" 106k $0.76")
    # the primary model and "+1" for the other one; the cost sums both models; 5930 s of activity; 5 prompts
    assert claude.startswith("09-28 claude a1c7e402 acme-shop claude-opus-5-5 +1 1h38m 5 ")
    assert claude.endswith(" 3.5M $2.65")  # 3,462,000 tokens, $2.6510
    assert "showing 3 of 3 sessions, total $3.63" in out


def test_sessions_pass_the_stored_counters_through(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> None:
    rec = make_record("counters", started_at="2026-09-29T10:00:00Z", wall_seconds=4000, active_seconds=3725,
                      prompts=7, tool_calls=123, files_touched=4, error_retries=2, turns=50,
                      usage={"claude-sonnet-5-5": Usage(input=1_000_000)})
    kw = hand(hand_config, [rec])
    entry = data("sessions", **kw)["sessions"][0]
    assert (entry["prompts"], entry["tool_calls"], entry["files_touched"], entry["error_retries"], entry["turns"]) == (7, 123, 4, 2, 50)
    assert (entry["active_seconds"], entry["wall_seconds"]) == (3725, 4000)
    # 1,000,000 x $2.00 / 1e6 = $2.00
    assert ucents(entry) == 200_000_000
    row = next(line for line in lines(run("sessions", **kw)) if line.startswith("09-29 claude"))
    assert row.endswith("1h02m 7 123 4 2 1.0M $2.00")


@pytest.fixture
def five_sessions(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> dict[str, Any]:
    """Five sessions on 09-25 .. 09-29 costing $6, $2, $11, $4, $8 (sonnet-5-5 input at $2.00 per 1M)."""
    sonnet = "claude-sonnet-5-5"
    recs = [
        make_record("session-0001-abcdefgh", started_at="2026-09-25T10:00:00Z", usage={sonnet: Usage(input=3_000_000)}),
        make_record("session-0002-bcdefghi", started_at="2026-09-26T10:00:00Z", usage={sonnet: Usage(input=1_000_000)}),
        # the two-model one: $10.00 of sonnet input + 100,000 gpt-6.1-sol output at $10.00 per 1M = $1.00
        make_record("session-0003-cdefghij", started_at="2026-09-27T10:00:00Z", agent="codex",
                    usage={sonnet: Usage(input=5_000_000), "gpt-6.1-sol": Usage(output=100_000)}),
        make_record("session-0004-defghijk", started_at="2026-09-28T10:00:00Z", usage={sonnet: Usage(input=2_000_000)}),
        make_record("session-0005-efghijkl", started_at="2026-09-29T10:00:00Z", usage={sonnet: Usage(input=4_000_000)}),
    ]
    return hand(hand_config, recs)


def test_sessions_limit_all_and_sort(five_sessions: dict[str, Any]) -> None:
    def dollars(report: dict[str, Any]) -> list[int]:
        return [e["cost"]["ucents"] // 100_000_000 for e in report["sessions"]]

    newest = data("sessions", "--limit", "2", **five_sessions)
    assert (newest["count"], newest["shown"], newest["total"]["ucents"]) == (5, 2, 3_100_000_000)  # 6+2+11+4+8 = $31
    assert [e["date"] for e in newest["sessions"]] == ["2026-09-29", "2026-09-28"] and dollars(newest) == [8, 4]
    dearest = data("sessions", "--limit", "2", "--sort", "cost", **five_sessions)
    assert dollars(dearest) == [11, 8] and dearest["shown"] == 2 and dearest["total"]["ucents"] == 3_100_000_000
    everything = data("sessions", "--all", "--sort", "cost", **five_sessions)
    assert dollars(everything) == [11, 8, 6, 4, 2] and everything["shown"] == 5
    assert [e["short_id"] for e in everything["sessions"]] == ["cdefghij", "efghijkl", "abcdefgh", "defghijk", "bcdefghi"]
    assert data("sessions", "--all", "--limit", "1", **five_sessions)["shown"] == 5  # --all wins
    assert data("sessions", **five_sessions)["shown"] == 5  # the default limit is 30

    two_models = everything["sessions"][0]  # gpt-6.1-sol wrote the most output, sonnet-5-5 is the other one
    assert (two_models["primary_model"], two_models["other_models"]) == ("gpt-6.1-sol", 1)

    footer = lines(run("sessions", "--limit", "2", **five_sessions))
    assert "showing 2 of 5 sessions ($12.00 of $31.00)" in footer  # the money of the rows shown, and of all of them
    assert "showing 5 of 5 sessions, total $31.00" in lines(run("sessions", "--all", **five_sessions))
    rows = [line for line in lines(run("sessions", "--sort", "cost", "--limit", "1", **five_sessions)) if line.startswith("09-27")]
    assert len(rows) == 1 and rows[0].startswith("09-27 codex cdefghij proj gpt-6.1-sol +1 ") and rows[0].endswith(" $11.00")


def test_sessions_filters_apply_before_the_limit() -> None:
    s = data("sessions", "--agent", "codex", "--limit", "1")
    assert (s["count"], s["shown"], [e["agent"] for e in s["sessions"]], s["total"]["ucents"]) == (1, 1, ["codex"], ASTRA)
    assert data("sessions", "--since", "2026-09-29")["count"] == 2
    assert data("sessions", "--until", "2026-09-28", "--project", "acme-shop")["sessions"][0]["agent"] == "claude"


# ---- models -------------------------------------------------------------------------------------------
def test_models_json_exact() -> None:
    m = data("models")
    assert list(m) == ["period", "filters", "sessions", "tokens", "total", "excluded_tokens", "models", "warnings"]
    assert (m["sessions"], m["total"]["ucents"]) == (3, TOTAL)
    assert m["tokens"] == data("summary")["tokens"]
    assert [(e["rank"], e["model"], e["provider"], e["status"]) for e in m["models"]] == [
        (1, "claude-opus-5-5", "anthropic", "priced"), (2, "gpt-6-astra", "openai", "priced"),
        (3, "gemini-2.5-pro", "google", "priced"), (4, "claude-haiku-4-5", "anthropic", "priced"),
    ]
    assert [ucents(e) for e in m["models"]] == [OPUS, ASTRA, GEMINI, HAIKU]
    assert [e["tokens"]["total"] for e in m["models"]] == [3_317_100, 106_000, 181_500, 144_900]
    # blended $ per 1M tokens = cost x 1e6 / tokens, in ucents, rounded half up:
    #   opus  259,180,000 x 1e6 / 3,317,100 = 78,134,515.09  ($0.7813)   astra  76,000,000 x 1e6 / 106,000 = 716,981,132.08
    #   gemini 22,000,000 x 1e6 / 181,500 = 121,212,121.21   haiku   5,920,000 x 1e6 / 144,900 = 40,855,762.59
    assert [e["blended_per_mtok"]["ucents"] for e in m["models"]] == [78_134_515, 716_981_132, 121_212_121, 40_855_763]
    assert m["models"][0]["blended_per_mtok"]["cents"] == 78  # $0.78134515 per 1M tokens
    # list prices per 1M: $4/$20, $10/$50, $1.25/$10, $1/$5 (1 USD = 100_000_000 ucents)
    lists = [(e["list_price_per_mtok"]["input"]["ucents"], e["list_price_per_mtok"]["output"]["ucents"]) for e in m["models"]]
    assert lists == [(400_000_000, 2_000_000_000), (1_000_000_000, 5_000_000_000), (125_000_000, 1_000_000_000),
                     (100_000_000, 500_000_000)]


def test_models_table_matches_the_hand_numbers() -> None:
    out = lines(run("models"))
    assert "4 models, 3 sessions, total spend $3.63" in out
    assert "1 claude-opus-5-5 anthropic 1 3.3M $2.59 71.4% $0.7813 $4.00 $20.00" in out
    assert "2 gpt-6-astra openai 1 106k $0.76 20.9% $7.17 $10.00 $50.00" in out
    assert "3 gemini-2.5-pro google 1 182k $0.22 6.1% $1.21 $1.25 $10.00" in out
    assert "4 claude-haiku-4-5 anthropic 1 145k $0.0592 1.6% $0.4086 $1.00 $5.00" in out


def test_models_filters() -> None:
    m = data("models", "--agent", "claude")
    assert [(e["rank"], e["model"]) for e in m["models"]] == [(1, "claude-opus-5-5"), (2, "claude-haiku-4-5")]
    assert data("models", "--week-of", "2026-W39")["models"] == []


# ---- unverified and unknown prices --------------------------------------------------------------------
@pytest.fixture
def unpriced(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> dict[str, Any]:
    """Priced and unpriced usage side by side (gemini-3-flash is unverified in the built-in table, the other unknown).

    claude-sonnet-5-5   1,000,000 in + 100,000 out:  2,000,000 + 1,000,000 = 3,000,000 / 1e6 = $3.00  (alpha, 09-29)
    gemini-2.5-flash    1,000,000 in x $0.30 = 300,000 / 1e6 = $0.30                                   (alpha, 09-30)
    gemini-3-flash      1,500,000 in + 500,000 out: no verified price, 2,000,000 tokens left out
    mystery-model-9     250,000 in + 50,000 cache read: not in the table, 300,000 tokens left out      (beta, 09-30)
    """
    recs = [
        make_record("s1", agent="claude", project="alpha", started_at="2026-09-29T10:00:00Z",
                    usage={"claude-sonnet-5-5": Usage(input=1_000_000, output=100_000)}),
        make_record("s2", agent="gemini", project="alpha", started_at="2026-09-30T09:00:00Z",
                    usage={"gemini-3-flash": Usage(input=1_500_000, output=500_000),
                           "gemini-2.5-flash": Usage(input=1_000_000)}),
        make_record("s3", agent="codex", project="beta", started_at="2026-09-30T10:00:00Z",
                    usage={"mystery-model-9": Usage(input=250_000, cache_read=50_000)}),
    ]
    return hand(hand_config, recs)


PRICED = 300_000_000 + 30_000_000  # $3.30


def test_unpriced_models_are_excluded_from_totals_and_named(unpriced: dict[str, Any]) -> None:
    s = data("summary", **unpriced)
    assert s["total"] == {"cents": 330, "ucents": PRICED}
    assert s["excluded_tokens"] == 2_300_000
    assert s["tokens"]["total"] == 1_100_000 + 3_000_000 + 300_000  # every session's tokens, priced or not
    assert [(w["model"], w["status"], w["excluded_tokens"]) for w in s["warnings"]] == [
        ("gemini-3-flash", "unverified", 2_000_000), ("mystery-model-9", "unknown", 300_000)]
    assert "unverified" in s["warnings"][0]["message"] and "not in the price table" in s["warnings"][1]["message"]
    by_model = {m["model"]: m for m in s["by_model"]}
    assert (by_model["gemini-3-flash"]["cost"], by_model["gemini-3-flash"]["share_bp"]) == (None, None)  # no figure, not 0
    assert by_model["gemini-3-flash"]["status"] == "unverified" and by_model["mystery-model-9"]["status"] == "unknown"
    assert [ucents(m) for m in s["by_model"]] == [300_000_000, 30_000_000, None, None]  # unpriced models come last
    # a project with some priced usage keeps that spend; one with none has no figure
    alpha, beta = ({p["project"]: p for p in s["by_project"]})["alpha"], ({p["project"]: p for p in s["by_project"]})["beta"]
    assert (ucents(alpha), alpha["unpriced_tokens"]) == (PRICED, 2_000_000)
    assert (beta["cost"], beta["unpriced_tokens"]) == (None, 300_000)
    for table in ("by_model", "by_project", "by_day", "by_week"):
        assert sum(ucents(e) or 0 for e in s[table]) == PRICED, table


def test_unpriced_models_summary_text(unpriced: dict[str, Any]) -> None:
    out = lines(run("summary", **unpriced))
    assert "3 sessions, 2 projects, 4 models, total spend $3.30+" in out  # + : also holds unpriced tokens
    assert "claude claude-sonnet-5-5 1 1.0M 0 0 100k $3.00 90.9%" in out
    assert "gemini gemini-2.5-flash 1 1.0M 0 0 0 $0.30 9.1%" in out
    assert "gemini gemini-3-flash 1 1.5M 0 0 500k unverified -" in out  # after the priced ones, marked
    assert "codex mystery-model-9 1 250k 50.0k 0 0 unknown -" in out
    assert "alpha 2 3.5M 0 0 600k $3.30+ 100%" in out
    assert "beta 1 250k 50.0k 0 0 - -" in out
    assert "warning: 2 models without a verified price, 2.3M tokens are not counted in any dollar figure" in out
    assert "! 2.0M tokens gemini-3-flash: price is unverified, its spend is not counted (see pricing.yaml)" in out
    assert "! 300k tokens mystery-model-9: not in the price table, its spend is not counted (add it to pricing.yaml)" in out
    assert "+ also holds tokens without a verified price, which the cost leaves out" in out


def test_unpriced_models_sessions_and_models_commands(unpriced: dict[str, Any]) -> None:
    s = data("sessions", **unpriced)
    by_id = {e["session_id"]: e for e in s["sessions"]}
    assert (ucents(by_id["s1"]), by_id["s1"]["unpriced_tokens"]) == (300_000_000, 0)
    assert (ucents(by_id["s2"]), by_id["s2"]["unpriced_tokens"]) == (30_000_000, 2_000_000)  # partial: only the priced part
    assert (by_id["s3"]["cost"], by_id["s3"]["unpriced_tokens"]) == (None, 300_000)
    assert (by_id["s2"]["primary_model"], by_id["s2"]["other_models"]) == ("gemini-3-flash", 1)
    assert s["total"]["ucents"] == PRICED and [w["model"] for w in s["warnings"]] == ["gemini-3-flash", "mystery-model-9"]
    out = lines(run("sessions", **unpriced))
    assert next(line for line in out if line.startswith("09-30 codex")).endswith(" 300k -")
    assert next(line for line in out if line.startswith("09-30 gemini")).endswith(" 3.0M $0.30+")
    assert next(line for line in out if line.startswith("09-29 claude")).endswith(" 1.1M $3.00")
    assert "showing 3 of 3 sessions, total $3.30+" in out  # + : the total leaves unpriced tokens out
    assert any(line.startswith("warning: 2 models without a verified price") for line in out)

    m = data("models", **unpriced)
    assert [(e["rank"], e["model"], e["status"], ucents(e)) for e in m["models"]] == [
        (1, "claude-sonnet-5-5", "priced", 300_000_000), (2, "gemini-2.5-flash", "priced", 30_000_000),
        (None, "gemini-3-flash", "unverified", None), (None, "mystery-model-9", "unknown", None)]
    assert m["models"][2]["blended_per_mtok"] is None and m["models"][2]["list_price_per_mtok"] is None
    assert m["models"][2]["provider"] == "google" and m["models"][3]["provider"] == ""
    out = lines(run("models", **unpriced))
    assert "1 claude-sonnet-5-5 anthropic 1 1.1M $3.00 90.9% $2.73 $2.00 $10.00" in out  # $3.00 / 1.1M tokens
    assert "- gemini-3-flash google 1 2.0M unverified - - - -" in out
    assert "- mystery-model-9 - 1 300k unknown - - - -" in out
    assert any("gemini-3-flash: price is unverified" in line for line in out)


def test_nothing_priced_means_no_figure_not_zero(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> None:
    rec = make_record("u1", agent="gemini", usage={"gemini-3-flash": Usage(input=1_500_000, output=500_000)})
    kw = hand(hand_config, [rec])
    s = data("summary", **kw)
    assert s["total"] == {"cents": 0, "ucents": 0} and s["excluded_tokens"] == 2_000_000
    assert (s["by_project"][0]["cost"], s["by_day"][0]["cost"], s["by_week"][0]["cost"]) == (None, None, None)
    out = lines(run("summary", **kw))
    assert "1 session, 1 project, 1 model, total spend n/a (nothing priced)" in out
    assert "gemini gemini-3-flash 1 1.5M 0 0 500k unverified -" in out
    assert "Total 1 1.5M 0 0 500k - -" in out
    assert "warning: 1 model without a verified price, 2.0M tokens are not counted in any dollar figure" in out
    assert not any(line.startswith("+ also holds") for line in out)  # no row mixes priced and unpriced tokens


def test_partly_unverified_prices_are_warned_about_but_counted(
    hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord], tmp_path: Path,
) -> None:
    """A model with only its cache price unverified is priced (cache reads at the input rate) and still warned about."""
    table_text = "schema: 1\nmodels:\n  m1:\n    provider: acme\n    input: 2.0\n    cached_input: unverified\n    output: 10.0\n"
    cfg = hand_config([make_record("p1", usage={"m1": Usage(input=1_000_000, cache_read=1_000_000)})])
    cfg.write_text(cfg.read_text() + f"pricing_file: {tmp_path / 'prices.yaml'}\n")
    (tmp_path / "prices.yaml").write_text(table_text)
    s = data("summary", config=cfg, ingest=False)
    # 1,000,000 x 2.00 + 1,000,000 x 2.00 (cache read billed at the input rate) = 4,000,000 / 1e6 = $4.00
    assert s["total"]["ucents"] == 400_000_000 and s["excluded_tokens"] == 0
    assert [(w["model"], w["status"], w["excluded_tokens"]) for w in s["warnings"]] == [("m1", "partial", 0)]
    out = lines(run("summary", config=cfg, ingest=False))
    assert "warning: some prices are only partly verified" in out


# ---- weeks, timezones, sub-cent money -----------------------------------------------------------------
@pytest.fixture
def three_weeks(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> dict[str, Any]:
    """One session in each of 2026-W38, W39 and W40, with token counts that give sub-cent amounts.

    gpt-6.1-sol (in 2.00, read 0.10, out 10.00) 09-15: 123,457 x 2.00 = 246,914; 7,654,321 x 0.10 = 765,432.1;
        98,765 x 10.00 = 987,650  ->  1,999,996.1 / 1e6 = $1.9999961 = 199,999,610 ucents
    claude-sonnet-5-5 (2.00 / 0.20 / write 2.50 / 10.00) 09-23: 1,111 x 2.00 = 2,222; 2,222,222 x 0.20 = 444,444.4;
        333,333 x 2.50 = 833,332.5; 4,444 x 10.00 = 44,440  ->  1,324,438.9 / 1e6 = $1.3244389 = 132,443,890 ucents
    gpt-6.1-sol 09-30: 5 x 2.00 + 12 x 0.10 + 7 x 10.00 = 81.2 / 1e6 = $0.0000812 = 8,120 ucents
    """
    recs = [
        make_record("w38", agent="codex", started_at="2026-09-15T10:00:00Z",
                    usage={"gpt-6.1-sol": Usage(input=123_457, cache_read=7_654_321, output=98_765)}),
        make_record("w39", started_at="2026-09-23T10:00:00Z",
                    usage={"claude-sonnet-5-5": Usage(input=1_111, cache_read=2_222_222, cache_write=333_333, output=4_444)}),
        make_record("w40", agent="codex", started_at="2026-09-30T08:00:00Z",
                    usage={"gpt-6.1-sol": Usage(input=5, cache_read=12, output=7)}),
    ]
    return hand(hand_config, recs)


W38, W39, W40 = 199_999_610, 132_443_890, 8_120


def test_by_week_rollup_and_exact_sums(three_weeks: dict[str, Any]) -> None:
    s = data("summary", **three_weeks)
    assert s["total"]["ucents"] == W38 + W39 + W40 == 332_451_620
    assert [(w["week"], ucents(w), w["sessions"]) for w in s["by_week"]] == [
        ("2026-W38", W38, 1), ("2026-W39", W39, 1), ("2026-W40", W40, 1)]
    assert [(d["day"], ucents(d)) for d in s["by_day"]] == [("2026-09-15", W38), ("2026-09-23", W39), ("2026-09-30", W40)]
    for table in ("by_model", "by_project", "by_day", "by_week"):
        assert sum(ucents(e) or 0 for e in s[table]) == s["total"]["ucents"], table
    out = lines(run("summary", **three_weeks))
    assert "Spend by ISO week" in out
    assert "2026-W38 1 123k 7.7M 0 98.8k $2.00 60.2%" in out  # 199,999,610 / 332,451,620 = 60.16%
    assert "2026-W40 1 5 12 0 7 $0.0001 <0.1%" in out  # $0.0000812 at 4 decimals; 0.002% of the total, never "0.0%"


def test_rollup_only_when_the_period_spans_several_weeks(three_weeks: dict[str, Any]) -> None:
    week = data("summary", "--week", **three_weeks)  # now = Wed 2026-09-30: W40 only
    assert (week["total"]["ucents"], week["sessions"]) == (W40, 1)
    assert not any("ISO week" in line for line in lines(run("summary", "--week", **three_weeks)))
    assert data("summary", "--week-of", "2026-W38", **three_weeks)["total"]["ucents"] == W38
    span = data("summary", "--since", "2026-09-15", "--until", "2026-09-23", **three_weeks)
    assert span["total"]["ucents"] == W38 + W39 and len(span["by_week"]) == 2
    assert "Spend by ISO week" in lines(run("summary", "--since", "2026-09-15", "--until", "2026-09-23", **three_weeks))
    assert data("summary", "--since", "2026-09-15", "--until", "2026-09-15", **three_weeks)["total"]["ucents"] == W38
    assert data("summary", "--until", "2026-09-14", **three_weeks)["sessions"] == 0


def test_sub_cent_amounts_keep_their_precision(three_weeks: dict[str, Any]) -> None:
    out = lines(run("sessions", **three_weeks))
    assert next(line for line in out if line.startswith("09-30 codex")).endswith(" 24 $0.0001")  # 8,120 ucents = $0.0000812
    assert next(line for line in out if line.startswith("09-23 claude")).endswith(" 2.6M $1.32")  # $1.3244389: cents from $1 up
    entry = data("sessions", **three_weeks)["sessions"][0]
    assert entry["cost"] == {"cents": 0, "ucents": 8_120}  # 0.00812 cents rounds to 0 whole cents; ucents stay exact


def test_days_and_weeks_follow_the_configured_timezone(
    hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord],
) -> None:
    """America/Sao_Paulo is UTC-3 all year (no DST since 2019). Each session: 1,000,000 x $2.00 / 1e6 = $2.00."""
    try:
        zoneinfo.ZoneInfo("America/Sao_Paulo")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone database")
    usage = {"claude-sonnet-5-5": Usage(input=1_000_000)}
    recs = [
        make_record("t3", started_at="2026-09-29T02:59:59Z", usage=usage),  # 09-28 23:59:59 local
        make_record("t1", started_at="2026-09-30T02:30:00Z", usage=usage),  # 09-29 23:30 local, but 09-30 in UTC
        make_record("t2", started_at="2026-09-30T03:00:00Z", usage=usage),  # 09-30 00:00:00 local, the first instant
    ]
    kw = hand(hand_config, recs, timezone="America/Sao_Paulo")
    assert [(d["day"], d["sessions"]) for d in data("summary", **kw)["by_day"]] == [
        ("2026-09-28", 1), ("2026-09-29", 1), ("2026-09-30", 1)]
    until = data("sessions", "--until", "2026-09-29", **kw)
    assert sorted(e["session_id"] for e in until["sessions"]) == ["t1", "t3"]  # t1 is on the 29th where the user lives
    assert [e["session_id"] for e in data("sessions", "--since", "2026-09-30", **kw)["sessions"]] == ["t2"]
    assert [e["session_id"] for e in data("sessions", "--since", "2026-09-29", "--until", "2026-09-29", **kw)["sessions"]] == ["t1"]
    assert data("summary", "--since", "2026-09-29", **kw)["period"]["since"] == "2026-09-29T03:00:00Z"
    # "now" is Sunday 2026-09-27 23:00 local (Monday 02:00 UTC): the local week is still W39
    early = data("summary", "--week", now="2026-09-28T02:00:00Z", **kw)
    assert early["period"]["label"] == "week 2026-09-21 .. 2026-09-27" and early["sessions"] == 0


# ---- empty results ------------------------------------------------------------------------------------
@pytest.mark.parametrize("command", COMMANDS)
def test_empty_period_is_friendly_and_exits_zero(command: str) -> None:
    result = run(command, "--week-of", "2026-W39")
    assert result.exit_code == 0
    # no setup advice (the database is not empty): the dates that do have data instead
    assert lines(result) == ["no sessions in this period (week 2026-09-21 .. 2026-09-27)", "data exists from 2026-09-28 to 2026-09-30"]
    assert "Traceback" not in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_empty_database_says_how_to_fill_it(command: str, hand_config: Callable[..., Path]) -> None:
    result = run(command, **hand(hand_config, []))
    assert result.exit_code == 0
    out = lines(result)
    assert out[0] == "no sessions in this period (all time)"
    assert "agentfare ingest" in out[1] and "agentfare config" in out[1]


def test_empty_results_in_json_keep_every_key(hand_config: Callable[..., Path]) -> None:
    kw = hand(hand_config, [])
    s = data("summary", **kw)
    assert (s["sessions"], s["projects"], s["models"], s["total"], s["excluded_tokens"]) == (0, 0, 0, {"cents": 0, "ucents": 0}, 0)
    assert s["by_model"] == s["by_project"] == s["by_day"] == s["by_week"] == s["warnings"] == []
    assert s["tokens"] == {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "total": 0}
    t = data("sessions", **kw)
    assert (t["count"], t["shown"], t["sessions"], t["total"]) == (0, 0, [], {"cents": 0, "ucents": 0})
    assert data("models", **kw)["models"] == []


def test_a_first_run_ingests_before_reporting() -> None:
    """No --no-ingest: the fixture logs are read into the empty database, and the note goes to stderr."""
    result = run("summary", "--json")
    assert "ingested 3" in result.stderr
    assert json.loads(result.stdout)["sessions"] == 3
    assert run("summary", "--json").stderr == ""  # nothing new the second time


# ---- errors -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--since", "yesterday"], "invalid date 'yesterday'"),
        (["--until", "2026-02-30"], "invalid date '2026-02-30'"),
        (["--since", "2026-13-01"], "invalid date"),
        (["--since", "2026-9-1"], "expected YYYY-MM-DD"),
        (["--until", "2026-09-1"], "invalid date '2026-09-1': expected YYYY-MM-DD"),
        (["--since", "2026/09/01"], "expected YYYY-MM-DD"),
        (["--until", "2026-09-01T00:00"], "expected YYYY-MM-DD"),
        (["--since", "2026-09-30", "--until", "2026-09-01"], "2026-09-30"),
        (["--week-of", "2026-39"], "invalid ISO week"),
        (["--week-of", "2026-W54"], "invalid ISO week"),
        (["--week", "--week-of", "2026-W39"], "cannot be combined"),
        (["--week", "--since", "2026-09-01"], "cannot be combined"),
        (["--week-of", "2026-W39", "--until", "2026-09-30"], "cannot be combined"),
        (["--agent", "gpt"], "unknown agent 'gpt'"),
        (["--project", ""], "--project needs a value"),
        (["--since", " "], "--since needs a value"),
        (["--week-of", ""], "--week-of needs a value"),
    ],
)
def test_bad_options_are_clean_errors(command: str, args: list[str], fragment: str) -> None:
    result = run(command, *args)
    assert result.exit_code == 2, result.output
    assert result.stderr.startswith("error: ") and fragment in result.stderr
    assert result.stdout == "" and "Traceback" not in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_environment_and_config_problems_are_clean_errors(command: str, tmp_path: Path) -> None:
    result = run(command, "--week", env={"AGENTFARE_NOW": "not a time"})
    assert (result.exit_code, result.stderr.startswith("error: "), "AGENTFARE_NOW" in result.stderr) == (2, True, True)
    result = run(command, config=tmp_path / "missing.yaml")
    assert result.exit_code == 2 and result.stderr.startswith("error: config file not found")
    (tmp_path / "bad.yaml").write_text("timezone: Mars/Olympus_Mons\n")
    result = run(command, config=tmp_path / "bad.yaml")
    assert result.exit_code == 2 and "unknown timezone" in result.stderr and "Traceback" not in result.output


@pytest.mark.parametrize(("args", "fragment"), [(["--sort", "price"], "unknown --sort 'price'"), (["--limit", "0"], "--limit must be at least 1")])
def test_bad_sessions_options_are_clean_errors(args: list[str], fragment: str) -> None:
    result = run("sessions", *args)
    assert result.exit_code == 2 and result.stderr.startswith("error: ") and fragment in result.stderr


# ---- plain output: no colour, no privacy leaks, 80 columns --------------------------------------------
@pytest.mark.parametrize("command", COMMANDS)
def test_piped_output_has_no_ansi_codes(command: str) -> None:
    result = run(command, "--week")
    assert result.exit_code == 0 and "\x1b" not in result.stdout + result.stderr


@pytest.mark.parametrize("command", COMMANDS)
def test_forcing_colour_does_add_codes(command: str) -> None:
    """The guard for the test above: colour is possible, so its absence there is a property of the commands."""
    result = run(command, env={"FORCE_COLOR": "1", "TERM": "xterm-256color"})
    assert "\x1b[" in result.stdout


@pytest.mark.parametrize("command", COMMANDS)
def test_json_is_plain_json_even_when_colour_is_forced(command: str) -> None:
    result = run(command, "--json", env={"FORCE_COLOR": "1"})
    assert "\x1b" not in result.stdout and json.loads(result.stdout)["period"]["label"] == "all time"


@pytest.mark.parametrize("command", COMMANDS)
def test_output_carries_no_log_content(command: str) -> None:
    result = run(command)
    assert "PRIVACY-CANARY" not in result.output and "/home/dev" not in result.output


def tables(result: Result) -> list[tuple[int, int]]:
    """Every table in stdout as (its width, number of body rows): from its header rule down while lines stay that wide."""
    found: list[tuple[int, int]] = []
    width = rows = 0
    for line in [*result.stdout.splitlines(), ""]:
        if width and len(line) == width:
            rows += set(line) != {"─"}  # the rule above a total row is not a row
        elif width:
            found.append((width, rows))
            width = 0
        if not width and line and set(line) == {"─"}:
            width, rows = len(line), 0
    return found


@pytest.mark.parametrize(("command", "count"), [("summary", 3), ("sessions", 1), ("models", 1)])
def test_fits_80_columns_and_stays_aligned(command: str, count: int) -> None:
    result = run(command, columns=80)
    assert result.exit_code == 0
    assert max(len(line) for line in result.stdout.splitlines()) <= 80
    found = tables(result)
    assert len(found) == count
    assert all(width <= 80 and rows >= 3 for width, rows in found)  # every row is as wide as the header rule: aligned


def test_80_columns_never_cut_a_number() -> None:
    out = " ".join(lines(run("summary", columns=80)))
    for cell in ("$2.59", "$0.0592", "$3.63", "3.1M", "155k", "42.9k", "71.4%", "██████████"):
        assert cell in out
    assert "…" not in out
    sessions = lines(run("sessions", columns=80))
    assert any(line.endswith(" $2.65") and "a1c7e402" in line for line in sessions)  # the cost and the id survive the narrow layout
    assert any(line.startswith("(left out to fit 80 columns: Tokens;") for line in sessions)  # and the tokens are what gives way
    assert any(line.endswith(" $0.0592 1.6% $0.4086 $1.00 $5.00") for line in lines(run("models", columns=80)))


@pytest.mark.parametrize("command", COMMANDS)
def test_wide_terminals_show_everything_uncut(command: str) -> None:
    out = run(command, columns=200).stdout
    assert "…" not in out and "left out" not in out
    if command == "sessions":
        assert "4a6e8f10" in out and "a1c7e402" in out  # the short ids come back with the room


def test_print_table_cuts_only_marked_columns() -> None:
    cols = [Col("Name", min_width=8), Col("Note", min_width=8, cut_last=True), Col("Tokens", True), Col("Cost", True)]
    rows = [["a-very-long-project-name", "an-even-longer-model-name-here", "12.3k", "$1,234.56"], ["b", "c", "9", "$0.0042"]]
    for width in (120, 72, 60, 45, 33):  # 33 is below every floor: the shortenable columns go down to their headers
        buf = io.StringIO()
        print_table(Console(file=buf, width=width, force_terminal=False, color_system=None), cols, rows,
                    ["Total", "", "12.3k", "$1,234.56"])
        text = buf.getvalue()
        assert max(len(line) for line in text.splitlines()) <= width, width
        assert "12.3k" in text and "$1,234.56" in text and "$0.0042" in text, width  # numbers whole at every width
        assert ("a-very-long-project-name" in text) == (width >= 71), width  # 71: every column whole, one cell apart


def test_print_table_leaves_optional_columns_out_lowest_first_and_says_which_it_drew() -> None:
    cols = [Col("Name", min_width=6), Col("A", True, optional=2), Col("B\nlong", True, optional=1), Col("Cost", True)]
    rows = [["alpha-beta-gamma", "123456", "654321", "$1.00"]]
    for width, drawn, left_out in (
        (80, ["Name", "A", "B long", "Cost"], None),
        (34, ["Name", "A", "B long", "Cost"], None),  # one cell apart and Name cut, but nothing left out
        (25, ["Name", "A", "Cost"], "B long"),  # B has the lowest number
        (15, ["Name", "Cost"], "A, B long"),  # then A, in column order not drop order
    ):
        buf = io.StringIO()
        result = print_table(Console(file=buf, width=width, force_terminal=False, color_system=None), cols, rows)
        text = " ".join(buf.getvalue().split())
        assert result == drawn, width
        assert ("left out to fit" in text) == (left_out is not None), width
        if left_out:
            assert f"(left out to fit {width} columns: {left_out}; --json has everything)" in text, width
        assert "123456" in text or "A" not in drawn  # a column that is drawn is drawn whole
        assert "$1.00" in text


def test_names_that_look_like_markup_or_are_not_ascii_print_literally(
    hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord],
) -> None:
    names = ["[red]x[/red]", "Ação-é", "日本語"]
    recs = [make_record(f"n{i}", project=name, started_at=f"2026-09-2{i}T10:00:00Z") for i, name in enumerate(names)]
    kw = hand(hand_config, recs)
    assert sorted(p["project"] for p in data("summary", **kw)["by_project"]) == sorted(names)  # JSON keeps them exact
    for command in ("summary", "sessions"):  # the two that show project names
        out = run(command, **kw).stdout
        assert "[red]x[/red]" in out and "Ação-é" in out and "日本語" in out, command
    assert data("sessions", "--project", "AÇÃO-É", **kw)["count"] == 1  # the project filter folds case and accents


# ---- hostile names, other encodings, narrow terminals ---------------------------------------------------
def test_control_characters_in_names_never_reach_the_terminal(
    hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord],
) -> None:
    """A directory called ``x<ESC>[31m`` or ``a<newline>b`` must not colour, move or split a report; JSON keeps it exact."""
    names = ["bad\x1b[31mred", "line1\nline2", "tab\tname", "ding\x07dong", "evil\u202egpj", "nul\x00byte"]
    recs = [make_record(f"c{i}", project=name, started_at=f"2026-09-2{i}T10:00:00Z") for i, name in enumerate(names)]
    kw = hand(hand_config, recs)
    assert sorted(p["project"] for p in data("summary", **kw)["by_project"]) == sorted(names)
    shown = ["bad?[31mred", "line1?line2", "tab?name", "ding?dong", "evil?gpj", "nul?byte"]
    for command in COMMANDS:
        result = run(command, **kw)
        assert result.exit_code == 0
        assert not any(ch in result.stdout for ch in "\x1b\x07\x00\u202e\t"), command
    summary, sessions = lines(run("summary", **kw)), lines(run("sessions", **kw))
    for name in shown:
        assert any(line.startswith(f"{name} 1 ") for line in summary), name  # one whole row per project
        assert any(f" {name} " in f" {line} " for line in sessions), name
    assert "AgentFare summary: all time, project bad?[31mred" in lines(run("summary", "--project", "bad\x1b[31mred", **kw))


def test_a_console_that_cannot_write_the_glyphs_still_prints(
    hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord],
) -> None:
    """The bar, the ellipsis and non-ASCII names need a Unicode terminal; on an ASCII one they degrade, they do not crash."""
    recs = [make_record("a1", project="Ação-é-projeto-com-nome-longo", usage={"claude-sonnet-5-5": Usage(input=1_000_000)}),
            make_record("a2", project="x" * 40, started_at="2026-09-29T10:00:00Z", usage={"claude-sonnet-5-5": Usage(input=2_000_000)})]
    cfg = hand_config(recs)
    for command in COMMANDS:
        result = CliRunner(charset="ascii").invoke(
            app, ["--config", str(cfg), "--no-ingest", command], env={"COLUMNS": "60", "AGENTFARE_NOW": NOW})
        assert result.exception is None, (command, result.exception)
        assert result.exit_code == 0 and "…" not in result.stdout and "█" not in result.stdout, command
    out = CliRunner(charset="ascii").invoke(app, ["--config", str(cfg), "--no-ingest", "summary"], env={"COLUMNS": "100"}).stdout
    assert "##########" in out and "A??o-?-projeto" in out  # the bar falls back to '#', the accents to '?'


@pytest.fixture
def long_names(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord], tmp_path: Path) -> dict[str, Any]:
    """Three models that share their first 14 letters, in three projects; input is $2.00 per 1M tokens.

    acme-large-two-preview    6,119,460,000 tokens = $12,238.92     (alpha-service-backend, 09-27)
    acme-large-two-lite       1,000,000 in + 1 out = $2.00001       (beta-service-frontend, 09-28)
    acme-large-three-preview  1,500 tokens = $0.003                 (gamma-service-worker, 09-29)
    """
    models = ("acme-large-two-preview", "acme-large-two-lite", "acme-large-three-preview")
    (tmp_path / "long-prices.yaml").write_text(
        "schema: 1\nmodels:\n" + "".join(f"  {m}: {{provider: acme, input: 2.0, output: 10.0}}\n" for m in models))
    recs = [
        make_record("l1", agent="gemini", project="alpha-service-backend", started_at="2026-09-27T10:00:00Z",
                    usage={models[0]: Usage(input=6_119_460_000)}),
        make_record("l2", agent="gemini", project="beta-service-frontend", started_at="2026-09-28T10:00:00Z",
                    usage={models[1]: Usage(input=1_000_000, output=1)}),
        make_record("l3", agent="gemini", project="gamma-service-worker", started_at="2026-09-29T10:00:00Z",
                    usage={models[2]: Usage(input=1_500)}),
    ]
    cfg = hand_config(recs)
    cfg.write_text(cfg.read_text() + f"pricing_file: {tmp_path / 'long-prices.yaml'}\n")
    return {"config": cfg, "ingest": False}


LONG_COSTS = ["$12,238.92", "$2.00", "$0.0030"]


@pytest.mark.parametrize("columns", [50, 55, 60, 70, 80, 100, 200])
@pytest.mark.parametrize("command", COMMANDS)
def test_narrow_terminals_leave_columns_out_instead_of_cutting_numbers(
    command: str, columns: int, long_names: dict[str, Any],
) -> None:
    result = run(command, columns=columns, **long_names)
    assert result.exit_code == 0
    out = result.stdout
    assert max(len(line) for line in out.splitlines()) <= columns
    assert all(cost in out for cost in LONG_COSTS)  # the money column is never the one that gives way
    assert not re.search(r"[0-9$%.,kMB]…", out)  # nor any other number
    if columns <= 60:
        assert "left out to fit" in " ".join(lines(result))  # and the reader is told what is missing


@pytest.mark.parametrize("command", COMMANDS)
def test_model_names_that_are_cut_still_tell_the_models_apart(command: str, long_names: dict[str, Any]) -> None:
    full = ("acme-large-two-preview", "acme-large-two-lite", "acme-large-three-preview")
    for columns in (60, 70, 80, 90):
        out = lines(run(command, columns=columns, **long_names))
        stubs = {word for line in out for word in line.split() if word.startswith("acme") and word != "acme"}  # the models table also has a provider called acme
        assert len(stubs) == 3, (columns, stubs)  # three models, three different labels
        for stub in stubs:  # each is a model's start, then the ellipsis, then its end (or the whole name)
            head, _, tail = stub.partition("…")
            assert any(name.startswith(head) and name.endswith(tail) for name in full), (columns, stub)


def test_below_any_usable_width_the_tables_run_wide_rather_than_cut_a_number(long_names: dict[str, Any]) -> None:
    for command in COMMANDS:
        out = run(command, columns=30, **long_names).stdout
        assert all(cost in out for cost in LONG_COSTS) and not re.search(r"[0-9$%.,kMB]…", out), command


def test_the_legend_names_only_the_columns_that_are_shown(long_names: dict[str, Any]) -> None:
    wide, narrow = lines(run("sessions", columns=200, **long_names)), lines(run("sessions", columns=60, **long_names))
    assert "Prm prompts, Tool tool calls, File files touched, Err tool errors" in wide
    assert not any(line.startswith("Prm prompts") for line in narrow)  # the counters gave way; so did their key
    note = left_out_note(narrow)
    assert "left out to fit 60 columns: Prm," in note and "--json has everything" in note


def test_a_share_below_a_tenth_of_a_percent_is_never_shown_as_zero(long_names: dict[str, Any]) -> None:
    """$0.003 of $12,240.92 is 0.00002 of the total: 0 basis points, but the row did spend something."""
    m = data("models", **long_names)
    tiny = next(e for e in m["models"] if e["cost"]["ucents"] == 300_000)
    assert tiny["share_bp"] == 0  # JSON: exact at basis-point resolution
    rows = lines(run("models", **long_names))
    assert any(line.startswith("3 acme-large-three-preview") and " $0.0030 <0.1% " in line for line in rows)
    assert not any(" 0.0% " in line + " " for line in rows)


def test_totals_carry_the_unpriced_marker_in_every_command(unpriced: dict[str, Any]) -> None:
    """summary marks its total with '+' and its legend when unpriced tokens are left out; sessions and models do the same."""
    sessions, models = lines(run("sessions", **unpriced)), lines(run("models", **unpriced))
    assert "showing 3 of 3 sessions, total $3.30+" in sessions
    assert "4 models, 3 sessions, total spend $3.30+" in models
    assert "+ also holds tokens without a verified price, which the cost leaves out" in sessions + models


def test_nothing_priced_is_not_a_zero_total_in_any_command(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> None:
    rec = make_record("u1", agent="gemini", usage={"gemini-3-flash": Usage(input=1_500_000, output=500_000)})
    kw = hand(hand_config, [rec])
    assert "showing 1 of 1 session, total n/a (nothing priced)" in lines(run("sessions", **kw))
    assert "1 model, 1 session, total spend n/a (nothing priced)" in lines(run("models", **kw))
    assert data("sessions", **kw)["tokens"]["total"] == data("models", **kw)["tokens"]["total"] == 2_000_000


# ---- the number formatters ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("n", "shown"),
    [(0, "0"), (999, "999"), (1_000, "1.0k"), (12_345, "12.3k"), (99_949, "99.9k"), (99_950, "100k"), (181_500, "182k"),
     (999_400, "999k"), (999_500, "1.0M"), (4_500_000, "4.5M"), (99_990_000, "100M"), (1_234_567_890, "1.2B"),
     (12_345_678_901, "12.3B"), (123_456_789_012, "123B")],
)
def test_compact_tokens(n: int, shown: str) -> None:
    assert tokens(n) == shown


def test_a_share_is_rounded_once_from_the_amounts_not_from_basis_points(
    hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]
) -> None:
    """$0.049499 of $1.00 is 4.9499%, so 4.9%. Rounded to basis points first (495) and to a tenth after that it would read 5.0%."""
    records = [
        make_record("a", agent="codex", usage={"gpt-6.1-sol": Usage(cache_read=494_990)}),  # 494,990 x $0.10 / 1M
        make_record("b", agent="claude", usage={"claude-haiku-4-5": Usage(cache_read=9_505_010)}),  # the rest of a dollar
    ]
    kw = hand(hand_config, records)
    assert [(e["model"], e["share_bp"]) for e in data("models", **kw)["models"]] == [("claude-haiku-4-5", 9505), ("gpt-6.1-sol", 495)]
    assert any(line.startswith("2 gpt-6.1-sol openai 1 495k $0.0495 4.9% ") for line in lines(run("models", **kw)))
    assert any(line.startswith("codex gpt-6.1-sol 1 0 495k 0 0 $0.0495 4.9%") for line in lines(run("summary", **kw)))


def test_money_percent_and_duration_formats() -> None:
    assert [usd(u) for u in (0, 76_000_000, 5_920_000, 100_000_000, 99_999_999, 265_100_000, 12_345_678_901)] == [
        "$0.00", "$0.76", "$0.0592", "$1.00", "$1.00", "$2.65", "$123.46"]
    assert [percent(bp) for bp in (None, 0, 1, 4, 5, 7002, 9995, 10_000)] == [
        "-", "0.0%", "<0.1%", "<0.1%", "0.1%", "70.0%", "100%", "100%"]
    assert [duration(s) for s in (0, 59, 60, 595, 3600, 5930, 100_000)] == ["0s", "59s", "1m00s", "9m55s", "1h00m", "1h38m", "27h46m"]


# ---- sessions that span days and weeks: money is attributed to when it was spent ----------------------------
@pytest.fixture
def spanning(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> dict[str, Any]:
    """One session that runs from Sunday 2026-09-27 into Monday 2026-09-28 (ISO weeks 39 and 40), and a short one.

    span (two models, 15-minute slots): sonnet-5-5 1,000,000 in at 23:30 Sunday = $2.00, 3,000,000 in at 00:15 Monday = $6.00,
        gpt-6.1-sol 500,000 in at 00:15 Monday = $1.00; the whole session is $9.00, $2.00 of it spent on Sunday
    whole (Monday 10:00): sonnet-5-5 1,000,000 in = $2.00
    """
    sonnet, sol = "claude-sonnet-5-5", "gpt-6.1-sol"
    slots = {("2026-09-27T23:30:00Z", sonnet): Usage(input=1_000_000), ("2026-09-28T00:15:00Z", sonnet): Usage(input=3_000_000),
             ("2026-09-28T00:15:00Z", sol): Usage(input=500_000)}
    span = make_record("span-session-0001", project="long", started_at="2026-09-27T23:30:00Z", wall_seconds=2700,
                       usage={sonnet: Usage(input=4_000_000), sol: Usage(input=500_000)}, slots=slots)
    whole = make_record("whole-session-002", project="short", started_at="2026-09-28T10:00:00Z", usage={sonnet: Usage(input=1_000_000)})
    return hand(hand_config, [span, whole])


DOLLAR = 100_000_000


def test_a_session_spanning_two_days_and_weeks_counts_where_it_was_spent(spanning: dict[str, Any]) -> None:
    s = data("summary", **spanning)
    assert (s["sessions"], s["total"]["ucents"]) == (2, 11 * DOLLAR)
    assert [(d["day"], ucents(d), d["sessions"]) for d in s["by_day"]] == [
        ("2026-09-27", 2 * DOLLAR, 1), ("2026-09-28", 9 * DOLLAR, 2)]  # the long session is on both days; Monday holds $7 + $2
    assert [(w["week"], ucents(w), w["sessions"]) for w in s["by_week"]] == [("2026-W39", 2 * DOLLAR, 1), ("2026-W40", 9 * DOLLAR, 2)]
    assert [(m["model"], ucents(m), m["sessions"]) for m in s["by_model"]] == [
        ("claude-sonnet-5-5", 10 * DOLLAR, 2), ("gpt-6.1-sol", DOLLAR, 1)]
    for table in ("by_model", "by_project", "by_day", "by_week"):
        assert sum(ucents(e) or 0 for e in s[table]) == s["total"]["ucents"], table
    out = lines(run("summary", **spanning))
    assert "a session that spans several days counts once per day; the Total row counts it once" in out
    assert "a session that spans several weeks counts once per week; the Total row counts it once" in out
    assert "a session that used several models counts once per model; the Total row counts it once" in out
    assert any(line.startswith("2026-09-27 1 ") and " $2.00 " in line for line in out)
    assert any(line.startswith("2026-09-28 2 ") and " $9.00 " in line for line in out)


def test_a_session_that_stays_inside_one_day_needs_no_note_about_days(spanning: dict[str, Any]) -> None:
    out = lines(run("summary", "--since", "2026-09-28", "--until", "2026-09-28", **spanning))
    assert not any("counts once per day" in line or "counts once per week" in line for line in out)


@pytest.mark.parametrize(
    ("args", "total", "sessions"),
    [
        ([], 11 * DOLLAR, 2),
        (["--since", "2026-09-28"], 9 * DOLLAR, 2),  # $7 of the long session and all of the short one
        (["--until", "2026-09-27"], 2 * DOLLAR, 1),  # only what the long session spent on Sunday
        (["--week-of", "2026-W39"], 2 * DOLLAR, 1),
        (["--week-of", "2026-W40"], 9 * DOLLAR, 2),
        (["--since", "2026-09-28", "--until", "2026-09-28", "--project", "long"], 7 * DOLLAR, 1),
    ],
)
def test_every_command_reports_the_same_money_for_a_period(spanning: dict[str, Any], args: list[str], total: int, sessions: int) -> None:
    summary, listing, models = (data(c, *args, **spanning) for c in COMMANDS)
    assert summary["total"]["ucents"] == listing["total"]["ucents"] == models["total"]["ucents"] == total
    assert summary["sessions"] == listing["count"] == models["sessions"] == sessions
    assert sum(ucents(e) or 0 for e in listing["sessions"]) == sum(ucents(m) or 0 for m in models["models"]) == total
    assert summary["tokens"] == listing["tokens"] == models["tokens"]


def test_a_session_cut_at_the_edge_of_the_period_is_marked_and_explained(spanning: dict[str, Any]) -> None:
    whole = data("sessions", **spanning)
    assert [e["clipped"] for e in whole["sessions"]] == [False, False]  # no period: nothing is left out
    assert not any("span the period edge" in line for line in lines(run("sessions", **spanning)))
    monday = data("sessions", "--since", "2026-09-28", **spanning)
    by_id = {e["session_id"]: e for e in monday["sessions"]}
    long, short = by_id["span-session-0001"], by_id["whole-session-002"]
    assert (long["clipped"], short["clipped"]) == (True, False)
    assert ucents(long) == 7 * DOLLAR and long["tokens"]["total"] == 3_500_000  # what was spent on Monday only
    assert long["started_at"] == "2026-09-27T23:30:00Z" and long["wall_seconds"] == 2700  # the rest is the whole session's
    assert (long["primary_model"], long["other_models"]) == ("claude-sonnet-5-5", 1)
    assert long["date"] == "2026-09-28"  # the first day it spent money in the period, so the list stays inside the period
    out = lines(run("sessions", "--since", "2026-09-28", **spanning))
    assert next(line for line in out if " long " in line).endswith(" 3.5M $7.00*")  # starred
    assert next(line for line in out if " short " in line).endswith(" 1.0M $2.00")
    assert "* sessions that span the period edge show only what was spent inside it" in out
    assert "showing 2 of 2 sessions, total $9.00" in out
    assert data("sessions", "--until", "2026-09-27", **spanning)["sessions"][0]["clipped"] is True


def test_a_clipped_session_is_marked_next_to_one_with_unpriced_tokens(
    hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord],
) -> None:
    """Both markers can sit on one cost column; the figures stay aligned on the right."""
    slots = {("2026-09-27T23:30:00Z", "claude-sonnet-5-5"): Usage(input=1_000_000), ("2026-09-28T00:15:00Z", "claude-sonnet-5-5"): Usage(input=1_000_000),
             ("2026-09-28T00:15:00Z", "mystery-model-9"): Usage(input=1_000)}
    recs = [make_record("span-mixed-00001", started_at="2026-09-27T23:30:00Z", slots=slots,
                        usage={"claude-sonnet-5-5": Usage(input=2_000_000), "mystery-model-9": Usage(input=1_000)}),
            make_record("plain-session-003", started_at="2026-09-28T10:00:00Z", usage={"claude-sonnet-5-5": Usage(input=1_000_000)})]
    out = lines(run("sessions", "--since", "2026-09-28", **hand(hand_config, recs)))
    assert next(line for line in out if short_id("span-mixed-00001") in line).endswith(" 1.0M $2.00+*")
    assert next(line for line in out if short_id("plain-session-003") in line).endswith(" 1.0M $2.00")


def test_the_header_of_a_period_view_says_what_the_money_is(spanning: dict[str, Any]) -> None:
    note = "spent inside the period only: a session that spans its edge counts in part"
    for command in ("summary", "models"):
        assert note in lines(run(command, "--week", **spanning)), command
        assert note not in lines(run(command, **spanning)), command
    assert note not in lines(run("sessions", "--week", **spanning))  # the star and its own note say it where it matters


# ---- summary --top ------------------------------------------------------------------------------------------
@pytest.fixture
def twelve(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> dict[str, Any]:
    """Project p01 .. p12 on the days 2026-09-01 .. 09-12, each a session of n dollars (n x 500,000 input tokens at $2.00 per 1M)."""
    recs = [make_record(f"t{n:02d}", project=f"p{n:02d}", started_at=f"2026-09-{n:02d}T10:00:00Z",
                        usage={"claude-sonnet-5-5": Usage(input=n * 500_000)}) for n in range(1, 13)]
    return hand(hand_config, recs)


def table_rows(out: list[str], title: str) -> list[str]:
    """The body rows of one summary table, total row included."""
    first = next(i for i, line in enumerate(out) if line == title or line.startswith(f"{title} ("))  # a folded table says so in its caption
    rule = next(i for i in range(first, len(out)) if set(out[i]) == {"─"})  # under the header, which has two lines
    end = next(i for i in range(rule, len(out)) if out[i].startswith("Total"))
    return [line for line in out[rule + 1:end + 1] if set(line) != {"─"}]


def test_top_limits_the_project_and_day_tables_and_adds_one_row_for_the_rest(twelve: dict[str, Any]) -> None:
    out = lines(run("summary", **twelve))  # the default is 10
    projects, days = table_rows(out, "Spend by project"), table_rows(out, "Spend by day")
    assert [row.split()[0] for row in projects] == [f"p{n:02d}" for n in range(12, 2, -1)] + ["2", "Total"]  # the ten biggest
    assert "2 more (total $3.00) 2 1.5M 0 0 0 $3.00 3.8%" in projects  # p01 + p02: $1 + $2 of $78
    assert [row.split()[0] for row in days] == [f"2026-09-{n:02d}" for n in range(3, 13)] + ["2", "Total"]  # the ten latest
    assert "2 more (total $3.00) 2 1.5M 0 0 0 $3.00 3.8%" in days  # 09-01 and 09-02, the oldest, come last
    assert projects[-1].startswith("Total 12 ") and days[-1].startswith("Total 12 ")
    for rows in (projects, days):  # the rows, the one for the rest included, add up to the total row
        figures = [re.search(r" (\d+) \S+ \S+ \S+ \S+ \$([\d,.]+) [\d.<]+%", row) for row in rows[:-1]]
        assert all(figures) and sum(int(m[1]) for m in figures if m) == 12 and sum(float(m[2]) for m in figures if m) == 78.0


def test_top_n_and_zero(twelve: dict[str, Any]) -> None:
    out = lines(run("summary", "--top", "3", **twelve))
    assert "9 more (total $45.00) 9 22.5M 0 0 0 $45.00 57.7%" in table_rows(out, "Spend by project")  # 1 + ... + 9
    assert [r.split()[0] for r in table_rows(out, "Spend by day")[:3]] == ["2026-09-10", "2026-09-11", "2026-09-12"]
    assert "9 more (total $45.00) 9 22.5M 0 0 0 $45.00 57.7%" in table_rows(out, "Spend by day")
    for args in (["--top", "0"], ["--top", "12"], ["--top", "50"]):  # everything fits: no row for the rest
        out = lines(run("summary", *args, **twelve))
        assert len(table_rows(out, "Spend by project")) == 13 and len(table_rows(out, "Spend by day")) == 13
        assert not any("more (total" in line for line in out), args
    assert [r.split()[0] for r in table_rows(lines(run("summary", "--top", "1", **twelve)), "Spend by project")][:2] == ["p12", "11"]


def test_top_leaves_the_week_table_and_the_json_alone(twelve: dict[str, Any]) -> None:
    out = lines(run("summary", "--top", "1", **twelve))
    weeks = table_rows(out, "Spend by ISO week")
    assert [r.split()[0] for r in weeks] == ["2026-W36", "2026-W37", "Total"]  # both weeks, no row for the rest
    s = data("summary", "--top", "1", **twelve)
    assert (len(s["by_project"]), len(s["by_day"]), len(s["by_week"])) == (12, 12, 2)
    assert s == data("summary", "--top", "0", **twelve) == data("summary", **twelve)


def test_top_rest_row_with_unpriced_money(unpriced: dict[str, Any], make_record: Callable[..., SessionRecord]) -> None:
    """beta and gamma have no priced spend at all, alpha has $3.30 and a leftover: the row for the rest says so honestly.

    (A third project and a third day: with only one row to hide, ``--top`` shows it as itself.)"""
    extra = make_record("s4", agent="codex", project="gamma", started_at="2026-09-28T10:00:00Z", usage={"mystery-model-9": Usage(input=100_000)})
    with Store(unpriced["config"].parent / "hand.db") as store:
        store.upsert_session(extra)
    out = lines(run("summary", "--top", "1", **unpriced))
    projects = table_rows(out, "Spend by project")
    assert projects[0].startswith("alpha 2 ") and projects[1] == "2 more 2 350k 50.0k 0 0 - -"  # nothing priced: no total in the label
    days = table_rows(out, "Spend by day")
    assert days[0].startswith("2026-09-30 2 ") and " $0.30+ 9.1% " in days[0]
    assert days[1].startswith("2 more (total $3.00+) 2 1.1M 0 0 100k $3.00+ 90.9%")  # 09-29 and 09-28: gamma's tokens have no price


def test_a_single_hidden_row_is_shown_as_itself(twelve: dict[str, Any]) -> None:
    """``--top 11`` of 12 rows would fold one row into a row that says 1 more: it is shown instead, and nothing is captioned."""
    out = lines(run("summary", "--top", "11", **twelve))
    for title in ("Spend by project", "Spend by day"):
        rows = table_rows(out, title)
        assert len(rows) == 13 and not any(row.startswith("1 more") for row in rows), title
    assert "Spend by project" in out and "Spend by day" in out  # no caption: nothing is hidden
    assert not any("more (total" in line for line in out)
    folded = lines(run("summary", "--top", "10", **twelve))  # two rows hidden: folded
    assert any(row.startswith("2 more (total $3.00)") for row in table_rows(folded, "Spend by project"))


def test_a_folded_table_says_what_it_shows(twelve: dict[str, Any]) -> None:
    out = lines(run("summary", "--top", "3", **twelve))
    assert "Spend by project (top 3 by spend)" in out and "Spend by day (latest 3 days)" in out
    assert [r.split()[0] for r in table_rows(out, "Spend by day")[:3]] == ["2026-09-10", "2026-09-11", "2026-09-12"]  # the latest, oldest first
    assert "Spend by day (latest 1 day)" in lines(run("summary", "--top", "1", **twelve))
    whole = lines(run("summary", "--top", "0", **twelve))
    assert "Spend by project" in whole and not any("(top" in line or "(latest" in line for line in whole)


def test_a_bad_top_is_a_clean_error() -> None:
    result = run("summary", "--top", "-1")
    assert result.exit_code == 2 and result.stderr.startswith("error: --top must be 0 or more, got -1")
    assert run("summary", "--top", "many").exit_code == 2


def test_the_label_of_the_rest_row_is_never_cut_while_the_column_can_be_as_wide(twelve: dict[str, Any]) -> None:
    for columns in (80, 100):
        out = run("summary", columns=columns, **twelve).stdout
        assert "2 more (total $3.00)" in out, columns
        assert max(len(line) for line in out.splitlines()) <= columns


# ---- sessions: the footer, the id, model names that are cut -------------------------------------------------
def test_the_footer_of_a_cut_list_gives_the_money_of_the_rows_shown_and_of_all(unpriced: dict[str, Any]) -> None:
    assert "showing 1 of 3 sessions (n/a of $3.30+)" in lines(run("sessions", "--limit", "1", **unpriced))  # s3 has no priced spend
    assert "showing 2 of 3 sessions ($0.30+ of $3.30+)" in lines(run("sessions", "--limit", "2", **unpriced))
    assert "showing 3 of 3 sessions, total $3.30+" in lines(run("sessions", **unpriced))  # nothing cut: as before


@pytest.mark.parametrize("columns", [80, 70, 60])
def test_the_id_outlives_the_tokens_on_a_narrow_terminal(columns: int) -> None:
    """``agentfare advise`` prints the short id of a session: it has to be findable in the list."""
    result = run("sessions", columns=columns)
    assert all(sid in result.stdout for sid in ("4a6e8f10", "7c8d9e0f", "a1c7e402"))
    note = left_out_note(lines(result))  # the note may wrap over several lines
    left_out = note.partition("columns: ")[2].partition(";")[0]
    assert "Tokens" in left_out and "ID" not in left_out.split(", ")


def test_the_id_is_the_last_optional_column_to_go(long_names: dict[str, Any]) -> None:
    drop_order = ["Tokens", "Err", "File", "Tool", "Prm", "Active", "ID", "Project"]
    previous: set[str] = set()
    for columns in (200, 100, 80, 70, 60, 50, 40):
        note = left_out_note(lines(run("sessions", columns=columns, **long_names)))
        left_out = set(note.partition("columns: ")[2].partition(";")[0].split(", ")) - {""}
        assert left_out == set(drop_order[:len(left_out)]) and previous <= left_out, (columns, left_out)  # always the next in line
        previous = left_out
    assert {"Tokens", "Err", "File"} <= previous  # the drops are real, not vacuous


@pytest.mark.parametrize(
    "name", ["claude-opus-4-5-20251101", "gpt-5.1-codex-max", "gemini-2.5-flash-lite", "acme-large-three-preview", "claude-haiku-4-5-20251001"],
)
def test_a_cut_model_name_keeps_its_end_and_never_splits_a_number(name: str) -> None:
    from agentfare.render import _cut

    numbers = [m.span() for m in re.finditer(r"[0-9]+(?:\.[0-9]+)*", name)]
    for width in range(12, len(name)):
        cut = _cut(name, width, "…", middle=True)
        head, ellipsis, tail = cut.partition("…")
        assert ellipsis and len(cut) <= width and name.startswith(head) and name.endswith(tail), (width, cut)
        assert head and tail  # the start and the end both survive
        for edge in (len(head), len(name) - len(tail)):
            assert not any(a < edge < b for a, b in numbers), (width, cut)  # no cut inside 2025, 5.1 or 20251101


def test_cut_names_of_different_models_are_different(long_names: dict[str, Any]) -> None:
    from agentfare.render import Col

    cols = [Col("Model", min_width=8, middle=True)]
    rows = [[m] for m in ("acme-large-two-preview", "acme-large-two-lite", "acme-large-three-preview")]
    for width in (40, 30, 26, 22, 20, 18):
        buf = io.StringIO()
        print_table(Console(file=buf, width=width, force_terminal=False, color_system=None), cols, rows)
        body = [line.strip() for line in buf.getvalue().splitlines()[2:] if line.strip()]
        assert len(set(body)) == 3, (width, body)


# ---- the label of a period, strict dates --------------------------------------------------------------------
@pytest.mark.parametrize("command", COMMANDS)
def test_period_labels_read_from_up_to_and_a_range(command: str) -> None:
    for args, label in (
        (["--since", "2026-09-01"], "from 2026-09-01"),
        (["--until", "2026-09-30"], "up to 2026-09-30"),
        (["--since", "2026-09-01", "--until", "2026-09-30"], "2026-09-01 .. 2026-09-30"),
    ):
        assert data(command, *args)["period"]["label"] == label
        header = lines(run(command, *args))[0]
        assert header == f"AgentFare {command}: {label}" and "..." not in header
    assert lines(run(command, "--since", "2026-09-01", "--project", "x"))[0] == f"no sessions in this period (from 2026-09-01, project x)"


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("day", ["2026-9-1", "2026-09-1", "26-09-01", "2026/09/01", "20260901", "２０２６-09-01"])
def test_a_date_has_one_spelling(command: str, day: str) -> None:
    for flag in ("--since", "--until"):
        result = run(command, flag, day)
        assert result.exit_code == 2 and f"invalid date {day!r}: expected YYYY-MM-DD" in result.stderr and result.stdout == ""
    assert run(command, "--since", " 2026-09-01 ").exit_code == 0  # the blanks around a date are not part of it


# ---- an empty result because of --project -------------------------------------------------------------------
@pytest.mark.parametrize("command", COMMANDS)
def test_an_unknown_project_lists_the_closest_names(command: str) -> None:
    out = lines(run(command, "--project", "acme-shoop"))
    assert out == ["no sessions in this period (all time, project acme-shoop)",
                   "no project is called 'acme-shoop'; closest names: acme-shop"]
    assert lines(run(command, "--project", "ACME-SHO"))[1] == "no project is called 'ACME-SHO'; closest names: acme-shop"  # case folded


@pytest.mark.parametrize("command", COMMANDS)
def test_an_unknown_project_with_no_near_name_lists_the_biggest(command: str) -> None:
    out = lines(run(command, "--project", "zzzzzz"))
    assert out[1] == "no project is called 'zzzzzz'; the biggest by spend: acme-shop, fake-repo, demo-notes-app"  # $2.65, $0.76, $0.22


@pytest.mark.parametrize("command", COMMANDS)
def test_a_known_project_with_nothing_in_the_period_is_not_a_typo(command: str) -> None:
    out = lines(run(command, "--project", "acme-shop", "--since", "2026-09-30"))
    assert out == ["no sessions in this period (from 2026-09-30, project acme-shop)",
                   "data exists from 2026-09-28 to 2026-09-28 (project acme-shop)"]  # the dates of that project, not of the database
    assert lines(run(command, "--project", "acme-shop", "--agent", "codex")) == ["no sessions in this period (all time, project acme-shop, agent codex)"]


def test_the_suggestions_name_at_most_five_projects_and_leave_the_json_alone(twelve: dict[str, Any]) -> None:
    out = lines(run("sessions", "--project", "zzzzzz", **twelve))
    assert out[1] == "no project is called 'zzzzzz'; the biggest by spend: p12, p11, p10, p09, p08"
    assert data("sessions", "--project", "zzzzzz", **twelve)["count"] == 0
    assert list(data("sessions", "--project", "zzzzzz", **twelve)) == ["period", "filters", "count", "shown", "tokens", "total", "excluded_tokens", "sessions", "warnings"]


def test_short_flags_keep_working() -> None:
    assert data("sessions", "-n", "1")["shown"] == 1
    assert data("sessions", "-a", "codex")["count"] == 1
    assert data("summary", "-a", "gemini")["sessions"] == 1 and data("models", "-a", "claude")["sessions"] == 1


def test_the_model_of_a_cut_session_is_the_one_that_wrote_most_inside_the_period(
    hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord],
) -> None:
    """Sonnet wrote most of the session, all of it on Sunday; on Monday only haiku worked, so it is the model of that view."""
    slots = {("2026-09-27T23:30:00Z", "claude-sonnet-5-5"): Usage(input=1_000, output=900_000),
             ("2026-09-28T00:15:00Z", "claude-haiku-4-5"): Usage(input=1_000, output=10)}
    rec = make_record("two-models-0001", started_at="2026-09-27T23:30:00Z", slots=slots,
                      usage={"claude-sonnet-5-5": Usage(input=1_000, output=900_000), "claude-haiku-4-5": Usage(input=1_000, output=10)})
    kw = hand(hand_config, [rec])
    whole, monday = (data("sessions", *args, **kw)["sessions"][0] for args in ([], ["--since", "2026-09-28"]))
    assert (whole["primary_model"], whole["other_models"]) == ("claude-sonnet-5-5", 1)
    assert (monday["primary_model"], monday["other_models"], [m["model"] for m in monday["models"]]) == ("claude-haiku-4-5", 0, ["claude-haiku-4-5"])


# ---- model names on a narrow terminal -----------------------------------------------------------------
@pytest.fixture
def model_names(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> dict[str, Any]:
    """Models that share a long start, in projects whose names are long too (claude-opus-4-5-20251101 has no price here)."""
    models = {"alpha-service-backend": "claude-sonnet-5-5", "beta-service-frontend": "claude-opus-5-5",
              "gamma-service-worker": "claude-opus-4-5-20251101", "delta-service-batch": "claude-haiku-4-5"}
    recs = [make_record(f"m{i}", project=project, started_at=f"2026-09-2{i + 1}T10:00:00Z", usage={model: Usage(input=1_000_000 - i)})
            for i, (project, model) in enumerate(models.items())]
    return hand(hand_config, recs)


NAMES = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-4-5-20251101", "claude-haiku-4-5")


def labels(out: str) -> list[str]:
    """The words of a table that start a Claude model's name, as drawn (whole or cut)."""
    return [word for word in out.partition("warning:")[0].split() if word.startswith("claude-")]


@pytest.mark.parametrize("columns", [40, 60, 80])
@pytest.mark.parametrize("command", ["sessions", "models", "summary"])
def test_cut_model_names_are_cut_at_the_end_one_way_and_stay_different(command: str, columns: int, model_names: dict[str, Any]) -> None:
    out = run(command, columns=columns, **model_names).stdout
    assert max(len(line) for line in out.splitlines()) <= columns
    shown = labels(out.split("Spend by project")[0] if command == "summary" else out)  # the model table: one row per model
    assert len(shown) == len(set(shown)) == 4, (columns, shown)  # four models, four different labels
    for label in shown:
        head, ellipsis, rest = label.partition("…")
        assert rest == "" and [n for n in NAMES if n.startswith(head)], (columns, label)  # a start of a name, then one ellipsis at its end
        assert sum(n.startswith(head) for n in NAMES) == 1 or not ellipsis, (columns, label)  # and of one name only
    assert "~" not in out and "..." not in out  # one style: no ASCII stand-in either
    assert not re.search(r"claude-\S*…\S", out)  # never an ellipsis in the middle of a name


@pytest.mark.parametrize("command", ["sessions", "models", "summary"])
def test_where_there_is_room_the_model_names_are_whole_and_the_projects_give_way(command: str, model_names: dict[str, Any]) -> None:
    for columns in (60, 80):
        out = run(command, columns=columns, **model_names).stdout
        assert all(name in out for name in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5")), (command, columns)
        if command == "sessions":
            assert "alpha-service-backend" not in out  # the model is what is kept, the project is cut


def test_at_40_columns_the_model_name_keeps_most_of_its_length(model_names: dict[str, Any]) -> None:
    out = run("sessions", columns=40, **model_names).stdout
    assert all(len(label) >= 13 for label in labels(out)), labels(out)  # claude-sonnet… rather than claude-…-5-5


def test_the_labels_of_a_crushed_table_are_still_different(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> None:
    """Far below any width the tables fit: the columns are cut past their floors, but two models never come out as one."""
    recs = [make_record(f"c{i}", agent="gemini", usage={f"acme-model-{name}": Usage(input=1000 + i)}, project=f"p{i}", started_at=f"2026-09-2{i}T10:00:00Z")
            for i, name in enumerate(("standard-one", "standard-two", "standard-three"), 1)]
    kw = hand(hand_config, recs)
    for columns in (20, 25, 30):
        for command in ("sessions", "models", "summary"):
            out = run(command, columns=columns, **kw).stdout
            cut = [word for word in out.split() if word.startswith("acme-")]
            assert len(set(cut[:3])) == 3, (command, columns, cut)


# ---- the ways a name can be asked for ----------------------------------------------------------------
@pytest.fixture
def named(hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> dict[str, Any]:
    names = ["Vibe-Trading-Dashboard", "acme-web", "acme-api", "work", "Work", "other"]
    recs = [make_record(f"n{i}", project=name, started_at=f"2026-09-2{i}T10:00:00Z", usage={"claude-sonnet-5-5": Usage(input=100_000)})
            for i, name in enumerate(names)]
    return hand(hand_config, recs)


@pytest.mark.parametrize("command", COMMANDS)
def test_an_unknown_project_is_matched_by_prefix_and_by_substring_before_spelling(command: str, named: dict[str, Any]) -> None:
    def hint(name: str) -> str:
        return lines(run(command, "--project", name, **named))[1]

    assert hint("vibe") == "no project is called 'vibe'; closest names: Vibe-Trading-Dashboard"  # the start, in any case
    assert hint("trading") == "no project is called 'trading'; closest names: Vibe-Trading-Dashboard"  # inside the name
    assert hint("acm") == "no project is called 'acm'; closest names: acme-api, acme-web"  # several: dearest first, then by name
    assert hint("dash") == "no project is called 'dash'; closest names: Vibe-Trading-Dashboard"
    assert hint("wok") == "no project is called 'wok'; closest names: Work, work"  # spelt about like: both cases, none merged


@pytest.mark.parametrize("command", COMMANDS)
def test_an_unknown_agent_with_data_under_the_others_says_so(command: str, named: dict[str, Any]) -> None:
    assert lines(run(command, "--agent", "gemini", **named)) == [
        "no sessions in this period (all time, agent gemini)", "the database has no gemini sessions"]


@pytest.mark.parametrize("command", COMMANDS)
def test_setup_advice_only_when_the_database_is_empty(command: str, hand_config: Callable[..., Path]) -> None:
    out = lines(run(command, "--since", "2031-01-01", **hand(hand_config, [])))  # nothing stored at all
    assert out[0] == "no sessions in this period (from 2031-01-01)" and len(out) == 2
    assert "agentfare ingest" in out[1] and "agentfare config" in out[1] and "data exists" not in out[1]
    out = lines(run(command, "--since", "2031-01-01"))  # the fixtures hold three sessions: the data is there, the view missed it
    assert out == ["no sessions in this period (from 2031-01-01)", "data exists from 2026-09-28 to 2026-09-30"]


@pytest.mark.parametrize("command", COMMANDS)
def test_the_dates_of_the_data_are_the_local_days(command: str, hand_config: Callable[..., Path], make_record: Callable[..., SessionRecord]) -> None:
    """Sessions at 2026-09-01 02:00 UTC and 2026-09-20 23:30 UTC are on the 31 August (22:00) and the 20 September (19:30) in Toronto."""
    recs = [make_record("e1", started_at="2026-09-01T02:00:00Z"), make_record("e2", started_at="2026-09-20T23:30:00Z")]
    kw = hand(hand_config, recs, timezone="America/Toronto")
    assert lines(run(command, "--since", "2031-01-01", **kw))[1] == "data exists from 2026-08-31 to 2026-09-20"


# ---- project names that differ only by case ---------------------------------------------------------
def test_projects_that_differ_only_by_case_are_listed_as_given_and_found_together(named: dict[str, Any]) -> None:
    s = data("summary", **named)
    assert sorted(p["project"] for p in s["by_project"] if p["project"].casefold() == "work") == ["Work", "work"]  # two rows, not one
    out = lines(run("summary", **named))
    assert sum(1 for line in out if line.startswith(("work ", "Work "))) == 2
    assert "names that differ only by case are listed apart (Work, work); --project ignores case and finds them all" in out
    for spelling in ("work", "WORK", "Work"):
        found = data("sessions", "--project", spelling, **named)
        assert sorted(e["project"] for e in found["sessions"]) == ["Work", "work"], spelling  # the same two, each as it is written
        assert found["filters"]["project"] == spelling
    assert not any("differ only by case" in line for line in lines(run("summary", "--project", "acme-web", **named)))


def test_the_help_of_project_says_the_match_ignores_case() -> None:
    for command in COMMANDS:
        text = CliRunner().invoke(app, [command, "--help"], env={"COLUMNS": "120"}).stdout
        help_text = " ".join("".join(" " if ch in "│╭╮╰╯─" else ch for ch in text).split())
        assert "The match ignores case: work finds both work and Work, which are listed apart." in help_text, command


# ---- sessions --sort cost ----------------------------------------------------------------------------
def test_a_list_sorted_by_cost_says_so_in_its_header() -> None:
    assert lines(run("sessions", "--sort", "cost"))[0] == "AgentFare sessions: all time; sorted by cost, dearest first"
    assert lines(run("sessions", "--sort", "cost", "--week"))[0] == "AgentFare sessions: week 2026-09-28 .. 2026-10-04; sorted by cost, dearest first"
    assert lines(run("sessions"))[0] == "AgentFare sessions: all time"
    assert lines(run("sessions", "--sort", "date"))[0] == "AgentFare sessions: all time"
    assert list(data("sessions", "--sort", "cost")) == list(data("sessions"))  # the JSON is as it was


# ---- prices on a narrow terminal ---------------------------------------------------------------------
@pytest.mark.parametrize("columns", [40, 60, 80])
def test_the_price_table_wraps_instead_of_running_past_the_terminal(columns: int) -> None:
    result = run("prices", columns=columns, ingest=False)
    assert result.exit_code == 0, result.output
    out = result.stdout.splitlines()
    assert max(len(line) for line in out if "pricing.yaml" not in line) <= columns  # only the path of the file stays whole
    text = "\n".join(out)
    for model in ("claude-opus-5-5", "gpt-6-astra", "gemini-2.5-pro", "gemini-3-flash"):
        assert text.count(model) >= 1, model
    assert "12.50" not in text or "gpt-6-astra" in text  # the numbers are still there
    if columns < 80:
        assert not any(line.startswith("  model ") for line in out)  # no header over columns that are not there
        assert any(line.strip().startswith("verified, checked") for line in out)
    assert json.loads(run("prices", "--json", columns=columns, ingest=False).stdout)["models"]  # the JSON does not depend on the width

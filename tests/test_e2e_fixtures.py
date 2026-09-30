"""End-to-end tests: the real CLI, in a subprocess, on the three synthetic fixtures, against hand-computed numbers.

Every number asserted here is derived by hand in ``tests/fixtures/EXPECTED.md`` (token by token, price by price) and
re-derived by ``scripts/verify_fixtures.py``, which reads the raw logs and ``pricing.yaml`` with its own code and
never imports agentfare. The literals below carry their arithmetic in comments; they are NOT computed from the
product's code, so a bug in a parser, in the cost engine or in the router shows up as a mismatch here.

Setup for every command: the installed ``agentfare`` entry point, ``--config tests/fixtures/config.yaml`` (sources
are the fixture logs, ``timezone: UTC``), a throwaway ``AGENTFARE_HOME``, ``AGENTFARE_NOW=2026-09-30T12:00:00Z``,
``COLUMNS=100`` and an empty ``HOME``: nothing of the real machine is read or written.

``TestFreshCloneScript`` checks what ``scripts/verify_fresh_clone.sh`` can do offline (usage, the FAIL exit, the expected numbers).
``TestDefinitionOfDone`` is the acceptance test of the product. Its third criterion, "a fresh clone plus
``pip install -e .`` gives a working ``agentfare --help``", is proven for real by ``scripts/verify_fresh_clone.sh``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG = FIXTURES / "config.yaml"
EXPECTED_MD = FIXTURES / "EXPECTED.md"
NOW = "2026-09-30T12:00:00Z"  # a Wednesday, ISO week 2026-W40 (Mon 2026-09-28 .. Sun 2026-10-04)

# =====================================================================================================
# THE HAND-COMPUTED NUMBERS (details and audit trail: tests/fixtures/EXPECTED.md)
# =====================================================================================================
# Prices, USD per 1M tokens, from src/agentfare/data/pricing.yaml (checked 2026-09-30):
#                       input  cache read  cache write 5m  cache write 1h  output
#   claude-opus-5-5      4.00      0.20          5.00            8.00        20.00
#   claude-haiku-4-5     1.00      0.10          1.25            2.00         5.00   (logged as claude-haiku-4-5-20251001)
#   gpt-6-astra         10.00      1.00         12.50           12.50        50.00
#   gpt-6.1-sol          2.00      0.10          2.50            2.50        10.00
#   gemini-2.5-pro       1.25      0.125         1.25 (input)    1.25 (input) 10.00
PRICES: dict[str, tuple[str, str, str, str, str]] = {
    "claude-opus-5-5": ("4.00", "0.20", "5.00", "8.00", "20.00"),
    "claude-haiku-4-5": ("1.00", "0.10", "1.25", "2.00", "5.00"),
    "claude-sonnet-5-5": ("2.00", "0.20", "2.50", "4.00", "10.00"),
    "gpt-6-astra": ("10.00", "1.00", "12.50", "12.50", "50.00"),
    "gpt-6.1-sol": ("2.00", "0.10", "2.50", "2.50", "10.00"),
    "gemini-2.5-pro": ("1.25", "0.125", "1.25", "1.25", "10.00"),
    "gemini-3.1-flash-lite": ("0.25", "0.025", "0.25", "0.25", "1.50"),
    "gemini-3-flash-preview": ("0.50", "0.05", "0.50", "0.50", "3.00"),
    "gemini-3.1-pro-preview": ("2.00", "0.20", "2.00", "2.00", "12.00"),
}
PRICE_BUCKETS = ("input", "cache_read", "cache_write", "cache_write_1h", "output")

# Tokens per (session, model), as (input, cache read, cache write 5m, cache write 1h, output). Counted by hand from the
# raw logs (EXPECTED.md lists every response):
TOKENS: dict[tuple[str, str], tuple[int, int, int, int, int]] = {
    # Claude: 36 Opus responses = 31 x (1,200 / 80,000 / 3,000 / 2,000 / 900) + 5 x (400 / 120,000 / 0 / 0 / 3,000)
    #   input 31 x 1,200 + 5 x 400 = 39,200;  cache read 31 x 80,000 + 5 x 120,000 = 3,080,000
    #   writes 5m 31 x 3,000 = 93,000, 1h 31 x 2,000 = 62,000;  output 31 x 900 + 5 x 3,000 = 42,900
    ("claude", "claude-opus-5-5"): (39_200, 3_080_000, 93_000, 62_000, 42_900),
    # 7 Haiku responses = 4 x (800 / 15,000 / 0 / 3,000 / 400) + 3 x (500 / 20,000 / 2,000 / 0 / 200)
    #   input 4 x 800 + 3 x 500 = 4,700;  cache read 4 x 15,000 + 3 x 20,000 = 120,000
    #   writes 5m 3 x 2,000 = 6,000, 1h 4 x 3,000 = 12,000;  output 4 x 400 + 3 x 200 = 2,200
    ("claude", "claude-haiku-4-5"): (4_700, 120_000, 6_000, 12_000, 2_200),
    # Codex: 6 responses, uncached = input - cached: 10k+4k+3k+3k+8k+12k = 40,000; cached 0+8k+12k+15k+12k+13k = 60,000
    #   output (reasoning included) 0.5k+1k+0.5k+1k+1k+2k = 6,000
    ("codex", "gpt-6-astra"): (40_000, 60_000, 0, 0, 6_000),
    # Gemini: 5 messages, input - cached + tool: 10k + 12k + 20k + 20k + 30k = 92,000; cached 0+20k+20k+20k+20k = 80,000
    #   output + thoughts: 1,000 + 1,500 + 2,000 + 1,000 + 4,000 = 9,500
    ("gemini", "gemini-2.5-pro"): (92_000, 80_000, 0, 0, 9_500),
}

# Cost in micro-cents (1 cent = 1,000,000 ucents; USD x 100,000,000):
#   claude-opus-5-5   39,200 x 4 + 3,080,000 x 0.20 + 93,000 x 5 + 62,000 x 8 + 42,900 x 20, all / 1M
#                     = 0.1568 + 0.616 + 0.465 + 0.496 + 0.858 = $2.5918
#   claude-haiku-4-5  4,700 x 1 + 120,000 x 0.10 + 6,000 x 1.25 + 12,000 x 2 + 2,200 x 5, all / 1M
#                     = 0.0047 + 0.012 + 0.0075 + 0.024 + 0.011 = $0.0592
#   gpt-6-astra       40,000 x 10 + 60,000 x 1 + 6,000 x 50, all / 1M = 0.40 + 0.06 + 0.30 = $0.76
#   gemini-2.5-pro    92,000 x 1.25 + 80,000 x 0.125 + 9,500 x 10, all / 1M = 0.115 + 0.01 + 0.095 = $0.22
UCENTS = {
    "claude-opus-5-5": 259_180_000,
    "claude-haiku-4-5": 5_920_000,
    "gpt-6-astra": 76_000_000,
    "gemini-2.5-pro": 22_000_000,
}
CLAUDE_UCENTS = 265_100_000  # 2.5918 + 0.0592 = $2.651
TOTAL_UCENTS = 363_100_000  # 2.651 + 0.76 + 0.22 = $3.631, prints as $3.63 (363 cents, half up)

# What the router says (docs/ROUTING.md; EXPECTED.md, "The router"):
#   codex: uncached 40,000 x 2 + cached 60,000 x 0.10 + output 6,000 x 10, all / 1M = 0.08 + 0.006 + 0.06 = $0.146 on
#          gpt-6.1-sol vs $0.76 actual: 76 / 0.146 = 5.205x >= 1.2x, so the switch is advised; saved $0.614
#   gemini: the same tokens on gpt-6.1-sol: 92,000 x 2 + 80,000 x 0.10 + 9,500 x 10 = 0.184 + 0.008 + 0.095 = $0.287 >
#          $0.22 actual, so nothing is advised ("already cheaper")
#   claude: score 7 is heavy, and router.recommend.heavy is null: kept
CODEX_ON_SOL_UCENTS = 14_600_000
GEMINI_ON_SOL_UCENTS = 28_700_000
CLAUDE_BEFORE_LOCAL_MIDNIGHT = 162_040_000  # the Claude session's spend before 10:00Z (Honolulu Sunday 2026-09-27), summed from its responses by the independent script
CLAUDE_AFTER_LOCAL_MIDNIGHT = 103_060_000  # and from 10:00Z on (Monday): 162,040,000 + 103,060,000 = 265,100,000
SAVED_UCENTS = 61_400_000  # 76,000,000 - 14,600,000
ROUTER_UCENTS = 301_700_000  # 363,100,000 - 61,400,000 = $3.017
SWITCH_LINE = "session 7c8d9e0f: you spent $0.76 on gpt-6-astra → gpt-6.1-sol would likely have handled this for ~$0.1460 (5.2x cheaper)"


def money(cents: int, ucents: int) -> dict[str, int]:
    return {"cents": cents, "ucents": ucents}


def bucket_tokens(input: int, cache_read: int, cache_write: int, output: int) -> dict[str, int]:
    return {"input": input, "cache_read": cache_read, "cache_write": cache_write, "output": output, "total": input + cache_read + cache_write + output}


# The token mix the CLI shows: cache_write is the 5-minute and 1-hour writes added together.
OPUS_TOKENS = bucket_tokens(39_200, 3_080_000, 155_000, 42_900)  # 93,000 + 62,000 = 155,000 writes; total 3,317,100
HAIKU_TOKENS = bucket_tokens(4_700, 120_000, 18_000, 2_200)  # 6,000 + 12,000 = 18,000 writes; total 144,900
ASTRA_TOKENS = bucket_tokens(40_000, 60_000, 0, 6_000)  # total 106,000
GEMINI_TOKENS = bucket_tokens(92_000, 80_000, 0, 9_500)  # total 181,500
CLAUDE_TOKENS = bucket_tokens(43_900, 3_200_000, 173_000, 45_100)  # Opus + Haiku; total 3,462,000
ALL_TOKENS = bucket_tokens(175_900, 3_340_000, 173_000, 60_600)  # total 3,749,500
ZERO_TOKENS = bucket_tokens(0, 0, 0, 0)

# =====================================================================================================
# running the real CLI
# =====================================================================================================
Cmd = list[str]


class Result(NamedTuple):
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


def entry_point() -> Path | None:
    """The ``agentfare`` console script that ``pip install`` put next to this interpreter."""
    for folder in (sysconfig.get_path("scripts"), str(Path(sys.executable).parent)):
        found = shutil.which("agentfare", path=folder)
        if found:
            return Path(found)
    return None


ENTRY = entry_point()
LAUNCH: Cmd = [str(ENTRY)] if ENTRY else [sys.executable, "-m", "agentfare"]


def clean_env(home: Path, now: str | None, columns: int, extra: dict[str, str | None] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTFARE_") and k not in ("COLUMNS", "LINES", "NO_COLOR", "FORCE_COLOR")}
    userhome = home / "userhome"  # an empty HOME: the real ~/.claude, ~/.codex, ~/.gemini and ~/.agentfare stay out of reach
    userhome.mkdir(exist_ok=True)
    env.update(HOME=str(userhome), AGENTFARE_HOME=str(home), COLUMNS=str(columns), PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_COLOR="1")
    if now:
        env["AGENTFARE_NOW"] = now
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)  # None removes a variable
        else:
            env[key] = value
    return env


def cli(
    home: Path,
    *args: str,
    now: str | None = NOW,
    config: Path | None = CONFIG,
    columns: int = 100,
    cwd: Path | None = None,
    env: dict[str, str | None] | None = None,
    launch: Cmd | None = None,
    stdin: int | None = subprocess.DEVNULL,
    ok: bool = True,
    merged: bool = False,
) -> Result:
    """Run ``agentfare [--config C] ARGS`` in a subprocess. Fails the test on a non-zero exit unless ``ok`` is False.

    ``merged=True`` sends stderr into the same pipe as stdout (what a terminal shows), so the order of the lines is the order in time."""
    home.mkdir(parents=True, exist_ok=True)
    command = [*(launch or LAUNCH), *(["--config", str(config)] if config else []), *args]
    pipes: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT} if merged else {"capture_output": True}
    done = subprocess.run(
        command, text=True, encoding="utf-8", timeout=180, stdin=stdin, cwd=cwd or home, env=clean_env(home, now, columns, env), **pipes,
    )
    result = Result(tuple(args), done.returncode, done.stdout, done.stderr)
    if ok:
        assert result.returncode == 0, f"agentfare {' '.join(args)} exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return result


def norm(text: str) -> str:
    """One line of output with the table padding and the bar-chart blocks squeezed out."""
    return " ".join(text.replace("█", " ").split())


def norm_lines(text: str) -> set[str]:
    return {n for line in text.splitlines() if (n := norm(line))}


def panel(text: str) -> list[str]:
    """The rows of a Rich panel without its border, whitespace squeezed."""
    rows = [norm(re.sub(r"[│╭╮╰╯─]", " ", line)) for line in text.splitlines()]
    return [r for r in rows if r]


def shown(row: str, rows: list[str]) -> bool:
    """``row`` is one of ``rows``, or is what two or three consecutive rows say together: a row of the panel that was wrapped."""
    return row in rows or any(" ".join(rows[i:j]) == row for i in range(len(rows)) for j in range(i + 2, min(i + 4, len(rows) + 1)))


def assert_prints(output: str, expected: list[str]) -> None:
    printed = norm_lines(output)
    missing = [line for line in expected if norm(line) not in printed]
    assert not missing, "lines not printed (whitespace squeezed):\n  " + "\n  ".join(missing) + "\n--- output ---\n" + output


# The lines the tests below look for in the text output are documented in tests/fixtures/EXPECTED.md as well
# (TestExpectedDocument checks that every one of them is written there).
SUMMARY_LINES = [
    "AgentFare summary: all time",
    "3 sessions, 3 projects, 4 models, total spend $3.63",
    "claude claude-opus-5-5 1 39.2k 3.1M 155k 42.9k $2.59 71.4%",
    "codex gpt-6-astra 1 40.0k 60.0k 0 6.0k $0.76 20.9%",
    "gemini gemini-2.5-pro 1 92.0k 80.0k 0 9.5k $0.22 6.1%",
    "claude claude-haiku-4-5 1 4.7k 120k 18.0k 2.2k $0.0592 1.6%",
    "Total 3 176k 3.3M 173k 60.6k $3.63 100%",
    "acme-shop 1 43.9k 3.2M 173k 45.1k $2.65 73.0%",
    "fake-repo 1 40.0k 60.0k 0 6.0k $0.76 20.9%",
    "demo-notes-app 1 92.0k 80.0k 0 9.5k $0.22 6.1%",
    "2026-09-28 1 43.9k 3.2M 173k 45.1k $2.65 73.0%",
    "2026-09-29 1 40.0k 60.0k 0 6.0k $0.76 20.9%",
    "2026-09-30 1 92.0k 80.0k 0 9.5k $0.22 6.1%",
]
SESSIONS_LINES = [
    "AgentFare sessions: all time",
    "09-30 gemini 4a6e8f10 demo-notes-app gemini-2.5-pro 9m55s 3 7 2 1 182k $0.22",
    "09-29 codex 7c8d9e0f fake-repo gpt-6-astra 10m00s 1 5 2 1 106k $0.76",
    "09-28 claude a1c7e402 acme-shop claude-opus-5-5 +1 1h38m 5 46 10 5 3.5M $2.65",
    "showing 3 of 3 sessions, total $3.63",
]
MODELS_LINES = [
    "4 models, 3 sessions, total spend $3.63",
    "1 claude-opus-5-5 anthropic 1 3.3M $2.59 71.4% $0.7813 $4.00 $20.00",
    "2 gpt-6-astra openai 1 106k $0.76 20.9% $7.17 $10.00 $50.00",
    "3 gemini-2.5-pro google 1 182k $0.22 6.1% $1.21 $1.25 $10.00",
    "4 claude-haiku-4-5 anthropic 1 145k $0.0592 1.6% $0.4086 $1.00 $5.00",
]
ADVISE_LINES = [
    "3 sessions, 1 with a switch advised",
    "1. " + SWITCH_LINE,
    "codex, fake-repo, 2026-09-29: score 2/10 (light): files_touched 2 +1, active_minutes 10 +1",
    "2 more sessions where no switch is advised (heavy, already cheaper, no verified price): $2.87 spent; --all lists them.",
    "2026-W40 3 1 $3.6310 $3.0170 $0.6140 16.9%",
    "Total 3 1 $3.6310 $3.0170 $0.6140 16.9%",
    "Of the saving, $0.6140 (100%) comes from sessions with prompts and $0.00 (0.0%) from sub-agent or background threads with 0 prompts.",
]
ADVISE_ALL_LINES = [
    "2. session a1c7e402: you spent $2.65 on claude-opus-5-5 (+1 more model): no switch advised, heavy: keep",
    "claude, acme-shop, 2026-09-28: score 7/10 (heavy): input_tokens 3.4M +1, tool_calls 46 +1, files_touched 10 +2, error_retries 5 +1, active_minutes 98 +2",
    "3. session 4a6e8f10: you spent $0.22 on gemini-2.5-pro: no switch advised, already cheaper than gpt-6.1-sol",
    "gemini, demo-notes-app, 2026-09-30: score 1/10 (light): files_touched 2 +1",
]
REPORT_LINES = [
    "# AgentFare weekly report: 2026-W40",
    "Sep 28 to Oct 4, 2026 (in progress). 3 sessions, 3 agents, 4 models, 3 projects.",
    "**I spent $3.63 on AI coding agents this week. In sessions you prompted yourself, a cost router would have saved 16.9% ($0.61) "
    "of the $3.63 they cost; across all sessions, including sub-agent and background threads: 16.9% ($0.61).**",
    "| Actual | $3.63 |",
    "| With the router | $3.02 |",
    "| Saved | $0.61 (16.9%) |",
    "| claude-opus-5-5 | 1 | $2.59 | 71.4% |",
    "| gpt-6-astra | 1 | $0.76 | 20.9% |",
    "| gemini-2.5-pro | 1 | $0.22 | 6.1% |",
    "| claude-haiku-4-5 | 1 | $0.06 | 1.6% |",
    "| claude | 1 | $2.65 | 73.0% |",
    "| Mon Sep 28 | 1 | $2.65 |",
    "| Tue Sep 29 | 1 | $0.76 |",
    "| Wed Sep 30 | 1 | $0.22 |",
    "| Light | 2 | $0.98 |",
    "| Heavy | 1 | $2.65 |",
    "| `7c8d9e0f` | codex | Project B | gpt-6-astra | gpt-6.1-sol | $0.76 | $0.15 | $0.61 (5.2x cheaper) |",
]

# `agentfare run --dry-run`, the fixtures' config, the fixtures ingested (one past session per agent: fewer than 5 of the agent and tier, so
# the default token budget of `router.run.default_tokens`), a working directory of two files. Each product below is tokens x USD per 1M,
# i.e. millionths of a dollar: 60,000 + 120,000 + 62,500 + 70,000 = 312,500 is $0.3125. The baseline is the model with the most spend in the
# agent's last 30 days of history, counted back from the newest session in the database (the Gemini one, 2026-09-30, so the window starts on
# 2026-08-31; the fixtures: Opus $2.59 beats Haiku $0.06; gpt-6-astra; gemini-2.5-pro), priced on the same budget.
#   claude light (26 chars: "fix the typo in the README"): light keywords typo + readme = -3, the medium keyword fix = +1, floored at 0 -> light
#     estimate on claude-sonnet-5-5: 30,000 x 2 + 600,000 x 0.20 + 25,000 x 2.50 + 7,000 x 10 = 60,000 + 120,000 + 62,500 + 70,000 = $0.3125
#     baseline on claude-opus-5-5:   30,000 x 4 + 600,000 x 0.20 + 25,000 x 5    + 7,000 x 20 = 120,000 + 120,000 + 125,000 + 140,000 = $0.5050
#     saving 0.5050 - 0.3125 = $0.1925, 0.1925 / 0.5050 = 38.1%
#   claude medium (27 chars: "refactor the billing module"): heavy keyword refactor = +2 -> score 2 -> medium
#     sonnet: 135,000 x 2 + 8,800,000 x 0.20 + 130,000 x 2.50 + 65,000 x 10 = 270,000 + 1,760,000 + 325,000 + 650,000 = $3.005
#     opus:   135,000 x 4 + 8,800,000 x 0.20 + 130,000 x 5    + 65,000 x 20 = 540,000 + 1,760,000 + 650,000 + 1,300,000 = $4.25
#     saving $1.245 (29.3%)
#   claude heavy (57 chars: "... and migrate it to the new API"): keywords refactor + migrat = 2 + 1 = +3, the medium keyword api = +1 -> score 4 -> heavy
#     opus: 770,000 x 4 + 118,000,000 x 0.20 + 1,500,000 x 5 + 480,000 x 20 = 3,080,000 + 23,600,000 + 7,500,000 + 9,600,000 = $43.78
RUN_CASES: list[tuple[str, str, list[str]]] = [
    ("claude", "fix the typo in the README", [
        "agent claude",
        "prompt 26 chars, from the command line",
        "tier light (score 0)",
        "- light keywords typo, readme: -3",
        "- medium keyword fix: +1",
        "model claude-sonnet-5-5 (router pick)",
        "estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)",
        "baseline ~$0.5050 on claude-opus-5-5 (your most used claude model in the last 30 days, since 2026-08-31)",
        "saving ~$0.1925 (38.1% less than the baseline)",
        "command claude --model claude-sonnet-5-5 -p '<prompt: 26 chars>'",
    ]),
    ("claude", "refactor the billing module", [
        "prompt 27 chars, from the command line",
        "tier medium (score 2)",
        "- heavy keyword refactor: +2",
        "model claude-sonnet-5-5 (router pick)",
        "estimate ~$3.01 on claude-sonnet-5-5, typical medium task (no history yet)",
        "baseline ~$4.25 on claude-opus-5-5 (your most used claude model in the last 30 days, since 2026-08-31)",
        "saving ~$1.25 (29.3% less than the baseline)",
        "command claude --model claude-sonnet-5-5 -p '<prompt: 27 chars>'",
    ]),
    ("claude", "refactor the billing module and migrate it to the new API", [
        "prompt 57 chars, from the command line",
        "tier heavy (score 4)",
        "- heavy keywords refactor, migrat: +3",
        "- medium keyword api: +1",
        "model claude-opus-5-5 (router pick)",
        "estimate ~$43.78 on claude-opus-5-5, typical heavy task (no history yet)",
        "baseline ~$43.78 on claude-opus-5-5 (your most used claude model in the last 30 days, since 2026-08-31)",
        "command claude --model claude-opus-5-5 -p '<prompt: 57 chars>'",  # the pick is the baseline: no saving row (TestRun.test_no_saving_row_when_...)
    ]),
    # codex light: 30,000 x 2 + 600,000 x 0.10 + 25,000 x 2.50 + 7,000 x 10 = 60,000 + 60,000 + 62,500 + 70,000 = $0.2525 on gpt-6.1-sol
    #   baseline on gpt-6-astra: 30,000 x 10 + 600,000 x 1 + 25,000 x 12.50 + 7,000 x 50 = 300,000 + 600,000 + 312,500 + 350,000 = $1.5625
    #   saving $1.31 (83.8%)
    ("codex", "fix the typo in the README", [
        "tier light (score 0)",
        "model gpt-6.1-sol (router pick)",
        "estimate ~$0.2525 on gpt-6.1-sol, typical light task (no history yet)",
        "baseline ~$1.56 on gpt-6-astra (your most used codex model in the last 30 days, since 2026-08-31)",
        "saving ~$1.31 (83.8% less than the baseline)",
    ]),
    # codex heavy: 770,000 x 10 + 118,000,000 x 1 + 1,500,000 x 12.50 + 480,000 x 50 = 7,700,000 + 118,000,000 + 18,750,000 + 24,000,000 = $168.45
    ("codex", "refactor the billing module and migrate it to the new API", [
        "tier heavy (score 4)",
        "model gpt-6-astra (router pick)",
        "estimate ~$168.45 on gpt-6-astra, typical heavy task (no history yet)",
    ]),
    # gemini light: 30,000 x 0.25 + 600,000 x 0.025 + 25,000 x 0.25 + 7,000 x 1.50 = 7,500 + 15,000 + 6,250 + 10,500 = $0.03925
    #   baseline on gemini-2.5-pro (the gemini model of the fixtures, cache writes at the input rate):
    #   30,000 x 1.25 + 600,000 x 0.125 + 25,000 x 1.25 + 7,000 x 10 = 37,500 + 75,000 + 31,250 + 70,000 = $0.21375
    #   saving 0.21375 - 0.03925 = $0.1745, 0.1745 / 0.21375 = 81.6%
    ("gemini", "fix the typo in the README", [
        "tier light (score 0)",
        "model gemini-3.1-flash-lite (router pick)",
        "estimate ~$0.0393 on gemini-3.1-flash-lite, typical light task (no history yet)",
        "baseline ~$0.2138 on gemini-2.5-pro (your most used gemini model in the last 30 days, since 2026-08-31)",
        "saving ~$0.1745 (81.6% less than the baseline)",
    ]),
    # gemini medium: 135,000 x 0.50 + 8,800,000 x 0.05 + 130,000 x 0.50 + 65,000 x 3 = 67,500 + 440,000 + 65,000 + 195,000 = $0.7675
    ("gemini", "refactor the billing module", [
        "tier medium (score 2)",
        "model gemini-3-flash-preview (router pick)",
        "estimate ~$0.7675 on gemini-3-flash-preview, typical medium task (no history yet)",
    ]),
]


# =====================================================================================================
# fixtures
# =====================================================================================================
@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A data directory that the first command fills by auto-ingesting the fixture logs; the tests only read it."""
    path = tmp_path_factory.mktemp("e2e-home")
    cli(path, "summary", "--json")  # first run: the ingest happens here
    return path


@pytest.fixture(scope="module")
def summary(home: Path) -> dict[str, Any]:
    return cli(home, "summary", "--json").json()


@pytest.fixture(scope="module")
def advise_all(home: Path) -> dict[str, Any]:
    return cli(home, "advise", "--all", "--json").json()


def fresh_home(tmp_path: Path, name: str = "home") -> Path:
    return tmp_path / name


def write_config(path: Path, **settings: Any) -> Path:
    """A config file whose only log sources are the fixtures (absolute paths), ``timezone: UTC`` unless overridden."""
    sources = {"claude": FIXTURES / "claude" / "projects", "codex": FIXTURES / "codex" / "sessions", "gemini": FIXTURES / "gemini" / "tmp"}
    data = {"timezone": "UTC", "sources": {agent: [str(folder)] for agent, folder in sources.items()}, **settings}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# =====================================================================================================
# the hand-computed literals are consistent with each other
# =====================================================================================================
class TestTheLiteralsAddUp:
    """A typo in a literal above would make a correct product fail (or a wrong one pass): recompute them from prices x tokens."""

    @pytest.mark.parametrize("key", list(TOKENS))
    def test_tokens_times_price_is_the_ucents_written_above(self, key: tuple[str, str]) -> None:
        prices = [Decimal(p) for p in PRICES[key[1]]]
        cost = sum(Decimal(n) * p for n, p in zip(TOKENS[key], prices, strict=True)) * 100  # USD/1M x tokens x 100 = ucents
        assert cost == UCENTS[key[1]]

    def test_the_sums(self) -> None:
        assert sum(UCENTS.values()) == TOTAL_UCENTS
        assert UCENTS["claude-opus-5-5"] + UCENTS["claude-haiku-4-5"] == CLAUDE_UCENTS
        assert TOTAL_UCENTS - SAVED_UCENTS == ROUTER_UCENTS
        assert UCENTS["gpt-6-astra"] - CODEX_ON_SOL_UCENTS == SAVED_UCENTS

    def test_repricing_on_the_recommended_model(self) -> None:
        sol = [Decimal(p) for p in PRICES["gpt-6.1-sol"]]
        for key, expected in ((("codex", "gpt-6-astra"), CODEX_ON_SOL_UCENTS), (("gemini", "gemini-2.5-pro"), GEMINI_ON_SOL_UCENTS)):
            assert sum(Decimal(n) * p for n, p in zip(TOKENS[key], sol, strict=True)) * 100 == expected

    def test_the_bucket_dicts_match_the_token_table(self) -> None:
        def folded(*keys: tuple[str, str]) -> dict[str, int]:
            rows = [TOKENS[k] for k in keys]
            return bucket_tokens(sum(r[0] for r in rows), sum(r[1] for r in rows), sum(r[2] + r[3] for r in rows), sum(r[4] for r in rows))

        assert OPUS_TOKENS == folded(("claude", "claude-opus-5-5"))
        assert HAIKU_TOKENS == folded(("claude", "claude-haiku-4-5"))
        assert CLAUDE_TOKENS == folded(("claude", "claude-opus-5-5"), ("claude", "claude-haiku-4-5"))
        assert ALL_TOKENS == folded(*TOKENS)


# =====================================================================================================
# summary
# =====================================================================================================
class TestSummary:
    def test_the_first_run_ingests_and_keeps_stdout_clean(self, tmp_path: Path) -> None:
        first = cli(fresh_home(tmp_path), "summary", "--json")
        assert first.json()["total"] == money(363, TOTAL_UCENTS)  # stdout is pure JSON even on the first run
        assert "ingested 3 new/updated sessions" in first.stderr  # the progress notes go to stderr

    def test_totals(self, summary: dict[str, Any]) -> None:
        assert summary["period"] == {"label": "all time", "since": None, "until": None}
        assert summary["filters"] == {"project": None, "agent": None}
        assert (summary["sessions"], summary["projects"], summary["models"]) == (3, 3, 4)
        assert summary["tokens"] == ALL_TOKENS
        assert summary["total"] == money(363, 363_100_000)  # 259.18 + 5.92 + 76 + 22 = 363.10 cents
        assert summary["excluded_tokens"] == 0
        assert summary["warnings"] == []

    def test_by_model(self, summary: dict[str, Any]) -> None:
        # share = cost / 363.1 cents, in basis points: 259.18 -> 7138, 76 -> 2093, 22 -> 606, 5.92 -> 163 (they add up to 10,000)
        assert summary["by_model"] == [
            {"agent": "claude", "model": "claude-opus-5-5", "provider": "anthropic", "status": "priced", "sessions": 1, "tokens": OPUS_TOKENS,
             "cost": money(259, 259_180_000), "share_bp": 7138, "unpriced_tokens": 0},
            {"agent": "codex", "model": "gpt-6-astra", "provider": "openai", "status": "priced", "sessions": 1, "tokens": ASTRA_TOKENS,
             "cost": money(76, 76_000_000), "share_bp": 2093, "unpriced_tokens": 0},
            {"agent": "gemini", "model": "gemini-2.5-pro", "provider": "google", "status": "priced", "sessions": 1, "tokens": GEMINI_TOKENS,
             "cost": money(22, 22_000_000), "share_bp": 606, "unpriced_tokens": 0},
            {"agent": "claude", "model": "claude-haiku-4-5", "provider": "anthropic", "status": "priced", "sessions": 1, "tokens": HAIKU_TOKENS,
             "cost": money(6, 5_920_000), "share_bp": 163, "unpriced_tokens": 0},  # logged as claude-haiku-4-5-20251001, priced as its alias
        ]
        assert sum(row["share_bp"] for row in summary["by_model"]) == 10_000

    def test_by_project(self, summary: dict[str, Any]) -> None:
        assert summary["by_project"] == [
            {"project": "acme-shop", "sessions": 1, "tokens": CLAUDE_TOKENS, "cost": money(265, CLAUDE_UCENTS), "share_bp": 7301, "unpriced_tokens": 0},
            {"project": "fake-repo", "sessions": 1, "tokens": ASTRA_TOKENS, "cost": money(76, 76_000_000), "share_bp": 2093, "unpriced_tokens": 0},
            {"project": "demo-notes-app", "sessions": 1, "tokens": GEMINI_TOKENS, "cost": money(22, 22_000_000), "share_bp": 606, "unpriced_tokens": 0},
        ]  # 265.1 / 363.1 = 73.01%

    def test_by_day(self, summary: dict[str, Any]) -> None:
        assert summary["by_day"] == [
            {"day": "2026-09-28", "sessions": 1, "tokens": CLAUDE_TOKENS, "cost": money(265, CLAUDE_UCENTS), "share_bp": 7301, "unpriced_tokens": 0},
            {"day": "2026-09-29", "sessions": 1, "tokens": ASTRA_TOKENS, "cost": money(76, 76_000_000), "share_bp": 2093, "unpriced_tokens": 0},
            {"day": "2026-09-30", "sessions": 1, "tokens": GEMINI_TOKENS, "cost": money(22, 22_000_000), "share_bp": 606, "unpriced_tokens": 0},
        ]

    def test_by_week(self, summary: dict[str, Any]) -> None:
        # Mon 2026-09-28, Tue 09-29 and Wed 09-30 are all in ISO week 2026-W40
        assert summary["by_week"] == [
            {"week": "2026-W40", "sessions": 3, "tokens": ALL_TOKENS, "cost": money(363, TOTAL_UCENTS), "share_bp": 10_000, "unpriced_tokens": 0}
        ]

    def test_to_the_cent(self, summary: dict[str, Any], home: Path) -> None:
        """The printed total is the sum of the hand-computed model costs, rounded once (half up) at the end."""
        exact = sum(UCENTS.values())
        assert exact == 363_100_000  # 259,180,000 + 5,920,000 + 76,000,000 + 22,000,000 ucents = 363.10 cents
        assert summary["total"]["ucents"] == exact
        assert summary["total"]["cents"] == (exact + 500_000) // 1_000_000 == 363  # 363.10 cents -> 363
        by_model = {row["model"]: row["cost"] for row in summary["by_model"]}
        assert {m: c["ucents"] for m, c in by_model.items()} == UCENTS
        assert {m: c["cents"] for m, c in by_model.items()} == {"claude-opus-5-5": 259, "claude-haiku-4-5": 6, "gpt-6-astra": 76, "gemini-2.5-pro": 22}
        assert sum(c["cents"] for c in by_model.values()) == summary["total"]["cents"]  # rounded rows add up to the rounded total
        for group in ("by_project", "by_day"):
            assert sum(row["cost"]["cents"] for row in summary[group]) == 363  # 265 + 76 + 22
        assert "total spend $3.63" in cli(home, "summary").stdout

    def test_text(self, home: Path) -> None:
        out = cli(home, "summary").stdout
        assert_prints(out, SUMMARY_LINES)
        assert "warning" not in out.lower()  # every fixture model has a verified price

    def test_the_one_hour_cache_writes_are_priced_at_their_own_rate(self, summary: dict[str, Any]) -> None:
        # A token at $P per 1M costs 100 x P ucents: 500 ucents as a 5-minute write, 800 as a 1-hour write. Billing the 62,000
        # one-hour writes as 5-minute ones would lose 62,000 x (800 - 500) = 18,600,000 ucents ($0.186).
        opus = next(r for r in summary["by_model"] if r["model"] == "claude-opus-5-5")
        assert opus["cost"]["ucents"] == 259_180_000
        assert opus["cost"]["ucents"] != 259_180_000 - 62_000 * (800 - 500) == 240_580_000

    def test_the_summary_is_stable_when_ingested_again(self, tmp_path: Path) -> None:
        home = fresh_home(tmp_path)
        assert cli(home, "summary", "--json").json()["total"] == money(363, TOTAL_UCENTS)
        again = cli(home, "ingest")
        assert "read 0 session records from 3 files (0 parsed, 3 unchanged); the database holds 3 distinct sessions" in again.stdout  # incremental: nothing changed
        assert cli(home, "--no-ingest", "summary", "--json").json()["total"] == money(363, TOTAL_UCENTS)
        full = cli(home, "ingest", "--full")
        assert "3 sessions stored (claude: 1, codex: 1, gemini: 1)" in full.stdout
        assert cli(home, "--no-ingest", "summary", "--json").json()["total"] == money(363, TOTAL_UCENTS)  # re-parsing changes nothing (no double count)


class TestLiveLogs:
    """Agents keep appending to their logs: the next command picks the growth up, and nothing is counted twice."""

    def test_a_log_that_grows_is_ingested_again_and_a_repeated_response_is_not_billed_twice(self, tmp_path: Path) -> None:
        folder = tmp_path / "codex"
        folder.mkdir()
        (source,) = (FIXTURES / "codex" / "sessions").rglob("rollout-*.jsonl")
        log = folder / source.name
        log.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        config = write_config(tmp_path / "config.yaml", sources={"claude": [], "gemini": [], "codex": [str(folder)]})
        home = fresh_home(tmp_path)

        def state() -> tuple[int, dict[str, int], int, int]:
            data = cli(home, "summary", "--json", config=config).json()
            session = cli(home, "sessions", "--json", config=config).json()["sessions"][0]
            return data["total"]["ucents"], data["tokens"], session["wall_seconds"], session["active_seconds"]

        assert state() == (76_000_000, ASTRA_TOKENS, 600, 600)  # the fixture as it is: $0.76
        # a seventh response, ten minutes after the last event: 20,000 input of which 10,000 cached, 1,000 output on gpt-6-astra
        #   uncached 10,000 x 10 + cached 10,000 x 1 + output 1,000 x 50 = 100,000 + 10,000 + 50,000 = $0.16 more
        thread, turn = "01a0d8b0-1a2b-7c3d-8e4f-5a6b7c8d9e0f", "01a0d8b0-1c00-7000-8000-000000000001"
        usage = {"input_tokens": 20_000, "cached_input_tokens": 10_000, "cache_write_input_tokens": 0, "output_tokens": 1_000, "reasoning_output_tokens": 0, "total_tokens": 21_000}
        record = {"timestamp": "2026-09-29T10:20:00.000Z", "ordinal": 50, "type": "token_usage_record",
                  "payload": {"thread_id": thread, "turn_id": turn, "session_id": thread, "root_turn_id": turn, "response_id": "resp_fake_0007", "usage": usage}}
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        grown = bucket_tokens(50_000, 70_000, 0, 7_000)  # 40,000 + 10,000 uncached; 60,000 + 10,000 cached; 6,000 + 1,000 output
        assert state() == (92_000_000, grown, 1_200, 1_200)  # $0.76 + $0.16; 600 s more wall time, and the 10-minute gap is under the 30-minute idle limit
        # the same response written again (logs do repeat themselves) and a line cut off in the middle: nothing changes
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n" + '{"timestamp":"2026-09-29T10:21:00.000Z","ordinal":51,"type":"event_msg","payload":{"type":"toke')
        assert state() == (92_000_000, grown, 1_200, 1_200)
        assert cli(home, "sessions", "--json", config=config).json()["count"] == 1  # still one session


class TestFilters:
    """--week, --week-of, --since, --until, --project, --agent and the timezone, with the frozen clock."""

    def totals(self, home: Path, *args: str, now: str | None = NOW, config: Path | None = CONFIG) -> tuple[int, int]:
        data = cli(home, "summary", "--json", *args, now=now, config=config).json()
        return data["sessions"], data["total"]["ucents"]

    def test_week_is_the_iso_week_of_the_frozen_now(self, home: Path) -> None:
        assert self.totals(home, "--week") == (3, TOTAL_UCENTS)  # 2026-09-30 is in W40, where all three sessions are
        data = cli(home, "summary", "--week", "--json").json()
        assert data["period"] == {"label": "week 2026-09-28 .. 2026-10-04", "since": "2026-09-28T00:00:00Z", "until": "2026-10-05T00:00:00Z"}

    def test_a_now_in_another_week_finds_nothing(self, home: Path) -> None:
        assert self.totals(home, "--week", now="2026-10-05T12:00:00Z") == (0, 0)  # Monday, 2026-W41
        assert self.totals(home, "--week", now="2026-09-27T12:00:00Z") == (0, 0)  # Sunday, 2026-W39: the sessions are still to come
        assert "no sessions in this period (week 2026-10-05 .. 2026-10-11)" in cli(home, "summary", "--week", now="2026-10-05T12:00:00Z").stdout

    def test_the_last_second_of_the_week_still_counts(self, home: Path) -> None:
        assert self.totals(home, "--week", now="2026-10-04T23:59:59Z") == (3, TOTAL_UCENTS)  # Sunday night, still W40
        assert self.totals(home, "--week", now="2026-09-28T00:00:00Z") == (3, TOTAL_UCENTS)  # Monday midnight, the first second of W40

    def test_week_of(self, home: Path) -> None:
        assert self.totals(home, "--week-of", "2026-W40") == (3, TOTAL_UCENTS)
        assert self.totals(home, "--week-of", "2026-W39") == (0, 0)
        assert self.totals(home, "--week-of", "2026-W41") == (0, 0)

    def test_since_and_until_are_inclusive_days(self, home: Path) -> None:
        assert self.totals(home, "--since", "2026-09-29") == (2, 98_000_000)  # codex 76,000,000 + gemini 22,000,000
        assert self.totals(home, "--until", "2026-09-28") == (1, CLAUDE_UCENTS)  # the claude session started on 2026-09-28
        assert self.totals(home, "--since", "2026-09-29", "--until", "2026-09-29") == (1, 76_000_000)
        assert self.totals(home, "--since", "2026-09-30", "--until", "2026-09-30") == (1, 22_000_000)
        assert self.totals(home, "--since", "2026-10-01") == (0, 0)

    def test_agent_and_project(self, home: Path) -> None:
        assert self.totals(home, "--agent", "codex") == (1, 76_000_000)
        assert self.totals(home, "--agent", "claude") == (1, CLAUDE_UCENTS)
        assert self.totals(home, "--project", "ACME-SHOP") == (1, CLAUDE_UCENTS)  # case-insensitive
        assert self.totals(home, "--project", "demo-notes-app", "--agent", "gemini") == (1, 22_000_000)
        assert self.totals(home, "--project", "demo-notes-app", "--agent", "codex") == (0, 0)

    def test_a_bad_filter_is_a_usage_error(self, home: Path) -> None:
        bad = cli(home, "summary", "--week", "--since", "2026-09-28", ok=False)
        assert bad.returncode == 2 and "error:" in bad.stderr
        assert cli(home, "summary", "--week-of", "2026-W99", ok=False).returncode == 2

    def test_days_and_weeks_follow_the_configured_timezone(self, tmp_path: Path) -> None:
        """Honolulu is UTC-10: local midnight is 10:00Z. Money belongs to when it was spent, so the Claude session (09:00Z to 11:27Z, Mon
        2026-09-28) is split: what it spent before 10:00Z was Sunday 2026-09-27 (week W39), the rest Monday (W40)."""
        config = write_config(tmp_path / "config.yaml", timezone="Pacific/Honolulu")
        data = cli(fresh_home(tmp_path), "summary", "--json", config=config).json()
        # codex 10:00Z -> Tue 09-29 00:00; gemini 09:00Z (Wed 09-30 in UTC) -> Tue 09-29 23:00: both are Tuesday, 76,000,000 + 22,000,000
        assert [(r["day"], r["cost"]["ucents"]) for r in data["by_day"]] == [
            ("2026-09-27", CLAUDE_BEFORE_LOCAL_MIDNIGHT), ("2026-09-28", CLAUDE_AFTER_LOCAL_MIDNIGHT), ("2026-09-29", 98_000_000),
        ]
        assert [(r["week"], r["cost"]["ucents"]) for r in data["by_week"]] == [
            ("2026-W39", CLAUDE_BEFORE_LOCAL_MIDNIGHT), ("2026-W40", CLAUDE_AFTER_LOCAL_MIDNIGHT + 98_000_000),
        ]
        assert data["total"]["ucents"] == TOTAL_UCENTS and data["sessions"] == 3  # the whole session is still one session, counted once
        assert CLAUDE_BEFORE_LOCAL_MIDNIGHT + CLAUDE_AFTER_LOCAL_MIDNIGHT == CLAUDE_UCENTS
        week = cli(fresh_home(tmp_path), "summary", "--week", "--json", config=config).json()
        # now = Wed 02:00 local, W40 starts Monday 00:00 in Honolulu: the Claude session is in it, but only what it spent from then on
        assert (week["sessions"], week["total"]["ucents"]) == (3, CLAUDE_AFTER_LOCAL_MIDNIGHT + 98_000_000)
        assert week["period"]["since"] == "2026-09-28T10:00:00Z"
        sessions = cli(fresh_home(tmp_path), "sessions", "--week", "--json", config=config).json()
        claude = next(s for s in sessions["sessions"] if s["agent"] == "claude")
        assert claude["clipped"] is True and claude["cost"]["ucents"] == CLAUDE_AFTER_LOCAL_MIDNIGHT  # what was spent inside the period only
        assert claude["wall_seconds"] == 8_840 and claude["tool_calls"] == 46  # everything else about it stays whole-session
        assert sessions["total"]["ucents"] == week["total"]["ucents"]  # sessions, summary (and models) agree for the same period
        advise = cli(fresh_home(tmp_path), "advise", "--json", config=config).json()
        # W39: the Claude session's Sunday part, kept (heavy). W40: its Monday part + codex + gemini, $0.76 -> $0.146 switched, 30.5% saved.
        # The session is in both weeks, and once in the total.
        assert [(w["week"], w["sessions"], w["switched"], w["actual"]["ucents"], w["recommended"]["ucents"], w["saved"]["ucents"], w["saved_bp"]) for w in advise["weeks"]] == [
            ("2026-W39", 1, 0, CLAUDE_BEFORE_LOCAL_MIDNIGHT, CLAUDE_BEFORE_LOCAL_MIDNIGHT, 0, 0),
            ("2026-W40", 3, 1, CLAUDE_AFTER_LOCAL_MIDNIGHT + 98_000_000, CLAUDE_AFTER_LOCAL_MIDNIGHT + 98_000_000 - SAVED_UCENTS, SAVED_UCENTS, 3054),
        ]
        assert (advise["total"]["sessions"], advise["total"]["actual"]["ucents"], advise["total"]["saved"]["ucents"], advise["total"]["saved_bp"]) == (3, TOTAL_UCENTS, SAVED_UCENTS, 1691)

    def test_the_timezone_split_agrees_with_the_independent_derivation(self) -> None:
        """scripts/verify_fixtures.py prices every response exactly and puts it on the local day of its own record time."""
        verifier = load_verifier()
        models, aliases = verifier.load_prices()
        zone = ZoneInfo("Pacific/Honolulu")
        by_day: dict[str, Any] = {}
        for session in verifier.load_sessions():
            for r in session.responses:
                day = r["when"].astimezone(zone).date().isoformat()
                by_day[day] = by_day.get(day, 0) + verifier.price_of(r["tokens"], models[aliases.get(r["model"], r["model"])])
        assert {day: verifier.ucents(cost) for day, cost in by_day.items()} == {
            "2026-09-27": CLAUDE_BEFORE_LOCAL_MIDNIGHT, "2026-09-28": CLAUDE_AFTER_LOCAL_MIDNIGHT, "2026-09-29": 98_000_000,
        }

    def test_the_idle_gap_setting_changes_active_time_only(self, tmp_path: Path) -> None:
        """The Claude log is silent from 09:34:10 to 10:22:40 (2,910 s). Over the 30-minute default that gap is not active time;
        with idle_gap_minutes: 60 nothing is left out, so active = wall = 09:00:00 to 11:27:20 = 8,840 s. Tokens and money do not move."""
        config = write_config(tmp_path / "config.yaml", idle_gap_minutes=60)
        sessions = cli(fresh_home(tmp_path), "sessions", "--json", config=config).json()["sessions"]
        by_agent = {s["agent"]: s for s in sessions}
        assert by_agent["claude"]["active_seconds"] == 8_840 == by_agent["claude"]["wall_seconds"]
        assert (by_agent["codex"]["active_seconds"], by_agent["gemini"]["active_seconds"]) == (600, 595)  # no gap over 60 minutes to remove
        assert sum(s["cost"]["ucents"] for s in sessions) == TOTAL_UCENTS
        advise = cli(fresh_home(tmp_path), "advise", "--all", "--json", config=config).json()
        claude = next(s for s in advise["sessions"] if s["agent"] == "claude")
        assert claude["signals"]["active_minutes"] == pytest.approx(8840 / 60)  # 147.3 minutes, still >= 80: 2 points, score unchanged


# =====================================================================================================
# sessions
# =====================================================================================================
class TestSessions:
    def test_json(self, home: Path) -> None:
        data = cli(home, "sessions", "--json").json()
        assert (data["count"], data["shown"]) == (3, 3)
        assert data["tokens"] == ALL_TOKENS and data["total"] == money(363, TOTAL_UCENTS)
        gemini, codex, claude = data["sessions"]  # newest first
        assert gemini == {
            "date": "2026-09-30", "started_at": "2026-09-30T09:00:00Z", "agent": "gemini", "session_id": "9f3c2a71-5b8e-4d0a-9c1e-2b7d4a6e8f10",
            "short_id": "4a6e8f10", "project": "demo-notes-app", "primary_model": "gemini-2.5-pro", "other_models": 0,
            "models": [{"model": "gemini-2.5-pro", "tokens": GEMINI_TOKENS, "cost": money(22, 22_000_000)}],
            # 09:00:00 -> 09:09:55 = 595 s; 3 prompts (u1, u2, u3), 7 tool calls, 2 files written (notes.py, storage.py: config.py's write failed),
            # 1 error (that failed write), 5 billed messages
            "active_seconds": 595, "wall_seconds": 595, "prompts": 3, "tool_calls": 7, "files_touched": 2, "error_retries": 1, "turns": 5,
            "tokens": GEMINI_TOKENS, "cost": money(22, 22_000_000), "unpriced_tokens": 0, "clipped": False,
        }
        assert codex == {
            "date": "2026-09-29", "started_at": "2026-09-29T10:00:00Z", "agent": "codex", "session_id": "01a0d8b0-1a2b-7c3d-8e4f-5a6b7c8d9e0f",
            "short_id": "7c8d9e0f", "project": "fake-repo", "primary_model": "gpt-6-astra", "other_models": 0,
            "models": [{"model": "gpt-6-astra", "tokens": ASTRA_TOKENS, "cost": money(76, 76_000_000)}],
            # 10:00:00 -> 10:10:00 = 600 s; 1 prompt, 5 tool calls, 2 files (an update and an add), 1 failed script, 6 responses
            "active_seconds": 600, "wall_seconds": 600, "prompts": 1, "tool_calls": 5, "files_touched": 2, "error_retries": 1, "turns": 6,
            "tokens": ASTRA_TOKENS, "cost": money(76, 76_000_000), "unpriced_tokens": 0, "clipped": False,
        }
        assert claude == {
            "date": "2026-09-28", "started_at": "2026-09-28T09:00:00Z", "agent": "claude", "session_id": "2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402",
            "short_id": "a1c7e402", "project": "acme-shop", "primary_model": "claude-opus-5-5", "other_models": 1,
            "models": [
                {"model": "claude-opus-5-5", "tokens": OPUS_TOKENS, "cost": money(259, 259_180_000)},
                {"model": "claude-haiku-4-5", "tokens": HAIKU_TOKENS, "cost": money(6, 5_920_000)},
            ],
            # 09:00:00 -> 11:27:20 = 8,840 s wall, of which 2,910 s (09:34:10 -> 10:22:40) idle: 5,930 s active; 5 prompts; 46 tool calls;
            # 10 files written; 5 tool errors (denied and blocked calls are not errors); 36 + 7 = 43 responses
            "active_seconds": 5_930, "wall_seconds": 8_840, "prompts": 5, "tool_calls": 46, "files_touched": 10, "error_retries": 5, "turns": 43,
            "tokens": CLAUDE_TOKENS, "cost": money(265, CLAUDE_UCENTS), "unpriced_tokens": 0, "clipped": False,
        }

    def test_text(self, home: Path) -> None:
        assert_prints(cli(home, "sessions").stdout, SESSIONS_LINES)

    def test_sort_limit_and_filters(self, home: Path) -> None:
        by_cost = cli(home, "sessions", "--sort", "cost", "--json").json()["sessions"]
        assert [s["short_id"] for s in by_cost] == ["a1c7e402", "7c8d9e0f", "4a6e8f10"]  # $2.651, $0.76, $0.22
        limited = cli(home, "sessions", "--sort", "cost", "--limit", "1", "--json").json()
        assert (limited["count"], limited["shown"], limited["sessions"][0]["short_id"]) == (3, 1, "a1c7e402")
        assert [s["agent"] for s in cli(home, "sessions", "--agent", "codex", "--json").json()["sessions"]] == ["codex"]
        assert [s["project"] for s in cli(home, "sessions", "--project", "Acme-Shop", "--json").json()["sessions"]] == ["acme-shop"]
        assert cli(home, "sessions", "--since", "2026-09-30", "--json").json()["count"] == 1


# =====================================================================================================
# models
# =====================================================================================================
class TestModels:
    def test_json(self, home: Path) -> None:
        data = cli(home, "models", "--json").json()
        assert (data["sessions"], data["tokens"], data["total"]) == (3, ALL_TOKENS, money(363, TOTAL_UCENTS))
        # blended $/1M = cost / tokens x 1M, rounded to a ucent: 259,180,000 x 1,000,000 / 3,317,100 = 78,134,515
        #   76,000,000 / 106,000 -> 716,981,132;  22,000,000 / 181,500 -> 121,212,121;  5,920,000 / 144,900 -> 40,855,763
        assert data["models"] == [
            {"rank": 1, "model": "claude-opus-5-5", "provider": "anthropic", "status": "priced", "sessions": 1, "tokens": OPUS_TOKENS,
             "cost": money(259, 259_180_000), "share_bp": 7138, "unpriced_tokens": 0, "blended_per_mtok": money(78, 78_134_515),
             "list_price_per_mtok": {"input": money(400, 400_000_000), "output": money(2000, 2_000_000_000)}},
            {"rank": 2, "model": "gpt-6-astra", "provider": "openai", "status": "priced", "sessions": 1, "tokens": ASTRA_TOKENS,
             "cost": money(76, 76_000_000), "share_bp": 2093, "unpriced_tokens": 0, "blended_per_mtok": money(717, 716_981_132),
             "list_price_per_mtok": {"input": money(1000, 1_000_000_000), "output": money(5000, 5_000_000_000)}},
            {"rank": 3, "model": "gemini-2.5-pro", "provider": "google", "status": "priced", "sessions": 1, "tokens": GEMINI_TOKENS,
             "cost": money(22, 22_000_000), "share_bp": 606, "unpriced_tokens": 0, "blended_per_mtok": money(121, 121_212_121),
             "list_price_per_mtok": {"input": money(125, 125_000_000), "output": money(1000, 1_000_000_000)}},
            {"rank": 4, "model": "claude-haiku-4-5", "provider": "anthropic", "status": "priced", "sessions": 1, "tokens": HAIKU_TOKENS,
             "cost": money(6, 5_920_000), "share_bp": 163, "unpriced_tokens": 0, "blended_per_mtok": money(41, 40_855_763),
             "list_price_per_mtok": {"input": money(100, 100_000_000), "output": money(500, 500_000_000)}},
        ]

    def test_text(self, home: Path) -> None:
        assert_prints(cli(home, "models").stdout, MODELS_LINES)


# =====================================================================================================
# advise
# =====================================================================================================
class TestAdvise:
    def test_json_savings_number(self, home: Path) -> None:
        data = cli(home, "advise", "--json").json()
        assert data["counts"] == {"sessions": 3, "listed": 1, "candidates": 1}
        (session,) = data["sessions"]  # only the switchable session is listed on its own
        assert session["line"] == SWITCH_LINE
        assert session["saved"] == money(61, SAVED_UCENTS)  # 76,000,000 - 14,600,000 ucents = $0.614
        assert session["recommended"] == money(15, CODEX_ON_SOL_UCENTS)  # $0.146 is 14.6 cents, listed as 15
        assert session["actual"] == money(76, 76_000_000)
        assert session["ratio"] == 5.21  # 76 / 0.146 = 5.2055
        # the other two are folded into one line: $2.651 + $0.22 = $2.871
        assert data["not_listed"]["no_switch"] == {"sessions": 2, "switchable": 0, "actual": money(287, 287_100_000), "saved": money(0, 0)}
        assert data["not_listed"]["background"]["sessions"] == 0 and data["not_listed"]["small"]["sessions"] == 0

    def test_the_weekly_router_savings_row(self, home: Path) -> None:
        data = cli(home, "advise", "--json").json()
        # W40: 3 fully priced sessions, 1 switched. Actual $3.631; with the switch $3.631 - $0.614 = $3.017; saved 0.614 / 3.631 = 16.91% -> 1691 bp
        week = {"week": "2026-W40", "sessions": 3, "switched": 1, "skipped": 0, "actual": money(363, TOTAL_UCENTS),
                "recommended": money(302, ROUTER_UCENTS), "saved": money(61, SAVED_UCENTS), "saved_bp": 1691}
        assert data["weeks"] == [week]
        assert data["total"] == {**week, "week": "total"}
        assert data["warnings"] == []

    def test_all_sessions_signals_score_tier_and_advice(self, advise_all: dict[str, Any]) -> None:
        codex, claude, gemini = advise_all["sessions"]  # the switch first, then the others by spend
        assert advise_all["counts"] == {"sessions": 3, "listed": 3, "candidates": 3}
        assert codex["short_id"] == "7c8d9e0f" and codex["switch"] is True
        # codex: input 100,000 (< 2M: 0), 5 tool calls (< 25: 0), 2 files (>= 1: 1), 1 error (< 2: 0), 600 s = 10.0 min (>= 10: 1) -> 2, light
        assert codex["signals"] == {"input_tokens": 100_000, "tool_calls": 5, "files_touched": 2, "error_retries": 1, "active_minutes": 10.0}
        assert codex["points"] == {"input_tokens": 0, "tool_calls": 0, "files_touched": 1, "error_retries": 0, "active_minutes": 1}
        assert (codex["score"], codex["tier"], codex["prompts"], codex["model"], codex["models"]) == (2, "light", 1, "gpt-6-astra", ["gpt-6-astra"])
        assert (codex["recommended_model"], codex["ratio"], codex["note"]) == ("gpt-6.1-sol", 5.21, "")
        assert codex["explanation"] == "score 2/10 (light): files_touched 2 +1, active_minutes 10 +1"
        assert codex["actual"] == money(76, 76_000_000) and codex["recommended"] == money(15, CODEX_ON_SOL_UCENTS) and codex["saved"] == money(61, SAVED_UCENTS)
        # claude: input 3,416,900 (>= 2M: 1), 46 tool calls (>= 25: 1), 10 files (>= 6: 2), 5 errors (>= 2: 1), 5,930 s = 98.8 min (>= 80: 2) -> 7, heavy: kept
        assert claude["short_id"] == "a1c7e402" and claude["switch"] is False
        assert claude["signals"]["input_tokens"] == 3_416_900  # 39,200 + 4,700 + 3,080,000 + 120,000 + 93,000 + 6,000 + 62,000 + 12,000
        assert (claude["signals"]["tool_calls"], claude["signals"]["files_touched"], claude["signals"]["error_retries"]) == (46, 10, 5)
        assert claude["signals"]["active_minutes"] == pytest.approx(5930 / 60)
        assert claude["points"] == {"input_tokens": 1, "tool_calls": 1, "files_touched": 2, "error_retries": 1, "active_minutes": 2}
        assert (claude["score"], claude["tier"], claude["note"], claude["recommended_model"], claude["ratio"]) == (7, "heavy", "heavy: keep", None, None)
        assert claude["models"] == ["claude-haiku-4-5-20251001", "claude-opus-5-5"]  # as logged
        assert claude["actual"] == money(265, CLAUDE_UCENTS) and claude["saved"] == money(0, 0)
        # gemini: input 172,000 (0), 7 tools (0), 2 files (1), 1 error (0), 595 s = 9.92 min (< 10: 0) -> 1, light; on gpt-6.1-sol the same tokens cost
        # $0.287 > $0.22, so nothing is advised
        assert gemini["short_id"] == "4a6e8f10" and gemini["switch"] is False
        assert gemini["signals"]["input_tokens"] == 172_000  # 92,000 + 80,000
        assert gemini["signals"]["active_minutes"] == pytest.approx(595 / 60)
        assert gemini["points"] == {"input_tokens": 0, "tool_calls": 0, "files_touched": 1, "error_retries": 0, "active_minutes": 0}
        assert (gemini["score"], gemini["tier"], gemini["note"], gemini["recommended_model"]) == (1, "light", "already cheaper than gpt-6.1-sol", None)
        assert gemini["actual"] == money(22, 22_000_000)

    def test_text(self, home: Path) -> None:
        out = cli(home, "advise").stdout
        collapsed = norm(out)  # the switch line wraps at 100 columns: compare on one line
        for line in ADVISE_LINES:
            assert norm(line) in collapsed, f"missing: {line}\n{out}"
        assert "estimate re-prices the same token counts" in collapsed  # the caveat is printed once

    def test_all_text(self, home: Path) -> None:
        out = cli(home, "advise", "--all").stdout
        collapsed = norm(out)
        for line in [*ADVISE_LINES[:3], *ADVISE_ALL_LINES]:
            assert norm(line) in collapsed, f"missing: {line}\n{out}"

    def test_filters(self, home: Path) -> None:
        only_claude = cli(home, "advise", "--project", "acme-shop", "--json").json()
        assert only_claude["counts"]["sessions"] == 1 and only_claude["sessions"] == []  # heavy: nothing to list
        assert only_claude["total"]["saved"] == money(0, 0) and only_claude["total"]["actual"] == money(265, CLAUDE_UCENTS)
        codex = cli(home, "advise", "--agent", "codex", "--json").json()
        assert codex["total"]["saved"] == money(61, SAVED_UCENTS) and codex["total"]["actual"] == money(76, 76_000_000)
        assert codex["total"]["saved_bp"] == 8079  # 0.614 / 0.76 = 80.79%
        assert cli(home, "advise", "--week-of", "2026-W39", "--json").json()["counts"]["sessions"] == 0

    def test_a_stricter_minimum_ratio_removes_the_advice(self, tmp_path: Path) -> None:
        """5.2055x is advised at the default 1.2 but not when the config asks for 6x: the note then says how cheap it would have been."""
        config = write_config(tmp_path / "config.yaml", router={"min_saving_ratio": 6})
        data = cli(fresh_home(tmp_path), "advise", "--all", "--json", config=config).json()
        codex = next(s for s in data["sessions"] if s["agent"] == "codex")
        assert codex["switch"] is False and codex["note"] == "gpt-6.1-sol is only 5.2x cheaper (minimum 6x)"
        assert data["total"]["saved"] == money(0, 0)


# =====================================================================================================
# report
# =====================================================================================================
class TestReport:
    def test_markdown(self, home: Path) -> None:
        out = cli(home, "report").stdout
        printed = {line.strip() for line in out.splitlines()}
        missing = [line for line in REPORT_LINES if line not in printed]
        assert not missing, f"missing report lines: {missing}\n{out}"

    def test_the_default_format_is_markdown(self, home: Path) -> None:
        assert cli(home, "report", "--format", "md").stdout == cli(home, "report").stdout

    def test_projects_are_hidden_unless_asked(self, home: Path) -> None:
        hidden = cli(home, "report").stdout
        assert all(name not in hidden for name in ("acme-shop", "fake-repo", "demo-notes-app"))  # reports get posted
        shown = cli(home, "report", "--show-projects").stdout
        assert "| `7c8d9e0f` | codex | fake-repo | gpt-6-astra | gpt-6.1-sol | $0.76 | $0.15 | $0.61 (5.2x cheaper) |" in shown

    def test_json_agrees_with_summary_and_advise(self, home: Path, summary: dict[str, Any]) -> None:
        report = cli(home, "report", "--json").json()
        assert (report["week"], report["first_day"], report["last_day"], report["generated"]) == ("2026-W40", "2026-09-28", "2026-10-04", "2026-09-30")
        for key in ("sessions", "projects", "models", "tokens", "total", "excluded_tokens", "warnings"):
            assert report[key] == summary[key], key
        assert report["agents"] == 3
        by_model = [{k: v for k, v in row.items() if k not in ("agent", "provider", "status")} for row in summary["by_model"]]
        assert report["by_model"] == by_model
        assert [row for row in report["by_day"] if row["sessions"]] == summary["by_day"]
        assert len(report["by_day"]) == 7  # the whole week, empty days included
        assert [row for row in report["by_day"] if not row["sessions"]] == [
            {"day": d, "sessions": 0, "tokens": ZERO_TOKENS, "cost": money(0, 0), "share_bp": 0, "unpriced_tokens": 0}
            for d in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04")
        ]
        assert [(r["agent"], r["cost"]) for r in report["by_agent"]] == [("claude", money(265, CLAUDE_UCENTS)), ("codex", money(76, 76_000_000)), ("gemini", money(22, 22_000_000))]
        advise = cli(home, "advise", "--json").json()
        assert report["router"] == advise["total"] | {"week": "2026-W40"}  # the same numbers as `advise`
        assert report["router"]["saved"] == money(61, SAVED_UCENTS) and report["router"]["saved_bp"] == 1691
        assert report["tiers"] == [
            {"tier": "light", "sessions": 2, "cost": money(98, 98_000_000), "unpriced_sessions": 0},  # codex + gemini
            {"tier": "medium", "sessions": 0, "cost": money(0, 0), "unpriced_sessions": 0},
            {"tier": "heavy", "sessions": 1, "cost": money(265, CLAUDE_UCENTS), "unpriced_sessions": 0},
        ]
        assert report["top_sessions"] == [
            {"short_id": "7c8d9e0f", "agent": "codex", "project": "Project B", "model": "gpt-6-astra", "recommended_model": "gpt-6.1-sol", "tier": "light",
             "actual": money(76, 76_000_000), "recommended": money(15, CODEX_ON_SOL_UCENTS), "saved": money(61, SAVED_UCENTS)}
        ]

    def test_html_is_one_static_page(self, home: Path, tmp_path: Path) -> None:
        target = tmp_path / "out" / "weekly.html"
        target.parent.mkdir()
        cli(home, "report", "--format", "html", "--output", str(target))
        page = target.read_text(encoding="utf-8")
        assert page.lstrip().lower().startswith("<!doctype html>") and "<title>AgentFare weekly report 2026-W40</title>" in page
        assert not re.search(r"<script|<link|<img|<iframe|<object|<embed|@import|url\(", page, re.I)  # no script, no external file
        assert not re.search(r"https?://", page)  # and no network reference at all
        text = norm(re.sub(r"<[^>]+>", " ", page))
        for figure in ("$3.63", "$3.02", "$0.61", "16.9%", "$2.59", "$0.76", "$0.22", "$0.06", "$2.65", "$0.98", "$0.15", "5.2x cheaper"):
            assert figure in text, figure
        assert all(name not in page for name in ("acme-shop", "fake-repo", "demo-notes-app", "PRIVACY-CANARY"))
        assert sorted(p.name for p in target.parent.iterdir()) == ["weekly.html"]  # a single file

    def test_html_default_file_name(self, home: Path, tmp_path: Path) -> None:
        cli(home, "report", "--format", "html", cwd=tmp_path)
        assert (tmp_path / "agentfare-report-2026-W40.html").is_file()

    def test_a_week_without_sessions(self, home: Path) -> None:
        out = cli(home, "report", "--week-of", "2026-W39").stdout
        assert "**No sessions were recorded in 2026-W39 (Sep 21 to Sep 27, 2026), so there is nothing to report.**" in out


# =====================================================================================================
# run
# =====================================================================================================
class TestRun:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        """A working directory with two files: the repo-size rule (200+ files) stays silent."""
        path = tmp_path / "repo"
        path.mkdir()
        (path / "a.py").write_text("x = 1\n")
        (path / "b.py").write_text("y = 2\n")
        return path

    @pytest.mark.parametrize(("agent", "prompt", "expected"), RUN_CASES, ids=[f"{a}-{p.split()[0]}-{len(p)}" for a, p, _ in RUN_CASES])
    def test_dry_run_shows_the_pick_and_the_estimate(self, home: Path, repo: Path, agent: str, prompt: str, expected: list[str]) -> None:
        flag = {"claude": "-p", "codex": "exec", "gemini": "-p"}[agent]  # how each agent takes its prompt
        out = cli(home, "run", "--dry-run", "--", agent, flag, prompt, cwd=repo).stdout
        rows = panel(out)
        missing = [row for row in expected if not shown(row, rows)]
        assert not missing, f"missing rows: {missing}\n{out}"
        assert "agentfare run (dry run)" in out
        assert prompt not in out  # only the length is ever shown, never the prompt

    @pytest.mark.parametrize(("agent", "flag", "prompt"), [
        ("claude", "-p", "refactor the billing module and migrate it to the new API"),  # heavy: claude-opus-5-5, the baseline
        ("codex", "exec", "refactor the billing module and migrate it to the new API"),  # heavy: gpt-6-astra, the baseline
    ])
    def test_no_saving_row_when_the_router_runs_the_baseline_itself(self, home: Path, repo: Path, agent: str, flag: str, prompt: str) -> None:
        rows = panel(cli(home, "run", "--dry-run", "--", agent, flag, prompt, cwd=repo).stdout)
        assert any(row.startswith("baseline ~$") for row in rows) and not any(row.startswith("saving") for row in rows)
        data = cli(home, "run", "--dry-run", "--json", "--", agent, flag, prompt, cwd=repo).json()
        assert (data["baseline_basis"], data["baseline_since"]) == ("recent", "2026-08-31")

    def test_the_pick_is_shown_before_anything_runs_and_nothing_runs_without_a_yes(self, home: Path, repo: Path, tmp_path: Path) -> None:
        agent_log = tmp_path / "agent.log"
        fake = tmp_path / "bin"
        fake.mkdir()
        (fake / "claude").write_text(f'#!/bin/sh\necho "FAKE-AGENT-RAN"\nprintf "%s\\n" "$@" > "{agent_log}"\nexit 7\n')
        (fake / "claude").chmod(0o755)
        env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}
        prompt = "fix the typo in the README"
        # stdin is not a terminal and there is no --yes: the decision is printed, the agent is NOT started, exit code 3
        refused = cli(home, "run", "--", "claude", "-p", prompt, cwd=repo, env=env, ok=False)
        assert refused.returncode == 3
        assert "estimate ~$0.3125 on claude-sonnet-5-5" in norm(refused.stderr) and "model claude-sonnet-5-5 (router pick)" in norm(refused.stderr)
        assert not agent_log.exists() and "FAKE-AGENT-RAN" not in refused.stdout
        # --dry-run never starts it either
        cli(home, "run", "--dry-run", "--", "claude", "-p", prompt, cwd=repo, env=env)
        assert not agent_log.exists()
        # with --yes the pick is printed (stderr) and then the agent runs with the routed model; its output and exit code pass through
        ran = cli(home, "run", "--yes", "--", "claude", "-p", prompt, cwd=repo, env=env, ok=False)
        assert ran.returncode == 7 and ran.stdout.strip() == "FAKE-AGENT-RAN"
        assert "estimate ~$0.3125 on claude-sonnet-5-5" in norm(ran.stderr)
        assert agent_log.read_text().split("\n")[:-1] == ["--model", "claude-sonnet-5-5", "-p", prompt]  # the real prompt reaches the agent untouched

    def test_the_decision_is_on_screen_before_the_agent_starts(self, home: Path, repo: Path, tmp_path: Path) -> None:
        """With --yes the pick, its reasons, the estimate and the command are all printed before the agent's first byte: stdout and
        stderr share one pipe here (as on a terminal), so the order of the text is the order in time."""
        fake = tmp_path / "bin"
        fake.mkdir()
        (fake / "claude").write_text('#!/bin/sh\necho "FAKE-AGENT-RAN"\n')
        (fake / "claude").chmod(0o755)
        shown = cli(home, "run", "--yes", "--", "claude", "-p", "fix the typo in the README", cwd=repo, env={"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}, merged=True).stdout
        assert shown.count("FAKE-AGENT-RAN") == 1
        ran_at = shown.index("FAKE-AGENT-RAN")
        for decision in (r"tier\s+light", r"model\s+claude-sonnet-5-5\s+\(router pick\)", r"estimate\s+~\$0\.3125", r"baseline\s+~\$0\.5050", r"command\s+claude --model claude-sonnet-5-5"):
            found = re.search(decision, shown)
            assert found and found.start() < ran_at, f"{decision!r} is not printed before the agent ran:\n{shown}"

    def test_force_model(self, home: Path, repo: Path) -> None:
        # the same light task forced onto gpt-6.1-sol: 30,000 x 2 + 600,000 x 0.10 + 25,000 x 2.50 + 7,000 x 10 = $0.2525; baseline stays opus $0.5050
        out = cli(home, "run", "--dry-run", "--force-model", "gpt-6.1-sol", "--", "claude", "-p", "fix the typo in the README", cwd=repo).stdout
        rows = panel(out)
        assert "tier light (score 0)" in rows and "model gpt-6.1-sol (--force-model)" in rows
        assert "estimate ~$0.2525 on gpt-6.1-sol, typical light task (no history yet)" in rows
        assert "saving ~$0.2525 (50.0% less than the baseline)" in rows
        assert "command claude --model gpt-6.1-sol -p '<prompt: 26 chars>'" in rows

    def test_the_fixtures_alone_are_too_little_history_for_a_median(self, home: Path, repo: Path) -> None:
        """One session per agent, below the 5 needed: every estimate above says 'no history yet'."""
        out = cli(home, "run", "--dry-run", "--", "claude", "-p", "fix the typo in the README", cwd=repo).stdout
        assert "no history yet" in out and "median of" not in out

    @staticmethod
    def codex_history(tmp_path: Path, copies: int) -> Path:
        """A config whose only log source is ``copies`` renamed copies of the Codex fixture: identical sessions with a new thread id each
        (light tier, 1 prompt, 40,000 / 60,000 / 0 / 6,000 tokens on gpt-6-astra, $0.76 each)."""
        (source,) = (FIXTURES / "codex" / "sessions").rglob("rollout-*.jsonl")
        original = "01a0d8b0-1a2b-7c3d-8e4f-5a6b7c8d9e0f"
        folder = tmp_path / "codex" / "sessions" / "2026" / "09" / "29"
        folder.mkdir(parents=True)
        for n in range(copies):
            thread = original[:-1] + str(n)
            (folder / f"rollout-2026-09-29T10-00-00-{thread}.jsonl").write_text(source.read_text(encoding="utf-8").replace(original, thread), encoding="utf-8")
        return write_config(tmp_path / "config.yaml", sources={"claude": [], "gemini": [], "codex": [str(tmp_path / "codex" / "sessions")]})

    @pytest.mark.parametrize("copies", [4, 5])
    def test_the_estimate_is_the_median_of_past_sessions_from_five_on(self, tmp_path: Path, repo: Path, copies: int) -> None:
        config = self.codex_history(tmp_path, copies)
        home = fresh_home(tmp_path)
        assert cli(home, "summary", "--json", config=config).json()["sessions"] == copies
        rows = panel(cli(home, "run", "--dry-run", "--", "codex", "exec", "fix the typo in the README", cwd=repo, config=config).stdout)
        if copies < 5:  # not enough history: the default token budget of the light tier, $0.2525 on gpt-6.1-sol (see RUN_CASES)
            assert "estimate ~$0.2525 on gpt-6.1-sol, typical light task (no history yet)" in rows
            return
        # The median of five identical sessions is that session: 40,000 x 2 + 60,000 x 0.10 + 6,000 x 10 = 80,000 + 6,000 + 60,000 = $0.146 on
        # gpt-6.1-sol, and $0.76 on gpt-6-astra (the baseline): 40,000 x 10 + 60,000 x 1 + 6,000 x 50. Saving $0.614 = 80.8% of $0.76.
        assert "tier light (score 0)" in rows and "model gpt-6.1-sol (router pick)" in rows
        assert "estimate ~$0.1460 on gpt-6.1-sol, median of 5 past codex/light sessions" in rows
        assert "baseline ~$0.76 on gpt-6-astra (your most used codex model in the last 30 days, since 2026-08-30)" in " ".join(rows)  # 30 days before the newest session, 2026-09-29
        assert "saving ~$0.6140 (80.8% less than the baseline)" in rows
        # the same five sessions through `advise`: 5 x $0.76 = $3.80 actual, 5 x $0.146 = $0.73 with the router, $3.07 saved (80.79%)
        total = cli(home, "advise", "--json", config=config).json()["total"]
        assert (total["sessions"], total["switched"], total["actual"]["ucents"], total["recommended"]["ucents"], total["saved"]["ucents"], total["saved_bp"]) == (
            5, 5, 380_000_000, 73_000_000, 307_000_000, 8079,
        )


# =====================================================================================================
# a machine that has never run AgentFare and has no agent logs
# =====================================================================================================
class TestFreshMachine:
    """No --config, no AGENTFARE_HOME: the real default locations, under an empty HOME."""

    @staticmethod
    def bare(home: Path, *args: str) -> Result:
        return cli(home, *args, config=None, env={"AGENTFARE_HOME": None})

    def test_help_and_version_write_nothing(self, tmp_path: Path) -> None:
        self.bare(tmp_path, "--help")
        self.bare(tmp_path, "--version")
        assert list((tmp_path / "userhome").iterdir()) == []  # not even ~/.agentfare

    def test_every_command_says_what_to_do_when_there_is_nothing_to_read(self, tmp_path: Path) -> None:
        summary = self.bare(tmp_path, "summary")
        assert "no sessions in this period (all time)" in summary.stdout
        assert "the database has no sessions at all: run agentfare ingest" in norm(summary.stdout)
        assert self.bare(tmp_path, "summary", "--json").json()["total"] == money(0, 0)
        assert self.bare(tmp_path, "advise", "--json").json()["counts"]["sessions"] == 0
        assert "there is nothing to report" in self.bare(tmp_path, "report").stdout
        for command in ("sessions", "models", "advise"):
            assert "no sessions in this period" in self.bare(tmp_path, command).stdout, command
        assert sorted(p.name for p in (tmp_path / "userhome").iterdir()) == [".agentfare"]  # the only thing created
        assert sorted(p.name for p in (tmp_path / "userhome" / ".agentfare").iterdir()) == ["agentfare.db"]

    def test_config_init_writes_the_documented_defaults_and_will_not_overwrite(self, tmp_path: Path) -> None:
        defaults = self.bare(tmp_path, "config", "--default").stdout
        written = self.bare(tmp_path, "config", "--init")
        target = tmp_path / "userhome" / ".agentfare" / "config.yaml"
        assert "wrote" in written.stdout and target.read_text(encoding="utf-8") == defaults
        again = cli(tmp_path, "config", "--init", config=None, env={"AGENTFARE_HOME": None}, ok=False)
        assert again.returncode == 2 and "already exists (use --force to overwrite)" in norm(again.stderr)
        self.bare(tmp_path, "config", "--init", "--force")


# =====================================================================================================
# the price guard
# =====================================================================================================
def builtin_pricing_path() -> Path:
    spec = importlib.util.find_spec("agentfare")
    assert spec and spec.submodule_search_locations
    return Path(next(iter(spec.submodule_search_locations))) / "data" / "pricing.yaml"


def effective_rates(entry: dict[str, Any]) -> tuple[Decimal, ...]:
    """(input, cache read, cache write 5m, cache write 1h, output) as billed: a missing cache_write is billed at the input
    price, a missing cache_write_1h at the cache_write price."""
    inp = Decimal(str(entry["input"]))
    write = Decimal(str(entry["cache_write"])) if entry.get("cache_write") is not None else inp
    write_1h = Decimal(str(entry["cache_write_1h"])) if entry.get("cache_write_1h") is not None else write
    return inp, Decimal(str(entry["cached_input"])), write, write_1h, Decimal(str(entry["output"]))


def generated_block(name: str) -> str:
    text = EXPECTED_MD.read_text(encoding="utf-8")
    match = re.search(rf"<!-- BEGIN generated: {name} -->\n(.*?)\n<!-- END generated: {name} -->", text, re.S)
    assert match, f"EXPECTED.md has no generated block {name!r}"
    return match.group(1)


def prices_in_expected_md() -> dict[str, tuple[Decimal, ...]]:
    rows: dict[str, tuple[Decimal, ...]] = {}
    for line in generated_block("prices").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0].startswith("`"):
            rows[cells[0].strip("`")] = tuple(Decimal(c.lstrip("$")) for c in cells[1:6])
    return rows


class TestPriceGuard:
    """A price change must force a new hand verification: these fail when pricing.yaml no longer says what EXPECTED.md and the
    literals in this file were computed with. Then redo the arithmetic (scripts/verify_fixtures.py --markdown prints the new
    tables), update EXPECTED.md and the literals at the top of this file, and only then let the guard pass again."""

    def test_the_builtin_prices_are_the_ones_recorded_in_expected_md(self) -> None:
        doc = yaml.safe_load(builtin_pricing_path().read_text(encoding="utf-8"))
        recorded = prices_in_expected_md()
        assert set(recorded) == set(PRICES), "EXPECTED.md and this file list different models"
        for model, rates in recorded.items():
            entry = doc["models"][model]
            assert entry["input"] != "unverified" and entry["output"] != "unverified", f"{model} lost its verified price"
            assert effective_rates(entry) == rates, f"pricing.yaml changed the price of {model}: re-verify tests/fixtures/EXPECTED.md"

    def test_the_prices_in_expected_md_are_the_literals_of_this_file(self) -> None:
        assert prices_in_expected_md() == {m: tuple(Decimal(p) for p in rates) for m, rates in PRICES.items()}

    def test_the_price_table_the_cli_shows_is_the_same(self, home: Path) -> None:
        shown = {row["model"]: row for row in cli(home, "prices", "--json").json()["models"]}
        for model, rates in PRICES.items():
            row = shown[model]
            assert row["status"] == "verified", model
            per_m = {k: (Decimal(v["usd"]) if v else None) for k, v in row["prices_per_mtok"].items()}
            inp = per_m["input"]
            write = per_m["cache_write"] if per_m["cache_write"] is not None else inp
            write_1h = per_m["cache_write_1h"] if per_m["cache_write_1h"] is not None else write
            assert (inp, per_m["cached_input"], write, write_1h, per_m["output"]) == tuple(Decimal(p) for p in rates), model

    def test_the_price_dates_are_recorded(self) -> None:
        doc = yaml.safe_load(builtin_pricing_path().read_text(encoding="utf-8"))
        block = generated_block("prices")
        for model in PRICES:
            checked = doc["models"][model]["checked"]
            assert isinstance(checked, date) and checked >= date(2026, 9, 30), model  # never older than the verification the document describes
        assert "2026-09-30" in block

    def test_the_root_pricing_yaml_is_the_builtin_table(self) -> None:
        assert (ROOT / "pricing.yaml").read_bytes() == builtin_pricing_path().read_bytes()


# =====================================================================================================
# privacy
# =====================================================================================================
CANARY_FILES = {
    "PRIVACY-CANARY-CLAUDE": "claude",
    "PRIVACY-CANARY-CODEX": "codex",
    "PRIVACY-CANARY-GEMINI": "gemini",
}


def fixture_text_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file() and p.suffix in (".jsonl", ".json", ".txt") or p.name == ".project_root")


def paths_inside_the_logs() -> set[str]:
    """Every working-directory path, file path and repository URL the fixture logs mention (none of them may reach the database)."""
    found: set[str] = set()
    for file in fixture_text_files():
        text = file.read_text(encoding="utf-8", errors="replace")
        found.update(m.group(0).rstrip(".,:;") for m in re.finditer(r"/(?:home|elsewhere|skills)/[A-Za-z0-9_./-]+", text))
        found.update(re.findall(r"github\.com/[A-Za-z0-9_./-]+", text))
    return {p for p in found if p not in str(ROOT)}  # (a checkout under /home/dev/... must not make its own path a "leak")


class TestPrivacy:
    def test_the_fixtures_really_plant_the_canaries(self) -> None:
        """Without this the scan below could pass because a marker was renamed."""
        for marker, agent in CANARY_FILES.items():
            hits = [f for f in fixture_text_files() if marker.encode() in f.read_bytes()]
            assert hits and all(f"/{agent}/" in f.as_posix() for f in hits), marker
        assert len(paths_inside_the_logs()) >= 12  # cwd, file paths and the repository URL of all three fixtures
        assert "/home/dev/acme-shop/src/cart.py" in paths_inside_the_logs() and "/home/tester/src/fake-repo-checkout" in paths_inside_the_logs()

    def test_no_canary_and_no_working_path_reaches_the_database(self, home: Path) -> None:
        """The whole data directory (SQLite file, journal, anything else) is scanned as raw bytes: the file contents of a
        deleted or overwritten row would still be visible there."""
        files = [p for p in home.rglob("*") if p.is_file() and "userhome" not in p.parts]
        assert any(p.name == "agentfare.db" for p in files)
        blob = b"".join(p.read_bytes() for p in files)
        assert b"PRIVACY-CANARY" not in blob
        leaked = [p for p in sorted(paths_inside_the_logs()) if p.encode() in blob]
        assert leaked == []
        # positive controls: the scan reads the right bytes, and the metadata that is meant to be kept is there
        for kept in (b"acme-shop", b"fake-repo", b"demo-notes-app", b"claude-opus-5-5", b"gpt-6-astra", b"gemini-2.5-pro", b"9f3c2a71-5b8e-4d0a-9c1e-2b7d4a6e8f10"):
            assert kept in blob, kept
        # the only path the database keeps is the one of the log file it came from (an audit trail), never one from inside a log
        assert b"tests/fixtures/codex/sessions/2026/09/29/rollout-2026-09-29T10-00-00-01a0d8b0" in blob

    def test_no_command_prints_a_canary_or_a_working_path(self, home: Path, tmp_path: Path) -> None:
        commands = [
            ("summary",), ("summary", "--json"), ("sessions",), ("sessions", "--json"), ("models",), ("models", "--json"), ("advise", "--all"),
            ("advise", "--all", "--json"), ("report",), ("report", "--json"), ("report", "--show-projects"), ("config",), ("prices",), ("ingest",),
        ]
        inside = paths_inside_the_logs()
        for args in commands:
            result = cli(home, *args)
            for stream in (result.stdout, result.stderr):
                assert "PRIVACY-CANARY" not in stream, args
                assert not [p for p in inside if p in stream], args
        target = tmp_path / "r.html"
        cli(home, "report", "--format", "html", "-o", str(target))
        assert "PRIVACY-CANARY" not in target.read_text(encoding="utf-8")

    def test_a_marker_in_a_prompt_never_shows_up_in_run_output(self, home: Path, tmp_path: Path) -> None:
        secret = "PRIVACY-CANARY-RUN-e2e refactor the billing module"
        result = cli(home, "run", "--dry-run", "--", "claude", "-p", secret, cwd=tmp_path)
        assert "PRIVACY-CANARY" not in result.stdout + result.stderr  # the prompt is scored, and only its length is shown
        assert f"prompt {len(secret)} chars, from the command line" in panel(result.stdout)


# =====================================================================================================
# no network
# =====================================================================================================
GUARD = r"""
import os, socket, sys

log = os.environ["E2E_NETWORK_LOG"]

def blocked(name):
    def guard(*args, **kwargs):
        with open(log, "a") as fh:
            fh.write(name + "\n")
        raise OSError("network blocked by test_e2e_fixtures: " + name)
    return guard

for method in ("connect", "connect_ex", "sendto", "sendmsg"):
    setattr(socket.socket, method, blocked("socket." + method))
for function in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
    setattr(socket, function, blocked("socket." + function))
"""
GUARDED_CLI = GUARD + "\nfrom agentfare.cli import _entry\nsys.argv[0] = 'agentfare'\n_entry()\n"


def guarded(home: Path, log: Path, *args: str, ok: bool = True) -> Result:
    return cli(home, *args, launch=[sys.executable, "-c", GUARDED_CLI], env={"E2E_NETWORK_LOG": str(log)}, ok=ok)


class TestNoNetwork:
    def test_the_guard_blocks_every_way_out(self, tmp_path: Path) -> None:
        """A guard that blocks nothing would make the test below meaningless: try each door with the same code."""
        log = tmp_path / "attempts.log"
        script = GUARD + r"""
import socket
for reach in (
    lambda: socket.socket().connect(("127.0.0.1", 9)),
    lambda: socket.socket().connect_ex(("127.0.0.1", 9)),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9)),
    lambda: socket.create_connection(("127.0.0.1", 9)),
    lambda: socket.getaddrinfo("example.com", 80),
    lambda: socket.gethostbyname("example.com"),
):
    try:
        reach()
    except OSError as exc:
        assert "network blocked" in str(exc)
    else:
        raise SystemExit("a network call went through")
"""
        done = subprocess.run([sys.executable, "-c", script], env={**os.environ, "E2E_NETWORK_LOG": str(log)}, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert log.read_text().split() == ["socket.connect", "socket.connect_ex", "socket.sendto", "socket.create_connection", "socket.getaddrinfo", "socket.gethostbyname"]

    def test_every_command_but_prices_refresh_runs_with_the_network_cut(self, tmp_path: Path) -> None:
        log = tmp_path / "attempts.log"
        log.write_text("")
        home = tmp_path / "home"
        commands = [
            ("ingest",), ("ingest", "--full"), ("summary",), ("summary", "--json"), ("summary", "--week"), ("sessions",), ("models",), ("advise",), ("advise", "--all", "--json"),
            ("report",), ("report", "--json"), ("report", "--format", "html", "-o", str(tmp_path / "r.html")), ("config",), ("prices",), ("prices", "--json"),
            ("run", "--dry-run", "--", "claude", "-p", "refactor the billing module"),
        ]
        for args in commands:
            result = guarded(home, log, *args)
            assert result.returncode == 0, args
        assert guarded(home, log, "summary", "--json").json()["total"] == money(363, TOTAL_UCENTS)  # and the numbers are the real ones
        assert log.read_text() == "", f"network attempts: {log.read_text()}"

    def test_prices_refresh_is_the_one_command_that_reaches_for_the_network(self, tmp_path: Path) -> None:
        """With the network cut, `prices --refresh` trips the guard: the one place where AgentFare would go online. Its target here
        is a local address that is never contacted, because the guard raises before any packet leaves the machine."""
        log = tmp_path / "attempts.log"
        log.write_text("")
        home = tmp_path / "home"
        home.mkdir()
        (home / "pricing.yaml").write_text("models:\n  m-1:\n    input: 1\n    cached_input: 0.1\n    output: 2\n    source: http://127.0.0.1:9/prices\n")
        guarded(home, log, "prices", "--refresh", "--allow-private-hosts", "--json", ok=False)
        assert log.read_text().strip() != "", "prices --refresh did not try the network, or the guard is broken"


# =====================================================================================================
# the documents and the independent derivation
# =====================================================================================================
def load_verifier() -> Any:
    spec = importlib.util.spec_from_file_location("verify_fixtures", ROOT / "scripts" / "verify_fixtures.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no scripts/__pycache__ left in the checkout
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = keep
    return module


def machine_readable() -> dict[str, str]:
    text = EXPECTED_MD.read_text(encoding="utf-8")
    match = re.search(r"<!-- BEGIN machine-readable -->\n(.*?)\n<!-- END machine-readable -->", text, re.S)
    assert match, "EXPECTED.md has no machine-readable block"
    return {k: v for k, v in re.findall(r"^([a-z_.]+) = (.*)$", match.group(1), re.M)}


class TestExpectedDocument:
    def test_the_machine_readable_block_is_what_the_tests_assert(self) -> None:
        assert machine_readable() == {
            "summary.sessions": "3",
            "summary.total_ucents": str(TOTAL_UCENTS),
            "summary.total_cents": "363",
            "summary.total_usd": "$3.63",
            "advise.switched": "1",
            "advise.saved_ucents": str(SAVED_UCENTS),
            "advise.saved_usd": "$0.6140",
            "advise.switch_line": SWITCH_LINE,
        }

    def test_every_line_the_tests_look_for_is_written_in_expected_md(self) -> None:
        doc = norm(EXPECTED_MD.read_text(encoding="utf-8"))
        expected = [*SUMMARY_LINES, *SESSIONS_LINES, *MODELS_LINES, *ADVISE_LINES, *ADVISE_ALL_LINES, *REPORT_LINES, *(row for *_, rows in RUN_CASES for row in rows)]
        undocumented = [line for line in expected if norm(line) not in doc]
        assert not undocumented, "not in EXPECTED.md:\n  " + "\n  ".join(undocumented)

    def test_the_generated_tables_are_current(self) -> None:
        """EXPECTED.md's derivation tables are the output of scripts/verify_fixtures.py: a fixture or price change that is not
        re-verified (and re-generated with --markdown) fails here."""
        verifier = load_verifier()
        problems = verifier.check_doc(EXPECTED_MD, verifier.markdown_blocks(verifier.derive()))
        assert problems == []

    def test_the_verifier_runs_as_a_script_without_agentfare(self) -> None:
        """Run it with `import agentfare` made impossible: it must not need the product it verifies."""
        script = ROOT / "scripts" / "verify_fixtures.py"
        code = f"import runpy, sys; sys.modules['agentfare'] = None; sys.argv = [{str(script)!r}, '--check']; runpy.run_path({str(script)!r}, run_name='__main__')"
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "every generated block matches the fixtures" in done.stdout
        assert not re.search(r"^\s*(?:from|import)\s+agentfare", script.read_text(encoding="utf-8"), re.M)


@pytest.fixture(scope="module")
def derived() -> dict[str, Any]:
    return load_verifier().derive()


class TestIndependentDerivation:
    """The independent script's numbers (own log readers, exact Fractions) agree with the literals above and with the CLI."""

    def test_it_agrees_with_the_hand_computed_literals(self, derived: dict[str, Any]) -> None:
        tokens = {(s["agent"], m): tuple(t[b] for b in PRICE_BUCKETS) for s in derived["sessions"] for m, t in s["tokens"].items()}
        assert tokens == TOKENS
        assert {m: v["ucents"] for m, v in derived["totals"]["model"].items()} == UCENTS
        assert derived["totals"]["ucents"] == TOTAL_UCENTS
        assert derived["savings"] == {"2026-W40": {"sessions": 3, "switched": 1, "actual_ucents": TOTAL_UCENTS, "router_ucents": ROUTER_UCENTS}}
        assert {s["agent"]: s["router"]["recommended_ucents"] for s in derived["sessions"]} == {"claude": None, "codex": CODEX_ON_SOL_UCENTS, "gemini": GEMINI_ON_SOL_UCENTS}

    def test_it_agrees_with_the_cli_session_by_session(self, derived: dict[str, Any], home: Path, advise_all: dict[str, Any]) -> None:
        cli_sessions = {s["agent"]: s for s in cli(home, "sessions", "--json").json()["sessions"]}
        advice = {s["agent"]: s for s in advise_all["sessions"]}
        for mine in derived["sessions"]:
            theirs, verdict = cli_sessions[mine["agent"]], advice[mine["agent"]]
            for key in ("session_id", "short_id", "project", "started_at", "active_seconds", "wall_seconds", "prompts", "tool_calls", "files_touched", "error_retries", "turns"):
                assert theirs[key] == mine[key], (mine["agent"], key)
            assert theirs["cost"]["ucents"] == mine["ucents"], mine["agent"]
            assert {m["model"]: m["cost"]["ucents"] for m in theirs["models"]} == mine["ucents_by_model"], mine["agent"]
            router = mine["router"]
            assert (verdict["score"], verdict["tier"], verdict["points"]) == (router["score"], router["tier"], router["points"]), mine["agent"]
            assert verdict["switch"] is router["advised"], mine["agent"]
            if router["advised"]:
                assert verdict["saved"]["ucents"] == router["saved_ucents"]

    def test_the_router_defaults_are_the_ones_the_hand_computation_used(self, home: Path) -> None:
        """If a default threshold changes, the scores derived by hand are stale: this fails first."""
        everything = yaml.safe_load(cli(home, "config", "--default").stdout)
        defaults = everything["router"]
        verifier = load_verifier()
        assert everything["idle_gap_minutes"] * 60 == verifier.IDLE_GAP_SECONDS  # the active times (5,930 s, 600 s, 595 s) assume 30 minutes
        assert {name: (limits["medium"], limits["heavy"]) for name, limits in defaults["signals"].items()} == verifier.SIGNAL_LIMITS
        assert (defaults["tiers"]["light_max"], defaults["tiers"]["medium_max"]) == verifier.TIER_CUTS
        assert defaults["recommend"] == verifier.RECOMMEND
        assert Decimal(str(defaults["min_saving_ratio"])) == Decimal("1.2")
        assert defaults["run"]["models"] == verifier.RUN_MODELS
        assert defaults["run"]["default_tokens"] == verifier.RUN_DEFAULT_TOKENS
        assert defaults["run"]["tiers"] == {"light_max": 0, "medium_max": 2}  # one reason is enough for medium, three points make heavy
        assert defaults["run"]["prompt_chars"] == {"medium": 400, "heavy": 1500} and defaults["run"]["keyword_points"] == 2


# =====================================================================================================
# scripts/verify_fresh_clone.sh (its clone-and-pip path needs a package index and git; the rest is checked here, offline)
# =====================================================================================================
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestFreshCloneScript:
    SCRIPT = ROOT / "scripts" / "verify_fresh_clone.sh"

    def run_script(self, tmp_path: Path, *args: str, pythons: str = "no-such-python-3.99") -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "VERIFY_PYTHONS": pythons, "VERIFY_TMPDIR": str(tmp_path)}
        return subprocess.run(["bash", str(self.SCRIPT), *args], capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path, stdin=subprocess.DEVNULL)

    def test_help_and_bad_usage(self, tmp_path: Path) -> None:
        helped = self.run_script(tmp_path, "--help")
        assert helped.returncode == 0 and "--worktree" in helped.stdout and "VERIFY_PIP_ARGS" in helped.stdout
        assert self.run_script(tmp_path, "--no-such-option").returncode == 2
        assert self.run_script(tmp_path, "one", "two").returncode == 2
        assert self.run_script(tmp_path, "--worktree", str(tmp_path / "missing")).returncode == 2

    def test_it_reports_fail_and_exits_non_zero_when_a_check_fails(self, tmp_path: Path) -> None:
        """No interpreter to test with is a failed check: the table says FAIL, the last line says FAIL and the status is 1. (The same
        script, on a tree whose EXPECTED.md, dependencies, version, CLI or tests were broken on purpose, ended with FAIL and status 1 in
        each case when it was reviewed.)"""
        done = self.run_script(tmp_path, "--worktree", str(ROOT))
        assert done.returncode == 1, done.stdout + done.stderr
        assert re.search(r"^FAIL\s+interpreters\b", done.stdout, re.M) and "RESULT: FAIL (1 of 3 checks failed)" in done.stdout
        # the parts that need no pip: the working tree was copied, and the expected numbers were read back out of EXPECTED.md
        assert re.search(r"^PASS\s+copy\s+working tree\b", done.stdout, re.M)
        assert f"total $3.63 ({TOTAL_UCENTS} ucents), saved $0.6140" in done.stdout
        assert not list(tmp_path.glob("afc.*")), "the temporary directory was not removed"

    def test_a_missing_expected_md_is_a_failure(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        shutil.copytree(FIXTURES, tree / "tests" / "fixtures", ignore=shutil.ignore_patterns("__pycache__"))
        (tree / "pyproject.toml").write_text('[project]\nversion = "0.0.0"\n', encoding="utf-8")
        (tree / "tests" / "fixtures" / "EXPECTED.md").unlink()
        done = self.run_script(tmp_path, "--worktree", str(tree))
        assert done.returncode == 1 and re.search(r"^FAIL\s+expectations\b", done.stdout, re.M), done.stdout


class TestContinuousIntegration:
    """.github/workflows/ci.yml runs on GitHub, where nobody is watching: check what can be checked here."""

    def test_the_workflow_installs_like_a_user_and_covers_the_supported_pythons(self) -> None:
        doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        triggers = doc.get(True, doc.get("on"))  # PyYAML reads the key `on` as the boolean True
        assert {"push", "pull_request"} <= set(triggers)
        job = doc["jobs"]["test"]
        versions = job["strategy"]["matrix"]["python-version"]
        assert all(isinstance(v, str) for v in versions), "quote the versions: YAML reads 3.10 as the number 3.1"
        floor = re.search(r'^requires-python\s*=\s*">=(\d+\.\d+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
        assert floor and floor.group(1) in versions, "the oldest Python the package supports must be in the matrix"
        assert {"ubuntu-latest", "macos-latest"} <= set(job["strategy"]["matrix"]["os"])  # macOS and Linux are the targets
        steps = job["steps"]
        assert [s["uses"].split("@")[0] for s in steps if "uses" in s] == ["actions/checkout", "actions/setup-python"]
        assert all(re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", s["uses"]) for s in steps if "uses" in s), "pin actions to a full commit SHA (a tag can be moved)"
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert re.findall(r"uses: actions/[\w-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", text, re.M), "say which release each SHA is, in a comment"
        assert steps[1]["with"]["python-version"] == "${{ matrix.python-version }}"
        script = "\n".join(s.get("run", "") for s in steps)
        assert script.index("pip install -e '.[dev]'") < script.index("agentfare --help") < script.index("pytest")


# =====================================================================================================
# the definition of done
# =====================================================================================================
USAGE = re.compile(r"Usage: (?:python\S* -m )?agentfare")  # the entry point says `agentfare`, `python -m agentfare` says so


class TestDefinitionOfDone:
    """What the owner asked for, as three tests. (A fresh clone + ``pip install -e .`` giving a working ``agentfare --help`` is
    what ``scripts/verify_fresh_clone.sh`` proves, on every interpreter it finds; the third test below is its heart.)"""

    def test_summary_matches_pricing_yaml_to_the_cent(self, tmp_path: Path) -> None:
        """``agentfare summary`` on the sample fixtures prints dollar math that matches pricing.yaml to the cent.

        The expected side is computed here from the raw pricing.yaml and the hand-counted tokens (TOKENS), with Decimal: no
        AgentFare code is involved. Whatever the file says today, the CLI must agree with it exactly."""
        prices = yaml.safe_load(builtin_pricing_path().read_text(encoding="utf-8"))["models"]
        by_model: dict[str, Decimal] = {}
        for (_, model), tokens in TOKENS.items():
            rates = effective_rates(prices[model])
            dollars = sum(Decimal(n) * r for n, r in zip(tokens, rates, strict=True)) / 1_000_000
            by_model[model] = dollars
        total_dollars = sum(by_model.values())
        total_cents = int((total_dollars * 100).quantize(Decimal(1), rounding="ROUND_HALF_UP"))

        out = cli(fresh_home(tmp_path), "summary")
        data = cli(fresh_home(tmp_path), "summary", "--json").json()
        assert data["total"]["ucents"] == int(total_dollars * 100_000_000)  # exact, to the micro-cent
        assert data["total"]["cents"] == total_cents
        assert f"total spend ${total_cents // 100}.{total_cents % 100:02d}" in out.stdout
        for row in data["by_model"]:
            assert row["cost"]["ucents"] == int(by_model[row["model"]] * 100_000_000), row["model"]
        # (With the prices of 2026-09-30 this is $3.63 = 363,100,000 ucents: TestSummary pins that, TestPriceGuard flags a price change.)

    def test_advise_produces_a_savings_number(self, tmp_path: Path) -> None:
        """``agentfare advise`` prints a savings number: what a switch would have saved, and the weekly router savings."""
        text = norm(cli(fresh_home(tmp_path), "advise").stdout)
        assert "you spent $0.76 on gpt-6-astra → gpt-6.1-sol would likely have handled this for ~$0.1460 (5.2x cheaper)" in text
        assert "2026-W40 3 1 $3.6310 $3.0170 $0.6140 16.9%" in text  # weekly router savings: $0.6140, 16.9% of the spend
        data = cli(fresh_home(tmp_path), "advise", "--json").json()
        assert data["total"]["saved"] == money(61, SAVED_UCENTS)
        assert data["total"]["saved"]["ucents"] > 0

    def test_help_works(self, tmp_path: Path) -> None:
        """``agentfare --help`` works through the installed entry point and ``python -m agentfare``, and so does every command's --help."""
        assert ENTRY is not None, "no `agentfare` console script next to this interpreter: was the package installed with pip install -e .?"
        commands = ["ingest", "config", "summary", "sessions", "models", "advise", "run", "report", "prices"]
        for launch in ([str(ENTRY)], [sys.executable, "-m", "agentfare"]):
            top = cli(fresh_home(tmp_path), "--help", config=None, launch=launch)
            assert USAGE.search(top.stdout) and all(name in top.stdout for name in commands), launch
            for name in commands:
                sub = cli(fresh_home(tmp_path), name, "--help", config=None, launch=launch)
                assert USAGE.search(sub.stdout) and "--help" in sub.stdout, (launch, name)
            version = cli(fresh_home(tmp_path), "--version", config=None, launch=launch)
            assert re.fullmatch(r"agentfare \d+\.\d+\.\d+\s*", version.stdout), version.stdout
        no_args = cli(fresh_home(tmp_path), config=None, ok=False)  # `agentfare` alone shows the help too
        assert USAGE.search(no_args.stdout + no_args.stderr)

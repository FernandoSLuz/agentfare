#!/usr/bin/env python3
"""Generates the synthetic history that `agentfare demo` runs on: src/agentfare/demo_data/.

    python scripts/make_demo_data.py            # rewrite the committed files
    python scripts/make_demo_data.py --check    # exit 1 when the committed files differ from a fresh generation

Everything here is invented. The output is deterministic: a fixed seed, no clock, nothing read from the machine and no
floating point beyond plain arithmetic, so every Python on every platform writes the same bytes (`tests/test_demo_data.py`
compares the committed files with a fresh generation). Nothing is read from ~/.claude, ~/.codex or ~/.gemini: the
projects, paths, ids and text below are made up, and every text field of every log carries the marker PRIVACY-CANARY-DEMO,
which the tests look for in the database (it must never be there).

The history is 45 sessions in the real on-disk formats of the three agents (docs/LOG-FORMATS.md, tests/fixtures):

    claude  18 sessions   claude/projects/<encoded cwd>/<session>.jsonl, sub-agent transcripts below <session>/subagents/
    codex   18 sessions   codex/sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl
    gemini   9 sessions   gemini/tmp/<slug>/chats/session-<minute>-<id>.jsonl, and .project_root next to chats/

over 2026-09-21 to 2026-09-30 (UTC): ISO week 2026-W39, complete, and 2026-W40, in progress at the frozen clock of the demo
(AGENTFARE_NOW=2026-09-30T18:00:00Z). The projects are storefront-api, notes-app, infra-terraform, data-pipeline and
docs-site. The story the numbers tell:

* Claude Code: seven long Opus sessions (42 to 54 tool calls, 7 to 11 files, retries) that the router keeps, one of them
  written mostly with 1-hour cache writes, one with two Sonnet sub-agents, one that runs across midnight and one still
  going at 17:52; one medium and one light Opus session that a cheaper model would have handled; nine short Sonnet
  sessions, which are already cheap.
* Codex CLI: light and medium sessions on the flagship gpt-6-astra, which `advise` moves to gpt-6.1-sol for about a fifth
  of the price; one heavy astra session, which it keeps; sessions already on gpt-6.1-sol and gpt-6-sol; two sub-agent threads
  (0 prompts); and two auto-review threads on codex-auto-review, a model with no published price, which is left out of
  every dollar total and named in a warning.
* Gemini CLI: gemini-2.5-pro and gemini-3.1-pro-preview sessions, already cheaper than gpt-6.1-sol or too close to it to be
  worth a switch.

Every token count is a multiple of 10, so that every price of pricing.yaml (at most three decimals per million tokens) gives
a whole number of micro-cents and the totals do not depend on how a session is cut into slots. `PLANS` is the token table:
per session the tokens of each model in the five buckets of agentfare.models.Usage, the tool calls, the files touched and
the tool errors. `tests/test_demo_data.py` prices it by hand from pricing.yaml and compares the result with what the real
parsers, the database, `agentfare summary` and `agentfare advise` make of the files. `DOCUMENTED` holds the headline numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "agentfare" / "demo_data"

CANARY = "PRIVACY-CANARY-DEMO"
SEED = "agentfare-demo-2026-09"
NOW = "2026-09-30T18:00:00Z"  # the frozen clock of `agentfare demo`
YEAR = 2026
HOME = "/home/demo/projects"  # an invented home directory (tests/test_hygiene.py allows /home/demo)
CLAUDE_VERSION = "2.1.284"
CODEX_VERSION = "0.159.0"
IDLE_GAP_SECONDS = 1800  # agentfare's default idle gap: a pause longer than this is not active time

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
ASTRA, SOL, SOL_OLD, REVIEW = "gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "codex-auto-review"
G25, G31 = "gemini-2.5-pro", "gemini-3.1-pro-preview"
UNPRICED = (REVIEW,)  # in the logs, with no verified price in pricing.yaml

Tokens = tuple[int, int, int, int, int]  # input, cache_read, cache_write, cache_write_1h, output: the buckets of agentfare.models.Usage


def T(input_k: float, cache_read_k: float, cache_write_k: float, cache_write_1h_k: float, output_k: float) -> Tokens:
    """Tokens written in thousands (one decimal at most), as whole tokens."""
    values = tuple(round(k * 1000) for k in (input_k, cache_read_k, cache_write_k, cache_write_1h_k, output_k))
    assert all(v % 10 == 0 for v in values), values
    return values  # type: ignore[return-value]


@dataclass(frozen=True)
class Part:
    """The responses of one model. ``sub`` names the sub-agent transcript that holds them (None: the main conversation)."""

    model: str
    tokens: Tokens
    tools: int
    sub: str | None = None


@dataclass(frozen=True)
class Plan:
    key: str
    agent: str
    project: str
    start: str  # "MM-DD HH:MM", UTC, 2026
    minutes: int  # from the first prompt to the last reply
    parts: tuple[Part, ...]  # the first one is the main conversation, the others are sub-agents
    prompts: int  # what a person typed (a sub-agent or review thread has none)
    files: int  # distinct files written or edited
    errors: int  # tool calls that failed
    kind: str = "main"  # Codex only: "main", "subagent" (a thread the agent spawned) or "review" (an auto-review thread)
    pause: tuple[int, int] | None = None  # (percent of the session, minutes): silence between two prompts, not active time

    @property
    def tools(self) -> int:
        return sum(p.tools for p in self.parts)


def one(model: str, tokens: Tokens, tools: int) -> tuple[Part, ...]:
    return (Part(model, tokens, tools),)


# ---- the token table -----------------------------------------------------------------------------------------------------
# Key prefix: c Claude Code, x Codex CLI, g Gemini CLI, r Codex auto-review. Tokens are written in thousands.
PLANS: tuple[Plan, ...] = (
    # Monday 2026-09-21
    Plan("c01", "claude", "storefront-api", "09-21 09:05", 118, one(OPUS, T(330, 24500, 620, 0, 190), 50), 4, 9, 4),
    Plan("x01", "codex", "notes-app", "09-21 10:20", 14, one(ASTRA, T(110, 420, 0, 0, 34), 13), 1, 2, 0),
    Plan("g01", "gemini", "notes-app", "09-21 11:05", 16, one(G25, T(90, 300, 0, 0, 14), 12), 1, 2, 1),
    Plan("c02", "claude", "docs-site", "09-21 13:30", 12, one(SONNET, T(32, 310, 30, 0, 14), 9), 1, 1, 0),
    Plan("x02", "codex", "data-pipeline", "09-21 15:10", 18, one(SOL, T(90, 400, 0, 0, 26), 14), 1, 2, 1),
    # Tuesday 2026-09-22
    Plan("c03", "claude", "storefront-api", "09-22 08:45", 47, one(OPUS, T(120, 5200, 210, 0, 70), 30), 2, 4, 2),
    Plan("x03", "codex", "storefront-api", "09-22 09:50", 17, one(ASTRA, T(130, 500, 0, 0, 38), 16), 1, 2, 1),
    Plan("x04", "codex", "docs-site", "09-22 12:40", 15, one(ASTRA, T(100, 380, 0, 0, 30), 12), 1, 2, 0),
    Plan("r01", "codex", "storefront-api", "09-22 10:10", 3, one(REVIEW, T(18, 40, 0, 0, 2), 0), 0, 0, 0, kind="review"),
    Plan("c04", "claude", "notes-app", "09-22 11:15", 9, one(SONNET, T(28, 260, 26, 0, 12), 7), 1, 1, 0),
    Plan("g02", "gemini", "docs-site", "09-22 14:00", 38, one(G31, T(380, 1100, 0, 0, 60), 27), 3, 3, 2),
    Plan("c05", "claude", "infra-terraform", "09-22 15:40", 14, one(SONNET, T(36, 340, 34, 0, 16), 10), 1, 2, 0),
    # Wednesday 2026-09-23
    Plan("c06", "claude", "infra-terraform", "09-23 09:00", 96, one(OPUS, T(280, 19000, 500, 0, 150), 44), 3, 7, 3),
    Plan("x05", "codex", "data-pipeline", "09-23 11:30", 21, one(ASTRA, T(120, 450, 0, 0, 36), 18), 1, 2, 1),
    Plan("c07", "claude", "storefront-api", "09-23 13:10", 11, one(SONNET, T(30, 290, 28, 0, 13), 8), 1, 1, 0),
    Plan("g03", "gemini", "data-pipeline", "09-23 14:20", 24, one(G25, T(150, 520, 0, 0, 22), 19), 2, 2, 1),
    Plan("x06", "codex", "docs-site", "09-23 16:05", 8, one(ASTRA, T(70, 180, 0, 0, 18), 10), 0, 1, 0, kind="subagent"),
    # Thursday 2026-09-24
    Plan("x07", "codex", "data-pipeline", "09-24 08:50", 104, one(ASTRA, T(1400, 8600, 0, 0, 170), 62), 4, 8, 4),
    Plan("g04", "gemini", "notes-app", "09-24 10:30", 19, one(G25, T(100, 360, 0, 0, 16), 14), 1, 2, 0),
    Plan("c09", "claude", "notes-app", "09-24 11:40", 88,
         (Part(OPUS, T(260, 14800, 420, 0, 140), 36), Part(SONNET, T(60, 900, 70, 0, 25), 4, "explore-1"),
          Part(SONNET, T(40, 600, 50, 0, 18), 2, "explore-2")), 3, 8, 3),
    Plan("x08", "codex", "docs-site", "09-24 15:00", 13, one(ASTRA, T(90, 360, 0, 0, 30), 12), 1, 1, 0),
    Plan("r02", "codex", "data-pipeline", "09-24 15:02", 2, one(REVIEW, T(14, 30, 0, 0, 2), 0), 0, 0, 0, kind="review"),
    Plan("c10", "claude", "data-pipeline", "09-24 16:20", 13, one(SONNET, T(34, 300, 32, 0, 15), 10), 1, 2, 1),
    # Friday 2026-09-25
    Plan("c11", "claude", "storefront-api", "09-25 09:15", 132, one(OPUS, T(290, 22000, 150, 1300, 210), 50), 5, 10, 5),
    Plan("x09", "codex", "infra-terraform", "09-25 12:10", 28, one(SOL_OLD, T(210, 1000, 0, 0, 60), 24), 2, 3, 1),
    Plan("g05", "gemini", "data-pipeline", "09-25 13:40", 92,
         (Part(G31, T(1000, 5600, 0, 0, 160), 46), Part(G31, T(100, 600, 0, 0, 20), 8, "agent-1")), 4, 8, 4),
    Plan("x10", "codex", "storefront-api", "09-25 15:30", 36, one(ASTRA, T(310, 1600, 0, 0, 95), 32), 2, 3, 2),
    Plan("c12", "claude", "infra-terraform", "09-25 22:40", 160, one(OPUS, T(300, 20000, 520, 0, 160), 46), 3, 7, 3),
    # Saturday 2026-09-26 and Sunday 2026-09-27
    Plan("x11", "codex", "notes-app", "09-26 11:00", 12, one(ASTRA, T(100, 380, 0, 0, 32), 11), 1, 1, 0),
    Plan("g06", "gemini", "notes-app", "09-27 10:10", 14, one(G25, T(80, 280, 0, 0, 12), 10), 1, 1, 0),
    # Monday 2026-09-28
    Plan("c14", "claude", "data-pipeline", "09-28 09:05", 152, one(OPUS, T(330, 25600, 560, 0, 180), 48), 4, 9, 4, pause=(50, 40)),
    Plan("x12", "codex", "storefront-api", "09-28 10:40", 16, one(ASTRA, T(120, 440, 0, 0, 36), 14), 1, 2, 0),
    Plan("c15", "claude", "docs-site", "09-28 12:00", 12, one(SONNET, T(33, 320, 31, 0, 14), 9), 1, 1, 0),
    Plan("g07", "gemini", "docs-site", "09-28 13:30", 11, one(G25, T(70, 240, 0, 0, 10), 8), 1, 1, 0),
    Plan("x13", "codex", "data-pipeline", "09-28 14:10", 9, one(ASTRA, T(110, 300, 0, 0, 26), 11), 0, 1, 0, kind="subagent"),
    # Tuesday 2026-09-29
    Plan("c16", "claude", "storefront-api", "09-29 08:40", 14, one(OPUS, T(40, 620, 40, 0, 12), 9), 1, 1, 0),
    Plan("x14", "codex", "storefront-api", "09-29 10:00", 27, one(SOL, T(240, 1200, 0, 0, 70), 26), 2, 3, 1),
    Plan("c17", "claude", "infra-terraform", "09-29 11:20", 13, one(SONNET, T(35, 330, 32, 0, 15), 10), 1, 2, 0),
    Plan("g08", "gemini", "data-pipeline", "09-29 14:00", 41, one(G31, T(400, 1200, 0, 0, 70), 30), 3, 3, 2),
    Plan("x15", "codex", "data-pipeline", "09-29 16:10", 15, one(ASTRA, T(130, 480, 0, 0, 40), 15), 1, 2, 1),
    # Wednesday 2026-09-30, in progress at 18:00
    Plan("c18", "claude", "docs-site", "09-30 08:55", 8, one(SONNET, T(25, 220, 22, 0, 10), 6), 1, 1, 0),
    Plan("x16", "codex", "notes-app", "09-30 09:45", 33, one(ASTRA, T(260, 1400, 0, 0, 80), 28), 2, 2, 2),
    Plan("g09", "gemini", "storefront-api", "09-30 11:30", 17, one(G25, T(110, 380, 0, 0, 15), 13), 1, 2, 1),
    Plan("c19", "claude", "notes-app", "09-30 13:05", 13, one(SONNET, T(34, 300, 30, 0, 15), 10), 1, 2, 0),
    Plan("c20", "claude", "storefront-api", "09-30 15:20", 152, one(OPUS, T(380, 28000, 600, 0, 220), 54), 4, 11, 5),
)

# The headline numbers of the table, each re-derived by tests/test_demo_data.py from PLANS and pricing.yaml and compared with
# the real commands (amounts in micro-cents, 1 cent = 1,000,000). Spend leaves out the two codex-auto-review threads, which
# have no verified price; the router saves 21.4% of it with 14 advised switches.
DOCUMENTED: dict[str, Any] = {
    "sessions": 45,
    "by_agent": {"claude": 18, "codex": 18, "gemini": 9},
    "projects": 5,
    "models": 8,  # seven with a price, and codex-auto-review without one
    "spend_ucents": 19_396_450_000,  # $193.9645
    "excluded_tokens": 106_000,  # the two codex-auto-review threads
    "weeks": {"2026-W39": 14_291_250_000, "2026-W40": 5_105_200_000},
    "priced_sessions": 43,
    "switched": 14,
    "router_ucents": 15_241_650_000,
    "saved_ucents": 4_154_800_000,
    "saved_prompted_ucents": 3_791_600_000,  # sessions a person prompted
    "saved_background_ucents": 363_200_000,  # sub-agent threads with 0 prompts
}


# ---- a small deterministic random generator ------------------------------------------------------------------------------
MASK = (1 << 64) - 1


class Rng:
    """splitmix64 seeded from a text. Integers and IEEE arithmetic only (no math library), so that every Python draws the
    same numbers: the standard `random` module promises that for `random()` but not for its helpers."""

    def __init__(self, seed: str) -> None:
        self.state = int.from_bytes(hashlib.sha256(f"{SEED}:{seed}".encode()).digest()[:8], "big")

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * ((self.next() >> 11) / (1 << 53))

    def below(self, n: int) -> int:
        return self.next() % n

    def pick(self, items: Any) -> Any:
        return items[self.below(len(items))]

    def shuffle(self, items: list[Any]) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]


def fake_uuid(label: str, version: int = 4, prefix_hex: str = "") -> str:
    """A UUID made from a text. ``prefix_hex`` (12 hex digits) replaces its start, which is how a UUIDv7 carries its time."""
    h = hashlib.sha256(f"{SEED}:uuid:{label}".encode()).hexdigest()
    h = prefix_hex + h[len(prefix_hex):]
    return f"{h[:8]}-{h[8:12]}-{version}{h[13:16]}-{'89ab'[int(h[16], 16) % 4]}{h[17:20]}-{h[20:32]}"


def fake_hex(label: str, n: int) -> str:
    return hashlib.sha256(f"{SEED}:hex:{label}".encode()).hexdigest()[:n]


def split(total: int, weights: list[float], unit: int = 10) -> list[int]:
    """``total`` cut in proportion to ``weights`` into multiples of ``unit`` that add up to ``total`` exactly."""
    assert total % unit == 0
    units = total // unit
    whole = sum(weights)
    parts = [int(units * w / whole) for w in weights]
    parts[max(range(len(weights)), key=lambda i: weights[i])] += units - sum(parts)
    return [p * unit for p in parts]


def split_tokens(rng: Rng, tokens: Tokens, n: int) -> list[Tokens]:
    """The five buckets of one model spread over ``n`` responses the way a real conversation does: a context that grows (cache
    reads), a big write at the start, small fresh input, and output that varies."""
    inp, read, write, write_1h, out = tokens
    w_in = [rng.uniform(0.6, 1.4) for _ in range(n)]
    w_read = [0.02 if i == 0 else (0.25 + i / n) * rng.uniform(0.85, 1.15) for i in range(n)]
    w_write = [rng.uniform(0.3, 1.0) for _ in range(n)]
    w_write_1h = [rng.uniform(0.6, 1.0) for _ in range(n)]
    if n > 1:
        w_write[0] = sum(w_write[1:]) * 0.35 / 0.65
        w_write_1h[0] = sum(w_write_1h[1:]) * 0.2 / 0.8
    w_out = [rng.uniform(0.3, 2.0) for _ in range(n)]
    w_out[-1] *= 1.5
    columns = (split(inp, w_in), split(read, w_read), split(write, w_write), split(write_1h, w_write_1h), split(out, w_out))
    return [tuple(col[i] for col in columns) for i in range(n)]  # type: ignore[misc]


# ---- the story of one session: who said what when, which tool, which file ------------------------------------------------
PATHS = {
    "storefront-api": ["src/orders/service.py", "src/orders/routes.py", "src/cart/pricing.py", "src/cart/models.py", "src/payments/client.py",
                       "src/payments/webhooks.py", "src/auth/tokens.py", "src/inventory/stock.py", "tests/test_orders.py",
                       "tests/test_cart.py", "tests/test_payments.py", "migrations/0042_add_coupons.py", "config/settings.py",
                       "src/shipping/rates.py"],
    "notes-app": ["src/editor/Editor.tsx", "src/editor/toolbar.ts", "src/sync/queue.ts", "src/sync/merge.ts", "src/store/notes.ts",
                  "src/ui/Sidebar.tsx", "src/ui/theme.css", "src/search/fuzzy.ts", "tests/sync.test.ts", "tests/editor.test.tsx",
                  "src/api/client.ts", "src/export/markdown.ts", "src/export/pdf.ts", "package.json"],
    "infra-terraform": ["modules/vpc/main.tf", "modules/vpc/variables.tf", "modules/db/main.tf", "modules/queue/main.tf",
                        "envs/prod/main.tf", "envs/staging/main.tf", "envs/prod/terraform.tfvars", "modules/iam/policies.tf",
                        "modules/cdn/main.tf", "modules/monitoring/alarms.tf", "modules/dns/main.tf", "modules/cache/main.tf",
                        "scripts/plan.sh", "README.md"],
    "data-pipeline": ["dags/ingest_orders.py", "dags/daily_rollup.py", "etl/transform.py", "etl/schema.py", "etl/loaders/warehouse.py",
                      "etl/loaders/s3.py", "tests/test_transform.py", "tests/test_schema.py", "sql/rollup.sql", "sql/dedupe.sql",
                      "config/pipeline.yaml", "etl/quality.py", "etl/cli.py", "docs/runbook.md"],
    "docs-site": ["docs/getting-started.md", "docs/api/orders.md", "docs/api/auth.md", "docs/guides/webhooks.md", "docusaurus.config.js",
                  "src/components/Banner.tsx", "sidebars.js", "docs/changelog.md", "docs/faq.md", "static/redirects.json",
                  "src/pages/index.tsx", "docs/guides/migrations.md", "package.json", "docs/api/errors.md"],
}
SHELL_COMMANDS = ("pytest -q", "npm test", "terraform validate", "make lint", "python -m etl.cli --dry-run", "npm run build")


@dataclass
class Call:
    """One tool call: a generic kind (read, search, list, shell, edit, agent), the file it names, and whether it failed."""

    kind: str
    path: str | None = None
    fails: bool = False


@dataclass
class Step:
    """One model response: when, which model and tokens, and the tool it calls (None for the last reply of a turn)."""

    at: int  # seconds since the epoch
    model: str
    tokens: Tokens
    call: Call | None
    turn: int
    after: int = 0  # seconds until the tool's output comes back


@dataclass
class Script:
    plan: Plan
    cwd: str
    start: int
    prompts: list[int]  # when each prompt is typed (a review or sub-agent thread has one hand-off message)
    steps: list[Step]  # the responses of the main conversation, in time order
    subs: list[tuple[Part, list[Step]]] = field(default_factory=list)


def epoch(day_time: str) -> int:
    month_day, hhmm = day_time.split()
    month, day = (int(x) for x in month_day.split("-"))
    hour, minute = (int(x) for x in hhmm.split(":"))
    return int(datetime(YEAR, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


def iso(seconds: int, millis: int = 0) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def tool_kinds(rng: Rng, tools: int, files: int, errors: int, spawns: int) -> list[tuple[str, bool]]:
    """(kind, fails) for each tool call of the main conversation: reading first (and the calls that start sub-agents), then the
    edits spread over the rest of the session with test runs between them, and the failures among the test runs."""
    if tools == 0:
        return []
    reading = -(-tools * 3 // 10)
    later = tools - reading
    extra = min(files // 2, max(0, later - files - errors))  # a file edited twice
    edits = files + extra
    shells = later - edits
    assert reading >= spawns and shells >= errors >= 0, (tools, files, errors)
    at = {(i * later) // edits for i in range(edits)} if edits else set()
    shell_slots = [i for i in range(later) if i not in at]
    rng.shuffle(shell_slots)
    failing = set(shell_slots[:errors])
    head = [("list" if i == 0 else rng.pick(("read", "read", "search")), False) for i in range(reading)]
    for i in range(spawns):
        head[1 + 2 * i] = ("agent", False)
    return head + [("edit" if i in at else "shell", i in failing) for i in range(later)]


def compose(rng: Rng, total: int, weights: list[float]) -> list[int]:
    return [c // 10 for c in split(total * 10, weights, 10)]


def script(plan: Plan) -> Script:
    rng = Rng(plan.key)
    start = epoch(plan.start)
    cwd = f"{HOME}/{plan.project}"
    main, subs = plan.parts[0], plan.parts[1:]
    pool = list(PATHS[plan.project])
    rng.shuffle(pool)
    kinds = tool_kinds(rng, main.tools, plan.files, plan.errors, len(subs))
    edit_paths = iter(pool[i % max(1, plan.files)] for i in range(len(kinds)))
    calls = []
    for kind, fails in kinds:
        if kind == "edit":
            calls.append(Call("edit", f"{cwd}/{next(edit_paths)}", fails))
        elif kind in ("read", "list"):
            calls.append(Call(kind, f"{cwd}/{rng.pick(PATHS[plan.project])}", fails))
        else:
            calls.append(Call(kind, None, fails))
    # turns: one per prompt (a thread with no prompt has one), each with some tool calls and a last reply
    turns = max(1, plan.prompts)
    per_turn = compose(rng, len(calls), [rng.uniform(0.5, 1.5) for _ in range(turns)])
    responses: list[tuple[Call | None, int]] = []
    queue = iter(calls)
    for turn, n in enumerate(per_turn):
        responses += [(next(queue), turn) for _ in range(n)]
        responses.append((None, turn))
    # time: the gap before a response that opens a new turn is longer (the person reads and types)
    across = [responses[i][1] != responses[i - 1][1] for i in range(1, len(responses))]
    lead = rng.below(20) + 8 if len(responses) > 1 else plan.minutes * 60  # seconds from the first prompt to the first response
    pause_seconds = plan.pause[1] * 60 if plan.pause else 0
    raw = [rng.uniform(0.6, 1.4) * (3.2 if a else 1.0) for a in across]
    budget = plan.minutes * 60 - lead - pause_seconds
    gaps = [max(2, int(budget * w / sum(raw))) for w in raw]
    if gaps:
        gaps[max(range(len(gaps)), key=lambda i: raw[i])] += budget - sum(gaps)
    pause_at = None
    if plan.pause:
        target = len(gaps) * plan.pause[0] // 100
        turn_starts = [i for i, a in enumerate(across) if a]
        pause_at = next((i for i in turn_starts if i >= target), turn_starts[-1])
        gaps[pause_at] += pause_seconds
    assert all(g <= IDLE_GAP_SECONDS for i, g in enumerate(gaps) if i != pause_at), (plan.key, max(gaps))
    times = [start + lead]
    for g in gaps:
        times.append(times[-1] + g)
    tokens = split_tokens(rng, main.tokens, len(responses))
    steps = []
    prompts = [start]
    for i, ((call, turn), at, used) in enumerate(zip(responses, times, tokens)):
        room = times[i + 1] - at if i + 1 < len(times) else 0
        steps.append(Step(at, main.model, used, call, turn, max(2, min(int(room * 0.55), rng.below(40) + 3)) if call else 0))
        if call is None and i + 1 < len(times):
            prompts.append(times[i + 1] - (rng.below(20) + 6) if i == pause_at else at + int(gaps[i] * 0.7))
    result = Script(plan, cwd, start, prompts, steps)
    # sub-agents: a short burst after the session has got going, reading only
    for part in subs:
        n = part.tools + 1
        toks = split_tokens(rng, part.tokens, n)
        at = start + int(plan.minutes * 60 * rng.uniform(0.3, 0.6))
        seq = []
        for i in range(n):
            at += rng.below(25) + 12
            call = Call(rng.pick(("read", "read", "search")), f"{cwd}/{rng.pick(PATHS[plan.project])}") if i < part.tools else None
            seq.append(Step(at, part.model, toks[i], call, 0, rng.below(6) + 2 if call else 0))
        result.subs.append((part, seq))
    return result


def jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in records)


def ms_of(label: str) -> int:
    return int(fake_hex(label, 4), 16) % 900 + 50


# ---- Claude Code ---------------------------------------------------------------------------------------------------------
def claude_tool(call: Call, uid: str, cwd: str, n: int) -> dict[str, Any]:
    if call.kind == "edit":
        if n % 5 == 4:
            return {"type": "tool_use", "id": uid, "name": "Write", "input": {"file_path": call.path, "content": f"# {CANARY}\n"}}
        return {"type": "tool_use", "id": uid, "name": "Edit",
                "input": {"file_path": call.path, "old_string": f"{CANARY} old", "new_string": f"{CANARY} new"}}
    if call.kind == "read":
        return {"type": "tool_use", "id": uid, "name": "Read", "input": {"file_path": call.path}}
    if call.kind == "list":
        return {"type": "tool_use", "id": uid, "name": "Glob", "input": {"pattern": "**/*"}}
    if call.kind == "search":
        return {"type": "tool_use", "id": uid, "name": "Grep", "input": {"pattern": f"{CANARY}-pattern", "path": cwd}}
    if call.kind == "agent":
        return {"type": "tool_use", "id": uid, "name": "Agent",
                "input": {"description": f"{CANARY} explore", "prompt": f"{CANARY} look around", "subagent_type": "Explore"}}
    return {"type": "tool_use", "id": uid, "name": "Bash",
            "input": {"command": f"{SHELL_COMMANDS[n % len(SHELL_COMMANDS)]} # {CANARY}", "description": f"{CANARY} run"}}


class ClaudeLog:
    """One Claude Code transcript: every record has the envelope of the real format and points at the one before it."""

    def __init__(self, session: str, cwd: str, agent: str | None = None) -> None:
        self.session, self.cwd, self.agent = session, cwd, agent
        self.records: list[dict[str, Any]] = []
        self.parent: str | None = None
        self.count = 0

    def add(self, kind: str, at: int, label: str, **fields: Any) -> None:
        uid = fake_uuid(f"{self.session}:{self.agent}:{self.count}")
        rec: dict[str, Any] = {"parentUuid": self.parent, "isSidechain": self.agent is not None}
        if self.agent:
            rec["agentId"] = self.agent
        rec.update(fields)
        rec.update(type=kind, uuid=uid, timestamp=iso(at, ms_of(f"{uid}{label}")), cwd=self.cwd, sessionId=self.session, version=CLAUDE_VERSION)
        self.records.append(rec)
        self.parent = uid
        self.count += 1

    def prompt(self, at: int, content: str) -> None:
        self.add("user", at, "prompt", message={"role": "user", "content": content})

    def response(self, step: Step, n: int, *, final: bool, stream: bool) -> None:
        """One API response. With ``stream`` it is written as two records that share their message id, the first with the
        snapshot of the stream start (output tokens of 2), as Claude Code does."""
        inp, read, write, write_1h, out = step.tokens
        msg = f"msg_{fake_hex(f'{self.session}:{self.agent}:msg{n}', 16)}"

        def usage(output: int) -> dict[str, Any]:
            return {"input_tokens": inp, "cache_creation_input_tokens": write + write_1h, "cache_read_input_tokens": read,
                    "output_tokens": output, "cache_creation": {"ephemeral_5m_input_tokens": write, "ephemeral_1h_input_tokens": write_1h}}

        def message(block: dict[str, Any], output: int, stop: str | None) -> dict[str, Any]:
            return {"model": step.model, "id": msg, "type": "message", "role": "assistant", "content": [block],
                    "stop_reason": stop, "usage": usage(output)}

        if final:
            self.add("assistant", step.at, "text", message=message({"type": "text", "text": f"{CANARY} fake answer"}, out, "end_turn"))
            return
        if stream:
            self.add("assistant", step.at - 2, "thinking",
                     message=message({"type": "thinking", "thinking": f"{CANARY} fake thinking", "signature": "FAKE-SIGNATURE"}, 2, None))
        uid = f"toolu_{fake_hex(f'{self.session}:{self.agent}:tool{n}', 22)}"
        assert step.call is not None
        self.add("assistant", step.at, "tool", message=message(claude_tool(step.call, uid, self.cwd, n), out, "tool_use"))

    def result(self, step: Step, n: int) -> None:
        assert step.call is not None
        uid = f"toolu_{fake_hex(f'{self.session}:{self.agent}:tool{n}', 22)}"
        text = f"{CANARY} fake failure" if step.call.fails else f"{CANARY} fake output"
        block = {"tool_use_id": uid, "type": "tool_result", "content": text, "is_error": step.call.fails}
        self.add("user", step.at + step.after, "result", message={"role": "user", "content": [block]})


def claude_files(s: Script) -> dict[str, str]:
    plan = s.plan
    session = fake_uuid(f"claude:{plan.key}")
    folder = "-" + s.cwd.strip("/").replace("/", "-")
    base = f"claude/projects/{folder}/{session}"
    log = ClaudeLog(session, s.cwd)
    log.records.append({"type": "permission-mode", "permissionMode": "default", "sessionId": session})
    prompt_at = iter(s.prompts)
    turn = -1
    for n, step in enumerate(s.steps):
        if step.turn != turn:
            turn = step.turn
            log.prompt(next(prompt_at), f"{CANARY} fake prompt {turn + 1} for the demo")
        if step.call is None:
            log.response(step, n, final=True, stream=False)
        else:
            log.response(step, n, final=False, stream=fake_hex(f"{session}:{n}", 2) < "40")
            log.result(step, n)
    files = {f"{base}.jsonl": jsonl(log.records)}
    for part, steps in s.subs:
        agent = fake_hex(f"{session}:{part.sub}", 16)
        sub = ClaudeLog(session, s.cwd, agent)
        sub.prompt(steps[0].at - 6, f"{CANARY} fake task for the sub-agent")
        for n, step in enumerate(steps):
            if step.call is None:
                sub.response(step, n, final=True, stream=False)
            else:
                sub.response(step, n, final=False, stream=False)
                sub.result(step, n)
        files[f"{base}/subagents/agent-{agent}.jsonl"] = jsonl(sub.records)
        files[f"{base}/subagents/agent-{agent}.meta.json"] = json.dumps({"agentType": "Explore", "description": f"{CANARY} survey"}) + "\n"
    return files


# ---- Codex CLI -----------------------------------------------------------------------------------------------------------
def codex_files(s: Script) -> dict[str, str]:
    plan = s.plan
    start_ms = s.start * 1000
    thread = fake_uuid(f"codex:{plan.key}", version=7, prefix_hex=f"{start_ms:012x}")
    moment = datetime.fromtimestamp(s.start, timezone.utc)
    stem = moment.strftime("%Y-%m-%dT%H-%M-%S")
    rel = f"codex/sessions/{moment:%Y/%m/%d}/rollout-{stem}-{thread}.jsonl"
    records: list[dict[str, Any]] = []

    def add(at: int, kind: str, payload: dict[str, Any], label: str) -> None:
        records.append({"timestamp": iso(at, ms_of(f"{thread}{label}{len(records)}")), "ordinal": len(records), "type": kind, "payload": payload})

    source: Any = "cli"
    if plan.kind == "subagent":
        source = {"subagent": {"thread_spawn": {"parent_thread_id": fake_uuid(f"codex:{plan.key}:parent", 7), "depth": 1}}}
    elif plan.kind == "review":
        source = {"subagent": {"other": "guardian"}}
    add(s.start, "session_meta", {
        "id": thread, "timestamp": iso(s.start), "cwd": s.cwd, "originator": "codex_cli_rs", "cli_version": CODEX_VERSION,
        "source": source, "model_provider": "openai",
        "git": {"commit_hash": fake_hex(f"{thread}:commit", 40), "branch": "main", "repository_url": f"https://example.invalid/demo/{plan.project}.git"},
    }, "meta")
    totals = [0, 0, 0, 0]
    turn_id = ""
    prompt_at = iter(s.prompts)
    turn = -1
    for n, step in enumerate(s.steps):
        if step.turn != turn:
            if turn >= 0:
                add(s.steps[n - 1].at + 1, "event_msg", {"type": "task_complete", "turn_id": turn_id}, "done")
            turn = step.turn
            turn_id = fake_uuid(f"codex:{plan.key}:turn{turn}", 7, f"{(s.start + turn) * 1000:012x}")
            at = next(prompt_at)
            add(at, "event_msg", {"type": "task_started", "turn_id": turn_id}, "start")
            add(at, "turn_context", {"turn_id": turn_id, "cwd": s.cwd, "model": step.model, "approval_policy": "on-request",
                                     "sandbox_policy": {"type": "workspace-write"}, "effort": "medium"}, "ctx")
            add(at + 1, "event_msg", {"type": "item_completed", "turn_id": turn_id, "item": {
                "type": "UserMessage", "id": f"item-{n}", "content": [{"type": "text", "text": f"{CANARY} fake prompt {turn + 1} for the demo", "text_elements": []}]}}, "user")
        inp, read, write, _, out = step.tokens
        used = (inp + read + write, read, write, out)
        totals = [a + b for a, b in zip(totals, used)]
        reasoning = out * 45 // 100 // 10 * 10

        def counts(values: tuple[int, int, int, int] | list[int], think: int) -> dict[str, int]:
            return {"input_tokens": values[0], "cached_input_tokens": values[1], "cache_write_input_tokens": values[2],
                    "output_tokens": values[3], "reasoning_output_tokens": think, "total_tokens": values[0] + values[3]}

        call = step.call
        if call is not None:
            call_id = f"call_{fake_hex(f'{thread}:call{n}', 24)}"
            if call.kind == "edit":
                assert call.path is not None
                text = f'await tools.apply_patch("*** Begin Patch\\n*** Update File: {call.path}\\n@@\\n-{CANARY} old\\n+{CANARY} new\\n*** End Patch");'
            else:
                command = SHELL_COMMANDS[n % len(SHELL_COMMANDS)] if call.kind == "shell" else f"cat {call.path}" if call.path else "ls"
                text = f'const r = await tools.exec_command({{cmd: "{command} # {CANARY}"}}); text(r.output);'
            add(step.at, "response_item", {"type": "custom_tool_call", "status": "completed", "call_id": call_id, "name": "exec", "input": text}, "call")
            if call.kind == "edit":
                add(step.at + 1, "event_msg", {"type": "item_completed", "turn_id": turn_id, "item": {
                    "type": "FileChange", "id": f"item-{n}f", "status": "completed",
                    "changes": {call.path: {"type": "update", "unified_diff": f"@@\n-{CANARY} old\n+{CANARY} new\n", "move_path": None}}}}, "file")
        else:
            add(step.at, "response_item", {"type": "message", "role": "assistant", "phase": "final_answer",
                                           "content": [{"type": "output_text", "text": f"{CANARY} fake answer"}]}, "answer")
        add(step.at + 1, "event_msg", {"type": "token_count", "info": {
            "total_token_usage": counts(totals, totals[3] * 45 // 100 // 10 * 10),
            "last_token_usage": counts(used, reasoning), "model_context_window": 258400}}, "tokens")
        if call is not None:
            head = "Script failed" if call.fails else "Script completed"
            add(step.at + step.after, "response_item", {"type": "custom_tool_call_output", "call_id": call_id, "output": [
                {"type": "input_text", "text": f"{head}\nWall time 0.4 seconds\nOutput:\n"},
                {"type": "input_text", "text": f"{CANARY} fake {'failure' if call.fails else 'output'}"}]}, "out")
    add(s.steps[-1].at + 2, "event_msg", {"type": "task_complete", "turn_id": turn_id}, "done")
    return {rel: jsonl(records)}


# ---- Gemini CLI ----------------------------------------------------------------------------------------------------------
def gemini_tool(call: Call, at: int, uid: str, cwd: str, n: int) -> dict[str, Any]:
    names = {"read": "read_file", "list": "list_directory", "search": "search_file_content", "shell": "run_shell_command",
             "agent": "codebase_investigator"}
    arguments = {
        "read": {"file_path": call.path}, "list": {"dir_path": cwd}, "search": {"pattern": f"{CANARY}-pattern"},
        "shell": {"command": f"{SHELL_COMMANDS[n % len(SHELL_COMMANDS)]} # {CANARY}"}, "agent": {"objective": f"{CANARY} explore"},
    }
    if call.kind == "edit":
        name = "replace" if n % 5 else "write_file"
        args: dict[str, Any] = {"file_path": call.path}
        args.update({"old_string": f"{CANARY} old", "new_string": f"{CANARY} new"} if name == "replace" else {"content": f"# {CANARY}\n"})
    else:
        name, args = names[call.kind], arguments[call.kind]
    output = f"{CANARY} fake failure" if call.fails else f"{CANARY} fake output"
    return {"id": uid, "name": name, "args": args, "status": "error" if call.fails else "success",
            "result": [{"functionResponse": {"id": uid, "name": name, "response": {"output": output}}}], "timestamp": iso(at)}


def gemini_messages(session: str, cwd: str, steps: list[Step], prompts: list[int], key: str, *, first_prompt: bool = True) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    prompt_at = iter(prompts)
    turn = -1
    for n, step in enumerate(steps):
        if step.turn != turn and first_prompt:
            turn = step.turn
            at = next(prompt_at)
            records.append({"id": f"u{key}{turn}", "timestamp": iso(at, ms_of(f"{session}u{turn}")), "type": "user",
                            "content": [{"text": f"{CANARY} fake prompt {turn + 1} for the demo"}]})
        inp, read, _, _, out = step.tokens
        tool = inp * 4 // 100 // 10 * 10
        thoughts = out * 40 // 100 // 10 * 10
        message: dict[str, Any] = {
            "id": f"g{key}{n}", "timestamp": iso(step.at, ms_of(f"{session}g{n}")), "type": "gemini", "content": f"{CANARY} fake answer",
            "thoughts": [{"subject": f"{CANARY} thought", "description": f"{CANARY} fake thinking", "timestamp": iso(step.at, ms_of(f"{session}g{n}"))}],
            "tokens": {"input": inp - tool + read, "output": out - thoughts, "cached": read, "thoughts": thoughts, "tool": tool,
                       "total": inp + read + out},
            "model": step.model,
        }
        if step.call is not None:
            message["toolCalls"] = [gemini_tool(step.call, step.at + step.after, f"{key}-{n}-{fake_hex(f'{session}:{n}', 6)}", cwd, n)]
        records.append(message)
    return records


def gemini_files(s: Script) -> dict[str, str]:
    plan = s.plan
    session = fake_uuid(f"gemini:{plan.key}")
    moment = datetime.fromtimestamp(s.start, timezone.utc)
    base = f"gemini/tmp/{plan.project}"
    last = max([s.steps[-1].at] + [steps[-1].at for _, steps in s.subs])
    project_hash = hashlib.sha256(f"{SEED}:project:{plan.project}".encode()).hexdigest()
    header = {"sessionId": session, "projectHash": project_hash, "startTime": iso(s.start), "lastUpdated": iso(last), "kind": "main"}
    records = [header, *gemini_messages(session, s.cwd, s.steps, s.prompts, "a"), {"$set": {"lastUpdated": iso(last)}}]
    name = f"session-{moment:%Y-%m-%dT%H-%M}-{session[:8]}.jsonl"
    files = {f"{base}/chats/{name}": jsonl(records), f"{base}/.project_root": s.cwd}
    for part, steps in s.subs:
        agent = f"{part.sub}"
        sub_header = {**header, "sessionId": agent, "startTime": iso(steps[0].at - 5), "lastUpdated": iso(steps[-1].at), "kind": "subagent"}
        records = [sub_header, *gemini_messages(agent, s.cwd, steps, [], "s", first_prompt=False)]
        files[f"{base}/chats/{session}/{agent}.jsonl"] = jsonl(records)
    return files


# ---- the whole tree ------------------------------------------------------------------------------------------------------
CONFIG = """\
# Config for the synthetic demo logs that `agentfare demo` runs on. Relative paths are resolved against this file's directory.
#   AGENTFARE_HOME=$(mktemp -d) AGENTFARE_NOW=2026-09-30T18:00:00Z agentfare --config config.yaml summary
timezone: UTC
sources:
  claude: [claude/projects]
  codex: [codex/sessions]
  gemini: [gemini/tmp]
"""

WRITERS = {"claude": claude_files, "codex": codex_files, "gemini": gemini_files}


def generate() -> dict[str, str]:
    """Every file of the demo data, as ``{path relative to demo_data: text}``, in a stable order."""
    files = {"config.yaml": CONFIG}
    for plan in PLANS:
        files.update(WRITERS[plan.agent](script(plan)))
    return dict(sorted(files.items()))


def on_disk() -> dict[str, str]:
    return {p.relative_to(OUT).as_posix(): p.read_text(encoding="utf-8") for p in sorted(OUT.rglob("*")) if p.is_file()} if OUT.is_dir() else {}


def write(files: dict[str, str]) -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    for rel, text in files.items():
        target = OUT / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate src/agentfare/demo_data (synthetic logs for `agentfare demo`).")
    parser.add_argument("--check", action="store_true", help="do not write: exit 1 when the committed files differ from a fresh generation")
    args = parser.parse_args(argv)
    fresh = generate()
    if args.check:
        have = on_disk()
        drift = sorted({*fresh, *have} - {p for p in fresh if have.get(p) == fresh[p]})
        for path in drift:
            print(f"differs: {path}", file=sys.stderr)
        return 1 if drift else 0
    write(fresh)
    size = sum(len(text.encode()) for text in fresh.values())
    print(f"wrote {len(fresh)} files, {size / 1024:.0f} KiB, to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

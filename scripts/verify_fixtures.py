#!/usr/bin/env python3
"""Independent arithmetic for the synthetic fixtures. Does NOT import agentfare.

This script re-derives, from the raw JSONL fixtures and the raw ``pricing.yaml``, everything that
``tests/fixtures/EXPECTED.md`` and ``tests/test_e2e_fixtures.py`` state about the three sample sessions: tokens,
dollars, router signals, score, tier, recommendation, savings and the ``agentfare run`` estimates. It uses only the
standard library and PyYAML, its own minimal reader for each agent's log format (rules taken from the parser
docstrings) and exact ``fractions.Fraction`` arithmetic, so a bug in the product's parsers or cost engine cannot hide
behind the same bug here.

    python scripts/verify_fixtures.py             # human-readable report
    python scripts/verify_fixtures.py --json      # every derived number, machine-readable
    python scripts/verify_fixtures.py --markdown  # the generated tables of EXPECTED.md
    python scripts/verify_fixtures.py --check     # exit 1 unless EXPECTED.md holds exactly those tables

Money is exact: dollars = tokens x (USD per 1M tokens) / 1,000,000. One micro-cent ("ucent") is 1e-8 USD, so a
token at $P per 1M tokens costs 100 * P ucents and every fixture cost below is a whole number of ucents (the script
asserts it).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
PRICING = ROOT / "src" / "agentfare" / "data" / "pricing.yaml"
EXPECTED_MD = FIXTURES / "EXPECTED.md"

IDLE_GAP_SECONDS = 30 * 60  # idle_gap_minutes: 30 (config default; the fixtures' config.yaml does not change it)
BUCKETS = ("input", "cache_read", "cache_write", "cache_write_1h", "output")
UCENTS_PER_USD = 100 * 1_000_000

# ---- the router defaults, copied from docs/ROUTING.md (a test compares them with `agentfare config --default`) ----
SIGNAL_LIMITS = {  # signal -> (medium, heavy); a signal scores 0 below medium, 1 from medium, 2 from heavy
    "input_tokens": (2_000_000, 25_000_000),
    "tool_calls": (25, 180),
    "files_touched": (1, 6),
    "error_retries": (2, 8),
    "active_minutes": (10, 80),
}
TIER_CUTS = (2, 6)  # score <= 2 light, <= 6 medium, else heavy
RECOMMEND = {"light": "gpt-6.1-sol", "medium": "gpt-6.1-sol", "heavy": None}
MIN_SAVING_RATIO = Fraction(6, 5)  # min_saving_ratio: 1.2
RUN_MODELS = {  # router.run.models
    "claude": {"light": "claude-sonnet-5-5", "medium": "claude-sonnet-5-5", "heavy": "claude-opus-5-5"},
    "codex": {"light": "gpt-6.1-sol", "medium": "gpt-6.1-sol", "heavy": "gpt-6-astra"},
    "gemini": {"light": "gemini-3.1-flash-lite", "medium": "gemini-3-flash-preview", "heavy": "gemini-3.1-pro-preview"},
}
RUN_DEFAULT_TOKENS = {  # router.run.default_tokens (used below 5 past sessions of the same agent and tier)
    "light": {"input": 30_000, "cache_read": 600_000, "cache_write": 25_000, "output": 7_000},
    "medium": {"input": 135_000, "cache_read": 8_800_000, "cache_write": 130_000, "output": 65_000},
    "heavy": {"input": 770_000, "cache_read": 118_000_000, "cache_write": 1_500_000, "output": 480_000},
}


# =============================================================================================
# small helpers
# =============================================================================================
def dec(x: Fraction | int, places: int = 2) -> str:
    """Exact decimal text of a number whose denominator has only the factors 2 and 5, at least ``places`` decimals."""
    x = Fraction(x)
    d = x.denominator
    for p in (2, 5):
        while d % p == 0:
            d //= p
    assert d == 1, f"{x} has no finite decimal expansion"
    while (x * 10**places).denominator != 1:
        places += 1
    n = abs(int(x * 10**places))
    whole, frac = divmod(n, 10**places)
    return f"{'-' if x < 0 else ''}{whole}.{frac:0{places}d}"


def usd(x: Fraction | int, places: int = 2) -> str:
    return "$" + dec(x, places)


def ucents(x: Fraction) -> int:
    """Whole micro-cents of a dollar amount (asserted exact)."""
    scaled = Fraction(x) * UCENTS_PER_USD
    assert scaled.denominator == 1, f"{x} USD is not a whole number of micro-cents"
    return int(scaled)


def cents_half_up(u: int) -> int:
    return (u + 500_000) // 1_000_000


def thousands(n: int) -> str:
    return f"{n:,}"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    """A log timestamp cut to whole seconds (the parsers do the same), as aware UTC."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0)


def iso_week(dt: datetime) -> str:
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Objects of a JSONL file; blank, truncated or non-object lines are skipped."""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def active_seconds(stamps: list[datetime]) -> int:
    ordered = sorted(stamps)
    gaps = [int((b - a).total_seconds()) for a, b in zip(ordered, ordered[1:])]
    return sum(g for g in gaps if 0 <= g <= IDLE_GAP_SECONDS)


def basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


# =============================================================================================
# prices: raw YAML, every number an exact Fraction
# =============================================================================================
class _ExactLoader(yaml.SafeLoader):
    """SafeLoader that reads YAML floats as Fractions of the literal text (4.0 -> 4, 0.125 -> 1/8), never as binary floats."""


_ExactLoader.add_constructor("tag:yaml.org,2002:float", lambda loader, node: Fraction(loader.construct_scalar(node)))


def load_prices(path: Path = PRICING) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_ExactLoader)
    return doc["models"], doc.get("aliases") or {}


def rate(entry: dict[str, Any], bucket: str) -> Fraction:
    """USD per 1M tokens of one usage bucket. A missing cache_write is billed at the input rate, a missing
    cache_write_1h at the cache_write rate (pricing.yaml header)."""
    if bucket == "input":
        return Fraction(entry["input"])
    if bucket == "cache_read":
        return Fraction(entry["cached_input"])
    if bucket == "output":
        return Fraction(entry["output"])
    cache_write = Fraction(entry["cache_write"]) if "cache_write" in entry else Fraction(entry["input"])
    if bucket == "cache_write":
        return cache_write
    return Fraction(entry["cache_write_1h"]) if "cache_write_1h" in entry else cache_write


def price_of(tokens: dict[str, int], entry: dict[str, Any]) -> Fraction:
    """Dollars for a token dict at one model's rates: sum(tokens x rate) / 1,000,000, exact."""
    return sum((Fraction(tokens.get(b, 0)) * rate(entry, b) for b in BUCKETS), Fraction(0)) / 1_000_000


# =============================================================================================
# one session, as this script understands it
# =============================================================================================
class Session:
    def __init__(self, agent: str, session_id: str, project: str) -> None:
        self.agent, self.session_id, self.project = agent, session_id, project
        self.usage: dict[str, dict[str, int]] = {}  # model as logged -> bucket -> tokens
        self.responses: list[dict[str, Any]] = []  # one row per billed response: label, model, raw counters, tokens
        self.stamps: list[datetime] = []
        self.tool_calls = self.files_touched = self.error_retries = self.prompts = 0

    def add_response(self, label: str, model: str, tokens: dict[str, int], when: datetime, raw: dict[str, int] | None = None) -> None:
        """Bill one response: ``tokens`` are the normalised buckets, ``when`` the time of the record that carries it (money belongs
        to when it was spent), ``raw`` the counters as the log names them."""
        into = self.usage.setdefault(model, dict.fromkeys(BUCKETS, 0))
        for bucket, n in tokens.items():
            into[bucket] += n
        self.responses.append({"label": label, "model": model, "tokens": {b: tokens.get(b, 0) for b in BUCKETS}, "when": when, "raw": raw or {}})

    @property
    def started(self) -> datetime:
        return min(self.stamps)

    @property
    def active(self) -> int:
        return active_seconds(self.stamps)

    @property
    def short_id(self) -> str:
        return self.session_id[-8:]


# ---- Claude Code -------------------------------------------------------------------------
_CLAUDE_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_CLAUDE_INJECTED = re.compile(
    r"\s*(?:<(?:command-name|command-message|command-args|local-command-stdout|local-command-stderr"
    r"|local-command-caveat|system-reminder|task-notification|bash-input|bash-stdout|bash-stderr"
    r"|user-prompt-submit-hook|ide_opened_file|ide_selection|paseo-system)(?=[\s>/])|\[Request interrupted)"
)


def _typed_by_a_person(rec: dict[str, Any], content: Any) -> bool:
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly") or rec.get("isSidechain"):
        return False
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind", "human") != "human":
        return False
    if rec.get("promptSource") == "system":
        return False
    if isinstance(content, str):
        return bool(content.strip()) and not _CLAUDE_INJECTED.match(content)
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    if any(b.get("type") == "image" for b in blocks):
        return True
    return any(
        isinstance(b.get("text"), str) and b["text"].strip() and not _CLAUDE_INJECTED.match(b["text"])
        for b in blocks
        if b.get("type") == "text"
    )


def read_claude(main: Path) -> Session:
    """One Claude Code session: the main file plus every sub-agent transcript below it.

    * an API response is written as several assistant records sharing message.id: the LAST record is the response;
    * cache writes: cache_creation_input_tokens is the total, cache_creation.ephemeral_1h_input_tokens the 1-hour part
      (no cache_creation object: all 5-minute);
    * records that name another session (a fork's replayed history) and <synthetic> messages are skipped.
    """
    sid = main.stem
    files = [main, *sorted((main.parent / sid / "subagents").rglob("*.jsonl"))]
    responses: dict[str, tuple[str, dict[str, int], datetime]] = {}
    tool_ids: set[str] = set()
    written: set[str] = set()
    errors: set[str] = set()
    prompts: set[str] = set()
    stamps: list[datetime] = []
    cwd: str | None = None
    for file in files:
        for rec in read_jsonl(file):
            if rec.get("type") not in ("user", "assistant"):
                continue
            if any(rec.get(k) not in (None, sid) for k in ("sessionId", "session_id")):
                continue  # replayed history of another session
            stamps.append(parse_ts(rec["timestamp"]))
            cwd = cwd or rec.get("cwd")
            message = rec.get("message") or {}
            content = message.get("content")
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            if rec["type"] == "assistant":
                if message.get("model") == "<synthetic>":
                    continue
                for block in blocks:
                    if block.get("type") != "tool_use":
                        continue
                    tool_ids.add(block["id"])
                    if block.get("name") in _CLAUDE_WRITE_TOOLS:
                        args = block.get("input") or {}
                        path = args.get("file_path") or args.get("notebook_path") or args.get("path")
                        if path:
                            written.add(path)
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                total_writes = usage.get("cache_creation_input_tokens") or 0
                one_hour = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
                responses[message["id"]] = (  # last record of a message.id wins, and its time is the time of the response
                    message["model"],
                    {
                        "input": usage.get("input_tokens") or 0,
                        "cache_read": usage.get("cache_read_input_tokens") or 0,
                        "cache_write": total_writes - one_hour,
                        "cache_write_1h": one_hour,
                        "output": usage.get("output_tokens") or 0,
                    },
                    stamps[-1],
                )
            else:
                carries_result = False
                for block in blocks:
                    if block.get("type") == "tool_result":
                        carries_result = True
                        if block.get("is_error") is True and not rec.get("toolDenialKind"):
                            errors.add(block["tool_use_id"])
                if file == main and not carries_result and _typed_by_a_person(rec, content):
                    prompts.add(rec["uuid"])
    session = Session("claude", sid, basename(cwd or ""))
    for label, (model, tokens, when) in responses.items():
        session.add_response(label, model, tokens, when)
    session.stamps = stamps
    session.tool_calls, session.files_touched = len(tool_ids), len(written)
    session.error_retries, session.prompts = len(errors), len(prompts)
    return session


# ---- Codex CLI ---------------------------------------------------------------------------
def read_codex(path: Path) -> Session:
    """One Codex CLI thread.

    * input_tokens INCLUDES cached_input_tokens and output_tokens INCLUDES reasoning: uncached = input - cached;
    * each response is billed once: token_usage_record deduplicated by response_id; a token_count event is the same
      response as a usage record with the same counts, and one whose running total did not move is a rate-limit update;
    * model of a response = turn_context.model of its turn.
    Only the parts of the activity rules that the fixture exercises are implemented (see the docstring of
    agentfare.parsers.codex for the full rules).
    """
    meta: dict[str, Any] = {}
    turn_model: dict[str, str] = {}
    stamps: list[datetime] = []
    seen_ids: set[str] = set()
    billed: list[tuple[str, str, tuple[int, int, int, int], datetime]] = []  # (turn_id, response label, (input, cached, written, output), time)
    unmatched_records: Counter[tuple[int, int, int, int]] = Counter()
    unmatched_events: Counter[tuple[int, int, int, int]] = Counter()
    last_total: tuple[int, int, int, int] | None = None
    call_ids: set[str] = set()
    failed_calls: set[str] = set()
    changed: set[str] = set()
    prompts: set[str] = set()

    def counts(u: dict[str, Any]) -> tuple[int, int, int, int]:
        return (u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0,
                u.get("cache_write_input_tokens") or 0, u.get("output_tokens") or 0)

    for rec in read_jsonl(path):
        stamps.append(parse_ts(rec["timestamp"]))
        payload = rec.get("payload") or {}
        kind, sub = rec.get("type"), payload.get("type")
        if kind == "session_meta":
            meta = payload
        elif kind == "turn_context":
            turn_model[payload["turn_id"]] = payload["model"]
        elif kind == "token_usage_record":
            if payload["response_id"] in seen_ids:
                continue
            seen_ids.add(payload["response_id"])
            c = counts(payload["usage"])
            if unmatched_events[c]:
                unmatched_events[c] -= 1  # its token_count was seen first and already billed it
            else:
                unmatched_records[c] += 1
                billed.append((payload["turn_id"], payload["response_id"], c, stamps[-1]))
        elif kind == "event_msg" and sub == "token_count":
            info = payload.get("info")
            if not info:
                continue
            total = counts(info["total_token_usage"])
            if total == last_total:
                continue  # rate-limit-only update
            last_total = total
            last = counts(info["last_token_usage"])
            if unmatched_records[last]:
                unmatched_records[last] -= 1  # its usage record was seen first and already billed it
            elif any(last):
                unmatched_events[last] += 1
                billed.append((payload.get("turn_id", ""), f"token_count at {rec['timestamp']}", last, stamps[-1]))
        elif kind == "response_item" and sub in ("function_call", "custom_tool_call", "local_shell_call"):
            call_ids.add(payload["call_id"])
        elif kind == "response_item" and sub in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            text = "".join(part.get("text", "") for part in payload.get("output") or [] if isinstance(part, dict))
            if text.startswith("Script failed"):
                failed_calls.add(payload["call_id"])
        elif kind == "event_msg" and sub == "item_completed":
            item = payload.get("item") or {}
            if item.get("type") == "UserMessage":
                prompts.add(item["id"])
            elif item.get("type") == "FileChange" and item.get("status") not in ("failed", "declined"):
                for file, change in (item.get("changes") or {}).items():
                    changed.add(file)
                    if change.get("move_path"):
                        changed.add(change["move_path"])
    repo = (meta.get("git") or {}).get("repository_url") or ""
    project = re.sub(r"\.git$", "", basename(repo)) if repo else basename(meta.get("cwd") or "")
    session = Session("codex", meta["session_id"], project)
    for turn, label, (inp, cached, written, out), when in billed:
        session.add_response(
            label, turn_model[turn],
            {"input": inp - cached - written, "cache_read": cached, "cache_write": written, "output": out},
            when,
            {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out},
        )
    session.stamps = stamps
    session.tool_calls, session.files_touched = len(call_ids), len(changed)
    session.error_retries, session.prompts = len(failed_calls), len(prompts)
    return session


# ---- Gemini CLI --------------------------------------------------------------------------
def read_gemini(path: Path) -> Session:
    """One Gemini CLI session.

    * the LAST record with a message id is that message (the CLI re-appends it as tokens and tool calls arrive);
      {"$set"...} and {"$rewindTo"...} records are not messages and truncated lines are skipped;
    * tokens: input = promptTokenCount - cached + tool (toolUsePromptTokenCount), cache_read = cached,
      output = candidates + thoughts.
    """
    header: dict[str, Any] = {}
    messages: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl(path):
        if "sessionId" in rec and "id" not in rec:
            header = rec
        elif "id" in rec and "type" in rec:
            messages[rec["id"]] = rec
    root = (path.parent.parent / ".project_root").read_text(encoding="utf-8").strip()
    session = Session("gemini", header["sessionId"], basename(root))
    stamps = [parse_ts(header["startTime"]), parse_ts(header["lastUpdated"])]
    written: set[str] = set()
    for msg in messages.values():
        stamps.append(parse_ts(msg["timestamp"]))
        if msg["type"] == "user":
            parts = msg["content"] if isinstance(msg["content"], list) else []
            texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
            is_result = any(isinstance(p, dict) and "functionResponse" in p for p in parts)
            if not is_result and any(t.strip() and not t.startswith(("/", "?", "<session_context>", "<hook_context>")) for t in texts):
                session.prompts += 1
        if msg["type"] != "gemini":
            continue
        tokens = msg.get("tokens")
        if tokens:
            session.add_response(
                msg["id"], msg["model"],
                {
                    "input": tokens["input"] - tokens["cached"] + tokens["tool"],
                    "cache_read": tokens["cached"],
                    "output": tokens["output"] + tokens["thoughts"],
                },
                parse_ts(msg["timestamp"]),
                {k: tokens[k] for k in ("input", "cached", "tool", "output", "thoughts")},
            )
        for call in msg.get("toolCalls") or []:
            stamps.append(parse_ts(call["timestamp"]))
            session.tool_calls += 1
            if call["status"] in ("error", "cancelled"):
                session.error_retries += 1
            elif call["name"] in ("write_file", "replace"):
                written.add(call["args"]["file_path"])
    session.stamps = stamps
    session.files_touched = len(written)
    return session


def load_sessions() -> list[Session]:
    (claude,) = sorted((FIXTURES / "claude" / "projects").glob("*/*.jsonl"))
    (codex,) = sorted((FIXTURES / "codex" / "sessions").rglob("rollout-*.jsonl"))
    (gemini,) = sorted((FIXTURES / "gemini" / "tmp").glob("*/chats/session-*.jsonl"))
    return [read_claude(claude), read_codex(codex), read_gemini(gemini)]


# =============================================================================================
# pricing the sessions
# =============================================================================================
class Priced:
    """A session's usage folded onto canonical model ids and priced exactly."""

    def __init__(self, session: Session, models: dict[str, Any], aliases: dict[str, str]) -> None:
        self.s = session
        self.tokens: dict[str, dict[str, int]] = {}  # canonical model -> bucket -> tokens
        for logged, tokens in session.usage.items():
            model = aliases.get(logged, logged)
            into = self.tokens.setdefault(model, dict.fromkeys(BUCKETS, 0))
            for b in BUCKETS:
                into[b] += tokens[b]
        self.dollars = {m: price_of(t, models[m]) for m, t in self.tokens.items()}
        self.by_day: dict[str, Fraction] = {}  # UTC day of the response -> exact dollars: money belongs to when it was spent
        self.by_week: dict[str, Fraction] = {}
        for r in session.responses:
            cost = price_of(r["tokens"], models[aliases.get(r["model"], r["model"])])
            self.by_day[r["when"].date().isoformat()] = self.by_day.get(r["when"].date().isoformat(), 0) + cost
            self.by_week[iso_week(r["when"])] = self.by_week.get(iso_week(r["when"]), 0) + cost
        self.ucents_by_model = {m: ucents(d) for m, d in self.dollars.items()}
        self.total_ucents = sum(self.ucents_by_model.values())

    @property
    def all_tokens(self) -> dict[str, int]:
        return {b: sum(t[b] for t in self.tokens.values()) for b in BUCKETS}

    @property
    def primary_model(self) -> str:
        """The model with the most output tokens (ties: most total tokens, then name)."""
        return max(self.tokens, key=lambda m: (self.tokens[m]["output"], sum(self.tokens[m].values()), m))


def router(p: Priced, models: dict[str, Any]) -> dict[str, Any]:
    """Signals, points, score, tier, recommendation and re-priced cost of one session (docs/ROUTING.md, section 1)."""
    s = p.s
    total_input = sum(p.all_tokens[b] for b in ("input", "cache_read", "cache_write", "cache_write_1h"))
    values: dict[str, Fraction] = {
        "input_tokens": Fraction(total_input),
        "tool_calls": Fraction(s.tool_calls),
        "files_touched": Fraction(s.files_touched),
        "error_retries": Fraction(s.error_retries),
        "active_minutes": Fraction(s.active, 60),
    }
    points = {name: (2 if values[name] >= heavy else 1 if values[name] >= medium else 0) for name, (medium, heavy) in SIGNAL_LIMITS.items()}
    score = sum(points.values())
    tier = "light" if score <= TIER_CUTS[0] else "medium" if score <= TIER_CUTS[1] else "heavy"
    out: dict[str, Any] = {"values": values, "points": points, "score": score, "tier": tier, "actual_ucents": p.total_ucents}
    target = RECOMMEND[tier]
    out["target"] = target
    if target is None:
        out.update(advised=False, note=f"{tier}: keep", recommended_ucents=None)
        return out
    if set(p.tokens) == {target}:
        out.update(advised=False, note=f"already on {target}", recommended_ucents=None)
        return out
    recommended = ucents(price_of(p.all_tokens, models[target]))
    out["recommended_ucents"] = recommended
    if recommended >= p.total_ucents:
        out.update(advised=False, note=f"already cheaper than {target}")
    elif p.total_ucents * MIN_SAVING_RATIO.denominator < recommended * MIN_SAVING_RATIO.numerator:
        out.update(advised=False, note="below the minimum ratio")
    else:
        out.update(advised=True, note="", saved_ucents=p.total_ucents - recommended, ratio=Fraction(p.total_ucents, recommended))
    return out


def run_estimates(models: dict[str, Any], most_used: dict[str, str]) -> dict[str, dict[str, dict[str, Any]]]:
    """`agentfare run` estimate per agent and tier: the default token budget on the tier's model and on the baseline, which is the
    model with the most priced spend in the agent's history (here: the fixtures), else the agent's heavy-tier model."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for agent, by_tier in RUN_MODELS.items():
        baseline_model = most_used.get(agent, by_tier["heavy"])
        for tier, model in by_tier.items():
            budget = RUN_DEFAULT_TOKENS[tier]
            estimate = ucents(price_of(budget, models[model]))
            baseline = ucents(price_of(budget, models[baseline_model]))
            out.setdefault(agent, {})[tier] = {
                "model": model, "estimate_ucents": estimate, "baseline_model": baseline_model, "baseline_ucents": baseline,
                "saving_ucents": baseline - estimate,
                "saving_tenths_percent": (1000 * (baseline - estimate) * 2 + baseline) // (2 * baseline) if baseline > estimate else 0,
            }
    return out


# =============================================================================================
# the whole derivation
# =============================================================================================
def derive() -> dict[str, Any]:
    models, aliases = load_prices()
    priced = [Priced(s, models, aliases) for s in load_sessions()]
    sessions = []
    for p in priced:
        s = p.s
        sessions.append({
            "agent": s.agent, "session_id": s.session_id, "short_id": s.short_id, "project": s.project,
            "started_at": iso(s.started), "day": s.started.date().isoformat(), "week": iso_week(s.started),
            "wall_seconds": int((max(s.stamps) - min(s.stamps)).total_seconds()), "active_seconds": s.active,
            "prompts": s.prompts, "tool_calls": s.tool_calls, "files_touched": s.files_touched, "error_retries": s.error_retries,
            "tokens": p.tokens, "dollars": p.dollars, "ucents_by_model": p.ucents_by_model, "ucents": p.total_ucents,
            "by_day": {d: ucents(v) for d, v in p.by_day.items()}, "by_week": {w: ucents(v) for w, v in p.by_week.items()},
            "primary_model": p.primary_model, "router": router(p, models),
            "turns": len(s.responses), "responses": s.responses,
        })
    totals: dict[str, Any] = {"ucents": sum(x["ucents"] for x in sessions)}
    for key in ("project", "agent"):
        grouped: dict[str, int] = {}
        for x in sessions:
            grouped[x[key]] = grouped.get(x[key], 0) + x["ucents"]
        totals[key] = grouped
    for key in ("day", "week"):  # money belongs to when it was spent: each response counts on the UTC day and week of its own record
        grouped = {}
        for x in sessions:
            for when, cost in x[f"by_{key}"].items():
                grouped[when] = grouped.get(when, 0) + cost
        totals[key] = grouped
    by_model: dict[str, dict[str, Any]] = {}
    for x in sessions:
        for model, tokens in x["tokens"].items():
            row = by_model.setdefault(model, {"ucents": 0, "tokens": dict.fromkeys(BUCKETS, 0), "sessions": 0})
            row["ucents"] += x["ucents_by_model"][model]
            row["sessions"] += 1
            for b in BUCKETS:
                row["tokens"][b] += tokens[b]
    for row in by_model.values():
        row["share_bp"] = int(_round_half_up(Fraction(10_000 * row["ucents"], totals["ucents"])))
        row["blended_ucents_per_mtok"] = int(_round_half_up(Fraction(row["ucents"] * 1_000_000, sum(row["tokens"].values()))))
    totals["model"] = by_model
    savings: dict[str, dict[str, int]] = {}
    for x in sessions:
        row = savings.setdefault(x["week"], {"sessions": 0, "switched": 0, "actual_ucents": 0, "router_ucents": 0})
        row["sessions"] += 1
        row["switched"] += x["router"]["advised"]
        row["actual_ucents"] += x["ucents"]
        row["router_ucents"] += x["ucents"] - x["router"].get("saved_ucents", 0)
    spend: dict[str, dict[str, int]] = {}  # agent -> canonical model -> ucents; the model with the most spend is the `run` baseline
    for x in sessions:
        for model, cost in x["ucents_by_model"].items():
            spend.setdefault(x["agent"], {})[model] = spend.get(x["agent"], {}).get(model, 0) + cost
    most_used = {agent: min(by_model, key=lambda m: (-by_model[m], m)) for agent, by_model in spend.items()}
    used = sorted({m for x in sessions for m in x["tokens"]} | {m for m in RECOMMEND.values() if m}
                  | {m for by_tier in RUN_MODELS.values() for m in by_tier.values()})
    return {
        "prices": {m: {k: models[m][k] for k in ("input", "cached_input", "cache_write", "cache_write_1h", "output", "source", "checked") if k in models[m]} for m in used},
        "aliases": aliases, "sessions": sessions, "totals": totals, "savings": savings, "run": run_estimates(models, most_used),
    }


# =============================================================================================
# output
# =============================================================================================
def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Fraction):
        return str(obj) if obj.denominator != 1 else int(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


BUCKET_LABEL = {
    "input": "input (uncached)", "cache_read": "cache read", "cache_write": "cache write, 5 minutes",
    "cache_write_1h": "cache write, 1 hour", "output": "output (incl. thinking)",
}


def _usd_ucents(u: int, places: int = 4) -> str:
    return usd(Fraction(u, UCENTS_PER_USD), places)


def _round_half_up(x: Fraction, places: int = 0) -> Fraction:
    scaled = x * 10**places
    return Fraction(int(scaled + Fraction(1, 2)) if scaled >= 0 else -int(-scaled + Fraction(1, 2)), 10**places)


def _table(header: list[str], align: str, rows: list[list[str]]) -> list[str]:
    marks = ["---:" if a == "r" else "---" for a in align]
    return ["| " + " | ".join(header) + " |", "|" + "|".join(marks) + "|", *("| " + " | ".join(r) + " |" for r in rows)]


def markdown_blocks(data: dict[str, Any]) -> dict[str, str]:
    """The generated sections of EXPECTED.md, keyed by block name."""
    models, _ = load_prices()
    sessions = {x["agent"]: x for x in data["sessions"]}
    blocks: dict[str, str] = {}

    # ---- prices ------------------------------------------------------------------------------
    rows = []
    for model, p in data["prices"].items():
        rows.append([f"`{model}`", *(usd(rate(models[model], b)) for b in BUCKETS), str(p["checked"])])
    blocks["prices"] = "\n".join(_table(["model", "input", "cache read", "cache write 5m", "cache write 1h", "output", "checked"], "lrrrrrl", rows))

    # ---- the responses behind the token counts -----------------------------------------------------
    header = ["model", "responses", "input", "cache read", "cache write 5m", "cache write 1h", "output"]
    rows = []
    for model in sessions["claude"]["tokens"]:
        logged = [r for r in sessions["claude"]["responses"] if data["aliases"].get(r["model"], r["model"]) == model]
        classes = Counter(tuple(r["tokens"][b] for b in BUCKETS) for r in logged)
        for values, n in sorted(classes.items(), key=lambda kv: (-kv[1], kv[0])):
            rows.append([f"`{model}`", f"{n} x each", *(thousands(v) for v in values)])
        rows.append([f"`{model}`", f"**sum of {len(logged)}**", *(f"**{thousands(sessions['claude']['tokens'][model][b])}**" for b in BUCKETS)])
    blocks["responses-claude"] = "\n".join(_table(header, "lrrrrrr", rows))

    rows = []
    for r in sessions["codex"]["responses"]:
        raw = r["raw"]
        rows.append([f"`{r['label']}`", thousands(raw["input_tokens"]), thousands(raw["cached_input_tokens"]), thousands(raw["output_tokens"]),
                     f"{thousands(raw['input_tokens'])} - {thousands(raw['cached_input_tokens'])} = **{thousands(r['tokens']['input'])}**",
                     thousands(r["tokens"]["cache_read"]), thousands(r["tokens"]["output"])])
    total = sessions["codex"]["tokens"]["gpt-6-astra"]
    rows.append(["**sum**", "", "", "", f"**{thousands(total['input'])}**", f"**{thousands(total['cache_read'])}**", f"**{thousands(total['output'])}**"])
    blocks["responses-codex"] = "\n".join(_table(
        ["response", "input_tokens (with cached)", "cached_input_tokens", "output_tokens (with reasoning)", "uncached input = input - cached", "cache read", "output"], "lrrrrrr", rows))

    rows = []
    for r in sessions["gemini"]["responses"]:
        raw = r["raw"]
        rows.append([f"`{r['label']}`", thousands(raw["input"]), thousands(raw["cached"]), thousands(raw["tool"]), thousands(raw["output"]), thousands(raw["thoughts"]),
                     f"{thousands(raw['input'])} - {thousands(raw['cached'])} + {thousands(raw['tool'])} = **{thousands(r['tokens']['input'])}**",
                     thousands(r["tokens"]["cache_read"]), f"{thousands(raw['output'])} + {thousands(raw['thoughts'])} = **{thousands(r['tokens']['output'])}**"])
    total = sessions["gemini"]["tokens"]["gemini-2.5-pro"]
    rows.append(["**sum**", "", "", "", "", "", f"**{thousands(total['input'])}**", f"**{thousands(total['cache_read'])}**", f"**{thousands(total['output'])}**"])
    blocks["responses-gemini"] = "\n".join(_table(
        ["message id", "input", "cached", "tool", "output", "thoughts", "uncached input = input - cached + tool", "cache read", "output + thoughts"], "lrrrrrrrr", rows))

    # ---- tokens x price = dollars ---------------------------------------------------------------------
    for agent, x in sessions.items():
        lines: list[str] = []
        for model, tokens in x["tokens"].items():
            entry = models[model]
            rows = []
            for b in BUCKETS:
                if not tokens[b] and b in ("cache_write", "cache_write_1h") and agent != "claude":
                    continue  # this agent has no such bucket (billed at 0 tokens)
                price = rate(entry, b)
                dollars = Fraction(tokens[b]) * price / 1_000_000
                rows.append([BUCKET_LABEL[b], thousands(tokens[b]), usd(price), f"{thousands(tokens[b])} x {dec(price)} / 1,000,000", usd(dollars)])
            total = x["dollars"][model]
            rows.append(["**total**", thousands(sum(tokens.values())), "", "", f"**{usd(total)}** = {thousands(x['ucents_by_model'][model])} ucents"])
            lines += [f"`{model}` in the {agent} session `{x['short_id']}`:", ""]
            lines += _table(["bucket", "tokens", "USD per 1M", "tokens x price / 1,000,000", "dollars"], "lrrlr", rows)
            lines.append("")
        blocks[f"cost-{agent}"] = "\n".join(lines).rstrip()

    rows = []
    for x in data["sessions"]:
        for model, tokens in x["tokens"].items():
            rows.append([f"{x['agent']} `{x['short_id']}`", f"`{model}`", *(thousands(tokens[b]) for b in BUCKETS), usd(x["dollars"][model]), thousands(x["ucents_by_model"][model])])
    blocks["cost-summary"] = "\n".join(_table(["session", "model", "input", "cache read", "cache write 5m", "cache write 1h", "output", "dollars", "ucents"], "llrrrrrrr", rows))

    # ---- totals ------------------------------------------------------------------------------------------
    t = data["totals"]
    rows = [["**total**", "all sessions", _usd_ucents(t["ucents"]), thousands(t["ucents"]), str(cents_half_up(t["ucents"]))]]
    for group in ("model", "project", "day", "week", "agent"):
        for key, value in t[group].items():
            u = value["ucents"] if isinstance(value, dict) else value
            rows.append([group, f"`{key}`", _usd_ucents(u), thousands(u), str(cents_half_up(u))])
    blocks["totals"] = "\n".join(_table(["group", "key", "dollars", "ucents", "cents (half up)"], "llrrr", rows))

    # ---- what `agentfare models` and `agentfare sessions` show ------------------------------------------------
    rows = []
    for rank, (model, row) in enumerate(sorted(t["model"].items(), key=lambda kv: -kv[1]["ucents"]), 1):
        tokens = sum(row["tokens"].values())
        entry = models[model]
        rows.append([str(rank), f"`{model}`", thousands(tokens), _usd_ucents(row["ucents"]), str(row["share_bp"]),
                     thousands(row["blended_ucents_per_mtok"]), usd(entry["input"]), usd(entry["output"])])
    blocks["models-table"] = "\n".join(_table(["#", "model", "tokens", "spend", "share (basis points)", "blended ucents per 1M tokens", "list in $/1M", "list out $/1M"], "rlrrrrrr", rows))
    rows = []
    for x in sorted(data["sessions"], key=lambda y: y["started_at"], reverse=True):
        tokens = sum(sum(v.values()) for v in x["tokens"].values())
        rows.append([x["day"], x["agent"], f"`{x['short_id']}`", x["project"], f"`{x['primary_model']}`" + (f" +{len(x['tokens']) - 1}" if len(x["tokens"]) > 1 else ""),
                     f"{x['active_seconds']} s", str(x["prompts"]), str(x["tool_calls"]), str(x["files_touched"]), str(x["error_retries"]), thousands(tokens), _usd_ucents(x["ucents"])])
    blocks["sessions-table"] = "\n".join(_table(["day", "agent", "id", "project", "model", "active", "prompts", "tool calls", "files", "errors", "tokens", "cost"], "lllllrrrrrrr", rows))

    # ---- the router ----------------------------------------------------------------------------------------------
    rows = []
    for x in data["sessions"]:
        r = x["router"]
        cells = []
        for name in SIGNAL_LIMITS:
            v = r["values"][name]
            if name == "active_minutes":
                shown = f"{x['active_seconds']} s = {x['active_seconds'] // 60} min {x['active_seconds'] % 60} s"
            else:
                shown = thousands(int(v))
            cells.append(f"{shown} -> **{r['points'][name]}**")
        rows.append([f"{x['agent']} `{x['short_id']}`", *cells, f"**{r['score']}**", r["tier"]])
    header = ["session", *(f"`{n}` (medium {thousands(m)}, heavy {thousands(h)})" for n, (m, h) in SIGNAL_LIMITS.items()), "score", "tier"]
    blocks["router-signals"] = "\n".join(_table(header, "l" + "r" * 6 + "l", rows))

    rows = []
    for x in data["sessions"]:
        r = x["router"]
        who = f"{x['agent']} `{x['short_id']}`"
        if r["target"] is None or r.get("recommended_ucents") is None:
            rows.append([who, _usd_ucents(r["actual_ucents"]), r["tier"], "n/a", "n/a", f"no ({r['note']})", "$0"])
            continue
        rec = r["recommended_ucents"]
        ratio = Fraction(r["actual_ucents"], rec)
        shown_ratio = f"{thousands(r['actual_ucents'])} / {thousands(rec)} = {dec(_round_half_up(ratio, 4), 4)}"
        rows.append([who, _usd_ucents(r["actual_ucents"]), f"{r['tier']} -> `{r['target']}`", _usd_ucents(rec), shown_ratio,
                     "**yes**" if r["advised"] else f"no ({r['note']})", _usd_ucents(r["actual_ucents"] - rec) if r["advised"] else "$0"])
    blocks["router-advice"] = "\n".join(_table(["session", "actual", "tier -> compared with", "same tokens re-priced", "actual / re-priced", "advised", "saved"], "lrlrrlr", rows))

    rows = []
    for week, w in data["savings"].items():
        saved = w["actual_ucents"] - w["router_ucents"]
        pct = _round_half_up(Fraction(saved * 1000, w["actual_ucents"]))
        rows.append([week, str(w["sessions"]), str(w["switched"]), _usd_ucents(w["actual_ucents"]), _usd_ucents(w["router_ucents"]), _usd_ucents(saved), f"{int(pct) // 10}.{int(pct) % 10}%"])
    blocks["router-week"] = "\n".join(_table(["week", "sessions", "switched", "actual", "router", "saved", "saved %"], "lrrrrrr", rows))

    rows = []
    for agent, tiers in data["run"].items():
        for tier, e in tiers.items():
            pct = f"{e['saving_tenths_percent'] // 10}.{e['saving_tenths_percent'] % 10}%" if e["saving_ucents"] > 0 else "none" if e["saving_ucents"] == 0 else "costs more"
            rows.append([agent, tier, f"`{e['model']}`", _usd_ucents(e["estimate_ucents"]), f"{_usd_ucents(e['baseline_ucents'])} (`{e['baseline_model']}`)", f"{'-' if e['saving_ucents'] < 0 else ''}{_usd_ucents(abs(e['saving_ucents']))} ({pct})"])
    blocks["run-estimates"] = "\n".join(_table(["agent", "tier", "model", "estimate", "baseline (the agent's most used model)", "saving"], "lllrlr", rows))
    return blocks


_BLOCK = re.compile(r"<!-- BEGIN generated: (?P<name>[\w-]+) -->\n(?P<body>.*?)\n<!-- END generated: (?P=name) -->", re.S)


def check_doc(path: Path, blocks: dict[str, str]) -> list[str]:
    """Problems between the generated blocks and the blocks written in EXPECTED.md (empty = the document is current)."""
    found = {m["name"]: m["body"] for m in _BLOCK.finditer(path.read_text(encoding="utf-8"))}
    problems = [f"block {name!r} is missing from {path.name}" for name in blocks if name not in found]
    problems += [f"block {name!r} in {path.name} is not generated by this script" for name in found if name not in blocks]
    problems += [f"block {name!r} differs from what this script derives from the fixtures" for name in blocks if name in found and found[name].strip() != blocks[name].strip()]
    return problems


def report(data: dict[str, Any]) -> str:
    out: list[str] = []
    for x in data["sessions"]:
        r = x["router"]
        out.append(f"{x['agent']:7} {x['short_id']}  {x['project']:16} {x['started_at']}  wall {x['wall_seconds']}s  active {x['active_seconds']}s  "
                   f"turns {x['turns']} prompts {x['prompts']} tools {x['tool_calls']} files {x['files_touched']} errors {x['error_retries']}")
        for model, tokens in x["tokens"].items():
            out.append(f"        {model:28} " + "  ".join(f"{b}={tokens[b]}" for b in BUCKETS) + f"  -> {x['ucents_by_model'][model]} ucents ({usd(x['dollars'][model], 6)})")
        out.append(f"        score {r['score']} ({r['tier']}) points {r['points']} -> target {r['target']} advised={r['advised']} recommended={r.get('recommended_ucents')} note={r['note']!r}")
    t = data["totals"]
    out.append(f"TOTAL {t['ucents']} ucents = {usd(Fraction(t['ucents'], UCENTS_PER_USD), 4)} = {cents_half_up(t['ucents'])} cents")
    for group in ("project", "day", "week", "agent"):
        out.append(f"by {group}: {t[group]}")
    out.append(f"by model: { {m: (v['ucents'], v['share_bp'], v['blended_ucents_per_mtok']) for m, v in t['model'].items()} }")
    out.append(f"weekly savings: {data['savings']}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print every derived number as JSON")
    mode.add_argument("--markdown", action="store_true", help="print the generated tables of EXPECTED.md")
    mode.add_argument("--check", nargs="?", const=str(EXPECTED_MD), metavar="EXPECTED.md", help="exit 1 unless the document holds exactly the generated tables")
    args = ap.parse_args(argv)
    data = derive()
    if args.json:
        print(json.dumps(_jsonable(data), indent=2))
    elif args.markdown:
        for name, body in markdown_blocks(data).items():
            print(f"<!-- BEGIN generated: {name} -->\n{body}\n<!-- END generated: {name} -->\n")
    elif args.check:
        problems = check_doc(Path(args.check), markdown_blocks(data))
        print("\n".join(problems) if problems else f"{args.check}: every generated block matches the fixtures")
        return 1 if problems else 0
    else:
        print(report(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())

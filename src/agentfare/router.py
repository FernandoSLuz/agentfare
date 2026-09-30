"""The router: transparent complexity scoring (``advise``) and pre-flight model picks (``run``).

No ML and no hidden weights: only the thresholds in ``config.router`` decide anything, and every decision
carries a one-line explanation. ``docs/ROUTING.md`` documents every rule and every config key.

``advise``: each past session is scored on five signals (input tokens, tool calls, files touched, error
retries, active minutes). A signal is worth 0 below its ``medium`` threshold, 1 from ``medium`` and 2 from
``heavy``; the total (0-10) picks a tier. ``router.recommend[tier]`` names a model (null = keep yours); the
session's token usage is re-priced on it and a switch is advised only when that is at least
``min_saving_ratio`` times cheaper (integer arithmetic). A session with any usage that has no verified price
is not advised on and stays out of the savings totals (``Advice.actual_complete`` is False).

Money is attributed to when it was spent (``models.slot_of``): a session's usage is priced slot by slot, so the
weekly savings put each slot in the ISO week it belongs to and a session that spans two weeks is in both. The decision
to switch is taken on the whole session (its tokens over its whole life, like its tier) and only the money is cut to the
period asked for, so the weekly savings add up across periods and a slice is named after the model it actually used.

``run``: :func:`pick_for_run` scores the prompt length, the repo size and three keyword families (heavy, light and
medium, matched as accent-free word prefixes), then maps the tier to ``router.run.models[agent]``.
"""

from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .aggregate import period_for
from .config import Config, ConfigError, day_of, iso_week_of, parse_iso
from .cost import usage_cost_ucents
from .db import Store, StoredSession
from .models import Usage, short_id, slot_of
from .money import UCENTS_PER_CENT, format_usd_compact, ratio_str
from .pricing import PriceTable

TIERS = ("light", "medium", "heavy")
SIGNALS = ("input_tokens", "tool_calls", "files_touched", "error_retries", "active_minutes")
MAX_SCORE = 2 * len(SIGNALS)


# ---- configuration --------------------------------------------------------------------
def _number(value: Any, where: str) -> Decimal:
    """A non-negative finite number from the config (int or float as YAML read it), exactly as written."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"{where} must be a number, got {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ConfigError(f"{where} must be a number of at least 0, got {value!r}")
    return number


def _whole(value: Any, where: str) -> int:
    number = _number(value, where)
    if number != number.to_integral_value():
        raise ConfigError(f"{where} must be a whole number, got {value!r}")
    return int(number)


def _mapping(cfg: Config, dotted: str) -> dict[str, Any]:
    node = cfg.get(dotted)
    if not isinstance(node, dict):
        raise ConfigError(f"{dotted} must be a mapping")
    return node


def _limits(node: Any, where: str) -> tuple[Decimal, Decimal]:
    """(medium, heavy) of a ``{medium: .., heavy: ..}`` mapping."""
    if not isinstance(node, dict) or not {"medium", "heavy"} <= node.keys():
        raise ConfigError(f"{where} needs 'medium' and 'heavy' thresholds")
    medium, heavy = _number(node["medium"], f"{where}.medium"), _number(node["heavy"], f"{where}.heavy")
    if medium > heavy:
        raise ConfigError(f"{where}: medium ({node['medium']}) must not be above heavy ({node['heavy']})")
    return medium, heavy


def _signal_limits(cfg: Config) -> dict[str, tuple[Decimal, Decimal]]:
    """The thresholds of the five signals, each checked. A name that is not a signal is ignored here: the config warns about
    it once, with a did-you-mean (``config.unknown_keys``), and a typo must not stop ``advise``, ``report`` or ``run``."""
    table = _mapping(cfg, "router.signals")
    return {name: _limits(table.get(name), f"router.signals.{name}") for name in SIGNALS}


def _tier_cuts(cfg: Config, dotted: str) -> tuple[int, int]:
    """(light_max, medium_max): scores up to the first are light, up to the second medium, above it heavy."""
    node = _mapping(cfg, dotted)
    cuts = [_whole(node.get(key), f"{dotted}.{key}") for key in ("light_max", "medium_max")]
    if cuts[0] > cuts[1]:
        raise ConfigError(f"{dotted}: light_max ({cuts[0]}) must not be above medium_max ({cuts[1]})")
    return cuts[0], cuts[1]


def _tier_of(score: int, cuts: tuple[int, int]) -> str:
    return "light" if score <= cuts[0] else "medium" if score <= cuts[1] else "heavy"


def _tier_models(node: Any, where: str) -> dict[str, str | None]:
    """``{light: model|null, medium: .., heavy: ..}``; a missing tier means null."""
    if not isinstance(node, dict):
        raise ConfigError(f"{where} must map each tier to a model or null")
    if unknown := sorted(set(node) - set(TIERS)):
        raise ConfigError(f"{where}.{unknown[0]}: unknown tier (expected one of: {', '.join(TIERS)})")
    out: dict[str, str | None] = {}
    for tier in TIERS:
        value = node.get(tier)
        if value is not None and not (isinstance(value, str) and value.strip()):
            raise ConfigError(f"{where}.{tier} must be a model id or null, got {value!r}")
        out[tier] = value.strip() if value else None
    return out


def saving_ratio(cfg: Config) -> tuple[int, int]:
    """``router.min_saving_ratio`` as an exact fraction (numerator, denominator): 1.2 is 6/5, never 1.19999..."""
    raw = cfg.get("router.min_saving_ratio")
    ratio = _number(raw, "router.min_saving_ratio")
    if ratio < 1:
        raise ConfigError(f"router.min_saving_ratio must be at least 1, got {raw!r}")
    return ratio.as_integer_ratio()


def min_cost_ucents(cfg: Config) -> int:
    """``router.advise.min_cost_cents`` in micro-cents: below this a session is not listed on its own."""
    cents = _number(cfg.get("router.advise.min_cost_cents"), "router.advise.min_cost_cents")
    return int(cents * UCENTS_PER_CENT)


def _recommendations(cfg: Config) -> dict[str, str | None]:
    return _tier_models(cfg.get("router.recommend"), "router.recommend")


# ---- scoring --------------------------------------------------------------------------
@dataclass(frozen=True)
class Scored:
    """Complexity of one past session."""

    score: int  # 0-10, sum of ``points``
    tier: str  # "light" | "medium" | "heavy"
    points: dict[str, int] = field(default_factory=dict)  # signal name -> 0/1/2
    signals: dict[str, float] = field(default_factory=dict)  # signal name -> measured value

    def explain(self) -> str:
        """One line naming the signals that scored: ``score 7/10 (heavy): input_tokens 3.4M +2, tool_calls 45 +1``."""
        fired = [f"{n} {_show(n, self.signals[n])} +{self.points[n]}" for n in SIGNALS if self.points.get(n)]
        return f"score {self.score}/{MAX_SCORE} ({self.tier}): " + (", ".join(fired) or "no signal at or above medium")


def _show(signal: str, value: float) -> str:
    """Compact measured value: 3416900 -> 3.4M, 2891300000 -> 2.9B, 45 -> 45, 5930 seconds -> 98 (minutes, rounded down)."""
    n = int(value)  # minutes are shown whole and rounded down, so "20" always means "at least 20"
    if signal != "input_tokens" or n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n // 1_000}k"
    if n >= 1_000_000_000:  # the text every other view prints (``2.9B``, rounded half up, whole from 100B), not ``2891.3M``
        tenths = (n * 10 + 500_000_000) // 1_000_000_000
        return f"{tenths // 10}.{tenths % 10}B" if tenths < 1_000 else f"{(n + 500_000_000) // 1_000_000_000}B"
    tenths = n // 100_000
    return f"{tenths // 10}.{tenths % 10}M"


def _measure(s: StoredSession, whole: Usage | None) -> dict[str, Decimal]:
    """The five signals, exact: ``active_minutes`` stays seconds / 60 in decimal so a threshold compares exactly.
    ``whole`` is the session's usage over its whole life when ``s.usage`` was clipped to a period."""
    return {
        "input_tokens": Decimal((s.total_usage if whole is None else whole).total_input),
        "tool_calls": Decimal(s.tool_calls),
        "files_touched": Decimal(s.files_touched),
        "error_retries": Decimal(s.error_retries),
        "active_minutes": Decimal(s.active_seconds) / 60,
    }


def _points(value: Decimal, medium: Decimal, heavy: Decimal) -> int:
    return 2 if value >= heavy else 1 if value >= medium else 0


def _score(
    s: StoredSession, limits: dict[str, tuple[Decimal, Decimal]], cuts: tuple[int, int], whole: Usage | None = None,
) -> Scored:
    exact = _measure(s, whole)
    points = {name: _points(exact[name], *limits[name]) for name in SIGNALS}
    score = sum(points.values())
    signals: dict[str, float] = {name: int(v) for name, v in exact.items()}
    signals["active_minutes"] = s.active_seconds / 60
    return Scored(score=score, tier=_tier_of(score, cuts), points=points, signals=signals)


def score_session(s: StoredSession, cfg: Config) -> Scored:
    return _score(s, _signal_limits(cfg), _tier_cuts(cfg, "router.tiers"))


# ---- advice ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SlotSpend:
    """One 15-minute slot of a session: what it cost and what the same tokens would have cost on the advised model."""

    slot: str  # ``models.slot_of``: 'YYYY-MM-DDTHH:MM:00Z'
    actual_ucents: int  # the priced part when the session has unpriced usage
    recommended_ucents: int  # equal to ``actual_ucents`` when no switch is advised


@dataclass(frozen=True)
class Advice:
    """What the router would have done for one past session (the part of it inside the queried period).

    The decision (tier, which model, whether the saving is big enough) is that of the whole session; the amounts are the
    money of the period, slot by slot, so the advice of a week adds up to the advice of the weeks around it."""

    session: StoredSession
    scored: Scored
    model: str  # the model that wrote the most inside the period (``StoredSession.usage`` is clipped to it), as logged
    actual_ucents: int  # priced part of what you actually spent
    actual_complete: bool  # False when some of the session's usage has no verified price
    recommended_model: str | None  # None = keep the model you used
    recommended_ucents: int | None  # same tokens re-priced on the recommended model, slot by slot
    ratio: float | None  # actual / recommended, only when a switch is advised (display: money.ratio_str)
    note: str = ""  # human reason, e.g. "heavy: keep", "already cheaper than gpt-6.1-sol", "no verified price"
    spend: tuple[SlotSpend, ...] = ()  # per slot, oldest first: the integers the weekly savings are summed from
    other_models: int = 0  # how many more models have usage inside the period: the "+N more models" of the signature line

    @property
    def switch(self) -> bool:
        return self.recommended_model is not None and self.recommended_ucents is not None

    @property
    def saved_ucents(self) -> int:
        """What the switch saves inside the period. It can be below zero for a slice of a session: the decision is taken on the
        whole session, and the part of it inside this period may have been on a model that is cheaper than the advised one."""
        return self.actual_ucents - self.recommended_ucents if self.switch else 0  # type: ignore[operator]


def _plain(number: Decimal) -> str:
    """``1.20`` -> ``1.2``, ``10`` -> ``10``."""
    text = f"{number:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _ratio_floor(numerator: int, denominator: int) -> str:
    """``1.1x`` style ratio rounded down: shown next to the minimum it fell short of, 1.196 must not read as 1.2."""
    tenths = numerator * 10 // denominator
    return f"{tenths // 10}.{tenths % 10}x"


def _actual_by_slot(s: StoredSession, table: PriceTable) -> tuple[dict[str, int], bool]:
    """(cost of each slot of the session's usage, whether every model had a verified price), priced row by row
    (session, model, slot) exactly as ``summary`` does, so the totals of the two commands are the same integers.

    A session with no recorded usage still has its start slot, at zero, so the week it began in counts it."""
    cost: dict[str, int] = {}
    complete = True
    for (slot, model), usage in sorted(s.effective_slots().items()):
        price = table.priced(model)
        complete = complete and price is not None
        cost[slot] = cost.get(slot, 0) + (usage_cost_ucents(usage, price) if price else 0)
    return cost or {slot_of(parse_iso(s.started_at)): 0}, complete


def _main_model(usage: Mapping[str, Usage], fallback: str) -> tuple[str, int]:
    """(the model that wrote the most in ``usage``, how many more it holds), by the rule of ``SessionRecord.primary_model``
    (ties: most tokens, then name). ``usage`` is what is inside the period, so a slice of a session is named after what it
    holds, not after the model the rest of the session used. ``fallback`` when there is none."""
    if not usage:
        return fallback, 0
    return max(usage.items(), key=lambda kv: (kv[1].output, kv[1].total, kv[0]))[0], len(usage) - 1


def _verdict(
    basis: Mapping[str, Usage], tier: str, table: PriceTable, recommend: dict[str, str | None], min_ratio: tuple[int, int],
) -> tuple[str | None, str]:
    """(the model to switch to, or None and why not) for the tokens of the whole session per model, all of them priced.

    The order of the checks is the one of docs/ROUTING.md: no cost, no recommendation for the tier, already on it, no price for
    it, already cheaper, not cheaper enough."""
    priced = {model: price for model in basis if (price := table.priced(model))}
    actual = sum(usage_cost_ucents(usage, priced[model]) for model, usage in basis.items())
    if actual == 0:
        return None, "no cost"
    target = recommend[tier]
    if target is None:
        return None, f"{tier}: keep"
    if {table.resolve(model) for model in basis} == {table.resolve(target)}:
        return None, f"already on {target}"
    price = table.priced(target)
    if price is None:
        return None, f"{target} has no verified price"
    cheaper = usage_cost_ucents(sum(basis.values(), Usage()), price)
    num, den = min_ratio
    if cheaper >= actual:
        return None, f"already cheaper than {target}"
    if actual * den < cheaper * num:
        minimum = _plain(Decimal(num) / Decimal(den))
        return None, f"{target} is only {_ratio_floor(actual, cheaper)} cheaper (minimum {minimum}x)"
    return target, ""


def _advise(
    s: StoredSession,
    scored: Scored,
    table: PriceTable,
    recommend: dict[str, str | None],
    min_ratio: tuple[int, int],
    whole: Mapping[str, Usage] | None = None,
) -> Advice:
    by_slot, complete = _actual_by_slot(s, table)
    actual = sum(by_slot.values())
    model, others = _main_model(s.usage, s.primary_model)

    def spend(recommended: dict[str, int]) -> tuple[SlotSpend, ...]:
        return tuple(SlotSpend(slot, cost, recommended[slot]) for slot, cost in sorted(by_slot.items()))

    def keep(note: str) -> Advice:
        return Advice(s, scored, model, actual, complete, None, None, None, note, spend(by_slot), others)

    if not complete:
        return keep("no verified price")
    # Whether to switch is decided on the whole session, as its tier is, so that two views of it cannot disagree: the part inside
    # a week is not advised differently from the same part inside the whole history. A session that has usage with no verified
    # price outside the period is decided on what is inside it.
    basis = whole if whole and all(table.priced(m) for m in whole) else s.usage
    target, note = _verdict(basis, scored.tier, table, recommend, min_ratio)
    if target is None:
        return keep(note)
    price = table.priced(target)
    assert price is not None  # _verdict checked it
    tokens: dict[str, Usage] = {}  # the slot's tokens of every model together: one price, one rounding per slot
    for (slot, _), usage in s.effective_slots().items():
        tokens[slot] = tokens.get(slot, Usage()) + usage
    cheaper_by_slot = {slot: usage_cost_ucents(usage, price) for slot, usage in tokens.items()}
    cheaper = sum(cheaper_by_slot.values())
    ratio = actual / cheaper if cheaper else None
    return Advice(s, scored, model, actual, True, target, cheaper, ratio, "", spend(cheaper_by_slot), others)


def advise_sessions(
    sessions: Iterable[StoredSession], cfg: Config, table: PriceTable, whole_usage: Mapping[str, dict[str, Usage]] | None = None,
) -> list[Advice]:
    """One :class:`Advice` per session, for what each spent inside the period it was queried for (``StoredSession.usage`` is
    clipped to it).

    The five signals describe the whole session, so that its tier does not depend on the period asked for: the counters
    are whole-session already, and ``whole_usage`` (session key -> tokens per model, ``Store.whole_usage``) gives the
    tokens of a clipped session. The same tokens decide whether and where to switch (the recommended model and the
    ``min_saving_ratio`` gate), and only then are the actual and the recommended cost split by slot and clipped to the period.
    A session missing from ``whole_usage`` is decided on its own ``usage``."""
    limits, cuts = _signal_limits(cfg), _tier_cuts(cfg, "router.tiers")
    recommend, min_ratio = _recommendations(cfg), saving_ratio(cfg)
    whole = whole_usage or {}
    return [
        _advise(
            s, _score(s, limits, cuts, sum(whole[s.key].values(), Usage()) if s.key in whole else None), table, recommend, min_ratio,
            whole.get(s.key),
        )
        for s in sessions
    ]


def _spent(a: Advice) -> str:
    extra = a.other_models
    more = f" (+{extra} more model{'s' if extra > 1 else ''})" if extra > 0 else ""
    if a.actual_complete:
        amount = format_usd_compact(a.actual_ucents)
    else:
        amount = f"{format_usd_compact(a.actual_ucents)} (priced part only)" if a.actual_ucents else "an unknown amount"
    return f"you spent {amount} on {a.model}{more}" if a.model else f"you spent {amount} (no token usage recorded)"


def format_advice_line(a: Advice, encoding: str = "utf-8") -> str:
    """e.g. ``session 3f2a91c4: you spent $4.20 on <model> → gpt-6.1-sol would likely have handled this for ~$0.84 (5.0x cheaper)``.

    The arrow is ``->`` when ``encoding`` (that of the console it is printed on) cannot write ``→``."""
    who = f"session {short_id(a.session.session_id)}: {_spent(a)}"
    recommended = a.recommended_ucents
    if a.switch and recommended is not None:
        if recommended >= a.actual_ucents:  # the decision is about the whole session: this part of it was cheap already
            cheaper = "not cheaper for the part inside this period"
        else:
            cheaper = f"{ratio_str(a.actual_ucents, recommended)} cheaper" if recommended else "free"
        return f"{who} {_arrow(encoding)} {a.recommended_model} would likely have handled this for ~{format_usd_compact(recommended)} ({cheaper})"
    if not a.actual_complete:
        return f"{who}: no advice, {a.note}"
    return f"{who}: no switch advised, {a.note}"


def _arrow(encoding: str) -> str:
    try:
        "→".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "->"
    return "→"


# ---- weekly savings -------------------------------------------------------------------
@dataclass(frozen=True)
class WeekSavings:
    week: str  # ISO week label "2026-W40"
    sessions: int  # sessions counted in the totals below (fully priced ones)
    switched: int  # sessions where a cheaper model was advised
    actual_ucents: int
    recommended_ucents: int  # actual spend with every advised switch applied
    skipped: int = 0  # sessions left out because part of their usage has no verified price
    # (session key, fully priced, switch advised) of each session with usage in the week: a total counts one that spans
    # weeks once. Only ``weekly_savings`` fills it.
    members: frozenset[tuple[str, bool, bool]] = field(default=frozenset(), compare=False, repr=False)

    @property
    def saved_ucents(self) -> int:
        return self.actual_ucents - self.recommended_ucents


def _week_row(week: str, members: frozenset[tuple[str, bool, bool]], actual: int, recommended: int) -> WeekSavings:
    priced = [m for m in members if m[1]]
    return WeekSavings(
        week, len(priced), sum(m[2] for m in priced), actual, recommended, skipped=len(members) - len(priced), members=members
    )


def weekly_savings(advice: Iterable[Advice], cfg: Config) -> list[WeekSavings]:
    """One row per ISO week (configured timezone), oldest first, summed slot by slot: the week of a slot is the week of the
    moment the money was spent. A session that spans two weeks is counted in both. Sessions with unverified prices are only
    counted as skipped."""
    tz = cfg.tz
    money: dict[str, list[int]] = {}  # week -> [actual, recommended]
    members: dict[str, set[tuple[str, bool, bool]]] = {}
    for a in advice:
        who = (a.session.key, a.actual_complete, a.switch)
        for spent in a.spend:
            week = iso_week_of(spent.slot, tz)
            members.setdefault(week, set()).add(who)
            if a.actual_complete:
                row = money.setdefault(week, [0, 0])
                row[0] += spent.actual_ucents
                row[1] += spent.recommended_ucents
    return [_week_row(week, frozenset(members[week]), *money.get(week, (0, 0))) for week in sorted(members)]


def total_savings(weeks: Iterable[WeekSavings]) -> WeekSavings:
    rows = list(weeks)
    return _week_row(
        "total", frozenset().union(*(w.members for w in rows)),
        sum(w.actual_ucents for w in rows), sum(w.recommended_ucents for w in rows),
    )


def savings_split(advice: Iterable[Advice]) -> tuple[int, int]:
    """(saved by sessions a person prompted, saved by sessions with 0 prompts: sub-agent and background threads), in micro-cents.

    Both come from the per-slot integers the weekly table is summed from, so they add up to its Saved total."""
    saved = [(a.session.prompts > 0, a.saved_ucents) for a in advice]
    return sum(u for prompted, u in saved if prompted), sum(u for prompted, u in saved if not prompted)


# ---- live routing (`agentfare run`) -------------------------------------------------------
# Directories that hold dependencies, build output, tool caches or VCS data: they say nothing about the size of the task.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "build", "dist", "target", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".nuxt", ".svelte-kit", ".turbo", ".gradle", ".terraform", ".pnpm-store",
})
BASELINE_DAYS = 30  # how far back from the newest session in the database "the model you run now" looks
MIN_HISTORY = 5  # past sessions of the same agent and tier needed before their median replaces run.default_tokens
EXTRA_KEYWORDS = 2  # the heavy and light families add a point for each distinct keyword after the first, up to this many
MEDIUM_POINTS = 1  # the medium family adds this once, however many of its keywords are found


@dataclass(frozen=True)
class RunPick:
    agent: str  # "claude" | "codex" | "gemini" | custom agent name
    tier: str  # "light" | "medium" | "heavy"
    score: int
    reasons: list[str]  # one line per heuristic that fired, e.g. "prompt is 2100 chars (>= 1500): +2"
    model: str | None  # model to run; None when no verified/configured model exists for this agent
    est_ucents: int | None  # estimated cost of the task on ``model``; None when unpriced
    baseline_model: str | None  # what you would run without the router: your most used model for the agent lately, else its heavy-tier model
    baseline_ucents: int | None
    forced: bool = False  # True when --force-model overrode the router
    warnings: list[str] = field(default_factory=list)  # one entry per problem, e.g. "no verified price for X: cost estimate unavailable"
    history_sessions: int = 0  # past sessions of this agent and tier the token budget is the median of; 0: run.default_tokens
    baseline_most_used: bool = False  # ``baseline_model`` is the model with the most spend in your history, not a configured one
    baseline_basis: str | None = None  # "recent" (the last ``BASELINE_DAYS`` days of history), "all_time", "configured", None: no baseline
    baseline_since: str | None = None  # the first local day of that window, for "recent" (``YYYY-MM-DD``)
    same_as_baseline: bool = False  # the model to run is the baseline: there is no saving to show
    alias_for: str | None = None  # the model ``model`` stands for when it is one of ``router.run.model_aliases`` (``opus``)

    @property
    def priced_model(self) -> str | None:
        """The model the estimate is priced on: what ``model`` is an alias of, else ``model`` itself."""
        return self.alias_for or self.model


def count_files(root: Path, cap: int) -> int:
    """Files under ``root`` (skipping dependency, build and VCS directories), stopping once ``cap`` is reached.

    Only directory listings are read, never file contents; symlinks are not followed and unreadable
    directories count as empty.
    """
    count, pending = 0, [root]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in _SKIP_DIRS:
                            pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        count += 1
                        if count >= cap:
                            return count
        except OSError:
            continue
    return count


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding="ROUND_CEILING"))


def _size_rule(what: str, value: int, limits: tuple[Decimal, Decimal]) -> tuple[int, str] | None:
    """Points and reason for a measured size against a (medium, heavy) pair; None when it is below ``medium``."""
    medium, heavy = limits
    if value >= heavy:
        return 2, f"{what} (>= {heavy:f}): +2"
    if value >= medium:
        return 1, f"{what} (>= {medium:f}): +1"
    return None


def _fold(text: str) -> str:
    """``text`` in lower case and without accents (``Migração`` -> ``migracao``), so a stem matches a word however it is accented."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def _keywords(node: Any, where: str) -> list[str]:
    """The configured words in config order, each once however it is spelled (``Refactor`` after ``refactor`` adds nothing)."""
    if not isinstance(node, list) or not all(isinstance(k, str) and k.strip() for k in node):
        raise ConfigError(f"{where} must be a list of words")
    words: dict[str, str] = {}
    for word in node:
        words.setdefault(_fold(word.strip()), word.strip())
    return list(words.values())


def _found(words: list[str], prompt: str) -> list[str]:
    """The configured words that start a word of ``prompt``, in any case and with or without accents, in config order:
    ``refactor`` is found in ``refactoring`` but ``format`` is not found in ``information``. A word that starts where an
    earlier one of the list already matched is not counted again: ``migrat`` and ``migr`` are one keyword in ``migrate``."""
    text = _fold(prompt)
    claimed: set[int] = set()
    found: list[str] = []
    for word in words:
        stem = r"\s+".join(re.escape(part) for part in _fold(word).split())
        starts = {m.start() for m in re.finditer(rf"(?<!\w){stem}", text)}
        if starts - claimed:
            found.append(word)
            claimed |= starts
    return found


def _family_points(family: str, step: int, matches: int) -> int:
    """Points of a family with ``matches`` distinct keywords found. Heavy and light: ``step`` for the first and 1 for each
    further one, at most ``EXTRA_KEYWORDS`` more. Medium: ``MEDIUM_POINTS``, once."""
    return MEDIUM_POINTS if family == "medium" else step + min(matches - 1, EXTRA_KEYWORDS)


def _run_score(cfg: Config, prompt: str, repo_path: Path | None) -> tuple[int, list[str]]:
    points: list[tuple[int, str]] = []
    prompt_limits = _limits(cfg.get("router.run.prompt_chars"), "router.run.prompt_chars")
    if hit := _size_rule(f"prompt is {len(prompt)} chars", len(prompt), prompt_limits):
        points.append(hit)
    if repo_path is not None and repo_path.is_dir():
        limits = _limits(cfg.get("router.run.repo_files"), "router.run.repo_files")
        files = count_files(repo_path, _ceil(limits[1]))
        if hit := _size_rule(f"repo has {files}{'+' if files >= limits[1] else ''} files", files, limits):
            points.append(hit)
    step = _whole(cfg.get("router.run.keyword_points"), "router.run.keyword_points")
    for family, sign in (("heavy", 1), ("light", -1), ("medium", 1)):
        words = _found(_keywords(cfg.get(f"router.run.{family}_keywords"), f"router.run.{family}_keywords"), prompt)
        if words and step:
            worth = sign * _family_points(family, step, len(words))
            points.append((worth, f"{family} keyword{'s' if len(words) > 1 else ''} {', '.join(words)}: {worth:+d}"))
    score = max(0, sum(p for p, _ in points))
    return score, [reason for _, reason in points] or ["no rule fired: score 0"]


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    return (ordered[(len(ordered) - 1) // 2] + ordered[len(ordered) // 2]) // 2


def _history_usage(sessions: list[StoredSession], cfg: Config, tier: str) -> tuple[Usage, int] | None:
    """(median usage per token bucket, sessions it comes from) of the past ``sessions`` of this tier that a person drove
    (prompts >= 1); None with fewer than ``MIN_HISTORY`` of them."""
    limits, cuts = _signal_limits(cfg), _tier_cuts(cfg, "router.tiers")
    past = [s.total_usage for s in sessions if s.prompts >= 1 and _score(s, limits, cuts).tier == tier]
    if len(past) < MIN_HISTORY:
        return None
    return Usage(**{f.name: _median([getattr(u, f.name) for u in past]) for f in dataclasses.fields(Usage)}), len(past)


def _most_used_model(sessions: list[StoredSession], table: PriceTable, since: str | None = None) -> str | None:
    """The model with the most spend in ``sessions`` (its canonical id; ties: the name); models without a verified price
    are not compared. With ``since`` (a slot timestamp) only the slots from then on count. None when nothing was priced."""
    spend: dict[str, int] = {}
    for s in sessions:
        used = s.usage.items() if since is None else [(m, u) for (slot, m), u in s.effective_slots().items() if slot >= since]
        for model, usage in used:
            if price := table.priced(model):
                spend[price.model] = spend.get(price.model, 0) + usage_cost_ucents(usage, price)
    return min(spend, key=lambda m: (-spend[m], m)) if spend else None


def _baseline(
    cfg: Config, table: PriceTable, everyone: list[StoredSession], agent: str, heavy: str | None,
) -> tuple[str | None, str | None, str | None]:
    """(baseline model, how it was chosen, first day of the window) of ``run``: what you would have run without the router.

    The model with the most spend for ``agent`` in the last ``BASELINE_DAYS`` days of history, which is what you run now.
    The days are counted back from the newest spend in the database (all agents), never from the wall clock, so the same
    database always gives the same answer. Without priced spend in that window it is the model with the most spend
    overall, and without any, the agent's configured heavy-tier model."""
    mine = [s for s in everyone if s.agent == agent]
    newest = max((slot for s in everyone for slot, _ in s.effective_slots()), default=None)
    if newest is not None:
        since_day = (date.fromisoformat(day_of(newest, cfg.tz)) - timedelta(days=BASELINE_DAYS)).isoformat()
        start = period_for(cfg, since=since_day).since
        if recent := _most_used_model(mine, table, start):
            return recent, "recent", since_day
    if overall := _most_used_model(mine, table):
        return overall, "all_time", None
    return heavy, ("configured" if heavy else None), None


def _alias_of(cfg: Config, model: str | None) -> str | None:
    """What ``model`` stands for when it is one of ``router.run.model_aliases`` (case-insensitive), else None."""
    aliases: dict[str, str] = {}
    for alias, target in _mapping(cfg, "router.run.model_aliases").items():
        if not (isinstance(target, str) and target.strip()):
            raise ConfigError(f"router.run.model_aliases.{alias} must be a model id, got {target!r}")
        aliases[str(alias).strip().casefold()] = target.strip()
    return aliases.get(model.strip().casefold()) if model else None


def _default_usage(cfg: Config, tier: str) -> Usage:
    node = _mapping(cfg, "router.run.default_tokens").get(tier)
    names = {f.name for f in dataclasses.fields(Usage)}
    if not isinstance(node, dict) or not set(node) <= names:
        raise ConfigError(f"router.run.default_tokens.{tier} must map token buckets ({', '.join(sorted(names))}) to counts")
    return Usage(**{k: _whole(v, f"router.run.default_tokens.{tier}.{k}") for k, v in node.items()})


def _price(table: PriceTable, usage: Usage, model: str | None) -> int | None:
    price = table.priced(model) if model else None
    return usage_cost_ucents(usage, price) if price else None


def pick_for_run(
    cfg: Config,
    table: PriceTable,
    store: Store | None,
    *,
    agent: str,
    prompt: str,
    repo_path: Path | None = None,
    force_model: str | None = None,
) -> RunPick:
    """Choose a model for a task from quick heuristics (prompt length, repo size, keywords).

    Reads only the prompt string and counts files under ``repo_path`` (never file contents).
    The cost estimate uses the median usage of past sessions of the same agent and tier when
    ``store`` has enough history, else ``config.router.run.default_tokens``. The baseline is the model with the most
    spend for that agent in the last ``BASELINE_DAYS`` days of history (counted back from the newest session in the
    database), else the one with the most spend overall, else the heavy-tier model of the agent. A model that is one
    of ``router.run.model_aliases`` is priced as the model it stands for.

    ``reasons`` has one line per rule that fired, or ``["no rule fired: score 0"]`` when none did.
    """
    score, reasons = _run_score(cfg, prompt, repo_path)
    tier = _tier_of(score, _tier_cuts(cfg, "router.run.tiers"))
    configured = _mapping(cfg, "router.run.models").get(agent)
    by_tier = _tier_models(configured, f"router.run.models.{agent}") if configured is not None else None
    model = force_model or (by_tier[tier] if by_tier else None)
    alias_for = _alias_of(cfg, model)
    everyone = store.sessions() if store is not None else []
    past = [s for s in everyone if s.agent == agent]
    baseline, basis, since = _baseline(cfg, table, everyone, agent, by_tier["heavy"] if by_tier else None)
    warnings: list[str] = []
    if by_tier is None and not force_model:
        warnings.append(f"no model configured for agent {agent!r} (router.run.models): run it with its own default")
    elif model is None:
        warnings.append(f"no {tier}-tier model configured for {agent} (router.run.models): run it with its own default")
    usage, history = _history_usage(past, cfg, tier) or (_default_usage(cfg, tier), 0)
    est = _price(table, usage, alias_for or model)
    if model and est is None:
        warnings.append(f"no verified price for {alias_for or model}: cost estimate unavailable")
    base_cost = _price(table, usage, baseline)
    if baseline and base_cost is None:
        warnings.append(f"no verified price for {baseline}: baseline cost unavailable")
    picked = alias_for or model
    same = bool(baseline and picked and (table.resolve(baseline) or baseline) == (table.resolve(picked) or picked))
    return RunPick(
        agent=agent, tier=tier, score=score, reasons=reasons, model=model, est_ucents=est,
        baseline_model=baseline, baseline_ucents=base_cost, forced=bool(force_model),
        warnings=warnings, history_sessions=history, baseline_most_used=basis in ("recent", "all_time"), alias_for=alias_for,
        baseline_basis=basis, baseline_since=since, same_as_baseline=same,
    )

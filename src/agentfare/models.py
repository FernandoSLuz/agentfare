"""Shared data model. Metadata and token counts only: never prompt or file contents."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts for one model within one session, normalised across agents.

    Every parser must map its agent's native counters onto these buckets so that
    pricing is a plain ``tokens x price`` product with no double counting:

    * ``input``          uncached input tokens (billed at the input rate)
    * ``cache_read``     input tokens served from the prompt cache (cached-input rate)
    * ``cache_write``    input tokens written to the prompt cache with the default TTL,
                         i.e. 5-minute writes (cache-write rate, falls back to the input
                         rate when the price table has none)
    * ``output``         output tokens INCLUDING reasoning/thinking tokens
    * ``cache_write_1h`` input tokens written to the prompt cache with the 1-hour TTL
                         (Anthropic bills them at 2x the input price against 1.25x for
                         5-minute writes); last, so the first four stay positional

    ``cache_write + cache_write_1h`` is every token written to the cache.
    """

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    cache_write_1h: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input + other.input,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
            self.output + other.output,
            self.cache_write_1h + other.cache_write_1h,
        )

    @property
    def total_input(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.cache_write_1h

    @property
    def total(self) -> int:
        return self.total_input + self.output

    def is_empty(self) -> bool:
        return self.total == 0


AGENTS = ("claude", "codex", "gemini")

SLOT_MINUTES = 15  # every real UTC offset is a multiple of 15 minutes, so a slot maps to exactly one local day


def slot_of(ts: datetime) -> str:
    """The UTC 15-minute slot containing ``ts``, as ``YYYY-MM-DDTHH:MM:00Z`` (a naive ``ts`` is read as UTC)."""
    utc = (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc))
    slot = utc.replace(minute=utc.minute - utc.minute % SLOT_MINUTES, second=0, microsecond=0, tzinfo=None)
    return slot.isoformat(timespec="minutes") + ":00Z"  # isoformat, not strftime: ``%Y`` is not zero-padded for year 1 before Python 3.13


def short_id(session_id: str) -> str:
    """The last 8 characters of the id: the first 8 of a Codex (UUIDv7) id are a timestamp and repeat for weeks."""
    return session_id[-8:]


@dataclass(slots=True)
class SessionRecord:
    """One agent session, reduced to metadata. Produced by the parsers."""

    agent: str  # one of AGENTS
    session_id: str  # agent-native id, unique within an agent
    source_path: str  # log file the record came from (for the audit trail only)
    project: str  # repo / project name (basename), never a full path
    started_at: str  # ISO-8601 UTC, "YYYY-MM-DDTHH:MM:SSZ"
    ended_at: str  # ISO-8601 UTC, same format
    wall_seconds: int  # ended_at - started_at
    active_seconds: int  # wall time with idle gaps (> idle_gap) removed
    usage: dict[str, Usage] = field(default_factory=dict)  # model id -> tokens (whole session)
    # (slot, model) -> tokens, when the response was made: the money of a long session lands on the day it was spent.
    # Invariant: per model, the slots add up to ``usage``. Empty = everything counts at the session's start.
    slots: dict[tuple[str, str], Usage] = field(default_factory=dict)
    tool_calls: int = 0  # number of tool invocations
    files_touched: int = 0  # DISTINCT files written/edited (count only, no names)
    error_retries: int = 0  # tool calls that came back as an error (not ones the user or a permission rule refused)
    turns: int = 0  # model responses (API calls) after de-duplication
    prompts: int = 0  # user prompts (count only, no text)

    @property
    def key(self) -> str:
        return f"{self.agent}:{self.session_id}"

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for u in self.usage.values():
            total = total + u
        return total

    def effective_slots(self) -> dict[tuple[str, str], Usage]:
        """``slots``, or all of ``usage`` at the slot of ``started_at`` when the parser recorded no timing."""
        if self.slots:
            return self.slots
        from .config import parse_iso  # local import: config imports nothing from here, but keep models dependency-free

        start = slot_of(parse_iso(self.started_at))
        return {(start, model): u for model, u in self.usage.items()}

    @property
    def primary_model(self) -> str:
        """Model that produced the most output tokens (ties: most total tokens, then name)."""
        if not self.usage:
            return ""
        return max(self.usage.items(), key=lambda kv: (kv[1].output, kv[1].total, kv[0]))[0]


MAX_NAME_CHARS = 200  # a stored project name, session id or model id: real ones are a few dozen characters


def cap_name(text: str) -> str:
    """``text`` of at most ``MAX_NAME_CHARS`` characters: a longer name keeps its start and ends in ``...`` and eight hex digits
    of its hash, so two long names that share a start stay two names. A name within the limit is returned as it is."""
    if len(text) <= MAX_NAME_CHARS:
        return text
    mark = "..." + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]
    return text[: MAX_NAME_CHARS - len(mark)] + mark


def bounded(rec: SessionRecord) -> SessionRecord:
    """``rec`` with its project, session id and model ids capped (``cap_name``), the same for every agent; ``rec`` itself
    when none of them is too long. The names come from the logs, so one of a hundred thousand characters is possible: nothing
    that stores or draws it should ever see it whole."""
    names = {*rec.usage, *(model for _, model in rec.slots)}
    if len(rec.project) <= MAX_NAME_CHARS and len(rec.session_id) <= MAX_NAME_CHARS and all(len(n) <= MAX_NAME_CHARS for n in names):
        return rec
    usage: dict[str, Usage] = {}
    for model, u in rec.usage.items():
        name = cap_name(model)
        usage[name] = usage[name] + u if name in usage else u
    slots: dict[tuple[str, str], Usage] = {}
    for (slot, model), u in rec.slots.items():
        key = (slot, cap_name(model))
        slots[key] = slots[key] + u if key in slots else u
    return replace(rec, project=cap_name(rec.project), session_id=cap_name(rec.session_id), usage=usage, slots=slots)

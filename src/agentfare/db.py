"""SQLite storage (stdlib only). Stores metadata and token counts, never prompt or file contents."""

from __future__ import annotations

import functools
import os
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

from .config import fmt_iso, now_utc, parse_iso
from .models import SLOT_MINUTES, SessionRecord, Usage, bounded, slot_of
from .safeio import make_private_dirs, touch_private

# 2: session_usage.cache_write_1h (1-hour cache writes are priced apart from 5-minute ones)
# 3: session_usage.slot (one row per model and UTC 15-minute slot: money belongs to when it was spent)
SCHEMA_VERSION = 3


class StoreError(ValueError):
    """The database file cannot be opened, created, upgraded, read or written."""


def _newer_message(path: Path) -> str:
    return f"the database {path} was written by a newer agentfare; upgrade agentfare or delete the file"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    key            TEXT PRIMARY KEY,           -- "<agent>:<session_id>"
    agent          TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    project        TEXT NOT NULL,
    started_at     TEXT NOT NULL,              -- ISO-8601 UTC
    ended_at       TEXT NOT NULL,
    wall_seconds   INTEGER NOT NULL,
    active_seconds INTEGER NOT NULL,
    tool_calls     INTEGER NOT NULL,
    files_touched  INTEGER NOT NULL,           -- distinct count, no names
    error_retries  INTEGER NOT NULL,
    turns          INTEGER NOT NULL,
    prompts        INTEGER NOT NULL,           -- count, no text
    primary_model  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    ingested_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

CREATE TABLE IF NOT EXISTS ingested_files (
    path      TEXT PRIMARY KEY,
    agent     TEXT NOT NULL,
    signature TEXT NOT NULL,
    sessions  INTEGER NOT NULL
);
"""

# Kept apart from ``_SCHEMA`` because the upgrade from an older file rebuilds exactly this table.
_USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS session_usage (
    key            TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE,
    model          TEXT NOT NULL,
    slot           TEXT NOT NULL,                -- UTC 15-minute slot 'YYYY-MM-DDTHH:MM:00Z' (models.slot_of)
    input          INTEGER NOT NULL,
    cache_read     INTEGER NOT NULL,
    cache_write    INTEGER NOT NULL,             -- 5-minute (default TTL) writes
    output         INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL DEFAULT 0,   -- 1-hour writes
    PRIMARY KEY (key, model, slot)
);
"""
# Created after the upgrade, because an old table has no slot column yet.
_USAGE_SLOT_INDEX = "CREATE INDEX IF NOT EXISTS idx_usage_slot ON session_usage(slot)"

# ``slot_of(started_at)`` in SQL, for the rows of an old database: started_at is stored as 'YYYY-MM-DDTHH:MM:SSZ'.
_START_SLOT_SQL = (
    "substr(s.started_at, 1, 14)"
    f" || printf('%02d', CAST(substr(s.started_at, 15, 2) AS INTEGER) / {SLOT_MINUTES} * {SLOT_MINUTES}) || ':00Z'"
)


@dataclass(frozen=True, slots=True)
class StoredSession:
    key: str
    agent: str
    session_id: str
    project: str
    started_at: str
    ended_at: str
    wall_seconds: int
    active_seconds: int
    tool_calls: int
    files_touched: int
    error_retries: int
    turns: int
    prompts: int
    primary_model: str
    source_path: str
    usage: dict[str, Usage]  # model -> tokens; clipped to the queried period when Store.sessions got since/until
    # (slot, model) -> tokens, clipped the same way: when the money was spent (see models.SessionRecord.slots).
    # Empty means "all of ``usage`` counts at ``started_at``" (hand-built sessions, old rows).
    slots: dict[tuple[str, str], Usage] = field(default_factory=dict)
    clipped: bool = False  # True when part of the session's usage lies outside the queried period and was left out

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for u in self.usage.values():
            total = total + u
        return total

    def effective_slots(self) -> dict[tuple[str, str], Usage]:
        """``slots``, or all of ``usage`` at the slot of ``started_at`` when none were recorded."""
        if self.slots:
            return self.slots
        start = slot_of(parse_iso(self.started_at))
        return {(start, model): u for model, u in self.usage.items()}


_Method = TypeVar("_Method", bound=Callable[..., Any])

_KEYS_PER_QUERY = 500  # well under SQLite's limit on bound parameters, which is as low as 999 in old builds


def _slot_test(alias: str, lo: str | None, hi: str | None, *, outside: bool = False) -> tuple[str, list[str]]:
    """SQL and arguments testing ``alias.slot`` against ``[lo, hi)``: inside it, or (``outside``) not inside it.

    Empty when both bounds are open. A slot is inside when its own start lies in the range."""
    parts = [(f"{alias}.slot < ?", lo), (f"{alias}.slot >= ?", hi)] if outside else [
        (f"{alias}.slot >= ?", lo), (f"{alias}.slot < ?", hi)
    ]
    used = [(sql, bound) for sql, bound in parts if bound]
    return (" OR " if outside else " AND ").join(sql for sql, _ in used), [bound for _, bound in used]


@functools.lru_cache(maxsize=4096)
def _canonical_slot(slot: str) -> str:
    return slot_of(parse_iso(slot))


def _canonical_slots(slots: dict[tuple[str, str], Usage]) -> dict[tuple[str, str], Usage]:
    """``slots`` with every slot in the one spelling ``slot_of`` makes, rows that land in the same slot merged."""
    merged: dict[tuple[str, str], Usage] = {}
    for (slot, model), u in slots.items():
        key = (_canonical_slot(slot), model)
        merged[key] = merged[key] + u if key in merged else u
    return merged


def _damage_is_a_store_error(method: _Method) -> _Method:
    """Make a ``Store`` method raise ``StoreError`` when SQLite finds the file is not a database or is malformed.

    That is a plain ``sqlite3.DatabaseError``. Its subclasses stay as they are: an ``OperationalError`` (no such table)
    or a ``ProgrammingError`` (closed connection) is a bug in the statement or in the caller, not a damaged file.
    """

    @functools.wraps(method)
    def guarded(self: "Store", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except sqlite3.DatabaseError as exc:
            if type(exc) is not sqlite3.DatabaseError:
                raise
            raise self._unusable(exc) from exc

    return guarded  # type: ignore[return-value]


def _casefold(value: object) -> object:
    """Caseless comparison key: accents compare equal whether composed or decomposed (macOS returns the latter)."""
    if not isinstance(value, str):
        return value
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", value).casefold())


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        try:
            if str(path) != ":memory:":
                # What we create is private: a new directory is 0700 and a new database 0600. The file is made here, before
                # SQLite opens it, because the -journal and -wal files it creates later take the mode of the database.
                # A directory or a file that is already there keeps the mode it has.
                make_private_dirs(self.path.parent)
                touch_private(self.path)
            self.conn = sqlite3.connect(str(path))
        except (OSError, sqlite3.Error) as exc:
            raise self._unusable(exc) from exc
        try:
            self.conn.row_factory = sqlite3.Row
            # SQLite's own lower() only folds ASCII, so "AÇÃO" would not match "ação".
            self.conn.create_function("casefold", 1, _casefold, deterministic=True)
            self.conn.execute("PRAGMA foreign_keys = ON")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:  # a newer release wrote this file: its layout is not ours to touch or to stamp over
                self.conn.close()
                raise StoreError(_newer_message(self.path))
            self.conn.executescript(_SCHEMA + _USAGE_TABLE)
            self._migrate()
            self.conn.execute(_USAGE_SLOT_INDEX)
            if version < SCHEMA_VERSION:  # stamped only when it is lower, so opening a current file takes no write lock
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()
        except sqlite3.Error as exc:
            self.conn.close()
            raise self._unusable(exc) from exc

    @classmethod
    def open_read_only(cls, path: Path | str) -> "Store":
        """The database exactly as it is, for commands that only read (``run``): nothing is created, upgraded or written
        (``Store(path)`` stamps the schema version on every open and takes a write lock), and a writer that holds the lock
        costs a second at most. Raises ``StoreError`` when the file is not a database of this schema version."""
        store = cls.__new__(cls)
        store.path = Path(path)
        try:
            store.conn = sqlite3.connect(f"file:{quote(str(store.path.absolute()))}?mode=ro", uri=True, timeout=1)
        except sqlite3.Error as exc:
            raise store._unusable(exc) from exc
        try:
            store.conn.row_factory = sqlite3.Row
            store.conn.create_function("casefold", 1, _casefold, deterministic=True)
            version = store.conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as exc:
            store.conn.close()
            raise store._unusable(exc) from exc
        if version != SCHEMA_VERSION:
            store.conn.close()
            if version > SCHEMA_VERSION:
                raise StoreError(_newer_message(store.path))
            raise StoreError(f"the database {store.path} is in an older format (run agentfare ingest to upgrade it)")
        return store

    def _unusable(self, exc: Exception) -> StoreError:
        hint = ""
        if type(exc) is sqlite3.DatabaseError:  # not a database / malformed, as opposed to a path or permission problem
            hint = " (delete the file to start over: it is rebuilt from the agent logs that still exist)"
        elif isinstance(exc, OSError):  # a folder that cannot be made or written (a read-only home): deleting nothing helps
            hint = " (set AGENTFARE_HOME or --home to a folder you can write to; agentfare doctor checks the setup)"
        return StoreError(f"cannot use the database {self.path}: {exc}{hint}")

    def _usage_columns(self) -> set[str]:
        return {row["name"] for row in self.conn.execute("PRAGMA table_info(session_usage)")}

    def _migrate(self) -> None:
        """Upgrade a database written by an older agentfare in place (v1: no 1-hour column, v2: no slot column)."""
        if "slot" in self._usage_columns():
            return
        with self.conn:
            # Another command may open the same old file at the same time: whoever gets the write lock first upgrades
            # it, the others look again and find nothing to do.
            self.conn.execute("BEGIN IMMEDIATE")
            columns = self._usage_columns()
            if "slot" in columns:
                return
            # Forgetting the parsed files makes the next ingest re-read the logs: it splits v1's cache writes by TTL
            # and, for v1 and v2 alike, puts every response at the time it was made instead of at the session's start.
            self.conn.execute("DELETE FROM ingested_files")
            self.conn.execute("ALTER TABLE session_usage RENAME TO session_usage_old")
            self.conn.execute(_USAGE_TABLE)
            # Until that re-read, an old row counts at the slot its session started in. v1 wrote every cache write as a
            # 5-minute one, which is what the old column already holds.
            one_hour = "u.cache_write_1h" if "cache_write_1h" in columns else "0"
            self.conn.execute(
                "INSERT INTO session_usage (key, model, slot, input, cache_read, cache_write, output, cache_write_1h)"
                f" SELECT u.key, u.model, {_START_SLOT_SQL}, u.input, u.cache_read, u.cache_write, u.output, {one_hour}"
                " FROM session_usage_old u JOIN sessions s ON s.key = u.key"
            )
            self.conn.execute("DROP TABLE session_usage_old")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- writes --------------------------------------------------------------------
    @_damage_is_a_store_error
    def upsert_session(self, rec: SessionRecord) -> None:
        rec = bounded(rec)  # a name from the logs is stored at most MAX_NAME_CHARS long, whatever agent it came from
        # ``sessions()`` compares these as text, so they are stored in one spelling whatever the parser produced.
        started_at, ended_at = fmt_iso(parse_iso(rec.started_at)), fmt_iso(parse_iso(rec.ended_at))
        usage = _canonical_slots(rec.effective_slots())  # read before the old rows go: a bad slot leaves them alone
        with self.conn:
            self.conn.execute("DELETE FROM sessions WHERE key = ?", (rec.key,))  # cascades to usage
            self.conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.key, rec.agent, rec.session_id, rec.project, started_at, ended_at,
                    rec.wall_seconds, rec.active_seconds, rec.tool_calls, rec.files_touched,
                    rec.error_retries, rec.turns, rec.prompts, rec.primary_model, rec.source_path,
                    fmt_iso(now_utc()),
                ),
            )
            self.conn.executemany(
                "INSERT INTO session_usage (key, model, slot, input, cache_read, cache_write, output, cache_write_1h)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [
                    (rec.key, m, slot, u.input, u.cache_read, u.cache_write, u.output, u.cache_write_1h)
                    for (slot, m), u in usage.items()
                ],
            )

    @_damage_is_a_store_error
    def file_signature(self, path: str) -> str | None:
        row = self.conn.execute("SELECT signature FROM ingested_files WHERE path = ?", (path,)).fetchone()
        return row["signature"] if row else None

    @_damage_is_a_store_error
    def mark_file(self, path: str, agent: str, signature: str, sessions: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO ingested_files VALUES (?,?,?,?)", (path, agent, signature, sessions)
            )

    @_damage_is_a_store_error
    def session_copy(self, key: str) -> tuple[str, int] | None:
        """(``source_path``, total tokens) of the stored session ``key``, or None: what ingest compares a second copy of the
        same session (an archived and a live log, two roots) with."""
        row = self.conn.execute(
            "SELECT s.source_path, COALESCE((SELECT SUM(u.input + u.cache_read + u.cache_write + u.output + u.cache_write_1h)"
            " FROM session_usage u WHERE u.key = s.key), 0) FROM sessions s WHERE s.key = ?", (key,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    @_damage_is_a_store_error
    def clear(self) -> None:
        with self.conn:
            self.conn.executescript("DELETE FROM session_usage; DELETE FROM sessions; DELETE FROM ingested_files;")

    # ---- reads ---------------------------------------------------------------------
    @_damage_is_a_store_error
    def sessions(
        self,
        *,
        agent: str | None = None,
        project: str | None = None,
        since: str | None = None,  # ISO-8601: the slots from this instant on count
        until: str | None = None,  # ISO-8601: the slots before this instant count
    ) -> list[StoredSession]:
        """Sessions with usage in ``[since, until)`` by slot time, ordered by start then key.

        With a range each session's ``usage`` and ``slots`` hold only what was spent inside it and ``clipped`` says
        whether something was left out; every other field stays that of the whole session. Without one the whole
        session comes back, including a session that has no usage at all.
        """
        filters, args = [], []
        if agent:
            filters.append("s.agent = ?"); args.append(agent)
        if project:
            filters.append("casefold(s.project) = casefold(?)"); args.append(project)
        # Slots are 'YYYY-MM-DDTHH:MM:00Z' and compared as text, so bounds must be in that exact form.
        lo, hi = (fmt_iso(parse_iso(v)) if v else None for v in (since, until))
        inside, inside_args = _slot_test("u", lo, hi)
        outside, outside_args = _slot_test("o", lo, hi, outside=True)
        session_filters, clipped_sql, clipped_args = list(filters), "0", []
        if inside:
            session_filters.append(f"EXISTS (SELECT 1 FROM session_usage u WHERE u.key = s.key AND {inside})")
            clipped_sql = f"EXISTS (SELECT 1 FROM session_usage o WHERE o.key = s.key AND ({outside}))"
            clipped_args = outside_args
        rows = self.conn.execute(
            f"SELECT s.*, {clipped_sql} AS clipped FROM sessions s"
            + (" WHERE " + " AND ".join(session_filters) if session_filters else "") + " ORDER BY s.started_at, s.key",
            clipped_args + args + inside_args,
        ).fetchall()
        usage: dict[str, dict[str, Usage]] = {}
        slots: dict[str, dict[tuple[str, str], Usage]] = {}
        # Only the usage rows of the sessions above: an agent or project filter must not read the others' rows.
        conditions = filters + ([inside] if inside else [])
        usage_rows = self.conn.execute(
            "SELECT u.key, u.model, u.slot, u.input, u.cache_read, u.cache_write, u.output, u.cache_write_1h"
            " FROM session_usage u" + (" JOIN sessions s ON s.key = u.key" if filters else "")
            + (" WHERE " + " AND ".join(conditions) if conditions else ""),
            args + inside_args,
        )
        for key, model, slot, *tokens in usage_rows:
            u = Usage(*tokens)
            slots.setdefault(key, {})[(slot, model)] = u
            per_model = usage.setdefault(key, {})
            per_model[model] = per_model[model] + u if model in per_model else u
        return [
            StoredSession(
                key=r["key"], agent=r["agent"], session_id=r["session_id"], project=r["project"],
                started_at=r["started_at"], ended_at=r["ended_at"], wall_seconds=r["wall_seconds"],
                active_seconds=r["active_seconds"], tool_calls=r["tool_calls"], files_touched=r["files_touched"],
                error_retries=r["error_retries"], turns=r["turns"], prompts=r["prompts"],
                primary_model=r["primary_model"], source_path=r["source_path"], usage=usage.get(r["key"], {}),
                slots=slots.get(r["key"], {}), clipped=bool(r["clipped"]),
            )
            for r in rows
        ]

    @_damage_is_a_store_error
    def whole_usage(self, keys: Iterable[str]) -> dict[str, dict[str, Usage]]:
        """Each session's tokens per model over its whole life, whatever range ``sessions`` was clipped to.

        Every key asked for is in the answer; a session that has no usage (or does not exist) maps to ``{}``."""
        wanted = list(dict.fromkeys(keys))
        whole: dict[str, dict[str, Usage]] = {key: {} for key in wanted}
        for start in range(0, len(wanted), _KEYS_PER_QUERY):
            chunk = wanted[start : start + _KEYS_PER_QUERY]
            for key, model, *tokens in self.conn.execute(
                "SELECT key, model, SUM(input), SUM(cache_read), SUM(cache_write), SUM(output), SUM(cache_write_1h)"
                f" FROM session_usage WHERE key IN ({','.join('?' * len(chunk))}) GROUP BY key, model",
                chunk,
            ):
                whole[key][model] = Usage(*tokens)
        return whole

    @_damage_is_a_store_error
    def reread_pending(self) -> bool:
        """True while an ingest would still change what the database says: some stored session comes from a log file that no
        ingest has read since the upgrade of the database (``ingested_files`` is emptied by it), so its money still sits at
        the slot the session started in, and that file is still there to be read. A log the agent has since deleted cannot be
        re-read, so it does not count."""
        unread = self.conn.execute(
            "SELECT DISTINCT s.source_path FROM sessions s WHERE NOT EXISTS (SELECT 1 FROM ingested_files f WHERE f.path = s.source_path)"
        )
        return any(os.path.isfile(path) for (path,) in unread)

    @_damage_is_a_store_error
    def usage_range(self, *, agent: str | None = None, project: str | None = None) -> tuple[str, str] | None:
        """The first and last slot (UTC ``YYYY-MM-DDTHH:MM:00Z``) with usage of the sessions these filters keep, or None."""
        filters, args = [], []
        if agent:
            filters.append("s.agent = ?"); args.append(agent)
        if project:
            filters.append("casefold(s.project) = casefold(?)"); args.append(project)
        row = self.conn.execute(
            "SELECT MIN(u.slot), MAX(u.slot) FROM session_usage u JOIN sessions s ON s.key = u.key"
            + (" WHERE " + " AND ".join(filters) if filters else ""), args,
        ).fetchone()
        return (row[0], row[1]) if row and row[0] else None

    def iter_sessions(self, **kw: object) -> Iterator[StoredSession]:
        yield from self.sessions(**kw)  # type: ignore[arg-type]

    @_damage_is_a_store_error
    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

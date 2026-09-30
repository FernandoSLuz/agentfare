"""Ingestion: walk each agent's log directories, parse changed files, upsert sessions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import parsers
from .config import Config
from .db import Store, StoreError
from .models import AGENTS, SessionRecord, bounded
from .textsafe import clean_text
from .parsers import LogParser, get_parser


@dataclass
class IngestReport:
    files_seen: int = 0
    files_parsed: int = 0
    files_unchanged: int = 0
    sessions_read: int = 0  # session records stored from the files parsed: a session resumed in several files counts in each
    sessions: int = 0  # distinct sessions among them
    missing_roots: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    by_agent: dict[str, int] = field(default_factory=dict)  # distinct sessions stored per agent
    files_failed: int = 0  # files that could not be read or parsed: each one is also in ``errors``
    duplicates: int = 0  # distinct sessions found in more than one log file (see ``_keeps``)


def duplicates_warning(count: int) -> str:
    """The one line that says a session was found in several log files (an archived and a live copy, two roots)."""
    what = "session appears" if count == 1 else "sessions appear"
    return f"{count} {what} in more than one log file; the copy with the most tokens was kept"


def _describe(exc: BaseException) -> str:
    """``PermissionError: Permission denied``: what went wrong, without the path an OS error repeats (the line has it already)."""
    if isinstance(exc, OSError) and exc.strerror:
        return f"{type(exc).__name__}: {exc.strerror}"
    return f"{type(exc).__name__}: {exc}"


def _discover(parser: LogParser, agent: str, root: Path, report: IngestReport) -> list[Path]:
    """Log files under ``root`` in path order (so results never depend on directory listing order).

    A failure while walking is reported and keeps the files found before it, like a bad file would.
    """
    found: list[Path] = []
    try:
        found.extend(parser.discover(root))
    except Exception as exc:
        report.errors.append(clean_text(f"{agent}: {root}: {_describe(exc)}"))
    return sorted(found)


def _root_exists(agent: str, root: Path, report: IngestReport) -> bool:
    """False for a root that is absent (reported as missing) or cannot even be looked at (reported as an error).

    Before Python 3.14 ``Path.exists`` raises PermissionError for a root inside a directory without search permission.
    """
    try:
        present = root.exists()
    except OSError as exc:
        report.errors.append(clean_text(f"{agent}: {root}: {_describe(exc)}"))
        return False
    if not present:
        report.missing_roots.append(clean_text(f"{agent}: {root}"))
    return present


def _billed(rec: SessionRecord) -> SessionRecord | None:
    """``rec`` without zero-token models and slots (Claude's ``<synthetic>``, say), or None when nothing was billed.

    Keeping them would make every command warn about a model that cost nothing.
    """
    usage = {model: u for model, u in rec.usage.items() if not u.is_empty()}
    if not usage:
        return None
    slots = {key: u for key, u in rec.slots.items() if not u.is_empty()}
    if len(usage) == len(rec.usage) and len(slots) == len(rec.slots):
        return rec
    return replace(rec, usage=usage, slots=slots)


def _keeps(store: Store, rec: SessionRecord, copies: set[str]) -> bool:
    """Whether ``rec`` replaces what the database holds for its session.

    The same session can sit in two log files that no parser merges (an archived log and its live copy, two roots, a resumed
    thread): the copy with the most tokens wins, and on a tie the later file (by path), so the outcome never depends on which
    file was parsed last. A newer read of the very file the session came from always replaces it. ``copies`` collects the
    sessions found in more than one file."""
    held = store.session_copy(rec.key)
    if held is None or held[0] == rec.source_path:
        return True
    copies.add(rec.key)
    return (rec.total_usage.total, rec.source_path) >= (held[1], held[0])


def ingest(
    cfg: Config,
    store: Store,
    agents: tuple[str, ...] | list[str] | None = None,
    *,
    full: bool = False,
    on_file: Callable[[str, str], None] | None = None,
) -> IngestReport:
    """Incrementally ingest logs. ``full=True`` re-parses every file even when its signature is unchanged.

    A file's stored signature is the parser's plus what else shapes its records (the idle gap and the parsers'
    version), so changing either re-reads the logs without ``full``."""
    report = IngestReport()
    seen: set[str] = set()  # keys of the sessions stored in this run
    copies: set[str] = set()  # keys of the sessions found in more than one file
    idle_gap = cfg.idle_gap_seconds  # read once, outside the per-file guard: a bad setting is a config error, not one error per file
    for agent in agents or AGENTS:
        parser = get_parser(agent)
        for root in cfg.sources(agent):
            if not _root_exists(agent, root, report):
                continue
            for path in _discover(parser, agent, root, report):
                report.files_seen += 1
                key = str(path)
                try:
                    sig = f"{parser.signature(path)}|gap={idle_gap}|p={parsers.PARSERS_VERSION}"
                    if not full and store.file_signature(key) == sig:
                        report.files_unchanged += 1
                        continue
                    if on_file:
                        on_file(agent, key)
                    records = parser.parse(path, idle_gap_seconds=idle_gap)
                    stored = 0
                    for rec in records:
                        billed = _billed(bounded(rec))  # names are capped before the key is used to compare copies
                        if billed is None:  # nothing was billed: not interesting for cost tracking
                            continue
                        keep = _keeps(store, billed, copies)
                        report.duplicates = len(copies)
                        if not keep:  # another file holds more of this session: this copy is not stored
                            continue
                        store.upsert_session(billed)
                        stored += 1
                        report.sessions_read += 1
                        if billed.key not in seen:
                            seen.add(billed.key)
                            report.sessions += 1
                            report.by_agent[agent] = report.by_agent.get(agent, 0) + 1
                    store.mark_file(key, agent, sig, stored)
                    report.files_parsed += 1
                except StoreError:  # the database is unusable, not this file: every other file would fail the same way
                    raise
                except Exception as exc:  # one bad file must never abort the run
                    report.files_failed += 1
                    report.errors.append(clean_text(f"{agent}: {path}: {_describe(exc)}"))
    return report

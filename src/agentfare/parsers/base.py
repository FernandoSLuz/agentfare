"""Parser contract. One module per agent, each exposing a module-level ``PARSER``.

Rules every parser must follow (they are what the tests check):

* Read log files line by line, tolerate malformed/truncated lines and unknown record
  types (agents add new ones every release), never crash on a bad file.
* NEVER copy prompt text, assistant text, tool inputs/outputs or file contents into a
  SessionRecord. Only counts, ids, model names, timestamps and a project *name*.
* Normalise tokens into the five ``Usage`` buckets (see ``models.Usage``) and attribute
  them to the model that actually served each response (a session can use several).
* Fill ``SessionRecord.slots``: each response's tokens under the UTC 15-minute slot
  (``models.slot_of``) of its own timestamp, so money follows when it was spent. Per model the
  slots add up to ``usage``; a response with no usable timestamp counts at the session's first
  one. Bump ``parsers.PARSERS_VERSION`` whenever a parser's output for the same log changes.
* De-duplicate responses that the agent logs more than once (streaming chunks, replays
  after resume/compaction, forked threads) so tokens are never double counted.
* ``files_touched`` = distinct files written/edited, ``tool_calls`` = tool invocations,
  ``error_retries`` = tool calls whose result was an error, ``prompts`` = real user
  prompts (not tool results, not synthetic/injected messages).
* Timestamps are ISO-8601 UTC ``YYYY-MM-DDTHH:MM:SSZ``; ``active_seconds`` sums the gaps
  between consecutive events that are <= ``idle_gap_seconds``.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..models import SessionRecord


class LogParser(Protocol):
    agent: str

    def discover(self, root: Path) -> Iterator[Path]:
        """Yield the log files under ``root`` that each describe one session (recursive)."""

    def signature(self, path: Path) -> str:
        """Cheap change detector for ``path`` (and anything merged into it). Default: size + mtime."""

    def parse(self, path: Path, *, idle_gap_seconds: int = 1800) -> list[SessionRecord]:
        """Parse one discovered file into zero or more session records (empty/no-usage sessions may be omitted)."""


def is_regular_file(path: Path | str) -> bool:
    """True for a regular file, or a symlink that leads to one. A FIFO, socket, device or directory (or a link to one) is
    not a log: opening a named pipe blocks forever, so a scan must never try. ``os.stat`` follows links; a dangling link is False."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def regular_files(paths: Iterable[Path]) -> list[Path]:
    """``paths`` without anything that is not a regular file (see ``is_regular_file``), order kept."""
    return [p for p in paths if is_regular_file(p)]


def stat_signature(path: Path) -> str:
    st = os.stat(path)
    return f"{st.st_size}:{st.st_mtime_ns}"


def project_name(path_like: str | None, fallback: str = "unknown") -> str:
    """Basename of a working directory, handling POSIX and Windows separators. Never returns a full path."""
    if not path_like:
        return fallback
    cleaned = path_like.strip().rstrip("/\\")
    name = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    return name or fallback


def active_seconds(timestamps: list[datetime], idle_gap_seconds: int) -> int:
    """Sum of gaps between consecutive (sorted) timestamps that do not exceed the idle threshold."""
    ts = sorted(timestamps)
    total = 0
    for a, b in zip(ts, ts[1:]):
        gap = (b - a).total_seconds()
        if 0 <= gap <= idle_gap_seconds:
            total += int(gap)
    return total

"""Claude Code session logs -> ``SessionRecord``.

On-disk layout under ``~/.claude/projects``::

    <encoded-cwd>/<session-uuid>.jsonl                      the session, one JSON object per line
    <encoded-cwd>/<session-uuid>/subagents/**/agent-*.jsonl sub-agent and workflow transcripts
    <encoded-cwd>/<session-uuid>/{tool-results,workflows}/  spill files and scripts, not sessions

A session is its main file plus every sub-agent transcript below it: those requests are real
spend, so they are merged into the parent. Rules, each checked against real logs:

* **Streamed responses.** One API response is written as several ``assistant`` records that
  share ``message.id`` (one per content block). In older logs their ``usage`` is identical; in
  newer ones the early records carry the ``message_start`` snapshot (``output_tokens`` of 2-7)
  and only the last record has the final counts. Measured on 79,064 multi-record ids: 26,210
  identical, 52,854 growing, and the last record held every bucket's maximum in all but one. So
  each bucket is reduced with ``max`` over the records of one id: that equals the last record,
  does not depend on line order, and a stream cut short keeps the counts it had.
* **Iterations.** ``usage.iterations`` lists the sampling calls behind a response and adds up to
  the top-level counters in 141,028 of 141,032 records. Of the other four, three are one request
  (965k cache-read tokens) whose top-level counters are all zero and whose numbers exist only in
  ``iterations``; the fourth is a ``fallback_message`` retry. So each bucket takes the larger of
  the top-level counter and the sum of the iterations served by the message's model: an entry
  served by another model (the retry's first attempt) is not added to this one.
* **Cache writes by TTL.** ``usage.cache_creation`` splits the writes into ``ephemeral_5m_input_tokens``
  (billed at 1.25x the input price) and ``ephemeral_1h_input_tokens`` (2x). The flat
  ``cache_creation_input_tokens`` is the total, and the split only says how much of it is 1-hour: it
  agrees with the total in 365,311 of 365,315 real records (top level and iterations). The other four
  name writes that the total does not have: three are the zero-counter responses above, whose iterations
  carry the same split, and one is the ``fallback_message`` retry, whose split includes the first attempt
  of another model. So the 1-hour part is capped at the total, everything else is 5-minute (also when
  the record has no ``cache_creation`` at all, as in older logs), and no token is lost or invented.
  Writes are one bucket for the ``max`` reductions above, keeping the largest 1-hour part.
* **Skipped.** ``<synthetic>`` messages (interrupts, API errors) are not billed. Records whose
  ``sessionId`` or ``session_id`` names another session are history replayed into a forked or
  resumed file: the original file counts them, so counting them again would double the spend.
  The second field matters: one fork rewrote ``sessionId`` on 273 copied records and kept the
  parent in ``session_id``; without it that session read 3.4x the tokens Claude reports for it.
* **Tool calls** are distinct ``tool_use`` blocks by id (streaming repeats the block).
  ``files_touched`` counts distinct paths given to Edit/Write/MultiEdit/NotebookEdit.
  ``error_retries`` counts distinct ``tool_result`` blocks with ``is_error``, except those of a
  record that has ``toolDenialKind`` (``user-rejected``, ``permission-rule``, ``automode-blocked``,
  ``automode-unavailable``): a call the user or a permission rule refused is not a model failure. All
  63 denials in the author's logs are one-result records with ``is_error`` true and that field set.
* **Prompts** are ``user`` records of the main file that a human typed: not tool-result
  carriers, not ``isMeta`` / ``isCompactSummary`` / sidechain records, not ``origin.kind`` other
  than ``human`` (task notifications, peers, channels), not ``promptSource: system``, and not
  text that only carries an injected wrapper (slash-command echoes, ``<system-reminder>``,
  task notifications, ``<paseo-system>``, interrupt markers). Slash commands are therefore not
  prompts: their expansion is an ``isMeta`` record.
* **Slots.** The money of a response belongs to when it was spent, so each de-duplicated response is
  attributed to the 15-minute UTC slot of its last record (the one carrying the final usage; the latest
  timestamp of the id, so line order and copies of the file cannot change it). A response none of whose
  records has a usable timestamp counts at the session's first one. Sub-agent responses use their own
  timestamps. Per model the slots add up to ``usage`` by construction: both are sums of the same responses.
* **Time.** Events are the ``user`` and ``assistant`` records (sub-agents included), cut to
  whole seconds so summed gaps do not lose the sub-second part of every streamed record.
* **Project** is the basename of the first ``cwd`` in the session, with Claude Code worktrees
  (``<repo>/.claude/worktrees/<name>``) and ``<repo>-wt/<branch>`` / ``<repo>-wt-<branch>``
  folders folded back onto the repo.

**Copies of one session.** A session moved to another project folder is copied there, and the
copies then grow apart. Ingestion keeps one row per session id, so every copy parses to the
union of all copies (and of every sub-agent transcript): which copy is parsed last cannot matter.

``cost-state`` records (Claude's own per-model token totals) are deliberately ignored: they exist
in some files only and serve to cross-check this parser, not as an input. On the 44 (session, model)
pairs of the author's machine that have both, 16 agree exactly, 27 within 1% and 38 within 10%,
and the token sums agree within 0.4% (output tokens alone within 0.01%). The parser reads LOW on
short sessions (up to a third), because Claude bills calls it never writes to the transcript (away
summaries, title generation, prompt suggestions, compaction, and 21M haiku tokens that no
transcript mentions). A copy of another session's history that names no other session in any field
cannot be told from new work inside one file; none exists on the author's machine.

Priced end to end (1-hour writes at 2x the input price), the 17 of those pairs whose four buckets each
agree within 1% cost what Claude's own ``costUSD`` says: 16 to the micro-cent and 1 within 0.1%; with
every write priced as a 5-minute one none does (11.7% too low). Across all 71 cost-state entries,
``costUSD`` also falls between the all-5-minute and the all-1-hour price of Claude's own token counts,
except for the 8 that used web search: Claude adds $0.01 per request, which AgentFare does not price.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import fmt_iso, parse_iso
from ..models import SessionRecord, Usage, slot_of
from .base import active_seconds, is_regular_file, project_name, regular_files, stat_signature

_NOT_SESSIONS = frozenset({"subagents", "tool-results", "workflows"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_SYNTHETIC = "<synthetic>"
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = ("file_path", "notebook_path", "path")  # Write occasionally logs `path` instead of `file_path`
_INJECTED = re.compile(
    r"\s*(?:<(?:command-name|command-message|command-args|local-command-stdout|local-command-stderr"
    r"|local-command-caveat|system-reminder|task-notification|bash-input|bash-stdout|bash-stderr"
    r"|user-prompt-submit-hook|ide_opened_file|ide_selection|paseo-system)(?=[\s>/])|\[Request interrupted)"
)


class _Parser:
    agent = "claude"

    def discover(self, root: Path) -> Iterator[Path]:
        """Top-level session files under ``root`` (a projects dir or one project dir)."""
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _NOT_SESSIONS)
            for name in sorted(filenames):
                if name.endswith(".jsonl") and is_regular_file(Path(dirpath, name)):  # not a FIFO: opening one would hang
                    yield Path(dirpath, name)

    def signature(self, path: Path) -> str:
        """Main file size+mtime plus a digest of every sub-agent file's, so a change in any re-parses."""
        digest = hashlib.blake2b(digest_size=8)
        subagents = _subagent_files(path)
        for sub in subagents:
            st = sub.stat()
            digest.update(f"{sub.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
        return f"{stat_signature(path)}+{len(subagents)}:{digest.hexdigest()}"

    def parse(self, path: Path, *, idle_gap_seconds: int = 1800) -> list[SessionRecord]:
        session = _Session(path.stem)
        mains = [path, *_other_copies(path)]
        for file in [*mains, *_subagent_files(path)]:
            for rec in _records(file):
                session.feed(rec, main=file in mains)
        record = session.finish(str(path), idle_gap_seconds)
        return [record] if record else []


def _project_dirs(path: Path) -> list[Path]:
    try:
        return sorted(d for d in path.parent.parent.iterdir() if d.is_dir())
    except OSError:
        return [path.parent]


def _other_copies(path: Path) -> list[Path]:
    """The same session's main file in the other project folders.

    A session moved between folders is copied, and the two copies then grow apart: ingestion
    stores one row per session id, so each copy must produce the union or the row would depend on
    which copy happened to be parsed last.
    """
    candidates = (project / path.name for project in _project_dirs(path))
    return [other for other in candidates if other.is_file() and not other.samefile(path)]


def _subagent_files(path: Path) -> list[Path]:
    """Sub-agent transcripts of the session, from every project folder that holds some.

    A session moved between folders (a worktree, a relocated cwd) is copied there and keeps its
    older sub-agent transcripts behind, so one folder alone misses spend (74% of the largest
    session in the real logs this was built on). Identical copies are harmless because every
    counter below is keyed by message, tool or record id.
    """
    files: list[Path] = []
    for project in _project_dirs(path):
        base = project / path.stem / "subagents"
        if base.is_dir():
            files.extend(regular_files(sorted(base.rglob("*.jsonl"))))
    return files


def _records(path: Path) -> Iterator[dict[str, Any]]:
    """JSON objects of a JSONL file; blank, malformed, truncated or non-object lines are skipped."""
    with path.open("rb") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


class _Session:
    """Accumulates one session (main file plus sub-agent files) record by record."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.checks_owner = bool(_UUID.fullmatch(session_id))
        self.stamps: list[datetime] = []
        self.responses: dict[str, tuple[str, Usage, datetime | None]] = {}  # message.id -> (model, usage, last timestamp)
        self.tool_ids: set[str] = set()
        self.error_ids: set[str] = set()
        self.files: set[str] = set()
        self.prompt_ids: set[str] = set()
        self.cwd: str | None = None
        self.anonymous = 0

    def feed(self, rec: dict[str, Any], *, main: bool) -> None:
        kind = rec.get("type")
        if kind not in ("user", "assistant") or self._is_replay(rec):
            return
        stamp = _timestamp(rec.get("timestamp"))
        if stamp:
            self.stamps.append(stamp)
        if not self.cwd and isinstance(rec.get("cwd"), str):
            self.cwd = rec["cwd"]
        message = rec.get("message")
        if not isinstance(message, dict):
            return
        if kind == "assistant":
            self._assistant(rec, message, stamp)
        else:
            self._user(rec, message, main)

    def _is_replay(self, rec: dict[str, Any]) -> bool:
        """History of another session copied into this file (only judged for uuid-named files).

        A fork that rewrites ``sessionId`` on the copied records still names their session in ``session_id``.
        """
        if not self.checks_owner:
            return False
        owners = (rec.get("sessionId"), rec.get("session_id"))
        return any(isinstance(owner, str) and owner != self.session_id for owner in owners)

    def _unique(self, value: Any) -> str:
        """``value`` when it is a usable id, else a token no other record can share."""
        if isinstance(value, str) and value:
            return value
        self.anonymous += 1
        return f"anonymous-{self.anonymous}"

    def _assistant(self, rec: dict[str, Any], message: dict[str, Any], stamp: datetime | None) -> None:
        model = message.get("model")
        if model == _SYNTHETIC:
            return
        for block in _blocks(message.get("content")):
            if block.get("type") == "tool_use":
                self._tool_use(block)
        usage = _usage(message.get("usage"), model)
        if usage is None or not isinstance(model, str):
            return
        key = self._unique(message.get("id") or rec.get("requestId"))
        seen = self.responses.get(key)
        if seen is not None:
            usage = _peak(seen[1], usage)
            stamp = max((t for t in (seen[2], stamp) if t), default=None)
        self.responses[key] = (model, usage, stamp)

    def _tool_use(self, block: dict[str, Any]) -> None:
        tool_id = block.get("id")
        if not isinstance(tool_id, str):
            return
        self.tool_ids.add(tool_id)
        name = block.get("name")
        if isinstance(name, str) and name in _WRITE_TOOLS:
            target = _written_path(block.get("input"))
            if target:
                self.files.add(target)

    def _user(self, rec: dict[str, Any], message: dict[str, Any], main: bool) -> None:
        content = message.get("content")
        carries_result = False
        refused = isinstance(rec.get("toolDenialKind"), str) and bool(rec["toolDenialKind"])
        for block in _blocks(content):
            if block.get("type") == "tool_result":
                carries_result = True
                if block.get("is_error") is True and not refused:
                    self.error_ids.add(self._unique(block.get("tool_use_id")))
        if main and not carries_result and _is_prompt(rec, content):
            self.prompt_ids.add(self._unique(rec.get("uuid")))

    def finish(self, source_path: str, idle_gap_seconds: int) -> SessionRecord | None:
        if not self.stamps:
            return None
        started, ended = min(self.stamps), max(self.stamps)
        usage: dict[str, Usage] = {}
        slots: dict[tuple[str, str], Usage] = {}
        for model, used, when in self.responses.values():
            if not used.is_empty():
                usage[model] = usage.get(model, Usage()) + used
                at = (slot_of(when or started), model)
                slots[at] = slots.get(at, Usage()) + used
        return SessionRecord(
            agent="claude",
            session_id=self.session_id,
            source_path=source_path,
            project=_project(self.cwd),
            started_at=fmt_iso(started),
            ended_at=fmt_iso(ended),
            wall_seconds=int((ended - started).total_seconds()),
            active_seconds=active_seconds(self.stamps, idle_gap_seconds),
            usage=usage,
            slots=slots,
            tool_calls=len(self.tool_ids),
            files_touched=len(self.files),
            error_retries=len(self.error_ids),
            turns=len(self.responses),
            prompts=len(self.prompt_ids),
        )


def _timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return parse_iso(raw).replace(microsecond=0)
    except (ValueError, OverflowError):  # OverflowError: a year 1 date with an offset
        return None


def _blocks(content: Any) -> Iterator[dict[str, Any]]:
    if isinstance(content, list):
        yield from (b for b in content if isinstance(b, dict))


def _count(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    return value if isinstance(value, int) and value > 0 else 0


def _tokens(raw: dict[str, Any]) -> Usage:
    written = _count(raw, "cache_creation_input_tokens")
    split = raw.get("cache_creation")
    one_hour = _count(split, "ephemeral_1h_input_tokens") if isinstance(split, dict) else 0
    one_hour = min(one_hour, written)  # the flat counter is the total: the split can move tokens between TTLs, not add any
    return Usage(
        input=_count(raw, "input_tokens"),
        cache_read=_count(raw, "cache_read_input_tokens"),
        cache_write=written - one_hour,
        output=_count(raw, "output_tokens"),
        cache_write_1h=one_hour,
    )


def _usage(raw: Any, model: Any) -> Usage | None:
    """Billed tokens of one record: the top-level counters, or the sum of the iterations ``model`` served if larger."""
    if not isinstance(raw, dict):
        return None
    iterations = raw.get("iterations")
    served = Usage()
    if isinstance(iterations, list):
        for entry in iterations:
            if isinstance(entry, dict) and entry.get("model") in (None, model):
                served = served + _tokens(entry)
    return _peak(_tokens(raw), served)


def _peak(a: Usage, b: Usage) -> Usage:
    """Per-bucket maximum. Writes are one bucket (5-minute plus 1-hour) that keeps its largest 1-hour part, so a
    response one copy of which lacks the TTL split (all 5-minute) never gets its writes counted twice."""
    written = max(a.cache_write + a.cache_write_1h, b.cache_write + b.cache_write_1h)
    one_hour = max(a.cache_write_1h, b.cache_write_1h)
    return Usage(max(a.input, b.input), max(a.cache_read, b.cache_read), written - one_hour, max(a.output, b.output), one_hour)


def _written_path(tool_input: Any) -> str | None:
    if isinstance(tool_input, dict):
        for key in _PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _is_prompt(rec: dict[str, Any], content: Any) -> bool:
    """A user record that a person typed, as opposed to one the harness or an agent injected."""
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly") or rec.get("isSidechain"):
        return False
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind", "human") != "human":
        return False
    if rec.get("promptSource") == "system":
        return False
    if isinstance(content, str):
        return bool(content.strip()) and not _INJECTED.match(content)
    blocks = list(_blocks(content))
    if any(b.get("type") == "image" for b in blocks):
        return True
    texts = [b.get("text") for b in blocks if b.get("type") == "text"]
    return any(isinstance(t, str) and t.strip() and not _INJECTED.match(t) for t in texts)


def _project(cwd: str | None) -> str:
    """Repo name for a working directory, folding git worktrees onto their repo; never a full path."""
    path = (cwd or "").replace("\\", "/").rstrip("/")
    repo, is_claude_worktree, _ = path.partition("/.claude/worktrees/")
    if is_claude_worktree:
        path = repo
    else:
        parent, _, leaf = path.rpartition("/")
        if parent.endswith("-wt"):
            path = parent[: -len("-wt")]
        elif "-wt-" in leaf:
            path = f"{parent}/{leaf.split('-wt-', 1)[0]}"
    name = project_name(path)
    return "unknown" if re.fullmatch(r"[A-Za-z]:", name) else name


PARSER = _Parser()

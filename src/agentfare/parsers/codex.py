"""Codex CLI session logs -> ``SessionRecord``.

On-disk layout::

    ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-uuid>.jsonl
    ~/.codex/archived_sessions/rollout-<timestamp>-<thread-uuid>.jsonl   (flat; threads archived in the app)

The two directories hold different threads (no id is in both). One file is one thread and one session: a
sub-agent or auto-review ("guardian") thread is its own file, and its requests are real spend. Every line is
``{"timestamp", "ordinal", "type", "payload"}``; the versions seen (cli 0.151 to 0.159) differ in which
records exist, not in this envelope. Rules, each checked against about 2,250 real rollouts (24 GB):

* **Token semantics.** ``token_usage_record`` (one per model response: ``usage`` and a unique
  ``response_id``) and ``event_msg`` ``token_count`` (``info.last_token_usage`` is the same response,
  ``info.total_token_usage`` the thread's running total) follow OpenAI's convention, on every running total
  and usage record seen: ``input_tokens`` INCLUDES ``cached_input_tokens`` (cached <= input),
  ``output_tokens`` INCLUDES ``reasoning_output_tokens`` (reasoning <= output) and ``total_tokens ==
  input + output``. ``cache_write_input_tokens`` is read as one more part of ``input_tokens`` (cached +
  written <= input; it is 0 in every log seen). Hence ``Usage(input = input - cached - written,
  cache_read = cached, cache_write = written, output = output)``, with nothing counted twice.
* **Each response is billed once.** Usage is the sum of per-response counts, never a difference of running
  totals: totals restart when a thread is resumed, and a fork's first ``token_count`` carries its parent's
  total with an empty ``last_token_usage`` (inherited state, bills nothing). A ``token_count`` whose total
  equals the previous one is a rate-limit-only update. A usage record and a ``token_count`` with the same
  counts are one response in whichever order they come; a response seen in only one of them is still billed:
  context-compaction requests exist only as usage records (the running totals leave them out, about 1% of
  the input tokens seen) and logs from before usage records only have ``token_count``. A repeated
  ``response_id`` and a usage record of another thread are dropped. ``compacted`` records hold a replay of
  the history and a copy of the last usage record: they are never read.
* **Slots.** The money of a response belongs to when it was spent: each billed response (a compaction request
  too) goes to the UTC 15-minute slot of its own record's timestamp, so a thread that runs across midnight
  or a week boundary splits its cost where it was incurred. A response whose record has no usable timestamp
  goes to the slot of the thread's first timestamp. A thread written in one instant (see Time) lands in one
  slot. Per model, the slots add up to ``usage``.
* **Model** of a response is the ``turn_context.model`` of its turn (``turn_id``); a session may switch.
* **Forks.** A forked thread (``forked_from_id``) opens with a copy of its parent's history, the records
  before ``subagent_history_start_ordinal``. The copies seen hold messages only, but the parent's file
  already counted everything in them, so they are skipped whole. Newer forks keep no copy (``history_base``).
* **Tool calls** are distinct ``function_call`` / ``custom_tool_call`` / ``local_shell_call`` items by
  ``call_id``. In the versions seen the model writes scripts for a single ``exec`` tool, so that is one call
  however many commands the script runs.
* **Errors** (``error_retries``): a tool call is counted once when its output starts with ``Script failed``
  (the ``exec`` wrapper's verdict), or, for any other output, has an ``Exit code:`` / ``Process exited with
  code`` line or a JSON ``"exit_code"`` other than 0, or ``success: false``; or when a command (non-zero exit
  code), file change or MCP call that ran during the call reports ``status: failed``. A refused file change
  (``declined``) is not an error. Non-zero exits that are not real failures (``grep`` found nothing) count:
  the log cannot tell them apart.
* **Files touched** are the distinct paths of ``FileChange`` items that did not fail or get declined (both
  ends of a move), or, in logs without such items, the ``*** Add/Update/Delete File:`` and ``*** Move to:``
  headers of apply_patch text inside tool calls. Names only de-duplicate in memory and are never kept. Files
  written by a shell command (``cat > f``, ``sed -i``) leave no trace in the log and are not counted.
* **Prompts** are what a person sent: ``item_completed`` ``UserMessage`` items (a reply to an agent's
  question included), else ``user_message`` events. User-role ``response_item`` messages are not read: they
  mix in the injected AGENTS.md, environment and plugin blocks, which those two never carry. The one
  injected kind that does arrive as a ``UserMessage`` is ``<paseo-system>``, the Paseo orchestrator relaying
  agent replies and permission requests (423 of the 1,893 items of the main threads seen): not a prompt. A
  sub-agent thread has no prompts: its user messages are task hand-offs and review requests.
* **Time.** Every record is an event, cut to whole seconds so that summed gaps keep the sub-second part of
  bursts. A few versions write a whole thread in one instant (identical record timestamps); such a thread
  shows no duration.
* **Project** is the repository name of ``git.repository_url`` when the thread has one, else the basename of
  ``cwd``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import fmt_iso, parse_iso
from ..models import SessionRecord, Usage, slot_of
from .base import active_seconds, project_name, stat_signature

_UNKNOWN_MODEL = "unknown"
_COUNT_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
_CALLS = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
_OUTPUTS = frozenset({"function_call_output", "custom_tool_call_output", "local_shell_call_output"})
_EVENTS = frozenset({"token_count", "task_started", "user_message", "item_completed"})
_ITEMS = frozenset({b"UserMessage", b"CommandExecution", b"FileChange", b"McpToolCall"})
_TOOL_ITEMS = frozenset({"CommandExecution", "FileChange", "McpToolCall"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=\.jsonl$)", re.IGNORECASE)

# The fixed start of every record, so the many records that are not needed (reasoning, compaction replays,
# world state) cost one regex match instead of a JSON parse. Anything that does not match is parsed whole.
_HEAD = re.compile(rb'\{"timestamp":"([^"]*)",(?:"ordinal":(\d+),)?"type":"([a-z_]+)","payload":\{(?:"type":"([a-z_]+)")?')
_ITEM_TYPE = re.compile(rb'"item":\{"type":"(\w+)"')
_EXIT_LINE = re.compile(r"^(?:Exit code:|Process exited with code)\s*(-?\d+)", re.MULTILINE)
_EXIT_JSON = re.compile(r'"exit_code":\s*(-?\d+)')
# Header lines of apply_patch text. In a JSON or JS string literal the newline is the two characters \n.
_PATCH_PATH = re.compile(r"\*\*\* (?:(?:Add|Update|Delete) File|Move to): ((?:[^\n\"'`\\]|\\(?!n))+)")
_ORCHESTRATED = re.compile(r"\s*<paseo-system(?=[\s>/])")

_Counts = tuple[int, int, int, int]  # input (with cached), cached, cache written, output (with reasoning)


class _Parser:
    agent = "codex"

    def discover(self, root: Path) -> Iterator[Path]:
        yield from sorted(p for p in root.rglob("rollout-*.jsonl") if p.is_file())

    def signature(self, path: Path) -> str:
        return stat_signature(path)

    def parse(self, path: Path, *, idle_gap_seconds: int = 1800) -> list[SessionRecord]:
        session = _Session()
        with path.open("rb") as fh:
            for line in fh:
                session.feed(line)
        record = session.finish(path, idle_gap_seconds)
        return [record] if record else []


@dataclass
class _Session:
    stamps: list[datetime] = field(default_factory=list)
    last_second: str = ""
    last_time: datetime | None = None  # parsed form of last_second
    now: datetime | None = None  # time of the record being read, None when it has no usable timestamp
    meta: dict[str, Any] | None = None
    thread_id: str | None = None
    subagent: bool = False
    skip_below: int | None = None
    # tokens
    model: str | None = None
    turn_id: str | None = None
    turn_models: dict[str, str] = field(default_factory=dict)
    # (turn, model then, slot or None when the record had no usable time) -> usage
    billed: dict[tuple[str | None, str | None, str | None], Usage] = field(default_factory=dict)
    turns: int = 0
    last_total: _Counts | None = None
    response_ids: set[str] = field(default_factory=set)
    by_record: Counter[_Counts] = field(default_factory=Counter)  # billed from a usage record, token_count not seen yet
    by_count: Counter[_Counts] = field(default_factory=Counter)  # billed from a token_count, usage record not seen yet
    # activity
    call_ids: set[str] = field(default_factory=set)
    tool_calls: int = 0
    open_call: str | None = None
    errored: set[str] = field(default_factory=set)
    error_retries: int = 0
    patch_files: set[str] = field(default_factory=set)
    changed_files: set[str] = field(default_factory=set)
    saw_file_change: bool = False
    prompt_items: int = 0
    prompt_events: int = 0

    def feed(self, line: bytes) -> None:
        envelope = _envelope(line)
        if envelope is None:
            return
        rec, stamp, ordinal, kind, ptype = envelope
        if self.skip_below is not None and isinstance(ordinal, int) and ordinal < self.skip_below:
            return
        self._stamp(stamp)
        if not self._wanted(kind, ptype, line):
            return
        payload = (rec or _load(line) or {}).get("payload")
        if isinstance(payload, dict):
            self._record(kind, ptype, payload)

    def _wanted(self, kind: Any, ptype: str | None, line: bytes) -> bool:
        if kind == "session_meta":
            return self.meta is None
        if kind in ("turn_context", "token_usage_record"):
            return True
        if kind == "event_msg":
            if ptype == "item_completed":
                item = _ITEM_TYPE.search(line, 0, 600)
                return item is None or item[1] in _ITEMS
            return ptype in _EVENTS
        if kind == "response_item":
            return ptype in _CALLS or ptype in _OUTPUTS
        return False

    def _record(self, kind: Any, ptype: str | None, p: dict[str, Any]) -> None:
        if kind == "session_meta":
            self._start(p)
        elif kind == "turn_context":
            self._turn_context(p)
        elif kind == "token_usage_record":
            self._usage_record(p)
        elif kind == "event_msg":
            self._event(ptype, p)
        else:
            self._response_item(ptype, p)

    # ---- envelope ------------------------------------------------------------------------
    def _stamp(self, raw: Any) -> None:
        if not isinstance(raw, str):
            self.now = None
            return
        second = raw[:19]
        if second != self.last_second:
            self.last_second = second
            self.last_time = _timestamp(raw)
            if self.last_time:
                self.stamps.append(self.last_time)
        self.now = self.last_time

    def _start(self, p: dict[str, Any]) -> None:
        self.meta = p
        self.thread_id = p["id"] if isinstance(p.get("id"), str) else None
        source = p.get("source")
        self.subagent = isinstance(source, dict) and "subagent" in source
        first_own = p.get("subagent_history_start_ordinal")
        if p.get("forked_from_id") and isinstance(first_own, int):
            self.skip_below = first_own

    def _turn_context(self, p: dict[str, Any]) -> None:
        self._turn(p.get("turn_id"))
        model = p.get("model")
        if isinstance(model, str) and model:
            self.model = model
            if self.turn_id:
                self.turn_models[self.turn_id] = model

    def _turn(self, turn_id: Any) -> None:
        if isinstance(turn_id, str) and turn_id:
            self.turn_id = turn_id

    def _event(self, ptype: str | None, p: dict[str, Any]) -> None:
        if ptype == "token_count":
            self._token_count(p)
        elif ptype == "item_completed":
            self._item(p.get("item"))
        elif ptype == "task_started":
            self._turn(p.get("turn_id"))
        elif ptype == "user_message" and _typed(p.get("message")):
            self.prompt_events += 1

    # ---- tokens --------------------------------------------------------------------------
    def _usage_record(self, p: dict[str, Any]) -> None:
        if self.thread_id and p.get("thread_id") not in (None, self.thread_id):
            return  # another thread's response, replayed into this file
        response_id = p.get("response_id")
        if isinstance(response_id, str):
            if response_id in self.response_ids:
                return
            self.response_ids.add(response_id)
        counts = _counts(p.get("usage"))
        if counts is not None:
            self._response(p.get("turn_id"), counts, self.by_record, self.by_count)

    def _token_count(self, p: dict[str, Any]) -> None:
        info = p.get("info")
        total = _counts(info.get("total_token_usage")) if isinstance(info, dict) else None
        if total is None or total == self.last_total:
            return  # no info, or only the rate limits changed
        last = _counts(info.get("last_token_usage"))
        if last is None:
            last = total if self.last_total is None else _delta(total, self.last_total)
        self.last_total = total
        self._response(self.turn_id, last, self.by_count, self.by_record)

    def _response(self, turn_id: Any, counts: _Counts, mine: Counter[_Counts], other: Counter[_Counts]) -> None:
        """Bill a response unless the other kind of record already did (they are matched by their counts)."""
        if other[counts]:
            other[counts] -= 1
            return
        mine[counts] += 1
        used = _usage(counts)
        if used.is_empty():
            return
        key = (turn_id if isinstance(turn_id, str) else None, self.model, slot_of(self.now) if self.now else None)
        self.billed[key] = self.billed.get(key, Usage()) + used
        self.turns += 1

    # ---- tools, files, errors, prompts ---------------------------------------------------
    def _response_item(self, ptype: str | None, p: dict[str, Any]) -> None:
        if ptype in _CALLS:
            self._call(p)
        elif ptype in _OUTPUTS:
            call_id = p.get("call_id") if isinstance(p.get("call_id"), str) else None
            if call_id is not None and call_id == self.open_call:
                self.open_call = None
            if _output_failed(p):
                self._error(call_id)

    def _call(self, p: dict[str, Any]) -> None:
        call_id = p.get("call_id") if isinstance(p.get("call_id"), str) else None
        if call_id is not None:
            if call_id in self.call_ids:
                return
            self.call_ids.add(call_id)
        self.tool_calls += 1
        self.open_call = call_id
        text = _call_text(p)
        if "Begin Patch" in text:
            self.patch_files.update(m[1].strip() for m in _PATCH_PATH.finditer(text) if m[1].strip())

    def _item(self, item: Any) -> None:
        if not isinstance(item, dict):
            return
        kind = _name(item.get("type"))
        if kind == "UserMessage" and _typed(_first_text(item.get("content"))):
            self.prompt_items += 1
        elif kind == "FileChange":
            self._file_change(item)
        if kind in _TOOL_ITEMS and _item_failed(item) and self.open_call is not None:
            self._error(self.open_call)

    def _file_change(self, item: dict[str, Any]) -> None:
        self.saw_file_change = True
        changes = item.get("changes")
        if item.get("status") in ("failed", "declined") or not isinstance(changes, dict):
            return
        for path, change in changes.items():
            self.changed_files.add(path)
            moved_to = change.get("move_path") if isinstance(change, dict) else None
            if isinstance(moved_to, str) and moved_to:
                self.changed_files.add(moved_to)

    def _error(self, call_id: str | None) -> None:
        if call_id is not None:
            if call_id in self.errored:
                return
            self.errored.add(call_id)
        self.error_retries += 1

    # ---- result --------------------------------------------------------------------------
    def finish(self, path: Path, idle_gap_seconds: int) -> SessionRecord | None:
        if not self.billed or not self.stamps:
            return None
        started, ended = min(self.stamps), max(self.stamps)
        fallback = next(iter(self.turn_models.values()), _UNKNOWN_MODEL)
        first_slot = slot_of(started)
        usage: dict[str, Usage] = {}
        slots: dict[tuple[str, str], Usage] = {}
        for (turn_id, model_then, slot), used in self.billed.items():
            model = self.turn_models.get(turn_id) if turn_id else None
            name = model or model_then or fallback
            usage[name] = usage.get(name, Usage()) + used
            cell = (slot or first_slot, name)
            slots[cell] = slots.get(cell, Usage()) + used
        return SessionRecord(
            agent="codex",
            session_id=self.thread_id or _file_id(path),
            source_path=str(path),
            project=_project(self.meta or {}),
            started_at=fmt_iso(started),
            ended_at=fmt_iso(ended),
            wall_seconds=int((ended - started).total_seconds()),
            active_seconds=active_seconds(self.stamps, idle_gap_seconds),
            usage=usage,
            slots=dict(sorted(slots.items())),
            tool_calls=self.tool_calls,
            files_touched=len(self.changed_files if self.saw_file_change else self.patch_files),
            error_retries=self.error_retries,
            turns=self.turns,
            prompts=0 if self.subagent else (self.prompt_items or self.prompt_events),
        )


def _envelope(line: bytes) -> tuple[dict[str, Any] | None, Any, Any, Any, str | None] | None:
    """``(record if already parsed, timestamp, ordinal, record type, payload type)``; None when not a record."""
    head = _HEAD.match(line)
    if head and (head[4] or head[3] not in (b"response_item", b"event_msg")):
        ordinal = int(head[2]) if head[2] else None
        return None, head[1].decode("ascii", "replace"), ordinal, head[3].decode(), head[4].decode() if head[4] else None
    rec = _load(line)
    if rec is None:
        return None
    payload = rec.get("payload")
    ptype = payload.get("type") if isinstance(payload, dict) else None
    return rec, rec.get("timestamp"), rec.get("ordinal"), _name(rec.get("type")), _name(ptype)


def _name(raw: Any) -> str | None:
    """A record or payload type, when it is a string (the sets it is looked up in cannot take a list or a dict)."""
    return raw if isinstance(raw, str) else None


def _load(line: bytes) -> dict[str, Any] | None:
    try:
        rec = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return rec if isinstance(rec, dict) else None


def _timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return parse_iso(raw).replace(microsecond=0)
    except ValueError:
        return None


def _count(raw: Any) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def _counts(raw: Any) -> _Counts | None:
    if not isinstance(raw, dict):
        return None
    total_input, cached, written, output = (_count(raw.get(key)) for key in _COUNT_KEYS)
    return total_input, cached, written, output


def _delta(total: _Counts, before: _Counts) -> _Counts:
    return (max(total[0] - before[0], 0), max(total[1] - before[1], 0), max(total[2] - before[2], 0), max(total[3] - before[3], 0))


def _usage(counts: _Counts) -> Usage:
    total_input, cached, written, output = counts
    return Usage(input=max(total_input - cached - written, 0), cache_read=cached, cache_write=written, output=output)


def _call_text(p: dict[str, Any]) -> str:
    for key in ("input", "arguments", "action"):
        value = p.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value)
    return ""


def _output_failed(p: dict[str, Any]) -> bool:
    output = p.get("output")
    if isinstance(output, dict):
        if output.get("success") is False:
            return True
        output = output.get("content")
    text = _first_text(output)
    if text.startswith("Script "):
        return text.startswith("Script failed")  # the exec wrapper's own verdict; its body is script output
    code = _EXIT_LINE.search(text[:400]) or (_EXIT_JSON.search(text[-400:]) if text.startswith("{") else None)
    return bool(code) and int(code[1]) != 0


def _first_text(output: Any) -> str:
    """The output as text: a string, or the first text part of a list (that part carries the status header)."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return next((b["text"] for b in output if isinstance(b, dict) and isinstance(b.get("text"), str)), "")
    return ""


def _typed(text: Any) -> bool:
    """False for a user message that the Paseo orchestrator wrote; anything else counts as a person's prompt."""
    return not (isinstance(text, str) and _ORCHESTRATED.match(text))


def _item_failed(item: dict[str, Any]) -> bool:
    if item.get("status") == "failed":
        return True
    code = item.get("exit_code")
    return item.get("type") == "CommandExecution" and isinstance(code, int) and code != 0


def _file_id(path: Path) -> str:
    found = _UUID.search(path.name)
    return found[0] if found else path.stem


def _project(meta: dict[str, Any]) -> str:
    """Repo name of the thread's git remote, else the working directory's basename; never a full path."""
    git = meta.get("git")
    remote = git.get("repository_url") if isinstance(git, dict) else None
    if isinstance(remote, str) and remote.strip():
        name = project_name(remote if "://" in remote else remote.replace(":", "/")).removesuffix(".git")
        if name and name != "unknown":
            return name
    cwd = meta.get("cwd")
    return project_name(cwd if isinstance(cwd, str) else None)


PARSER = _Parser()

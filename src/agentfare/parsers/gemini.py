"""Gemini CLI session logs -> ``SessionRecord``.

Layout under ``~/.gemini/tmp`` (Gemini CLI 0.60, ``ChatRecordingService``)::

    <slug>/.project_root                        working directory of the project (one line)
    <slug>/chats/session-<UTC minute>-<first 8 chars of sessionId>.jsonl    one session
    <slug>/chats/<sessionId>/<agent id>.jsonl   sub-agent transcripts (``kind: "subagent"``)
    <slug>/{logs,tool-outputs,...}              not sessions

A session file is JSONL: a header ``{sessionId, projectHash, startTime, lastUpdated, kind}``, then
messages ``{id, timestamp, type: user|gemini|info|..., content}`` (a ``gemini`` message also has
``model``, ``tokens`` and ``toolCalls``), ``{"$set": {...}}`` metadata patches and
``{"$rewindTo": "<id>"}``. The CLI re-appends a message whenever it changes (tokens or tool calls
arrive later), so the LAST record with an id is that message. CLIs before the JSONL format wrote one
``session-*.json`` object ``{sessionId, ..., messages: [...]}``; a session stored in both forms is
read from the JSONL.

Rules (the CLI's own loader and recorder are the spec; the only real logs available while writing
this hold no conversations, so token handling is checked against the source, not against data):

* **Billing view.** The CLI's loader drops the messages after ``$rewindTo`` and replaces the whole
  list on ``$set.messages`` (written at startup, when a resumed chat is rebuilt and when its
  history is compacted). Those responses were still billed, and the compacted ones are the
  expensive part of long sessions, so both records are ignored: every message ever recorded counts
  once, by id. The ``messages`` inside ``$set`` are ignored too: each is a message that was already
  appended or a token-less copy under a fresh id (compaction re-records the kept tail), which would
  inflate ``prompts``. The one place ``messages`` is read is the legacy JSON object.
* **Tokens** are the Gemini API's ``usageMetadata``: ``input`` (promptTokenCount) includes
  ``cached``, ``output`` (candidatesTokenCount) excludes ``thoughts``, and ``tool``
  (toolUsePromptTokenCount) is extra prompt. So ``input = input - cached + tool``,
  ``cache_read = cached``, ``cache_write = 0`` (Gemini has no write fee) and
  ``output = output + thoughts`` (thinking is billed as output). The model is ``message.model``. The CLI
  records none for a reply with no text, thoughts or tool calls (a model that answers a tool result with
  nothing, which is billed like any other reply), so that one is attributed to the model of the reply before
  it in the same file, and to ``unknown`` (unpriced, hence reported) when there is none.
  Calls the CLI makes outside the chat (model router/classifier, compaction summaries, web tools)
  are not in these files and so not in the totals.
* **Slots** (``SessionRecord.slots``) put each response's tokens in the UTC 15-minute slot of the message's
  own ``timestamp``, so a session that runs past midnight or a week edge splits its money where it was
  spent. The CLI stamps a message once, in ``newMessage`` when it is created, and every later rewrite of it
  (``recordMessageTokens`` and ``recordToolCalls`` re-push the same object, ``updateMessagesFromHistory``
  spreads the existing one) keeps that timestamp. So the LAST record with an id gives the time, and it is
  the one the first version had. A response with no usable timestamp counts at the session's first one
  (``started_at``), what ``SessionRecord.effective_slots`` does for a parser that records no timing.
  Sub-agent responses use their own timestamps. Per model the slots add up to ``usage`` exactly.
* **Turns** are ``gemini`` messages with token counts. **Tool calls** are the ``toolCalls``
  entries of ``gemini`` messages. **Errors** are calls with status ``error`` (the tool failed) or
  ``cancelled`` (the user rejected or aborted it; Claude's parser counts rejections as errors too),
  plus shell commands that ran but did not exit cleanly: the CLI records those as ``success`` and only
  says so in the output's trailing ``Exit Code: N`` / ``Signal:`` line (inside the ``<untrusted_context>``
  wrapper the shell tool puts around its output), or, for a command killed after running without output
  for too long, in the opening words of the output. Claude (``is_error``) and Codex both count these.
  The other CoreToolCallStatus values (validating, scheduled, executing,
  awaiting_approval, success) are not errors. ``files_touched`` counts distinct ``file_path`` values of
  ``write_file`` / ``replace`` calls that succeeded; shell commands cannot be attributed to files.
* **Prompts** are ``user`` messages of the main file that a person typed. Not tool-result carriers
  (a ``functionResponse`` part) and not text the CLI treats as non-conversation
  (``isIgnoredUserContent``: empty, ``/`` or ``?`` prefixed, ``<session_context>``,
  ``<hook_context>``; plus the ``<state_snapshot>`` written by compaction). Known over-count: the CLI
  records turns it injects itself (loop-recovery feedback, IDE context) and, whenever it masks old tool
  output or restores a checkpoint, replays the whole history, as plain ``user`` messages that look typed.
  Only ``prompts`` is affected: replayed ``gemini`` messages carry no tokens, tool calls or files.
* **Imports.** ``gemini --session-file`` starts a new session whose first message is an ``import-<ms>``
  notice, followed by the source session's messages copied verbatim (tokens, tool calls and their original
  timestamps). Those were billed in the source session, so a message older than the notice is skipped
  everywhere, including the time span.
* **Sub-agents** are separate files whose directory is the parent's sanitised sessionId. They are
  real spend, so they are merged into the parent (a sub-agent's ``user`` message is the task its
  parent gave it, not a prompt) and ``signature()`` covers them.
* **Time.** Events are the header times, message timestamps and tool-call timestamps, cut to whole
  seconds. **Project** is the basename of ``.project_root``, else the slug directory name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import fmt_iso, parse_iso
from ..models import SessionRecord, Usage, slot_of
from .base import active_seconds, is_regular_file, project_name, stat_signature

_CHATS = "chats"
_SESSION_PREFIX = "session-"
_NOT_PROJECT_DIRS = frozenset({"tool-outputs"})  # large spill directories, never sessions
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")  # sanitizeFilenamePart
_UNKNOWN_MODEL = "unknown"
_WRITE_TOOLS = ("write_file", "replace")  # tuples, not sets: a malformed name may be unhashable
_FAILED = ("error", "cancelled")
_SHELL_TOOL = "run_shell_command"
# What the shell tool appends to its output when the command did not exit cleanly. The CLI still records
# such a call as "success": only a command that could not be run at all is an "error". Only the end of the
# output is searched: the footer is short, and a long run of look-alike lines must not make the match slow.
# The shell tool wraps its output in ``<untrusted_context>`` tags, so the footer is followed by the end tag
# (CLIs before the wrapper did not add it).
_UNCLEAN_EXIT = re.compile(
    r"\n(?:Exit Code: -?[1-9]\d*|Signal: \S+)(?:\n(?:Signal|Background PIDs|Process Group PGID): [^\n]*)*"
    r"(?:\s*</untrusted_context>)?\s*$"
)
_FOOTER_WINDOW = 1024
# How the output starts when the shell tool killed a command that printed nothing for too long.
_TIMED_OUT = re.compile(
    r"(?:<untrusted_context>\n)?Command was automatically cancelled because it exceeded the timeout"
)
_META_KEYS = ("sessionId", "startTime", "lastUpdated")
_NOT_PROMPTS = ("/", "?", "<session_context>", "<hook_context>", "<state_snapshot>")
_IMPORT_NOTICE = re.compile(r"import-\d+")  # id of the info message that opens a ``--session-file`` session


class _Parser:
    agent = "gemini"

    def discover(self, root: Path) -> Iterator[Path]:
        """Session files in every ``chats`` directory under ``root``; sub-agent files are merged, not listed."""
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _NOT_PROJECT_DIRS)
            if os.path.basename(dirpath) == _CHATS:  # no pruning: a project called "chats" has tmp/chats/chats
                for name in _session_files(filenames, _SESSION_PREFIX):
                    if is_regular_file(Path(dirpath, name)):  # not a FIFO: opening one would hang
                        yield Path(dirpath, name)

    def signature(self, path: Path) -> str:
        """Main file size+mtime plus a digest of every sub-agent file's, so a change in any re-parses."""
        digest = hashlib.blake2b(digest_size=8)
        subagents = _subagent_files(path, _header_session_id(path))
        for sub in subagents:
            digest.update(f"{sub.name}:{stat_signature(sub)}\n".encode())
        return f"{stat_signature(path)}+{len(subagents)}:{digest.hexdigest()}"

    def parse(self, path: Path, *, idle_gap_seconds: int = 1800) -> list[SessionRecord]:
        main = _load(path)
        subagents = [_load(p) for p in _subagent_files(path, _header_session_id(path))]
        record = _combine(main, subagents, main.session_id or path.stem, path, idle_gap_seconds)
        return [record] if record else []


def _session_files(names: Iterable[str], prefix: str = "") -> list[str]:
    """Session files among ``names``, one per session: the JSONL form wins over the legacy JSON one."""
    chosen: dict[str, str] = {}
    for name in sorted(names):  # "x.json" sorts before "x.jsonl", so the JSONL name replaces it
        stem, ext = os.path.splitext(name)
        if name.startswith(prefix) and ext in (".json", ".jsonl"):
            chosen[stem] = name
    return list(chosen.values())


def _subagent_files(path: Path, session_id: str | None) -> list[Path]:
    """Sub-agent transcripts of a session: ``chats/<sessionId>/*.jsonl``."""
    if not session_id:
        return []
    directory = path.parent / _UNSAFE.sub("_", session_id)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [p for p in (directory / name for name in _session_files(names)) if is_regular_file(p)]


def _records(path: Path) -> Iterator[dict[str, Any]]:
    """JSON objects of a session file: one per JSONL line (malformed lines are skipped) or the whole legacy file."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        chunks: Iterable[str] = [fh.read()] if path.suffix == ".json" else fh
        for chunk in chunks:
            try:
                rec = json.loads(chunk)
            except (ValueError, RecursionError):
                continue
            if isinstance(rec, dict):
                yield rec


def _header_session_id(path: Path) -> str | None:
    for rec in _records(path):  # only the first record is read
        session_id = rec.get("sessionId")
        return session_id if isinstance(session_id, str) else None
    return None


@dataclass(frozen=True, slots=True)
class _Message:
    """What one message contributes. Its text is inspected once and never kept."""

    stamps: list[datetime]
    when: datetime | None = None  # the message's own timestamp, not cut to whole seconds
    prompt: bool = False
    model: str | None = None  # the model that served a reply; None when the CLI recorded none
    usage: Usage | None = None  # None: the message carries no usable token counters
    tool_calls: int = 0
    errors: int = 0
    files: frozenset[str] = frozenset()


@dataclass
class _Session:
    meta: dict[str, Any] = field(default_factory=dict)
    messages: dict[str, _Message] = field(default_factory=dict)  # message id -> latest version
    imported_at: datetime | None = None  # when ``gemini --session-file`` built this session

    @property
    def session_id(self) -> str | None:
        value = self.meta.get("sessionId")
        return value if isinstance(value, str) and value else None

    def feed(self, rec: dict[str, Any]) -> None:
        # Same precedence as the CLI's loader: message, then metadata patch, then header.
        if isinstance(rec.get("id"), str):
            self._keep_message(rec)
        elif isinstance(rec.get("$set"), dict):
            self._keep_meta(rec["$set"])
        elif isinstance(rec.get("sessionId"), str) and isinstance(rec.get("projectHash"), str):
            self._keep_meta(rec)
            legacy = rec.get("messages")
            for message in legacy if isinstance(legacy, list) else ():
                if isinstance(message, dict) and isinstance(message.get("id"), str):
                    self._keep_message(message)

    def _keep_message(self, rec: dict[str, Any]) -> None:
        message = self.messages[rec["id"]] = _summarize(rec)
        if rec.get("type") == "info" and _IMPORT_NOTICE.fullmatch(rec["id"]):
            self.imported_at = message.when

    def carried_over(self, message: _Message) -> bool:
        """A message ``--session-file`` copied from another session: billed there, at its own time."""
        return self.imported_at is not None and message.when is not None and message.when < self.imported_at

    def _keep_meta(self, values: dict[str, Any]) -> None:
        self.meta.update((k, values[k]) for k in _META_KEYS if k in values)

    def header_stamps(self) -> list[datetime]:
        return _stamps([self.meta.get("startTime"), self.meta.get("lastUpdated")])


def _load(path: Path) -> _Session:
    session = _Session()
    for rec in _records(path):
        session.feed(rec)
    return session


def _combine(
    main: _Session, subagents: list[_Session], session_id: str, path: Path, idle_gap_seconds: int
) -> SessionRecord | None:
    usage: dict[str, Usage] = {}
    spent: list[tuple[datetime | None, str, Usage]] = []  # (when, model, tokens) of every response
    stamps: list[datetime] = []
    files: set[str] = set()
    tool_calls = errors = turns = prompts = 0
    for session in (main, *subagents):
        stamps += session.header_stamps()
        served_by = _UNKNOWN_MODEL  # a reply that names no model was served by the one before it
        for message in session.messages.values():
            if session.carried_over(message):
                continue
            served_by = message.model or served_by
            stamps += message.stamps
            tool_calls += message.tool_calls
            errors += message.errors
            files |= message.files
            if message.usage is not None:
                turns += 1
                usage[served_by] = usage.get(served_by, Usage()) + message.usage
                spent.append((message.when, served_by, message.usage))
            if message.prompt and session is main:
                prompts += 1
    if not usage or not stamps:
        return None
    started, ended = min(stamps), max(stamps)
    return SessionRecord(
        agent="gemini",
        session_id=session_id,
        source_path=str(path),
        project=_project(path.parent),
        started_at=fmt_iso(started),
        ended_at=fmt_iso(ended),
        wall_seconds=int((ended - started).total_seconds()),
        active_seconds=active_seconds(stamps, idle_gap_seconds),
        usage=usage,
        slots=_slots(spent, slot_of(started)),
        tool_calls=tool_calls,
        files_touched=len(files),
        error_retries=errors,
        turns=turns,
        prompts=prompts,
    )


def _slots(spent: list[tuple[datetime | None, str, Usage]], undated: str) -> dict[tuple[str, str], Usage]:
    """Tokens per (slot, model); a response with no timestamp counts in the ``undated`` slot."""
    slots: dict[tuple[str, str], Usage] = {}
    for when, model, used in spent:
        key = (slot_of(when) if when else undated, model)
        slots[key] = slots.get(key, Usage()) + used
    return dict(sorted(slots.items()))


def _summarize(rec: dict[str, Any]) -> _Message:
    when = _parse(rec.get("timestamp"))
    stamps = [when.replace(microsecond=0)] if when else []
    kind = rec.get("type")
    if kind == "user":
        return _Message(stamps, when, prompt=_is_prompt(rec.get("content")))
    if kind != "gemini":
        return _Message(stamps, when)
    raw_calls = rec.get("toolCalls")
    calls = [c for c in raw_calls if isinstance(c, dict)] if isinstance(raw_calls, list) else []
    model = rec.get("model")
    return _Message(
        stamps + _stamps(c.get("timestamp") for c in calls),
        when,
        model=model if isinstance(model, str) and model else None,
        usage=_usage(rec.get("tokens")),
        tool_calls=len(calls),
        errors=sum(_failed(c) for c in calls),
        files=frozenset(path for c in calls if (path := _written_path(c))),
    )


def _parse(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except (ValueError, OverflowError):
            pass
    return None


def _stamps(raw: Iterable[Any]) -> list[datetime]:
    """The valid timestamps among ``raw``, cut to whole seconds."""
    return [t.replace(microsecond=0) for t in map(_parse, raw) if t]


def _count(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    return value if type(value) is int and value > 0 else 0  # not bool: true is not one token


def _usage(tokens: Any) -> Usage | None:
    """The message's tokens in the four ``Usage`` buckets; None when it has no usable counters."""
    if not isinstance(tokens, dict):
        return None
    cached = _count(tokens, "cached")
    used = Usage(
        input=max(_count(tokens, "input") - cached, 0) + _count(tokens, "tool"),
        cache_read=cached,
        cache_write=0,
        output=_count(tokens, "output") + _count(tokens, "thoughts"),
    )
    return None if used.is_empty() else used


def _failed(call: dict[str, Any]) -> bool:
    status = call.get("status")
    if status in _FAILED:
        return True
    return status == "success" and call.get("name") == _SHELL_TOOL and any(_unclean(text) for text in _outputs(call))


def _unclean(output: str) -> bool:
    """Whether the shell tool's output says its command was killed or exited non-zero."""
    return bool(_TIMED_OUT.match(output) or _UNCLEAN_EXIT.search(output[-_FOOTER_WINDOW:]))


def _outputs(call: dict[str, Any]) -> Iterator[str]:
    """The texts a finished call handed back to the model (``result`` is a list of parts, or a bare string)."""
    result = call.get("result")
    for part in result if isinstance(result, list) else [result]:
        if isinstance(part, str):
            yield part
        elif isinstance(part, dict) and isinstance(part.get("functionResponse"), dict):
            response = part["functionResponse"].get("response")
            output = response.get("output") if isinstance(response, dict) else None
            if isinstance(output, str):
                yield output


def _written_path(call: dict[str, Any]) -> str | None:
    if call.get("status") != "success" or call.get("name") not in _WRITE_TOOLS:
        return None
    args = call.get("args")
    path = args.get("file_path") if isinstance(args, dict) else None
    return path if isinstance(path, str) and path else None


def _is_prompt(content: Any) -> bool:
    """A user message a person typed, as opposed to a tool result or context the CLI injected."""
    text = ""
    for part in content if isinstance(content, list) else [content]:
        if isinstance(part, dict) and "functionResponse" in part:
            return False
        piece = part.get("text") if isinstance(part, dict) else part
        if isinstance(piece, str):
            text += piece
    text = text.strip()
    return bool(text) and not text.startswith(_NOT_PROMPTS)


def _project(chats: Path) -> str:
    """Name of the project that owns a ``chats`` directory; never a full path."""
    try:
        root = (chats.parent / ".project_root").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        root = ""
    return project_name(root, fallback=chats.parent.name or "unknown")


PARSER = _Parser()

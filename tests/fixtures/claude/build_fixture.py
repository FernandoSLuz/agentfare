"""Builds the synthetic Claude Code fixture under ``tests/fixtures/claude/projects``.

Deterministic (no clock, no randomness). ``python tests/fixtures/claude/build_fixture.py`` rewrites the
committed files; tests/test_parser_claude.py fails if they drift from this script. Every text field
carries the privacy marker. The story is one 2026-09-28 (UTC) session in ``/home/dev/acme-shop``:

    09:00:00  prompt 1: tool work with two failing test runs, ends 09:12
    09:13:30  prompt 2 (cwd moves to .../src); the sub-agent is spawned at 09:20:10 and works until
              09:32:05 while the main file is silent (09:20:10 -> 09:32:10); a second, workflow
              sub-agent works at 09:25
    09:34:10  last event before the idle gap; 48.5 minutes of silence follow
    10:22:40  prompt 3; 10:55:30 prompt 4 (image); interrupt 11:22; prompt 5 at 11:25:00; last event 11:27:20

Two tool calls are refused rather than failed: a user-rejected ``git commit`` in turn 3 (toolDenialKind
``user-rejected``) and a sub-agent's ``npm publish`` stopped by a permission rule (``permission-rule``).
Cache writes come in three shapes: both TTLs in one response (class A), 1-hour only (D) and no
``cache_creation`` object at all, as in older logs (C).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

CANARY = "PRIVACY-CANARY-CLAUDE"
SESSION = "2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402"
OTHER_SESSION = "9d1b7e40-55c2-4f0a-8a36-0c4be2f7a915"
PROJECT_DIR = "-home-dev-acme-shop"
CWD = "/home/dev/acme-shop"
CWD_SRC = CWD + "/src"
DAY = "2026-09-28"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-4-5-20251001"
AGENT_1 = "a1b2c3d4e5f60789"
AGENT_2 = "b2c3d4e5f6071890"

# class -> (input, cache_read, cache_write, output, of which 1-hour writes) of one API response; cache_write is the
# flat total (5-minute + 1-hour), as in cache_creation_input_tokens. Z is replayed history.
USAGE = {
    "A": (1200, 80_000, 5_000, 900, 2_000),  # both TTLs: 3,000 five-minute and 2,000 one-hour writes
    "B": (400, 120_000, 0, 3_000, 0),
    "C": (500, 20_000, 2_000, 200, 0),  # an older Claude Code: no cache_creation object, all writes are 5-minute
    "D": (800, 15_000, 3_000, 400, 3_000),  # one-hour writes only
    "Z": (9_999_999, 9_999_999, 9_999_999, 9_999_999, 9_999_999),
}
NO_TTL_SPLIT = frozenset({"C"})
PARTIAL_OUTPUT = 2  # output_tokens on the early records of a streamed response
UNRECORDED_HAIKU = (1850, 0, 0, 31)  # a title-generation call: billed and in cost-state, absent from transcripts

# (tool name, path or command, outcome: ok | error | denied (user rejected it) | blocked (a permission rule did) | dup | deferred)
Tool = tuple[str, str | None, str]


def ok(name: str, arg: str | None = None) -> Tool:
    return (name, arg, "ok")


def seconds(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def stamp(at: int, ms: int, day: str = DAY) -> str:
    h, rest = divmod(at, 3600)
    m, s = divmod(rest, 60)
    return f"{day}T{h:02d}:{m:02d}:{s:02d}.{ms:03d}Z"


def tool_input(name: str, arg: str | None) -> dict[str, Any]:
    if name == "NotebookEdit":
        return {"notebook_path": arg, "new_source": f"# {CANARY}", "edit_mode": "replace"}
    if name == "MultiEdit":
        return {"file_path": arg, "edits": [{"old_string": f"{CANARY} old", "new_string": f"{CANARY} new"}]}
    if name == "Edit":
        return {"file_path": arg, "old_string": f"{CANARY} old", "new_string": f"{CANARY} new", "replace_all": False}
    if name == "Write":
        return {"file_path": arg, "content": f"# {CANARY}\n"}
    if name == "Read":
        return {"file_path": arg}
    if name == "Bash":
        return {"command": f"{arg} # {CANARY}", "description": f"{CANARY} run"}
    if name == "Grep":
        return {"pattern": f"{CANARY}-pattern", "path": CWD}
    if name == "Glob":
        return {"pattern": "**/*.py"}
    if name == "WebFetch":
        return {"url": "https://example.invalid/docs", "prompt": f"{CANARY} summarise"}
    return {"description": f"{CANARY} explore", "prompt": f"{CANARY} look around", "subagent_type": "Explore"}  # Agent


class Fixture:
    """Shared counters, so uuids are unique across files, and the per-model totals of what is written."""

    def __init__(self) -> None:
        self.n = 0
        self.totals: dict[str, list[int]] = {OPUS: [0] * 4, HAIKU: [0] * 4}

    def uid(self, prefix: str = "") -> str:
        self.n += 1
        return f"{prefix}{self.n:04d}" if prefix else f"00000000-0000-4000-8000-{self.n:012d}"


class Log:
    """One JSONL file. ``at`` is the clock in seconds after midnight; a str ``when`` is ``HH:MM:SS``."""

    def __init__(self, fx: Fixture, session: str = SESSION, agent: str | None = None, day: str = DAY) -> None:
        self.fx, self.session, self.agent, self.day = fx, session, agent, day
        self.lines: list[str] = []
        self.cwd = CWD
        self.at = 0
        self.parent: str | None = None
        self.assistant_of: dict[str, str] = {}  # tool_use id -> uuid of the assistant record holding it

    def advance(self, when: int | str) -> int:
        self.at = seconds(when) if isinstance(when, str) else self.at + when
        return self.at

    def emit(self, rec: dict[str, Any]) -> None:
        self.lines.append(json.dumps(rec, separators=(",", ":"), ensure_ascii=False))

    def other(self, rec: dict[str, Any]) -> None:
        """A record that is not part of the conversation (titles, snapshots, unknown types)."""
        self.emit({**rec, "sessionId": self.session})

    def base(self, kind: str, at: int, ms: int, **fields: Any) -> dict[str, Any]:
        uuid = self.fx.uid()
        rec: dict[str, Any] = {"parentUuid": self.parent, "isSidechain": self.agent is not None}
        if self.agent:
            rec["agentId"] = self.agent
        rec.update(fields)
        rec.update(
            type=kind, uuid=uuid, timestamp=stamp(at, ms, self.day), userType="external", entrypoint="cli",
            cwd=self.cwd, sessionId=self.session, version="2.9.4", gitBranch="feature/coupons",
        )
        self.parent = uuid
        return rec

    # ---- user side ---------------------------------------------------------------------
    def user(self, content: Any, when: int | str, **fields: Any) -> dict[str, Any]:
        rec = self.base("user", self.advance(when), 120, message={"role": "user", "content": content}, **fields)
        self.emit(rec)
        return rec

    def prompt(self, content: Any, when: int | str, *, origin: bool = True) -> dict[str, Any]:
        extra: dict[str, Any] = {"promptId": self.fx.uid(), "permissionMode": "default"}
        if origin:
            extra.update(origin={"kind": "human"}, promptSource="typed")
        return self.user(content, when, **extra)

    def result(self, tool_id: str, name: str, outcome: str, when: int | str) -> None:
        error = outcome in ("error", "denied", "blocked", "dup")
        if outcome == "denied":
            content = f"The user doesn't want to proceed with this tool use. {CANARY}"
        elif outcome == "blocked":
            content = f"Permission to use {name} has been denied. {CANARY}"
        elif error:
            content = f"Exit code 1\n{CANARY} FAILED tests/test_coupons.py"
        else:
            content = f"{CANARY} output of {name}"
        detail: Any = f"Error: {content}" if error else {"stdout": content, "stderr": "", "interrupted": False}
        extra = {"toolDenialKind": {"denied": "user-rejected", "blocked": "permission-rule"}[outcome]} if outcome in ("denied", "blocked") else {}
        block = {"tool_use_id": tool_id, "type": "tool_result", "content": content, "is_error": error}
        rec = self.user([block], when, toolUseResult=detail, sourceToolAssistantUUID=self.assistant_of[tool_id], **extra)
        if outcome == "dup":  # the same result written twice (seen after resume and compaction)
            self.emit(rec)

    # ---- assistant side ----------------------------------------------------------------
    def usage(self, cls: str, *, final: bool) -> dict[str, Any]:
        inp, cache_read, cache_write, out, one_hour = USAGE[cls]
        split: dict[str, Any] = {} if cls in NO_TTL_SPLIT else {
            "cache_creation": {"ephemeral_1h_input_tokens": one_hour, "ephemeral_5m_input_tokens": cache_write - one_hour}
        }
        usage: dict[str, Any] = {
            "input_tokens": inp, "cache_creation_input_tokens": cache_write, "cache_read_input_tokens": cache_read,
            "output_tokens": out if final else PARTIAL_OUTPUT,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}, "service_tier": "standard",
            **split, "inference_geo": "", "iterations": None, "speed": None,
        }
        if final:
            usage["speed"] = "standard"
            usage["iterations"] = [{
                "type": "message", "input_tokens": inp, "output_tokens": out,
                "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write, **split,
            }]
        return usage

    def respond(
        self, model: str, cls: str, when: int | str, *, tools: tuple[Tool, ...] = (), text: str | None = None,
        thinking: bool = True, style: str = "growing",
    ) -> list[str]:
        """One API response, written as one record per content block, all sharing message.id.

        style "growing": the early records carry the message_start snapshot (tiny output_tokens, no
        stop_reason) and the last one the final counts. style "identical": every record repeats the
        final usage (older logs). Returns the ids of the tool_use blocks whose result is still owed.
        """
        blocks: list[dict[str, Any]] = []
        if thinking:
            blocks.append({"type": "thinking", "thinking": f"{CANARY} thinking", "signature": "FAKE-SIGNATURE"})
        if text:
            blocks.append({"type": "text", "text": f"{CANARY} {text}"})
        ids = [self.fx.uid("toolu_") for _ in tools]
        for tool_id, (name, arg, _) in zip(ids, tools, strict=True):
            blocks.append({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input(name, arg), "caller": {"type": "direct"}})
        message_id, request_id = self.fx.uid("msg_"), self.fx.uid("req_")
        start = self.advance(when)
        for i, block in enumerate(blocks):
            last = i == len(blocks) - 1
            final = last or style == "identical"
            at = start + (3 if last and len(blocks) > 1 else 0)
            message = {
                "model": model, "id": message_id, "type": "message", "role": "assistant", "content": [block],
                "stop_reason": ("tool_use" if tools else "end_turn") if final else None, "stop_sequence": None,
                "usage": self.usage(cls, final=final),
            }
            rec = self.base("assistant", at, 300 + i, message=message, requestId=request_id)
            self.emit(rec)
            if block["type"] == "tool_use":
                self.assistant_of[block["id"]] = rec["uuid"]
            self.at = at
        if self.session == SESSION:
            for i, value in enumerate(USAGE[cls][:4]):
                self.fx.totals[model][i] += value
        owed = []
        for tool_id, (name, _, outcome) in zip(ids, tools, strict=True):
            if outcome == "deferred":
                owed.append(tool_id)
            else:
                self.result(tool_id, name, outcome, 3)
        return owed

    def synthetic(self, when: int | str) -> None:
        zero = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        message = {
            "model": "<synthetic>", "id": self.fx.uid("msg_"), "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": f"No response requested. {CANARY}"}],
            "stop_reason": "stop_sequence", "stop_sequence": "", "usage": zero,
        }
        self.emit(self.base("assistant", self.advance(when), 400, message=message))


def build_logs(fx: Fixture) -> tuple[Log, Log, Log]:
    main = Log(fx)
    agent1 = Log(fx, agent=AGENT_1)
    agent2 = Log(fx, agent=AGENT_2)
    main.other({"type": "permission-mode", "permissionMode": "default"})

    # history of ANOTHER session copied into this file by a fork/resume: must never be counted
    replay = Log(fx, OTHER_SESSION, day="2026-09-27")
    replay.cwd = "/elsewhere/other-project"
    replay.user(f"{CANARY} replayed prompt", "23:00:00")
    replay.respond(OPUS, "Z", 5, text="replayed answer")
    main.lines += replay.lines

    # the same from a fork that rewrote sessionId on the copied records and kept the original in session_id
    fork = Log(fx, OTHER_SESSION, day="2026-09-26")
    fork.cwd = "/elsewhere/other-project"
    fork.user(f"{CANARY} forked prompt", "22:00:00")
    fork.respond(OPUS, "Z", 5, text="forked answer")
    for line in fork.lines:
        main.emit({**json.loads(line), "sessionId": SESSION, "session_id": OTHER_SESSION})

    main.other({"type": "attachment", "attachment": {"type": "hook_success", "content": CANARY}, "timestamp": stamp(seconds("08:00:00"), 0), "cwd": CWD})
    main.other({"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(seconds("08:59:30"), 0), "content": f"{CANARY} queued"})

    # ---- turn 1 -------------------------------------------------------------------------
    main.prompt(f"{CANARY} add coupon codes to the cart", "09:00:00")
    main.user(f"Base directory for this skill: /skills/coupons\n{CANARY} skill body", 1, isMeta=True)
    main.respond(OPUS, "A", "09:00:40", text="reading the cart", tools=(ok("Read", CWD + "/src/cart.py"),))
    main.respond(OPUS, "A", 50, text="searching", tools=(ok("Grep"), ok("Glob")), style="identical")
    main.respond(OPUS, "A", 70, tools=(ok("Read", CWD + "/src/models.py"),))
    main.respond(HAIKU, "C", 50, thinking=False, tools=(ok("Bash", "git status"),))
    main.respond(OPUS, "A", 60, tools=(ok("Read", CWD + "/src/config.py"), ok("Read", CWD + "/tests/test_cart.py")), style="identical")
    main.respond(OPUS, "A", 60, tools=(ok("Edit", CWD + "/src/cart.py"),))
    main.lines.append(f'{{"parentUuid":"{main.parent}","type":"assistant","message":{{"model":"{OPUS}","id":"msg_TRUNCATED {CANARY}')
    main.respond(OPUS, "A", 60, tools=(ok("Write", CWD + "/src/coupons.py"),))
    main.respond(OPUS, "B", 70, tools=(ok("Edit", CWD + "/src/coupons.py"),))
    main.respond(OPUS, "A", 50, tools=(("Bash", "pytest -q", "error"),))
    main.respond(OPUS, "A", 60, tools=(ok("Edit", CWD + "/src/models.py"),))
    main.other({"type": "future-widget-state", "timestamp": stamp(seconds("12:59:00"), 0), "payload": {"note": f"{CANARY} unknown record type"}})
    main.respond(OPUS, "A", 50, tools=(("Bash", "pytest -q", "error"),))
    main.respond(OPUS, "A", 50, tools=(ok("Edit", CWD + "/src/cart.py"),))
    main.respond(OPUS, "A", 40, tools=(ok("Bash", "pytest -q"),))
    main.respond(OPUS, "A", 30, text="the coupon feature is in place", thinking=False)

    # ---- turn 2 -------------------------------------------------------------------------
    main.prompt(
        [{"type": "text", "text": f"<ide_opened_file>{CANARY} opened src/config.py</ide_opened_file>"},
         {"type": "text", "text": f"{CANARY} also read the discount config"}],
        "09:13:30", origin=False,
    )
    main.cwd = CWD_SRC
    main.respond(OPUS, "A", 60, tools=(ok("Read", CWD_SRC + "/config.py"),))
    main.respond(OPUS, "A", 70, tools=(ok("Edit", CWD_SRC + "/config.py"),))
    main.respond(OPUS, "A", 70, tools=(("Bash", "python -m compileall", "error"),))
    main.respond(HAIKU, "C", 70, thinking=False, tools=(ok("Bash", "ls"),))
    main.user(f"<command-name>/context</command-name>\n<command-message>context</command-message>\n<command-args>{CANARY}</command-args>", "09:18:40")
    main.user(f"<local-command-stdout>{CANARY} context usage</local-command-stdout>", 1)
    (agent_call,) = main.respond(OPUS, "A", "09:20:07", text="delegating the survey", tools=(("Agent", None, "deferred"),))

    agent1.cwd = CWD_SRC
    agent1.user(f"{CANARY} survey the repo for coupon-related code", "09:20:12")
    agent1.respond(HAIKU, "D", "09:20:40", thinking=False, tools=(ok("Grep"), ("Bash", "npm publish", "blocked")))
    agent1.respond(HAIKU, "D", "09:23:40", thinking=False, tools=(ok("Read", CWD + "/src/cart.py"), ok("Read", CWD + "/src/coupons.py")))
    agent1.respond(HAIKU, "D", "09:27:15", thinking=False, tools=(("Bash", "pytest -q", "error"),))
    agent1.respond(OPUS, "A", "09:29:30", text="writing the helper", tools=(ok("Write", CWD + "/src/utils.py"), ok("Edit", CWD + "/src/cart.py")))
    agent1.respond(OPUS, "A", "09:32:05", text="survey done", thinking=False)

    agent2.cwd = CWD_SRC
    agent2.user(f"{CANARY} apply the discount rule", "09:25:00")
    agent2.respond(HAIKU, "D", "09:25:10", thinking=False, tools=(ok("Edit", CWD + "/src/coupons.py"), ok("Read", CWD + "/src/models.py")))

    main.result(agent_call, "Agent", "ok", "09:32:10")
    main.user(f"<task-notification>{CANARY} agent finished</task-notification>", "09:32:20", origin={"kind": "task-notification"}, promptSource="system")
    main.respond(OPUS, "B", "09:32:40", tools=(ok("Write", CWD + "/tests/test_coupons.py"),))
    main.respond(OPUS, "A", 40, tools=(ok("Bash", "pytest -q"),))
    main.respond(OPUS, "A", "09:34:10", text="coupons are tested", thinking=False)

    # ---- turn 3, after 48.5 minutes idle ---------------------------------------------------
    duplicated = main.prompt(f"{CANARY} document the coupons and tidy the README", "10:22:40")
    main.emit(duplicated)
    main.respond(OPUS, "A", "10:24:30", tools=(ok("Read", CWD + "/README.md"),))
    main.respond(OPUS, "A", 200, tools=(ok("Edit", CWD + "/README.md"),))
    main.respond(OPUS, "B", 220, tools=(ok("NotebookEdit", CWD + f"/notebooks/{CANARY}-analysis.ipynb"),))
    main.respond(OPUS, "A", 240, tools=(ok("Write", CWD + f"/docs/{CANARY}-coupons.md"),))
    main.respond(OPUS, "A", 200, tools=(("Bash", "git commit", "denied"),))
    main.respond(OPUS, "B", 240, tools=(ok("MultiEdit", CWD + "/tests/test_cart.py"),))
    main.respond(OPUS, "A", 230, tools=(("Bash", "pytest -q", "dup"),))
    main.respond(OPUS, "A", 200, text="docs and tests updated", thinking=False)

    # ---- turn 4 and 5 -----------------------------------------------------------------------
    main.user(f"This session is being continued from a previous conversation.\n{CANARY} summary", "10:53:00", isCompactSummary=True, isVisibleInTranscriptOnly=True)
    main.prompt(
        [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "RkFLRQ=="}},
         {"type": "text", "text": f"{CANARY} screenshot of the failing cart"}],
        "10:55:30",
    )
    main.respond(OPUS, "A", 200, tools=(ok("WebFetch"),))
    main.respond(HAIKU, "C", 200, thinking=False, tools=(ok("Bash", "git log"),))
    main.respond(OPUS, "B", 240, tools=(ok("Edit", CWD + "/src/coupons.py"),))
    main.respond(OPUS, "A", 240, tools=(ok("Read", CWD + "/src/cart.py"), ok("Read", CWD + "/src/models.py"), ok("Read", CWD + "/src/config.py")), style="identical")
    main.respond(OPUS, "A", 240, tools=(ok("Edit", CWD + "/tests/test_coupons.py"),))
    main.respond(OPUS, "A", 240, tools=(ok("Bash", "pytest -q"),))
    main.user([{"type": "text", "text": "[Request interrupted by user]"}], 200)
    main.synthetic(1)
    main.prompt(f"{CANARY} continue", "11:25:00")
    main.respond(OPUS, "A", "11:27:20", text="all done", thinking=False)

    # ---- trailer: records that are not conversation events (their timestamps must not count) --
    main.other({"type": "system", "subtype": "turn_duration", "durationMs": 1234, "timestamp": stamp(seconds("12:30:00"), 0), "uuid": fx.uid(), "cwd": CWD})
    main.other({"type": "ai-title", "aiTitle": f"{CANARY} coupons"})
    main.other({"type": "custom-title", "customTitle": f"{CANARY} coupons"})
    main.other({"type": "last-prompt", "lastPrompt": f"{CANARY} continue", "leafUuid": main.parent})
    main.other({"type": "cost-state", **cost_state(fx)})
    return main, agent1, agent2


def cost_state(fx: Fixture) -> dict[str, Any]:
    """Claude's own per-model totals: what was recorded plus the calls a transcript never shows."""
    def entry(totals: list[int]) -> dict[str, Any]:
        inp, cache_read, cache_write, out = totals
        return {"inputTokens": inp, "outputTokens": out, "cacheReadInputTokens": cache_read,
                "cacheCreationInputTokens": cache_write, "webSearchRequests": 0, "costUSD": 0, "thinkingTokens": 0}

    haiku = [a + b for a, b in zip(fx.totals[HAIKU], UNRECORDED_HAIKU, strict=True)]
    return {
        "totalCostUSD": 0, "totalAPIDuration": 0, "totalAPIDurationWithoutRetries": 0, "totalToolDuration": 0,
        "totalLinesAdded": 0, "totalLinesRemoved": 0, "totalDuration": 0, "startTime": 1790586000000,
        "modelUsage": {OPUS + "[1m]": entry(fx.totals[OPUS]), HAIKU: entry(haiku)}, "hasUnknownModelCost": True,
    }


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(out: Path) -> None:
    """Write ``projects/`` under ``out``, replacing what was there."""
    projects = out / "projects"
    shutil.rmtree(projects, ignore_errors=True)
    main, agent1, agent2 = build_logs(Fixture())
    session_dir = projects / PROJECT_DIR / SESSION
    write_lines(projects / PROJECT_DIR / f"{SESSION}.jsonl", main.lines)
    for path, log in (
        (session_dir / "subagents" / f"agent-{AGENT_1}.jsonl", agent1),
        (session_dir / "subagents" / "workflows" / "wf_demo-001" / f"agent-{AGENT_2}.jsonl", agent2),
    ):
        write_lines(path, log.lines)
        path.with_suffix(".meta.json").write_text(json.dumps({"agentType": "Explore", "description": f"{CANARY} survey"}) + "\n", encoding="utf-8")
    write_lines(session_dir / "tool-results" / "bqx1z9k2.txt", [f"{CANARY} spilled tool output"])


if __name__ == "__main__":
    build(Path(__file__).resolve().parent)

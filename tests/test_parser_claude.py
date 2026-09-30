"""Claude Code parser: discovery, signature, and the reduction of a session to a SessionRecord.

How the fixture was built
-------------------------
``tests/fixtures/claude/projects`` holds ONE synthetic session in Claude Code's real on-disk layout::

    projects/-home-dev-acme-shop/<uuid>.jsonl                          main transcript
    projects/-home-dev-acme-shop/<uuid>/subagents/agent-*.jsonl        sub-agent transcript
    projects/-home-dev-acme-shop/<uuid>/subagents/workflows/wf_*/...   workflow sub-agent transcript
    projects/-home-dev-acme-shop/<uuid>/tool-results/*.txt             spilled tool output (not a session)

JSONL cannot carry comments, so the files are written by ``tests/fixtures/claude/build_fixture.py``, a
deterministic script (no clock, no randomness) whose docstring tells the story of the session minute by
minute. Record shapes were copied from real logs (key names and types only, never content); every text
field is fake and holds ``PRIVACY-CANARY-CLAUDE``. To change the fixture edit the script, run it, and
update the numbers below; ``test_fixture_is_what_the_generator_writes`` fails when they drift apart.
Everything else (garbage lines, empty files, prompts rules, project names...) is built in ``tmp_path``.
"""

# =========================== Hand-computed expectations for the fixture ===========================
# Usage of ONE API response, by class (input / cache_read / cache_write / output; cache_write = 5-minute writes):
#   A opus   1,200 / 80,000 / 3,000 /   900   plus 2,000 one-hour writes (5,000 written in all, both TTLs in one response)
#   B opus     400 / 120,000 /    0 / 3,000
#   C haiku    500 / 20,000 / 2,000 /   200   its records have no cache_creation object (older Claude Code): all 5-minute
#   D haiku    800 / 15,000 /    0 /   400   plus 3,000 one-hour writes
# Responses (a response is one message.id however many records it was streamed as):
#   main file    29 x A + 5 x B (opus) and 3 x C (haiku)                                     = 37
#   sub-agents    2 x A (opus) and 4 x D (haiku), of which 1 x D is in the workflow agent     =  6
# Per-model totals, main + sub-agents:
#   claude-opus-5-5             input 31*1,200 + 5*400           =    39,200
#                               cache_read 31*80,000 + 5*120,000 = 3,080,000
#                               cache_write 31*3,000             =    93,000
#                               cache_write_1h 31*2,000          =    62,000   (155,000 written in all)
#                               output 31*900 + 5*3,000          =    42,900
#   claude-haiku-4-5-20251001   input 3*500 + 4*800             =     4,700
#                               cache_read 3*20,000 + 4*15,000  =   120,000
#                               cache_write 3*2,000             =     6,000
#                               cache_write_1h 4*3,000          =    12,000   (18,000 written in all)
#                               output 3*200 + 4*400            =     2,200
# Main file alone (what a parser that ignored sub-agents would report):
#   opus  39,200-2*1,200 = 36,800 / 3,080,000-2*80,000 = 2,920,000 / 93,000-2*3,000 = 87,000 / 42,900-2*900 = 41,100
#         / 62,000-2*2,000 = 58,000 one-hour writes
#   haiku 3 x C = 1,500 / 60,000 / 6,000 / 600 / 0 one-hour writes (every D response is in a sub-agent)
# Tool calls, distinct tool_use ids, by tool:
#   main       Read 9, Bash 11, Edit 8, Write 3, Grep 1, Glob 1, Agent 1, NotebookEdit 1, MultiEdit 1, WebFetch 1  = 37
#   agent a1   Grep 1, Read 2, Bash 2, Write 1, Edit 1 = 7;    agent a2   Edit 1, Read 1 = 2;              total    = 46
# files_touched = distinct paths given to Edit/Write/MultiEdit/NotebookEdit:
#   main: src/cart.py (Edit x2), src/coupons.py (Write, Edit x2), src/models.py, src/config.py (cwd changed to src/),
#         tests/test_coupons.py (Write, Edit), README.md, notebooks/<canary>-analysis.ipynb (notebook_path),
#         docs/<canary>-coupons.md, tests/test_cart.py (MultiEdit)                                               = 9
#   sub-agents add src/utils.py (new); their edits of src/cart.py and src/coupons.py are repeats                = 10
# error_retries = distinct tool_result blocks with is_error true, except the ones the user or a permission rule
#   refused (their record has toolDenialKind): 2 failing pytest runs + a failing compileall + a pytest run whose
#   result was written twice (counts once) + 1 failing pytest in agent a1                                          = 5
#   Not counted: a user-rejected git commit (turn 3) and a permission-rule npm publish (agent a1); both are errors
#   on the wire (is_error true) but not model failures. The main file alone has 4.
# turns = distinct non-synthetic message ids: 37 + 5 (agent a1) + 1 (agent a2)                                   = 43
# prompts = 5 typed prompts: 09:00:00, 09:13:30 (with an IDE context block), 10:22:40 (written twice, counts once),
#   10:55:30 (image + text), 11:25:00. Not prompts: skill body (isMeta), /context echo and its stdout, task
#   notification, compact summary, the interrupt marker, every tool_result carrier, the sub-agents' first messages.
# Time (UTC, whole seconds): started 09:00:00, ended 11:27:20 -> wall = 2h27m20s = 8,840 s.
#   The only gap over 30 minutes is 09:34:10 -> 10:22:40 = 48m30s = 2,910 s   => active = 8,840 - 2,910 = 5,930 s.
#   The main file alone is also silent 09:20:10 -> 09:32:10 (720 s) while sub-agents work; every other gap is <= 240 s.
#   idle gap 600 s : merged 5,930 s;  main file alone 5,930 - 720 = 5,210 s.   idle gap 3,000 s: everything = 8,840 s.
# Slots (UTC 15-minute slot of the LAST record of each response, sub-agents by their own timestamps): 43 responses in 8 slots,
#   class counts  A, B, C, D per slot:  09:00 13/1/1/0   09:15 4/0/1/4   09:30 3/1/0/0   10:15 2/0/0/0
#                                      10:30 2/2/0/0   10:45 3/0/0/0   11:00 2/1/1/0   11:15 2/0/0/0      (31 A, 5 B, 3 C, 4 D)
#   Nothing is spent in 09:45-10:14 (the idle gap) or 11:30 on, and everything is on 2026-09-28.
# Router signals: input incl. cache 43,900 + 3,200,000 + 173,000 (99,000 + 74,000 one-hour) = 3,416,900, 46 tool calls,
#   10 files, 5 errors, 98.8 active minutes.
# NOT counted although present in the files: replayed records of ANOTHER session (all buckets 9,999,999), once
#   under that session's own sessionId and once from a fork that rewrote sessionId (session_id names the parent),
#   one <synthetic> message, the partial output_tokens (2) of early streamed records, an attachment at 08:00:00,
#   a queue-operation at 08:59:30, a system record at 12:30:00 and an unknown record type at 12:59:00.
# Also present and ignored: 1 malformed line, 1 unknown record type, 1 cost-state record (cross-check only).
# ==========================================================================================================

from __future__ import annotations

import importlib.util
import itertools
import json
import random
import shutil
from collections.abc import Iterable
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agentfare.models import SessionRecord, Usage, slot_of
from agentfare.parsers import get_parser
from agentfare.parsers.claude import PARSER

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
PROJECTS = FIXTURES / "projects"
PROJECT = PROJECTS / "-home-dev-acme-shop"
SESSION = "2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402"
MAIN = PROJECT / f"{SESSION}.jsonl"
SESSION_DIR = PROJECT / SESSION
AGENT_1 = SESSION_DIR / "subagents" / "agent-a1b2c3d4e5f60789.jsonl"
AGENT_2 = SESSION_DIR / "subagents" / "workflows" / "wf_demo-001" / "agent-b2c3d4e5f6071890.jsonl"
CANARY = "PRIVACY-CANARY-CLAUDE"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-4-5-20251001"

FULL_USAGE = {
    OPUS: Usage(39_200, 3_080_000, 93_000, 42_900, cache_write_1h=62_000),
    HAIKU: Usage(4_700, 120_000, 6_000, 2_200, cache_write_1h=12_000),
}
MAIN_ONLY_USAGE = {OPUS: Usage(36_800, 2_920_000, 87_000, 41_100, cache_write_1h=58_000), HAIKU: Usage(1_500, 60_000, 6_000, 600)}


def parse(path: Path, idle: int | None = None) -> list[SessionRecord]:
    return PARSER.parse(path) if idle is None else PARSER.parse(path, idle_gap_seconds=idle)


def parse_one(path: Path, idle: int | None = None) -> SessionRecord:
    (record,) = parse(path, idle)
    return record


def dump(rec: SessionRecord) -> str:
    """The record as JSON text (``slots`` is keyed by (slot, model) pairs, which JSON cannot hold)."""
    plain = asdict(rec)
    plain["slots"] = {f"{slot} {model}": used for (slot, model), used in plain["slots"].items()}
    return json.dumps(plain)


def fixture_records(path: Path) -> list[dict[str, Any]]:
    """The well-formed lines of a fixture file (the main one holds a deliberately truncated line)."""
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    return records


@pytest.fixture(scope="module")
def record() -> SessionRecord:
    return parse_one(MAIN)


# ---- builders for small synthetic sessions ----------------------------------------------------------
SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BASE = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
_ids = itertools.count(1)


def ts(offset: float = 0) -> str:
    return (BASE + timedelta(seconds=offset)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def user(content: Any = "do the thing", at: float = 0, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "uuid": f"u{next(_ids)}", "timestamp": ts(at), "cwd": "/w/proj", "sessionId": SID,
            "message": {"role": "user", "content": content}, **extra}


def assistant(mid: str, usage: tuple[int, int, int, int] = (10, 200, 30, 40), at: float = 0, *, model: str = OPUS,
              content: Any = None, **extra: Any) -> dict[str, Any]:
    inp, cache_read, cache_write, out = usage
    message = {"model": model, "id": mid, "role": "assistant",
               "content": content if content is not None else [{"type": "text", "text": "ok"}],
               "usage": {"input_tokens": inp, "cache_read_input_tokens": cache_read,
                         "cache_creation_input_tokens": cache_write, "output_tokens": out}}
    return {"type": "assistant", "uuid": f"a{next(_ids)}", "timestamp": ts(at), "cwd": "/w/proj", "sessionId": SID,
            "message": message, **extra}


def tool_use(tool_id: str, name: str, **tool_input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def tool_result(tool_id: str, *, error: Any = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id, "content": "x"}
    if error is not None:
        block["is_error"] = error
    return block


def write_session(where: Path, records: Iterable[Any], session: str = SID, folder: str = "-w-proj") -> Path:
    """JSONL file; dict records are dumped, str/bytes items are written verbatim (for garbage lines)."""
    path = where / "projects" / folder / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        for rec in records:
            raw = rec if isinstance(rec, bytes) else rec.encode() if isinstance(rec, str) else json.dumps(rec).encode()
            fh.write(raw + b"\n")
    return path


def parse_records(tmp_path: Path, records: Iterable[Any], idle: int | None = None) -> SessionRecord:
    return parse_one(write_session(tmp_path, records), idle)


# ---- protocol and discovery -------------------------------------------------------------------------
def test_parser_is_registered_for_claude() -> None:
    assert PARSER.agent == "claude"
    assert get_parser("claude") is PARSER


def test_discover_yields_only_the_session_file() -> None:
    assert list(PARSER.discover(PROJECTS)) == [MAIN]  # root = the projects dir
    assert list(PARSER.discover(PROJECT)) == [MAIN]  # root = one project dir


def test_discover_skips_subagent_tool_result_and_workflow_folders(tmp_path: Path) -> None:
    first = write_session(tmp_path, [user()], SID)
    second = write_session(tmp_path, [user()], "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee", folder="-w-other")
    session_dir = first.parent / SID
    for skipped in (
        session_dir / "subagents" / "agent-1.jsonl",
        session_dir / "subagents" / "workflows" / "wf_1" / "agent-2.jsonl",
        session_dir / "tool-results" / "spill.jsonl",
        session_dir / "workflows" / "run.jsonl",
    ):
        skipped.parent.mkdir(parents=True, exist_ok=True)
        skipped.write_text(json.dumps(user()) + "\n")
    (first.parent / "notes.md").write_text("not a log")
    assert sorted(PARSER.discover(tmp_path)) == sorted([first, second])


def test_discover_of_a_missing_or_empty_root_is_empty(tmp_path: Path) -> None:
    assert list(PARSER.discover(tmp_path)) == []
    assert list(PARSER.discover(tmp_path / "missing")) == []


# ---- the fixture session ----------------------------------------------------------------------------
def test_identity_and_project(record: SessionRecord) -> None:
    assert record.agent == "claude"
    assert record.session_id == SESSION
    assert record.key == f"claude:{SESSION}"
    assert record.source_path == str(MAIN)
    # the first cwd wins: the session later moves to /home/dev/acme-shop/src, whose basename is not the project
    assert record.project == "acme-shop"


def test_time_window(record: SessionRecord) -> None:
    assert record.started_at == "2026-09-28T09:00:00Z"
    assert record.ended_at == "2026-09-28T11:27:20Z"
    assert record.wall_seconds == 8840
    assert record.active_seconds == 5930  # default idle gap of 1800 s removes only the 2,910 s gap


@pytest.mark.parametrize(("idle", "active"), [(600, 5930), (1800, 5930), (3000, 8840)])
def test_active_seconds_for_other_idle_gaps(idle: int, active: int) -> None:
    assert parse_one(MAIN, idle).active_seconds == active


def test_per_model_usage_is_exact(record: SessionRecord) -> None:
    assert record.usage == FULL_USAGE
    assert record.total_usage == Usage(43_900, 3_200_000, 99_000, 45_100, cache_write_1h=74_000)
    assert record.total_usage.total_input == 3_416_900
    assert record.primary_model == OPUS


def test_counters(record: SessionRecord) -> None:
    assert record.tool_calls == 46
    assert record.files_touched == 10
    assert record.error_retries == 5
    assert record.turns == 43
    assert record.prompts == 5


def test_fixture_signals_for_the_router(record: SessionRecord) -> None:
    """The five measurements the router scores (the thresholds are the router's business, not the fixture's)."""
    assert record.total_usage.total_input == 3_416_900  # input + cache reads + both kinds of cache writes
    assert (record.tool_calls, record.files_touched, record.error_retries) == (46, 10, 5)
    assert record.active_seconds == 5930  # 98.8 minutes


def test_synthetic_messages_are_neither_usage_nor_turns(record: SessionRecord) -> None:
    assert "<synthetic>" not in record.usage
    lines = MAIN.read_text().splitlines()
    assert sum('"model":"<synthetic>"' in line for line in lines) == 1  # present in the file, absent from the record


def test_subagents_are_merged_into_the_parent(tmp_path: Path, record: SessionRecord) -> None:
    main_only = parse_one(write_session(tmp_path, MAIN.read_text().splitlines(), SESSION))  # copy without <uuid>/subagents
    assert main_only.usage == MAIN_ONLY_USAGE
    assert (main_only.tool_calls, main_only.files_touched, main_only.error_retries, main_only.turns) == (37, 9, 4, 37)
    assert (record.tool_calls, record.files_touched, record.error_retries, record.turns) == (46, 10, 5, 43)
    assert main_only.prompts == record.prompts == 5  # a sub-agent's first message is not a human prompt
    assert (main_only.started_at, main_only.ended_at) == (record.started_at, record.ended_at)


def test_subagent_work_bridges_a_silent_stretch_of_the_main_file(tmp_path: Path) -> None:
    main_only = parse_one(write_session(tmp_path, MAIN.read_text().splitlines(), SESSION), idle=600)
    assert main_only.active_seconds == 5210  # 5,930 minus the 720 s the main file waited for its sub-agent
    assert parse_one(MAIN, idle=600).active_seconds == 5930


def test_replayed_history_of_another_session_is_ignored(record: SessionRecord) -> None:
    text = MAIN.read_text()
    assert '"sessionId":"9d1b7e40-55c2-4f0a-8a36-0c4be2f7a915"' in text and "9999999" in text
    forked = [rec for rec in fixture_records(MAIN) if rec.get("session_id") == "9d1b7e40-55c2-4f0a-8a36-0c4be2f7a915"]
    assert forked and {rec["sessionId"] for rec in forked} == {SESSION}  # the fork variant: copied under this session's own id
    assert record.started_at.startswith("2026-09-28") and record.project == "acme-shop"
    assert max(u.total for u in record.usage.values()) < 9_999_999


def test_replay_filter_needs_a_uuid_named_file(tmp_path: Path) -> None:
    records = [user(at=0), assistant("m1", at=5)]
    renamed = write_session(tmp_path, records, session="my-exported-session")  # records name another session id
    assert parse_one(renamed).usage[OPUS] == Usage(10, 200, 30, 40)
    uuid_named = write_session(tmp_path, records, session="bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    assert parse(uuid_named) == []  # all records replay another session: nothing of this one is left


def test_a_fork_that_rewrote_session_id_is_recognised_by_the_session_id_field(tmp_path: Path) -> None:
    """Measured: a forked file held 273 copied records under its own ``sessionId`` and the parent's ``session_id``."""
    parent = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    copied = [{**user("old prompt", at=-20), "session_id": parent},
              {**assistant("m_old", (900, 900, 900, 900), at=-10, content=[tool_use("t_old", "Write", file_path="/old")]), "session_id": parent},
              {**user([tool_result("t_old", error=True)], at=-5), "session_id": parent}]
    own = [{**user(at=0), "session_id": SID}, {**assistant("m1", at=5), "session_id": SID}, assistant("m2", (1, 1, 1, 1), at=6)]
    rec = parse_records(tmp_path, [*copied, *own])
    assert rec.usage == {OPUS: Usage(11, 201, 31, 41)}
    assert (rec.turns, rec.prompts, rec.tool_calls, rec.files_touched, rec.error_retries) == (2, 1, 0, 0, 0)
    assert (rec.started_at, rec.wall_seconds) == ("2026-09-28T10:00:00Z", 6)
    assert parse_one(write_session(tmp_path, copied, session="my-exported-session")).usage[OPUS] == Usage(900, 900, 900, 900)


def test_cost_state_is_only_a_cross_check(record: SessionRecord) -> None:
    (cost_state,) = [json.loads(line) for line in MAIN.read_text().splitlines() if '"type":"cost-state"' in line]
    reported = {model.split("[")[0]: u for model, u in cost_state["modelUsage"].items()}  # claude-opus-5-5[1m]
    as_tokens = {m: Usage(u["inputTokens"], u["cacheReadInputTokens"], u["cacheCreationInputTokens"], u["outputTokens"]) for m, u in reported.items()}
    written = {m: replace(u, cache_write=u.cache_write + u.cache_write_1h, cache_write_1h=0) for m, u in record.usage.items()}
    assert as_tokens[OPUS] == written[OPUS]  # transcript and cost-state agree (cost-state has no TTL split)...
    unrecorded = Usage(1850, 0, 0, 31)  # ...except for calls a transcript never shows (here a title generation)
    assert as_tokens[HAIKU] == written[HAIKU] + unrecorded
    assert "cost-state" not in dump(record)


# ---- de-duplication of streamed responses -----------------------------------------------------------
@pytest.mark.parametrize(
    "outputs",
    [(2, 2, 391), (391, 2, 2), (391, 391, 391), (2, 391)],
    ids=["growing", "growing-reversed", "identical", "two-records"],
)
def test_streamed_records_of_one_response_count_once_with_their_final_usage(tmp_path: Path, outputs: tuple[int, ...]) -> None:
    records = [assistant("msg_1", (5, 100, 20, out), at=i) for i, out in enumerate(outputs)]
    rec = parse_records(tmp_path, records)
    assert rec.usage == {OPUS: Usage(5, 100, 20, 391)}
    assert rec.turns == 1


def test_each_bucket_keeps_its_maximum_not_the_last_record(tmp_path: Path) -> None:
    rec = parse_records(tmp_path, [assistant("msg_1", (10, 500, 0, 5)), assistant("msg_1", (10, 400, 100, 300), at=1)])
    assert rec.usage == {OPUS: Usage(10, 500, 100, 300)}


def test_distinct_responses_add_up_per_model(tmp_path: Path) -> None:
    records = [assistant("m1", (1, 2, 3, 4)), assistant("m2", (10, 20, 30, 40), at=1), assistant("m3", (7, 7, 7, 7), at=2, model=HAIKU)]
    rec = parse_records(tmp_path, records)
    assert rec.usage == {OPUS: Usage(11, 22, 33, 44), HAIKU: Usage(7, 7, 7, 7)}
    assert rec.turns == 3


def test_a_response_without_id_falls_back_to_request_id_then_counts_alone(tmp_path: Path) -> None:
    by_request = [assistant("", at=i, requestId="req_1") for i in range(2)]
    unidentified = [assistant("", at=5), assistant("", at=6)]
    assert parse_records(tmp_path, by_request).turns == 1
    assert parse_records(tmp_path, unidentified).turns == 2


def test_synthetic_model_is_skipped_whatever_it_reports(tmp_path: Path) -> None:
    records = [assistant("m1"), assistant("m2", (999, 999, 999, 999), at=1, model="<synthetic>")]
    rec = parse_records(tmp_path, records)
    assert rec.usage == {OPUS: Usage(10, 200, 30, 40)}
    assert rec.turns == 1


def test_a_response_billed_zero_tokens_is_a_turn_but_not_a_model(tmp_path: Path) -> None:
    rec = parse_records(tmp_path, [assistant("m1", (0, 0, 0, 0)), assistant("m2", (1, 1, 1, 1), at=1, model=HAIKU)])
    assert rec.usage == {HAIKU: Usage(1, 1, 1, 1)}
    assert rec.turns == 2


# ---- usage.iterations -------------------------------------------------------------------------------
def entry(usage: tuple[int, int, int, int], kind: str = "message", **extra: Any) -> dict[str, Any]:
    inp, cache_read, cache_write, out = usage
    return {"type": kind, "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write, **extra}


def with_iterations(rec: dict[str, Any], iterations: Any) -> dict[str, Any]:
    rec["message"]["usage"]["iterations"] = iterations
    return rec


def test_a_response_whose_top_level_counters_are_zero_is_billed_from_its_iterations(tmp_path: Path) -> None:
    """Measured: 3 records of one response, top level all 0, iterations 2 / 964,720 / 660 / 1,147."""
    billed = (2, 964_720, 660, 1_147)
    records = [with_iterations(assistant("m1", (0, 0, 0, 0), at=i), [entry(billed, model=None)]) for i in range(3)]
    rec = parse_records(tmp_path, records)
    assert rec.usage == {OPUS: Usage(*billed)}
    assert rec.turns == 1


def test_iterations_that_repeat_the_top_level_counters_are_not_added_to_them(tmp_path: Path) -> None:
    rec = parse_records(tmp_path, [with_iterations(assistant("m1", (10, 200, 30, 40)), [entry((10, 200, 30, 40))])])
    assert rec.usage == {OPUS: Usage(10, 200, 30, 40)}


def test_iterations_reporting_less_than_the_top_level_never_lower_it(tmp_path: Path) -> None:
    partial = entry((10, 100, 0, 5))  # each bucket keeps its own maximum, like the streamed records do
    rec = parse_records(tmp_path, [with_iterations(assistant("m1", (10, 200, 30, 40)), [partial])])
    assert rec.usage == {OPUS: Usage(10, 200, 30, 40)}


def test_every_iteration_of_the_model_is_billed_even_when_the_top_level_leaves_one_out(tmp_path: Path) -> None:
    """The top level counts the reply only, ``iterations`` also lists a compaction call of the same model."""
    both = [entry((50, 300_000, 0, 2_000), "compaction"), entry((10, 200, 30, 40))]
    rec = parse_records(tmp_path, [with_iterations(assistant("m1", (10, 200, 30, 40)), both)])
    assert rec.usage == {OPUS: Usage(60, 300_200, 30, 2_040)}


def test_an_iteration_served_by_another_model_is_not_added_to_this_one(tmp_path: Path) -> None:
    """Measured: a ``fallback_message`` retry; the record and its top level are the fallback's."""
    tried = entry((2, 127_128, 3_657, 579), model=HAIKU)
    served = entry((2, 95_500, 0, 4_047), "fallback_message", model=OPUS)
    rec = parse_records(tmp_path, [with_iterations(assistant("m1", (2, 95_500, 0, 4_047)), [tried, served])])
    assert rec.usage == {OPUS: Usage(2, 95_500, 0, 4_047)}


@pytest.mark.parametrize("iterations", [None, "many", {}, [None, 3, "x", []], [{"type": "message", "input_tokens": "9"}]])
def test_malformed_iterations_fall_back_to_the_top_level_counters(tmp_path: Path, iterations: Any) -> None:
    rec = parse_records(tmp_path, [with_iterations(assistant("m1", (10, 200, 30, 40)), iterations)])
    assert rec.usage == {OPUS: Usage(10, 200, 30, 40)}


def test_iterations_of_streamed_records_keep_the_per_bucket_maximum(tmp_path: Path) -> None:
    early = with_iterations(assistant("m1", (10, 200, 30, 2), at=0), None)  # the message_start snapshot has none
    final = with_iterations(assistant("m1", (10, 200, 30, 40), at=1), [entry((10, 200, 30, 40))])
    assert parse_records(tmp_path, [early, final]).usage == {OPUS: Usage(10, 200, 30, 40)}
    assert parse_records(tmp_path / "reversed", [final, early]).usage == {OPUS: Usage(10, 200, 30, 40)}


# ---- cache writes by TTL ----------------------------------------------------------------------------
def ttl(five_minute: int, one_hour: int) -> dict[str, int]:
    return {"ephemeral_5m_input_tokens": five_minute, "ephemeral_1h_input_tokens": one_hour}


def with_split(rec: dict[str, Any], split: Any) -> dict[str, Any]:
    """``usage.cache_creation`` of the response; ``...`` removes it (older logs never had it)."""
    if split is ...:
        rec["message"]["usage"].pop("cache_creation", None)
    else:
        rec["message"]["usage"]["cache_creation"] = split
    return rec


@pytest.mark.parametrize(
    ("split", "expected"),
    [
        pytest.param(..., (30, 0), id="no-split-object-all-5m"),
        pytest.param(ttl(30, 0), (30, 0), id="5m-only"),
        pytest.param(ttl(0, 30), (0, 30), id="1h-only"),
        pytest.param(ttl(10, 20), (10, 20), id="both"),
        pytest.param({"ephemeral_1h_input_tokens": 20}, (10, 20), id="only-the-1h-key"),
        pytest.param({"ephemeral_5m_input_tokens": 30}, (30, 0), id="only-the-5m-key"),
        pytest.param(ttl(10, 5), (25, 5), id="parts-below-the-total-the-rest-is-5m"),
        pytest.param(ttl(0, 50), (0, 30), id="1h-above-the-total-is-capped"),
        pytest.param(ttl(50, 0), (30, 0), id="5m-above-the-total-changes-nothing"),
        pytest.param(ttl(0, 0), (30, 0), id="empty-split-all-5m"),
        pytest.param({"ephemeral_1h_input_tokens": "20"}, (30, 0), id="text-count"),
        pytest.param({"ephemeral_1h_input_tokens": -5}, (30, 0), id="negative-count"),
        pytest.param({"ephemeral_1h_input_tokens": None}, (30, 0), id="null-count"),
        pytest.param(None, (30, 0), id="null-split"),
        pytest.param("many", (30, 0), id="text-split"),
        pytest.param([20], (30, 0), id="list-split"),
    ],
)
def test_the_split_only_says_how_much_of_the_written_total_was_1_hour(tmp_path: Path, split: Any, expected: tuple[int, int]) -> None:
    rec = parse_records(tmp_path, [with_split(assistant("m1", (10, 200, 30, 40)), split)])
    assert rec.usage == {OPUS: Usage(10, 200, expected[0], 40, expected[1])}


@pytest.mark.parametrize("split", [ttl(3_657, 0), ttl(0, 660)])
def test_a_split_that_names_writes_the_total_does_not_have_invents_none(tmp_path: Path, split: dict[str, int]) -> None:
    """Measured: 4 records whose flat total is 0 but whose split is not (a fallback retry and a zero-counter response)."""
    rec = parse_records(tmp_path, [with_split(assistant("m1", (10, 200, 0, 40)), split)])
    assert rec.usage == {OPUS: Usage(10, 200, 0, 40)}


def test_the_split_adds_up_across_responses_and_models(tmp_path: Path) -> None:
    records = [with_split(assistant("m1", (1, 2, 30, 4), at=0), ttl(10, 20)), with_split(assistant("m2", (1, 2, 30, 4), at=1), ttl(30, 0)),
               with_split(assistant("m3", (1, 2, 30, 4), at=2), ...), with_split(assistant("m4", (1, 2, 5, 4), at=3, model=HAIKU), ttl(0, 5))]
    rec = parse_records(tmp_path, records)
    assert rec.usage == {OPUS: Usage(3, 6, 10 + 30 + 30, 12, cache_write_1h=20), HAIKU: Usage(1, 2, 0, 4, cache_write_1h=5)}
    assert rec.total_usage.total_input == 3 + 6 + 90 + 1 + 2 + 5 == 107 and rec.total_usage.total == 107 + 16


def test_iterations_that_carry_the_split_bill_it_when_the_top_level_is_empty(tmp_path: Path) -> None:
    """Measured: the top level says 0 written (with a split that names 660), its iterations name 660 with the same split."""
    billed = (2, 964_720, 660, 1_147)
    records = [with_iterations(with_split(assistant("m1", (0, 0, 0, 0), at=i), ttl(0, 660)), [entry(billed, model=None, cache_creation=ttl(0, 660))]) for i in range(3)]
    assert parse_records(tmp_path, records).usage == {OPUS: Usage(2, 964_720, 0, 1_147, 660)}


def test_iterations_that_repeat_the_top_level_split_are_not_added_to_it(tmp_path: Path) -> None:
    both = with_iterations(with_split(assistant("m1", (10, 200, 30, 40)), ttl(10, 20)), [entry((10, 200, 30, 40), cache_creation=ttl(10, 20))])
    assert parse_records(tmp_path, [both]).usage == {OPUS: Usage(10, 200, 10, 40, 20)}


def test_every_iteration_of_the_model_adds_its_own_split(tmp_path: Path) -> None:
    both = [entry((50, 300_000, 40, 2_000), "compaction", cache_creation=ttl(0, 40)), entry((10, 200, 30, 40), cache_creation=ttl(30, 0))]
    rec = parse_records(tmp_path, [with_iterations(with_split(assistant("m1", (10, 200, 30, 40)), ttl(30, 0)), both)])
    assert rec.usage == {OPUS: Usage(60, 300_200, 30, 2_040, 40)}


@pytest.mark.parametrize("top_level", [ttl(10, 20), ...], ids=["split-at-the-top", "no-split-at-the-top"])
@pytest.mark.parametrize("in_iteration", [ttl(10, 20), ...], ids=["split-in-the-iteration", "no-split-in-the-iteration"])
def test_a_missing_split_in_one_place_never_counts_the_writes_twice(tmp_path: Path, top_level: Any, in_iteration: Any) -> None:
    iteration = entry((10, 200, 30, 40))
    if in_iteration is not ...:
        iteration["cache_creation"] = in_iteration
    rec = parse_records(tmp_path, [with_iterations(with_split(assistant("m1", (10, 200, 30, 40)), top_level), [iteration])])
    (usage,) = rec.usage.values()
    assert usage.cache_write + usage.cache_write_1h == 30
    assert usage.cache_write_1h == (20 if ttl(10, 20) in (top_level, in_iteration) else 0)


@pytest.mark.parametrize("order", [(0, 1), (1, 0)], ids=["split-last", "split-first"])
def test_a_streamed_copy_without_the_split_never_doubles_the_writes(tmp_path: Path, order: tuple[int, int]) -> None:
    early = with_split(assistant("m1", (10, 200, 30, 2), at=0), ...)  # the message_start snapshot of an old log has no split
    final = with_split(assistant("m1", (10, 200, 30, 40), at=1), ttl(10, 20))
    rec = parse_records(tmp_path, [(early, final)[i] for i in order])
    assert rec.usage == {OPUS: Usage(10, 200, 10, 40, 20)}


def test_the_records_of_one_response_keep_the_largest_total_and_the_largest_1h_part(tmp_path: Path) -> None:
    a = with_split(assistant("m1", (10, 500, 100, 5)), ttl(50, 50))
    b = with_split(assistant("m1", (10, 400, 60, 300), at=1), ttl(0, 60))
    assert parse_records(tmp_path, [a, b]).usage == {OPUS: Usage(10, 500, 40, 300, 60)}


def test_a_fallback_retry_does_not_take_the_first_attempts_writes(tmp_path: Path) -> None:
    """Measured: the top-level split of a ``fallback_message`` response named the other model's 3,657 written tokens."""
    tried = entry((2, 127_128, 3_657, 579), model=HAIKU, cache_creation=ttl(3_657, 0))
    served = entry((2, 95_500, 0, 4_047), "fallback_message", model=OPUS, cache_creation=ttl(0, 0))
    rec = parse_records(tmp_path, [with_iterations(with_split(assistant("m1", (2, 95_500, 0, 4_047)), ttl(3_657, 0)), [tried, served])])
    assert rec.usage == {OPUS: Usage(2, 95_500, 0, 4_047)}


# ---- tools, files and errors ------------------------------------------------------------------------
def test_tool_use_blocks_count_once_per_id_across_streamed_records(tmp_path: Path) -> None:
    block = tool_use("toolu_1", "Bash", command="ls")
    records = [assistant("m1", content=[block]), assistant("m1", content=[block], at=1), assistant("m2", content=[tool_use("toolu_2", "Read", file_path="/a")], at=2)]
    assert parse_records(tmp_path, records).tool_calls == 2


@pytest.mark.parametrize(
    ("name", "key", "counts"),
    [("Edit", "file_path", True), ("Write", "file_path", True), ("MultiEdit", "file_path", True), ("NotebookEdit", "notebook_path", True),
     ("Write", "path", True), ("Read", "file_path", False), ("Grep", "path", False), ("Bash", "command", False), ("mcp__fs__write", "file_path", False)],
)
def test_only_write_tools_touch_files(tmp_path: Path, name: str, key: str, counts: bool) -> None:
    rec = parse_records(tmp_path, [assistant("m1", content=[tool_use("t1", name, **{key: "/repo/a.py"})])])
    assert (rec.files_touched, rec.tool_calls) == (1 if counts else 0, 1)


@pytest.mark.parametrize("name", [None, 5, [], {}, ["Edit"]])
def test_a_tool_use_whose_name_is_not_a_string_is_a_call_that_touches_nothing(tmp_path: Path, name: Any) -> None:
    rec = parse_records(tmp_path, [assistant("m1", content=[{"type": "tool_use", "id": "t1", "name": name, "input": {"file_path": "/a"}}])])
    assert (rec.tool_calls, rec.files_touched) == (1, 0)


def test_files_touched_counts_distinct_paths(tmp_path: Path) -> None:
    calls = [tool_use(f"t{i}", "Edit", file_path=p) for i, p in enumerate(["/r/a.py", "/r/b.py", "/r/a.py", "/r/a.py"])]
    calls.append(tool_use("t9", "Write"))  # no path at all: still a tool call, touches nothing countable
    rec = parse_records(tmp_path, [assistant("m1", content=calls)])
    assert (rec.files_touched, rec.tool_calls) == (2, 5)


def test_errors_are_distinct_tool_results_flagged_is_error(tmp_path: Path) -> None:
    results = [tool_result("t1", error=True), tool_result("t1", error=True), tool_result("t2", error=False),
               tool_result("t3", error=None), tool_result("t4", error=True), tool_result("t5", error="yes")]
    records = [user([r], at=i, toolUseResult="Error: x") for i, r in enumerate(results)]
    assert parse_records(tmp_path, records).error_retries == 2  # t1 (once) and t4; "yes" is not a boolean true


DENIAL_KINDS = ["user-rejected", "permission-rule", "automode-blocked", "automode-unavailable"]


@pytest.mark.parametrize("kind", DENIAL_KINDS)
def test_a_tool_call_the_user_or_a_permission_rule_refused_is_not_an_error(tmp_path: Path, kind: str) -> None:
    """Measured: every refusal in the real logs is a one-result record with is_error true and a toolDenialKind."""
    refused = user([tool_result("t1", error=True)], toolUseResult="Error: no", toolDenialKind=kind)
    assert parse_records(tmp_path, [refused]).error_retries == 0


@pytest.mark.parametrize("kind", [None, "", 1])
def test_only_a_non_empty_text_marks_a_refusal(tmp_path: Path, kind: Any) -> None:
    failed = user([tool_result("t1", error=True)], toolUseResult="Error: boom", toolDenialKind=kind)
    assert parse_records(tmp_path, [failed]).error_retries == 1


def test_refusals_and_failures_are_told_apart_in_one_session(tmp_path: Path) -> None:
    records = [user([tool_result("t1", error=True)], at=1, toolUseResult="Error: no", toolDenialKind="permission-rule"),
               user([tool_result("t2", error=True)], at=2, toolUseResult="Error: boom"),
               user([tool_result("t3", error=True)], at=3, toolUseResult="Error: no", toolDenialKind="user-rejected"),
               user([tool_result("t4", error=True)], at=4, toolUseResult="Error: boom")]
    assert parse_records(tmp_path, records).error_retries == 2


def test_a_refusal_is_still_a_tool_call_and_a_prompt_is_not_made_of_it(tmp_path: Path) -> None:
    records = [user(at=0), assistant("m1", at=1, content=[tool_use("t1", "Bash", command="rm -rf /")]),
               user([tool_result("t1", error=True)], at=2, toolUseResult="Error: no", toolDenialKind="user-rejected")]
    rec = parse_records(tmp_path, records)
    assert (rec.tool_calls, rec.error_retries, rec.prompts) == (1, 0, 1)


def test_errors_inside_subagent_files_count_and_refusals_do_not(tmp_path: Path) -> None:
    main = write_session(tmp_path, [user(at=0)])
    sub = main.with_suffix("") / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir(parents=True)
    failed = user([tool_result("t1", error=True)], at=5, isSidechain=True)
    refused = user([tool_result("t2", error=True)], at=6, isSidechain=True, toolDenialKind="permission-rule")
    sub.write_text(json.dumps(failed) + "\n" + json.dumps(refused) + "\n")
    assert parse_one(main).error_retries == 1


def test_the_fixture_holds_refused_tool_calls_that_are_not_counted_as_errors(record: SessionRecord) -> None:
    refused: list[str] = []
    failed: set[str] = set()
    for path in (MAIN, AGENT_1, AGENT_2):
        for rec in fixture_records(path):
            content = (rec.get("message") or {}).get("content")
            for block in content if rec["type"] == "user" and isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block["is_error"]:
                    if "toolDenialKind" in rec:
                        refused.append(rec["toolDenialKind"])
                    else:
                        failed.add(block["tool_use_id"])
    assert sorted(refused) == ["permission-rule", "user-rejected"]  # one in the main file, one in a sub-agent
    assert len(failed) == record.error_retries == 5


# ---- prompts ----------------------------------------------------------------------------------------
TEXT = {"type": "text", "text": "please refactor the module"}
IDE = {"type": "text", "text": "<ide_opened_file>the user opened a.py</ide_opened_file>"}
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "RkFLRQ=="}}
PROMPT_CASES = [
    pytest.param(user("fix the bug", origin={"kind": "human"}, promptSource="typed"), 1, id="typed"),
    pytest.param(user("fix the bug"), 1, id="older-format-without-origin"),
    pytest.param(user([TEXT]), 1, id="text-blocks"),
    pytest.param(user([IMAGE]), 1, id="image-only"),
    pytest.param(user([IMAGE, TEXT]), 1, id="image-and-text"),
    pytest.param(user([IDE, TEXT]), 1, id="ide-context-plus-text"),
    pytest.param(user("<my-own-tag>pasted</my-own-tag> now fix it", origin={"kind": "human"}), 1, id="unknown-tag-is-human-text"),
    pytest.param(user([tool_result("t1")], toolUseResult={"stdout": "x"}), 0, id="tool-result-carrier"),
    pytest.param(user([tool_result("t1"), TEXT]), 0, id="tool-result-with-text"),
    pytest.param(user("skill body", isMeta=True), 0, id="meta"),
    pytest.param(user("This session is being continued", isCompactSummary=True), 0, id="compact-summary"),
    pytest.param(user("shown only in the transcript", isVisibleInTranscriptOnly=True), 0, id="transcript-only"),
    pytest.param(user("task for a sub-agent", isSidechain=True), 0, id="sidechain"),
    pytest.param(user("<task-notification>done</task-notification>", origin={"kind": "task-notification"}), 0, id="task-notification-origin"),
    pytest.param(user("hello from a peer", origin={"kind": "peer"}), 0, id="peer-origin"),
    pytest.param(user("<command-name>/clear</command-name>"), 0, id="slash-command-echo"),
    pytest.param(user("<command-message>clear</command-message>", origin={"kind": "human"}), 0, id="slash-command-message-with-human-origin"),
    pytest.param(user("<local-command-stdout>ok</local-command-stdout>"), 0, id="local-command-stdout"),
    pytest.param(user("<system-reminder>be brief</system-reminder>"), 0, id="system-reminder"),
    pytest.param(user("<bash-input>ls</bash-input>"), 0, id="bash-mode-input"),
    pytest.param(user([{"type": "text", "text": "<paseo-system>\nharness instructions\n</paseo-system>"}], promptSource="sdk"), 0, id="paseo-system-wrapper"),
    pytest.param(user("injected by the harness", promptSource="system"), 0, id="prompt-source-system"),
    pytest.param(user("fix the bug", promptSource="sdk"), 1, id="prompt-source-sdk-without-origin"),
    pytest.param(user("fix the bug", origin={"kind": "human"}, promptSource="queued"), 1, id="queued-while-the-agent-worked"),
    pytest.param(user("<task-notification>done</task-notification>"), 0, id="task-notification-without-origin"),
    pytest.param(user([IDE]), 0, id="ide-context-only"),
    pytest.param(user("[Request interrupted by user]"), 0, id="interrupt-marker"),
    pytest.param(user([{"type": "text", "text": "[Request interrupted by user for tool use]"}]), 0, id="interrupt-marker-block"),
    pytest.param(user("   \n"), 0, id="blank"),
    pytest.param(user([]), 0, id="empty-list"),
    pytest.param(user(None), 0, id="no-content"),
]


@pytest.mark.parametrize(("rec", "expected"), PROMPT_CASES)
def test_which_user_records_are_prompts(tmp_path: Path, rec: dict[str, Any], expected: int) -> None:
    assert parse_records(tmp_path, [rec]).prompts == expected


def test_a_prompt_written_twice_counts_once(tmp_path: Path) -> None:
    same = user("fix it")
    assert parse_records(tmp_path, [same, same]).prompts == 1


def test_prompts_in_subagent_files_are_not_counted(tmp_path: Path) -> None:
    main = write_session(tmp_path, [user("real prompt", at=0)])
    sub = main.with_suffix("") / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(json.dumps(user("task from the parent agent", at=5)) + "\n")  # even without isSidechain
    assert parse_one(main).prompts == 1


# ---- project names ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("cwd", "project"),
    [
        ("/home/dev/acme-shop", "acme-shop"),
        ("/home/dev/acme-shop/", "acme-shop"),
        ("C:\\Users\\dev\\proj", "proj"),
        ("C:\\Users\\dev\\proj\\", "proj"),
        ("/mnt/c/Users/dev/proj", "proj"),
        ("/repo/.claude/worktrees/feature-x", "repo"),
        ("C:\\code\\repo\\.claude\\worktrees\\feature-x\\sub", "repo"),
        ("/srv/code/shop-wt/feature-x", "shop"),
        ("D:\\code\\shop-wt-feature-x", "shop"),
        ("/home/dev/my-wt", "my-wt"),
        ("/", "unknown"),
        ("C:\\", "unknown"),
        ("", "unknown"),
    ],
)
def test_project_is_the_repo_basename_never_a_path(tmp_path: Path, cwd: str, project: str) -> None:
    rec = parse_records(tmp_path, [{**user(), "cwd": cwd}])
    assert rec.project == project
    assert "/" not in rec.project and "\\" not in rec.project


def test_project_without_any_cwd_is_unknown(tmp_path: Path) -> None:
    bare = user()
    del bare["cwd"]
    assert parse_records(tmp_path, [bare]).project == "unknown"


# ---- time -------------------------------------------------------------------------------------------
def test_events_are_cut_to_whole_seconds_so_streamed_records_do_not_eat_active_time(tmp_path: Path) -> None:
    records = [user(at=0.9)] + [assistant(f"m{i}", at=0.9 + i * 0.5) for i in range(1, 100)]  # 99 records, 0.5 s apart
    rec = parse_records(tmp_path, records)
    assert (rec.started_at, rec.ended_at) == ("2026-09-28T10:00:00Z", "2026-09-28T10:00:50Z")
    assert (rec.wall_seconds, rec.active_seconds) == (50, 50)


def test_timestamps_with_offsets_are_converted_to_utc(tmp_path: Path) -> None:
    first = {**user(), "timestamp": "2026-09-28T12:00:00+02:00"}
    second = {**assistant("m1"), "timestamp": "2026-09-28T10:00:30.500Z"}
    rec = parse_records(tmp_path, [first, second])
    assert (rec.started_at, rec.ended_at, rec.wall_seconds) == ("2026-09-28T10:00:00Z", "2026-09-28T10:00:30Z", 30)


def test_only_conversation_records_are_events(tmp_path: Path) -> None:
    extras = [{"type": kind, "sessionId": SID, "timestamp": ts(offset)} for kind, offset in
              [("attachment", -3600), ("system", 7200), ("queue-operation", -60), ("something-new", 9000)]]
    rec = parse_records(tmp_path, [user(at=0), assistant("m1", at=10), *extras])
    assert (rec.started_at, rec.ended_at) == ("2026-09-28T10:00:00Z", "2026-09-28T10:00:10Z")


# ---- robustness -------------------------------------------------------------------------------------
GARBAGE = [b"\x00\xff\xfe not utf-8 or json", b"null", b"[]", b"123", b'"a string"', b"{", b"}", b"", b"{}", b'{"type":"user"}',
           b'{"type":"assistant","message":"nope"}', b'{"type":"assistant","message":{"usage":[]}}']


def clean_records() -> list[dict[str, Any]]:
    return [user("hello", at=0), assistant("m1", at=5, content=[tool_use("t1", "Edit", file_path="/a")]),
            user([tool_result("t1", error=True)], at=9), assistant("m2", (1, 2, 3, 4), at=12)]


def test_garbage_lines_change_nothing(tmp_path: Path) -> None:
    baseline = parse_records(tmp_path / "clean", clean_records())
    noisy: list[Any] = []
    for rec in clean_records():
        noisy += [*GARBAGE, rec]
    noisy += GARBAGE
    dirty = parse_records(tmp_path / "dirty", noisy)
    assert {**asdict(dirty), "source_path": ""} == {**asdict(baseline), "source_path": ""}
    assert baseline.usage[OPUS] == Usage(11, 202, 33, 44)  # sanity: the baseline is not trivially empty


def test_a_truncated_last_line_is_dropped(tmp_path: Path) -> None:
    lines = [json.dumps(r) for r in clean_records()]
    path = write_session(tmp_path, lines)
    with path.open("ab") as fh:
        fh.write(json.dumps(assistant("m3", (999, 999, 999, 999), at=20)).encode()[:-25])  # cut mid-record, no newline
    assert parse_one(path).usage[OPUS] == Usage(11, 202, 33, 44)


def test_a_transcript_cut_inside_its_last_line_still_parses(tmp_path: Path) -> None:
    lines = MAIN.read_text().splitlines()
    cut = write_session(tmp_path, lines[:-1] + [lines[-1][:40]], SESSION)  # the last line is cost-state
    assert parse_one(cut).usage == MAIN_ONLY_USAGE


@pytest.mark.parametrize("content", [b"", b"\n\n\n", b"   \n"])
def test_empty_files_yield_no_session(tmp_path: Path, content: bytes) -> None:
    path = write_session(tmp_path, [])
    path.write_bytes(content)
    assert parse(path) == []


def test_a_file_without_conversation_records_yields_no_session(tmp_path: Path) -> None:
    other = [{"type": "ai-title", "aiTitle": "x", "sessionId": SID}, {"type": "future-thing", "timestamp": ts(0), "sessionId": SID}, *GARBAGE]
    assert parse(write_session(tmp_path, other)) == []


def test_missing_or_malformed_usage_is_not_billed_but_the_record_still_counts(tmp_path: Path) -> None:
    def response(mid: str, usage: Any, at: float) -> dict[str, Any]:
        rec = assistant(mid, at=at, content=[tool_use(f"t_{mid}", "Bash", command="ls")])
        if usage is ...:
            del rec["message"]["usage"]
        else:
            rec["message"]["usage"] = usage
        return rec

    records = [user(at=0), response("m1", ..., 1), response("m2", None, 2), response("m3", "lots", 3),
               response("m4", {"input_tokens": None, "output_tokens": "7", "cache_read_input_tokens": -5}, 4),
               response("m5", {"output_tokens": 12}, 5)]
    rec = parse_records(tmp_path, records)
    assert rec.usage == {OPUS: Usage(0, 0, 0, 12)}
    assert rec.tool_calls == 5  # blocks are counted whatever happened to the usage
    assert rec.turns == 2  # m4 has a usage object (all zero) and m5 a real one
    assert rec.ended_at == "2026-09-28T10:00:05Z"


def test_missing_message_model_or_content_shapes_do_not_crash(tmp_path: Path) -> None:
    odd = [
        {"type": "assistant", "timestamp": ts(1), "sessionId": SID},
        {"type": "assistant", "timestamp": ts(2), "sessionId": SID, "message": {"id": "x", "usage": {"output_tokens": 5}}},
        assistant("m1", at=3, content="just a string"),
        assistant("m2", at=4, content=[None, 7, "x", {"type": "tool_use"}, {"type": "tool_use", "id": 5, "name": "Edit"}]),
        user(["text", 3, None], at=5),
        {"type": "user", "timestamp": ts(6), "sessionId": SID, "message": {"content": 12}},
    ]
    rec = parse_records(tmp_path, [user(at=0), *odd])
    assert rec.usage == {OPUS: Usage(20, 400, 60, 80)}
    assert rec.tool_calls == 0
    assert rec.ended_at == "2026-09-28T10:00:06Z"


def test_bad_timestamps_only_drop_that_event(tmp_path: Path) -> None:
    records = [user(at=0), {**assistant("m1"), "timestamp": "yesterday-ish"}, {**assistant("m2"), "timestamp": 12345},
               {**assistant("m3"), "timestamp": "0001-01-01T00:00:00+05:00"}, assistant("m4", at=30),
               {k: v for k, v in assistant("m5", at=40).items() if k != "timestamp"}]
    rec = parse_records(tmp_path, records)
    assert (rec.started_at, rec.ended_at, rec.turns) == ("2026-09-28T10:00:00Z", "2026-09-28T10:00:30Z", 5)


def test_a_session_with_no_billed_usage_keeps_its_counters(tmp_path: Path) -> None:
    rec = parse_records(tmp_path, [user("just a prompt", at=0), user("another", at=20)])
    assert (rec.usage, rec.turns, rec.prompts, rec.wall_seconds) == ({}, 0, 2, 20)


# ---- signature --------------------------------------------------------------------------------------
def append(path: Path, rec: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_signature_is_stable_and_covers_every_subagent_file(tmp_path: Path) -> None:
    shutil.copytree(PROJECTS, tmp_path / "projects")
    main = next(PARSER.discover(tmp_path / "projects"))
    subagents = main.with_suffix("") / "subagents"
    before = PARSER.signature(main)
    assert PARSER.signature(main) == before

    seen = {before}
    for changed in (subagents / AGENT_1.name, subagents / "workflows" / "wf_demo-001" / AGENT_2.name, subagents / "agent-new.jsonl", main):
        append(changed, user(at=1, isSidechain=True))
        current = PARSER.signature(main)
        assert current not in seen, changed.name
        seen.add(current)


def test_signature_ignores_files_that_are_not_subagent_transcripts(tmp_path: Path) -> None:
    shutil.copytree(PROJECTS, tmp_path / "projects")
    main = next(PARSER.discover(tmp_path / "projects"))
    before = PARSER.signature(main)
    (main.with_suffix("") / "tool-results" / "more.txt").write_text("spill")
    (main.with_suffix("") / "subagents" / "agent-a1.meta.json").write_text("{}")
    assert PARSER.signature(main) == before


# ---- sessions that live in more than one project folder ---------------------------------------------
def test_subagents_are_collected_from_every_project_folder_of_the_session(tmp_path: Path) -> None:
    records = [user(at=0), assistant("m_main", (1, 1, 1, 1), at=1)]
    old = write_session(tmp_path, records, folder="-w-old")
    new = write_session(tmp_path, records, folder="-w-new")  # the session was copied when its worktree moved
    for main, mid, usage in ((old, "m_old", (10, 10, 10, 10)), (new, "m_new", (100, 100, 100, 100))):
        sub = main.with_suffix("") / "subagents" / f"agent-{mid}.jsonl"
        sub.parent.mkdir(parents=True)
        append(sub, assistant(mid, usage, at=2, isSidechain=True))
    assert parse_one(old).usage == parse_one(new).usage == {OPUS: Usage(111, 111, 111, 111)}
    before = PARSER.signature(new)
    append(old.with_suffix("") / "subagents" / "agent-m_old.jsonl", assistant("m_more", at=3))
    assert PARSER.signature(new) != before


@pytest.mark.parametrize("stale_folder", ["-w-a-stale", "-w-z-stale"])
def test_copies_of_a_session_that_grew_apart_all_parse_to_the_union(tmp_path: Path, stale_folder: str) -> None:
    """Ingestion keeps one row per session id, so it must not matter which copy is parsed last."""
    shared = [user("first prompt", at=0), assistant("m1", (1, 1, 1, 1), at=1, content=[tool_use("t1", "Write", file_path="/a")])]
    newer = [assistant("m2", (10, 10, 10, 10), at=2, content=[tool_use("t2", "Write", file_path="/b")]), user("second prompt", at=3)]
    stale = write_session(tmp_path, shared, folder=stale_folder)  # the copy left behind when the session moved
    live = write_session(tmp_path, [*shared, *newer], folder="-w-live")
    from_stale, from_live = (replace(parse_one(path), source_path="") for path in (stale, live))
    assert from_stale == from_live
    assert from_live.usage == {OPUS: Usage(11, 11, 11, 11)}
    assert (from_live.turns, from_live.prompts, from_live.tool_calls, from_live.files_touched) == (2, 2, 2, 2)
    assert from_live.ended_at == "2026-09-28T10:00:03Z"


def test_identical_copies_of_a_subagent_transcript_do_not_double_count(tmp_path: Path) -> None:
    records = [user(at=0), assistant("m_main", (1, 1, 1, 1), at=1)]
    line = json.dumps(assistant("m_sub", (10, 10, 10, 10), at=2, isSidechain=True, content=[tool_use("t1", "Write", file_path="/a")])) + "\n"
    for folder in ("-w-a", "-w-b", "-w-c"):
        main = write_session(tmp_path, records, folder=folder)
        sub = main.with_suffix("") / "subagents" / "agent-1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(line)
    rec = parse_one(main)
    assert rec.usage == {OPUS: Usage(11, 11, 11, 11)}
    assert (rec.tool_calls, rec.files_touched, rec.turns) == (1, 1, 2)


# ---- slots: money is attributed to when it was spent ------------------------------------------------
RESPONSE_A = Usage(1_200, 80_000, 3_000, 900, cache_write_1h=2_000)
RESPONSE_B = Usage(400, 120_000, 0, 3_000)
RESPONSE_C = Usage(500, 20_000, 2_000, 200)
RESPONSE_D = Usage(800, 15_000, 0, 400, cache_write_1h=3_000)
FIXTURE_SLOTS = {  # slot -> number of responses of class A, B (opus), C, D (haiku): see the header
    "2026-09-28T09:00:00Z": (13, 1, 1, 0), "2026-09-28T09:15:00Z": (4, 0, 1, 4), "2026-09-28T09:30:00Z": (3, 1, 0, 0),
    "2026-09-28T10:15:00Z": (2, 0, 0, 0), "2026-09-28T10:30:00Z": (2, 2, 0, 0), "2026-09-28T10:45:00Z": (3, 0, 0, 0),
    "2026-09-28T11:00:00Z": (2, 1, 1, 0), "2026-09-28T11:15:00Z": (2, 0, 0, 0),
}


def slot_sums(rec: SessionRecord) -> dict[str, Usage]:
    sums: dict[str, Usage] = {}
    for (_, model), used in rec.slots.items():
        sums[model] = sums.get(model, Usage()) + used
    return sums


def assert_slots_add_up(rec: SessionRecord) -> None:
    """The invariant every consumer relies on: per model, the slots add up exactly to the session's usage."""
    assert slot_sums(rec) == rec.usage
    assert all(not used.is_empty() for used in rec.slots.values())
    assert {slot for slot, _ in rec.slots} == {slot_of(datetime.fromisoformat(slot)) for slot, _ in rec.slots}  # well-formed slots


def when(day: int, hour: int, minute: int = 0, second: int = 0, *, month: int = 9) -> float:
    """``at`` (seconds after BASE) of a UTC instant, so a test can place a record anywhere on the calendar."""
    return (datetime(2026, month, day, hour, minute, second, tzinfo=timezone.utc) - BASE).total_seconds()


def slotted(rec: SessionRecord) -> dict[str, Usage]:
    """slot -> usage summed over models, for sessions whose point is when rather than which model."""
    out: dict[str, Usage] = {}
    for (slot, _), used in rec.slots.items():
        out[slot] = out.get(slot, Usage()) + used
    return out


def test_the_fixture_is_spent_in_the_slots_its_responses_were_made(record: SessionRecord) -> None:
    expected: dict[tuple[str, str], Usage] = {}
    for slot, counts in FIXTURE_SLOTS.items():
        for n, cls, model in zip(counts, (RESPONSE_A, RESPONSE_B, RESPONSE_C, RESPONSE_D), (OPUS, OPUS, HAIKU, HAIKU), strict=True):
            if n:
                expected[(slot, model)] = expected.get((slot, model), Usage()) + sum((cls for _ in range(n)), Usage())
    assert record.slots == expected
    assert_slots_add_up(record)
    assert sum(sum(counts) for counts in FIXTURE_SLOTS.values()) == record.turns == 43


def test_the_slots_of_a_session_without_its_subagents_are_those_of_its_own_responses(tmp_path: Path) -> None:
    main_only = parse_one(write_session(tmp_path, MAIN.read_text().splitlines(), SESSION))
    assert main_only.usage == MAIN_ONLY_USAGE
    assert_slots_add_up(main_only)
    assert main_only.slots[("2026-09-28T09:15:00Z", HAIKU)] == RESPONSE_C  # the four D responses of the sub-agents are not in it
    assert parse_one(MAIN).slots[("2026-09-28T09:15:00Z", HAIKU)] == RESPONSE_C + sum((RESPONSE_D for _ in range(4)), Usage())


def test_a_session_spanning_midnight_utc_splits_at_the_edge(tmp_path: Path) -> None:
    records = [
        user(at=when(28, 23, 40)),
        assistant("m1", (1, 10, 100, 1_000), at=when(28, 23, 50)),
        assistant("m2", (2, 20, 200, 2_000), at=when(28, 23, 59, 59)),  # last second of the day
        assistant("m3", (4, 40, 400, 4_000), at=when(29, 0, 0, 0)),  # first second of the next one
        assistant("m4", (8, 80, 800, 8_000), at=when(29, 0, 14, 59)),
        assistant("m5", (16, 160, 1_600, 16_000), at=when(29, 0, 15)),
    ]
    rec = parse_records(tmp_path, records)
    assert slotted(rec) == {
        "2026-09-28T23:45:00Z": Usage(3, 30, 300, 3_000),
        "2026-09-29T00:00:00Z": Usage(12, 120, 1_200, 12_000),
        "2026-09-29T00:15:00Z": Usage(16, 160, 1_600, 16_000),
    }
    assert rec.usage == {OPUS: Usage(31, 310, 3_100, 31_000)}  # the session's whole usage is unchanged
    assert rec.started_at == "2026-09-28T23:40:00Z"  # and so are its other fields: it still starts the day before
    assert_slots_add_up(rec)


def test_a_session_spanning_a_week_boundary_splits_between_sunday_and_monday(tmp_path: Path) -> None:
    sunday, monday = datetime(2026, 9, 27), datetime(2026, 9, 28)
    assert sunday.isocalendar().week != monday.isocalendar().week and sunday.isoweekday() == 7  # the premise
    records = [
        user(at=when(27, 23, 50)),
        assistant("m1", (1, 1, 1, 1), at=when(27, 23, 52), model=HAIKU),
        assistant("m2", (2, 2, 2, 2), at=when(27, 23, 58), model=HAIKU),
        assistant("m3", (4, 4, 4, 4), at=when(28, 0, 3), model=HAIKU),
        assistant("m4", (8, 8, 8, 8), at=when(28, 0, 5), model=OPUS),
    ]
    rec = parse_records(tmp_path, records)
    assert rec.slots == {
        ("2026-09-27T23:45:00Z", HAIKU): Usage(3, 3, 3, 3),
        ("2026-09-28T00:00:00Z", HAIKU): Usage(4, 4, 4, 4),
        ("2026-09-28T00:00:00Z", OPUS): Usage(8, 8, 8, 8),
    }
    assert_slots_add_up(rec)


def test_a_response_is_attributed_to_its_last_record_even_when_it_streams_across_an_edge(tmp_path: Path) -> None:
    streamed = [assistant("m1", (5, 100, 20, out), at=at) for out, at in ((2, when(28, 12, 14, 57)), (2, when(28, 12, 14, 59)), (391, when(28, 12, 15, 2)))]
    for order in (streamed, streamed[::-1]):  # the line order does not decide it: the latest timestamp does
        rec = parse_records(tmp_path / str(order[0]["message"]["usage"]["output_tokens"]), [user(at=when(28, 12)), *order])
        assert rec.slots == {("2026-09-28T12:15:00Z", OPUS): Usage(5, 100, 20, 391)}


def test_a_response_whose_last_record_has_no_usable_timestamp_keeps_the_latest_usable_one(tmp_path: Path) -> None:
    early, final = assistant("m1", (5, 100, 20, 2), at=when(28, 12, 14)), assistant("m1", (5, 100, 20, 391), at=when(28, 12, 20))
    final["timestamp"] = "garbage"
    rec = parse_records(tmp_path, [user(at=when(28, 11, 5)), early, final])  # 11:00 would be the fallback slot
    assert rec.slots == {("2026-09-28T12:00:00Z", OPUS): Usage(5, 100, 20, 391)}


@pytest.mark.parametrize("bad", [..., None, "", "garbage", 12345, "2026-13-45T99:99:99Z"], ids=["missing", "null", "empty", "text", "number", "impossible"])
def test_a_response_without_a_usable_timestamp_counts_at_the_first_timestamp_of_the_session(tmp_path: Path, bad: Any) -> None:
    lost = assistant("m2", (7, 7, 7, 7), at=when(28, 13))
    if bad is ...:
        del lost["timestamp"]
    else:
        lost["timestamp"] = bad
    records = [user(at=when(28, 10, 7)), assistant("m1", (1, 1, 1, 1), at=when(28, 12)), lost]
    rec = parse_records(tmp_path, records)
    assert rec.slots == {("2026-09-28T10:00:00Z", OPUS): Usage(7, 7, 7, 7), ("2026-09-28T12:00:00Z", OPUS): Usage(1, 1, 1, 1)}
    assert rec.usage == {OPUS: Usage(8, 8, 8, 8)}


def test_a_timestamp_with_an_offset_is_slotted_in_utc(tmp_path: Path) -> None:
    local = assistant("m1", (3, 3, 3, 3), at=0)
    local["timestamp"] = "2026-09-28T23:50:00-03:00"  # 02:50 UTC the next day
    east = assistant("m2", (5, 5, 5, 5), at=0)
    east["timestamp"] = "2026-09-29T01:05:00+05:30"  # 19:35 UTC the day before
    rec = parse_records(tmp_path, [local, east])
    assert rec.slots == {("2026-09-29T02:45:00Z", OPUS): Usage(3, 3, 3, 3), ("2026-09-28T19:30:00Z", OPUS): Usage(5, 5, 5, 5)}


def test_subagent_responses_are_attributed_by_their_own_timestamps(tmp_path: Path) -> None:
    main = write_session(tmp_path, [user(at=when(28, 23, 50)), assistant("m_main", (1, 1, 1, 1), at=when(28, 23, 55))])
    sub = main.with_suffix("") / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir(parents=True)
    lines = [assistant("m_sub1", (10, 10, 10, 10), at=when(29, 0, 2), isSidechain=True), assistant("m_sub2", (20, 20, 20, 20), at=when(29, 0, 20), model=HAIKU, isSidechain=True)]
    sub.write_text("".join(json.dumps(line) + "\n" for line in lines))
    rec = parse_one(main)
    assert rec.slots == {
        ("2026-09-28T23:45:00Z", OPUS): Usage(1, 1, 1, 1),
        ("2026-09-29T00:00:00Z", OPUS): Usage(10, 10, 10, 10),
        ("2026-09-29T00:15:00Z", HAIKU): Usage(20, 20, 20, 20),
    }
    assert_slots_add_up(rec)


def test_a_response_copied_into_two_files_is_attributed_once(tmp_path: Path) -> None:
    """Copies of a session (and of its sub-agent files) hold the same responses: they must not add a second slot."""
    records = [user(at=0), assistant("m1", (1, 2, 3, 4), at=when(28, 11, 59, 59)), assistant("m1", (1, 2, 3, 40), at=when(28, 12, 0, 1))]
    for folder in ("-w-a", "-w-b"):
        main = write_session(tmp_path, records, folder=folder)
    assert parse_one(main).slots == {("2026-09-28T12:00:00Z", OPUS): Usage(1, 2, 3, 40)}


def test_two_models_in_one_slot_stay_apart(tmp_path: Path) -> None:
    rec = parse_records(tmp_path, [assistant("m1", (1, 1, 1, 1), at=1), assistant("m2", (2, 2, 2, 2), at=2, model=HAIKU), assistant("m3", (4, 4, 4, 4), at=3)])
    assert rec.slots == {("2026-09-28T10:00:00Z", OPUS): Usage(5, 5, 5, 5), ("2026-09-28T10:00:00Z", HAIKU): Usage(2, 2, 2, 2)}


def test_a_session_with_no_billed_usage_has_no_slots(tmp_path: Path) -> None:
    rec = parse_records(tmp_path, [user(at=0), assistant("m1", (0, 0, 0, 0), at=1)])
    assert rec.slots == {} and rec.usage == {}


# Randomized sessions: the builder knows what each response is worth and when its last record was written, so the
# expected slots come from the construction, not from the parser.
FUZZ_MODELS = (OPUS, HAIKU, "claude-sonnet-5-5")
UTC = timezone.utc


def fuzz_session(rng: random.Random, where: Path) -> tuple[Path, dict[tuple[str, str], Usage]]:
    """A random session: streamed responses of several models over several days, spread over the main file and two
    sub-agent files, some records with a missing or garbled timestamp. Returns the main file and the expected slots."""
    first = datetime(2026, 9, 26, tzinfo=UTC) + timedelta(seconds=rng.randrange(3 * 86_400))
    files: list[list[dict[str, Any]]] = [[user(at=(first - BASE).total_seconds())], [], []]
    stamps = [first]
    made: list[tuple[str, Usage, list[datetime]]] = []  # (model, final usage, usable timestamps of its records)
    for n in range(rng.randrange(1, 14)):
        model, last = rng.choice(FUZZ_MODELS), first + timedelta(seconds=rng.randrange(3 * 86_400), milliseconds=rng.randrange(1_000))
        final = (rng.randrange(0, 5_000), rng.randrange(0, 90_000), rng.randrange(0, 9_000), rng.randrange(1, 4_000))
        records = []
        for i in range(rng.randrange(1, 5)):  # record 0 is the last one written; the others stream earlier
            at = last - timedelta(seconds=rng.randrange(0, 40)) if i else last
            partial = tuple(rng.randrange(0, v + 1) for v in final)
            rec = assistant(f"msg_{n}", final if i == 0 else partial, at=(at - BASE).total_seconds(), model=model)
            if i and rng.random() < 0.3:
                rec["timestamp"] = rng.choice([None, "garbage", 7])
            records.append(rec)
        if rng.random() < 0.15:  # the whole response lost its timestamps
            for rec in records:
                del rec["timestamp"]
        usable = [datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00")) for rec in records if rec.get("timestamp") not in (None, "garbage", 7)]
        stamps += usable
        files[rng.randrange(3)] += records
        made.append((model, Usage(*final), usable))
    expected: dict[tuple[str, str], Usage] = {}
    for model, used, usable in made:
        key = (slot_of(max(usable, default=min(stamps))), model)
        expected[key] = expected.get(key, Usage()) + used
    main = write_session(where, files[0])
    for i, records in enumerate(files[1:]):
        if records:
            sub = main.with_suffix("") / "subagents" / f"agent-{i}.jsonl"
            sub.parent.mkdir(parents=True, exist_ok=True)
            sub.write_text("".join(json.dumps(rec) + "\n" for rec in records))
    return main, expected


@pytest.mark.parametrize("seed", range(60))
def test_random_sessions_are_attributed_slot_by_slot_and_always_add_up(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    main, expected = fuzz_session(rng, tmp_path)
    rec = parse_one(main)
    assert rec.slots == expected
    assert_slots_add_up(rec)
    for file in (main, *main.with_suffix("").rglob("*.jsonl")):  # the order of the lines in a file changes nothing
        lines = file.read_text().splitlines()
        rng.shuffle(lines)
        file.write_text("".join(line + "\n" for line in lines))
    assert parse_one(main).slots == expected


# ---- privacy ----------------------------------------------------------------------------------------
def strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


def test_no_marker_appears_anywhere_in_the_record(record: SessionRecord) -> None:
    assert CANARY not in repr(record)
    assert CANARY not in dump(record)
    assert isinstance(record.project, str) and "/" not in record.project


def test_the_fixture_has_every_shape_of_cache_write_the_parser_reads() -> None:
    shapes: set[str] = set()
    for path in (MAIN, AGENT_1, AGENT_2):
        for rec in fixture_records(path):
            usage = (rec.get("message") or {}).get("usage") or {}
            split = usage.get("cache_creation")
            if not usage.get("cache_creation_input_tokens"):
                continue
            if split is None:
                shapes.add("flat total only")
            elif split["ephemeral_5m_input_tokens"] and split["ephemeral_1h_input_tokens"]:
                shapes.add("both TTLs")
            elif split["ephemeral_1h_input_tokens"]:
                shapes.add("1h only")
    assert shapes == {"flat total only", "both TTLs", "1h only"}


def test_the_fixture_plants_the_marker_wherever_content_could_leak() -> None:
    found: set[str] = set()
    for path in (MAIN, AGENT_1, AGENT_2):
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                found.add("malformed line")
                continue
            message = rec.get("message") or {}
            content = message.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            if rec["type"] == "user" and "promptSource" in rec and CANARY in json.dumps(content):
                found.add("prompt")
            for block in blocks:
                kind = block.get("type") if isinstance(block, dict) else None
                if kind in ("text", "thinking") and rec["type"] == "assistant" and CANARY in json.dumps(block):
                    found.add("assistant " + kind)
                if kind == "tool_use" and CANARY in json.dumps(block["input"]):
                    found.add("tool input")
                if kind == "tool_use" and CANARY in block["input"].get("file_path", block["input"].get("notebook_path", "")):
                    found.add("file path")
                if kind == "tool_result" and CANARY in json.dumps(block["content"]):
                    found.add("tool output")
            if CANARY in json.dumps(rec.get("toolUseResult")):
                found.add("toolUseResult")
            if path != MAIN and rec["type"] == "user" and CANARY in json.dumps(content):
                found.add("subagent prompt")
    assert found >= {"prompt", "assistant text", "assistant thinking", "tool input", "file path", "tool output", "toolUseResult", "subagent prompt", "malformed line"}
    for extra in (*AGENT_1.parent.glob("*.meta.json"), *SESSION_DIR.glob("tool-results/*.txt")):
        assert CANARY in extra.read_text()
    assert sum(path.read_text().count(CANARY) for path in PROJECTS.rglob("*") if path.is_file()) > 150


# ---- the fixture is what the generator writes ------------------------------------------------------
def test_fixture_is_what_the_generator_writes(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("build_claude_fixture", FIXTURES / "build_fixture.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(tmp_path)
    committed = {p.relative_to(PROJECTS): p.read_bytes() for p in PROJECTS.rglob("*") if p.is_file()}
    rebuilt = {p.relative_to(tmp_path / "projects"): p.read_bytes() for p in (tmp_path / "projects").rglob("*") if p.is_file()}
    assert rebuilt == committed

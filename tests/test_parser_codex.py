"""Codex CLI parser: token semantics, de-duplication, forks, tool/file/error/prompt counts, robustness.

Hand-computed expectations for the fixture
``tests/fixtures/codex/sessions/2026/09/29/rollout-2026-09-29T10-00-00-01a0d8b0-....jsonl``
(one session, thread 01a0d8b0-1a2b-7c3d-8e4f-5a6b7c8d9e0f, a light-looking one, model gpt-6-astra):

Six model responses. ``input`` includes the cached part and ``output`` includes reasoning, so the
uncached input is ``input - cached``:

    response    input    cached   uncached   output   (reasoning, already inside output)
    R1         10,000         0     10,000      500    200
    R2         12,000     8,000      4,000    1,000    400
    R3         15,000    12,000      3,000      500      0
    R4         18,000    15,000      3,000    1,000      0
    R5         20,000    12,000      8,000    1,000      0
    R6         25,000    13,000     12,000    2,000  1,000
    total     100,000    60,000     40,000    6,000  1,600

so the record must carry ``Usage(input=40_000, cache_read=60_000, cache_write=0, output=6_000)`` and
``turns == 6``. In dollars, with prices per 1M tokens: 0.04 * input + 0.06 * cached_input + 0.006 * output;
at gpt-6-astra's built-in prices (10.00 / 1.00 / 50.00, checked 2026-09-30) that is 0.40 + 0.06 + 0.30 = $0.76.

The log also holds things that must NOT change those numbers: a repeated token_count after R2 (same totals,
only the rate limits differ), a token_count with ``info: null`` after R5, a second token_usage_record for R3
(same response_id), a record of a type from a "future release" whose payload carries 999,999 tokens, a line
cut off mid-write, and the injected AGENTS.md / environment messages.

Other expected values: 5 tool calls (4 ``exec``, 1 ``js``), 2 files touched (one patched, one added), 1 error
(the third ``exec`` prints ``Script failed``), 1 prompt, project ``fake-repo`` (from the git remote, not the
``fake-repo-checkout`` directory), 10:00:00Z to 10:10:00Z: 600 s wall and, with no gap over 30 minutes, 600 s
active (sub-second parts of the first and last record are cut). All of it is spent between 10:00 and 10:10, so
the whole usage sits in the one 15-minute slot 2026-09-29T10:00:00Z.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agentfare.config import Config
from agentfare.db import Store
from agentfare.ingest import ingest
from agentfare.models import SessionRecord, Usage
from agentfare.parsers import get_parser
from agentfare.parsers.codex import PARSER

MARKER = "PRIVACY-CANARY-CODEX"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "codex"
THREAD = "01a0d000-0000-7000-8000-000000000001"
START = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)


# ---- building synthetic rollouts ----------------------------------------------------------------
def counts(i: int = 0, c: int = 0, o: int = 0, r: int = 0, w: int = 0) -> dict[str, int]:
    return {"input_tokens": i, "cached_input_tokens": c, "cache_write_input_tokens": w, "output_tokens": o, "reasoning_output_tokens": r, "total_tokens": i + o}


class Log:
    """A rollout under construction: running ordinal, a clock that advances one second per record."""

    def __init__(self, tmp_path: Path, name: str = "rollout-2026-09-29T10-00-00-%s.jsonl", **meta: Any) -> None:
        self.path = tmp_path / (name % THREAD if "%s" in name else name)
        self.rows: list[str] = []
        self.clock = 0.0
        self.total = [0, 0, 0, 0]  # running input, cached, written, output of the token_count records
        if meta.pop("with_meta", True):
            self.add("session_meta", {"id": THREAD, "cwd": "/w/proj", "source": "vscode", **meta})

    def add(self, kind: str, payload: Any, at: float | None = None, **extra: Any) -> Log:
        self.clock = self.clock + 1 if at is None else at
        stamp = (START + timedelta(seconds=self.clock)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        rec = {"timestamp": stamp, "ordinal": len(self.rows), "type": kind, "payload": payload, **extra}
        self.rows.append(json.dumps(rec, separators=(",", ":")))
        return self

    def event(self, ptype: str, at: float | None = None, **payload: Any) -> Log:
        return self.add("event_msg", {"type": ptype, **payload}, at)

    def turn(self, model: str, turn_id: str = "t1", at: float | None = None) -> Log:
        self.event("task_started", at, turn_id=turn_id)
        return self.add("turn_context", {"turn_id": turn_id, "model": model})

    def usage_record(self, usage: Any, turn_id: str = "t1", response_id: str | None = None, thread: str = THREAD, at: float | None = None) -> Log:
        rid = response_id or f"resp_{len(self.rows)}"
        payload = {"thread_id": thread, "turn_id": turn_id, "response_id": rid, "usage": usage, "turn_token_usage": usage, "thread_token_usage": usage}
        return self.add("token_usage_record", payload, at)

    def token_count(self, last: dict[str, int] | None, total: dict[str, int] | None = None, at: float | None = None) -> Log:
        """A token_count; the total defaults to the running sum of the lasts (what the real logs carry)."""
        if total is None and last is not None:
            for k, key in enumerate(("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")):
                self.total[k] += last[key]
            total = counts(self.total[0], self.total[1], self.total[3], w=self.total[2])
        info = None if last is None else {"total_token_usage": total, "last_token_usage": last, "model_context_window": 258400}
        return self.event("token_count", at, info=info, rate_limits={"primary": {"used_percent": len(self.rows)}})

    def response(self, i: int, c: int = 0, o: int = 0, r: int = 0, w: int = 0, turn_id: str = "t1") -> Log:
        """One model response as the current logs write it: a usage record, then its token_count."""
        used = counts(i, c, o, r, w)
        return self.usage_record(used, turn_id).token_count(used)

    def call(self, call_id: str, kind: str = "custom_tool_call", name: str = "exec", **payload: Any) -> Log:
        return self.add("response_item", {"type": kind, "call_id": call_id, "name": name, **payload})

    def result(self, call_id: str, output: Any, kind: str = "custom_tool_call_output") -> Log:
        return self.add("response_item", {"type": kind, "call_id": call_id, "output": output})

    def item(self, item_type: str, **fields: Any) -> Log:
        return self.event("item_completed", item={"type": item_type, **fields})

    def raw(self, text: str) -> Log:
        self.rows.append(text)
        return self

    def write(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(self.rows) + "\n", encoding="utf-8")
        return self.path


def parse(log: Log | Path, **kw: Any) -> SessionRecord:
    path = log if isinstance(log, Path) else log.write()
    records = PARSER.parse(path, **kw)
    assert len(records) == 1
    return records[0]


def at(iso: str) -> float:
    """Seconds after START of an absolute UTC time, to pass as ``at=`` (a negative number is before START)."""
    return (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - START).total_seconds()


def turn_at(log: Log, when: str, model: str, turn_id: str = "t1") -> Log:
    return log.event("task_started", at(when), turn_id=turn_id).add("turn_context", {"turn_id": turn_id, "model": model}, at=at(when))


def response_at(log: Log, when: str, i: int, o: int = 10, turn_id: str = "t1") -> Log:
    """One response whose usage record and token_count both carry the time ``when``."""
    used = counts(i, 0, o)
    return log.usage_record(used, turn_id, at=at(when)).token_count(used, at=at(when))


def assert_slots_add_up(rec: SessionRecord) -> None:
    """The slot invariant: valid slot names, and per model the slots add up to the usage."""
    per_model: dict[str, Usage] = {}
    for (slot, model), used in rec.slots.items():
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:(00|15|30|45):00Z", slot), slot
        assert not used.is_empty()
        per_model[model] = per_model.get(model, Usage()) + used
    assert per_model == rec.usage
    assert list(rec.slots) == sorted(rec.slots)


def only_usage(record: SessionRecord) -> Usage:
    assert len(record.usage) == 1
    return next(iter(record.usage.values()))


def fixture_file() -> Path:
    (path,) = PARSER.discover(FIXTURE_ROOT)
    return path


# ---- discovery, signature, registry -------------------------------------------------------------
def test_registered_as_the_codex_parser() -> None:
    assert get_parser("codex") is PARSER
    assert PARSER.agent == "codex"


def test_discover_walks_the_dated_tree_and_a_flat_archive(tmp_path: Path) -> None:
    dated = Log(tmp_path / "sessions" / "2026" / "09" / "29").write()
    flat = tmp_path / "archived_sessions" / "rollout-2026-09-04T21-32-21-01a0e000-0000-7000-8000-000000000002.jsonl"
    flat.parent.mkdir()
    flat.write_text("{}\n")
    (tmp_path / "sessions" / "notes.jsonl").write_text("{}\n")
    (tmp_path / "sessions" / "rollout-not-a-file.jsonl").mkdir()
    assert list(PARSER.discover(tmp_path / "sessions")) == [dated]
    assert list(PARSER.discover(tmp_path / "archived_sessions")) == [flat]
    assert list(PARSER.discover(tmp_path / "missing")) == []


def test_signature_changes_when_the_file_grows(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("gpt-6-astra")
    path = log.write()
    before = PARSER.signature(path)
    assert PARSER.signature(path) == before
    with path.open("a") as fh:
        fh.write(log.response(10).rows[-1] + "\n")  # any growth changes the size
    assert PARSER.signature(path) != before


# ---- the fixture --------------------------------------------------------------------------------
def test_fixture_session_matches_the_hand_computed_totals() -> None:
    (rec,) = PARSER.parse(fixture_file())
    assert rec.agent == "codex"
    assert rec.key == "codex:01a0d8b0-1a2b-7c3d-8e4f-5a6b7c8d9e0f"
    assert rec.usage == {"gpt-6-astra": Usage(input=40_000, cache_read=60_000, cache_write=0, output=6_000)}
    assert rec.slots == {("2026-09-29T10:00:00Z", "gpt-6-astra"): rec.usage["gpt-6-astra"]}
    assert rec.turns == 6
    assert rec.tool_calls == 5
    assert rec.files_touched == 2
    assert rec.error_retries == 1
    assert rec.prompts == 1
    assert rec.project == "fake-repo"
    assert (rec.started_at, rec.ended_at) == ("2026-09-29T10:00:00Z", "2026-09-29T10:10:00Z")
    assert (rec.wall_seconds, rec.active_seconds) == (600, 600)
    assert rec.primary_model == "gpt-6-astra"


def test_fixture_is_in_the_real_shape_and_carries_the_canary_everywhere_it_should() -> None:
    lines = fixture_file().read_text(encoding="utf-8").splitlines()
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            parsed.append(None)
    assert parsed.count(None) == 1  # the line cut off mid-write
    assert all(set(r) >= {"timestamp", "ordinal", "type", "payload"} for r in parsed if r)
    payloads = [r["payload"] for r in parsed if r]
    prompt = [p for p in payloads if p.get("role") == "user" and "Please fix" in json.dumps(p)]
    assistant = [p for p in payloads if p.get("role") == "assistant"]
    tool_inputs = [p for p in payloads if p.get("type") in ("custom_tool_call", "function_call")]
    tool_outputs = [p for p in payloads if str(p.get("type", "")).endswith("_call_output")]
    for group in (prompt, assistant, tool_inputs, tool_outputs):
        assert group and all(MARKER in json.dumps(p) for p in group)
    assert len(re.findall(MARKER, "\n".join(lines))) >= 25


def test_the_canary_never_reaches_the_record() -> None:
    (rec,) = PARSER.parse(fixture_file())
    assert MARKER not in repr(rec)
    as_dict = asdict(rec)
    as_dict["slots"] = {f"{slot} {model}": used for (slot, model), used in as_dict["slots"].items()}  # JSON keys must be strings
    assert MARKER not in json.dumps(as_dict)


def test_ingesting_the_fixture_keeps_the_canary_out_of_the_database(tmp_path: Path, make_config: Callable[..., Config]) -> None:
    cfg = make_config(timezone="UTC", sources={"codex": [str(FIXTURE_ROOT / "sessions")]})
    db = tmp_path / "codex.db"
    with Store(db) as store:
        report = ingest(cfg, store, ["codex"])
        assert (report.by_agent, report.errors) == ({"codex": 1}, [])
        (stored,) = list(store.iter_sessions())
        assert stored.project == "fake-repo"
    assert MARKER.encode() not in db.read_bytes()


def test_parsing_does_not_depend_on_key_order_or_spacing(tmp_path: Path) -> None:
    """Records that do not start with the usual timestamp/ordinal/type/payload head are parsed whole."""
    expected = PARSER.parse(fixture_file())[0]
    rewritten = []
    for line in fixture_file().read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            rewritten.append(line)
            continue
        rewritten.append(json.dumps({"payload": rec["payload"], "type": rec["type"], "ordinal": rec["ordinal"], "timestamp": rec["timestamp"]}))
    path = tmp_path / fixture_file().name
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    (got,) = PARSER.parse(path)
    assert got.usage == expected.usage
    assert (got.tool_calls, got.files_touched, got.error_retries, got.turns, got.prompts, got.project) == (5, 2, 1, 6, 1, "fake-repo")
    assert (got.started_at, got.ended_at, got.active_seconds) == (expected.started_at, expected.ended_at, expected.active_seconds)


# ---- token semantics ----------------------------------------------------------------------------
def test_input_includes_cached_and_written_tokens_and_output_includes_reasoning(tmp_path: Path) -> None:
    rec = parse(Log(tmp_path).turn("m").response(i=1000, c=600, w=100, o=50, r=20))
    assert only_usage(rec) == Usage(input=300, cache_read=600, cache_write=100, output=50)
    assert only_usage(rec).total_input == 1000
    assert only_usage(rec).total == 1050  # total_tokens = input + output, reasoning is not added again


def test_a_repeated_token_count_is_billed_once(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("m")
    used = counts(5000, 1000, 200)
    total = counts(5000, 1000, 200)
    for _ in range(3):  # rate-limit-only updates repeat the same totals
        log.token_count(used, total)
    rec = parse(log)
    assert only_usage(rec) == Usage(input=4000, cache_read=1000, output=200)
    assert rec.turns == 1


def test_a_usage_record_and_its_token_count_are_one_response_in_either_order(tmp_path: Path) -> None:
    used = counts(2000, 500, 100)
    first = parse(Log(tmp_path / "a").turn("m").usage_record(used).token_count(used))
    swapped = parse(Log(tmp_path / "b").turn("m").token_count(used).usage_record(used))
    for rec in (first, swapped):
        assert only_usage(rec) == Usage(input=1500, cache_read=500, output=100)
        assert rec.turns == 1


def test_logs_without_usage_records_are_billed_from_the_token_count_deltas(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("m")
    for i, c, o in ((1000, 0, 100), (1500, 1000, 100), (2000, 1500, 300)):
        log.token_count(counts(i, c, o))
    rec = parse(log)
    assert only_usage(rec) == Usage(input=1000 + 500 + 500, cache_read=2500, output=500)
    assert rec.turns == 3


def test_a_token_count_without_last_usage_falls_back_to_the_difference_of_totals(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("m")
    log.event("token_count", info={"total_token_usage": counts(1000, 0, 100)})
    log.event("token_count", info={"total_token_usage": counts(3000, 1000, 250)})
    assert only_usage(parse(log)) == Usage(input=1000 + 1000, cache_read=1000, output=250)


def test_a_response_only_in_a_usage_record_is_billed(tmp_path: Path) -> None:
    """A context-compaction request has a usage record but the running totals leave it out."""
    log = Log(tmp_path).turn("m").response(1000, 0, 100).usage_record(counts(5000, 2000, 300)).response(1000, 500, 100)
    rec = parse(log)
    assert only_usage(rec) == Usage(input=1000 + 3000 + 500, cache_read=2500, output=500)
    assert rec.turns == 3


def test_totals_that_restart_do_not_bill_a_negative_or_repeated_amount(tmp_path: Path) -> None:
    """A resumed thread restarts its running totals; only the per-response counts are summed."""
    log = Log(tmp_path).turn("m")
    log.token_count(counts(1000, 0, 100), counts(1000, 0, 100))
    log.token_count(counts(1500, 0, 100), counts(2500, 0, 200))
    log.token_count(counts(400, 0, 40), counts(400, 0, 40))  # restarted
    log.token_count(counts(600, 0, 60), counts(1000, 0, 100))
    assert only_usage(parse(log)) == Usage(input=3500, output=300)


def test_an_inherited_running_total_with_an_empty_last_bills_nothing(tmp_path: Path) -> None:
    """A fork's first token_count carries its parent's total; ``last`` has only a total_tokens."""
    log = Log(tmp_path).turn("m")
    inherited = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 75466}
    log.token_count(inherited, counts(2_000_000, 1_900_000, 3000))
    log.token_count(counts(1000, 0, 100), counts(2_001_000, 1_900_000, 3100))
    rec = parse(log)
    assert only_usage(rec) == Usage(input=1000, output=100)
    assert rec.turns == 1


def test_a_response_id_seen_twice_is_billed_once(tmp_path: Path) -> None:
    used = counts(1000, 0, 100)
    log = Log(tmp_path).turn("m").usage_record(used, response_id="resp_a").usage_record(used, response_id="resp_a").token_count(used)
    rec = parse(log)
    assert only_usage(rec) == Usage(input=1000, output=100)
    assert rec.turns == 1


def test_usage_records_of_another_thread_are_not_billed(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("m").usage_record(counts(9000, 0, 900), thread="01a0ffff-0000-7000-8000-00000000dead").response(1000, 0, 100)
    assert only_usage(parse(log)) == Usage(input=1000, output=100)


def test_compacted_records_are_never_read(tmp_path: Path) -> None:
    replay = {"type": "function_call", "call_id": "call_old", "name": "exec", "input": "await tools.apply_patch('*** Begin Patch\\n*** Update File: old.py')"}
    log = Log(tmp_path).turn("m").response(1000, 0, 100)
    log.add("compacted", {"message": "", "latest_token_usage_record": {"response_id": "resp_new", "usage": counts(70000, 0, 7000)}, "replacement_history": [replay, {"type": "message", "role": "user", "content": []}]})
    rec = parse(log)
    assert only_usage(rec) == Usage(input=1000, output=100)
    assert (rec.tool_calls, rec.files_touched, rec.prompts) == (0, 0, 0)


def test_a_forks_copied_history_is_skipped_but_its_own_records_count(tmp_path: Path) -> None:
    log = Log(tmp_path, source={"subagent": {"thread_spawn": {}}}, forked_from_id="01a0aaaa-0000-7000-8000-000000000000", subagent_history_start_ordinal=5)
    log.add("session_meta", {"id": "01a0aaaa-0000-7000-8000-000000000000", "cwd": "/w/parent"})  # ordinal 1: the parent's meta
    log.call("call_parent")  # 2
    log.usage_record(counts(9000, 0, 900), thread=THREAD)  # 3: would bill if it were read
    log.token_count(counts(9000, 0, 900))  # 4
    log.turn("m").call("call_own").response(1000, 0, 100)  # 5..: the thread's own history
    rec = parse(log)
    assert only_usage(rec) == Usage(input=1000, output=100)
    assert rec.tool_calls == 1
    assert rec.session_id == THREAD
    assert rec.project == "proj"


def test_history_start_ordinal_only_marks_copied_history_in_forks(tmp_path: Path) -> None:
    """Auto-review threads have the field too, past their last record, and are not forks: all of it counts."""
    log = Log(tmp_path, source={"subagent": {"other": "guardian"}}, subagent_history_start_ordinal=99)
    rec = parse(log.turn("m").call("call_1").response(1000, 0, 100))
    assert only_usage(rec) == Usage(input=1000, output=100)
    assert rec.tool_calls == 1


# ---- models -------------------------------------------------------------------------------------
def test_each_turn_is_billed_to_the_model_that_served_it(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("gpt-a", "t1").response(1000, 0, 100, turn_id="t1").turn("gpt-b", "t2").response(2000, 500, 200, turn_id="t2")
    log.response(1000, 0, 100, turn_id="t2")
    rec = parse(log)
    assert rec.usage == {"gpt-a": Usage(input=1000, output=100), "gpt-b": Usage(input=1500 + 1000, cache_read=500, output=300)}
    assert rec.primary_model == "gpt-b"


def test_a_response_logged_before_its_turn_context_takes_the_model_of_its_turn(tmp_path: Path) -> None:
    """A compaction request opens a turn: its usage record precedes that turn's turn_context."""
    log = Log(tmp_path).turn("gpt-a", "t1").response(1000, 0, 100, turn_id="t1")
    log.event("task_started", turn_id="t2").usage_record(counts(8000, 0, 800), turn_id="t2").add("turn_context", {"turn_id": "t2", "model": "gpt-b"})
    assert parse(log).usage == {"gpt-a": Usage(input=1000, output=100), "gpt-b": Usage(input=8000, output=800)}


def test_a_log_without_any_turn_context_has_an_unknown_model(tmp_path: Path) -> None:
    assert parse(Log(tmp_path).response(1000, 0, 100)).usage == {"unknown": Usage(input=1000, output=100)}


# ---- slots: the money lands when it was spent ---------------------------------------------------
def test_a_session_across_midnight_is_split_where_the_money_was_spent(tmp_path: Path) -> None:
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=at("2026-09-29T23:40:00Z"))
    turn_at(log, "2026-09-29T23:40:01Z", "m")
    for when, i in (("2026-09-29T23:41:00Z", 1000), ("2026-09-29T23:44:59Z", 2000), ("2026-09-29T23:59:59Z", 4000), ("2026-09-30T00:00:00Z", 8000), ("2026-09-30T00:20:00Z", 16000)):
        response_at(log, when, i)
    rec = parse(log)
    assert rec.slots == {
        ("2026-09-29T23:30:00Z", "m"): Usage(input=3000, output=20),  # the 15 minutes from 23:30 to 23:45 hold two responses
        ("2026-09-29T23:45:00Z", "m"): Usage(input=4000, output=10),
        ("2026-09-30T00:00:00Z", "m"): Usage(input=8000, output=10),
        ("2026-09-30T00:15:00Z", "m"): Usage(input=16000, output=10),
    }
    assert rec.usage == {"m": Usage(input=31_000, output=50)}
    assert rec.turns == 5
    assert (rec.started_at, rec.ended_at, rec.wall_seconds) == ("2026-09-29T23:40:00Z", "2026-09-30T00:20:00Z", 2400)
    assert_slots_add_up(rec)


def test_a_session_across_a_week_boundary_keeps_each_model_in_the_slot_it_ran_in(tmp_path: Path) -> None:
    """Sunday 2026-09-27 to Monday 2026-09-28: the turns and models interleave with the clock."""
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=at("2026-09-27T23:50:00Z"))
    turn_at(log, "2026-09-27T23:50:01Z", "gpt-a", "t1")
    response_at(log, "2026-09-27T23:52:00Z", 1000, turn_id="t1")
    response_at(log, "2026-09-28T00:01:00Z", 2000, turn_id="t1")  # the same turn, now in Monday's first slot
    turn_at(log, "2026-09-28T00:05:00Z", "gpt-b", "t2")
    response_at(log, "2026-09-28T00:06:00Z", 4000, turn_id="t2")
    response_at(log, "2026-09-28T00:10:00Z", 8000, turn_id="t2")
    rec = parse(log)
    assert rec.slots == {
        ("2026-09-27T23:45:00Z", "gpt-a"): Usage(input=1000, output=10),
        ("2026-09-28T00:00:00Z", "gpt-a"): Usage(input=2000, output=10),
        ("2026-09-28T00:00:00Z", "gpt-b"): Usage(input=12_000, output=20),
    }
    assert rec.usage == {"gpt-a": Usage(input=3000, output=20), "gpt-b": Usage(input=12_000, output=20)}
    assert_slots_add_up(rec)


def test_a_compaction_request_lands_in_the_slot_of_its_own_record(tmp_path: Path) -> None:
    """Only a usage record exists for it, and it may come long after the last response."""
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=at("2026-09-29T10:00:00Z"))
    turn_at(log, "2026-09-29T10:00:01Z", "m")
    response_at(log, "2026-09-29T10:02:00Z", 1000)
    log.usage_record(counts(5000, 2000, 300), at=at("2026-09-29T11:20:00Z"))
    rec = parse(log)
    assert rec.slots == {
        ("2026-09-29T10:00:00Z", "m"): Usage(input=1000, output=10),
        ("2026-09-29T11:15:00Z", "m"): Usage(input=3000, cache_read=2000, output=300),
    }
    assert_slots_add_up(rec)


def test_a_response_seen_twice_is_attributed_to_the_record_that_billed_it(tmp_path: Path) -> None:
    used = counts(1000, 0, 10)
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=at("2026-09-29T10:00:00Z"))
    turn_at(log, "2026-09-29T10:00:01Z", "m")
    log.usage_record(used, at=at("2026-09-29T10:14:59Z")).token_count(used, at=at("2026-09-29T10:15:01Z"))
    assert parse(log).slots == {("2026-09-29T10:00:00Z", "m"): Usage(input=1000, output=10)}


def test_logs_without_usage_records_use_the_time_of_each_token_count(tmp_path: Path) -> None:
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=at("2026-09-29T23:50:00Z"))
    turn_at(log, "2026-09-29T23:50:01Z", "m")
    log.token_count(counts(1000, 0, 100), at=at("2026-09-29T23:55:00Z"))
    log.token_count(counts(3000, 1000, 300), at=at("2026-09-30T00:05:00Z"))
    rec = parse(log)
    assert rec.slots == {
        ("2026-09-29T23:45:00Z", "m"): Usage(input=1000, output=100),
        ("2026-09-30T00:00:00Z", "m"): Usage(input=2000, cache_read=1000, output=300),
    }
    assert_slots_add_up(rec)


def test_a_thread_written_in_one_instant_lands_in_one_slot(tmp_path: Path) -> None:
    """About 7% of the real files carry one timestamp on every record: no duration, one slot."""
    now = at("2026-09-29T23:59:59Z")
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=now)
    log.add("turn_context", {"turn_id": "t1", "model": "a"}, at=now).usage_record(counts(1000, 0, 10), at=now)
    log.add("turn_context", {"turn_id": "t2", "model": "b"}, at=now).usage_record(counts(2000, 0, 20), "t2", at=now)
    rec = parse(log)
    assert rec.slots == {("2026-09-29T23:45:00Z", "a"): Usage(input=1000, output=10), ("2026-09-29T23:45:00Z", "b"): Usage(input=2000, output=20)}
    assert (rec.started_at, rec.ended_at, rec.wall_seconds) == ("2026-09-29T23:59:59Z", "2026-09-29T23:59:59Z", 0)


@pytest.mark.parametrize("stamp", ["not a time", "", 12345, None, ["2026-09-30T00:05:00Z"], "2026-13-45T99:99:99Z"])
def test_a_usage_record_without_a_usable_time_falls_back_to_the_first_timestamp_of_the_session(tmp_path: Path, stamp: Any) -> None:
    lost = {"timestamp": stamp, "ordinal": 0, "type": "token_usage_record", "payload": {"thread_id": THREAD, "turn_id": "t1", "response_id": "lost", "usage": counts(500, 0, 5)}}
    log = Log(tmp_path, with_meta=False).raw(json.dumps(lost))  # before any record that has a time
    log.add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=at("2026-09-29T23:50:00Z"))
    turn_at(log, "2026-09-29T23:50:01Z", "m")
    response_at(log, "2026-09-30T00:05:00Z", 1000)
    log.raw(json.dumps({**lost, "payload": {**lost["payload"], "response_id": "lost-too"}}))  # and after them
    rec = parse(log)
    assert rec.slots == {
        ("2026-09-29T23:45:00Z", "m"): Usage(input=1000, output=10),
        ("2026-09-30T00:00:00Z", "m"): Usage(input=1000, output=10),
    }
    assert rec.usage == {"m": Usage(input=2000, output=20)}
    assert rec.turns == 3


def test_a_usage_record_with_no_timestamp_key_falls_back_too(tmp_path: Path) -> None:
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=at("2026-09-29T23:50:00Z"))
    turn_at(log, "2026-09-29T23:50:01Z", "m")
    log.raw(json.dumps({"type": "token_usage_record", "payload": {"thread_id": THREAD, "turn_id": "t1", "response_id": "x", "usage": counts(500, 0, 5)}}))
    response_at(log, "2026-09-30T00:05:00Z", 1000)
    assert parse(log).slots == {("2026-09-29T23:45:00Z", "m"): Usage(input=500, output=5), ("2026-09-30T00:00:00Z", "m"): Usage(input=1000, output=10)}


def test_a_record_time_in_another_utc_offset_is_read_as_the_instant_it_names(tmp_path: Path) -> None:
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=at("2026-09-29T23:50:00Z"))
    turn_at(log, "2026-09-29T23:50:01Z", "m")
    usage = counts(1000, 0, 10)
    log.raw(json.dumps({"timestamp": "2026-09-30T02:05:00+03:00", "ordinal": 9, "type": "token_usage_record", "payload": {"thread_id": THREAD, "turn_id": "t1", "response_id": "tz", "usage": usage}}))
    assert parse(log).slots == {("2026-09-29T23:00:00Z", "m"): Usage(input=1000, output=10)}


def random_log(tmp_path: Path, rng: random.Random) -> Log:
    """A rollout of random responses at random (even out-of-order) times, in every shape the parser reads."""
    log = Log(tmp_path, with_meta=False).add("session_meta", {"id": THREAD, "cwd": "/w/proj"}, at=rng.uniform(-200_000, 0))
    models = ["gpt-a", "gpt-b", "gpt-c"]
    turn = 0
    for _ in range(rng.randint(1, 40)):
        when = rng.choice([rng.uniform(-200_000, 200_000), rng.uniform(-900, 900), 0.0])
        if rng.random() < 0.25:
            turn += 1
            log.event("task_started", when, turn_id=f"t{turn}").add("turn_context", {"turn_id": f"t{turn}", "model": rng.choice(models)}, at=when)
        used = counts(rng.randint(0, 50_000), rng.randint(0, 20_000), rng.randint(0, 5_000), rng.randint(0, 1_000), rng.randint(0, 100))
        used["cached_input_tokens"] = min(used["cached_input_tokens"], used["input_tokens"])
        shape = rng.choice(["both", "both", "record", "count", "record-twice"])
        if shape != "count":
            log.usage_record(used, f"t{turn}", response_id=f"r{len(log.rows)}", at=when)
        if shape == "record-twice":
            log.usage_record(used, f"t{turn}", response_id=f"r{len(log.rows) - 1}", at=when + 1)
        if shape in ("both", "count"):
            log.token_count(used, at=when + rng.choice([0, 0, 1, 1000]))
    for k, row in enumerate(log.rows):
        if rng.random() < 0.15:  # a record whose time was lost or mangled
            rec = json.loads(row)
            if rng.random() < 0.5:
                del rec["timestamp"]
            else:
                rec["timestamp"] = rng.choice(["junk", "", 7, None, "2026-99-99T00:00:00Z"])
            log.rows[k] = json.dumps(rec)
    return log


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_the_slots_always_add_up_to_the_usage_per_model(tmp_path: Path, seed: int) -> None:
    log = random_log(tmp_path, random.Random(seed))
    records = PARSER.parse(log.write())
    if not records:  # nothing billed, or no record with a usable time
        return
    (rec,) = records
    assert rec.slots
    assert_slots_add_up(rec)
    assert sum(rec.slots.values(), Usage()) == rec.total_usage
    assert {model for _, model in rec.slots} == set(rec.usage)
    assert PARSER.parse(log.path)[0].slots == rec.slots  # the same file gives the same answer


# ---- tool calls, errors, files ------------------------------------------------------------------
@pytest.mark.parametrize(
    "output",
    [
        [{"type": "input_text", "text": "Script failed\nWall time 1.0 seconds\nOutput:\n"}, {"type": "input_text", "text": "x"}],
        "Exit code: 1\nWall time: 0.1 seconds\nOutput:\nboom",
        "Chunk ID: ab12\nWall time: 0.0 seconds\nProcess exited with code 127\nOriginal token count: 3\nOutput:\nnope",
        json.dumps({"output": "x" * 500, "metadata": {"exit_code": 2, "duration_seconds": 0.1}}),
        {"content": "boom", "success": False},
    ],
)
def test_outputs_that_show_a_failure_count_as_one_error(tmp_path: Path, output: Any) -> None:
    rec = parse(Log(tmp_path).turn("m").call("c1").result("c1", output).response(10))
    assert (rec.tool_calls, rec.error_retries) == (1, 1)


@pytest.mark.parametrize(
    "output",
    [
        [{"type": "input_text", "text": "Script completed\nWall time 1.0 seconds\nOutput:\n"}, {"type": "input_text", "text": "Exit code: 3"}],
        [{"type": "input_text", "text": "Script completed\nOutput:\nExit code: 3"}],
        "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nfine",
        json.dumps({"output": "ok", "metadata": {"exit_code": 0}}),
        {"content": "fine", "success": True},
        "",
        None,
    ],
)
def test_outputs_that_succeeded_are_not_errors(tmp_path: Path, output: Any) -> None:
    assert parse(Log(tmp_path).turn("m").call("c1").result("c1", output).response(10)).error_retries == 0


def test_a_failing_command_inside_a_call_counts_once_even_if_the_output_also_fails(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("m").call("c1").item("CommandExecution", status="failed", exit_code=1).item("CommandExecution", status="failed", exit_code=2)
    log.result("c1", [{"type": "input_text", "text": "Script failed\nOutput:\n"}]).response(10)
    log.call("c2").item("CommandExecution", status="completed", exit_code=0).result("c2", "Script completed")
    log.call("c3").item("McpToolCall", status="failed").result("c3", "Script completed")
    log.call("c4").item("FileChange", status="failed", changes={}).result("c4", "Script completed")
    log.call("c5").item("FileChange", status="declined", changes={"a.py": {"type": "update"}}).result("c5", "Script completed")
    log.call("c6").item("CommandExecution", exit_code=7).result("c6", "Script completed")  # no status: the exit code decides
    rec = parse(log)
    assert (rec.tool_calls, rec.error_retries) == (6, 4)  # c1, c3, c4, c6


def test_a_command_run_by_the_user_outside_any_tool_call_is_not_an_error(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("m").item("CommandExecution", status="failed", exit_code=1).call("c1").result("c1", "Script completed")
    assert parse(log.response(10)).error_retries == 0


def test_tool_calls_are_distinct_by_call_id_and_include_shell_calls(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("m").call("c1").call("c1").call("c2", "function_call", "js", arguments="{}")
    log.call("c3", "local_shell_call", name="shell", action={"type": "exec", "command": ["ls"]}).response(10)
    log.add("response_item", {"type": "message", "role": "assistant", "content": []}).add("response_item", {"type": "reasoning", "summary": []})
    assert parse(log).tool_calls == 3


def test_patch_headers_give_the_distinct_files_touched_when_the_log_has_no_file_change_items(tmp_path: Path) -> None:
    js = 'await tools.apply_patch("*** Begin Patch\\n*** Update File: src/a.py\\n@@\\n-x\\n+y\\n*** Add File: docs/b file.md\\n+hi\\n*** End Patch");'
    real_newlines = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** Delete File: old/c.py\n*** Move to: new/d.py\n*** End Patch"
    arguments = json.dumps({"command": ["apply_patch", "*** Begin Patch\n*** Add File: e.txt\n+hi\n*** End Patch"]})
    log = Log(tmp_path).turn("m").call("c1", input=js).call("c2", name="apply_patch", input=real_newlines)
    log.call("c3", "function_call", "shell", arguments=arguments)
    log.call("c4", "local_shell_call", action={"type": "exec", "command": ["apply_patch", "*** Begin Patch\n*** Update File: f.py\n*** End Patch"]})
    log.call("c5", input="await tools.exec_command({cmd: 'echo *** Update File: not a patch'})").response(10)
    assert parse(log).files_touched == 6  # a.py (twice), b file.md, c.py, d.py, e.txt, f.py


def test_file_change_items_are_the_files_touched_when_present(tmp_path: Path) -> None:
    patch = 'await tools.apply_patch("*** Begin Patch\\n*** Update File: x.py\\n*** Update File: y.py\\n*** End Patch");'
    log = Log(tmp_path).turn("m").call("c1", input=patch)
    log.item("FileChange", status="completed", changes={"/w/x.py": {"type": "update", "move_path": None}, "/w/n.py": {"type": "add", "content": ""}})
    log.item("FileChange", status="completed", changes={"/w/x.py": {"type": "update", "move_path": "/w/moved.py"}})
    log.item("FileChange", status="failed", changes={"/w/never.py": {"type": "add", "content": ""}})
    assert parse(log.response(10)).files_touched == 3  # x.py, n.py, moved.py: the patch text and the failed change are not used


# ---- prompts ------------------------------------------------------------------------------------
def test_prompts_are_the_user_message_items(tmp_path: Path) -> None:
    text = [{"type": "text", "text": "hello", "text_elements": []}]
    log = Log(tmp_path).turn("m").item("UserMessage", content=text).item("AgentMessage", content=text).item("UserMessage", content=text)
    log.add("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /w\n\n<INSTRUCTIONS>x</INSTRUCTIONS>"}]})
    log.add("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context><cwd>/w</cwd></environment_context>"}]})
    assert parse(log.response(10)).prompts == 2


def test_older_logs_count_user_message_events_and_items_win_when_both_exist(tmp_path: Path) -> None:
    events = Log(tmp_path / "a").turn("m").event("user_message", message="hi").event("user_message", message="again").response(10)
    assert parse(events).prompts == 2
    both = Log(tmp_path / "b").turn("m").event("user_message", message="hi").item("UserMessage", content=[]).response(10)
    assert parse(both).prompts == 1


def test_messages_the_paseo_orchestrator_wrote_are_not_prompts(tmp_path: Path) -> None:
    """``<paseo-system>`` wraps agent replies and permission requests; browser context is prepended to a typed message."""
    relayed = [{"type": "text", "text": "<paseo-system>\n<agent-response>done</agent-response>\n</paseo-system>", "text_elements": []}]
    browser = [{"type": "text", "text": "<in-app-browser-context>tab</in-app-browser-context>\nmake the header blue", "text_elements": []}]
    answer = [{"type": "text", "text": "<send_user_message_question_reply>yes</send_user_message_question_reply>", "text_elements": []}]
    log = Log(tmp_path / "a").turn("m").item("UserMessage", content=relayed).item("UserMessage", content=browser).item("UserMessage", content=answer)
    log.item("UserMessage", content=[{"type": "text", "text": "  <paseo-system source='x'>ping</paseo-system>"}]).item("UserMessage", content=relayed[0]["text"])
    assert parse(log.response(10)).prompts == 2
    events = Log(tmp_path / "b").turn("m").event("user_message", message="<paseo-system>x</paseo-system>").event("user_message", message="hi").event("user_message")
    assert parse(events.response(10)).prompts == 2  # a message that is missing still counts, as before


def test_sub_agent_threads_have_no_prompts(tmp_path: Path) -> None:
    for i, source in enumerate(({"subagent": {"thread_spawn": {"depth": 1}}}, {"subagent": {"other": "guardian"}})):
        log = Log(tmp_path / str(i), source=source, parent_thread_id="01a0aaaa-0000-7000-8000-000000000000")
        assert parse(log.turn("m").item("UserMessage", content=[]).response(10)).prompts == 0


# ---- project ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("meta", "project"),
    [
        ({"cwd": "/home/dev/checkout", "git": {"repository_url": "https://github.com/acme/widgets.git"}}, "widgets"),
        ({"cwd": "/home/dev/checkout", "git": {"repository_url": "https://token@github.com/acme/widgets"}}, "widgets"),
        ({"cwd": "/home/dev/checkout", "git": {"repository_url": "git@github.com:acme/widgets.git"}}, "widgets"),
        ({"cwd": "/home/dev/checkout", "git": {"repository_url": "git@example.com:widgets.git"}}, "widgets"),
        ({"cwd": "/home/dev/checkout", "git": {"branch": "main", "commit_hash": "abc"}}, "checkout"),
        ({"cwd": "/home/dev/checkout", "git": {"repository_url": ""}}, "checkout"),
        ({"cwd": "/home/dev/checkout/"}, "checkout"),
        ({"cwd": "C:\\Users\\dev\\src\\app"}, "app"),
        ({}, "unknown"),
    ],
)
def test_project_is_the_repository_name_or_the_directory_name(tmp_path: Path, meta: dict[str, Any], project: str) -> None:
    rec = parse(Log(tmp_path, **{"cwd": None, **meta}).turn("m").response(10))
    assert rec.project == project
    assert "/" not in rec.project and "\\" not in rec.project


# ---- time ---------------------------------------------------------------------------------------
def test_active_time_leaves_out_idle_gaps(tmp_path: Path) -> None:
    log = Log(tmp_path, with_meta=False)
    log.add("session_meta", {"id": THREAD, "cwd": "/w/p"}, at=0)
    log.add("turn_context", {"turn_id": "t1", "model": "m"}, at=10)
    log.usage_record(counts(10), at=300)
    log.event("task_started", at=300 + 3600, turn_id="t2")  # an hour of silence
    log.event("task_started", at=300 + 3600 + 120, turn_id="t3")
    rec = parse(log)
    assert (rec.started_at, rec.ended_at) == ("2026-09-29T10:00:00Z", "2026-09-29T11:07:00Z")
    assert rec.wall_seconds == 4020
    assert rec.active_seconds == 300 + 120  # the hour is idle
    assert parse(log, idle_gap_seconds=4000).active_seconds == 4020


def test_sub_second_records_still_add_up(tmp_path: Path) -> None:
    """Records are cut to whole seconds, not gap by gap: 100 records 0.4 s apart are 40 s of activity, not 0."""
    log = Log(tmp_path, with_meta=False)
    for k in range(100):
        log.event("task_started", at=0.4 * k, turn_id="t1")
    log.add("turn_context", {"turn_id": "t1", "model": "m"}, at=40.0).usage_record(counts(10), at=40.0)
    rec = parse(log)
    assert (rec.wall_seconds, rec.active_seconds) == (40, 40)


# ---- robustness ---------------------------------------------------------------------------------
def test_empty_and_garbage_files_yield_no_session(tmp_path: Path) -> None:
    blobs = {
        "empty": b"",
        "blank lines": b"\n\n   \n",
        "binary": bytes(range(256)) * 20,
        "not objects": b'[1,2,3]\n"text"\n42\nnull\n',
        "truncated": b'{"timestamp":"2026-09-29T10:00:00.000Z","ordinal":0,"type":"session_me',
        "bad utf8": b'\xff\xfe{"timestamp":\n',
    }
    for name, blob in blobs.items():
        path = tmp_path / f"rollout-2026-09-29T10-00-00-{name.replace(' ', '-')}.jsonl"
        path.write_bytes(blob)
        assert PARSER.parse(path) == [], name


def test_a_session_with_no_token_data_is_omitted(tmp_path: Path) -> None:
    assert PARSER.parse(Log(tmp_path).turn("m").call("c1").write()) == []


def test_bad_records_around_good_ones_are_skipped(tmp_path: Path) -> None:
    log = Log(tmp_path).turn("m")
    log.raw("{not json").raw("[]").raw('{"type":"event_msg","payload":"text"}').raw('{"timestamp":5,"type":"turn_context","payload":{"model":7}}')
    log.event("token_count", info="nonsense").event("token_count", info={"total_token_usage": [1, 2]}).event("token_count", info={"total_token_usage": {"input_tokens": "9", "output_tokens": -4}})
    log.usage_record({"input_tokens": True, "output_tokens": 1.5}).usage_record("not a dict").add("token_usage_record", "not a payload")
    log.add("response_item", {"type": "custom_tool_call", "call_id": 7, "input": 9}).add("response_item", {"type": "function_call_output", "output": {"content": 3}})
    log.add("future", None).add("response_item", []).item("FileChange", changes="x").item("Unknown").event("item_completed", item="text")
    log.response(1000, 0, 100)
    rec = parse(log)
    assert only_usage(rec) == Usage(input=1000, output=100)
    assert rec.tool_calls == 1


@pytest.mark.parametrize("junk", [{"a": 1}, ["b"], 7, None])
def test_types_that_are_not_strings_are_skipped(tmp_path: Path, junk: Any) -> None:
    """A dict or list as a record, payload or item type is unhashable: it must not abort the whole file."""
    log = Log(tmp_path).turn("m")
    log.raw(json.dumps({"timestamp": "2026-09-29T10:00:05.000Z", "ordinal": 90, "type": junk, "payload": {"type": "token_count"}}))
    log.raw(json.dumps({"timestamp": "2026-09-29T10:00:06.000Z", "ordinal": 91, "type": "event_msg", "payload": {"type": junk}}))
    log.raw(json.dumps({"timestamp": "2026-09-29T10:00:07.000Z", "ordinal": 92, "type": "response_item", "payload": {"type": junk, "call_id": "c9"}}))
    log.item(junk).item(junk, status="failed")
    rec = parse(log.response(1000, 0, 100))
    assert only_usage(rec) == Usage(input=1000, output=100)
    assert (rec.tool_calls, rec.error_retries, rec.prompts) == (0, 0, 0)


def test_token_count_with_null_info_is_ignored(tmp_path: Path) -> None:
    rec = parse(Log(tmp_path).turn("m").response(1000, 0, 100).token_count(None).token_count(None))
    assert only_usage(rec) == Usage(input=1000, output=100)
    assert rec.turns == 1


def test_a_log_cut_off_in_the_middle_keeps_what_was_complete(tmp_path: Path) -> None:
    path = Log(tmp_path).turn("m").response(1000, 0, 100).response(2000, 0, 200).write()
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 40])  # the last token_count is cut, its usage record is whole
    rec = parse(path)
    assert only_usage(rec) == Usage(input=3000, output=300)


def test_the_session_id_falls_back_to_the_file_name_without_a_meta_record(tmp_path: Path) -> None:
    log = Log(tmp_path, with_meta=False)
    rec = parse(log.turn("m").response(10))
    assert rec.session_id == THREAD
    assert rec.project == "unknown"
    odd = Log(tmp_path, "rollout-odd-name.jsonl", with_meta=False).turn("m").response(10).write()
    assert PARSER.parse(odd)[0].session_id == "rollout-odd-name"


def test_an_unreadable_file_raises_so_the_ingest_report_names_it(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        PARSER.parse(tmp_path / "gone.jsonl")

"""Gemini CLI parser: rules of ``agentfare.parsers.gemini`` against the fixture and hand-built logs.

Hand-computed expectations for the committed fixture
``tests/fixtures/gemini/tmp/demo-notes-app-1/chats/session-2026-09-30T09-00-9f3c2a71.jsonl``
(one session, model ``gemini-2.5-pro``, project root ``/home/demo/projects/demo-notes-app``):

    Usage = (input - cached + tool, cached, 0, output + thoughts)

    response  input   output  cached  thoughts  tool   ->  input    cache_read  output
    g1        10000     500       0       500      0   ->  10000          0      1000
    g2        30000    1000   20000       500   2000   ->  12000      20000      1500
    g3        40000    1000   20000      1000      0   ->  20000      20000      2000   rewound, still billed
    g4        40000     500   20000       500      0   ->  20000      20000      1000   replaces a 50000/1000 line
    g5        50000    3000   20000      1000      0   ->  30000      20000      4000
    total                                              ->  92000      80000      9500   (cache_write 0)

    g1 and g4 are written twice and g2 three times under the same id: the last record wins, so
    each is one turn (5 turns). g1's first version already carries the tokens, so counting every
    line would double them; g2's first version has ``tokens: null``.

    tool calls  g1: read_file, list_directory | g2: replace notes.py, write_file config.py (error)
                g3: read_file | g4: write_file storage module, run_shell_command      = 7
    error_retries  1 (the failed write_file)
    files_touched  2 (notes.py, storage module; the failed write to config.py does not count)
    prompts        3 (u1, u2, u3). Not prompts: the <session_context> message, the four
                   functionResponse carriers, and nothing else.
    time           start 09:00:00, end 09:09:55 (last lastUpdated / g5), wall 595 s. Gaps between
                   consecutive events: 10 28 3 2 1 68 3 1 1 83 45 3 1 31 20 260 5 6 1 23 0 = 595 s,
                   all under the 30 min idle gap. With a 60 s idle gap the 68, 83 and 260 s gaps
                   drop out: 595 - 411 = 184 s.
    price          at the gemini-2.5-pro rates in pricing.yaml (checked 2026-09-30: input 1.25, cached
                   0.125, output 10.00 per 1M tokens): 0.115 + 0.010 + 0.095 = $0.22.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agentfare.db import Store
from agentfare.ingest import ingest
from agentfare.models import SessionRecord, Usage, slot_of
from agentfare.parsers import get_parser
from agentfare.parsers.gemini import PARSER

MARKER = "PRIVACY-CANARY-GEMINI"
FIXTURE_TMP = Path(__file__).parent / "fixtures" / "gemini" / "tmp"
FIXTURE_FILE = FIXTURE_TMP / "demo-notes-app-1" / "chats" / "session-2026-09-30T09-00-9f3c2a71.jsonl"
PRO = "gemini-2.5-pro"
FLASH = "gemini-2.5-flash"
SID = "abcd1234-0000-4000-8000-000000000001"
BASE = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)


# ---- builders for hand-made logs ---------------------------------------------------------------
def at(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sec(seconds: float) -> str:
    """``at()`` as the parser reports it: whole seconds."""
    return at(seconds)[:-5] + "Z"


def header(session_id: str = SID, *, start: float = 0, kind: str = "main") -> dict[str, Any]:
    return {"sessionId": session_id, "projectHash": "0" * 64, "startTime": at(start), "lastUpdated": at(start),
            "kind": kind}


def tokens(input: int = 0, output: int = 0, cached: int = 0, thoughts: int = 0, tool: int = 0) -> dict[str, int]:
    return {"input": input, "output": output, "cached": cached, "thoughts": thoughts, "tool": tool,
            "total": input + output + thoughts + tool}


def user(mid: str, when: float, content: Any = f"{MARKER} prompt") -> dict[str, Any]:
    return {"id": mid, "timestamp": at(when), "type": "user", "content": content}


def call(name: str = "read_file", status: str = "success", path: str | None = None,
         when: float | None = None) -> dict[str, Any]:
    rec: dict[str, Any] = {"id": f"{name}-{status}", "name": name, "status": status,
                           "args": {"file_path": path} if path else {"command": MARKER}, "result": MARKER}
    if when is not None:
        rec["timestamp"] = at(when)
    return rec


def reply(mid: str, when: float, tok: dict[str, int] | None = None, *, model: str = PRO,
          calls: list[Any] | None = None) -> dict[str, Any]:
    rec: dict[str, Any] = {"id": mid, "timestamp": at(when), "type": "gemini", "content": f"{MARKER} reply",
                           "tokens": tok, "model": model}
    if calls is not None:
        rec["toolCalls"] = calls
    return rec


def write_lines(path: Path, records: list[Any]) -> Path:
    """One JSON object per line; strings are written verbatim so tests can plant broken lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def chats_dir(tmp_path: Path, slug: str = "proj") -> Path:
    return tmp_path / "tmp" / slug / "chats"


def make_session(tmp_path: Path, *records: Any, session_id: str = SID, slug: str = "proj",
                 with_header: bool = True) -> Path:
    path = chats_dir(tmp_path, slug) / f"session-2026-09-30T10-00-{session_id[:8]}.jsonl"
    return write_lines(path, [header(session_id), *records] if with_header else list(records))


def parse_one(path: Path, **kw: Any):
    (record,) = PARSER.parse(path, **kw)
    return record


# ---- the committed fixture ---------------------------------------------------------------------
def test_parser_is_registered() -> None:
    assert get_parser("gemini") is PARSER
    assert PARSER.agent == "gemini"


def test_fixture_session_totals() -> None:
    rec = parse_one(FIXTURE_FILE)
    assert rec.agent == "gemini"
    assert rec.session_id == "9f3c2a71-5b8e-4d0a-9c1e-2b7d4a6e8f10"
    assert rec.source_path == str(FIXTURE_FILE)
    assert rec.usage == {PRO: Usage(input=92000, cache_read=80000, cache_write=0, output=9500)}
    assert rec.primary_model == PRO
    assert rec.turns == 5
    assert rec.tool_calls == 7
    assert rec.error_retries == 1
    assert rec.files_touched == 2
    assert rec.prompts == 3
    # 09:00:38 to 09:09:55 is inside one slot: one (slot, model) entry holding all of the usage
    assert rec.slots == {("2026-09-30T09:00:00Z", PRO): rec.usage[PRO]}
    slots_add_up(rec)


def test_fixture_project_comes_from_project_root_not_the_slug() -> None:
    # the slug directory is "demo-notes-app-1" (a name collision suffix); .project_root has the real name
    assert FIXTURE_FILE.parent.parent.name == "demo-notes-app-1"
    assert parse_one(FIXTURE_FILE).project == "demo-notes-app"


def test_fixture_time_span_and_active_seconds() -> None:
    rec = parse_one(FIXTURE_FILE)
    assert (rec.started_at, rec.ended_at) == ("2026-09-30T09:00:00Z", "2026-09-30T09:09:55Z")
    assert (rec.wall_seconds, rec.active_seconds) == (595, 595)
    assert parse_one(FIXTURE_FILE, idle_gap_seconds=60).active_seconds == 184


def test_fixture_looks_like_a_light_session() -> None:
    rec = parse_one(FIXTURE_FILE)
    assert rec.tool_calls < 40 and rec.files_touched < 5 and rec.error_retries < 4
    assert rec.active_seconds < 20 * 60 and rec.total_usage.total_input < 300_000


def fixture_records() -> tuple[list[dict[str, Any]], list[str]]:
    """(records that parse, lines that do not) of the committed session file."""
    good, bad = [], []
    for line in FIXTURE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            good.append(json.loads(line))
        except ValueError:
            bad.append(line)
    return good, bad


def test_fixture_contains_every_kind_of_record_the_parser_must_survive() -> None:
    good, bad = fixture_records()
    assert len(bad) == 1  # a line cut off mid-write
    ids = [r["id"] for r in good if "id" in r]
    assert len(ids) > len(set(ids))  # messages re-appended under the same id
    assert any("$set" in r for r in good) and any("$rewindTo" in r for r in good)
    assert any(set(r) == {"$telemetry"} for r in good)  # a record type this parser has never heard of


def test_fixture_marker_is_planted_wherever_content_lives() -> None:
    good, bad = fixture_records()
    latest = {r["id"]: r for r in good if "id" in r}
    assert MARKER in json.dumps(latest["u1"]["content"])
    assert MARKER in latest["g1"]["content"] and MARKER in json.dumps(latest["g1"]["thoughts"])
    first_call = latest["g1"]["toolCalls"][0]
    assert MARKER in json.dumps(first_call["result"]) and MARKER in first_call["resultDisplay"]
    assert MARKER in json.dumps(latest["g4"]["toolCalls"][0]["args"])  # inside a file *name*
    assert MARKER in json.dumps(latest["ur1"]["content"])
    assert any(MARKER in str(r.get("$set", {}).get("summary")) for r in good)
    assert MARKER in bad[0]


def test_marker_never_reaches_the_record() -> None:
    rec = parse_one(FIXTURE_FILE)
    assert MARKER not in repr(rec)
    assert MARKER not in str(asdict(rec))


def test_ingest_end_to_end_stores_counts_and_no_content(make_config, tmp_path: Path) -> None:
    cfg = make_config(timezone="UTC", sources={"gemini": [str(FIXTURE_TMP)]})
    db = tmp_path / "gemini.sqlite"
    with Store(db) as store:
        first = ingest(cfg, store, ["gemini"])
        assert (first.files_seen, first.sessions, first.errors) == (1, 1, [])
        second = ingest(cfg, store, ["gemini"])
        assert (second.files_unchanged, second.files_parsed) == (1, 0)
        (stored,) = store.sessions()
        assert stored.usage == {PRO: Usage(92000, 80000, 0, 9500)}
        assert (stored.project, stored.tool_calls, stored.files_touched, stored.prompts) == ("demo-notes-app", 7, 2, 3)
    assert MARKER.encode() not in db.read_bytes()


# ---- discovery ---------------------------------------------------------------------------------
def test_discover_finds_the_fixture_session_only() -> None:
    assert list(PARSER.discover(FIXTURE_TMP)) == [FIXTURE_FILE]


def test_discover_lists_sessions_not_subagents_or_leftovers(tmp_path: Path) -> None:
    chats = chats_dir(tmp_path)
    for name in (
        "session-2026-09-30T10-00-aaaaaaaa.jsonl",
        "session-2026-09-30T10-05-bbbbbbbb.json",  # legacy form, alone
        "session-2026-09-30T10-10-cccccccc.json",  # legacy form ...
        "session-2026-09-30T10-10-cccccccc.jsonl",  # ... migrated to JSONL: only the JSONL is listed
        "session-2026-09-30T10-15-dddddddd.jsonl.unreadable-1790000000000",
        "session-2026-09-30T10-15-dddddddd.jsonl.tmp-4242",
        "notes.jsonl",
        "session-2026-09-30T10-20-eeeeeeee.txt",
    ):
        write_lines(chats / name, [header()])
    write_lines(chats / SID / "agent-1.jsonl", [header("agent-1", kind="subagent")])
    write_lines(tmp_path / "tmp" / "proj" / "logs" / "session-x.jsonl", [header()])
    write_lines(tmp_path / "tmp" / "proj" / "tool-outputs" / "chats" / "session-2026-09-30T10-25-ffffffff.jsonl", [header()])
    assert [p.name for p in PARSER.discover(tmp_path)] == [
        "session-2026-09-30T10-00-aaaaaaaa.jsonl",
        "session-2026-09-30T10-05-bbbbbbbb.json",
        "session-2026-09-30T10-10-cccccccc.jsonl",
    ]


def test_discover_walks_every_project_in_order_and_accepts_a_chats_dir_as_root(tmp_path: Path) -> None:
    for slug in ("zeta", "alpha"):
        make_session(tmp_path, slug=slug)
    found = [p.parent.parent.name for p in PARSER.discover(tmp_path / "tmp")]
    assert found == ["alpha", "zeta"]
    assert [p.name for p in PARSER.discover(chats_dir(tmp_path, "alpha"))] == [f"session-2026-09-30T10-00-{SID[:8]}.jsonl"]


def test_discover_finds_a_project_whose_slug_is_chats(tmp_path: Path) -> None:
    # slug = slugified basename of the project directory, so ~/dev/chats lives in tmp/chats/chats/
    inner = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1)), slug="chats")
    write_lines(inner.parent / SID / "agent-1.jsonl", [header("agent-1", kind="subagent")])
    assert inner.parent.parent.name == "chats" and inner.parent.name == "chats"
    assert list(PARSER.discover(tmp_path / "tmp")) == [inner]
    assert parse_one(inner).project == "chats"


# ---- replace-by-id, $rewindTo, $set ------------------------------------------------------------
def test_last_record_with_an_id_wins_whatever_it_carries(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        user("u1", 1),
        reply("g1", 5, None),  # tokens arrive later
        reply("g1", 5, tokens(input=1000, output=100)),  # provisional counts
        reply("g1", 5, tokens(input=2000, output=200, cached=500), calls=[call("read_file", when=6)]),
        reply("g1", 5, tokens(input=2000, output=200, cached=500), calls=[call("read_file", when=6)]),  # identical replay
    )
    rec = parse_one(path)
    assert rec.usage == {PRO: Usage(input=1500, cache_read=500, output=200)}
    assert (rec.turns, rec.tool_calls) == (1, 1)


def test_replacing_a_message_can_also_remove_what_the_earlier_version_had(tmp_path: Path) -> None:
    path = make_session(
        tmp_path, user("u1", 1),
        reply("g1", 5, tokens(input=100, output=10), calls=[call("write_file", path="/p/a.py"), call("read_file", "error")]),
        reply("g1", 5, tokens(input=100, output=10), calls=[call("read_file")]),
    )
    rec = parse_one(path)
    assert (rec.tool_calls, rec.files_touched, rec.error_retries) == (1, 0, 0)


def test_rewound_responses_are_still_billed(tmp_path: Path) -> None:
    # The CLI's loader drops everything from the rewound id on, but those responses cost money.
    path = make_session(
        tmp_path,
        user("u1", 1), reply("g1", 5, tokens(input=1000, output=100)),
        user("u2", 10), reply("g2", 15, tokens(input=3000, output=300), calls=[call("write_file", path="/p/x.py")]),
        {"$rewindTo": "u2"},
        user("u3", 20), reply("g3", 25, tokens(input=5000, output=500)),
        {"$rewindTo": "no-such-message"},  # the loader would clear everything; a bill cannot be un-billed
    )
    rec = parse_one(path)
    assert rec.usage == {PRO: Usage(input=9000, output=900)}
    assert (rec.turns, rec.prompts, rec.tool_calls, rec.files_touched) == (3, 3, 1, 1)
    assert rec.wall_seconds == 25


def test_set_messages_rewrites_do_not_inflate_prompts_or_tokens(tmp_path: Path) -> None:
    # After compaction the CLI writes {"$set": {"messages": [...]}}: the summary, plus the kept
    # tail re-recorded WITHOUT token data under fresh ids. The originals are already in the file.
    copies = [
        user("summary-1", 30, f"<state_snapshot>{MARKER}</state_snapshot>"),
        {"id": "ack-1", "timestamp": at(30), "type": "gemini", "content": "Got it. Thanks for the additional context!"},
        user("copy-of-u2", 30, f"{MARKER} the second prompt again"),
        reply("copy-of-g2", 30, None, calls=[call("write_file", path="/p/copy.py")]),
    ]
    path = make_session(
        tmp_path,
        user("u1", 1), reply("g1", 5, tokens(input=1000, output=100)),
        user("u2", 10), reply("g2", 15, tokens(input=3000, output=300)),
        {"$set": {"messages": copies, "lastUpdated": at(31)}},
        user("u3", 40), reply("g3", 45, tokens(input=500, output=50)),
    )
    rec = parse_one(path)
    assert rec.usage == {PRO: Usage(input=4500, output=450)}
    assert (rec.turns, rec.prompts, rec.tool_calls, rec.files_touched) == (3, 3, 0, 0)


def test_set_patches_only_feed_the_metadata_that_is_used(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        user("u1", 1), reply("g1", 5, tokens(input=100, output=10)),
        {"$set": {"summary": MARKER, "directories": ["/x"], "lastUpdated": at(90)}},
        {"$set": "not a mapping"},
    )
    rec = parse_one(path)
    assert rec.ended_at == sec(90)
    assert MARKER not in repr(rec)


# ---- gemini --session-file ---------------------------------------------------------------------
def imported_session(tmp_path: Path, *later: Any) -> Path:
    """The file ``gemini --session-file`` writes: a fresh header, an ``import-<ms>`` notice, then the messages of the
    source session copied verbatim (tokens and tool calls included, original timestamps), then whatever happens next."""
    copied = [
        user("u1", -3600, f"{MARKER} asked in the source session"),
        reply("g1", -3590, tokens(input=1000, output=100, cached=200),
              calls=[call("write_file", path="/p/a.py", when=-3580), call("read_file", "error", when=-3579)]),
    ]
    notice = {"id": "import-1790000000000", "timestamp": at(0), "type": "info", "content": f"Imported session from {MARKER}"}
    return make_session(tmp_path, notice, *copied, *later, session_id="fedcba98-0000-4000-8000-00000000000f")


def test_messages_copied_in_by_session_file_were_billed_in_the_source_session(tmp_path: Path) -> None:
    path = imported_session(
        tmp_path,
        user("u2", 10), reply("g2", 20, tokens(input=500, output=50), calls=[call("replace", path="/p/b.py", when=21)]),
    )
    rec = parse_one(path)
    assert rec.usage == {PRO: Usage(input=500, output=50)}
    assert (rec.turns, rec.prompts, rec.tool_calls, rec.files_touched, rec.error_retries) == (1, 1, 1, 1, 0)
    assert (rec.started_at, rec.ended_at, rec.wall_seconds) == (sec(0), sec(21), 21)  # not the source session's hour
    assert rec.slots == {("2026-09-30T10:00:00Z", PRO): Usage(input=500, output=50)}  # nor the source session's slots
    assert MARKER not in repr(rec)


def test_a_session_file_import_with_no_new_activity_has_nothing_to_bill(tmp_path: Path) -> None:
    assert PARSER.parse(imported_session(tmp_path)) == []


def test_a_copy_from_the_same_second_as_the_import_notice_is_still_a_copy(tmp_path: Path) -> None:
    notice = {"id": "import-1790000000900", "timestamp": at(0.9), "type": "info", "content": MARKER}
    path = make_session(tmp_path, notice, reply("g1", 0.1, tokens(input=3)), reply("g2", 1.2, tokens(input=4)))
    assert parse_one(path).usage == {PRO: Usage(input=4)}


def test_only_messages_older_than_the_import_notice_are_copies(tmp_path: Path) -> None:
    own = make_session(tmp_path, {"id": "import-1", "type": "info", "timestamp": at(0)}, user("u1", 5), reply("g1", 9, tokens(input=7)))
    assert parse_one(own).usage == {PRO: Usage(input=7)}  # newer than the notice: the session's own
    for notice in ({"id": "import-1", "type": "info"}, {"id": "import-x", "type": "info", "timestamp": at(0)},
                   {"id": "import-1", "type": "user", "timestamp": at(0)}):
        path = make_session(tmp_path, notice, reply("g1", -50, tokens(input=3)), reply("g2", 9, tokens(input=4)))
        assert parse_one(path).usage == {PRO: Usage(input=7)}  # no valid notice: nothing is a copy


# ---- tokens ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # prompt tokens include the cached ones; thoughts are billed as output; tool-use prompt is extra input
        (tokens(input=50000, output=3000, cached=20000, thoughts=1000), Usage(30000, 20000, 0, 4000)),
        (tokens(input=30000, output=1000, cached=20000, thoughts=500, tool=2000), Usage(12000, 20000, 0, 1500)),
        (tokens(input=1000, output=10), Usage(1000, 0, 0, 10)),
        ({"input": 700}, Usage(700, 0, 0, 0)),  # missing counters are zero
        ({"input": 100, "cached": 300, "output": 5}, Usage(0, 300, 0, 5)),  # cached larger than input: input floors at 0
        ({"input": "900", "output": -4, "cached": None, "thoughts": 2.5, "tool": [1]}, Usage()),  # junk counts as zero
        ({"input": True, "output": False, "cached": True, "thoughts": True, "tool": True}, Usage()),  # true is not 1
    ],
)
def test_token_mapping(tmp_path: Path, raw: dict[str, Any], expected: Usage) -> None:
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, raw))
    records = PARSER.parse(path)
    if expected.is_empty():
        assert records == []  # nothing was billed: no record, never an invented one
    else:
        assert records[0].usage == {PRO: expected}


def test_usage_is_kept_per_model(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        user("u1", 1),
        reply("g1", 5, tokens(input=1000, output=100), model=PRO),
        reply("g2", 9, tokens(input=400, output=40, cached=100), model=FLASH),
        reply("g3", 12, tokens(input=2000, output=200), model=PRO),
    )
    rec = parse_one(path)
    assert rec.usage == {PRO: Usage(input=3000, output=300), FLASH: Usage(input=300, cache_read=100, output=40)}
    assert (rec.primary_model, rec.turns) == (PRO, 3)


def nameless(mid: str, when: float, tok: dict[str, int] | None) -> dict[str, Any]:
    """A reply the CLI recorded without a ``model`` key."""
    rec = reply(mid, when, tok)
    del rec["model"]
    return rec


def test_a_response_without_a_model_and_nothing_before_it_is_attributed_to_unknown(tmp_path: Path) -> None:
    path = make_session(
        tmp_path, user("u1", 1), nameless("g1", 5, tokens(input=10, output=1)),
        {**reply("g2", 6, tokens(input=5, output=1)), "model": ["x"]},
    )
    assert parse_one(path).usage == {"unknown": Usage(input=15, output=2)}


def test_an_empty_reply_after_a_tool_result_is_billed_to_the_model_that_asked_for_the_tool(tmp_path: Path) -> None:
    # What GeminiChat does when the model answers a function response with nothing: recordMessageTokens, then
    # recordSyntheticMessage("gemini", []), a message that carries the tokens but no ``model``.
    carrier = {"id": "ur1", "timestamp": at(8), "type": "user", "content": [{"functionResponse": {"id": "c1"}}]}
    path = make_session(
        tmp_path, user("u1", 1),
        reply("g1", 5, tokens(input=1000, output=50, thoughts=100), model=FLASH, calls=[call("read_file", when=7)]),
        carrier,
        nameless("g2", 12, tokens(input=1500, output=0, cached=1000)),
        user("u2", 20), reply("g3", 25, tokens(input=400, output=40), model=PRO),
        carrier | {"id": "ur2", "timestamp": at(26)}, nameless("g4", 30, tokens(input=500, output=0)),
    )
    rec = parse_one(path)
    assert rec.usage == {
        FLASH: Usage(input=1500, cache_read=1000, output=150),  # g1 + g2
        PRO: Usage(input=900, output=40),  # g3 + g4
    }
    assert rec.turns == 4


def test_a_model_less_reply_does_not_borrow_a_model_from_another_file_or_a_later_reply(tmp_path: Path) -> None:
    parent = make_session(
        tmp_path, user("u1", 1), nameless("g1", 5, tokens(input=10)), reply("g2", 9, tokens(input=1), model=FLASH)
    )
    make_subagent(tmp_path, "agent-1", user("t", 10), reply("s1", 12, tokens(input=100), model=PRO))
    make_subagent(tmp_path, "agent-2", user("t", 11), nameless("s2", 13, tokens(input=1000)))
    assert parse_one(parent).usage == {
        "unknown": Usage(input=1010),  # g1 (no reply before it) and s2 (the other sub-agent's model is not its own)
        FLASH: Usage(input=1),
        PRO: Usage(input=100),
    }


def test_messages_that_are_not_model_responses_carry_no_usage(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        {**user("u1", 1), "tokens": tokens(input=999999)},
        {"id": "i1", "timestamp": at(2), "type": "info", "content": MARKER, "tokens": tokens(input=999999)},
        reply("g1", 5, tokens(input=100, output=10)),
    )
    rec = parse_one(path)
    assert rec.usage == {PRO: Usage(input=100, output=10)} and rec.turns == 1


# ---- tool calls, files, errors -----------------------------------------------------------------
def test_tool_call_statuses_and_files(tmp_path: Path) -> None:
    calls = [
        call("write_file", path="/p/a.py"),
        call("replace", path="/p/a.py"),  # same file again
        call("replace", path="/p/b.py"),
        call("write_file", "error", path="/p/never-written.py"),
        call("replace", "cancelled", path="/p/rejected.py"),
        call("write_file", "awaiting_approval", path="/p/pending.py"),
        call("read_file", path="/p/read-only.py"),  # reading is not touching
        call("run_shell_command", "error"),
        call("run_shell_command", "executing"),
        call("write_file", path=""),
        call("write_file"),
    ]
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1), calls=calls))
    rec = parse_one(path)
    assert rec.tool_calls == len(calls)
    assert rec.files_touched == 2
    assert rec.error_retries == 3  # error, cancelled, error; a running or waiting call is not a failure


def shell_call(result: Any, name: str = "run_shell_command", status: str = "success") -> dict[str, Any]:
    return {**call(name, status), "result": result}


def shell_result(output: str, *, wrapped: bool = False) -> list[dict[str, Any]]:
    """How the CLI records what the shell tool handed back: a functionResponse part. CLIs since 0.60 wrap the
    output in ``<untrusted_context>`` tags (``wrapUntrusted``); older ones handed it back bare."""
    if wrapped:
        output = f"<untrusted_context>\n{output}\n</untrusted_context>"
    return [{"functionResponse": {"id": "c1", "name": "run_shell_command", "response": {"output": output}}}]


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "untrusted_context"])
@pytest.mark.parametrize(
    ("output", "failed"),
    [
        ("Output: ok\nProcess Group PGID: 4242", False),
        ("Output: (empty)", False),
        ("Output: FAILED tests/test_x.py\nExit Code: 1\nProcess Group PGID: 4242", True),
        ("Output: (empty)\nExit Code: 127", True),
        ("Output: line one\nline two\n\nExit Code: -1\n", True),
        ("Output: killed\nSignal: SIGTERM\nProcess Group PGID: 9", True),
        ("Output: x\nExit Code: 2\nSignal: SIGINT\nBackground PIDs: 5, 6\nProcess Group PGID: 7", True),
        ("Output: Exit Code: 0\nProcess Group PGID: 4242", False),
        ("Output: x\nExit Code: 0", False),
        ("Output: a build log\nExit Code: 1\nwhich the script itself printed\nProcess Group PGID: 4242", False),
        ("Output: retrying\nExit Code: 1 (retrying)", False),
        ("Exit Code: 3 is in the first line", False),
    ],
)
def test_a_shell_command_that_did_not_exit_cleanly_is_an_error_though_recorded_as_success(
    tmp_path: Path, output: str, failed: bool, wrapped: bool
) -> None:
    result = shell_result(f"{MARKER} {output}", wrapped=wrapped)
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1), calls=[shell_call(result)]))
    rec = parse_one(path)
    assert rec.error_retries == int(failed)
    assert MARKER not in repr(rec)


def test_only_the_shell_tool_reports_its_exit_code_that_way(tmp_path: Path) -> None:
    unclean = "Output: x\nExit Code: 1"
    calls = [
        shell_call(shell_result(unclean), name="read_file"),  # somebody else's output that looks the same
        shell_call(unclean),  # a bare string, as older CLIs stored results
        shell_call({"functionResponse": {"response": {"output": unclean}}}),  # a single part, not a list
        shell_call(shell_result(unclean), status="error"),  # already an error: counted once
        shell_call(shell_result(unclean), status="executing"),  # not finished
    ]
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1), calls=calls))
    assert parse_one(path).error_retries == 3


def test_the_footer_survives_the_clis_truncation_of_a_long_output(tmp_path: Path) -> None:
    # formatTruncatedToolOutput keeps the head and the tail of the wrapped text and prefixes a notice
    tail = "x" * 500 + "\nExit Code: 1\nProcess Group PGID: 4242\n</untrusted_context>"
    out = (
        "Output too large. Showing first 8,000 and last 32,000 characters. For full output see: /t/o.txt\n"
        f"<untrusted_context>\nOutput: {MARKER}\n\n... [90,000 characters omitted] ...\n\n{tail}"
    )
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1), calls=[shell_call(shell_result(out))]))
    assert parse_one(path).error_retries == 1


@pytest.mark.parametrize(
    ("output", "failed"),
    [
        ("Command was automatically cancelled because it exceeded the timeout of 5.0 minutes without output. "
         "Below is the output before it was cancelled:\nwaiting for input", True),
        ("Command was automatically cancelled because it exceeded the timeout of 5.0 minutes without output. "
         "There was no output before it was cancelled.", True),
        ("Output: Command was automatically cancelled because it exceeded the timeout of 5.0 minutes", False),  # printed
        ("Command was cancelled by user before it could complete. There was no output before it was cancelled.", False),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "untrusted_context"])
def test_a_shell_command_killed_for_printing_nothing_is_an_error_though_recorded_as_success(
    tmp_path: Path, output: str, failed: bool, wrapped: bool
) -> None:
    # (the user's own cancellation is recorded as status "cancelled", which is counted through the status)
    result = shell_result(output, wrapped=wrapped)
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1), calls=[shell_call(result)]))
    assert parse_one(path).error_retries == int(failed)


def test_look_alike_lines_do_not_make_the_shell_check_slow(tmp_path: Path) -> None:
    flood = "Output: " + "\nSignal: a" * 10_000 + "\nnot the footer"
    path = make_session(
        tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1), calls=[shell_call(shell_result(flood, wrapped=True))])
    )
    started = time.monotonic()
    assert parse_one(path).error_retries == 0
    assert time.monotonic() - started < 1  # scanning all of it backtracks quadratically: seconds at this size


def test_the_footer_is_found_after_a_long_output(tmp_path: Path) -> None:
    out = "Output: " + "x" * 100_000 + "\nExit Code: 1\nProcess Group PGID: 4242"
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1), calls=[shell_call(shell_result(out))]))
    assert parse_one(path).error_retries == 1


@pytest.mark.parametrize(
    "result",
    [None, [], "", 3, [3, None, []], {"functionResponse": 3}, [{"functionResponse": {"response": 3}}],
     [{"functionResponse": {"response": {"output": ["Exit Code: 1"]}}}], [{"text": "Output: x\nExit Code: 1"}]],
)
def test_a_shell_result_of_an_unexpected_shape_is_not_an_error(tmp_path: Path, result: Any) -> None:
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1), calls=[shell_call(result)]))
    assert parse_one(path).error_retries == 0


def test_tool_calls_across_replies_add_up(tmp_path: Path) -> None:
    path = make_session(
        tmp_path, user("u1", 1),
        reply("g1", 5, tokens(input=1), calls=[call(), call()]),
        reply("g2", 9, tokens(input=1), calls=[call(), call("write_file", path="/p/a.py")]),
        reply("g3", 12, tokens(input=1)),
    )
    rec = parse_one(path)
    assert (rec.tool_calls, rec.files_touched, rec.turns) == (4, 1, 3)


# ---- prompts -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("content", "is_prompt"),
    [
        ("fix the bug", True),  # older CLIs stored a plain string
        ([{"text": "fix the bug"}], True),
        ([{"text": "look at this"}, {"inlineData": {"mimeType": "image/png", "data": "AA=="}}], True),
        ({"text": "a single part object"}, True),
        ([{"text": "fix"}, {"functionResponse": {"id": "x"}}], False),  # a tool result carrier
        ([{"functionResponse": {"id": "x", "name": "read_file", "response": {"output": "..."}}}], False),
        ([{"text": "<session_context>environment</session_context>"}], False),
        ("<hook_context>injected by a hook</hook_context>", False),
        ("<state_snapshot>compaction summary</state_snapshot>", False),
        ("/help", False),  # slash command
        ("?", False),
        ("   ", False),
        ("", False),
        ([{"inlineData": {}}], False),
        ([], False),
        (None, False),
        (42, False),
    ],
)
def test_which_user_messages_are_prompts(tmp_path: Path, content: Any, is_prompt: bool) -> None:
    path = make_session(tmp_path, user("u1", 1, content), reply("g1", 5, tokens(input=1)))
    assert parse_one(path).prompts == int(is_prompt)


def test_only_user_typed_messages_are_prompts(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        user("u1", 1), user("u2", 2), {**user("i1", 3), "type": "info"}, {**user("e1", 4), "type": "error"},
        reply("g1", 5, tokens(input=1), calls=[call()]),
    )
    assert parse_one(path).prompts == 2


# ---- time --------------------------------------------------------------------------------------
def test_timestamps_are_cut_to_whole_seconds_and_converted_to_utc(tmp_path: Path) -> None:
    path = write_lines(
        chats_dir(tmp_path) / f"session-2026-09-30T10-00-{SID[:8]}.jsonl",
        [
            {**header(), "startTime": "2026-09-30T07:00:00.900-03:00", "lastUpdated": "2026-09-30T07:00:00.900-03:00"},
            {**user("u1", 0), "timestamp": "2026-09-30T10:00:00.100Z"},
            {**reply("g1", 0, tokens(input=1)), "timestamp": "2026-09-30T10:00:01.050Z"},
        ],
    )
    rec = parse_one(path)
    assert (rec.started_at, rec.ended_at, rec.wall_seconds) == ("2026-09-30T10:00:00Z", "2026-09-30T10:00:01Z", 1)


def test_unparseable_timestamps_are_skipped(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        {**user("u1", 0), "timestamp": "yesterday"},
        {**reply("g1", 0, tokens(input=1)), "timestamp": 12345},
        {"id": "n1", "type": "info", "timestamp": "0001-01-01T00:00:00+14:00"},
        reply("g2", 30, tokens(input=1)),
    )
    rec = parse_one(path)
    assert (rec.started_at, rec.ended_at, rec.wall_seconds) == (sec(0), sec(30), 30)


def test_tool_call_and_last_updated_times_extend_the_session(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        user("u1", 5),
        reply("g1", 10, tokens(input=1), calls=[call(when=40)]),
        {"$set": {"lastUpdated": at(55)}},
    )
    rec = parse_one(path)
    assert (rec.wall_seconds, rec.active_seconds) == (55, 55)


def test_idle_gaps_are_not_active_time(tmp_path: Path) -> None:
    path = make_session(
        tmp_path, user("u1", 10), reply("g1", 20, tokens(input=1)), user("u2", 7200), reply("g2", 7230, tokens(input=1))
    )
    rec = parse_one(path)
    assert rec.wall_seconds == 7230
    assert rec.active_seconds == 50  # header -> u1 10 s, u1 -> g1 10 s, u2 -> g2 30 s; the 2 h pause is idle
    assert parse_one(path, idle_gap_seconds=5).active_seconds == 0


def test_a_session_without_any_timestamp_cannot_be_dated(tmp_path: Path) -> None:
    path = write_lines(
        chats_dir(tmp_path) / "session-x-y.jsonl",
        [{"id": "g1", "type": "gemini", "model": PRO, "tokens": tokens(input=10, output=1)}],
    )
    assert PARSER.parse(path) == []


# ---- slots: money is attributed to when it was spent -------------------------------------------
def slots_add_up(rec: SessionRecord) -> None:
    """The contract: slots are UTC quarter hours and, per model, add up to ``usage`` exactly."""
    per_model: dict[str, Usage] = {}
    for (slot, model), used in rec.slots.items():
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:(?:00|15|30|45):00Z", slot), slot
        assert not used.is_empty()
        per_model[model] = per_model.get(model, Usage()) + used
    assert per_model == rec.usage


def stamped(mid: str, timestamp: Any, tok: dict[str, int] | None, **kw: Any) -> dict[str, Any]:
    """A reply at an absolute ``timestamp`` (any JSON value)."""
    return {**reply(mid, 0, tok, **kw), "timestamp": timestamp}


def dated_session(tmp_path: Path, first: str, *records: Any, session_id: str = SID) -> Path:
    """A session whose header says it started at the ISO time ``first``."""
    head = {**header(session_id), "startTime": first, "lastUpdated": first}
    return write_lines(chats_dir(tmp_path) / f"session-{first[:10]}T00-00-{session_id[:8]}.jsonl", [head, *records])


def test_a_response_is_attributed_to_the_slot_of_its_own_timestamp(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        user("u1", 1),
        reply("g1", 5, tokens(input=100, output=10)),  # 10:00:05
        reply("g2", 899.9, tokens(input=200, output=20), model=FLASH),  # 10:14:59.9 is still the 10:00 slot
        reply("g3", 900, tokens(input=400, output=40)),  # 10:15:00
        reply("g4", 901, tokens(input=800, output=80)),  # same slot as g3: they add up
        reply("g5", 4000, tokens(input=1600, output=160, cached=600), model=FLASH),  # 11:06:40
    )
    rec = parse_one(path)
    assert rec.slots == {
        ("2026-09-30T10:00:00Z", PRO): Usage(input=100, output=10),
        ("2026-09-30T10:00:00Z", FLASH): Usage(input=200, output=20),
        ("2026-09-30T10:15:00Z", PRO): Usage(input=1200, output=120),
        ("2026-09-30T11:00:00Z", FLASH): Usage(input=1000, cache_read=600, output=160),
    }
    slots_add_up(rec)
    assert rec.effective_slots() is rec.slots  # the parser recorded timing: nothing is placed at the start


def test_a_session_that_runs_past_midnight_utc_splits_where_the_money_was_spent(tmp_path: Path) -> None:
    path = dated_session(
        tmp_path, "2026-10-01T23:50:00Z",
        reply("g1", 0, tokens(input=1000, output=100)) | {"timestamp": "2026-10-01T23:50:30.000Z"},
        stamped("g2", "2026-10-01T20:59:00-03:00", tokens(input=2000, output=200)),  # 23:59Z in another offset
        stamped("g3", "2026-10-01T23:59:59.999Z", tokens(input=4000, output=400)),
        stamped("g4", "2026-10-02T00:00:00.000Z", tokens(input=8000, output=800)),
        stamped("g5", "2026-10-01T21:20:00-03:00", tokens(input=16000, output=1600)),  # 00:20Z the next day
    )
    rec = parse_one(path)
    assert rec.started_at == "2026-10-01T23:50:00Z"  # the session "belongs" to Oct 1, its money does not all
    assert rec.slots == {
        ("2026-10-01T23:45:00Z", PRO): Usage(input=7000, output=700),
        ("2026-10-02T00:00:00Z", PRO): Usage(input=8000, output=800),
        ("2026-10-02T00:15:00Z", PRO): Usage(input=16000, output=1600),
    }
    slots_add_up(rec)


def test_a_session_that_crosses_a_week_boundary_splits_between_sunday_and_monday(tmp_path: Path) -> None:
    assert datetime(2026, 10, 4).isoweekday() == 7 and datetime(2026, 10, 5).isoweekday() == 1
    path = dated_session(
        tmp_path, "2026-10-04T23:40:00Z",
        stamped("g1", "2026-10-04T23:41:00Z", tokens(input=500, output=50), model=FLASH),
        stamped("g2", "2026-10-04T23:58:00Z", tokens(input=700, output=70)),
        stamped("g3", "2026-10-05T00:02:00Z", tokens(input=900, output=90)),
        stamped("g4", "2026-10-05T00:03:00Z", tokens(input=300, output=30), model=FLASH),
    )
    rec = parse_one(path)
    assert rec.slots == {
        ("2026-10-04T23:30:00Z", FLASH): Usage(input=500, output=50),
        ("2026-10-04T23:45:00Z", PRO): Usage(input=700, output=70),
        ("2026-10-05T00:00:00Z", PRO): Usage(input=900, output=90),
        ("2026-10-05T00:00:00Z", FLASH): Usage(input=300, output=30),
    }
    slots_add_up(rec)


def test_a_replaced_message_is_one_response_in_the_slot_of_its_last_record(tmp_path: Path) -> None:
    # The CLI stamps a message once and every rewrite keeps the stamp; the last record is the one that is read.
    path = make_session(
        tmp_path,
        user("u1", 1),
        reply("g1", 5, None), reply("g1", 5, tokens(input=1000, output=100)),
        reply("g1", 5, tokens(input=2000, output=200), calls=[call("read_file", when=6)]),
        reply("g2", 100, tokens(input=300, output=30)),
        reply("g2", 1000, tokens(input=400, output=40)),  # a log whose last record carries another time: that one counts
    )
    rec = parse_one(path)
    assert rec.slots == {
        ("2026-09-30T10:00:00Z", PRO): Usage(input=2000, output=200),
        ("2026-09-30T10:15:00Z", PRO): Usage(input=400, output=40),
    }
    assert rec.turns == 2
    slots_add_up(rec)


def test_a_response_without_a_usable_timestamp_counts_at_the_first_timestamp_of_the_session(tmp_path: Path) -> None:
    no_time = reply("g2", 0, tokens(input=20, output=2))
    del no_time["timestamp"]
    path = make_session(
        tmp_path,
        {"$set": {"startTime": at(-600)}},  # the session started at 09:50: that is where undated money counts
        stamped("g1", "yesterday", tokens(input=10, output=1)),
        no_time,
        stamped("g3", 12345, tokens(input=40, output=4), model=FLASH),
        reply("g4", 2400, tokens(input=80, output=8)),  # 10:40
    )
    rec = parse_one(path)
    assert rec.started_at == sec(-600)
    assert rec.slots == {
        ("2026-09-30T09:45:00Z", PRO): Usage(input=30, output=3),
        ("2026-09-30T09:45:00Z", FLASH): Usage(input=40, output=4),
        ("2026-09-30T10:30:00Z", PRO): Usage(input=80, output=8),
    }
    slots_add_up(rec)


def test_a_model_less_reply_is_in_its_own_slot_under_the_model_before_it(tmp_path: Path) -> None:
    path = make_session(
        tmp_path, user("u1", 1),
        reply("g1", 5, tokens(input=100), model=FLASH),
        nameless("g2", 1000, tokens(input=1500, cached=1000)),  # 10:16:40, served by the FLASH reply before it
    )
    rec = parse_one(path)
    assert rec.slots == {
        ("2026-09-30T10:00:00Z", FLASH): Usage(input=100),
        ("2026-09-30T10:15:00Z", FLASH): Usage(input=500, cache_read=1000),
    }
    slots_add_up(rec)


def test_subagent_responses_are_attributed_by_their_own_timestamps(tmp_path: Path) -> None:
    parent = make_session(
        tmp_path, user("u1", 1),
        reply("g1", 5, tokens(input=1000, output=100)),  # 10:00
        reply("g2", 2500, tokens(input=2000, output=200)),  # 10:41:40
    )
    make_subagent(
        tmp_path, "agent-1", user("task", 10),
        reply("s1", 950, tokens(input=300, output=30), model=FLASH),  # 10:15:50: a slot the parent has no entry in
        reply("s2", 2520, tokens(input=400, output=40)),  # 10:42:00: the parent's g2 slot and model
    )
    rec = parse_one(parent)
    assert rec.slots == {
        ("2026-09-30T10:00:00Z", PRO): Usage(input=1000, output=100),
        ("2026-09-30T10:15:00Z", FLASH): Usage(input=300, output=30),
        ("2026-09-30T10:30:00Z", PRO): Usage(input=2400, output=240),
    }
    slots_add_up(rec)


def fuzz_session(tmp_path: Path, rng: random.Random) -> tuple[Path, dict[tuple[str, str], Usage]]:
    """A random session in files, and the slots an independent reading of what was written gives."""
    start = datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc) + timedelta(seconds=rng.randrange(0, 4 * 3600))
    first = start.isoformat().replace("+00:00", "Z")
    undated = slot_of(start)
    expected: dict[tuple[str, str], Usage] = {}
    files: list[list[Any]] = [[], []]  # the main file and one sub-agent file
    for n in range(rng.randrange(1, 40)):
        when = start + timedelta(milliseconds=rng.randrange(0, 3 * 86_400_000))
        offset = timezone(timedelta(minutes=rng.choice([0, 0, -180, 330, 840, -720])))
        stamp: Any = rng.choice(
            [when.astimezone(offset).isoformat(), when.isoformat().replace("+00:00", "Z"), when.isoformat(), None, "soon", 7]
        )
        model = rng.choice([PRO, FLASH, "gemini-3-pro-preview"])
        tok = tokens(input=rng.randrange(1, 10**6), output=rng.randrange(0, 5000), cached=rng.randrange(0, 50),
                     thoughts=rng.randrange(0, 500), tool=rng.randrange(0, 5000))
        mid = f"g{n}"
        record = reply(mid, 0, None, model=model)
        if stamp is None:
            del record["timestamp"]
        else:
            record["timestamp"] = stamp
        versions = [record, *({**record, "tokens": tok} for _ in range(rng.randrange(1, 3)))]  # tokens arrive later
        used = Usage(input=max(tok["input"] - tok["cached"], 0) + tok["tool"], cache_read=tok["cached"],
                     output=tok["output"] + tok["thoughts"])
        valid = isinstance(stamp, str) and stamp != "soon"
        slot = slot_of(when) if valid else undated
        expected[(slot, model)] = expected.get((slot, model), Usage()) + used
        files[rng.randrange(2)] += versions
    main, sub = files
    path = dated_session(tmp_path, first, *main)
    if sub:
        write_lines(chats_dir(tmp_path) / SID / "agent-1.jsonl", [{**header("agent-1", kind="subagent"),
                                                                    "startTime": first, "lastUpdated": first}, *sub])
    return path, dict(sorted(expected.items()))


@pytest.mark.parametrize("seed", range(40))
def test_fuzz_slots_add_up_to_usage_and_follow_each_responses_timestamp(tmp_path: Path, seed: int) -> None:
    path, expected = fuzz_session(tmp_path, random.Random(seed))
    rec = parse_one(path)
    assert rec.slots == expected
    slots_add_up(rec)


# ---- project and session id --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("root_file", "expected"),
    [
        (b"/home/dev/projects/my-repo", "my-repo"),
        (b"/home/dev/projects/my-repo/\n", "my-repo"),
        (b"C:\\Users\\dev\\my-repo", "my-repo"),
        (b"  /srv/app  \r\n", "app"),
        (b"", "slug-1"),
        (b"\xff\xfe\x00", "slug-1"),  # not text
    ],
)
def test_project_is_the_basename_of_project_root(tmp_path: Path, root_file: bytes, expected: str) -> None:
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1)), slug="slug-1")
    (path.parent.parent / ".project_root").write_bytes(root_file)
    project = parse_one(path).project
    assert project == expected and "/" not in project and "\\" not in project


def test_project_falls_back_to_the_slug_directory(tmp_path: Path) -> None:
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1)), slug="my-slug")
    assert parse_one(path).project == "my-slug"  # no .project_root at all
    (path.parent.parent / ".project_root").mkdir()  # unreadable as a file
    assert parse_one(path).project == "my-slug"


def test_session_id_falls_back_to_the_file_name_without_a_header(tmp_path: Path) -> None:
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1)), with_header=False)
    rec = parse_one(path)
    assert rec.session_id == path.stem
    assert rec.usage == {PRO: Usage(input=1)}


# ---- legacy JSON -------------------------------------------------------------------------------
def legacy_conversation() -> dict[str, Any]:
    return {
        "sessionId": SID, "projectHash": "0" * 64, "startTime": at(0), "lastUpdated": at(90),
        "messages": [
            user("u1", 5, f"{MARKER} a plain string prompt"),
            reply("g1", 20, tokens(input=1000, output=100, cached=400, thoughts=50),
                  calls=[call("replace", path="/p/a.py", when=30), call("read_file", "error", when=31)]),
            {"id": "g2-nothing", "timestamp": at(40), "type": "gemini", "content": "no token data"},
            reply("g3", 60, tokens(input=200, output=20), model=FLASH),
        ],
    }


@pytest.mark.parametrize("indent", [2, None], ids=["pretty-printed", "compact"])
def test_legacy_single_object_json_is_parsed(tmp_path: Path, indent: int | None) -> None:
    path = chats_dir(tmp_path) / f"session-2026-09-30T10-00-{SID[:8]}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy_conversation(), indent=indent), encoding="utf-8")
    assert list(PARSER.discover(tmp_path)) == [path]
    rec = parse_one(path)
    assert rec.session_id == SID
    assert rec.usage == {PRO: Usage(input=600, cache_read=400, output=150), FLASH: Usage(input=200, output=20)}
    assert (rec.turns, rec.prompts, rec.tool_calls, rec.files_touched, rec.error_retries) == (2, 1, 2, 1, 1)
    assert (rec.started_at, rec.ended_at, rec.wall_seconds) == (sec(0), sec(90), 90)
    assert MARKER not in repr(rec)


def test_a_truncated_legacy_json_yields_nothing(tmp_path: Path) -> None:
    path = chats_dir(tmp_path) / "session-2026-09-30T10-00-abcd1234.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy_conversation(), indent=2)[:400], encoding="utf-8")
    assert PARSER.parse(path) == []


def test_a_migrated_session_is_read_from_the_jsonl_not_twice(tmp_path: Path) -> None:
    chats = chats_dir(tmp_path)
    chats.mkdir(parents=True)
    stem = f"session-2026-09-30T10-00-{SID[:8]}"
    (chats / f"{stem}.json").write_text(json.dumps(legacy_conversation()), encoding="utf-8")
    conv = legacy_conversation()
    write_lines(chats / f"{stem}.jsonl", [{k: v for k, v in conv.items() if k != "messages"}, *conv["messages"]])
    (found,) = PARSER.discover(tmp_path)
    assert found.suffix == ".jsonl"
    assert parse_one(found).usage == {PRO: Usage(input=600, cache_read=400, output=150), FLASH: Usage(input=200, output=20)}


# ---- sub-agents --------------------------------------------------------------------------------
def make_subagent(tmp_path: Path, name: str = "agent-1", *records: Any, session_id: str = SID) -> Path:
    directory = chats_dir(tmp_path) / session_id
    return write_lines(directory / f"{name}.jsonl", [header(name, kind="subagent"), *records])


def parent_with_subagent(tmp_path: Path) -> Path:
    parent = make_session(
        tmp_path,
        user("u1", 1),
        reply("g1", 5, tokens(input=1000, output=100), calls=[call("write_file", path="/p/a.py", when=6)]),
    )
    make_subagent(
        tmp_path, "agent-1",
        user("task", 10, f"{MARKER} the task the parent agent gave"),
        reply("s1", 20, tokens(input=2000, output=300, cached=500, thoughts=100), model=FLASH, calls=[
            call("write_file", path="/p/a.py"),  # a file the parent wrote too
            call("write_file", path="/p/b.py"),
            call("read_file", "error"),
        ]),
        reply("s2", 30, tokens(input=100, output=10), model=FLASH),
    )
    return parent


def test_subagent_files_are_merged_into_the_parent_session(tmp_path: Path) -> None:
    parent = parent_with_subagent(tmp_path)
    assert list(PARSER.discover(tmp_path)) == [parent]
    rec = parse_one(parent)
    assert rec.session_id == SID
    assert rec.usage == {PRO: Usage(input=1000, output=100), FLASH: Usage(input=1600, cache_read=500, output=410)}
    assert (rec.turns, rec.tool_calls, rec.files_touched, rec.error_retries) == (3, 4, 2, 1)
    assert rec.prompts == 1  # the sub-agent's "user" message is a task, not a person typing
    assert rec.wall_seconds == 30  # sub-agent activity is part of the timeline
    assert MARKER not in repr(rec)


def test_subagents_of_other_sessions_are_not_merged(tmp_path: Path) -> None:
    parent = parent_with_subagent(tmp_path)
    make_subagent(tmp_path, "agent-9", user("t", 1), reply("x", 2, tokens(input=7_000_000)), session_id="somebody-else")
    assert parse_one(parent).total_usage == Usage(input=2600, cache_read=500, output=510)


def test_legacy_subagent_json_is_merged_too(tmp_path: Path) -> None:
    parent = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=100)))
    directory = chats_dir(tmp_path) / SID
    directory.mkdir()
    conv = {**header("agent-1", kind="subagent"), "messages": [reply("s1", 8, tokens(input=50), model=FLASH)]}
    (directory / "agent-1.json").write_text(json.dumps(conv, indent=2), encoding="utf-8")
    assert parse_one(parent).usage == {PRO: Usage(input=100), FLASH: Usage(input=50)}


def test_signature_covers_the_subagent_files(tmp_path: Path) -> None:
    parent = parent_with_subagent(tmp_path)
    before = PARSER.signature(parent)
    assert PARSER.signature(parent) == before  # stable while nothing changes

    sub = chats_dir(tmp_path) / SID / "agent-1.jsonl"
    with sub.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(reply("s3", 40, tokens(input=1), model=FLASH)) + "\n")
    after_edit = PARSER.signature(parent)
    assert after_edit != before

    make_subagent(tmp_path, "agent-2", user("t", 50))
    after_new_file = PARSER.signature(parent)
    assert after_new_file != after_edit

    make_subagent(tmp_path, "agent-3", user("t", 60), session_id="somebody-else")  # someone else's sub-agent
    assert PARSER.signature(parent) == after_new_file


def test_signature_changes_when_the_session_file_changes(tmp_path: Path) -> None:
    path = make_session(tmp_path, user("u1", 1), reply("g1", 5, tokens(input=1)))
    before = PARSER.signature(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(reply("g2", 9, tokens(input=1))) + "\n")
    assert PARSER.signature(path) != before


def test_signature_of_a_header_only_and_of_a_legacy_file(tmp_path: Path) -> None:
    only_header = write_lines(chats_dir(tmp_path) / "session-2026-09-30T10-00-aaaaaaaa.jsonl", [header()])
    legacy = chats_dir(tmp_path) / "session-2026-09-30T10-00-bbbbbbbb.json"
    legacy.write_text(json.dumps(legacy_conversation(), indent=2), encoding="utf-8")
    for path in (only_header, legacy):
        assert PARSER.signature(path) == PARSER.signature(path)


# ---- robustness --------------------------------------------------------------------------------
def test_files_without_usage_produce_no_record(tmp_path: Path) -> None:
    # the shapes of the three real files found on a machine: a header, then the startup $set with a
    # <session_context> message, and no conversation
    context = user("ctx", 1, [{"text": "<session_context>x</session_context>"}])
    startup = {"$set": {"messages": [context], "lastUpdated": at(1)}}
    empty = chats_dir(tmp_path) / "session-2026-09-30T10-00-00000000.jsonl"
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")
    blank = write_lines(chats_dir(tmp_path) / "session-2026-09-30T10-00-00000001.jsonl", ["", "   ", "\r"])
    only_header = write_lines(chats_dir(tmp_path) / "session-2026-09-30T10-00-00000002.jsonl", [header()])
    with_startup = write_lines(chats_dir(tmp_path) / "session-2026-09-30T10-00-00000003.jsonl", [header(), startup])
    prompt_only = make_session(tmp_path, user("u1", 1), session_id="00000004-x")
    for path in (empty, blank, only_header, with_startup, prompt_only):
        assert PARSER.parse(path) == []
        PARSER.signature(path)


def test_garbage_in_a_session_file_is_survived(tmp_path: Path) -> None:
    chats = chats_dir(tmp_path)
    chats.mkdir(parents=True)
    path = chats / "session-2026-09-30T10-00-abcd1234.jsonl"
    good = json.dumps(reply("good", 9, tokens(input=100, output=10), calls=[call("write_file", path="/p/a.py")]))
    lines: list[bytes] = [
        json.dumps(header()).encode(),
        b"not json at all",
        b'{"id":"cut","type":"gemini","content":"' + MARKER.encode() + b" truncated",
        b"[1, 2, 3]",
        b'"a string"',
        b"null",
        b"42",
        b"[" * 100_000,
        b'{"id": 5, "type": "gemini", "tokens": {"input": 999999}}',
        b'{"id": "m1", "type": "gemini", "timestamp": 12, "tokens": {"input": "x"}, "toolCalls": "nope", "model": 3}',
        b'{"id": "m2", "type": ["gemini"], "tokens": {"input": 999999}}',
        b'{"id": "m3", "type": "gemini", "tokens": [], "toolCalls": [1, null, {"status": [], "name": {}},'
        b' {"status": "error", "name": "write_file", "args": [1]}]}',
        b'{"$set": null}',
        b'{"$rewindTo": 5}',
        b"\xff\xfe\x00garbage bytes\x80",
        good.encode(),
        b'{"id": "tail", "type": "gemini", "tokens": {"input": 5, "output": 1}',  # cut off mid-write
    ]
    path.write_bytes(b"\r\n".join(lines))
    rec = parse_one(path)
    assert rec.usage == {PRO: Usage(input=100, output=10)}
    assert (rec.turns, rec.tool_calls, rec.error_retries, rec.files_touched) == (1, 3, 1, 1)
    assert MARKER not in repr(rec)


def test_unknown_record_types_and_extra_fields_are_ignored(tmp_path: Path) -> None:
    extended = {**reply("g1", 5, tokens(input=100, output=10)), "futureField": {"nested": [1, 2]}, "displayContent": MARKER}
    path = make_session(
        tmp_path,
        {"$telemetry": {"event": MARKER}},
        {"type": "checkpoint", "payload": MARKER},
        {"id": "h1", "timestamp": at(3), "type": "hologram", "content": MARKER},
        user("u1", 1),
        extended,
    )
    rec = parse_one(path)
    assert rec.usage == {PRO: Usage(input=100, output=10)} and (rec.turns, rec.prompts) == (1, 1)
    assert MARKER not in repr(rec)

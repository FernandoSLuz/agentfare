import os
import sqlite3
import threading
import unicodedata
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentfare.config import Config, fmt_iso
from agentfare.db import SCHEMA_VERSION, Store, StoredSession, StoreError
from agentfare.ingest import ingest
from agentfare.models import SessionRecord, Usage, slot_of
from agentfare.parsers import get_parser

Make = Callable[..., SessionRecord]


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as s:
        yield s


def usage_rows(store: Store) -> list[tuple]:
    return [
        tuple(r)
        for r in store.conn.execute("SELECT key, model, input, cache_read, cache_write, output, cache_write_1h FROM session_usage ORDER BY 1, 2")
    ]


def slot_rows(store: Store) -> list[tuple]:
    return [
        tuple(r)
        for r in store.conn.execute("SELECT key, model, slot, input, output FROM session_usage ORDER BY key, slot, model")
    ]


class TestStore:
    def test_memory_database_starts_empty_with_the_schema(self, store: Store) -> None:
        assert store.count() == 0 and store.sessions() == []
        tables = {r["name"] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"sessions", "session_usage", "ingested_files"} <= tables
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_file_database_creates_parent_directories_and_persists(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "deeper" / "still" / "fare.db"
        with Store(db) as s:
            s.upsert_session(make_record("a"))
            s.mark_file("/logs/a.jsonl", "claude", "sig", 1)
        assert db.is_file()
        with Store(str(db)) as again:  # str paths work too
            assert again.count() == 1
            assert again.file_signature("/logs/a.jsonl") == "sig"

    def test_reopening_keeps_the_schema_and_data(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "fare.db"
        Store(db).close()
        with Store(db) as s:
            s.upsert_session(make_record("a"))
        with Store(db) as s:
            assert s.count() == 1

    def test_context_manager_closes_the_connection(self, make_record: Make) -> None:
        with Store(":memory:") as s:
            pass
        with pytest.raises(sqlite3.ProgrammingError):
            s.count()


class TestOneHourCacheWrites:
    def test_the_schema_has_the_1h_column_with_a_zero_default(self, store: Store) -> None:
        assert SCHEMA_VERSION == 3
        columns = {r["name"]: r for r in store.conn.execute("PRAGMA table_info(session_usage)")}
        assert columns["cache_write_1h"]["notnull"] == 1 and columns["cache_write_1h"]["dflt_value"] == "0"
        assert columns["cache_write"]["name"] == "cache_write"  # 5-minute writes keep their column

    def test_both_kinds_of_write_survive_a_round_trip_apart(self, store: Store, make_record: Make) -> None:
        usage = {"m": Usage(input=1, cache_read=2, cache_write=3, output=4, cache_write_1h=5)}
        store.upsert_session(make_record("a", usage=usage))
        assert usage_rows(store) == [("claude:a", "m", 1, 2, 3, 4, 5)]
        assert store.sessions()[0].usage == usage
        assert store.sessions()[0].total_usage.total_input == 1 + 2 + 3 + 5


OLD_SESSIONS_AND_FILES = """
CREATE TABLE sessions (
    key TEXT PRIMARY KEY, agent TEXT NOT NULL, session_id TEXT NOT NULL, project TEXT NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT NOT NULL, wall_seconds INTEGER NOT NULL, active_seconds INTEGER NOT NULL,
    tool_calls INTEGER NOT NULL, files_touched INTEGER NOT NULL, error_retries INTEGER NOT NULL, turns INTEGER NOT NULL,
    prompts INTEGER NOT NULL, primary_model TEXT NOT NULL, source_path TEXT NOT NULL, ingested_at TEXT NOT NULL
);
CREATE TABLE ingested_files (path TEXT PRIMARY KEY, agent TEXT NOT NULL, signature TEXT NOT NULL, sessions INTEGER NOT NULL);
"""
V1_SCHEMA = OLD_SESSIONS_AND_FILES + """
CREATE TABLE session_usage (
    key TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE, model TEXT NOT NULL, input INTEGER NOT NULL,
    cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, output INTEGER NOT NULL, PRIMARY KEY (key, model)
);
PRAGMA user_version = 1;
"""


def build_v1_database(path: Path, *, files: dict[str, str] | None = None) -> None:
    """A database as agentfare 0.1 wrote it: no cache_write_1h column, user_version 1, some ingested files."""
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO sessions VALUES ('claude:old', 'claude', 'old', 'proj', '2026-09-01T10:00:00Z', '2026-09-01T11:00:00Z',"
        " 3600, 1200, 5, 2, 1, 7, 3, 'claude-opus-5-5', '/logs/old.jsonl', '2026-09-01T12:00:00Z')"
    )
    conn.execute("INSERT INTO session_usage VALUES ('claude:old', 'claude-opus-5-5', 100, 200, 300, 40)")
    conn.executemany(
        "INSERT INTO ingested_files VALUES (?, 'claude', ?, 1)", [(name, sig) for name, sig in (files or {"/logs/old.jsonl": "sig-1"}).items()]
    )
    conn.commit()
    conn.close()


V2_SCHEMA = OLD_SESSIONS_AND_FILES + """
CREATE TABLE session_usage (
    key TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE, model TEXT NOT NULL, input INTEGER NOT NULL,
    cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, output INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (key, model)
);
PRAGMA user_version = 2;
"""
V2_SESSIONS = (  # (session id, started_at): start minutes that fall in every 15-minute slot, and a day boundary
    ("a", "2026-09-01T10:00:00Z"), ("b", "2026-09-01T10:14:59Z"), ("c", "2026-09-01T10:15:00Z"),
    ("d", "2026-09-01T10:37:12Z"), ("e", "2026-09-01T10:59:59Z"), ("f", "2026-09-01T23:59:59Z"), ("g", "2026-09-02T00:00:00Z"),
)


def build_v2_database(path: Path, *, files: dict[str, str] | None = None) -> None:
    """A database as agentfare 0.2 wrote it: the 1-hour column but no slot, user_version 2, one row per model."""
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    for session_id, started_at in V2_SESSIONS:
        conn.execute(
            "INSERT INTO sessions VALUES (?, 'claude', ?, 'proj', ?, ?, 600, 600, 1, 1, 0, 2, 1, 'm', '/logs/x.jsonl', '2026-09-30T00:00:00Z')",
            (f"claude:{session_id}", session_id, started_at, started_at),
        )
        conn.execute("INSERT INTO session_usage VALUES (?, 'm', 100, 200, 300, 40, 50)", (f"claude:{session_id}",))
    conn.executemany(
        "INSERT INTO ingested_files VALUES (?, 'claude', ?, 1)", [(name, sig) for name, sig in (files or {"/logs/x.jsonl": "sig-2"}).items()]
    )
    conn.commit()
    conn.close()


class TestMigrationFromVersion1:
    def test_an_old_database_gains_the_column_and_keeps_its_sessions(self, tmp_path: Path) -> None:
        db = tmp_path / "old.db"
        build_v1_database(db)
        with Store(db) as s:
            assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
            assert usage_rows(s) == [("claude:old", "claude-opus-5-5", 100, 200, 300, 40, 0)]  # every old write was read as 5-minute
            [old] = s.sessions()
            assert old.usage == {"claude-opus-5-5": Usage(100, 200, 300, 40)}
            assert (old.project, old.tool_calls, old.turns) == ("proj", 5, 7)

    def test_every_ingested_file_is_forgotten_so_the_next_run_parses_it_again(self, tmp_path: Path) -> None:
        db = tmp_path / "old.db"
        build_v1_database(db, files={"/logs/a.jsonl": "1", "/logs/b.jsonl": "2"})
        with Store(db) as s:
            assert s.conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 0
            assert s.file_signature("/logs/a.jsonl") is None

    def test_the_migration_runs_once(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "old.db"
        build_v1_database(db)
        with Store(db) as s:
            s.upsert_session(make_record("new", usage={"m": Usage(input=1, cache_write_1h=9)}))
            s.mark_file("/logs/new.jsonl", "claude", "sig", 1)
        with Store(db) as again:
            assert again.file_signature("/logs/new.jsonl") == "sig"  # not cleared a second time
            assert usage_rows(again)[0] == ("claude:new", "m", 1, 0, 0, 0, 9)
            assert again.count() == 2

    def test_a_new_column_lets_new_sessions_be_written_into_the_migrated_table(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "old.db"
        build_v1_database(db)
        with Store(db) as s:
            s.upsert_session(make_record("old", agent="claude", usage={"m": Usage(1, 2, 3, 4, cache_write_1h=5)}))  # replaces the old row
            assert usage_rows(s) == [("claude:old", "m", 1, 2, 3, 4, 5)]

    @pytest.mark.parametrize("build", [build_v1_database, build_v2_database], ids=["v1", "v2"])
    def test_connections_opening_an_old_database_at_the_same_time_all_succeed(
        self, tmp_path: Path, build: Callable[[Path], None]
    ) -> None:
        """Commands started together (a status bar and a terminal) all find the old file: one upgrades it, the rest wait."""
        for round_number in range(25):
            db = tmp_path / f"old{round_number}.db"
            build(db)
            start = threading.Barrier(6)
            failures: list[str] = []

            def open_it() -> None:
                start.wait()
                try:
                    Store(db).close()
                except StoreError as exc:
                    failures.append(str(exc))

            threads = [threading.Thread(target=open_it) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert failures == []

    def test_a_database_that_is_already_current_is_left_alone(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "fare.db"
        with Store(db) as s:
            s.upsert_session(make_record("a"))
            s.mark_file("/a", "claude", "1", 1)
        with Store(db) as again:
            assert again.file_signature("/a") == "1" and again.count() == 1

    def test_the_next_ingest_reparses_the_logs_and_splits_the_writes(self, tmp_path: Path) -> None:
        """End to end: a v1 database that already 'knows' the fixture log gets its cache writes split by TTL."""
        fixtures = Path(__file__).parent / "fixtures"
        cfg = Config.load(fixtures / "config.yaml", home=tmp_path)
        (log,) = get_parser("claude").discover(fixtures / "claude" / "projects")
        db = tmp_path / "old.db"
        build_v1_database(db, files={str(log): get_parser("claude").signature(log)})
        with Store(db) as s:
            report = ingest(cfg, s, ["claude"])
            assert (report.files_parsed, report.files_unchanged) == (1, 0)  # not skipped as unchanged
            assert report.errors == []
            by_id = {st.session_id: st for st in s.sessions(agent="claude")}
            opus = by_id["2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402"].usage["claude-opus-5-5"]
            assert (opus.cache_write, opus.cache_write_1h) == (93_000, 62_000)


class TestStoreErrors:
    def test_store_error_is_a_value_error_like_the_other_setup_errors(self) -> None:
        assert issubclass(StoreError, ValueError)

    def test_a_parent_that_is_a_file(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        with pytest.raises(StoreError, match=r"cannot use the database .*blocker.*fare\.db"):
            Store(blocker / "fare.db")

    def test_a_path_that_is_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "fare.db").mkdir()
        with pytest.raises(StoreError, match="cannot use the database"):
            Store(tmp_path / "fare.db")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_an_unwritable_directory(self, tmp_path: Path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        try:
            with pytest.raises(StoreError, match="cannot use the database") as caught:
                Store(locked / "fare.db")
            assert "delete the file" not in str(caught.value)  # a permission problem is not a corrupt file
        finally:
            locked.chmod(0o700)

    @pytest.mark.parametrize("junk", [b"this is not a database at all, just some text\n" * 20, b"\x00\xff\xfe" * 500])
    def test_a_corrupt_file_says_it_can_be_deleted(self, tmp_path: Path, junk: bytes) -> None:
        db = tmp_path / "fare.db"
        db.write_bytes(junk)
        with pytest.raises(StoreError, match="cannot use the database .*fare\\.db.*delete the file") as caught:
            Store(db)
        assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
        assert db.read_bytes() == junk  # never overwritten

    def test_a_truncated_database_is_a_corrupt_file_too(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "fare.db"
        with Store(db) as s:
            for i in range(50):
                s.upsert_session(make_record(f"s{i}"))
        data = db.read_bytes()
        db.write_bytes(data[: len(data) // 2])
        with pytest.raises(StoreError, match="malformed.*delete the file"):
            Store(db)


class TestOpenReadOnly:
    """``Store.open_read_only``: how ``agentfare run`` reads the history without ever writing it."""

    def test_it_reads_what_a_writer_stored(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "fare.db"
        with Store(db) as s:
            s.upsert_session(make_record("a"))
            s.upsert_session(make_record("b"))
            written = [x.key for x in s.sessions()]
        with Store.open_read_only(db) as ro:
            assert ro.count() == 2
            assert [x.key for x in ro.sessions()] == written
            assert [x.key for x in ro.sessions(project="PROJ")] == written  # casefold is registered

    def test_it_changes_nothing_on_disk(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "fare.db"
        with Store(db) as s:
            s.upsert_session(make_record("a"))
        before = (db.read_bytes(), db.stat().st_mtime_ns)
        with Store.open_read_only(db) as ro:
            ro.sessions()
            ro.whole_usage(["claude:a"])
        assert (db.read_bytes(), db.stat().st_mtime_ns) == before  # Store(path) stamps user_version on every open; this does not
        assert sorted(p.name for p in tmp_path.iterdir()) == ["fare.db"]  # no journal left behind

    def test_it_cannot_write(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "fare.db"
        Store(db).close()
        with Store.open_read_only(db) as ro, pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.upsert_session(make_record("a"))

    def test_a_writer_that_holds_the_lock_does_not_block_it(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "fare.db"
        with Store(db) as s:
            s.upsert_session(make_record("a"))
        writer = sqlite3.connect(db, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            with Store.open_read_only(db) as ro:
                assert ro.count() == 1
            assert time.monotonic() - started < 1
        finally:
            writer.execute("ROLLBACK")
            writer.close()

    @pytest.mark.parametrize(("build", "version"), [(build_v1_database, 1), (build_v2_database, 2)])
    def test_an_old_database_is_refused_and_left_as_it_is(self, tmp_path: Path, build: Callable[[Path], None], version: int) -> None:
        db = tmp_path / "old.db"
        build(db)
        before = db.read_bytes()
        with pytest.raises(StoreError, match=r"in an older format \(run agentfare ingest to upgrade it\)"):
            Store.open_read_only(db)
        assert db.read_bytes() == before  # not upgraded: only a command that writes does that
        assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == version

    def test_a_database_from_a_newer_version_is_refused(self, tmp_path: Path) -> None:
        db = tmp_path / "new.db"
        Store(db).close()
        conn = sqlite3.connect(db)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        with pytest.raises(StoreError, match="written by a newer agentfare"):
            Store.open_read_only(db)

    @pytest.mark.parametrize("junk", [b"this is not a database at all, just some text\n" * 20, b"\x00\xff\xfe" * 500])
    def test_a_file_that_is_not_a_database_is_a_store_error(self, tmp_path: Path, junk: bytes) -> None:
        db = tmp_path / "fare.db"
        db.write_bytes(junk)
        with pytest.raises(StoreError, match="cannot use the database .*fare\\.db"):
            Store.open_read_only(db)
        assert db.read_bytes() == junk

    def test_a_missing_file_is_not_created(self, tmp_path: Path) -> None:
        with pytest.raises(StoreError, match="cannot use the database"):
            Store.open_read_only(tmp_path / "missing" / "fare.db")
        assert list(tmp_path.iterdir()) == []


def damage_data_pages(db: Path) -> None:
    """Keep the header and the schema page, so the file still opens, and overwrite every page after them with garbage."""
    data = bytearray(db.read_bytes())
    data[4096:] = b"\xab" * (len(data) - 4096)
    db.write_bytes(bytes(data))


@pytest.fixture
def damaged(tmp_path: Path, make_record: Make) -> Store:
    """A store on a database that opens but cannot be read or written: what a half-written or bit-rotted file looks like."""
    db = tmp_path / "fare.db"
    with Store(db) as s:
        for i in range(20):
            s.upsert_session(make_record(f"s{i}", usage={"m": Usage(input=1)}))
            s.mark_file(f"/logs/{i}", "claude", "sig", 1)
    damage_data_pages(db)
    with Store(db) as opened:
        yield opened


class TestDamagedDatabase:
    """Every method turns sqlite's 'not a database' and 'malformed' into a StoreError that says what to do about it."""

    CALLS = {
        "sessions": lambda s, rec: s.sessions(),
        "sessions with filters": lambda s, rec: s.sessions(agent="claude", project="p", since="2026-09-01T00:00:00Z", until="2026-10-01T00:00:00Z"),
        "iter_sessions": lambda s, rec: list(s.iter_sessions()),
        "whole_usage": lambda s, rec: s.whole_usage(["claude:a"]),
        "count": lambda s, rec: s.count(),
        "file_signature": lambda s, rec: s.file_signature("/logs/1"),
        "mark_file": lambda s, rec: s.mark_file("/logs/new", "claude", "sig", 1),
        "upsert_session": lambda s, rec: s.upsert_session(rec),
        "clear": lambda s, rec: s.clear(),
    }

    @pytest.mark.parametrize("call", CALLS)
    def test_a_method_that_hits_the_damage_raises_a_store_error(self, damaged: Store, make_record: Make, call: str) -> None:
        with pytest.raises(StoreError, match=r"cannot use the database .*fare\.db: .*\(delete the file to start over") as caught:
            self.CALLS[call](damaged, make_record("new"))
        assert type(caught.value.__cause__) is sqlite3.DatabaseError  # the original stays for whoever wants it

    def test_the_hint_says_the_logs_rebuild_the_database(self, damaged: Store) -> None:
        with pytest.raises(StoreError, match="rebuilt from the agent logs that still exist"):
            damaged.count()

    def test_a_store_error_leaves_the_file_alone(self, tmp_path: Path, damaged: Store) -> None:
        before = (tmp_path / "fare.db").read_bytes()
        with pytest.raises(StoreError):
            damaged.sessions()
        assert (tmp_path / "fare.db").read_bytes() == before

    def test_opening_and_upgrading_a_damaged_old_database_is_a_store_error_too(self, tmp_path: Path) -> None:
        db = tmp_path / "old.db"
        build_v1_database(db, files={f"/logs/{i}": "sig" for i in range(300)})  # upgrading has to read what was ingested
        damage_data_pages(db)
        with pytest.raises(StoreError, match="delete the file to start over") as caught:
            Store(db)
        assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)


class TestRealSqlProblemsAreNotStoreErrors:
    """A wrong statement or a misused connection is a bug in agentfare, not a damaged file: it must stay visible as itself."""

    def test_a_missing_table_stays_an_operational_error(self, store: Store) -> None:
        store.conn.execute("DROP TABLE session_usage")
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            store.sessions()

    def test_a_closed_connection_stays_a_programming_error(self) -> None:
        with Store(":memory:") as s:
            pass
        for call in (s.count, s.sessions, lambda: s.file_signature("/x")):
            with pytest.raises(sqlite3.ProgrammingError):
                call()

    def test_a_constraint_violation_stays_an_integrity_error(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("a"))
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("INSERT INTO session_usage (key, model, input, cache_read, cache_write, output) VALUES ('nope', 'm', 0, 0, 0, 0)")


class TestUpsert:
    def test_a_session_round_trips_every_field(self, store: Store, make_record: Make, frozen_now: Callable[..., object]) -> None:
        frozen_now("2026-09-30T12:00:00Z")
        rec = make_record(
            "abc",
            agent="codex",
            project="MyRepo",
            started_at="2026-09-30T10:00:00Z",
            wall_seconds=3600,
            active_seconds=1200,
            usage={
                "gpt-6.1-sol": Usage(input=100, cache_read=200, cache_write=0, output=50),
                "gpt-6-astra": Usage(input=1, cache_read=2, cache_write=3, output=400, cache_write_1h=5),
            },
            tool_calls=12,
            files_touched=3,
            error_retries=2,
            turns=9,
            prompts=4,
            source_path="/logs/abc.jsonl",
        )
        store.upsert_session(rec)
        [got] = store.sessions()
        assert isinstance(got, StoredSession)
        assert (got.key, got.agent, got.session_id, got.project) == ("codex:abc", "codex", "abc", "MyRepo")
        assert (got.started_at, got.ended_at) == ("2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z")
        assert (got.wall_seconds, got.active_seconds) == (3600, 1200)
        assert (got.tool_calls, got.files_touched, got.error_retries, got.turns, got.prompts) == (12, 3, 2, 9, 4)
        assert got.primary_model == "gpt-6-astra" == rec.primary_model
        assert got.source_path == "/logs/abc.jsonl"
        assert got.usage == rec.usage
        assert got.total_usage == Usage(input=101, cache_read=202, cache_write=3, output=450, cache_write_1h=5)
        row = store.conn.execute("SELECT ingested_at FROM sessions").fetchone()
        assert row["ingested_at"] == "2026-09-30T12:00:00Z"  # AGENTFARE_NOW drives the timestamp

    def test_upsert_is_idempotent(self, store: Store, make_record: Make) -> None:
        rec = make_record("a", usage={"m1": Usage(input=1, output=2), "m2": Usage(cache_read=3)})
        for _ in range(3):
            store.upsert_session(rec)
        assert store.count() == 1
        assert usage_rows(store) == [("claude:a", "m1", 1, 0, 0, 2, 0), ("claude:a", "m2", 0, 3, 0, 0, 0)]
        assert store.sessions()[0].usage == rec.usage

    def test_upsert_replaces_the_usage_rows_and_the_session_fields(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("a", usage={"m1": Usage(input=1), "m2": Usage(input=2)}, tool_calls=5, project="old"))
        store.upsert_session(make_record("a", usage={"m3": Usage(output=9)}, tool_calls=7, project="new"))
        [got] = store.sessions()
        assert got.usage == {"m3": Usage(output=9)}
        assert (got.tool_calls, got.project, got.primary_model) == (7, "new", "m3")
        assert usage_rows(store) == [("claude:a", "m3", 0, 0, 0, 9, 0)]  # no orphans left behind

    def test_the_same_session_id_under_two_agents_is_two_sessions(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("same", agent="claude"))
        store.upsert_session(make_record("same", agent="codex"))
        assert sorted(s.key for s in store.sessions()) == ["claude:same", "codex:same"]

    def test_a_session_without_usage_is_stored_without_usage_rows(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("a", usage={}))
        [got] = store.sessions()
        assert got.usage == {} and got.primary_model == "" and got.total_usage.is_empty()

    def test_usage_rows_belong_to_their_session(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("a", usage={"m": Usage(input=1)}))
        store.upsert_session(make_record("b", usage={"m": Usage(input=2)}))
        by_key = {s.session_id: s.usage for s in store.sessions()}
        assert by_key == {"a": {"m": Usage(input=1)}, "b": {"m": Usage(input=2)}}

    def test_a_failed_upsert_rolls_back_completely(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("a", usage={"m": Usage(input=1)}))
        broken = make_record("a", usage={"m": Usage(input=2)})
        broken.tool_calls = None  # type: ignore[assignment]  # violates NOT NULL after the DELETE has run
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_session(broken)
        assert store.sessions()[0].usage == {"m": Usage(input=1)}  # the old version survived


class TestFileSignatures:
    def test_unknown_file(self, store: Store) -> None:
        assert store.file_signature("/nope") is None

    def test_mark_and_replace(self, store: Store) -> None:
        store.mark_file("/logs/a.jsonl", "claude", "100:1", 2)
        assert store.file_signature("/logs/a.jsonl") == "100:1"
        store.mark_file("/logs/a.jsonl", "claude", "200:2", 3)
        assert store.file_signature("/logs/a.jsonl") == "200:2"
        assert store.conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1
        row = store.conn.execute("SELECT agent, sessions FROM ingested_files").fetchone()
        assert (row["agent"], row["sessions"]) == ("claude", 3)

    def test_files_are_tracked_independently(self, store: Store) -> None:
        store.mark_file("/a", "claude", "1", 1)
        store.mark_file("/b", "codex", "2", 0)
        assert (store.file_signature("/a"), store.file_signature("/b")) == ("1", "2")

    def test_clear_forgets_everything(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("a"))
        store.mark_file("/a", "claude", "1", 1)
        store.clear()
        assert store.count() == 0 and store.file_signature("/a") is None and usage_rows(store) == []


class TestFilters:
    @pytest.fixture
    def filled(self, store: Store, make_record: Make) -> Store:
        for rec in (
            make_record("c1", agent="claude", project="Alpha", started_at="2026-09-28T00:00:00Z"),
            make_record("c2", agent="claude", project="beta", started_at="2026-09-29T12:00:00Z"),
            make_record("x1", agent="codex", project="alpha", started_at="2026-09-29T12:00:00Z"),
            make_record("g1", agent="gemini", project="Ação", started_at="2026-09-30T23:59:59Z"),
            make_record("c3", agent="claude", project="Alpha", started_at="2026-10-01T00:00:00Z"),
        ):
            store.upsert_session(rec)
        return store

    def ids(self, sessions: list[StoredSession]) -> list[str]:
        return [s.session_id for s in sessions]

    def test_no_filter_returns_everything_ordered_by_start_then_key(self, filled: Store) -> None:
        assert self.ids(filled.sessions()) == ["c1", "c2", "x1", "g1", "c3"]  # c2 < x1: same start, key order

    def test_agent(self, filled: Store) -> None:
        assert self.ids(filled.sessions(agent="claude")) == ["c1", "c2", "c3"]
        assert self.ids(filled.sessions(agent="codex")) == ["x1"]
        assert filled.sessions(agent="nobody") == []

    def test_project_is_case_insensitive(self, filled: Store) -> None:
        assert self.ids(filled.sessions(project="alpha")) == ["c1", "x1", "c3"]
        assert self.ids(filled.sessions(project="ALPHA")) == ["c1", "x1", "c3"]
        assert self.ids(filled.sessions(project="Beta")) == ["c2"]

    def test_project_is_an_exact_name_not_a_pattern(self, filled: Store) -> None:
        assert filled.sessions(project="alph") == []
        assert filled.sessions(project="alp%") == []
        assert filled.sessions(project="_lpha") == []

    def test_project_case_folding_works_beyond_ascii(self, filled: Store) -> None:
        assert self.ids(filled.sessions(project="AÇÃO")) == ["g1"]
        assert self.ids(filled.sessions(project="ação")) == ["g1"]

    def test_since_is_inclusive(self, filled: Store) -> None:
        assert self.ids(filled.sessions(since="2026-09-29T12:00:00Z")) == ["c2", "x1", "g1", "c3"]
        assert self.ids(filled.sessions(since="2026-09-29T12:00:01Z")) == ["g1", "c3"]

    def test_until_is_exclusive(self, filled: Store) -> None:
        assert self.ids(filled.sessions(until="2026-09-29T12:00:00Z")) == ["c1"]
        assert self.ids(filled.sessions(until="2026-10-01T00:00:00Z")) == ["c1", "c2", "x1", "g1"]

    def test_since_and_until_bound_a_half_open_interval(self, filled: Store) -> None:
        got = filled.sessions(since="2026-09-29T00:00:00Z", until="2026-10-01T00:00:00Z")
        assert self.ids(got) == ["c2", "x1", "g1"]
        assert filled.sessions(since="2026-09-30T00:00:00Z", until="2026-09-30T00:00:00Z") == []

    def test_bounds_in_any_iso_spelling_denote_the_same_instant(self, filled: Store) -> None:
        for spelling in ("2026-09-29T12:00:00Z", "2026-09-29T12:00:00+00:00", "2026-09-29T09:00:00-03:00", "2026-09-29T12:00:00.750Z"):
            assert self.ids(filled.sessions(since=spelling)) == ["c2", "x1", "g1", "c3"], spelling
            assert self.ids(filled.sessions(until=spelling)) == ["c1"], spelling
        assert self.ids(filled.sessions(since="2026-09-30")) == ["g1", "c3"]  # a bare date is midnight UTC

    def test_a_garbage_bound_is_an_error_not_an_empty_result(self, filled: Store) -> None:
        with pytest.raises(ValueError):
            filled.sessions(since="last tuesday")

    def test_filters_combine_with_and(self, filled: Store) -> None:
        got = filled.sessions(agent="claude", project="alpha", since="2026-09-29T00:00:00Z")
        assert self.ids(got) == ["c3"]

    def test_filters_do_not_leak_usage_between_sessions(self, filled: Store, make_record: Make) -> None:
        filled.upsert_session(make_record("u1", agent="claude", project="usage", usage={"only-u1": Usage(input=5)}))
        [got] = filled.sessions(project="usage")
        assert got.usage == {"only-u1": Usage(input=5)}

    def test_iter_sessions_takes_the_same_filters(self, filled: Store) -> None:
        assert self.ids(list(filled.iter_sessions(agent="codex"))) == ["x1"]
        assert self.ids(list(filled.iter_sessions())) == self.ids(filled.sessions())

    def test_values_are_bound_not_interpolated(self, filled: Store) -> None:
        assert filled.sessions(project="x' OR '1'='1") == []
        assert filled.count() == 5


class TestCanonicalTimestamps:
    """Filters compare timestamps as text, so the store itself must keep them in one spelling."""

    SPELLINGS = ("2026-09-30T10:00:00Z", "2026-09-30T10:00:00.500Z", "2026-09-30T10:00:00+00:00", "2026-09-30T07:00:00-03:00", "2026-09-30T10:00:00")

    def test_every_spelling_of_an_instant_is_stored_in_the_canonical_one(self, store: Store, make_record: Make) -> None:
        for i, spelling in enumerate(self.SPELLINGS):
            rec = make_record(f"s{i}")
            rec.started_at, rec.ended_at = spelling, spelling
            store.upsert_session(rec)
        assert {(s.started_at, s.ended_at) for s in store.sessions()} == {("2026-09-30T10:00:00Z", "2026-09-30T10:00:00Z")}

    def test_filters_see_the_instant_and_not_its_spelling(self, store: Store, make_record: Make) -> None:
        for i, spelling in enumerate(self.SPELLINGS):
            rec = make_record(f"s{i}")
            rec.started_at = spelling
            store.upsert_session(rec)
        assert len(store.sessions(since="2026-09-30T10:00:00Z")) == len(self.SPELLINGS)  # inclusive
        assert store.sessions(until="2026-09-30T10:00:00Z") == []  # exclusive
        assert len(store.sessions(until="2026-09-30T10:00:01Z")) == len(self.SPELLINGS)

    def test_an_unreadable_timestamp_is_refused_and_the_stored_version_survives(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("a", usage={"m": Usage(input=1)}))
        for field in ("started_at", "ended_at"):
            broken = make_record("a", usage={"m": Usage(input=2)})
            setattr(broken, field, "sometime last week")
            with pytest.raises(ValueError):
                store.upsert_session(broken)
        assert store.sessions()[0].usage == {"m": Usage(input=1)}


class TestProjectMatching:
    def test_composed_and_decomposed_accents_are_the_same_project(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("mac", project=unicodedata.normalize("NFD", "Ação")))  # what macOS file systems return
        assert [s.session_id for s in store.sessions(project=unicodedata.normalize("NFC", "AÇÃO"))] == ["mac"]
        assert [s.session_id for s in store.sessions(project="ação")] == ["mac"]


class TestMigrationToSlots:
    """Schema 3 keeps money by the 15-minute slot it was spent in: an old file is rebuilt in place and its logs are re-read."""

    @pytest.fixture(params=[build_v1_database, build_v2_database], ids=["v1", "v2"])
    def old(self, request: pytest.FixtureRequest, tmp_path: Path) -> Path:
        db = tmp_path / "old.db"
        request.param(db, files={"/logs/x.jsonl": "sig", "/logs/y.jsonl": "sig"})
        return db

    def test_the_old_rows_count_at_the_slot_their_session_started_in(self, old: Path) -> None:
        with Store(old) as s:
            assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 3
            [first, *_] = s.sessions()  # "old" in a v1 file, "a" in a v2 one: both start at 10:00
            assert list(first.slots) == [("2026-09-01T10:00:00Z", first.primary_model)] and first.effective_slots() == first.slots

    def test_every_ingested_file_is_forgotten_so_the_next_run_re_reads_it(self, old: Path) -> None:
        with Store(old) as s:
            assert s.conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 0
            assert s.file_signature("/logs/x.jsonl") is None

    def test_the_slot_computed_in_sql_is_the_one_slot_of_computes(self, tmp_path: Path) -> None:
        db = tmp_path / "v2.db"
        build_v2_database(db)
        with Store(db) as s:
            got = {x.session_id: x for x in s.sessions()}
            assert len(got) == len(V2_SESSIONS)
            for session_id, started_at in V2_SESSIONS:
                [(slot, _)] = got[session_id].slots
                assert slot == slot_of(datetime.fromisoformat(started_at.replace("Z", "+00:00"))), started_at
        assert got["d"].started_at == "2026-09-01T10:37:12Z" and [s for s, _ in got["d"].slots] == ["2026-09-01T10:30:00Z"]

    def test_a_v2_database_keeps_its_tokens_including_1h_writes(self, tmp_path: Path) -> None:
        db = tmp_path / "v2.db"
        build_v2_database(db)
        with Store(db) as s:
            got = {x.session_id: x for x in s.sessions()}
            assert got["c"].usage == {"m": Usage(100, 200, 300, 40, 50)}
            assert got["c"].slots == {("2026-09-01T10:15:00Z", "m"): Usage(100, 200, 300, 40, 50)}
            assert s.count() == len(V2_SESSIONS)

    def test_a_v1_database_reads_its_writes_as_5_minute_ones(self, tmp_path: Path) -> None:
        db = tmp_path / "v1.db"
        build_v1_database(db)
        with Store(db) as s:
            [old] = s.sessions()
            assert old.slots == {("2026-09-01T10:00:00Z", "claude-opus-5-5"): Usage(100, 200, 300, 40)}

    def test_the_upgrade_runs_once(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "v2.db"
        build_v2_database(db)
        with Store(db) as s:
            s.mark_file("/logs/new.jsonl", "claude", "sig", 1)
        with Store(db) as again:
            assert again.file_signature("/logs/new.jsonl") == "sig"  # not cleared a second time
            assert again.count() == len(V2_SESSIONS)

    def test_the_rebuilt_table_has_the_slot_in_its_key_and_an_index(self, old: Path) -> None:
        with Store(old) as s:
            columns = {r["name"]: r["pk"] for r in s.conn.execute("PRAGMA table_info(session_usage)")}
            assert {name for name, pk in columns.items() if pk} == {"key", "model", "slot"}
            assert "idx_usage_slot" in {r["name"] for r in s.conn.execute("PRAGMA index_list(session_usage)")}
            assert not s.conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%usage_old%'").fetchall()

    def test_new_sessions_can_be_written_into_the_upgraded_table(self, old: Path, make_record: Make) -> None:
        with Store(old) as s:
            before = s.count()
            slots = {("2026-09-01T09:00:00Z", "m"): Usage(input=1), ("2026-09-01T09:15:00Z", "m"): Usage(input=2)}
            s.upsert_session(make_record("fresh", usage={"m": Usage(input=3)}, slots=slots))
            assert s.count() == before + 1
            [fresh] = [x for x in s.sessions() if x.session_id == "fresh"]
            assert fresh.slots == slots and fresh.usage == {"m": Usage(input=3)}

    def test_a_current_database_is_not_rebuilt(self, tmp_path: Path, make_record: Make) -> None:
        db = tmp_path / "fare.db"
        with Store(db) as s:
            s.upsert_session(make_record("a"))
            s.mark_file("/a", "claude", "1", 1)
        with Store(db) as again:
            assert again.file_signature("/a") == "1" and again.count() == 1


class TestSlots:
    T0 = "2026-09-30T23:40:00Z"

    def spanning(self, make_record: Make, **extra: object) -> SessionRecord:
        """Started at 23:40 UTC, spent in three slots across midnight, on two models."""
        slots = {
            ("2026-09-30T23:30:00Z", "m1"): Usage(input=10, output=1),
            ("2026-09-30T23:45:00Z", "m1"): Usage(input=20, output=2),
            ("2026-10-01T00:00:00Z", "m1"): Usage(input=40, output=4),
            ("2026-10-01T00:00:00Z", "m2"): Usage(cache_read=7, cache_write_1h=3),
        }
        usage = {"m1": Usage(input=70, output=7), "m2": Usage(cache_read=7, cache_write_1h=3)}
        return make_record("span", started_at=self.T0, wall_seconds=3600, usage=usage, slots=slots, **extra)

    def test_one_row_per_model_and_slot(self, store: Store, make_record: Make) -> None:
        store.upsert_session(self.spanning(make_record))
        assert slot_rows(store) == [
            ("claude:span", "m1", "2026-09-30T23:30:00Z", 10, 1),
            ("claude:span", "m1", "2026-09-30T23:45:00Z", 20, 2),
            ("claude:span", "m1", "2026-10-01T00:00:00Z", 40, 4),
            ("claude:span", "m2", "2026-10-01T00:00:00Z", 0, 0),
        ]

    def test_the_slots_and_the_per_model_totals_come_back(self, store: Store, make_record: Make) -> None:
        rec = self.spanning(make_record)
        store.upsert_session(rec)
        [got] = store.sessions()
        assert got.slots == rec.slots and got.usage == rec.usage and not got.clipped
        assert got.total_usage == Usage(input=70, cache_read=7, output=7, cache_write_1h=3)

    def test_a_record_without_slots_counts_at_the_slot_of_its_start(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("plain", started_at="2026-09-30T10:37:12Z", usage={"m": Usage(input=5), "n": Usage(output=6)}))
        assert slot_rows(store) == [
            ("claude:plain", "m", "2026-09-30T10:30:00Z", 5, 0),
            ("claude:plain", "n", "2026-09-30T10:30:00Z", 0, 6),
        ]
        [got] = store.sessions()
        assert got.slots == {("2026-09-30T10:30:00Z", "m"): Usage(input=5), ("2026-09-30T10:30:00Z", "n"): Usage(output=6)}

    def test_a_session_started_in_a_later_offset_is_slotted_in_utc(self, store: Store, make_record: Make) -> None:
        rec = make_record("tz")
        rec.started_at = "2026-09-30T07:20:00-03:00"
        store.upsert_session(rec)
        assert [slot for _, _, slot, *_ in slot_rows(store)] == ["2026-09-30T10:15:00Z"]

    def test_slots_are_stored_in_one_spelling_and_the_ones_that_meet_are_merged(self, store: Store, make_record: Make) -> None:
        rec = make_record(
            "sloppy", usage={"m": Usage(input=3, output=4)},
            slots={("2026-09-30T10:07:00Z", "m"): Usage(input=1), ("2026-09-30T07:14:59-03:00", "m"): Usage(input=2, output=4)},
        )
        store.upsert_session(rec)
        assert slot_rows(store) == [("claude:sloppy", "m", "2026-09-30T10:00:00Z", 3, 4)]

    def test_an_unreadable_slot_is_refused_and_the_stored_version_survives(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("a", usage={"m": Usage(input=1)}))
        with pytest.raises(ValueError):
            store.upsert_session(make_record("a", usage={"m": Usage(input=2)}, slots={("sometime", "m"): Usage(input=2)}))
        assert store.sessions()[0].usage == {"m": Usage(input=1)}

    def test_upsert_replaces_every_slot_of_the_old_version(self, store: Store, make_record: Make) -> None:
        store.upsert_session(self.spanning(make_record))
        store.upsert_session(make_record("span", started_at=self.T0, usage={"m1": Usage(input=1)}, slots={("2026-09-30T23:30:00Z", "m1"): Usage(input=1)}))
        assert slot_rows(store) == [("claude:span", "m1", "2026-09-30T23:30:00Z", 1, 0)]

    def test_a_stored_session_with_no_slots_mirrors_the_record(self) -> None:
        stored = StoredSession(
            key="claude:x", agent="claude", session_id="x", project="p", started_at="2026-09-30T10:44:00Z",
            ended_at="2026-09-30T10:44:00Z", wall_seconds=0, active_seconds=0, tool_calls=0, files_touched=0,
            error_retries=0, turns=0, prompts=0, primary_model="m", source_path="/x", usage={"m": Usage(input=1)},
        )
        assert stored.slots == {} and not stored.clipped
        assert stored.effective_slots() == {("2026-09-30T10:30:00Z", "m"): Usage(input=1)}


class TestRangeClipping:
    """``sessions(since, until)`` filters and clips by slot time: it returns what was spent inside the range."""

    @pytest.fixture
    def filled(self, store: Store, make_record: Make) -> Store:
        store.upsert_session(TestSlots().spanning(make_record, tool_calls=9, files_touched=4, prompts=3))
        store.upsert_session(make_record("before", agent="codex", project="Other", started_at="2026-09-30T08:00:00Z", usage={"m1": Usage(input=1000)}))
        store.upsert_session(make_record("after", started_at="2026-10-01T05:00:00Z", usage={"m1": Usage(input=2000)}))
        store.upsert_session(make_record("empty", started_at="2026-09-30T23:50:00Z", usage={}))
        return store

    def only(self, sessions: list[StoredSession]) -> StoredSession:
        [got] = [s for s in sessions if s.session_id == "span"]
        return got

    def test_a_session_spanning_the_edge_shows_only_what_was_spent_inside(self, filled: Store) -> None:
        day1 = self.only(filled.sessions(since="2026-09-30T00:00:00Z", until="2026-10-01T00:00:00Z"))
        assert day1.slots == {("2026-09-30T23:30:00Z", "m1"): Usage(input=10, output=1), ("2026-09-30T23:45:00Z", "m1"): Usage(input=20, output=2)}
        assert day1.usage == {"m1": Usage(input=30, output=3)} and day1.clipped
        day2 = self.only(filled.sessions(since="2026-10-01T00:00:00Z", until="2026-10-02T00:00:00Z"))
        assert day2.slots == {("2026-10-01T00:00:00Z", "m1"): Usage(input=40, output=4), ("2026-10-01T00:00:00Z", "m2"): Usage(cache_read=7, cache_write_1h=3)}
        assert day2.usage == {"m1": Usage(input=40, output=4), "m2": Usage(cache_read=7, cache_write_1h=3)} and day2.clipped

    def test_the_two_halves_add_up_to_the_whole_session(self, filled: Store) -> None:
        whole = self.only(filled.sessions())
        halves = [self.only(filled.sessions(since=a, until=b)) for a, b in (("2026-09-30", "2026-10-01"), ("2026-10-01", "2026-10-02"))]
        assert {**halves[0].slots, **halves[1].slots} == whole.slots and len(halves[0].slots) + len(halves[1].slots) == len(whole.slots)
        assert whole.total_usage == halves[0].total_usage + halves[1].total_usage

    def test_everything_else_stays_the_whole_session(self, filled: Store) -> None:
        whole = self.only(filled.sessions())
        clipped = self.only(filled.sessions(since="2026-10-01T00:00:00Z", until="2026-10-02T00:00:00Z"))
        assert clipped.clipped and not whole.clipped
        for name in ("key", "started_at", "ended_at", "wall_seconds", "active_seconds", "tool_calls", "files_touched", "error_retries", "turns", "prompts", "primary_model", "project", "agent", "source_path"):
            assert getattr(clipped, name) == getattr(whole, name), name
        assert (clipped.tool_calls, clipped.files_touched, clipped.prompts) == (9, 4, 3)
        assert clipped.started_at == "2026-09-30T23:40:00Z"  # the start is before the range, yet the session is in it

    def test_a_session_is_returned_iff_it_has_usage_inside(self, filled: Store) -> None:
        ids = lambda **kw: [s.session_id for s in filled.sessions(**kw)]  # noqa: E731
        assert ids(since="2026-09-30T00:00:00Z", until="2026-10-01T00:00:00Z") == ["before", "span"]
        assert ids(since="2026-10-01T00:00:00Z", until="2026-10-02T00:00:00Z") == ["span", "after"]
        assert ids(since="2026-10-01T00:15:00Z", until="2026-10-02T00:00:00Z") == ["after"]
        assert ids(since="2026-10-03T00:00:00Z") == []

    def test_a_session_without_usage_is_only_in_the_whole_database(self, filled: Store) -> None:
        assert "empty" in [s.session_id for s in filled.sessions()]
        assert "empty" not in [s.session_id for s in filled.sessions(since="2000-01-01T00:00:00Z")]
        assert "empty" not in [s.session_id for s in filled.sessions(until="2100-01-01T00:00:00Z")]

    def test_a_range_that_holds_everything_clips_nothing(self, filled: Store) -> None:
        got = filled.sessions(since="2026-01-01T00:00:00Z", until="2027-01-01T00:00:00Z")
        assert [s.session_id for s in got] == ["before", "span", "after"] and not any(s.clipped for s in got)
        assert self.only(got) == self.only(filled.sessions())

    def test_no_range_clips_nothing(self, filled: Store) -> None:
        assert not any(s.clipped for s in filled.sessions())
        assert not any(s.clipped for s in filled.sessions(agent="claude"))

    def test_a_slot_is_inside_when_its_own_start_is(self, filled: Store) -> None:
        exactly = lambda since, until: self.only(filled.sessions(since=since, until=until)).slots  # noqa: E731
        assert list(exactly("2026-09-30T23:45:00Z", "2026-10-01T00:00:00Z")) == [("2026-09-30T23:45:00Z", "m1")]  # until is exclusive
        assert list(exactly("2026-09-30T23:45:00Z", "2026-10-01T00:00:01Z")) == [
            ("2026-09-30T23:45:00Z", "m1"), ("2026-10-01T00:00:00Z", "m1"), ("2026-10-01T00:00:00Z", "m2")
        ]  # since is inclusive

    def test_only_one_bound_clips_too(self, filled: Store) -> None:
        late = self.only(filled.sessions(since="2026-09-30T23:45:00Z"))
        assert len(late.slots) == 3 and late.clipped
        early = self.only(filled.sessions(until="2026-09-30T23:45:00Z"))
        assert list(early.slots) == [("2026-09-30T23:30:00Z", "m1")] and early.clipped

    def test_agent_and_project_filters_combine_with_the_range(self, filled: Store) -> None:
        got = filled.sessions(agent="codex", since="2026-09-30T00:00:00Z", until="2026-10-01T00:00:00Z")
        assert [s.session_id for s in got] == ["before"] and got[0].usage == {"m1": Usage(input=1000)}
        assert [s.session_id for s in filled.sessions(project="OTHER", since="2026-10-01T00:00:00Z")] == []
        assert [s.session_id for s in filled.sessions(project="proj", since="2026-10-01T00:00:00Z")] == ["span", "after"]

    def test_the_order_is_start_then_key_whatever_the_range(self, filled: Store, make_record: Make) -> None:
        for sid in ("b", "a"):
            filled.upsert_session(make_record(sid, started_at="2026-09-30T23:40:00Z", usage={"m": Usage(input=1)}))
        got = [s.session_id for s in filled.sessions(since="2026-09-30T23:00:00Z", until="2026-10-01T01:00:00Z")]
        assert got == ["a", "b", "span"]  # same start: by key, "claude:a" < "claude:b" < "claude:span"


class TestWholeUsage:
    def test_it_returns_the_whole_session_however_the_range_clipped_it(self, store: Store, make_record: Make) -> None:
        store.upsert_session(TestSlots().spanning(make_record))
        store.upsert_session(make_record("plain", usage={"m1": Usage(input=5)}))
        [clipped] = store.sessions(since="2026-10-01T00:00:00Z", until="2026-10-02T00:00:00Z")
        assert clipped.usage["m1"] == Usage(input=40, output=4)
        whole = store.whole_usage([clipped.key, "claude:plain"])
        assert whole == {
            "claude:span": {"m1": Usage(input=70, output=7), "m2": Usage(cache_read=7, cache_write_1h=3)},
            "claude:plain": {"m1": Usage(input=5)},
        }

    def test_a_session_without_usage_or_unknown_maps_to_an_empty_dict(self, store: Store, make_record: Make) -> None:
        store.upsert_session(make_record("empty", usage={}))
        assert store.whole_usage(["claude:empty", "claude:nope"]) == {"claude:empty": {}, "claude:nope": {}}
        assert store.whole_usage([]) == {}

    def test_many_keys_and_repeated_keys(self, store: Store, make_record: Make) -> None:
        keys = []
        for i in range(1234):  # more than one query's worth of bound parameters
            store.upsert_session(make_record(f"s{i}", usage={"m": Usage(input=i)}))
            keys.append(f"claude:s{i}")
        whole = store.whole_usage(keys + keys[:3])
        assert len(whole) == 1234 and all(whole[f"claude:s{i}"] == {"m": Usage(input=i)} for i in range(1234))


class TestSpeed:
    def test_a_realistic_database_loads_well_under_a_second(self, store: Store, make_record: Make) -> None:
        """2500 sessions of 20 slots each (about 50,000 usage rows), the size of a busy year."""
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(2500):
            begin = start + timedelta(minutes=37 * i)
            slots = {(slot_of(begin + timedelta(minutes=15 * j)), "m"): Usage(input=j + 1, output=1) for j in range(20)}
            usage = {"m": Usage(input=sum(u.input for u in slots.values()), output=20)}
            store.upsert_session(make_record(f"s{i}", agent=("claude", "codex", "gemini")[i % 3], project=f"p{i % 40}", started_at=fmt_iso(begin), usage=usage, slots=slots))
        assert store.conn.execute("SELECT COUNT(*) FROM session_usage").fetchone()[0] == 50_000
        for kwargs in ({}, {"agent": "claude"}, {"since": "2026-02-01T00:00:00Z", "until": "2026-02-08T00:00:00Z"}, {"project": "p3", "since": "2026-03-01T00:00:00Z"}):
            began = time.perf_counter()
            got = store.sessions(**kwargs)
            assert got and time.perf_counter() - began < 1.0, kwargs


class TestANewerDatabaseIsRefused:
    """A file a newer release wrote is not ours to open for writing, upgrade or stamp with an older version number."""

    def make(self, path: Path, version: int) -> None:
        Store(path).close()
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        conn.close()

    def test_opening_it_is_a_store_error_that_says_what_to_do(self, tmp_path: Path) -> None:
        db = tmp_path / "new.db"
        self.make(db, 9)
        with pytest.raises(StoreError, match=r"written by a newer agentfare; upgrade agentfare or delete the file") as caught:
            Store(db)
        assert str(db) in str(caught.value)

    def test_it_is_left_exactly_as_it_was(self, tmp_path: Path) -> None:
        db = tmp_path / "new.db"
        self.make(db, 9)
        before = db.read_bytes()
        with pytest.raises(StoreError):
            Store(db)
        assert db.read_bytes() == before
        assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 9  # not stamped back to ours

    def test_one_version_ahead_is_enough(self, tmp_path: Path) -> None:
        db = tmp_path / "new.db"
        self.make(db, SCHEMA_VERSION + 1)
        with pytest.raises(StoreError, match="newer agentfare"):
            Store(db)

    def test_the_read_only_open_says_the_same(self, tmp_path: Path) -> None:
        db = tmp_path / "new.db"
        self.make(db, 9)
        with pytest.raises(StoreError, match=r"written by a newer agentfare; upgrade agentfare or delete the file"):
            Store.open_read_only(db)

    def test_the_command_line_ends_in_error_2_not_a_traceback(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from agentfare.cli import app

        self.make(tmp_path / "agentfare.db", 9)
        for argv in (["summary"], ["--no-ingest", "sessions"], ["ingest"]):
            result = CliRunner().invoke(app, ["--home", str(tmp_path), *argv])
            assert result.exit_code == 2 and "written by a newer agentfare; upgrade agentfare or delete the file" in result.stderr, argv
            assert result.stderr.startswith("error: ") and "Traceback" not in result.output

    def test_the_version_is_stamped_only_when_it_is_lower(self, tmp_path: Path) -> None:
        db = tmp_path / "fare.db"
        with Store(db) as s:
            assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION  # a new file gets ours
        statements: list[str] = []
        real = sqlite3.connect

        def spying(*args: object, **kwargs: object) -> sqlite3.Connection:
            conn = real(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        sqlite3.connect = spying  # type: ignore[assignment]
        try:
            Store(db).close()
        finally:
            sqlite3.connect = real  # type: ignore[assignment]
        assert not any("user_version =" in statement for statement in statements)  # a current file is not written to again

    def test_a_lower_version_is_raised_to_ours(self, tmp_path: Path) -> None:
        db = tmp_path / "fare.db"
        self.make(db, 0)
        with Store(db) as s:
            assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


class TestRereadPending:
    """``Store.reread_pending``: a session whose log file no ingest has read since the upgrade (its money is still at the start
    slot) and that an ingest could still read."""

    def log(self, tmp_path: Path, name: str = "x.jsonl") -> Path:
        path = tmp_path / name
        path.write_text("{}\n")
        return path

    def test_an_upgraded_database_is_pending_until_its_logs_are_read(self, tmp_path: Path) -> None:
        db, log = tmp_path / "old.db", self.log(tmp_path)
        build_v2_database(db)
        sqlite3.connect(db).execute("UPDATE sessions SET source_path = ?", (str(log),)).connection.commit()
        with Store(db) as s:
            assert s.count() == len(V2_SESSIONS) and s.reread_pending()  # the upgrade forgot every file
            s.mark_file(str(log), "claude", "sig", 7)  # the file the old sessions came from is read again
            assert not s.reread_pending()

    def test_a_log_the_agent_has_deleted_cannot_be_read_again_so_it_is_not_pending(self, tmp_path: Path) -> None:
        db = tmp_path / "old.db"
        build_v2_database(db)  # its sessions name /logs/x.jsonl, which is not there
        with Store(db) as s:
            assert s.count() == len(V2_SESSIONS) and not s.reread_pending()

    def test_an_empty_database_and_a_fully_read_one_are_not(self, tmp_path: Path, store: Store, make_record: Make) -> None:
        log = self.log(tmp_path, "a.jsonl")
        assert not store.reread_pending()
        store.upsert_session(make_record("a", source_path=str(log)))
        assert store.reread_pending()
        store.mark_file(str(log), "claude", "sig", 1)
        assert not store.reread_pending()

    def test_one_file_read_and_another_not_is_still_behind(self, tmp_path: Path, store: Store, make_record: Make) -> None:
        one, two = self.log(tmp_path, "one.jsonl"), self.log(tmp_path, "two.jsonl")
        store.upsert_session(make_record("a", source_path=str(one)))
        store.upsert_session(make_record("b", source_path=str(two), agent="codex"))
        store.mark_file(str(one), "claude", "sig", 1)  # `ingest --agent claude` after the upgrade
        assert store.reread_pending()

    def test_a_real_ingest_clears_it(self, tmp_path: Path) -> None:
        fixtures = Path(__file__).parent / "fixtures"
        cfg = Config.load(fixtures / "config.yaml", home=tmp_path)
        with Store(tmp_path / "fare.db") as s:
            ingest(cfg, s)
            assert s.count() == 3 and not s.reread_pending()

    def test_end_to_end_the_note_is_on_stderr_once_and_the_json_stays_clean(self, tmp_path: Path) -> None:
        import json

        from typer.testing import CliRunner

        from agentfare.cli import app

        fixtures = Path(__file__).parent / "fixtures"
        (log,) = get_parser("claude").discover(fixtures / "claude" / "projects")
        build_v2_database(tmp_path / "agentfare.db")
        conn = sqlite3.connect(tmp_path / "agentfare.db")
        conn.execute("UPDATE sessions SET source_path = ?", (str(log),))  # logs that are still there to be read
        conn.commit()
        conn.close()
        base = ["--config", str(fixtures / "config.yaml"), "--home", str(tmp_path)]
        note = "database upgraded: day and week splits are approximate until agentfare ingest has re-read your logs"
        for command in (["summary"], ["sessions"], ["models"], ["advise"], ["summary", "--json"]):
            result = CliRunner().invoke(app, [*base, "--no-ingest", *command])
            assert result.exit_code == 0, (command, result.output)
            assert result.stderr.count(note) == 1 and note not in result.stdout, command
        json.loads(CliRunner().invoke(app, [*base, "--no-ingest", "summary", "--json"]).stdout)
        again = CliRunner().invoke(app, [*base, "summary"])  # a normal command re-reads the logs first
        assert again.exit_code == 0 and note not in again.stderr
        later = CliRunner().invoke(app, [*base, "--no-ingest", "summary"])
        assert later.exit_code == 0 and note not in later.stderr  # and then there is nothing left to say


class TestTheCopyOfASession:
    def test_the_source_and_the_size_of_what_is_stored(self, store: Store, make_record: Make) -> None:
        assert store.session_copy("claude:a") is None
        store.upsert_session(make_record("a", usage={"m": Usage(input=10, cache_read=20, cache_write=30, output=40, cache_write_1h=50)}))
        assert store.session_copy("claude:a") == ("/logs/a.jsonl", 150)
        store.upsert_session(make_record("b", usage={}))
        assert store.session_copy("claude:b") == ("/logs/b.jsonl", 0)

    def test_the_first_and_last_slot_with_usage_under_the_filters(self, store: Store, make_record: Make) -> None:
        assert store.usage_range() is None
        store.upsert_session(make_record("a", project="Acme", started_at="2026-09-01T10:05:00Z"))
        store.upsert_session(make_record("b", project="beta", agent="codex", started_at="2026-09-20T11:40:00Z"))
        assert store.usage_range() == ("2026-09-01T10:00:00Z", "2026-09-20T11:30:00Z")
        assert store.usage_range(project="acme") == ("2026-09-01T10:00:00Z", "2026-09-01T10:00:00Z")  # caseless, like --project
        assert store.usage_range(agent="codex") == ("2026-09-20T11:30:00Z", "2026-09-20T11:30:00Z")
        assert store.usage_range(agent="gemini") is None and store.usage_range(project="nobody") is None

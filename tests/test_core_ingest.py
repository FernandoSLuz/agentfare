from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from agentfare import ingest as ingest_module
from agentfare import parsers
from agentfare.config import Config, ConfigError
from agentfare.db import Store, StoreError
from agentfare.ingest import IngestReport, ingest
from agentfare.models import AGENTS, SessionRecord, Usage

MODEL = "claude-sonnet-5-5"


class FakeParser:
    """Reads ``*.log`` files with one session per line: ``<id> <project> <input> <output>``.

    ``-`` as the input count means the session has no usage; a trailing ``+synthetic`` adds a second model that
    used no tokens; ``BOOM`` raises. The signature is the file content itself, so tests do not depend on mtime
    granularity.
    """

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.parsed: list[str] = []
        self.idle_gaps: list[int] = []
        self.discover_error: Exception | None = None
        self.reverse = False

    def discover(self, root: Path) -> Iterator[Path]:
        if self.discover_error:
            raise self.discover_error
        yield from sorted(root.rglob("*.log"), reverse=self.reverse)

    def signature(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def parse(self, path: Path, *, idle_gap_seconds: int = 1800) -> list[SessionRecord]:
        self.parsed.append(path.name)
        self.idle_gaps.append(idle_gap_seconds)
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line == "BOOM":
                raise RuntimeError("boom")
            session_id, project, tokens_in, tokens_out, *flags = line.split()
            usage: dict[str, Usage] = {}
            if tokens_in != "-":
                usage = {MODEL: Usage(input=int(tokens_in), output=int(tokens_out))}
            if "+synthetic" in flags:
                usage["<synthetic>"] = Usage()
            records.append(
                SessionRecord(
                    agent=self.agent, session_id=session_id, source_path=str(path), project=project,
                    started_at="2026-09-30T10:00:00Z", ended_at="2026-09-30T10:10:00Z",
                    wall_seconds=600, active_seconds=600, usage=usage,
                )
            )
        return records


class Env:
    def __init__(self, tmp_path: Path, make_config: Callable[..., Config], monkeypatch: pytest.MonkeyPatch) -> None:
        self.claude_root = tmp_path / "logs" / "claude"
        self.claude_root.mkdir(parents=True)
        self.codex_root = tmp_path / "logs" / "codex-not-created"
        self.parsers = {agent: FakeParser(agent) for agent in AGENTS}
        self.cfg = make_config(
            timezone="UTC",
            idle_gap_minutes=7,
            sources={"claude": [str(self.claude_root)], "codex": [str(self.codex_root)], "gemini": []},
        )
        self.store = Store(":memory:")
        monkeypatch.setattr(ingest_module, "get_parser", lambda agent: self.parsers[agent])

    @property
    def claude(self) -> FakeParser:
        return self.parsers["claude"]

    def write(self, name: str, *lines: str) -> Path:
        path = self.claude_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def run(self, **kw: object) -> IngestReport:
        self.claude.parsed.clear()
        return ingest(self.cfg, self.store, **kw)  # type: ignore[arg-type]


@pytest.fixture
def env(tmp_path: Path, make_config: Callable[..., Config], monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    e = Env(tmp_path, make_config, monkeypatch)
    yield e
    e.store.close()


def stored_ids(env: Env) -> list[str]:
    return sorted(s.session_id for s in env.store.sessions())


class TestIncremental:
    def test_first_run_parses_and_stores_everything(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1", "s2 alpha 20 2")
        env.write("sub/b.log", "s3 beta 30 3")
        rep = env.run()
        assert (rep.files_seen, rep.files_parsed, rep.files_unchanged) == (2, 2, 0)
        assert rep.sessions == 3 and rep.by_agent == {"claude": 3}
        assert rep.errors == []
        assert stored_ids(env) == ["s1", "s2", "s3"]
        assert env.store.sessions(project="beta")[0].usage == {MODEL: Usage(input=30, output=3)}

    def test_unchanged_files_are_skipped(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.run()
        rep = env.run()
        assert env.claude.parsed == []
        assert (rep.files_seen, rep.files_parsed, rep.files_unchanged, rep.sessions) == (1, 0, 1, 0)
        assert rep.by_agent == {}
        assert env.store.count() == 1

    def test_the_signature_is_recorded_under_the_path(self, env: Env) -> None:
        path = env.write("a.log", "s1 alpha 10 1")
        env.run()
        # the parser's own signature (here the content), the idle gap in seconds (7 minutes) and the parsers' version
        assert env.store.file_signature(str(path)) == f"{path.read_text()}|gap=420|p={parsers.PARSERS_VERSION}"

    def test_a_changed_signature_re_parses_only_that_file(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        b = env.write("b.log", "s2 alpha 20 2")
        env.run()
        b.write_text("s2 alpha 99 9\ns4 alpha 5 5\n", encoding="utf-8")
        rep = env.run()
        assert env.claude.parsed == ["b.log"]
        assert (rep.files_parsed, rep.files_unchanged, rep.sessions) == (1, 1, 2)
        assert stored_ids(env) == ["s1", "s2", "s4"]
        assert {s.session_id: s.usage[MODEL].input for s in env.store.sessions()} == {"s1": 10, "s2": 99, "s4": 5}

    def test_full_re_parses_unchanged_files_without_duplicating(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.write("b.log", "s2 alpha 20 2")
        env.run()
        rep = env.run(full=True)
        assert env.claude.parsed == ["a.log", "b.log"]
        assert (rep.files_parsed, rep.files_unchanged, rep.sessions) == (2, 0, 2)
        assert env.store.count() == 2

    def test_new_files_are_picked_up_later(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.run()
        env.write("c.log", "s9 gamma 1 1")
        rep = env.run()
        assert env.claude.parsed == ["c.log"] and rep.sessions == 1
        assert stored_ids(env) == ["s1", "s9"]

    def test_history_survives_when_a_log_is_deleted(self, env: Env) -> None:
        path = env.write("a.log", "s1 alpha 10 1")
        env.run()
        path.unlink()
        rep = env.run()
        assert rep.files_seen == 0 and stored_ids(env) == ["s1"]

    def test_files_are_processed_in_path_order_whatever_the_parser_yields(self, env: Env) -> None:
        for name in ("c.log", "a.log", "b.log"):
            env.write(name, f"{name[0]} alpha 1 1")
        env.claude.reverse = True
        seen: list[tuple[str, str]] = []
        env.run(on_file=lambda agent, path: seen.append((agent, Path(path).name)))
        assert seen == [("claude", "a.log"), ("claude", "b.log"), ("claude", "c.log")]

    def test_on_file_is_called_only_for_files_that_get_parsed(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        seen: list[str] = []
        env.run(on_file=lambda agent, path: seen.append(path))
        assert len(seen) == 1 and seen[0].endswith("a.log")
        env.run(on_file=lambda agent, path: seen.append(path))
        assert len(seen) == 1  # nothing changed
        env.run(full=True, on_file=lambda agent, path: seen.append(path))
        assert len(seen) == 2

    def test_the_configured_idle_gap_reaches_the_parser(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.run()
        assert env.claude.idle_gaps == [7 * 60]


class TestSignatureCoversWhatShapesTheRecords:
    def test_changing_the_idle_gap_re_reads_the_logs_without_full(self, env: Env, make_config: Callable[..., Config]) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.run()
        assert env.run().files_unchanged == 1
        env.cfg = make_config(timezone="UTC", idle_gap_minutes=45, sources=env.cfg.data["sources"])
        rep = env.run()
        assert env.claude.parsed == ["a.log"] and env.claude.idle_gaps[-1] == 45 * 60
        assert (rep.files_parsed, rep.files_unchanged) == (1, 0)
        assert env.run().files_unchanged == 1  # and then it settles again

    def test_a_new_parsers_version_re_reads_the_logs_without_full(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.run()
        monkeypatch.setattr(parsers, "PARSERS_VERSION", parsers.PARSERS_VERSION + 1)
        rep = env.run()
        assert env.claude.parsed == ["a.log"] and (rep.files_parsed, rep.files_unchanged) == (1, 0)
        assert env.store.count() == 1
        assert env.run().files_unchanged == 1

    def test_a_database_that_remembers_the_bare_parser_signature_is_re_read(self, env: Env) -> None:
        """What version 2 stored: the signature alone. It never equals the new, longer one."""
        path = env.write("a.log", "s1 alpha 10 1")
        env.store.mark_file(str(path), "claude", path.read_text(), 1)
        rep = env.run()
        assert env.claude.parsed == ["a.log"] and rep.files_parsed == 1


class TestCounts:
    def test_a_session_found_in_two_files_is_read_twice_and_counted_once(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1", "s2 alpha 20 2")
        env.write("b.log", "s1 alpha 30 3")  # the same session again, as a resumed one is
        rep = env.run()
        assert (rep.files_parsed, rep.sessions_read, rep.sessions) == (2, 3, 2)
        assert rep.by_agent == {"claude": 2}
        assert env.store.count() == rep.sessions

    def test_records_that_were_not_stored_are_not_counted(self, env: Env) -> None:
        env.write("a.log", "s1 alpha - 0", "s2 alpha 10 1")
        rep = env.run()
        assert (rep.sessions_read, rep.sessions) == (1, 1)

    def test_an_unchanged_run_reads_nothing(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.run()
        rep = env.run()
        assert (rep.sessions_read, rep.sessions, rep.by_agent) == (0, 0, {})

    def test_each_agent_counts_its_own_distinct_sessions(self, env: Env, tmp_path: Path) -> None:
        env.write("a.log", "same alpha 10 1", "same alpha 11 1")
        codex_root = tmp_path / "codex-logs"
        codex_root.mkdir()
        (codex_root / "x.log").write_text("same beta 5 5\n", encoding="utf-8")
        env.cfg = Config(home=env.cfg.home, data={**env.cfg.data, "sources": {"claude": [str(env.claude_root)], "codex": [str(codex_root)], "gemini": []}}, base_dir=tmp_path)
        rep = env.run()
        assert (rep.sessions_read, rep.sessions, rep.by_agent) == (3, 2, {"claude": 1, "codex": 1})


class TestSlotsSurviveIngestion:
    def record(self, **extra: object) -> SessionRecord:
        usage = {MODEL: Usage(input=30, output=3), "<synthetic>": Usage()}
        slots = {
            ("2026-09-30T23:45:00Z", MODEL): Usage(input=10, output=1),
            ("2026-10-01T00:00:00Z", MODEL): Usage(input=20, output=2),
            ("2026-10-01T00:00:00Z", "<synthetic>"): Usage(),
            ("2026-10-01T00:15:00Z", MODEL): Usage(),
        }
        return SessionRecord(
            agent="claude", session_id="s", source_path="/x", project="p", started_at="2026-09-30T23:50:00Z",
            ended_at="2026-10-01T00:10:00Z", wall_seconds=1200, active_seconds=1200, usage=usage, slots=slots, **extra,  # type: ignore[arg-type]
        )

    def test_slots_of_zero_token_models_and_empty_slots_are_dropped_with_them(self) -> None:
        billed = ingest_module._billed(self.record())
        assert billed is not None
        assert billed.usage == {MODEL: Usage(input=30, output=3)}
        assert billed.slots == {
            ("2026-09-30T23:45:00Z", MODEL): Usage(input=10, output=1), ("2026-10-01T00:00:00Z", MODEL): Usage(input=20, output=2)
        }

    def test_a_record_with_nothing_to_drop_is_passed_through_as_it_is(self) -> None:
        rec = self.record()
        rec.usage, rec.slots = {MODEL: Usage(input=1)}, {("2026-09-30T23:45:00Z", MODEL): Usage(input=1)}
        assert ingest_module._billed(rec) is rec

    def test_a_parser_that_fills_slots_gets_them_stored(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.claude.parse = lambda path, *, idle_gap_seconds=1800: [self.record()]  # type: ignore[method-assign]
        env.run()
        [got] = env.store.sessions()
        assert sorted(got.slots) == [("2026-09-30T23:45:00Z", MODEL), ("2026-10-01T00:00:00Z", MODEL)]
        assert got.usage == {MODEL: Usage(input=30, output=3)}
        [day2] = env.store.sessions(since="2026-10-01T00:00:00Z")
        assert day2.usage == {MODEL: Usage(input=20, output=2)} and day2.clipped


class TestRecordsWithoutUsage:
    def test_sessions_without_usage_are_not_stored(self, env: Env) -> None:
        env.write("a.log", "s1 alpha - 0", "s2 alpha 10 1")
        rep = env.run()
        assert stored_ids(env) == ["s2"]
        assert (rep.sessions, rep.by_agent) == (1, {"claude": 1})

    def test_a_file_with_only_empty_sessions_is_still_marked_as_seen(self, env: Env) -> None:
        path = env.write("a.log", "s1 alpha - 0")
        rep = env.run()
        assert rep.files_parsed == 1 and rep.sessions == 0 and env.store.count() == 0
        assert env.store.file_signature(str(path)) is not None
        env.run()
        assert env.claude.parsed == []  # not parsed over and over

    def test_sessions_whose_models_all_have_zero_tokens_are_not_stored(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 0 0", "s2 alpha 10 1", "s3 alpha - 0 +synthetic")
        rep = env.run()
        assert stored_ids(env) == ["s2"] and rep.sessions == 1

    def test_zero_token_models_are_dropped_from_a_session_that_has_real_usage(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1 +synthetic")
        env.run()
        [got] = env.store.sessions()
        assert got.usage == {MODEL: Usage(input=10, output=1)}
        assert got.primary_model == MODEL
        assert env.store.conn.execute("SELECT COUNT(*) FROM session_usage").fetchone()[0] == 1


class TestFailures:
    def test_one_bad_file_is_reported_and_does_not_stop_the_others(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        bad = env.write("b.log", "BOOM")
        env.write("c.log", "s3 alpha 30 3")
        rep = env.run()
        assert stored_ids(env) == ["s1", "s3"]
        assert (rep.files_seen, rep.files_parsed) == (3, 2)
        assert rep.errors == [f"claude: {bad}: RuntimeError: boom"]

    def test_a_failed_file_is_retried_until_it_parses(self, env: Env) -> None:
        bad = env.write("b.log", "BOOM")
        assert len(env.run().errors) == 1
        assert env.store.file_signature(str(bad)) is None
        assert len(env.run().errors) == 1 and env.claude.parsed == ["b.log"]
        bad.write_text("s5 alpha 5 5\n", encoding="utf-8")
        rep = env.run()
        assert rep.errors == [] and stored_ids(env) == ["s5"]

    def test_a_file_that_cannot_be_read_is_isolated_too(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.write("b.log", "not enough fields")  # the fake parser dies unpacking it
        rep = env.run()
        assert stored_ids(env) == ["s1"] and len(rep.errors) == 1 and "ValueError" in rep.errors[0]

    def test_a_failing_signature_is_isolated(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.write("b.log", "s2 alpha 20 2")

        def flaky(path: Path) -> str:
            if path.name == "a.log":
                raise PermissionError("denied")
            return path.read_text(encoding="utf-8")

        monkeypatch.setattr(env.claude, "signature", flaky)
        rep = env.run()
        assert stored_ids(env) == ["s2"] and rep.errors and "PermissionError" in rep.errors[0]

    def test_a_failure_while_walking_a_root_is_reported_and_other_roots_still_run(self, env: Env, tmp_path: Path) -> None:
        second = tmp_path / "logs" / "claude2"
        second.mkdir()
        (second / "z.log").write_text("s7 alpha 7 7\n", encoding="utf-8")
        env.cfg.data["sources"]["claude"].append(str(second))
        env.write("a.log", "s1 alpha 10 1")

        original = env.claude.discover

        def discover(root: Path) -> Iterator[Path]:
            if root == env.claude_root:
                raise OSError("cannot list directory")
            return original(root)

        env.claude.discover = discover  # type: ignore[method-assign]
        rep = env.run()
        assert stored_ids(env) == ["s7"]
        assert rep.errors == [f"claude: {env.claude_root}: OSError: cannot list directory"]

    def test_a_walk_that_fails_half_way_keeps_what_it_found_before_failing(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.write("b.log", "s2 alpha 20 2")
        original = env.claude.discover

        def discover(root: Path) -> Iterator[Path]:
            for i, path in enumerate(original(root)):
                if i == 1:
                    raise OSError("vanished")
                yield path

        env.claude.discover = discover  # type: ignore[method-assign]
        rep = env.run()
        assert stored_ids(env) == ["s1"] and len(rep.errors) == 1


class TestRoots:
    def test_missing_roots_are_reported_and_skipped(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        rep = env.run()
        assert rep.missing_roots == [f"codex: {env.codex_root}"]
        assert rep.errors == [] and rep.files_seen == 1

    def test_an_agent_without_sources_is_silent(self, env: Env) -> None:
        rep = env.run(agents=["gemini"])
        assert rep.missing_roots == [] and rep.files_seen == 0

    def test_agents_can_be_limited(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        rep = env.run(agents=("codex",))
        assert rep.files_seen == 0 and rep.missing_roots == [f"codex: {env.codex_root}"]
        assert env.store.count() == 0
        assert env.run(agents=["claude"]).sessions == 1

    def test_several_roots_of_one_agent_are_all_scanned(self, env: Env, tmp_path: Path) -> None:
        second = tmp_path / "elsewhere"
        second.mkdir()
        (second / "z.log").write_text("s8 alpha 8 8\n", encoding="utf-8")
        env.cfg.data["sources"]["claude"].append(str(second))
        env.write("a.log", "s1 alpha 10 1")
        assert env.run().files_seen == 2 and stored_ids(env) == ["s1", "s8"]

    def test_each_agent_uses_its_own_parser_and_is_counted_separately(self, env: Env, tmp_path: Path) -> None:
        codex_root = tmp_path / "logs" / "codex-real"
        codex_root.mkdir()
        (codex_root / "x.log").write_text("x1 alpha 5 5\nx2 alpha 6 6\n", encoding="utf-8")
        env.cfg.data["sources"]["codex"] = [str(codex_root)]
        env.write("a.log", "s1 alpha 10 1")
        rep = env.run()
        assert rep.by_agent == {"claude": 1, "codex": 2} and rep.sessions == 3
        assert sorted(s.key for s in env.store.sessions()) == ["claude:s1", "codex:x1", "codex:x2"]

    def test_an_unknown_agent_name_is_an_error_not_a_silent_skip(self, make_config: Callable[..., Config]) -> None:
        with Store(":memory:") as store, pytest.raises(KeyError, match="unknown agent"):
            ingest(make_config(), store, ["not-an-agent"])


class TestConfigErrorsAreNotFileErrors:
    def test_a_bad_idle_gap_stops_the_run_once_instead_of_failing_every_file(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.write("b.log", "s2 alpha 20 2")
        env.cfg.data["idle_gap_minutes"] = -5
        with pytest.raises(ConfigError, match="idle_gap_minutes"):
            env.run()
        assert env.claude.parsed == [] and env.store.count() == 0


class TestDamagedDatabaseIsNotAFileError:
    """A database that cannot be used is one problem for the whole run, not one 'could not parse' per log file."""

    @pytest.mark.parametrize("method", ["upsert_session", "mark_file", "file_signature"])
    def test_a_store_error_stops_the_run_at_the_first_file(self, env: Env, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.write("b.log", "s2 alpha 20 2")

        def broken(*args: object, **kwargs: object) -> None:
            raise StoreError("cannot use the database x.db: database disk image is malformed (delete the file to start over)")

        monkeypatch.setattr(env.store, method, broken)
        with pytest.raises(StoreError, match="delete the file to start over"):
            env.run()
        assert len(env.claude.parsed) <= 1  # the second file was never tried

    def test_other_errors_of_one_file_are_still_only_that_files_error(self, env: Env) -> None:
        env.write("a.log", "BOOM")
        env.write("b.log", "s2 alpha 20 2")
        rep = env.run()
        assert len(rep.errors) == 1 and stored_ids(env) == ["s2"]


class Unreadable(type(Path())):  # type: ignore[misc]
    """A root that cannot be looked at, like one inside a directory without the search permission."""

    def exists(self, **kw: object) -> bool:
        raise PermissionError(13, "Permission denied", str(self))


class TestUnreadableRoot:
    def test_a_root_that_cannot_be_checked_is_reported_and_the_next_one_still_runs(
        self, env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.write("a.log", "s1 alpha 10 1")
        locked = Unreadable(tmp_path / "locked")
        monkeypatch.setattr(env.cfg, "sources", lambda agent: [locked, env.claude_root] if agent == "claude" else [])
        rep = env.run()
        assert stored_ids(env) == ["s1"]
        assert len(rep.errors) == 1 and rep.errors[0].startswith(f"claude: {locked}: PermissionError")


class TestTheSameSessionInTwoFiles:
    """Two log files that no parser merges (an archived log and its live copy, two roots) hold the same session: the copy
    with the most tokens stays, whichever file is read last."""

    def tokens(self, env: Env, key: str = "claude:s1") -> int:
        return env.store.session_copy(key)[1]  # type: ignore[index]

    def test_the_bigger_copy_stays_even_when_the_smaller_one_is_read_last(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 300 30")
        env.write("b.log", "s1 alpha 10 1")  # read after a.log, a truncated copy
        rep = env.run()
        assert self.tokens(env) == 330 and env.store.session_copy("claude:s1")[0].endswith("a.log")  # type: ignore[index]
        assert rep.duplicates == 1 and (rep.sessions_read, rep.sessions, env.store.count()) == (1, 1, 1)  # the loser was not stored

    def test_the_bigger_copy_replaces_a_smaller_one_read_first(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.write("b.log", "s1 alpha 300 30")
        rep = env.run()
        assert self.tokens(env) == 330 and rep.duplicates == 1

    def test_on_a_tie_the_later_file_stays(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1")
        env.write("b.log", "s1 alpha 10 1")
        rep = env.run()
        assert env.store.session_copy("claude:s1")[0].endswith("b.log") and rep.duplicates == 1  # type: ignore[index]
        env.claude.reverse = True  # walked the other way round: the same file stays
        env.run(full=True)
        assert env.store.session_copy("claude:s1")[0].endswith("b.log")  # type: ignore[index]

    def test_the_outcome_does_not_depend_on_the_order_of_the_walk(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 300 30")
        env.write("b.log", "s1 alpha 10 1")
        env.run()
        env.claude.reverse = True
        rep = env.run(full=True)
        assert self.tokens(env) == 330 and rep.duplicates == 1

    def test_a_smaller_copy_that_changes_later_does_not_take_the_session_back(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 300 30")
        env.write("b.log", "s1 alpha 10 1")
        env.run()
        env.write("b.log", "s1 alpha 20 2")  # the truncated copy grows, but not past the other
        rep = env.run()
        assert env.claude.parsed == ["b.log"] and self.tokens(env) == 330 and rep.duplicates == 1 and rep.sessions_read == 0
        env.write("b.log", "s1 alpha 900 90")  # until it does
        rep = env.run()
        assert self.tokens(env) == 990 and env.store.session_copy("claude:s1")[0].endswith("b.log")  # type: ignore[index]

    def test_the_file_a_session_came_from_always_replaces_it_when_it_changes(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 300 30")
        env.run()
        env.write("a.log", "s1 alpha 10 1")  # rotated or cut: what the file says now is what the session is
        rep = env.run()
        assert self.tokens(env) == 11 and rep.duplicates == 0 and rep.sessions_read == 1

    def test_no_copy_no_duplicate(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1", "s2 alpha 20 2")
        env.write("b.log", "s3 alpha 30 3")
        assert env.run().duplicates == 0

    def test_each_session_is_counted_once_however_many_copies(self, env: Env) -> None:
        env.write("a.log", "s1 alpha 10 1", "s2 alpha 20 2")
        env.write("b.log", "s1 alpha 11 1", "s2 alpha 21 2")
        env.write("c.log", "s1 alpha 12 1")
        assert env.run().duplicates == 2

    def test_the_warning_names_the_count(self) -> None:
        from agentfare.ingest import duplicates_warning

        assert duplicates_warning(1) == "1 session appears in more than one log file; the copy with the most tokens was kept"
        assert duplicates_warning(3) == "3 sessions appear in more than one log file; the copy with the most tokens was kept"


FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_LOG = FIXTURES / "claude" / "projects" / "-home-dev-acme-shop" / "2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402.jsonl"
CODEX_LOG = next((FIXTURES / "codex" / "sessions").rglob("rollout-*.jsonl"))


class TestRealLogsCopiedAndCut:
    """The same on the real parsers, with truncated copies: Codex's live and archived directories, two Claude roots."""

    @pytest.mark.parametrize("cut_is_read_first", [True, False])
    @pytest.mark.parametrize(
        ("agent", "source", "keep", "roots"),
        [("codex", CODEX_LOG, 14, ("sessions", "archived_sessions")), ("claude", CLAUDE_LOG, 60, ("one", "two"))],
        ids=["codex", "claude"],
    )
    def test_the_whole_copy_is_the_one_kept_whichever_is_read_last(
        self, tmp_path: Path, agent: str, source: Path, keep: int, roots: tuple[str, str], cut_is_read_first: bool
    ) -> None:
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
        first, second = roots
        placed = {}
        for root, body, name in ((first, lines[:keep] if cut_is_read_first else lines, "cut" if cut_is_read_first else "whole"),
                                 (second, lines if cut_is_read_first else lines[:keep], "whole" if cut_is_read_first else "cut")):
            (tmp_path / root / "x").mkdir(parents=True)
            placed[name] = tmp_path / root / "x" / source.name
            placed[name].write_text("".join(body), encoding="utf-8")
        cfg = Config(home=tmp_path / "home", base_dir=tmp_path, data={
            "timezone": "UTC", "idle_gap_minutes": 30,
            "sources": {a: [str(tmp_path / first), str(tmp_path / second)] if a == agent else [] for a in AGENTS}})
        parser = parsers.get_parser(agent)
        with Store(tmp_path / "fare.db") as store:
            rep = ingest(cfg, store, [agent])
            whole_tokens, cut_tokens = (parser.parse(placed[name])[0].total_usage.total for name in ("whole", "cut"))
            [kept] = store.sessions()
            assert 0 < cut_tokens < whole_tokens  # the cut copy is a real, smaller session
            assert rep.errors == [] and (rep.files_parsed, rep.duplicates, store.count()) == (2, 1, 1)
            assert kept.total_usage.total == whole_tokens and kept.source_path == str(placed["whole"])
            assert ingest(cfg, store, [agent], full=True).duplicates == 1  # read again in full: the same outcome
            assert store.sessions()[0].source_path == str(placed["whole"])

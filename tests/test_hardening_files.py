"""What AgentFare writes to disk: private modes for what it creates, and no write through a link somebody planted.

The modes are checked in a child process with ``umask 022`` (the usual default, under which a plain ``open`` makes a
world-readable file), through the real command line.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentfare.cli import app

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config.yaml"
NOW = "2026-09-30T12:00:00Z"
ENTRY = "import os; os.umask(0o022); from agentfare.cli import _entry; _entry()"


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def cli(tmp_path: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """The command line in its own process under umask 022; HOME and AGENTFARE_HOME are throwaway."""
    env = {**os.environ, "HOME": str(tmp_path / "h"), "AGENTFARE_NOW": NOW}
    env.pop("AGENTFARE_HOME", None)
    env.pop("AGENTFARE_CONFIG", None)
    return subprocess.run(
        [sys.executable, "-c", ENTRY, *args], capture_output=True, text=True, env=env, cwd=cwd or tmp_path, timeout=120, check=False
    )


class TestPrivateModes:
    def test_the_data_directory_is_0700_and_the_database_0600(self, tmp_path: Path) -> None:
        home = tmp_path / "fresh" / "data"
        done = cli(tmp_path, "--config", str(FIXTURE_CONFIG), "--home", str(home), "ingest")
        assert done.returncode == 0, done.stderr
        assert mode(home) == 0o700 and mode(home.parent) == 0o700  # both were created by us
        assert mode(home / "agentfare.db") == 0o600
        assert sorted(p.name for p in home.iterdir() if p.name != "agentfare.db") == []  # no sidecar left behind

    def test_the_journal_of_an_open_transaction_is_private_too(self, tmp_path: Path) -> None:
        script = (
            "import os, stat, sys\n"
            "os.umask(0o022)\n"
            "from agentfare.db import Store\n"
            "store = Store(sys.argv[1])\n"
            "store.conn.execute(\"INSERT INTO ingested_files VALUES ('p', 'claude', 's', 0)\")\n"  # opens a transaction: the journal exists until commit
            "for suffix in ('', '-journal'):\n"
            "    print(suffix or 'db', oct(stat.S_IMODE(os.stat(sys.argv[1] + suffix).st_mode)))\n"
        )
        done = subprocess.run([sys.executable, "-c", script, str(tmp_path / "d" / "x.db")], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert done.stdout.split("\n")[:2] == ["db 0o600", "-journal 0o600"]

    def test_a_directory_and_a_database_the_user_already_has_keep_their_modes(self, tmp_path: Path) -> None:
        home = tmp_path / "mine"
        home.mkdir()
        home.chmod(0o755)
        db = home / "agentfare.db"
        db.write_bytes(b"")
        db.chmod(0o644)
        done = cli(tmp_path, "--config", str(FIXTURE_CONFIG), "--home", str(home), "ingest")
        assert done.returncode == 0, done.stderr
        assert (mode(home), mode(db)) == (0o755, 0o644)

    def test_config_init_writes_a_private_file_in_a_private_directory(self, tmp_path: Path) -> None:
        home = tmp_path / "newhome"
        done = cli(tmp_path, "--home", str(home), "config", "--init")
        assert done.returncode == 0, done.stderr
        assert (mode(home), mode(home / "config.yaml")) == (0o700, 0o600)

    def test_a_report_file_is_private_by_default_name_and_with_o(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        work = tmp_path / "work"
        work.mkdir()
        base = ("--config", str(FIXTURE_CONFIG), "--home", str(home))
        done = cli(tmp_path, *base, "report", "--format", "html", "--week-of", "2026-W40", cwd=work)
        assert done.returncode == 0, done.stderr
        assert mode(work / "agentfare-report-2026-W40.html") == 0o600
        for name, args in (("named.md", ()), ("named.html", ("--format", "html")), ("named.json", ("--json",))):
            done = cli(tmp_path, *base, "report", *args, "-o", str(work / name), cwd=work)
            assert done.returncode == 0, done.stderr
            assert mode(work / name) == 0o600, name

    def test_a_report_overwritten_keeps_the_mode_it_had(self, tmp_path: Path) -> None:
        target = tmp_path / "shared.md"
        target.write_text("old")
        target.chmod(0o644)
        done = cli(tmp_path, "--config", str(FIXTURE_CONFIG), "--home", str(tmp_path / "home"), "report", "-o", str(target))
        assert done.returncode == 0, done.stderr
        assert mode(target) == 0o644 and target.read_text().startswith("# AgentFare weekly report")


@pytest.fixture
def invoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201
    monkeypatch.setenv("AGENTFARE_NOW", NOW)
    monkeypatch.chdir(tmp_path)

    def run(*args: str):  # noqa: ANN202
        return CliRunner().invoke(app, ["--config", str(FIXTURE_CONFIG), "--home", str(tmp_path / "home"), *args])

    return run


class TestReportFilesAndLinks:
    def test_the_default_report_name_is_not_written_through_a_planted_symlink(self, tmp_path: Path, invoke) -> None:  # noqa: ANN001
        victim = tmp_path / "victim.txt"
        victim.write_text("precious")
        (tmp_path / "agentfare-report-2026-W40.html").symlink_to(victim)
        result = invoke("report", "--format", "html", "--week-of", "2026-W40")
        assert result.exit_code == 2 and "symbolic link" in result.stderr
        assert victim.read_text() == "precious"

    def test_a_dangling_symlink_at_the_default_name_is_refused_and_its_target_not_created(self, tmp_path: Path, invoke) -> None:  # noqa: ANN001
        target = tmp_path / "not-there.txt"
        (tmp_path / "agentfare-report-2026-W40.html").symlink_to(target)
        result = invoke("report", "--format", "html", "--week-of", "2026-W40")
        assert result.exit_code == 2 and "symbolic link" in result.stderr
        assert not target.exists()

    def test_the_default_name_is_overwritten_when_it_is_the_last_runs_regular_file(self, tmp_path: Path, invoke) -> None:  # noqa: ANN001
        page = tmp_path / "agentfare-report-2026-W40.html"
        page.write_text("last run")
        result = invoke("report", "--format", "html", "--week-of", "2026-W40")
        assert result.exit_code == 0, result.stderr
        assert page.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_a_dangling_symlink_given_with_o_is_refused_with_a_clear_error(self, tmp_path: Path, invoke) -> None:  # noqa: ANN001
        target = tmp_path / "gone" / "x.md"
        link = tmp_path / "out.md"
        link.symlink_to(target)
        result = invoke("report", "-o", str(link))
        assert result.exit_code == 2 and "symbolic link" in result.stderr and "does not exist" in result.stderr
        assert not target.parent.exists()

    def test_a_symlink_to_an_existing_file_given_with_o_is_written_as_before(self, tmp_path: Path, invoke) -> None:  # noqa: ANN001
        real = tmp_path / "real.md"
        real.write_text("old")
        link = tmp_path / "out.md"
        link.symlink_to(real)
        result = invoke("report", "-o", str(link))
        assert result.exit_code == 0, result.stderr
        assert real.read_text(encoding="utf-8").startswith("# AgentFare weekly report") and link.is_symlink()


class TestConfigInit:
    def test_init_force_overwrites_a_config_that_is_not_valid_yaml(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text("timezone: [unclosed\n  - : :\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["--home", str(home), "config", "--init", "--force"])
        assert result.exit_code == 0, result.stderr
        assert (home / "config.yaml").read_text(encoding="utf-8").startswith("# AgentFare configuration")
        assert CliRunner().invoke(app, ["--home", str(home), "--no-ingest", "config"]).exit_code == 0  # and it loads again

    @pytest.mark.parametrize("body", ["- just\n- a list\n", "42\n", "\x00\x01 binary"])
    def test_init_force_overwrites_a_config_that_is_not_a_mapping(self, tmp_path: Path, body: str) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text(body, encoding="utf-8")
        assert CliRunner().invoke(app, ["--home", str(home), "config", "--init", "--force"]).exit_code == 0
        assert (home / "config.yaml").read_text(encoding="utf-8").startswith("# AgentFare configuration")

    def test_plain_init_still_refuses_to_overwrite_and_leaves_the_file_alone(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text("timezone: [unclosed\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["--home", str(home), "config", "--init"])
        assert result.exit_code == 2 and "already exists" in result.stderr and "--force" in result.stderr
        assert (home / "config.yaml").read_text(encoding="utf-8") == "timezone: [unclosed\n"

    def test_init_force_with_config_overwrites_that_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "mine.yaml"
        bad.write_text("{{{", encoding="utf-8")
        result = CliRunner().invoke(app, ["--config", str(bad), "--home", str(tmp_path / "home"), "config", "--init", "--force"])
        assert result.exit_code == 0, result.stderr
        assert bad.read_text(encoding="utf-8").startswith("# AgentFare configuration") and not (tmp_path / "home" / "config.yaml").exists()

    def test_init_without_a_config_file_still_writes_to_the_data_directory(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        result = CliRunner().invoke(app, ["--home", str(home), "config", "--init"])
        assert result.exit_code == 0 and (home / "config.yaml").is_file()

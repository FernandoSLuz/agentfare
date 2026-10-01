"""`agentfare demo`: the product run on the packaged synthetic history, without touching the user's own logs or ~/.agentfare.

The command runs as a subprocess here, with a throwaway HOME and a clean environment, so that what it reads and writes can be
seen from outside: no ~/.agentfare, nothing left behind, and nothing taken from the agent logs the environment points at.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import agentfare
from agentfare.cli import app

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path(agentfare.__file__).parent
DEMO_DATA = PACKAGE / "demo_data"
NOW = "2026-09-30T18:00:00Z"
SPEND_UCENTS, SAVED_UCENTS, ROUTER_UCENTS = 19_396_450_000, 4_154_800_000, 15_241_650_000  # scripts/make_demo_data.py DOCUMENTED
ANSI = re.compile(r"\x1b\[")


def clean_env(home: Path, **extra: str) -> dict[str, str]:
    """The environment of a person who has agent logs of their own and an AgentFare of their own: none of it may matter."""
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "SYSTEMROOT", "LANG", "LC_ALL") if key in os.environ}
    env.update(HOME=str(home), TMPDIR=str(home / "tmp"), PYTHONDONTWRITEBYTECODE="1", COLUMNS="100", **extra)
    return env


def demo(tmp_path: Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, "-m", "agentfare", "demo", *args], capture_output=True, text=True, timeout=120,
                          env=env if env is not None else clean_env(home), cwd=cwd or tmp_path)


def listing(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


class TestRunsOnTheDemoData:
    def test_it_prints_the_banner_the_summary_the_top_three_pieces_of_advice_and_what_to_try_next(self, tmp_path: Path) -> None:
        result = demo(tmp_path)
        assert result.returncode == 0, result.stderr
        out = result.stdout
        first = out.splitlines()[0]
        assert "synthetic" in first.lower() and "demo" in first.lower() and len(first) <= 80  # one line
        assert "AgentFare summary: all time" in out
        assert "45 sessions, 5 projects, 8 models, total spend $193.96+" in out
        assert "AgentFare advise: all time" in out and "Router savings (per ISO week)" in out
        advice = re.findall(r"^\s*(\d+)\. session ", out, re.M)
        assert advice == ["1", "2", "3"], "the top 3 pieces of advice"
        assert "codex-auto-review" in out  # the model with no price is named in the warning
        for command in ("agentfare summary", "agentfare advise", "agentfare run --dry-run --", "agentfare report --format html -o"):
            assert command in out.split("Next", 1)[1], command
        assert "--config" in out and "--keep" in out
        assert not ANSI.search(out) and not ANSI.search(result.stderr)
        assert result.stderr == "", result.stderr  # not even the "first run" note of an ingest

    def test_it_creates_nothing_in_the_home_directory_and_cleans_up_after_itself(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / "tmp").mkdir(parents=True)
        before = listing(home)
        result = demo(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not (home / ".agentfare").exists()
        assert listing(home) == before, "the demo's temporary directory is gone"
        assert listing(tmp_path) == ["home", "home/tmp"]

    def test_it_ignores_the_agent_logs_and_the_agentfare_setup_of_the_person_running_it(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        mine = home / ".claude" / "projects" / "-home-dev-secret-project"
        mine.mkdir(parents=True)
        (mine / "5d3c8f1e-0000-4000-8000-000000000001.jsonl").write_text(json.dumps({
            "type": "assistant", "timestamp": "2026-09-29T10:00:00.000Z", "cwd": "/home/dev/secret-project",
            "sessionId": "5d3c8f1e-0000-4000-8000-000000000001",
            "message": {"id": "m1", "model": "claude-opus-5-5", "content": [], "usage": {"input_tokens": 9_000_000, "output_tokens": 9_000_000}}}) + "\n")
        (home / ".agentfare").mkdir()
        (home / ".agentfare" / "config.yaml").write_text("timezone: Asia/Tokyo\nsources: {claude: [/nonexistent]}\n")
        (home / ".agentfare" / "pricing.yaml").write_text("models:\n  claude-opus-5-5: {input: 999, output: 999, source: x}\n")
        (home / "tmp").mkdir()
        env = clean_env(home, CLAUDE_CONFIG_DIR=str(home / ".claude"), AGENTFARE_NOW="2020-01-01T00:00:00Z",
                        AGENTFARE_CONFIG=str(home / ".agentfare" / "config.yaml"), AGENTFARE_HOME=str(home / ".agentfare"))
        result = demo(tmp_path, "--json", env=env)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["total"]["ucents"] == SPEND_UCENTS and data["sessions"] == 45
        assert not (home / ".agentfare" / "agentfare.db").exists()
        assert sorted(p.name for p in (home / ".agentfare").iterdir()) == ["config.yaml", "pricing.yaml"]
        assert "secret-project" not in result.stdout + result.stderr

    def test_the_global_options_do_not_point_it_at_something_else(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        result = subprocess.run([sys.executable, "-m", "agentfare", "--config", str(tmp_path / "no.yaml"), "--home", str(elsewhere), "demo", "--json"],
                                capture_output=True, text=True, timeout=120, env=clean_env(tmp_path), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["total"]["ucents"] == SPEND_UCENTS
        assert not elsewhere.exists()

    def test_it_does_not_touch_the_clock_or_the_environment_of_the_process_that_runs_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setattr(tempfile, "tempdir", None)
        before = dict(os.environ)
        result = CliRunner().invoke(app, ["demo", "--json"])
        assert result.exit_code == 0, result.output
        assert dict(os.environ) == before and "AGENTFARE_NOW" not in os.environ
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith("agentfare")] == []


class TestJson:
    def test_it_prints_totals_and_savings_and_nothing_else(self, tmp_path: Path) -> None:
        result = demo(tmp_path, "--json")
        assert result.returncode == 0 and result.stderr == "", result.stderr
        data: dict[str, Any] = json.loads(result.stdout)
        assert data["synthetic"] is True and data["now"] == NOW
        assert (data["sessions"], data["projects"], data["models"]) == (45, 5, 8)
        assert data["total"] == {"cents": SPEND_UCENTS // 1_000_000, "ucents": SPEND_UCENTS}
        savings = data["savings"]
        assert (savings["actual"]["ucents"], savings["router"]["ucents"], savings["saved"]["ucents"]) == (SPEND_UCENTS, ROUTER_UCENTS, SAVED_UCENTS)
        assert savings["saved_bp"] == 2142 and savings["switched"] == 14 and savings["sessions"] == 43
        assert savings["prompted"]["ucents"] + savings["background"]["ucents"] == SAVED_UCENTS
        assert [w["week"] for w in data["weeks"]] == ["2026-W39", "2026-W40"]
        assert [a["short_id"] for a in data["top_advice"]] and len(data["top_advice"]) == 3
        assert {"line", "saved", "actual", "recommended", "recommended_model", "agent", "project", "model"} <= set(data["top_advice"][0])
        assert [w["model"] for w in data["warnings"]] == ["codex-auto-review"]
        assert data["kept"] is None and data["report"] is None

    def test_the_keys_are_the_same_in_every_run(self, tmp_path: Path) -> None:
        assert demo(tmp_path, "--json").stdout == demo(tmp_path, "--json").stdout


class TestKeep:
    def test_it_keeps_the_demo_and_prints_the_flags_to_use_it(self, tmp_path: Path) -> None:
        kept = tmp_path / "kept"
        result = demo(tmp_path, "--keep", str(kept))
        assert result.returncode == 0, result.stderr
        assert (kept / "demo_data" / "config.yaml").is_file() and (kept / "home" / "agentfare.db").is_file()
        assert str(kept) in result.stdout
        commands = [line.strip() for line in result.stdout.splitlines() if "--config" in line and "--home" in line]
        assert len(commands) >= 4
        for line in commands:
            argv = shlex.split(line)
            assert argv[0] == "agentfare" and f"{kept}/demo_data/config.yaml" in argv and f"{kept}/home" in argv
            run = subprocess.run([sys.executable, "-m", "agentfare", *argv[1:]], capture_output=True, text=True, timeout=120,
                                 env=clean_env(tmp_path / "home", AGENTFARE_NOW=NOW), cwd=tmp_path)
            assert run.returncode == 0, (line, run.stderr)
        assert not (tmp_path / "home" / ".agentfare").exists()

    def test_what_is_kept_answers_like_the_demo_did(self, tmp_path: Path) -> None:
        kept = tmp_path / "kept"
        assert demo(tmp_path, "--keep", str(kept)).returncode == 0
        run = subprocess.run([sys.executable, "-m", "agentfare", "--config", str(kept / "demo_data" / "config.yaml"), "--home", str(kept / "home"),
                              "--no-ingest", "summary", "--json"], capture_output=True, text=True, timeout=120,
                             env=clean_env(tmp_path / "home", AGENTFARE_NOW=NOW))
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout)["total"]["ucents"] == SPEND_UCENTS

    def test_the_demo_json_names_where_it_was_kept(self, tmp_path: Path) -> None:
        kept = tmp_path / "kept"
        data = json.loads(demo(tmp_path, "--keep", str(kept), "--json").stdout)
        assert data["kept"] == {"dir": str(kept), "config": str(kept / "demo_data" / "config.yaml"), "home": str(kept / "home")}

    def test_it_never_writes_into_a_directory_that_is_not_empty(self, tmp_path: Path) -> None:
        full = tmp_path / "full"
        full.mkdir()
        (full / "mine.txt").write_text("keep me")
        result = demo(tmp_path, "--keep", str(full))
        assert result.returncode == 2 and result.stdout == ""
        assert result.stderr.startswith("error: ") and result.stderr.count("\n") == 1 and "not empty" in result.stderr
        assert listing(full) == ["mine.txt"] and (full / "mine.txt").read_text() == "keep me"

    def test_an_empty_directory_is_fine(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert demo(tmp_path, "--keep", str(empty)).returncode == 0
        assert (empty / "home" / "agentfare.db").is_file()


class TestReport:
    def test_it_writes_the_html_report_of_the_demo_week(self, tmp_path: Path) -> None:
        result = demo(tmp_path, "--report", "week.html")
        assert result.returncode == 0, result.stderr
        page = (tmp_path / "week.html").read_text(encoding="utf-8")
        assert "AgentFare" in page and "2026-W40" in page and "$51.05" in page
        assert "<script" not in page and "http://" not in page and "https://" not in page
        assert "week.html" in result.stdout

    def test_the_json_names_the_report(self, tmp_path: Path) -> None:
        target = tmp_path / "out" / "r.html"
        target.parent.mkdir()
        data = json.loads(demo(tmp_path, "--json", "--report", str(target)).stdout)
        assert data["report"] == str(target) and target.is_file()

    def test_a_report_it_cannot_write_is_one_error_line(self, tmp_path: Path) -> None:
        result = demo(tmp_path, "--report", str(tmp_path / "missing" / "dir" / "r.html"))
        assert result.returncode == 2 and result.stderr.startswith("error: ")


class TestHelpAndPackaging:
    def test_the_command_is_listed_and_documents_its_options(self) -> None:
        def flat(*args: str) -> str:
            """The help page without its box and its line breaks."""
            output = CliRunner().invoke(app, [*args, "--help"], env={"COLUMNS": "100"}).output
            return " ".join(re.sub(r"[│╭╮╰╯─]", " ", output).split())

        assert re.search(r"\bdemo Run AgentFare on invented sessions", flat())
        text = flat("demo")
        for option in ("--keep DIR", "--report FILE", "--json"):
            assert option in text
        assert "synthetic" in text.lower() and "own agent logs" in text.lower() and "~/.agentfare" in text

    def test_the_demo_files_are_inside_the_package_and_in_its_package_data(self) -> None:
        patterns = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]["agentfare"]
        covered = {Path(p).relative_to(PACKAGE).as_posix() for pattern in patterns for p in glob.glob(str(PACKAGE / pattern), recursive=True)}
        shipped = {p.relative_to(PACKAGE).as_posix() for p in DEMO_DATA.rglob("*") if p.is_file()}
        assert shipped and not (shipped - covered), sorted(shipped - covered)  # a dot file such as .project_root needs its own pattern


def test_a_closed_output_pipe_ends_quietly(tmp_path: Path) -> None:
    """`agentfare demo | head -3` is a normal thing to type: exit 0, nothing on stderr (it used to exit 1)."""
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.Popen([sys.executable, "-m", "agentfare", "demo"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=clean_env(home))
    assert proc.stdout is not None
    for _ in range(3):
        proc.stdout.readline()
    proc.stdout.close()  # the reader is gone, as `head` would be
    err = proc.stderr.read() if proc.stderr else ""
    assert proc.wait(timeout=120) == 0
    assert "Traceback" not in err and "BrokenPipe" not in err

"""A named pipe (or a link to one) among the logs must not hang a scan: only regular files are read.

The scan runs in a child process with a timeout, so a parser that blocks on a pipe fails the test instead of hanging it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCAN = """
import sys
from pathlib import Path
from agentfare.parsers import get_parser

parser = get_parser(sys.argv[1])
files = sorted(parser.discover(Path(sys.argv[2])))
records = 0
for path in files:
    parser.signature(path)
    records += len(parser.parse(path))
print(len(files), records)
"""


def scan(agent: str, root: Path, timeout: float = 15) -> tuple[int, int]:
    try:
        done = subprocess.run([sys.executable, "-c", SCAN, agent, str(root)], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail(f"scanning {agent} logs hung for {timeout} s: a special file was opened")
    assert done.returncode == 0, done.stderr
    files, records = done.stdout.split()
    return int(files), int(records)


def fifo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    return path


@pytest.fixture
def claude_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES / "claude" / "projects", root)
    return root


def test_the_claude_fixture_scans_quickly_to_one_session(claude_root: Path) -> None:
    assert scan("claude", claude_root) == (1, 1)


def test_a_fifo_named_like_a_claude_session_is_skipped(claude_root: Path) -> None:
    project = next(claude_root.iterdir())
    fifo(project / "deadbeef-0000-4000-8000-000000000000.jsonl")
    assert scan("claude", claude_root) == (1, 1)


def test_a_symlink_to_a_fifo_is_skipped_too(claude_root: Path, tmp_path: Path) -> None:
    pipe = fifo(tmp_path / "pipe")
    project = next(claude_root.iterdir())
    (project / "deadbeef-0000-4000-8000-000000000001.jsonl").symlink_to(pipe)
    assert scan("claude", claude_root) == (1, 1)


def test_a_fifo_among_the_sub_agent_transcripts_is_skipped(claude_root: Path) -> None:
    session = next(p for p in next(claude_root.iterdir()).iterdir() if p.is_dir())
    fifo(session / "subagents" / "agent-0000000000000000.jsonl")
    fifo(session / "subagents" / "workflows" / "wf_demo-001" / "agent-1111111111111111.jsonl")
    assert scan("claude", claude_root) == (1, 1)


def test_a_fifo_in_a_copy_of_the_session_in_another_project_folder_is_skipped(claude_root: Path) -> None:
    project = next(claude_root.iterdir())
    session = next(p for p in project.iterdir() if p.is_dir())
    other = claude_root / "-home-dev-other"
    fifo(other / session.name / "subagents" / "agent-2222222222222222.jsonl")
    fifo(other / f"{session.name}.jsonl")
    assert scan("claude", claude_root) == (1, 1)


def test_a_real_sub_agent_file_behind_a_symlink_is_still_read(claude_root: Path, tmp_path: Path) -> None:
    session = next(p for p in next(claude_root.iterdir()).iterdir() if p.is_dir())
    real = next((session / "subagents").glob("agent-*.jsonl"))
    kept = tmp_path / "kept.jsonl"
    shutil.copy(real, kept)
    (session / "subagents" / "agent-3333333333333333.jsonl").symlink_to(kept)
    assert scan("claude", claude_root) == (1, 1)


@pytest.fixture
def gemini_root(tmp_path: Path) -> Path:
    root = tmp_path / "tmp"
    shutil.copytree(FIXTURES / "gemini" / "tmp", root)
    return root


def test_the_gemini_fixture_scans_quickly_to_one_session(gemini_root: Path) -> None:
    assert scan("gemini", gemini_root) == (1, 1)


def test_a_fifo_named_like_a_gemini_session_is_skipped(gemini_root: Path, tmp_path: Path) -> None:
    chats = next(gemini_root.iterdir()) / "chats"
    fifo(chats / "session-2026-09-30T10-00-aaaaaaaa.jsonl")
    fifo(chats / "session-2026-09-30T10-00-bbbbbbbb.json")
    (chats / "session-2026-09-30T10-00-cccccccc.jsonl").symlink_to(fifo(tmp_path / "pipe"))
    assert scan("gemini", gemini_root) == (1, 1)


def test_a_fifo_among_the_gemini_sub_agent_files_is_skipped(gemini_root: Path) -> None:
    chats = next(gemini_root.iterdir()) / "chats"
    fifo(chats / "9f3c2a71-5b8e-4d0a-9c1e-2b7d4a6e8f10" / "sub.jsonl")  # chats/<sessionId>/*.jsonl of the fixture's session
    assert scan("gemini", gemini_root) == (1, 1)


def test_a_fifo_named_like_a_codex_rollout_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    shutil.copytree(FIXTURES / "codex" / "sessions", root)
    fifo(root / "2026" / "09" / "29" / "rollout-2026-09-29T11-00-00-01a00000-0000-7000-8000-000000000009.jsonl")
    assert scan("codex", root) == (1, 1)

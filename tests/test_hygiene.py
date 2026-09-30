"""Nothing taken from the machine the project was written on may be committed: no user name, no home directory, no real
session id. The names that are allowed in examples are the invented ones below."""

from __future__ import annotations

import getpass
import os
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist", "node_modules"}
HOME_PLACEHOLDERS = {"dev", "tester", "demo"}  # /home/dev, /home/tester, /home/demo and C:\Users\dev; code lives under /srv/code


def tree() -> Iterator[tuple[Path, list[str]]]:
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in files:
            path = Path(directory, name)
            if path.is_symlink():
                continue
            try:
                yield path, path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue  # a binary file


def hits(pattern: re.Pattern[str], allowed: set[str] = frozenset()) -> list[str]:  # type: ignore[assignment]
    """``file:line`` of every line where ``pattern`` matches with a first group that is not allowed. Never the text itself."""
    found = []
    for path, lines in tree():
        for number, line in enumerate(lines, 1):
            if any(match.group(1) not in allowed for match in pattern.finditer(line)):
                found.append(f"{path.relative_to(ROOT)}:{number}")
    return found


def test_only_the_invented_home_directories_appear_in_paths() -> None:
    posix = re.compile(r"/home/([A-Za-z0-9_.][A-Za-z0-9_.-]*)")
    windows = re.compile(r"[A-Za-z]:\\+Users\\+([A-Za-z0-9_.][A-Za-z0-9_.-]*)")
    mac = re.compile(r"/Users/([A-Za-z0-9_.][A-Za-z0-9_.-]*)")
    assert hits(posix, HOME_PLACEHOLDERS) == []
    assert hits(windows, HOME_PLACEHOLDERS) == []
    assert hits(mac, HOME_PLACEHOLDERS) == []


def test_the_user_of_this_machine_and_their_home_are_not_in_the_tree() -> None:
    user, home = getpass.getuser(), str(Path.home())
    needles = [re.escape(home)] if len(home) > len("/root") else []
    if len(user) >= 5 and user not in HOME_PLACEHOLDERS:
        needles.append(rf"(?:/home/|/Users/|\\Users\\|~){re.escape(user)}(?![A-Za-z0-9_])")
    if needles:
        pattern = re.compile("(" + "|".join(needles) + ")")
        assert hits(pattern) == [], "this machine's user name or home directory is committed"

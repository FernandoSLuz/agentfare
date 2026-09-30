"""Files AgentFare creates: private to the user, and never written through a link somebody else planted. Files the user
names (a config, a price table, a prompt file) are read only when they are regular files of a sane size.

The data directory holds prompts' metadata (project names, token counts, paths), so what we *create* is made for the
owner alone (directories 0700, files 0600, whatever the umask says). What the user already has is never changed: an
existing directory or file keeps its mode, and an existing file that is replaced keeps the mode it had.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path

PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600

MAX_CONFIG_BYTES = 10 * 1024 * 1024  # a config file or a price table: the real ones are a few kilobytes
MAX_PROMPT_BYTES = 2 * 1024 * 1024  # a --prompt-file


def make_private_dirs(path: Path | str) -> None:
    """``mkdir -p``, except that every directory created here is 0700; one that already exists is left as it is."""
    missing: list[Path] = []
    here = Path(path)
    while not here.exists():
        missing.append(here)
        if here.parent == here:
            break
        here = here.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, PRIVATE_DIR)
        except FileExistsError:  # made by somebody else meanwhile, which is fine; a dangling link or a file is not
            if not directory.is_dir():
                raise


def touch_private(path: Path | str) -> None:
    """Create ``path`` (0600) when it does not exist. Do it before SQLite opens the file: its ``-journal`` and ``-wal``
    files are created with the mode of the database, so they are private too."""
    try:
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, PRIVATE_FILE))
    except PermissionError:
        if not os.path.exists(path):  # a file that exists but is not writable is the user's business (SQLite decides)
            raise


def write_text_atomic(target: Path, text: str) -> None:
    """Replace ``target`` with ``text`` through a fresh temporary file in the same directory.

    The temporary name is chosen by ``mkstemp`` (``O_EXCL``, 0600), so a link planted at a predictable name leads nowhere.
    A file that already exists keeps its mode; a new one is 0600. ``target`` itself is written as given: pass the
    resolved path to update where a symlink points and leave the link in place."""
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                mode = stat.S_IMODE(target.stat().st_mode)
            except FileNotFoundError:
                mode = PRIVATE_FILE
            os.fchmod(fh.fileno(), mode)
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


class NotWritableError(OSError):
    """A report target that is refused on purpose, with the reason in the message."""


def write_new_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, a name AgentFare chose (the default report name in the current directory).

    A new file is created with ``O_CREAT | O_EXCL | O_NOFOLLOW`` and mode 0600. A regular file that is already there (last
    run's report) is overwritten and keeps its mode; a symbolic link or anything else is refused."""
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, PRIVATE_FILE)
    except FileExistsError:
        try:
            fd = os.open(path, flags)  # no O_CREAT: nothing can be created through a link, and O_NOFOLLOW refuses one
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise NotWritableError(f"{path} is a symbolic link: refusing to write through it") from exc
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise NotWritableError(f"{path} exists and is not a regular file: refusing to write to it") from None
        os.ftruncate(fd, 0)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_chosen_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, a file the user named with ``-o``: created 0600 when new, overwritten as it is (keeping
    its mode) when it exists, a link to a file followed. A link that leads nowhere is refused instead of creating its target."""
    if os.path.islink(path) and not os.path.exists(path):
        raise NotWritableError(f"{path} is a symbolic link to a file that does not exist: refusing to create its target")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


class NotReadableError(OSError):
    """A file the user named that is refused on purpose (not a regular file, too large), with the reason as the message."""


def read_text_limited(path: Path | str, limit: int, *, errors: str = "strict") -> str:
    """The UTF-8 text of ``path``, a file the user named, read only when it is a regular file of at most ``limit`` bytes.

    A FIFO, a device (``/dev/zero`` would never end) or a directory is refused before a byte is read, and the file is opened
    without blocking, so a FIFO nobody writes to cannot hold the command either. The size is checked twice: from the file's
    metadata, and again on what was read, in case it grows. Raises ``NotReadableError`` (an ``OSError``) for what is refused,
    and the usual ``OSError`` for a file that is missing or not allowed; a decoding error is a ``UnicodeDecodeError``."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise NotReadableError("not a regular file (a directory, a pipe or a device)")
        if info.st_size > limit:
            raise NotReadableError(f"larger than {limit // 2**20} MB")
        with os.fdopen(fd, "r", encoding="utf-8", errors=errors) as fh:  # takes over the descriptor, and closes it
            fd = -1
            text = fh.read(limit + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(text) > limit:
        raise NotReadableError(f"larger than {limit // 2**20} MB")
    return text

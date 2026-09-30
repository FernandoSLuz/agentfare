"""Text from outside (a log file name, a server reply, an exception message) made safe to print."""

from __future__ import annotations

import re

# Control characters (terminal escapes among them), line and paragraph separators and the bidirectional overrides that
# make text read backwards: a project or model name is data from the logs, and none of these may reach the terminal.
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def clean_text(text: str) -> str:
    """``text`` with every unsafe character replaced by ``?``: a name from the logs, safe for a terminal or a page."""
    return _UNSAFE.sub("?", text)


def clean_lines(text: str) -> str:
    """``clean_text`` for a message that is meant to have several lines (a YAML error points at the line it found): the
    newlines stay, everything else that is unsafe becomes ``?``."""
    return "\n".join(clean_text(line) for line in text.split("\n"))

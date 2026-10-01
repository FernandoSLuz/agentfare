#!/usr/bin/env python3
"""Builds every image of the README and the repository page, from the synthetic demo data and nothing else.

    python scripts/make_assets.py                         # every image, into docs/images/
    python scripts/make_assets.py --only summary advise   # only these (names below, or --list)
    python scripts/make_assets.py --list                  # the names and the files they make

The terminal screenshots and the GIF show the real output of the real commands, run on src/agentfare/demo_data (45
invented sessions, see scripts/make_demo_data.py) in a scratch directory with the clock frozen at the demo's, so that every
run draws the same pictures. Nothing is read from your own agent logs or from ~/.agentfare, and every path that could name
this machine is replaced before it is drawn (the script stops if one is left).

The two pictures of the setup, `init` and `doctor`, and the first scene of the GIF are drawn differently, because they show what
a person sees right after installing: AgentFare installed with `uv tool install` into a scratch home, the invented logs in the
default folders of that home (`~/.claude/projects`, `~/.codex/sessions`, `~/.gemini/tmp`), stand-ins for the three agents in
`~/.local/bin`, no data directory yet, no config file and the real clock. Their `Shot` has `own_home=True`, and each of them runs
on a home that has never run AgentFare (so `doctor` shows a machine that is ready and has not read anything yet). They need `uv`
(and the network or a warm uv cache, for typer, rich, PyYAML and Jinja2).

Development-only requirements (none of them is a dependency of agentfare, and none is needed to use it):

    chromium or google-chrome   the PNGs: terminal windows, hero, social preview, the report in both themes and on a phone
    uv                          the init and doctor pictures, which run an installed copy (uv tool install into a scratch home)
    Pillow                      the frames of demo.gif (pip install pillow)
    ffmpeg                      assembles demo.gif with a palette of its own
    rsvg-convert                how-it-works.png from how-it-works.svg (librsvg; ImageMagick's `magick` is the fallback)
    a monospace font            JetBrains Mono, else DejaVu Sans Mono, Liberation Mono or Noto Sans Mono (any one is found with fc-match)

Add a terminal screenshot: one line in SHOTS below (a name and the words typed after `agentfare`), then run
`python scripts/make_assets.py --only <name>`. tests/test_assets.py reads SHOTS and OUTPUTS, so the new file is expected on
disk, under 2.5 MB, and 1000 pixels wide.
"""

from __future__ import annotations

import argparse
import getpass
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "docs" / "images"
DEMO_DATA = ROOT / "src" / "agentfare" / "demo_data"
NOW = "2026-09-30T18:00:00Z"  # the clock of `agentfare demo`
COLUMNS = 100  # the width the commands format for; the windows are drawn for exactly this many characters
MAX_BYTES = 2_500_000

# ---- what to draw ----------------------------------------------------------------------------------------------------------
# One line per terminal screenshot: the file docs/images/<name>.png, the words typed after `agentfare`, and (when the output
# is longer than a screenshot should be) how many lines of it to show. The shot runs on the demo data, from an empty directory.


@dataclass(frozen=True)
class Shot:
    name: str
    argv: tuple[str, ...]
    lines: int | None = None
    own_home: bool = False  # run as an installed copy in the scratch home of a new user (see the top of this file)


SHOTS = (
    Shot("summary", ("summary",), lines=30),
    Shot("models", ("models",)),
    Shot("advise", ("advise", "--limit", "3")),
    Shot("run", ("run", "--dry-run", "--", "claude", "-p", "refactor the auth module and migrate the database")),
    Shot("run-light", ("run", "--dry-run", "--", "codex", "exec", "fix the typo in the README")),
    Shot("sessions", ("sessions", "-n", "14")),
    Shot("prices", ("prices",)),
    Shot("init", ("init",), own_home=True),
    Shot("doctor", ("doctor",), own_home=True),
)


@dataclass(frozen=True)
class Scene:
    """One step of demo.gif: the command is typed, its real output appears, and the picture rests for ``pause`` seconds."""

    argv: tuple[str, ...]
    lines: int | None
    pause: float
    pipe: tuple[tuple[str, ...], ...] = ()  # commands the output goes through, as typed after a `|`
    own_home: bool = False  # as Shot.own_home


GIF_SCENES = (
    Scene(("init",), 31, 3.0, own_home=True),
    Scene(("summary",), 30, 1.3),
    Scene(("advise", "--limit", "3"), 29, 1.7),
    Scene(("run", "--dry-run", "--", "claude", "-p", "refactor the auth module and migrate the database"), None, 1.9),
)

# Every asset: its name for --only, and the files in docs/images/ it writes. tests/test_assets.py compares this with the folder.
OUTPUTS: dict[str, tuple[str, ...]] = {
    "hero": ("hero.png",),
    "social-preview": ("social-preview.png",),
    **{shot.name: (f"{shot.name}.png",) for shot in SHOTS},
    "report-dark": ("report-dark.png",),
    "report-light": ("report-light.png",),
    "report-mobile": ("report-mobile.png",),
    "how-it-works": ("how-it-works.svg", "how-it-works.png"),
    "demo": ("demo.gif",),
}
TAGLINE = ("Know what your AI coding agents cost.", "Route the routine work to cheaper models.")

# ---- the look --------------------------------------------------------------------------------------------------------------
BG, BAR, EDGE, FG, MUTED = "#0d1117", "#161b22", "#30363d", "#e6edf3", "#8b949e"
BLUE, GREEN, AMBER = "#3987e5", "#199e70", "#fab219"
PALETTE = {  # the 16 terminal colours, in the colours of a dark GitHub terminal
    30: "#6e7681", 31: "#ff7b72", 32: "#3fb950", 33: "#e3b341", 34: "#79c0ff", 35: "#d2a8ff", 36: "#56d4dd", 37: "#e6edf3",
    90: "#8b949e", 91: "#ffa198", 92: "#56d364", 93: "#f2cc60", 94: "#a5d6ff", 95: "#e2c5ff", 96: "#76e3ea", 97: "#ffffff",
}
DIM = 0.6  # how much of its colour a dim run keeps
MONO_CSS = "'JetBrains Mono', 'JetBrainsMono Nerd Font', 'DejaVu Sans Mono', 'Liberation Mono', 'Noto Sans Mono', monospace"
SANS_CSS = "Inter, 'Adwaita Sans', 'Noto Sans', 'Liberation Sans', 'Helvetica Neue', Arial, sans-serif"
MARK = (  # the AgentFare mark: docs/brand/logo-mark.svg, inlined (no glow filter, so it stays crisp at small sizes)
    '<svg width="{s}" height="{s}" viewBox="0 0 512 512" role="img" aria-label="AgentFare">'
    '<defs><linearGradient id="afbg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0f172a"/><stop offset="1" stop-color="#27215c"/></linearGradient><linearGradient id="afgo" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#2dd4bf"/><stop offset="1" stop-color="#4ade80"/></linearGradient></defs><rect width="512" height="512" rx="116" fill="url(#afbg)"/><rect x="3" y="3" width="506" height="506" rx="113" fill="none" stroke="#fff" stroke-opacity=".14" stroke-width="2"/><path d="M104 256H196" stroke="#cbd5e1" stroke-width="22" stroke-linecap="round" fill="none"/><path d="M196 256C252 256 262 160 320 160H372" stroke="#64748b" stroke-width="16" stroke-linecap="round" fill="none"/><circle cx="404" cy="160" r="22" fill="none" stroke="#64748b" stroke-width="12"/><path d="M196 256C252 256 262 352 320 352H372" stroke="url(#afgo)" stroke-width="26" stroke-linecap="round" fill="none"/><circle cx="404" cy="352" r="30" fill="url(#afgo)"/><circle cx="104" cy="256" r="24" fill="#e2e8f0"/>'
    '</svg>'
)


def png_width(path: Path) -> int:
    with path.open("rb") as fh:
        return int.from_bytes(fh.read(24)[16:20], "big")


def say(text: str) -> None:
    print(text, flush=True)


def need(program: str, *alternatives: str, why: str) -> str:
    for name in (program, *alternatives):
        if found := shutil.which(name):
            return found
    raise SystemExit(f"error: {program} is needed for {why} and was not found (see the requirements at the top of this script)")


def dollars(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


# ---- the scratch workspace: demo data, a database and a clock ------------------------------------------------------------
class Workspace:
    """The demo data copied to a scratch directory and read into a database there, and the way to run agentfare on them: the
    clock frozen at the demo's, a throwaway HOME, a fixed width, and stand-ins for the agents that `agentfare run` looks for."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="agentfare-assets-"))
        self.data = self.root / "demo_data"
        shutil.copytree(DEMO_DATA, self.data)
        self.work = self.root / "demo"  # the directory the commands run in: `run` counts its files, so it stays empty
        self.work.mkdir()
        (self.root / "user").mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for agent in ("claude", "codex", "gemini"):
            stub = bin_dir / agent
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(self.root / "user"),
            "LANG": "C.UTF-8", "TERM": "xterm-256color", "COLUMNS": str(COLUMNS), "AGENTFARE_NOW": NOW,
            "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.person: Path | None = None  # the home of the installed copy, made the first time a shot asks for it
        self.run("ingest")

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def run(self, *argv: str, colour: bool = False) -> str:
        """``agentfare <argv>`` on the demo data; its standard output, with the paths of this machine replaced."""
        env = {**self.env, **({"FORCE_COLOR": "1"} if colour else {})}
        command = [sys.executable, "-m", "agentfare", "--config", str(self.data / "config.yaml"), "--home", str(self.root / "home"), *argv]
        done = subprocess.run(command, cwd=self.work, env=env, capture_output=True, text=True, timeout=120)
        if done.returncode:
            raise SystemExit(f"error: `agentfare {shlex.join(argv)}` failed ({done.returncode}): {done.stderr.strip()}")
        return self.scrub(done.stdout)

    def install_for_a_person(self) -> Path:
        """The home of a new user who installed AgentFare with ``uv tool install``: the invented logs in the default folders of
        that home, the three agents (stand-ins) in ~/.local/bin and the real ``agentfare`` there too. Made once, on first use.
        The package is installed from a copy of the sources, so that nothing is built inside the checkout."""
        if self.person:
            return self.person
        uv = need("uv", why="the init and doctor pictures (they run an installed copy)")
        home = self.root / "person"
        for agent, folder in (("claude", ".claude/projects"), ("codex", ".codex/sessions"), ("gemini", ".gemini/tmp")):
            shutil.copytree(self.data / agent / Path(folder).name, home / folder)
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for agent in ("claude", "codex", "gemini"):
            stub = bin_dir / agent
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        source = self.root / "install-source"
        source.mkdir()
        for name in ("src", "pyproject.toml", "README.md", "LICENSE"):
            (shutil.copytree if (ROOT / name).is_dir() else shutil.copy2)(ROOT / name, source / name)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(self.root / "uv-cache")),
               "UV_PYTHON_PREFERENCE": "system", "UV_NO_PROGRESS": "1", "LANG": "C.UTF-8",
               "UV_TOOL_DIR": str(home / ".uv-tools")}
        done = subprocess.run([uv, "tool", "install", "--no-config", "--quiet", str(source)], env=env, capture_output=True, text=True, timeout=600)
        if done.returncode or not (bin_dir / "agentfare").exists():
            raise SystemExit(f"error: `uv tool install` failed ({done.returncode}): {done.stderr.strip()[-400:]}")
        self.person = home
        return home

    def run_as_a_person(self, *argv: str) -> str:
        """``agentfare <argv>`` as a new user runs it right after installing: no data directory yet, no config file, the real
        clock and colours on. Standard output and error come together, in the order they were written, as a terminal shows them."""
        home = self.install_for_a_person()
        env = {"HOME": str(home), "PATH": f"{home / '.local' / 'bin'}:/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "xterm-256color",
               "COLUMNS": str(COLUMNS), "FORCE_COLOR": "1", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        shutil.rmtree(home / ".agentfare", ignore_errors=True)
        done = subprocess.run([str(home / ".local" / "bin" / "agentfare"), *argv], cwd=self.work, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        if done.returncode:
            raise SystemExit(f"error: `agentfare {shlex.join(argv)}` failed ({done.returncode}): {done.stdout.strip()[-400:]}")
        return self.scrub(done.stdout)

    def scrub(self, text: str) -> str:
        """Paths that name this machine become neutral ones, and nothing that identifies it may be left."""
        text = re.sub(r"\((?:/[^\s()]+)?/pricing\.yaml\)", "(…/agentfare/data/pricing.yaml)", text)
        text = text.replace(str(self.root), "~/agentfare-demo").replace(str(ROOT), "~/agentfare")
        for needle in {str(Path.home()), str(self.root), str(ROOT), "/tmp/", "/work/", "agentfare-assets"}:
            if needle and needle in text:
                raise SystemExit(f"error: the output of a command names this machine ({needle!r}); refusing to draw it")
        user = getpass.getuser()
        if len(user) >= 4 and re.search(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", text):
            raise SystemExit("error: the output of a command names the user of this machine; refusing to draw it")
        return text

    def json(self, *argv: str) -> dict[str, Any]:
        return json.loads(self.run(*argv, "--json"))  # type: ignore[no-any-return]

    def numbers(self) -> dict[str, int]:
        """The week of the demo in cents, for the pictures that draw its bars: what was spent, what the router would have spent."""
        total = self.json("advise")["total"]
        return {"actual": total["actual"]["cents"], "router": total["recommended"]["cents"], "saved": total["saved"]["cents"],
                "saved_bp": total["saved_bp"], "sessions": total["sessions"] + total["skipped"]}


# ---- Chromium ------------------------------------------------------------------------------------------------------------
class Browser:
    """Headless Chromium with a profile of its own (it never touches yours): HTML file in, PNG out."""

    def __init__(self, scratch: Path) -> None:
        self.binary = need("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", why="the PNG images")
        self.profile = scratch / "chromium-profile"
        self.scratch = scratch

    def base(self, width: int, height: int, scale: int) -> list[str]:
        return [self.binary, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={self.profile}",
                f"--window-size={width},{height}", f"--force-device-scale-factor={scale}", "--hide-crash-restore-bubble"]

    def shoot(self, page: Path, out: Path, width: int, height: int, *, scale: int = 1, transparent: bool = False, attempts: int = 4) -> None:
        command = self.base(width, height, scale) + (["--default-background-color=00000000"] if transparent else [])
        done = subprocess.run([*command, f"--screenshot={out}", page.as_uri()], capture_output=True, text=True, timeout=120)
        if not out.is_file() or out.stat().st_size == 0:
            raise SystemExit(f"error: chromium made no screenshot of {page.name}: {done.stderr.strip()[-300:]}")
        if png_width(out) != width * scale:  # the window did not get its size: draw it again
            if attempts > 1:
                return self.shoot(page, out, width, height, scale=scale, transparent=transparent, attempts=attempts - 1)
            raise SystemExit(f"error: chromium drew {page.name} {png_width(out)} px wide instead of {width * scale}")

    def measure(self, page: Path, width: int, *, probe: bool = True) -> list[float]:
        """[x, y, width, height of the element ``.card``, height of the whole page] at this window width: the page's own
        script puts them in its title (with the width of the window it saw), and --dump-dom prints the page after it ran. A page
        that does it itself (``probe=False``) is loaded as it is. Headless Chromium now and then lays a page out in a window
        that has not got its size yet, so a measurement made in a window of another width is thrown away and made again."""
        target = page
        if probe:
            target = page.with_name(page.stem + "-probe.html")
            script = ('<script>addEventListener("load",function(){var r=document.querySelector(".card").getBoundingClientRect();'
                      'document.title=JSON.stringify([r.x,r.y,r.width,r.height,document.documentElement.scrollHeight,innerWidth])});</script></body>')
            target.write_text(page.read_text(encoding="utf-8").replace("</body>", script), encoding="utf-8")
        for _ in range(8):
            done = subprocess.run([*self.base(width, 1000, 1), "--dump-dom", target.as_uri()], capture_output=True, text=True, timeout=120)
            found = re.search(r"<title>(\[[^<]*\])</title>", done.stdout)
            if found and (values := json.loads(found[1]))[5] == width:
                return values[:5]  # type: ignore[no-any-return]
        raise SystemExit(f"error: could not measure the report page at {width} px: {done.stderr.strip()[-300:]}")


# ---- terminal output as text with colours -------------------------------------------------------------------------------------
Run = tuple[str, bool, bool, str | None]  # text, bold, dim, colour


def parse_ansi(text: str) -> list[list[Run]]:
    """The lines of ``text`` as runs of (text, bold, dim, colour): the colours and weights the commands write as SGR codes."""
    bold = dim = False
    colour: str | None = None
    lines: list[list[Run]] = []
    for raw in text.split("\n"):
        runs: list[Run] = []
        position = 0
        for match in re.finditer(r"\x1b\[([0-9;]*)m", raw):
            if match.start() > position:
                runs.append((raw[position:match.start()], bold, dim, colour))
            position = match.end()
            codes = [int(c) if c else 0 for c in match[1].split(";")]
            i = 0
            while i < len(codes):
                code = codes[i]
                if code == 0:
                    bold = dim = False
                    colour = None
                elif code == 1:
                    bold = True
                elif code == 2:
                    dim = True
                elif code == 22:
                    bold = dim = False
                elif code in PALETTE:
                    colour = PALETTE[code]
                elif code == 39:
                    colour = None
                elif code in (38, 48) and i + 1 < len(codes):  # extended colours: only the foreground is drawn
                    width = 2 if codes[i + 1] == 5 else 4
                    if code == 38 and codes[i + 1] == 2 and i + 4 < len(codes):
                        colour = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                    i += width
                i += 1
        if position < len(raw):
            runs.append((raw[position:], bold, dim, colour))
        lines.append(runs)
    if lines and not lines[-1]:
        lines.pop()  # the newline that ends the output
    return lines


def wrap_runs(lines: list[list[Run]], columns: int) -> list[list[Run]]:
    """A terminal breaks a line that is longer than the window: so do the pictures."""
    out: list[list[Run]] = []
    for runs in lines:
        current: list[Run] = []
        used = 0
        for text, bold, dim, colour in runs:
            while len(text) > columns - used:
                take = columns - used
                current.append((text[:take], bold, dim, colour))
                out.append(current)
                current, used, text = [], 0, text[take:]
            if text:
                current.append((text, bold, dim, colour))
                used += len(text)
        out.append(current)
    return out


def typed(argv: tuple[str, ...]) -> str:
    """The words after `agentfare` as a person types them: an argument with spaces goes in double quotes."""
    return " ".join(f'"{a}"' if " " in a and '"' not in a else shlex.quote(a) for a in argv)


def command_line(argv: tuple[str, ...]) -> list[Run]:
    """What the person types: the prompt in green and the command in bold."""
    return [("$ ", True, False, PALETTE[32]), (f"agentfare {typed(argv)}", True, False, PALETTE[97])]


def screen(ws: Workspace, argv: tuple[str, ...], lines: int | None, pipe: tuple[tuple[str, ...], ...] = (), *, own_home: bool = False) -> list[list[Run]]:
    """The prompt line, then the real output of the command (its first ``lines`` lines). A command that is piped into other
    commands is run that way, as a pipe has no colours."""
    text = ws.run_as_a_person(*argv) if own_home else ws.run(*argv, colour=not pipe)
    for words in pipe:
        text = subprocess.run(words, input=text, capture_output=True, text=True, check=True, timeout=60).stdout
    output = parse_ansi(text)
    typed_line = command_line(argv)
    if pipe:
        typed_line = [typed_line[0], (typed_line[1][0] + "".join(f" | {shlex.join(words)}" for words in pipe), True, False, PALETTE[97])]
    return [typed_line, *wrap_runs(output[:lines] if lines else output, COLUMNS)]


# ---- terminal screenshots -------------------------------------------------------------------------------------------------
TERM_WIDTH = 1000
TERM_MARGIN = 12  # room for the window's shadow on a transparent background
FONT_PX, LINE_PX = 15, 22  # a character is 0.6 em wide in every font this script asks for, so 100 columns are 900 pixels
TITLE_PX = 38


def run_html(runs: list[Run]) -> str:
    parts = []
    for text, bold, dim, colour in runs:
        style = ";".join(filter(None, [f"color:{colour}" if colour else "", "font-weight:700" if bold else "", f"opacity:{DIM}" if dim else ""]))
        parts.append(f'<span style="{style}">{html.escape(text)}</span>' if style else html.escape(text))
    return "".join(parts)


def terminal_page(screen_lines: list[list[Run]], title: str) -> tuple[str, int]:
    """(the page, its height in pixels): a dark window with the three dots, a title and the text of the screen."""
    window = TITLE_PX + 1 + 20 + LINE_PX * len(screen_lines) + 24
    body = "\n".join(run_html(runs) for runs in screen_lines)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; background: transparent; }}
.win {{ box-sizing: border-box; margin: {TERM_MARGIN}px; width: {TERM_WIDTH - 2 * TERM_MARGIN}px; height: {window}px; border-radius: 12px; overflow: hidden;
  background: {BG}; border: 1px solid {EDGE}; box-shadow: 0 10px 30px rgba(1, 4, 9, 0.55); }}
.bar {{ position: relative; height: {TITLE_PX}px; background: {BAR}; border-bottom: 1px solid {EDGE}; }}
.dots {{ position: absolute; left: 16px; top: 13px; display: flex; gap: 8px; }}
.dots i {{ width: 12px; height: 12px; border-radius: 50%; display: block; }}
.title {{ position: absolute; left: 0; right: 0; top: 0; line-height: {TITLE_PX}px; text-align: center; font: 13px/{TITLE_PX}px {SANS_CSS}; color: {MUTED}; }}
pre {{ margin: 0; padding: 20px 36px 24px; font: {FONT_PX}px/{LINE_PX}px {MONO_CSS}; color: {FG}; white-space: pre; font-variant-ligatures: none; }}
</style></head><body>
<div class="win"><div class="bar"><div class="dots"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i></div>
<div class="title">{html.escape(title)}</div></div>
<pre>{body}</pre></div></body></html>"""
    return page, window + 2 * TERM_MARGIN


def build_shot(shot: Shot, ws: Workspace, browser: Browser) -> list[Path]:
    title = "agentfare: invented sessions in a scratch home" if shot.own_home else "agentfare demo: invented sessions"
    page, height = terminal_page(screen(ws, shot.argv, shot.lines, own_home=shot.own_home), title)
    html_file = browser.scratch / f"{shot.name}.html"
    html_file.write_text(page, encoding="utf-8")
    out = IMAGES / f"{shot.name}.png"
    browser.shoot(html_file, out, TERM_WIDTH, height, transparent=True)
    return [out]


# ---- hero and social preview ------------------------------------------------------------------------------------------------
def bars_card(n: dict[str, int]) -> str:
    """The cost comparison of the demo week: what was spent against what the router would have spent."""
    router_pct = n["router"] * 1000 // n["actual"] / 10
    return f"""<div class="card">
<div class="cap">{n['sessions']} invented sessions</div>
<div class="row"><span class="lab">You spent</span><span class="track"><span class="fill blue" style="width:100%"></span></span><b>{dollars(n['actual'])}</b></div>
<div class="row"><span class="lab">With the router</span><span class="track"><span class="fill green" style="width:{router_pct}%"></span></span><b>{dollars(n['router'])}</b></div>
<div class="save"><span class="pill">{n['saved_bp'] // 100}.{n['saved_bp'] % 100 // 10}% less</span><span class="note">{dollars(n['saved'])} saved by moving routine sessions to a cheaper model</span></div>
</div>"""


def banner_page(n: dict[str, int], *, margin: int, headline_px: int, name_px: int, chips: bool) -> str:
    first, second = TAGLINE
    chip_row = "".join(f"<span>{name}</span>" for name in ("Claude Code", "Codex CLI", "Gemini CLI")) if chips else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; width: 1280px; height: 640px; background: #0b0f14; font-family: {SANS_CSS}; color: #fff; overflow: hidden; }}
.wrap {{ position: relative; box-sizing: border-box; width: 1280px; height: 640px; padding: {margin}px; display: flex; align-items: center; gap: 56px;
  background: radial-gradient(900px 520px at 8% 0%, rgba(57, 135, 229, 0.30), transparent 70%), radial-gradient(700px 500px at 100% 100%, rgba(25, 158, 112, 0.18), transparent 70%), #0b0f14; }}
.left {{ flex: 1 1 auto; min-width: 0; }}
.brand {{ display: flex; align-items: center; gap: 18px; margin-bottom: 30px; font-size: {name_px}px; font-weight: 800; letter-spacing: -0.02em; }}
h1 {{ margin: 0; font-size: {headline_px}px; line-height: 1.22; font-weight: 700; letter-spacing: -0.015em; text-wrap: balance; }}
h1 span {{ display: block; margin-top: 8px; color: #7fb0ee; text-wrap: balance; }}
.chips {{ display: flex; gap: 10px; margin-top: 34px; }}
.chips span {{ padding: 7px 14px; border-radius: 999px; background: rgba(255, 255, 255, 0.07); border: 1px solid rgba(255, 255, 255, 0.14); color: #c9d1d9; font-size: 18px; font-weight: 600; }}
.foot {{ margin: 22px 0 0; color: #8b949e; font-size: 19px; }}
.card {{ flex: 0 0 auto; width: 470px; box-sizing: border-box; padding: 30px 32px 28px; border-radius: 22px; background: rgba(22, 27, 34, 0.92);
  border: 1px solid rgba(255, 255, 255, 0.12); box-shadow: 0 24px 60px rgba(0, 0, 0, 0.5); }}
.cap {{ color: #8b949e; font-size: 16px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 22px; }}
.row {{ display: grid; grid-template-columns: 1fr auto; row-gap: 10px; align-items: center; margin-bottom: 20px; }}
.row .lab {{ grid-column: 1; font-size: 20px; font-weight: 600; color: #e6edf3; }}
.row b {{ grid-column: 2; grid-row: 1; font-size: 24px; font-variant-numeric: tabular-nums; }}
.track {{ grid-column: 1 / 3; display: block; height: 16px; border-radius: 8px; background: #2c3138; overflow: hidden; }}
.fill {{ display: block; height: 100%; border-radius: 8px; }}
.blue {{ background: {BLUE}; }} .green {{ background: {GREEN}; }}
.save {{ display: flex; align-items: center; gap: 14px; margin-top: 26px; padding-top: 22px; border-top: 1px solid rgba(255, 255, 255, 0.1); }}
.pill {{ padding: 8px 16px; border-radius: 999px; background: rgba(25, 158, 112, 0.2); color: #3ddc9a; font-size: 24px; font-weight: 800; white-space: nowrap; }}
.note {{ color: #9ba6b2; font-size: 16px; line-height: 1.35; }}
</style></head><body><div class="wrap">
<div class="left"><div class="brand">{MARK.format(s=name_px + 10)}<span>AgentFare</span></div>
<h1>{html.escape(first)}<span>{html.escape(second)}</span></h1>
<div class="chips">{chip_row}</div>
<p class="foot">Local-first. Nothing leaves your machine. MIT licensed.</p></div>
{bars_card(n)}
</div></body></html>"""


def build_banner(name: str, ws: Workspace, browser: Browser) -> list[Path]:
    """1280 x 640, drawn at twice the size and brought down, so that the edges and the type are smooth."""
    n = ws.numbers()
    if name == "hero":
        page = banner_page(n, margin=72, headline_px=40, name_px=44, chips=True)
    else:  # GitHub crops a social preview at the edges on some sites: the content stays well inside
        page = banner_page(n, margin=96, headline_px=44, name_px=50, chips=False)
    html_file = browser.scratch / f"{name}.html"
    html_file.write_text(page, encoding="utf-8")
    big = browser.scratch / f"{name}-2x.png"
    browser.shoot(html_file, big, 1280, 640, scale=2)
    return [downscale(big, IMAGES / f"{name}.png", 1280, 640)]


def downscale(source: Path, out: Path, width: int, height: int | None = None) -> Path:
    from PIL import Image

    with Image.open(source) as image:
        size = (width, height or round(image.height * width / image.width))
        image.convert("RGBA" if image.mode == "RGBA" else "RGB").resize(size, Image.Resampling.LANCZOS).save(out, optimize=True)
    return out


# ---- the weekly report, in both themes and on a phone ----------------------------------------------------------------------------
def report_page(ws: Workspace, scratch: Path, theme: str) -> Path:
    """The report of the demo week as one HTML file, in the theme asked for whatever the machine prefers: the page picks its
    colours with a prefers-color-scheme media query, which is made to always (or never) apply. The page behind the card is
    transparent, so the picture is the card alone."""
    page = scratch / f"report-{theme}.html"
    source = scratch / "report-source.html"
    ws.run("report", "--format", "html", "--show-projects", "-o", str(source))
    text = source.read_text(encoding="utf-8")
    assert "@media (prefers-color-scheme: light)" in text
    text = text.replace("@media (prefers-color-scheme: light)", "@media all" if theme == "light" else "@media not all")
    text = text.replace("</head>", "<style>html, body { background: transparent !important; }</style></head>")
    page.write_text(text, encoding="utf-8")
    return page


def phone_page(page: Path, width: int) -> Path:
    """The report inside an iframe that is ``width`` pixels wide. Headless Chromium will not make a window narrower than about
    500 pixels, but an iframe has a viewport of its own, so the page's phone layout (its media queries) applies to it. The
    frame has the colour scheme of the page, or the browser would paint an opaque background behind it."""
    host = page.with_name(page.stem + "-phone.html")
    body = html.escape(page.read_text(encoding="utf-8"), quote=True)
    host.write_text(
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="color-scheme" content="dark"><style>html,body{{margin:0;background:transparent;color-scheme:dark}}'
        f'iframe{{display:block;border:0;width:{width}px;height:6000px;background:transparent}}</style></head><body>'
        f'<iframe id="f" srcdoc="{body}"></iframe><script>addEventListener("load",function(){{'
        'var f=document.getElementById("f"),d=f.contentDocument;f.style.height=d.documentElement.scrollHeight+"px";'
        'var r=d.querySelector(".card").getBoundingClientRect();'
        'document.title=JSON.stringify([r.x,r.y,r.width,r.height,d.documentElement.scrollHeight,innerWidth])});</script></body></html>',
        encoding="utf-8")
    return host


def build_report(name: str, ws: Workspace, browser: Browser) -> list[Path]:
    """The report of the demo week as the card alone: 1200 px wide windows give a 1080 px card, and the phone is 390 px."""
    from PIL import Image

    phone = name.endswith("mobile")
    theme = "light" if name.endswith("light") else "dark"
    page = report_page(ws, browser.scratch, theme)
    out = IMAGES / f"{name}.png"
    if phone:
        host = phone_page(page, 390)
        x, y, w, h, total = browser.measure(host, 500, probe=False)
        shot = browser.scratch / f"{name}-full.png"
        browser.shoot(host, shot, 500, int(total) + 1, transparent=True)
        box = (0, 0, 390, round(y + h) + round(y))  # the page as a phone shows it: the card with the margin of the page around it
    else:
        x, y, w, h, total = browser.measure(page, 1200)
        shot = browser.scratch / f"{name}-full.png"
        browser.shoot(page, shot, 1200, int(total) + 1, transparent=True)
        box = (round(x), round(y), round(x + w), round(y + h))
    with Image.open(shot) as image:
        image.crop(box).save(out, optimize=True)
    return [out]


# ---- the architecture diagram ------------------------------------------------------------------------------------------------
def diagram_svg() -> str:
    """How the pieces fit: three agents' logs in, parsers, a local database of counts, the prices and rules, four commands out,
    and a dashed line around everything that says nothing leaves the machine."""
    width, height, centre = 1280, 540, 300
    font = "Inter, 'Adwaita Sans', 'Noto Sans', 'Liberation Sans', Helvetica, Arial, sans-serif"
    mono = "'JetBrains Mono', 'DejaVu Sans Mono', 'Liberation Mono', monospace"

    def box(x: int, y: int, w: int, h: int, title: str, lines: tuple[str, ...], accent: str) -> str:
        text = "".join(f'<text x="{x + 22}" y="{y + 58 + 22 * i}" class="sub">{html.escape(line)}</text>' for i, line in enumerate(lines))
        return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" class="box"/><rect x="{x}" y="{y + 12}" width="5" height="{h - 24}" rx="2.5" fill="{accent}"/>'
                f'<text x="{x + 22}" y="{y + 34}" class="title">{html.escape(title)}</text>{text}')

    def line(*points: tuple[int, int], head: bool = False) -> str:
        path = "M" + " L".join(f"{x} {y}" for x, y in points)
        marker = ' marker-end="url(#head)"' if head else ""  # not inline: Python 3.11 allows no backslash inside the braces of an f-string
        return f'<path d="{path}" class="arrow"{marker}/>'

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
        "aria-label=\"How AgentFare works: the session logs of Claude Code, Codex CLI and Gemini CLI are read by parsers into a local SQLite "
        "database of token counts, priced with a YAML table and shown by summary, advise, run and report. Nothing leaves your machine.\">",
        "<title>How AgentFare works</title>",
        f"<style>.bg{{fill:{BG}}}.frame{{fill:none;stroke:#3fb950;stroke-width:2;stroke-dasharray:10 8;opacity:.75}}"
        f".box{{fill:{BAR};stroke:{EDGE};stroke-width:1.5}}.title{{font:700 20px {font};fill:#fff}}.sub{{font:500 15px {font};fill:#a8b3bf}}"
        f".arrow{{stroke:#6e7681;stroke-width:2;fill:none;stroke-linejoin:round}}.cap{{font:700 13px {font};fill:{MUTED};letter-spacing:.09em}}"
        f".safe{{font:700 19px {font};fill:#3fb950}}.note{{font:500 15px {font};fill:{MUTED}}}.cmd{{font:700 17px {mono};fill:#fff}}</style>",
        '<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="11" markerHeight="11" orient="auto">'
        '<path d="M0 0 L10 5 L0 10 z" fill="#6e7681"/></marker></defs>',
        f'<rect class="bg" width="{width}" height="{height}" rx="24"/>',
        f'<rect class="frame" x="24" y="24" width="{width - 48}" height="{height - 48}" rx="22"/>',
        '<text x="56" y="66" class="safe">Nothing leaves your machine</text>',
        f'<text x="{width - 56}" y="66" text-anchor="end" class="note">The only network call is the optional prices --refresh.</text>',
        '<text x="56" y="108" class="cap">YOUR AGENTS\' LOGS</text><text x="360" y="108" class="cap">PARSERS</text><text x="582" y="108" class="cap">LOCAL DATABASE</text>'
        '<text x="846" y="108" class="cap">PRICES AND RULES</text><text x="1040" y="108" class="cap">COMMANDS</text>',
    ]
    agents = (("Claude Code", "~/.claude/projects", "#d97757"), ("Codex CLI", "~/.codex/sessions", "#10a37f"), ("Gemini CLI", "~/.gemini/tmp", "#4285f4"))
    top = centre - (3 * 98 + 2 * 24) // 2
    for i, (name, where, accent) in enumerate(agents):
        y = top + i * 122
        parts.append(box(56, y, 252, 98, name, (where, "session logs (JSONL)"), accent))
        parts.append(line((308, y + 49), (334, y + 49)))
    parts.append(line((334, top + 49), (334, top + 2 * 122 + 49)))
    parts.append(line((334, centre), (360, centre), head=True))
    parts.append(box(360, centre - 70, 182, 140, "Parsers", ("one per agent", "tokens in 5 buckets", "no text kept"), BLUE))
    parts.append(line((542, centre), (582, centre), head=True))
    parts.append(box(582, centre - 88, 222, 176, "Local SQLite", ("ids, times, counts", "tokens per model", "per 15-minute slot", "no prompts, replies", "or file contents"), BLUE))
    parts.append(line((804, centre), (846, centre), head=True))
    parts.append(box(846, centre - 70, 168, 140, "Prices", ("pricing.yaml", "unverified models", "left out, named"), AMBER))
    parts.append(line((1014, centre), (1024, centre)))
    commands = (("summary", "spend by model, day"), ("advise", "where cheaper works"), ("run", "pick a model first"), ("report", "markdown or HTML"))
    first = centre - (4 * 74 + 3 * 18) // 2
    parts.append(line((1024, first + 37), (1024, first + 3 * 92 + 37)))
    for i, (cmd, what) in enumerate(commands):
        y = first + i * 92
        parts.append(line((1024, y + 37), (1040, y + 37), head=True))
        parts.append(f'<rect x="1040" y="{y}" width="192" height="74" rx="14" class="box"/><rect x="1040" y="{y + 12}" width="5" height="50" rx="2.5" fill="{GREEN}"/>'
                     f'<text x="1062" y="{y + 32}" class="cmd">{cmd}</text><text x="1062" y="{y + 55}" class="sub">{html.escape(what)}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def build_diagram() -> list[Path]:
    svg = IMAGES / "how-it-works.svg"
    png = IMAGES / "how-it-works.png"
    svg.write_text(diagram_svg(), encoding="utf-8")
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "--width=1920", "--background-color=none", str(svg), "-o", str(png)], check=True, timeout=120)
    else:
        subprocess.run([need("magick", "convert", why="how-it-works.png"), "-background", "none", "-density", "144", str(svg), "-resize", "1920x", str(png)],
                       check=True, timeout=120)
    return [svg, png]


# ---- the GIF -----------------------------------------------------------------------------------------------------------------
GIF_WIDTH, GIF_ROWS = 900, 32
GIF_FONT_PX, GIF_LINE_PX, GIF_PAD = 14, 20, 30
GIF_FPS = 12.5
GIF_TYPE_FRAMES = 12  # the longest command takes this many frames to type, so that a long one is not a wait


def font_files() -> tuple[Path, Path]:
    """(regular, bold) files of the first monospace family fontconfig has among the ones this script prefers."""
    match = need("fc-match", why="finding a monospace font")
    for family in ("JetBrains Mono", "JetBrainsMono Nerd Font", "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono"):
        found = []
        for style in ("Regular", "Bold"):
            done = subprocess.run([match, "-f", "%{family}\n%{file}", f"{family}:style={style}"], capture_output=True, text=True)
            name, _, path = done.stdout.partition("\n")
            found.append(Path(path) if family.lower().replace(" ", "") in name.lower().replace(" ", "") and path else None)
        if found[0] and found[1]:
            return found[0], found[1]
    raise SystemExit("error: no monospace font found (install JetBrains Mono, DejaVu Sans Mono or Liberation Mono)")


def hex_rgb(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def mix(foreground: tuple[int, int, int], keep: float) -> tuple[int, int, int]:
    back = hex_rgb(BG)
    return tuple(round(f * keep + b * (1 - keep)) for f, b in zip(foreground, back))  # type: ignore[return-value]


class Terminal:
    """Draws the screens of demo.gif: a window with the three dots and 32 rows of text, at twice the size and brought down."""

    def __init__(self) -> None:
        from PIL import ImageFont

        regular, bold = font_files()
        self.scale = 2
        size = GIF_FONT_PX * self.scale
        self.fonts = (ImageFont.truetype(str(regular), size), ImageFont.truetype(str(bold), size))
        self.cell = (GIF_WIDTH - 2 * GIF_PAD) / COLUMNS  # the width of a character, so that 100 columns fill the window
        self.height = 36 + GIF_PAD // 2 + GIF_ROWS * GIF_LINE_PX + GIF_PAD // 2

    def blank(self) -> Any:
        from PIL import Image, ImageDraw

        s = self.scale
        image = Image.new("RGB", (GIF_WIDTH * s, self.height * s), hex_rgb(BG))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, GIF_WIDTH * s, 36 * s), fill=hex_rgb(BAR))
        draw.line((0, 36 * s, GIF_WIDTH * s, 36 * s), fill=hex_rgb(EDGE), width=s)
        for i, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            cx, cy, r = (18 + 20 * i) * s, 18 * s, 6 * s
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=hex_rgb(colour))
        return image

    def draw(self, image: Any, rows: list[list[Run]], *, cursor_at: tuple[int, int] | None = None) -> Any:
        from PIL import ImageDraw

        s = self.scale
        draw = ImageDraw.Draw(image)
        top = 36 + GIF_PAD // 2
        for r, runs in enumerate(rows[-GIF_ROWS:]):
            col = 0
            for text, bold, dim, colour in runs:
                rgb = hex_rgb(colour or FG)
                rgb = mix(rgb, DIM) if dim else rgb
                for ch in text:
                    if ch != " ":
                        draw.text((round((GIF_PAD + col * self.cell) * s), (top + r * GIF_LINE_PX) * s), ch, font=self.fonts[bold], fill=rgb)
                    col += 1
        if cursor_at:
            col, r = cursor_at
            x, y = (GIF_PAD + col * self.cell) * s, (top + r * GIF_LINE_PX + 2) * s
            draw.rectangle((x, y, x + self.cell * s, y + (GIF_LINE_PX - 4) * s), fill=hex_rgb("#c9d1d9"))
        return image

    def frame(self, rows: list[list[Run]], cursor_at: tuple[int, int] | None = None) -> Any:
        from PIL import Image

        image = self.draw(self.blank(), rows, cursor_at=cursor_at)
        return image.resize((GIF_WIDTH, self.height), Image.Resampling.LANCZOS)


def build_gif(ws: Workspace, scratch: Path) -> list[Path]:
    ffmpeg = need("ffmpeg", why="demo.gif")
    term = Terminal()
    frames_dir = scratch / "gif"
    frames_dir.mkdir()
    entries: list[tuple[Path, float]] = []
    count = 0

    def add(image: Any, seconds: float) -> None:
        nonlocal count
        path = frames_dir / f"f{count:04d}.png"
        image.save(path)
        entries.append((path, seconds))
        count += 1

    step = 1 / GIF_FPS
    add(term.frame([[("$ ", True, False, PALETTE[32])]], cursor_at=(2, 0)), 0.3)
    for scene in GIF_SCENES:
        rows = screen(ws, scene.argv, scene.lines, scene.pipe, own_home=scene.own_home)
        typed = rows[0]
        text = typed[1][0]
        per_frame = max(3, -(-len(text) // GIF_TYPE_FRAMES))
        for shown in range(per_frame, len(text) + per_frame, per_frame):
            shown = min(shown, len(text))
            line = [typed[0], (text[:shown], True, False, PALETTE[97])]
            add(term.frame([line], cursor_at=(2 + shown, 0)), step)
        add(term.frame(rows, cursor_at=None), scene.pause)
        add(term.frame([[("$ ", True, False, PALETTE[32])]], cursor_at=(2, 0)), 0.2)
    listing = frames_dir / "frames.ffconcat"
    listing.write_text("".join(f"file '{p.name}'\nduration {d:.3f}\n" for p, d in entries) + f"file '{entries[-1][0].name}'\n", encoding="utf-8")
    out = IMAGES / "demo.gif"
    graph = (f"fps={GIF_FPS},split[a][b];[a]palettegen=max_colors=96:stats_mode=full[p];"
             "[b][p]paletteuse=dither=none:diff_mode=rectangle")
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-vf", graph, "-loop", "0", str(out)],
                   check=True, cwd=frames_dir, timeout=600)
    seconds = sum(d for _, d in entries)
    say(f"  demo.gif runs {seconds:.1f} s")
    return [out]


# ---- running it ---------------------------------------------------------------------------------------------------------------
def builders(ws: Workspace, scratch: Path, browser: Browser | None) -> dict[str, Callable[[], list[Path]]]:
    def chrome() -> Browser:
        nonlocal browser
        browser = browser or Browser(scratch)
        return browser

    table: dict[str, Callable[[], list[Path]]] = {
        "hero": lambda: build_banner("hero", ws, chrome()),
        "social-preview": lambda: build_banner("social-preview", ws, chrome()),
        "report-dark": lambda: build_report("report-dark", ws, chrome()),
        "report-light": lambda: build_report("report-light", ws, chrome()),
        "report-mobile": lambda: build_report("report-mobile", ws, chrome()),
        "how-it-works": build_diagram,
        "demo": lambda: build_gif(ws, scratch),
    }
    for shot in SHOTS:
        table[shot.name] = lambda shot=shot: build_shot(shot, ws, chrome())  # type: ignore[misc]
    return table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the images of the README into docs/images/ from the synthetic demo data.")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="build only these assets (see --list)")
    parser.add_argument("--list", action="store_true", help="print the asset names and the files they write, then stop")
    args = parser.parse_args(argv)
    if args.list:
        for name, files in OUTPUTS.items():
            print(f"{name:16} {' '.join(files)}")
        return 0
    names = args.only or list(OUTPUTS)
    if unknown := [n for n in names if n not in OUTPUTS]:
        parser.error(f"unknown asset {', '.join(unknown)} (known: {', '.join(OUTPUTS)})")
    IMAGES.mkdir(parents=True, exist_ok=True)
    ws = Workspace()
    try:
        table = builders(ws, ws.root, None)
        for name in names:
            say(f"{name}")
            for path in table[name]():
                size = path.stat().st_size
                if size > MAX_BYTES:
                    raise SystemExit(f"error: {path.name} is {size / 1e6:.1f} MB, over the {MAX_BYTES / 1e6:.1f} MB limit")
                shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
                say(f"  wrote {shown} ({size / 1024:.0f} KiB)")
    finally:
        ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

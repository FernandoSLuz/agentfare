"""Nothing but `agentfare prices --refresh` may touch the network: a static scan of the source and a runtime guard."""

from __future__ import annotations

import ast
import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

import agentfare
from agentfare.cli import app

SRC = Path(agentfare.__file__).parent
FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config.yaml"
NETWORK_MODULES = (
    "urllib.request", "urllib.robotparser", "urllib3", "http.client", "http.server", "socket", "socketserver", "ssl",
    "ftplib", "smtplib", "poplib", "imaplib", "nntplib", "telnetlib", "xmlrpc", "wsgiref", "asyncio", "webbrowser",
    "multiprocessing.connection", "requests", "httpx", "aiohttp", "websockets", "websocket",
)  # fmt: skip
RUN_LINE = ["run", "--dry-run", "--", "claude", "-p", "fix a typo"]  # agentfare's own options come first; after -- is the agent's command
DYNAMIC_ALLOWED = {SRC / "parsers" / "__init__.py"}  # imports the parser module named after an agent from a fixed list
ONLY_NETWORK_MODULE = SRC / "pricefetch.py"
ONLY_IMPORTER = SRC / "commands" / "prices.py"


def dotted(node: ast.expr) -> str:
    """'urllib.request.urlopen' for that attribute chain, '' when it does not start at a plain name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else ""


def network_imports(source: str) -> set[str]:
    """Network-capable modules a piece of code imports or reaches, at any depth (a lazy import inside a function
    counts, and so does ``urllib.request.urlopen`` after only ``import urllib.parse``)."""
    found: set[str] = set()

    def flag(name: str) -> None:
        found.update(m for m in NETWORK_MODULES if name == m or name.startswith(m + "."))

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                flag(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            flag(node.module)
            for alias in node.names:  # "from urllib import request"
                flag(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Attribute):
            flag(dotted(node))
        elif isinstance(node, ast.Call):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if called in ("__import__", "import_module") and node.args and isinstance(node.args[0], ast.Constant):
                flag(str(node.args[0].value))
    return found


def dynamic_code(source: str) -> list[str]:
    """Calls that could load a network module under a name the scan cannot read: an import whose module name is not
    a literal, eval, exec and compile."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            literal = bool(node.args) and isinstance(node.args[0], ast.Constant)
            builtin_code = isinstance(node.func, ast.Name) and called in ("eval", "exec", "compile")  # not re.compile
            if builtin_code or (called in ("__import__", "import_module") and not literal):
                found.append(f"{called}() at line {node.lineno}")
    return found


def python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py"))


# ---- static scan ----------------------------------------------------------------------------------------
def test_only_pricefetch_imports_network_modules() -> None:
    offenders = {str(p.relative_to(SRC)): sorted(found) for p in python_files() if p != ONLY_NETWORK_MODULE and (found := network_imports(p.read_text()))}
    assert offenders == {}
    assert {"urllib.request", "http.client"} <= network_imports(ONLY_NETWORK_MODULE.read_text())  # the scan does see the real thing


@pytest.mark.parametrize(
    "code",
    [
        "import socket",
        "import urllib.request",
        "from urllib import request",
        "from urllib.request import urlopen",
        "from http import client",
        "import http.client as h",
        "def f():\n    import ssl",
        "import requests, json",
        "__import__('ssl')",
        "import importlib\nimportlib.import_module('urllib.request')",
        "import urllib.parse\nurllib.request.urlopen('http://x')",  # request is only loaded, not imported, here
        "import http\nhttp.client.HTTPSConnection('x')",
        "import asyncio",
        "import webbrowser",
        "from urllib import robotparser",
    ],
)
def test_the_scan_catches_every_way_of_importing_a_network_module(code: str) -> None:
    assert network_imports(code)


@pytest.mark.parametrize("code", ["import json", "from urllib.parse import urlsplit", "import urllib.parse", "from http import HTTPStatus", "import subprocess"])
def test_the_scan_leaves_harmless_imports_alone(code: str) -> None:
    assert network_imports(code) == set()


@pytest.mark.parametrize("code", ["__import__(name)", "importlib.import_module(f'x.{agent}')", "eval(text)", "exec(text)", "compile(text, 'f', 'exec')"])
def test_the_scan_flags_code_that_hides_what_it_loads(code: str) -> None:
    assert dynamic_code(code)


@pytest.mark.parametrize("code", ["import_module('json')", "__import__('json')", "print(text)", "re.compile('x')"])
def test_the_scan_leaves_literal_imports_alone(code: str) -> None:
    assert dynamic_code(code) == []


def test_no_module_hides_what_it_imports() -> None:
    offenders = {str(p.relative_to(SRC)): found for p in python_files() if p not in DYNAMIC_ALLOWED and (found := dynamic_code(p.read_text()))}
    assert offenders == {}


def test_only_the_prices_command_reaches_pricefetch_and_only_when_refreshing() -> None:
    importers = []
    for path in python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            if any(n.split(".")[-1] == "pricefetch" for n in names):
                importers.append((path, node, tree))
    assert {p for p, _, _ in importers} == {ONLY_IMPORTER}
    for path, node, tree in importers:
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        chain = []
        while node in parents:
            node = parents[node]
            chain.append(node)
        lazy = any(isinstance(n, ast.FunctionDef) for n in chain)
        type_checking_only = any(isinstance(n, ast.If) and getattr(n.test, "id", "") == "TYPE_CHECKING" for n in chain)
        assert lazy or type_checking_only, f"{path.name} imports pricefetch when the module loads"


# ---- runtime guard --------------------------------------------------------------------------------------
def command_lines() -> list[list[str]]:
    """Every command the Typer app registers, as argument lists to run, found from the app itself so that commands
    added later are covered without touching this test. `prices --refresh` is the one thing left out; `run` is only
    ever run with --dry-run so the test can never start a real agent."""
    group = typer.main.get_command(app)
    lines: list[list[str]] = []
    for name, command in group.commands.items():  # type: ignore[attr-defined]
        options = {opt for p in command.params for opt in p.opts}
        if name == "run":
            if "--dry-run" in options:
                lines.append(RUN_LINE)
            continue
        args = [name]
        for param in command.params:
            if getattr(param, "param_type_name", "") == "argument" and param.required:
                args.append("claude" if param.name == "agent" else "typo in the readme")
        lines.append(args)
        if "--json" in options:
            lines.append(args + ["--json"])
        if name == "prices":
            lines.append(args + ["--unverified"])
    return lines


def test_the_command_list_is_read_from_the_app() -> None:
    lines = command_lines()
    assert ["prices"] in lines and ["prices", "--json"] in lines and ["summary"] in lines
    assert not any("--refresh" in line for line in lines)
    assert [line for line in lines if line[0] == "run"] == [RUN_LINE]  # once, and never without --dry-run
    assert RUN_LINE.index("--dry-run") < RUN_LINE.index("--") < RUN_LINE.index("claude")  # run reads everything after the first argument as the agent's own command line


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every way into the network, and the start of any process, fails loudly and is recorded, even if a command swallows the error."""
    attempts: list[str] = []

    def blocked(name: str) -> Any:
        def guard(*args: object, **kwargs: object) -> Any:
            attempts.append(name)
            raise OSError(f"network blocked by the test: {name}")

        return guard

    monkeypatch.setattr(subprocess.Popen, "__init__", blocked("subprocess.Popen"))  # `run` starts the agent this way, and only when it is not a dry run
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):  # the last two reach a host over UDP with no connect
        monkeypatch.setattr(socket.socket, name, blocked(f"socket.{name}"))
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        monkeypatch.setattr(socket, name, blocked(f"socket.{name}"))
    return attempts


def invoke(home: Path, *args: str) -> Any:
    return CliRunner().invoke(app, ["--config", str(FIXTURE_CONFIG), "--home", str(home), *args], env={"AGENTFARE_NOW": "2026-09-30T12:00:00Z"})


def test_no_command_but_prices_refresh_touches_the_network(no_network: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # a command that writes a relative file writes it here
    for args in command_lines():
        result = invoke(tmp_path / "home", *args)
        assert no_network == [], f"`agentfare {' '.join(args)}` tried to use the network or start a process: {no_network}"
        # a command that crashed before it got anywhere proves nothing (exit codes are fine: Exit is a SystemExit)
        assert not isinstance(result.exception, Exception), f"`agentfare {' '.join(args)}` crashed: {result.exception!r}"


def test_the_run_line_is_a_real_dry_run_that_gets_as_far_as_the_decision(no_network: list[str], tmp_path: Path) -> None:
    """A usage error would also pass the guard above, having done nothing: this one shows the dry run itself ran."""
    result = invoke(tmp_path / "home", *RUN_LINE)
    assert result.exit_code == 0, result.output
    panel = " ".join(re.sub(r"[│╭╮╰╯─]", " ", result.stdout).split())  # the box borders and the wrapping taken away
    assert "agentfare run (dry run) agent claude" in panel and "command claude --model" in panel
    assert no_network == []  # the agent was not started


@pytest.mark.parametrize(
    "reach",
    [
        lambda: socket.socket().connect(("127.0.0.1", 9)),
        lambda: socket.socket().connect_ex(("127.0.0.1", 9)),
        lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9)),
        lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b"x"], [], 0, ("127.0.0.1", 9)),
        lambda: socket.create_connection(("127.0.0.1", 9)),
        lambda: socket.getaddrinfo("example.com", 80),
        lambda: socket.gethostbyname("example.com"),
        lambda: socket.gethostbyaddr("127.0.0.1"),
        lambda: socket.getnameinfo(("127.0.0.1", 80), 0),
    ],
    ids=["connect", "connect_ex", "sendto", "sendmsg", "create_connection", "getaddrinfo", "gethostbyname", "gethostbyaddr", "getnameinfo"],
)
def test_the_guard_closes_every_way_out_of_the_machine(no_network: list[str], reach: Any) -> None:
    with pytest.raises(OSError, match="network blocked"):
        reach()
    assert len(no_network) == 1


def test_the_guard_does_stop_prices_refresh(no_network: list[str], tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "pricing.yaml").write_text("models:\n  m-1:\n    input: 1\n    output: 2\n    source: http://127.0.0.1:9/prices\n")
    out = invoke(home, "prices", "--refresh", "--allow-private-hosts", "--json")
    assert no_network, "the guard never fired, so the test above proves nothing"
    assert out.exit_code == 1
    assert json.loads(out.stdout)["results"][0]["status"] == "error"


def test_running_every_other_command_never_loads_the_fetch_module(tmp_path: Path) -> None:
    """In a fresh interpreter: importing the CLI and running everything but `prices --refresh` leaves pricefetch (and
    the HTTP client it wraps) unloaded."""
    script = (
        "import json, sys\n"
        "from typer.testing import CliRunner\n"
        "from agentfare.cli import app\n"
        "for args in json.loads(sys.argv[1]):\n"
        f"    CliRunner().invoke(app, ['--config', {str(FIXTURE_CONFIG)!r}, '--home', sys.argv[2], *args])\n"
        "loaded = [m for m in ('agentfare.pricefetch', 'urllib.request') if m in sys.modules]\n"
        "print('loaded:' + ','.join(loaded))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, json.dumps(command_lines()), str(tmp_path / "home")],
        capture_output=True, text=True, cwd=tmp_path, timeout=120,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "loaded:"

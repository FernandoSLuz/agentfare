"""Install paths: what a person gets after `uv tool install`, `pip install` in a venv, `pipx` or `pip install git+https://...`.

Three layers, from fast to slow:

* Always, offline and in milliseconds: the package metadata in pyproject.toml (license, urls, classifiers, python
  floor, the four runtime dependencies), the entry point, and that every data file the program reads at run time is named
  by a ``package-data`` pattern, so it is in the wheel. The program is also started the way an installed copy is, from a
  folder that is not the source tree.
* When ``setuptools`` can be imported: the wheel itself is built from a copy of the tree (no network, a second or two),
  its contents and metadata are checked, and it is run from a place that is not the source tree.
* Only with ``AGENTFARE_TEST_INSTALL=1``: the commands that were verified for the README, run for real in throwaway
  environments (a venv with pip, uv tool, pip install and uninstall). They need the network to fetch the dependencies, so
  they never run by default; the CI-free way to repeat them is ``AGENTFARE_TEST_INSTALL=1 pytest tests/test_install_paths.py``.

Nothing here touches ~/.agentfare, the real agent logs or a shell startup file.
"""

from __future__ import annotations

import configparser
import glob
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any

import pytest

import agentfare

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path(agentfare.__file__).parent
META: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")) if (ROOT / "pyproject.toml").is_file() else {}
pytestmark = pytest.mark.skipif(not META, reason="needs a checkout with pyproject.toml (an installed copy has none)")
PROJECT: dict[str, Any] = META.get("project", {})
REPO = "https://github.com/FernandoSLuz/agentfare"
RUNTIME_DEPENDENCIES = {"typer", "rich", "pyyaml", "jinja2"}  # never grows: a new runtime dependency is a decision, not a side effect
COMMANDS = ("init", "ingest", "config", "summary", "sessions", "models", "advise", "run", "report", "prices", "doctor")


def requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def clean_environment(home: Path, **extra: str) -> dict[str, str]:
    """A child process environment of its own: an empty HOME, no AgentFare variable, no log folder variable."""
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env.update(HOME=str(home), COLUMNS="100", **extra)
    return env


# ---- the metadata ----------------------------------------------------------------------------------------------------
class TestMetadata:
    def test_the_license_is_mit_and_its_file_ships(self) -> None:
        assert PROJECT["license"] == "MIT"  # an SPDX expression, which setuptools 77 and newer writes as License-Expression
        assert PROJECT["license-files"] == ["LICENSE"]
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert text.startswith("MIT License") and "Permission is hereby granted, free of charge" in text

    def test_the_project_urls_point_at_the_repository(self) -> None:
        assert PROJECT["urls"] == {"Homepage": REPO, "Source": REPO, "Issues": f"{REPO}/issues"}

    def test_the_python_floor_and_the_classifiers_agree(self) -> None:
        floor = re.fullmatch(r">=(3)\.(\d+)", PROJECT["requires-python"])
        assert floor, PROJECT["requires-python"]
        listed = sorted(int(m[1]) for c in PROJECT["classifiers"] if (m := re.fullmatch(r"Programming Language :: Python :: 3\.(\d+)", c)))
        assert listed and listed[0] == int(floor[2]) and listed == list(range(listed[0], listed[-1] + 1)), listed  # from the floor up, no gap
        assert "Operating System :: POSIX :: Linux" in PROJECT["classifiers"] and "Operating System :: MacOS" in PROJECT["classifiers"]
        assert not any(c.startswith("License ::") for c in PROJECT["classifiers"])  # the SPDX expression says it; a License classifier is deprecated beside it

    def test_there_are_only_the_four_runtime_dependencies(self) -> None:
        assert {requirement_name(r) for r in PROJECT["dependencies"]} == RUNTIME_DEPENDENCIES
        assert {requirement_name(r) for r in PROJECT["optional-dependencies"]["dev"]} == {"pytest"}

    def test_the_version_in_pyproject_is_the_one_the_program_reports(self) -> None:
        assert PROJECT["version"] == agentfare.__version__
        done = subprocess.run([sys.executable, "-m", "agentfare", "--version"], capture_output=True, text=True, timeout=60)
        assert done.stdout.strip() == f"agentfare {PROJECT['version']}"

    def test_the_readme_is_the_long_description_and_exists(self) -> None:
        assert PROJECT["readme"] == "README.md" and (ROOT / "README.md").is_file()

    def test_the_entry_point_is_a_command_that_resolves(self) -> None:
        assert PROJECT["scripts"] == {"agentfare": "agentfare.cli:_entry"}
        module, _, function = PROJECT["scripts"]["agentfare"].partition(":")
        assert callable(getattr(importlib.import_module(module), function))


# ---- what the wheel has to hold -------------------------------------------------------------------------------------------
def data_files() -> set[str]:
    """Every file under the package that is not Python code or a cache: what the program reads at run time."""
    found = set()
    for path in PACKAGE.rglob("*"):
        rel = path.relative_to(PACKAGE)
        if path.is_file() and path.suffix not in (".py", ".pyc") and "__pycache__" not in rel.parts:
            found.add(rel.as_posix())
    return found


def packaged_by_patterns() -> set[str]:
    """The files the ``package-data`` patterns pick, the way setuptools reads them (``**`` recurses, ``*`` skips dotfiles)."""
    patterns = META["tool"]["setuptools"]["package-data"]["agentfare"]
    return {Path(p).as_posix() for pattern in patterns for p in glob.glob(pattern, root_dir=PACKAGE, recursive=True) if (PACKAGE / p).is_file()}


class TestPackageData:
    def test_the_files_the_program_reads_are_all_packaged(self) -> None:
        missing = sorted(data_files() - packaged_by_patterns())
        assert missing == [], f"add a package-data pattern in pyproject.toml, or the wheel will not carry: {missing}"

    def test_the_price_table_and_the_report_templates_are_packaged(self) -> None:
        packaged = packaged_by_patterns()
        assert {"data/pricing.yaml", "templates/report.html.j2", "templates/report.md.j2"} <= packaged

    def test_the_demo_data_is_packaged_when_there_is_a_demo(self) -> None:
        demo = PACKAGE / "demo_data"
        if not demo.is_dir():
            pytest.skip("this tree has no packaged demo data")
        packaged = packaged_by_patterns()
        assert "demo_data/config.yaml" in packaged
        assert any(name.endswith(".project_root") for name in packaged), "Gemini's .project_root files start with a dot: a glob does not match them"
        assert sorted(n for n in data_files() if n.startswith("demo_data/")) == sorted(n for n in packaged if n.startswith("demo_data/"))

    def test_no_pattern_is_dead(self) -> None:
        for pattern in META["tool"]["setuptools"]["package-data"]["agentfare"]:
            assert glob.glob(pattern, root_dir=PACKAGE, recursive=True), f"{pattern} matches nothing: a file moved and the wheel lost it"

    def test_the_price_table_the_wheel_ships_is_the_one_at_the_root(self) -> None:
        assert (ROOT / "pricing.yaml").read_bytes() == (PACKAGE / "data" / "pricing.yaml").read_bytes()

    def test_nothing_that_is_not_ours_is_packaged(self) -> None:
        for name in packaged_by_patterns():
            assert not re.search(r"\.(db|sqlite|log|pyc)$|(^|/)(\.env|config\.local)", name), name


# ---- the program, started from somewhere else ----------------------------------------------------------------------------------
class TestFromAnotherFolder:
    """What a person types after installing, run from a folder that is not the source tree, with an empty HOME."""

    @staticmethod
    def agentfare(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        work = tmp_path / "elsewhere"
        work.mkdir(exist_ok=True)
        env = clean_environment(tmp_path / "home", AGENTFARE_HOME=str(tmp_path / "home" / ".agentfare"), PYTHONPATH=str(PACKAGE.parent))  # the code under test, installed or not
        return subprocess.run([sys.executable, "-m", "agentfare", *args], cwd=work, capture_output=True, text=True, timeout=120, env=env)

    def test_help_lists_every_command(self, tmp_path: Path) -> None:
        done = self.agentfare(tmp_path, "--help")
        assert done.returncode == 0, done.stderr
        for command in COMMANDS:
            assert re.search(rf"^│ {command}\s", done.stdout, re.M), command

    def test_doctor_works_on_a_machine_with_nothing(self, tmp_path: Path) -> None:
        done = self.agentfare(tmp_path, "doctor", "--json")
        assert done.returncode == 0, done.stderr
        report = json.loads(done.stdout)
        assert report["ok"] is True and not (tmp_path / "home" / ".agentfare").exists()  # and it created nothing
        by_id = {c["id"]: c for c in report["checks"]}
        assert by_id["price_table"]["data"]["models"] > 20 and by_id["python"]["status"] == "ok"

    def test_init_dry_run_looks_and_writes_nothing(self, tmp_path: Path) -> None:
        done = self.agentfare(tmp_path, "init", "--dry-run")
        assert done.returncode == 0, done.stderr
        assert "Dry run" in done.stdout and not (tmp_path / "home" / ".agentfare").exists()

    def test_the_first_report_command_on_an_empty_machine_says_what_to_do(self, tmp_path: Path) -> None:
        done = self.agentfare(tmp_path, "summary")
        assert done.returncode == 0 and "agentfare init" in done.stdout and "agentfare doctor" in done.stdout


# ---- the wheel ---------------------------------------------------------------------------------------------------------------
def build_with_setuptools(tmp_path_factory: pytest.TempPathFactory, kind: str) -> Path:
    """A wheel or an sdist of a copy of the tree, built in a child process with setuptools' own backend (no network, no pip)."""
    try:
        import setuptools
    except ImportError:
        pytest.skip(f"building the {kind} needs setuptools (pip install setuptools); the checks above do not")
    if int(setuptools.__version__.split(".")[0]) < 77:
        pytest.skip("pyproject.toml asks for setuptools>=77 (SPDX license expressions)")
    work = tmp_path_factory.mktemp(f"{kind}-build")
    copy = work / "tree"
    ignore = shutil.ignore_patterns(".venv", "venv", ".git", "__pycache__", "build", "dist", "*.egg-info", ".pytest_cache", ".agentfare")
    shutil.copytree(ROOT, copy, ignore=ignore)
    out = work / "out"
    out.mkdir()
    code = f"import sys; from setuptools import build_meta; print(build_meta.build_{kind}(sys.argv[1]))"
    done = subprocess.run([sys.executable, "-c", code, str(out)], cwd=copy, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    [built] = out.glob("*.whl" if kind == "wheel" else "*.tar.gz")
    return built


def build_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_with_setuptools(tmp_path_factory, "wheel")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_wheel(tmp_path_factory)


class TestWheel:
    def names(self, wheel: Path) -> list[str]:
        with zipfile.ZipFile(wheel) as z:
            return z.namelist()

    def metadata(self, wheel: Path) -> Any:
        with zipfile.ZipFile(wheel) as z:
            [name] = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
            return Parser().parsestr(z.read(name).decode("utf-8"))

    def test_it_is_named_for_the_version_and_works_on_any_python_3(self, wheel: Path) -> None:
        assert wheel.name == f"agentfare-{PROJECT['version']}-py3-none-any.whl"
        assert wheel.stat().st_size < 4_000_000

    def test_it_holds_the_price_table_the_templates_and_the_demo_data(self, wheel: Path) -> None:
        names = set(self.names(wheel))
        assert {"agentfare/data/pricing.yaml", "agentfare/templates/report.html.j2", "agentfare/templates/report.md.j2"} <= names
        if (PACKAGE / "demo_data").is_dir():
            assert "agentfare/demo_data/config.yaml" in names
            assert any(n.endswith("/.project_root") for n in names)
            wanted = {f"agentfare/{n}" for n in data_files() if n.startswith("demo_data/")}
            assert wanted <= names, sorted(wanted - names)

    def test_every_module_of_the_source_is_in_it_and_nothing_else_is(self, wheel: Path) -> None:
        in_wheel = {n for n in self.names(wheel) if n.endswith(".py")}
        in_source = {f"agentfare/{p.relative_to(PACKAGE).as_posix()}" for p in PACKAGE.rglob("*.py")}
        assert in_wheel == in_source
        assert not any(part in ("tests", "docs", "scripts", ".github") for n in self.names(wheel) for part in n.split("/")[:1])

    def test_the_modules_in_it_are_the_ones_in_the_tree(self, wheel: Path) -> None:
        with zipfile.ZipFile(wheel) as z:
            for name in ("agentfare/cli.py", "agentfare/commands/init.py", "agentfare/commands/doctor.py", "agentfare/setup_checks.py"):
                assert z.read(name) == (ROOT / "src" / name).read_bytes(), name

    def test_the_entry_point_makes_an_agentfare_command(self, wheel: Path) -> None:
        with zipfile.ZipFile(wheel) as z:
            [name] = [n for n in z.namelist() if n.endswith("entry_points.txt")]
            parser = configparser.ConfigParser()
            parser.read_string(z.read(name).decode())
        assert dict(parser["console_scripts"]) == {"agentfare": "agentfare.cli:_entry"}

    def test_the_metadata_says_mit_and_names_the_repository(self, wheel: Path) -> None:
        meta = self.metadata(wheel)
        assert meta["Name"] == "agentfare" and meta["Version"] == PROJECT["version"]
        assert meta["License-Expression"] == "MIT"
        assert meta["Requires-Python"] == PROJECT["requires-python"]
        urls = dict(item.split(", ", 1) for item in meta.get_all("Project-URL"))
        assert urls == {"Homepage": REPO, "Source": REPO, "Issues": f"{REPO}/issues"}
        assert set(meta.get_all("Classifier")) == set(PROJECT["classifiers"])
        assert meta["Description-Content-Type"] == "text/markdown" and "AgentFare" in meta.get_payload()
        required = {requirement_name(r) for r in meta.get_all("Requires-Dist") if "extra ==" not in r}
        assert required == RUNTIME_DEPENDENCIES

    def test_the_license_text_is_inside(self, wheel: Path) -> None:
        with zipfile.ZipFile(wheel) as z:
            [name] = [n for n in z.namelist() if n.endswith("licenses/LICENSE")]
            assert z.read(name).decode().startswith("MIT License")

    def test_it_runs_from_a_folder_that_is_not_the_source_tree(self, wheel: Path, tmp_path: Path) -> None:
        """Unpacked like an install would (here: onto PYTHONPATH, the dependencies being the ones of this environment): the
        price table, the templates and the demo data are found next to the code, and nothing is read from the source tree."""
        site = tmp_path / "site"
        with zipfile.ZipFile(wheel) as z:
            z.extractall(site)
        work = tmp_path / "elsewhere"
        work.mkdir()
        env = clean_environment(tmp_path / "home", PYTHONPATH=str(site), AGENTFARE_HOME=str(tmp_path / "home" / ".agentfare"))

        def agentfare(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, "-m", "agentfare", *args], cwd=work, capture_output=True, text=True, timeout=120, env=env)

        located = subprocess.run([sys.executable, "-c", "import agentfare; print(agentfare.__file__)"], cwd=work, capture_output=True, text=True, env=env)
        assert Path(located.stdout.strip()).is_relative_to(site), located.stdout
        assert agentfare("--version").stdout.strip() == f"agentfare {PROJECT['version']}"
        doctor = agentfare("doctor", "--json")
        assert doctor.returncode == 0, doctor.stdout + doctor.stderr
        by_id = {c["id"]: c for c in json.loads(doctor.stdout)["checks"]}
        assert by_id["package"]["data"]["location"] == str(site / "agentfare")
        assert by_id["price_table"]["data"]["source"] == "builtin" and by_id["price_table"]["data"]["path"].startswith(str(site))
        prices = agentfare("prices", "--json")
        assert prices.returncode == 0 and len(json.loads(prices.stdout)["models"]) > 20
        report = agentfare("report", "--format", "md", "-o", "-")  # the template is found and rendered, on an empty database
        assert report.returncode == 0, report.stderr
        assert (site / "agentfare" / "demo_data").is_dir() == (PACKAGE / "demo_data").is_dir()


@pytest.fixture(scope="module")
def sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_with_setuptools(tmp_path_factory, "sdist")


class TestSdist:
    """The CI job ``package`` runs the whole suite from an unpacked sdist, so the sdist must carry every file that a test or a
    reader opens: the second README, the community files and every image and page the READMEs link to."""

    def names(self, sdist: Path) -> set[str]:
        with tarfile.open(sdist) as tar:
            return {n.split("/", 1)[1] for n in tar.getnames() if "/" in n}

    def test_it_carries_every_file_at_the_root_of_the_tree(self, sdist: Path) -> None:
        names = self.names(sdist)
        at_root = {p.name for p in ROOT.iterdir() if p.is_file() and p.name != ".gitignore"}
        assert sorted(at_root - names) == [], "add them to MANIFEST.in: the unpacked sdist runs the suite and these are read by it"

    def test_it_carries_what_both_readmes_link_to_and_show(self, sdist: Path) -> None:
        names = self.names(sdist)
        for readme in ("README.md", "README.pt-BR.md"):
            text = (ROOT / readme).read_text(encoding="utf-8")
            targets = re.findall(r"\]\(([^)\s]+)", text) + re.findall(r'(?:src|href)="([^"]+)"', text)
            local = {t.split("#", 1)[0].rstrip("/") for t in targets if not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", t) and not t.startswith("#")}
            missing = sorted(t for t in local if t and t not in names and not any(n.startswith(t + "/") for n in names))
            assert missing == [], f"{readme} links to files the sdist lacks: {missing}"

    def test_it_holds_the_package_data_and_no_build_leftovers(self, sdist: Path) -> None:
        names = self.names(sdist)
        assert "src/agentfare/data/pricing.yaml" in names and "src/agentfare/demo_data/config.yaml" in names
        assert not any("__pycache__" in n or n.endswith((".pyc", ".db")) for n in names)


# ---- the verified commands, run for real ------------------------------------------------------------------------------------------
REAL = os.environ.get("AGENTFARE_TEST_INSTALL") == "1"
needs_real = pytest.mark.skipif(not REAL, reason="set AGENTFARE_TEST_INSTALL=1 to run the real installs (they fetch the dependencies from the network)")


def run(*args: str | Path, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """A command in a child process. Without ``env`` the child gets this process' environment minus what would let the
    program being installed leak in from the one running the tests (PYTHONPATH, the active virtual environment)."""
    if env is None:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME") and not k.startswith("PIP_")}
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=600, env=env, cwd=cwd)


@needs_real
class TestRealInstalls:
    """Each test is one of the install paths of the README, with the exact commands that were verified.

    * ``python3 -m venv .venv && .venv/bin/pip install agentfare-X.Y.Z-py3-none-any.whl`` (a clone is installed the same way
      with ``pip install .``, and ``pip install git+https://github.com/FernandoSLuz/agentfare`` builds that wheel itself)
    * ``uv tool install agentfare-X.Y.Z-py3-none-any.whl`` (the same with a path to the clone, or a ``git+https`` address)
    * ``pipx install ...`` does what the first one does: one venv for the program and its command linked into a folder on PATH
    """

    def check_program(self, command: Path, tmp_path: Path, *, path: str) -> None:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        work = tmp_path / "elsewhere"
        work.mkdir(exist_ok=True)
        env = clean_environment(home, AGENTFARE_HOME=str(home / ".agentfare"))
        env["PATH"] = path
        version = run(command, "--version", env=env, cwd=work)
        assert version.returncode == 0 and version.stdout.strip() == f"agentfare {PROJECT['version']}", version.stderr
        assert run(command, "--help", env=env, cwd=work).returncode == 0
        doctor = run(command, "doctor", "--json", env=env, cwd=work)
        assert doctor.returncode == 0, doctor.stdout + doctor.stderr
        package = {c["id"]: c for c in json.loads(doctor.stdout)["checks"]}["package"]
        assert package["status"] == "ok" and package["data"]["installed"] is True and package["data"]["editable"] is False
        dry = run(command, "init", "--dry-run", env=env, cwd=work)
        assert dry.returncode == 0 and "Dry run" in dry.stdout
        assert not (home / ".agentfare").exists()

    def test_venv_and_pip_install_upgrade_uninstall(self, wheel: Path, tmp_path: Path) -> None:
        venv = tmp_path / "venv"
        assert run(sys.executable, "-m", "venv", venv).returncode == 0
        pip, command = venv / "bin" / "pip", venv / "bin" / "agentfare"
        installed = run(pip, "install", "--disable-pip-version-check", wheel)
        assert installed.returncode == 0, installed.stderr
        self.check_program(command, tmp_path, path=f"{venv / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin")
        again = run(pip, "install", "--disable-pip-version-check", "--upgrade", wheel)
        assert again.returncode == 0 and "already installed with the same version" in again.stdout  # an upgrade to the same version changes nothing
        assert run(pip, "uninstall", "-y", "agentfare").returncode == 0
        assert not command.exists()
        assert not (tmp_path / "home" / ".agentfare").exists()  # uninstalling never touches the data directory

    def test_pip_install_from_a_clone_directory(self, tmp_path: Path) -> None:
        copy = tmp_path / "clone"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".venv", "venv", ".git", "__pycache__", "build", "dist", "*.egg-info", ".pytest_cache"))
        venv = tmp_path / "venv"
        assert run(sys.executable, "-m", "venv", venv).returncode == 0
        done = run(venv / "bin" / "pip", "install", "--disable-pip-version-check", copy)
        assert done.returncode == 0, done.stderr
        self.check_program(venv / "bin" / "agentfare", tmp_path, path=f"{venv / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin")

    @pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
    def test_uv_tool_install_upgrade_uninstall(self, wheel: Path, tmp_path: Path) -> None:
        tools, bin_dir = tmp_path / "uv-tools", tmp_path / "uv-bin"
        env = {**clean_environment(tmp_path / "uv-home"), "UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(bin_dir)}
        installed = run("uv", "tool", "install", wheel, env=env)
        assert installed.returncode == 0, installed.stderr
        self.check_program(bin_dir / "agentfare", tmp_path, path=f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
        upgraded = run("uv", "tool", "upgrade", "agentfare", env=env)
        assert upgraded.returncode == 0 and "Nothing to upgrade" in upgraded.stderr + upgraded.stdout
        assert run("uv", "tool", "uninstall", "agentfare", env=env).returncode == 0
        assert not (bin_dir / "agentfare").exists()

    def test_the_pipx_way_is_a_venv_and_a_link_on_path(self, wheel: Path, tmp_path: Path) -> None:
        """pipx is not needed to know what it does: one venv per program, its command linked into a folder on PATH."""
        venv, links = tmp_path / "pipx-venv", tmp_path / "pipx-bin"
        links.mkdir()
        assert run(sys.executable, "-m", "venv", venv).returncode == 0
        assert run(venv / "bin" / "pip", "install", "--disable-pip-version-check", wheel).returncode == 0
        (links / "agentfare").symlink_to(venv / "bin" / "agentfare")
        self.check_program(links / "agentfare", tmp_path, path=f"{links}{os.pathsep}/usr/bin{os.pathsep}/bin")
        doctor = run(links / "agentfare", "doctor", "--json", env={**clean_environment(tmp_path / "home"), "PATH": f"{links}{os.pathsep}/usr/bin"})
        command = {c["id"]: c for c in json.loads(doctor.stdout)["checks"]}["command"]
        assert command["status"] == "ok", command  # the link is not mistaken for a second install that shadows this one

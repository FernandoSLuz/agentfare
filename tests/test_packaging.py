"""The release machinery, checked where it can be checked offline: the dependency floors and the job that proves them, the
pinned CI actions, and what the source distribution carries."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
pytestmark = pytest.mark.skipif(not CI.is_file(), reason="needs a checkout with .github (an installed copy has none)")


def workflow() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def name_of(requirement: str) -> str:
    return re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def floors() -> dict[str, str]:
    """``{name: version}`` for every dependency of pyproject.toml written as ``name>=version``."""
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    out = {}
    for requirement in meta["project"]["dependencies"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)\s*>=\s*([0-9][0-9A-Za-z.]*)(?:\s*,\s*(?:<|!=)[0-9][0-9A-Za-z.]*)*", requirement.strip())
        assert match, f"write dependencies as name>=floor (and <, != only where a break is known): {requirement}"
        out[name_of(match[1])] = match[2]
    return out


def as_tuple(version: str) -> tuple[int, ...]:
    """``0.24`` and ``0.24.0`` are the same version."""
    numbers = [int(part) for part in re.findall(r"\d+", version)]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def pins() -> dict[str, str]:
    out = {}
    for line in (ROOT / "constraints-min.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][0-9A-Za-z.]*)", line)
            assert match, f"constraints-min.txt pins exact versions, got {line!r}"
            out[name_of(match[1])] = match[2]
    return out


class TestDependencyFloors:
    def test_every_direct_dependency_has_a_floor_and_a_pin_at_that_floor(self) -> None:
        declared, pinned = floors(), pins()
        assert {"typer", "rich", "pyyaml", "jinja2"} <= set(declared), declared
        for name, version in declared.items():
            assert name in pinned and as_tuple(pinned[name]) == as_tuple(version), f"constraints-min.txt must pin {name}=={version}, the floor in pyproject.toml"

    def test_the_floors_are_not_the_broken_ones(self) -> None:
        """typer 0.12 with the click that pip resolves prints only the version (0.16 is the first that works with it), and with a
        click older than 8.2.1 the suite fails: the floor of typer must be past the first, and the click that goes with it not below the second."""
        assert as_tuple(floors()["typer"]) >= (0, 16)
        assert as_tuple(pins().get("click", floors().get("click", "0"))) >= (8, 2, 1)

    def test_the_one_typer_release_that_ignores_option_environment_variables_is_excluded(self) -> None:
        """typer 0.26.0 drops the environment variable of an option, so AGENTFARE_CONFIG would silently not work."""
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        [requirement] = [r for r in meta["project"]["dependencies"] if name_of(r) == "typer"]
        assert "!=0.26.0" in requirement.replace(" ", "")

    def test_only_constrained_packages_are_named_in_the_constraints(self) -> None:
        declared = set(floors())
        extra = set(pins()) - declared
        assert extra <= {"click", "markupsafe", "markdown-it-py", "pygments", "shellingham", "mdurl"}, extra  # transitive pins, each needed

    def test_an_upper_bound_is_written_only_with_a_reason(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for line in re.findall(r'^\s*"([^"]*(?:<|!=)[^"]*)",?\s*(#.*)?$', text, re.M):
            assert line[1], f"explain the upper bound or exclusion on {line[0]} in a comment"


class TestWorkflow:
    def text(self) -> str:
        return CI.read_text(encoding="utf-8")

    def test_every_action_is_a_full_sha_with_its_release_in_a_comment(self) -> None:
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", self.text(), re.M)
        assert uses
        for ref, rest in uses:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"pin {ref} to a full commit SHA"
            assert re.fullmatch(r"\s*# v\d+\.\d+\.\d+\s*", rest), f"say which release {ref} is: '# vX.Y.Z'"

    def test_no_job_keeps_the_git_credentials_after_checkout(self) -> None:
        for job_name, job in workflow()["jobs"].items():
            checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
            assert checkouts, job_name
            assert all(s.get("with", {}).get("persist-credentials") is False for s in checkouts), job_name

    def test_the_token_can_only_read(self) -> None:
        doc = workflow()
        assert doc["permissions"] == {"contents": "read"}
        assert not any("permissions" in job for job in doc["jobs"].values()), "a job may not widen the token"

    def test_a_job_installs_the_floor_versions_on_the_oldest_python_and_runs_the_suite(self) -> None:
        jobs = workflow()["jobs"]
        [(name, job)] = [(n, j) for n, j in jobs.items() if "constraints-min.txt" in yaml.safe_dump(j)]
        steps = job["steps"]
        python = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python@"))
        floor = re.search(r'^requires-python\s*=\s*">=(\d+\.\d+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
        assert floor and python["with"]["python-version"] == floor[1], name
        script = "\n".join(s.get("run", "") for s in steps)
        assert "pip install -c constraints-min.txt -e '.[dev]'" in script
        assert script.index("constraints-min.txt") < script.index("pytest")

    def test_the_yaml_is_valid_and_quotes_the_python_versions(self) -> None:
        doc = workflow()
        assert {"test", "oldest-dependencies"} <= set(doc["jobs"])
        assert all(isinstance(v, str) for v in doc["jobs"]["test"]["strategy"]["matrix"]["python-version"])


class TestOldestPython:
    """CI runs the suite on Python 3.11, and `pyproject.toml` promises it. Python 3.12 allows an f-string to reuse its own quote,
    hold a backslash or span lines inside the braces, so code written on a newer Python can be a syntax error on 3.11 without
    anything on that Python telling the author. `test_assets.py` loads `scripts/make_assets.py`, so one such line stops the whole
    run on 3.11 at collection."""

    def sources(self) -> list[Path]:
        return sorted(p for folder in ("src", "scripts", "tests") for p in (ROOT / folder).rglob("*.py") if "__pycache__" not in p.parts)

    @staticmethod
    def fstring_problems(path: Path) -> list[str]:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, str(path), feature_version=(3, 11))
        problems = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            opening = re.match(r"[rRbBuUfF]*(\'\'\'|\"\"\"|\'|\")", ast.get_source_segment(source, node) or "")
            if not opening:
                continue
            quote = opening[1]
            for part in node.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                expr = ast.get_source_segment(source, part.value) or ""
                for reason, found in (("a backslash", "\\" in expr), (f"the quote {quote} again", quote in expr), ("a line break", "\n" in expr and len(quote) == 1)):
                    if found:
                        problems.append(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}:{part.lineno}: an f-string expression holds {reason}, which Python 3.11 rejects: {expr[:60]!r}")
        return problems

    def test_no_file_uses_an_f_string_form_that_python_3_11_rejects(self) -> None:
        problems = [problem for path in self.sources() for problem in self.fstring_problems(path)]
        assert problems == [], "\n".join(problems)

    def test_the_check_finds_what_it_is_meant_to_find(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.py"
        bad.write_text("head = True\nx = f'<a{\" b\\\"c\" if head else \"\"}>'\ny = f\"{ {'k': 1}[\"k\"] }\"\n", encoding="utf-8")
        found = self.fstring_problems(bad) if sys.version_info >= (3, 12) else ["not checkable here"]
        assert found, "an f-string with a backslash or its own quote inside the braces should be reported"


class TestSourceDistribution:
    def manifest(self) -> str:
        return (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    def test_the_manifest_carries_what_the_tests_and_the_reader_need(self) -> None:
        text = self.manifest()
        for wanted in ("README.md", "LICENSE", "demo.tape", "constraints-min.txt", "graft docs", "graft scripts", "graft tests"):
            assert wanted in text, wanted
        assert "src/agentfare/data/pricing.yaml" in text  # the real file behind the pricing.yaml link at the root

    def test_everything_the_manifest_names_exists(self) -> None:
        for line in self.manifest().splitlines():
            line = line.split("#", 1)[0].strip()
            words = line.split()
            if not words or words[0] not in ("include", "graft"):
                continue
            for target in words[1:]:
                assert (ROOT / target).exists(), f"MANIFEST.in names {target}, which is not in the tree"

    def test_the_root_pricing_file_is_the_packaged_one(self) -> None:
        assert (ROOT / "pricing.yaml").read_bytes() == (ROOT / "src" / "agentfare" / "data" / "pricing.yaml").read_bytes()

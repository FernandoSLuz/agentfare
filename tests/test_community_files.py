"""The files a public repository needs besides the code: CHANGELOG.md, SECURITY.md, CODE_OF_CONDUCT.md, the issue forms, the pull
request template and the Dependabot config.

They are checked for what GitHub needs from them (valid YAML, the keys of an issue form, a schedule Dependabot understands), for what
the project promises in them (a private way to report a vulnerability, the Keep a Changelog layout, the Contributor Covenant
attribution, a bug form that asks for `agentfare doctor --json` and warns against pasting logs, a pull request checklist that is the one
of CONTRIBUTING.md), for the rule that no e-mail address was invented, and for links: relative links must resolve, and so must every
link to this repository's own files or README sections that is written with the full GitHub address.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentfare import __version__

ROOT = Path(__file__).resolve().parent.parent
GITHUB = ROOT / ".github"
FORMS = GITHUB / "ISSUE_TEMPLATE"
REPO = "https://github.com/FernandoSLuz/agentfare"
pytestmark = pytest.mark.skipif(not GITHUB.is_dir(), reason="needs a checkout with .github (an installed copy has none)")

CHANGELOG = ROOT / "CHANGELOG.md"
SECURITY = ROOT / "SECURITY.md"
CONDUCT = ROOT / "CODE_OF_CONDUCT.md"
PULL_REQUEST = GITHUB / "PULL_REQUEST_TEMPLATE.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
ALL_TEXT = [CHANGELOG, SECURITY, CONDUCT, PULL_REQUEST, CONTRIBUTING, ROOT / "README.md", ROOT / "README.pt-BR.md", *sorted(FORMS.glob("*.yml")), GITHUB / "dependabot.yml"]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load(path: Path) -> Any:
    return yaml.safe_load(text(path))


def headings(markdown: str) -> set[str]:
    """The anchors GitHub gives the headings of a markdown file."""
    markdown = re.sub(r"^```.*?^```", "", markdown, flags=re.M | re.S)
    found, seen = set(), {}
    for title in re.findall(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", markdown, re.M):
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title).replace("`", "").lower()
        slug = "".join(c for c in title if c.isalnum() or c in " -_").replace(" ", "-")
        seen[slug] = seen.get(slug, -1) + 1
        found.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
    return found


# ---- CHANGELOG.md ---------------------------------------------------------------------------------------------------
class TestChangelog:
    def test_it_follows_keep_a_changelog(self) -> None:
        body = text(CHANGELOG)
        assert body.startswith("# Changelog\n")
        assert "keepachangelog.com" in body and "semver.org" in body
        assert "## [Unreleased]" in body
        types = set(re.findall(r"^### (.+)$", body, re.M))
        assert types <= {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}, f"Keep a Changelog has no section named {types}"

    def test_the_newest_release_is_the_version_of_the_package_and_has_a_date(self) -> None:
        versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$", text(CHANGELOG), re.M)
        assert versions, "no released version is listed"
        assert versions[0][0] == __version__, "the newest entry of the changelog is not the version of the package"
        assert versions[0] == ("0.1.0", "2026-09-30") or versions[0][0] != "0.1.0"
        dates = [date for _, date in versions]
        assert dates == sorted(dates, reverse=True), "newest release first"

    def test_the_first_release_summarises_what_ships(self) -> None:
        entry = text(CHANGELOG).split("## [0.1.0]")[1]
        for needle in ("Claude Code", "Codex CLI", "Gemini CLI", "SQLite", "micro-cents", "agentfare init", "agentfare doctor", "agentfare demo",
                       "advise", "run", "report", "README.pt-BR.md", "GitHub", "uv"):
            assert needle in entry, f"the 0.1.0 entry should mention {needle}"

    def test_every_version_has_a_link_definition_to_this_repository(self) -> None:
        body = text(CHANGELOG)
        for version in re.findall(r"^## \[([^\]]+)\]", body, re.M):
            definition = re.search(rf"^\[{re.escape(version)}\]: (\S+)$", body, re.M)
            assert definition, f"[{version}] has no link definition at the bottom"
            assert definition[1].startswith(REPO + "/"), definition[1]
        assert f"{REPO}/compare/v{__version__}...HEAD" in body


# ---- SECURITY.md ----------------------------------------------------------------------------------------------------
class TestSecurityPolicy:
    def test_it_lists_the_supported_versions(self) -> None:
        major, minor = __version__.split(".")[:2]
        body = text(SECURITY)
        assert "## Supported versions" in body and f"{major}.{minor}.x" in body

    def test_it_reports_privately_through_github_and_says_not_to_open_a_public_issue(self) -> None:
        body = text(SECURITY)
        assert "Report a vulnerability" in body and f"{REPO}/security" in body
        assert "do not open a public issue" in body.lower()

    def test_it_names_what_is_in_scope(self) -> None:
        body = text(SECURITY).lower()
        scope = body.split("## what is in scope")[1].split("## what is out of scope")[0]
        for needle in ("log parsing", "file handling", "prices --refresh", "network"):
            assert needle in scope, f"the scope should cover {needle}"
        assert "## what is out of scope" in body

    def test_it_states_its_response_expectations_modestly(self) -> None:
        body = text(SECURITY).lower()
        expectations = body.split("## what to expect")[1]
        assert "spare time" in expectations and "no bug bounty" in expectations
        assert "guarantee" not in expectations and "24 hours" not in expectations

    def test_it_tells_people_not_to_send_their_own_logs(self) -> None:
        assert "do not send logs from your own agents" in text(SECURITY).lower()


# ---- CODE_OF_CONDUCT.md ---------------------------------------------------------------------------------------------
class TestCodeOfConduct:
    def test_it_is_the_contributor_covenant_2_1_and_says_so(self) -> None:
        body = text(CONDUCT)
        assert "Contributor Covenant" in body and "version 2.1" in body
        assert "https://www.contributor-covenant.org/version/2/1/code_of_conduct" in body
        assert "CC BY 4.0" in body, "the Covenant is CC BY 4.0: say so"

    def test_it_has_the_sections_of_the_covenant(self) -> None:
        body = text(CONDUCT)
        for section in ("Our pledge", "Our standards", "Enforcement responsibilities", "Scope", "Enforcement", "Enforcement guidelines", "Attribution"):
            assert f"## {section}\n" in body, section
        for step in ("Correction", "Warning", "Temporary ban", "Permanent ban"):
            assert step in body

    def test_reports_go_through_github_and_no_address_was_invented(self) -> None:
        enforcement = text(CONDUCT).split("## Enforcement\n")[1].split("## Enforcement guidelines")[0]
        assert "GitHub" in enforcement and f"{REPO}/security" in enforcement
        assert not EMAIL.search(text(CONDUCT)), "a code of conduct without a mailbox must not invent one"


# ---- issue forms ---------------------------------------------------------------------------------------------------
FORM_FILES = sorted(p for p in FORMS.glob("*.yml") if p.name != "config.yml")


class TestIssueForms:
    def test_there_is_a_bug_report_and_a_feature_request(self) -> None:
        assert [p.name for p in FORM_FILES] == ["bug_report.yml", "feature_request.yml"]

    @pytest.mark.parametrize("path", FORM_FILES, ids=lambda p: p.name)
    def test_a_form_has_what_github_requires(self, path: Path) -> None:
        form = load(path)
        assert {"name", "description", "body"} <= set(form), "an issue form needs name, description and body"
        assert isinstance(form["body"], list) and form["body"]
        assert isinstance(form.get("labels"), list) and form["labels"]
        ids = []
        for field in form["body"]:
            assert field["type"] in {"markdown", "textarea", "input", "dropdown", "checkboxes"}, field["type"]
            if field["type"] != "markdown":
                assert field["id"] not in ids, f"duplicate id {field['id']}"
                ids.append(field["id"])
                assert field["attributes"].get("label") or field["type"] == "checkboxes"
            if field["type"] == "dropdown":
                assert len(field["attributes"]["options"]) >= 2
            if field["type"] == "checkboxes":
                assert all("label" in option for option in field["attributes"]["options"])
        assert any(f.get("validations", {}).get("required") for f in form["body"]), "ask for at least one thing"
        assert all(re.fullmatch(r"[a-z0-9_-]+", i) for i in ids), "ids are plain words"

    def test_the_bug_form_asks_for_doctor_output_and_the_environment(self) -> None:
        form = load(FORMS / "bug_report.yml")
        labels = " | ".join(str(f.get("attributes", {}).get("label", "")) for f in form["body"])
        for needle in ("agentfare doctor --json", "AgentFare version", "installed", "Operating system", "Agents and their versions", "Steps to reproduce"):
            assert needle in labels, f"the bug form should ask for: {needle}"
        doctor = next(f for f in form["body"] if f.get("id") == "doctor")
        assert doctor["validations"]["required"] is True and doctor["attributes"]["render"] == "json"

    def test_the_bug_form_warns_against_pasting_logs_and_asks_for_redaction(self) -> None:
        form = load(FORMS / "bug_report.yml")
        intro = next(f for f in form["body"] if f["type"] == "markdown")["attributes"]["value"]
        assert "Never paste your agent logs" in intro
        doctor = next(f for f in form["body"] if f.get("id") == "doctor")
        assert "user name" in doctor["attributes"]["description"], "doctor --json can hold paths: say so"
        privacy = next(f for f in form["body"] if f.get("id") == "privacy")
        assert privacy["attributes"]["options"][0]["required"] is True
        assert "SECURITY.md" in intro, "a vulnerability does not go in a public issue"

    def test_the_feature_form_states_the_rules_of_the_project(self) -> None:
        body = text(FORMS / "feature_request.yml")
        for needle in ("local-first", "prices --refresh", "prompts, replies or file contents", "micro-cents", "LOG-FORMATS.md"):
            assert needle in body, needle

    def test_blank_issues_are_off_and_the_contact_links_help(self) -> None:
        config = load(FORMS / "config.yml")
        assert config["blank_issues_enabled"] is False
        links = config["contact_links"]
        assert links and all({"name", "url", "about"} <= set(link) and link["url"].startswith("https://") for link in links)
        urls = [link["url"] for link in links]
        assert f"{REPO}#faq-and-troubleshooting" in urls, "point to the troubleshooting section of the README"
        assert f"{REPO}/security/advisories/new" in urls


# ---- pull request template -----------------------------------------------------------------------------------------
class TestPullRequestTemplate:
    def checklist(self, path: Path, heading: str) -> list[str]:
        return re.findall(r"^- \[ \] (.+)$", text(path).split(heading)[1], re.M)

    def test_the_checklist_is_the_one_of_contributing(self) -> None:
        template = self.checklist(PULL_REQUEST, "## Checklist")
        contributing = self.checklist(CONTRIBUTING, "## Pull request checklist")
        assert template == contributing, "the pull request template and CONTRIBUTING.md must list the same items, in the same order"
        assert len(template) >= 10

    def test_it_covers_tests_invariants_privacy_and_docs(self) -> None:
        body = text(PULL_REQUEST)
        for needle in ("pytest", "PRIVACY-CANARY", "Decimal", "network", "PARSERS_VERSION", "SCHEMA_VERSION", "verified price", "test_readme.py --write",
                       "README.pt-BR.md", "CHANGELOG.md", "make_assets.py"):
            assert needle in body, needle


# ---- dependabot -----------------------------------------------------------------------------------------------------
class TestDependabot:
    def test_it_updates_github_actions_and_pip_weekly_and_quietly(self) -> None:
        config = load(GITHUB / "dependabot.yml")
        assert config["version"] == 2
        updates = {u["package-ecosystem"]: u for u in config["updates"]}
        assert set(updates) == {"github-actions", "pip"}
        for ecosystem, update in updates.items():
            assert update["directory"] == "/", ecosystem
            assert update["schedule"]["interval"] == "weekly", ecosystem
            assert 1 <= update["open-pull-requests-limit"] <= 5, f"{ecosystem}: keep it quiet"

    def test_the_workflow_it_updates_exists(self) -> None:
        assert list((GITHUB / "workflows").glob("*.yml"))


# ---- no invented contact, links that resolve ------------------------------------------------------------------------
@pytest.mark.parametrize("path", ALL_TEXT, ids=lambda p: p.name)
def test_no_file_carries_an_invented_email_address(path: Path) -> None:
    found = [m for m in EMAIL.findall(text(path)) if not m.endswith("users.noreply.github.com")]
    assert not found, f"{path.name} names an e-mail address: {found}"


REPO_LINKS = sorted(
    {
        (path.name, link)
        for path in ALL_TEXT
        for link in re.findall(rf"{re.escape(REPO)}(?:/blob/main/[^\s)\"'>]+|#[A-Za-z0-9_-]+)", text(path))
    }
)


@pytest.mark.parametrize(("name", "link"), REPO_LINKS, ids=[f"{n} {link[len(REPO):]}" for n, link in REPO_LINKS])
def test_a_link_to_this_repository_points_at_a_file_or_a_heading_that_exists(name: str, link: str) -> None:
    rest = link[len(REPO):]
    if rest.startswith("#"):
        assert rest[1:] in headings(text(ROOT / "README.md")), f"{name}: the README has no heading {rest}"
        return
    path, _, anchor = rest[len("/blob/main/"):].partition("#")
    assert (ROOT / path).is_file(), f"{name}: {path} does not exist"
    if anchor and path.endswith(".md"):
        assert anchor in headings(text(ROOT / path)), f"{name}: {path} has no heading #{anchor}"


def test_there_are_repository_links_to_check() -> None:
    assert len(REPO_LINKS) >= 5


def test_every_markdown_file_here_ends_with_a_newline_and_has_no_trailing_spaces() -> None:
    for path in (CHANGELOG, SECURITY, CONDUCT, PULL_REQUEST):
        body = text(path)
        assert body.endswith("\n") and not body.endswith("\n\n"), path.name
        assert not [n for n, line in enumerate(body.split("\n"), 1) if line != line.rstrip()], f"{path.name} has spaces at the end of a line"

# Contributing

AgentFare is a few readable modules, integer money and tests that pin every rule. This page is the short version, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains the modules and the invariants in full. By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md). A security problem goes through [SECURITY.md](SECURITY.md), not a public issue.

## Dev setup

You need Python 3.11 or newer.

```sh
git clone https://github.com/FernandoSLuz/agentfare
cd agentfare
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
agentfare --help
pytest
```

### Running the suite

The whole suite takes a minute or two. `pytest tests/test_router.py -q` runs one file, and `pytest -k unverified` runs the tests whose names match. Run the whole suite once before you open a pull request, because the docs, the packaging and the privacy tests read files all over the tree.

Some tests only run where a tool exists: the ones that build the images need Chromium, Pillow, ffmpeg and `uv` (see [Redraw the images](#redraw-the-images)), and `AGENTFARE_TEST_INSTALL=1 pytest tests/test_install_paths.py` also runs real `pip` and `uv tool` installs of a freshly built wheel, which needs the network for the dependencies.

The dependency floors in `pyproject.toml` are promises, and CI keeps them: the `oldest-dependencies` job installs exactly the versions of `constraints-min.txt` on Python 3.11 (`pip install -c constraints-min.txt -e '.[dev]'`) and runs this same suite. When you start using something from a newer typer, rich, PyYAML or Jinja2, raise the floor and the pin together; `tests/test_packaging.py` fails when they disagree. To reproduce the job locally, make a Python 3.11 virtual environment and run that command in it. The workflow pins each action to a full commit SHA with its release in a comment: look a new release up with `gh api repos/<owner>/<repo>/git/ref/tags/<tag>` and change both.

Tests never touch the network or your real `~/.agentfare`, `~/.claude`, `~/.codex` or `~/.gemini`: `tests/conftest.py` gives every test a throwaway `HOME` and `AGENTFARE_HOME`. Drive the CLI with `typer.testing.CliRunner`, or with a subprocess for output that must match byte for byte, against `tests/fixtures/config.yaml` with `AGENTFARE_NOW=2026-09-30T12:00:00Z`, a temporary `AGENTFARE_HOME` and a pinned `COLUMNS` where width matters. Never weaken a test to make it pass.

Two test files hold the numbers worked out by hand. `tests/test_e2e_fixtures.py` runs the real CLI on the three sample sessions and asserts the figures of `tests/fixtures/EXPECTED.md`, which `scripts/verify_fixtures.py` re-derives without importing agentfare (`python scripts/verify_fixtures.py --check` fails when the document drifts). `tests/test_e2e_slots.py` does the same for sessions that cross a day or a week, in UTC and in another timezone. Before you open a pull request, `scripts/verify_fresh_clone.sh --worktree` installs a copy of your working tree into a fresh virtual environment and runs the demo and the suite there.

## The invariants

Break one only with a test that explains why. The reasons are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#invariants).

- Prompt text, replies, tool inputs and outputs, and file contents are never stored, printed or written to a report. Real agent logs are private: nothing from your own logs goes into a commit, a fixture, a test or a doc. In an example path use an invented home directory such as `/home/dev`: `tests/test_hygiene.py` fails on any other home directory and on the user name of the machine that runs the tests.
- Money is integers (micro-cents), prices are `Decimal`, and no `float` touches an amount.
- A model without a verified price stays out of dollar totals, and every command that shows money warns about it.
- Only `agentfare prices --refresh` uses the network, and only `pricefetch.py` may import a network module.
- Every parser maps into the five `Usage` buckets, and a response the agent logged twice is counted once.
- Money is attributed to when it was spent. A parser fills `SessionRecord.slots` from each response's own timestamp (per model the slots add up to `usage`), so a session that spans midnight or a week boundary is split between the days and weeks, and every command shows the same total for the same period. Never count `aggregate.Row`s as sessions, never read a session's start as "when it was spent", and leave a session's counters and score whole-session.
- A change to what any parser produces for the same log needs `parsers.PARSERS_VERSION` bumped, so that databases already filled are re-read. Changing the database schema needs `db.SCHEMA_VERSION` bumped and an in-place upgrade with a test.
- What AgentFare creates is private and what it reads may be hostile. Write files through `safeio` (directories 0700, files 0600, nothing written through a planted link), run YAML from a file through `safeyaml.check_expansion`, read only regular files as logs, and send every name from a log, a server or an error message through `textsafe.clean_text` before it reaches a terminal or a page.
- Output is deterministic under `AGENTFARE_NOW`, `AGENTFARE_HOME` and the config `timezone`.

## Add a model price

1. Read the price on the provider's own pricing or model page. Do not copy it from an aggregator.
2. Add a block to `src/agentfare/data/pricing.yaml`. Prices are USD per 1M tokens. `input`, `cached_input` and `output` are required, and each is a number or the word `unverified`. `cache_write` (5-minute writes) and `cache_write_1h` are optional, and a price left out is billed at the input price. `source` is the page you read, `checked` is the date you read it, and `note` holds the tier rules, promotions and shutdown dates, with their dates.

   ```yaml
   my-model:
     provider: openai
     input: 1.0
     cached_input: 0.10
     output: 4.0
     source: https://example.com/pricing
     checked: 2026-09-30
     note: Above 200K input tokens the whole request costs 2x.
   ```

3. A price you could not read is `unverified` for all three required fields, so the model is left out of dollar totals and named in a warning.
4. A second spelling of the id (a dated or cloud-vendor name) goes under `aliases:`.
5. Run `pytest tests/test_pricing_table.py tests/test_core_pricing.py`, then `python tests/test_readme.py --write`, which rebuilds the price table of the README. Record what you read in [docs/PRICING-SOURCES.md](docs/PRICING-SOURCES.md).

## Add a parser

An agent is one module in `src/agentfare/parsers/` with a module-level `PARSER`. [docs/LOG-FORMATS.md](docs/LOG-FORMATS.md#adding-an-agent) has the contract, the wiring steps and what the tests must cover.

## Add a command

1. Create `src/agentfare/commands/<name>.py` with a `register(app)` function, and add the module to the import and the registration loop in `cli.py`.
2. Build a plain dict of integers and strings first, and draw the text from that dict. `--json` prints the dict, so the two cannot disagree. Reuse the helpers in `render.py`: the shared options and `resolve_filters`, `print_table`, `emit_json` and `clean_errors`. Names from the logs go through `render.plain` or `render.clean_text`, an empty view through `render.print_empty`, and a table through `print_table` with `Col`s, which cuts and drops columns on a narrow terminal (see [Tables on a narrow terminal](docs/ARCHITECTURE.md#tables-on-a-narrow-terminal)).
3. Write tests against the fixtures. `tests/test_no_network.py` finds your command from the app and runs it under a guard that blocks the network, so it is covered without any change.
4. Show the command in a `sh` block of the README (`tests/test_readme.py` fails for a registered command the README never shows), with an output block if the output is worth showing (the marker format is in the docstring of `tests/test_readme.py`), and run `python tests/test_readme.py --write`. Translate the new text into `README.pt-BR.md`.
5. Add the command to the module table of [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and an entry to [CHANGELOG.md](CHANGELOG.md). A command that lists things supports `--json`, and a command that only looks (like `doctor`) must never write or open a log file.

## Keep the docs true

`tests/test_readme.py` runs the output blocks of the README, rebuilds its generated tables (prices, signals, tiers, run rules, config keys), parses every `agentfare` command in the docs and checks the relative links. If you change the output of a command, the price table or a config default, the test fails until you run:

```sh
python tests/test_readme.py --write
```

Review the diff it makes to `README.md`. A new config key also needs a line in `CONFIG_TEXT` in the same file.

`README.pt-BR.md` is the Brazilian Portuguese version of the README. `tests/test_readme_pt.py` checks that it has the same headings, and the same code blocks, images and links in the same order. After you change a code block, a generated table or an output block of the English file, copy them across and then translate the prose around them:

```sh
python tests/test_readme_pt.py --write
```

The community files (`CHANGELOG.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `.github/ISSUE_TEMPLATE/`, `.github/PULL_REQUEST_TEMPLATE.md` and `.github/dependabot.yml`) are checked by `tests/test_community_files.py`.

### Redraw the images

The screenshots, the GIF, the diagram and the banners in `docs/images/` are drawn by `scripts/make_assets.py` from the invented demo data, never from real logs. When you change what a command prints, redraw them:

```sh
python scripts/make_assets.py                     # every image
python scripts/make_assets.py --only init doctor  # only these
python scripts/make_assets.py --list              # the names and the files they make
```

The script needs Chromium, Pillow, ffmpeg, `rsvg-convert` and `uv`, none of which is a dependency of AgentFare. Run it with a Python that has Pillow and can import `agentfare`. A new screenshot is one line in the `SHOTS` table at the top of the script. [docs/DEMO.md](docs/DEMO.md) has the details, and `python scripts/make_demo_data.py` rewrites the demo data itself.

## Releasing

Build the release artifacts with `python -m build` in CI (the `package` job of `.github/workflows/ci.yml` does it from a clean checkout), not on a personal machine: a tarball made by hand carries the local user name and group of whoever packed it. If you must repack an sdist yourself, unpack it and pack it again with `tar --owner=0 --group=0 --numeric-owner`, so that the published file names nobody.

The release checklist:

1. Decide the version. It lives in `pyproject.toml` and in `src/agentfare/__init__.py`, and the README says it too (`The version is ...`), in both languages.
2. Move the entries of `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) under a new heading with the version and the date, start a new empty `Unreleased`, and update the two link definitions at the bottom.
3. Re-check the prices: `agentfare prices --refresh`, read each `changed?` against the provider's page, edit `pricing.yaml`, then run `python tests/test_readme.py --write` and update [docs/PRICING-SOURCES.md](docs/PRICING-SOURCES.md). Note any price that expires, with its date.
4. Redraw the images if any output changed (`python scripts/make_assets.py`), and look at them.
5. Run the whole suite, `scripts/verify_fresh_clone.sh --worktree` and, on Python 3.11, the oldest-dependencies install described under [Dev setup](#dev-setup).
6. Commit, push and wait for CI: the test matrix on Ubuntu and macOS, the `oldest-dependencies` job and the `package` job.
7. Tag the commit `vX.Y.Z`, push the tag and write the GitHub release from the changelog entry. Attach the sdist and the wheel that the `package` job built.
8. From a clean home, run the install commands of the README against the tag, then `agentfare --version`, `agentfare doctor` and `agentfare demo`.

AgentFare is not on PyPI. If that changes, both README files must say so in the same commit.

## Pull request checklist

- [ ] `pytest` passes.
- [ ] New behaviour has tests, and any log fixtures are synthetic, with a `PRIVACY-CANARY-*` marker in every content field.
- [ ] No prompt text, real project name or real log content in code, tests, fixtures or docs.
- [ ] Amounts stay integers and prices stay `Decimal`.
- [ ] Prices come from the provider's page, with `source` and `checked`.
- [ ] Nothing outside `pricefetch.py` touches the network.
- [ ] A change to what a parser produces bumped `parsers.PARSERS_VERSION`.
- [ ] A model without a verified price stays out of dollar totals and is named in a warning.
- [ ] A schema change bumped `db.SCHEMA_VERSION` and has an upgrade test.
- [ ] `python tests/test_readme.py --write` was run and its diff reviewed.
- [ ] The docs that describe the change are updated: `README.md`, `README.pt-BR.md`, `docs/` and `CHANGELOG.md`.
- [ ] If the output of a command changed, `python scripts/make_assets.py` redrew the images.

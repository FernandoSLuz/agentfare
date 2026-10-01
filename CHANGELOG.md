# Changelog

All notable changes to AgentFare are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the major version is 0, a minor release may change behaviour, and this file says so when it does.

## [Unreleased]

## [0.1.0] - 2026-09-30

The first public release. It is alpha: the commands, the `--json` keys and the database schema are tested and in use, and rough edges are likely. The README lists the known limitations under [Limitations](README.md#limitations): the savings are estimates, some calls an agent never logs are not counted, and AgentFare has been used on Linux only.

### Added

- Parsers for the session logs of Claude Code (checked against 2.1.284), Codex CLI (0.159.0) and Gemini CLI (0.60.0). Each one maps its agent's counters onto the same five token buckets, removes responses the agent logged twice and keeps no prompt, reply, tool input or output, or file content. The Gemini CLI parser was written from the CLI's own source, because no real conversation was available.
- A local SQLite database (schema version 3) with incremental ingest. A database from an older release is upgraded in place, and one from a newer release is refused and left alone. Everything AgentFare creates is private to the user: directories are `0700`, files are `0600`, and nothing is written through a planted symbolic link.
- Money as integer micro-cents, attributed to the 15-minute slot in which it was spent, so a session that runs across midnight or a week boundary is split between the days and weeks it ran in and every command shows the same total for the same period.
- A price table in YAML with the source page and the date of every price, the word `unverified` for a price the provider's page did not confirm, a user override file, and `agentfare prices --refresh`, which compares the table with the official pages and edits nothing. A model without a verified price stays out of every dollar total and is named in a warning.
- The commands `init`, `doctor`, `demo`, `ingest`, `config`, `summary`, `sessions`, `models`, `advise`, `run`, `report` and `prices`.
- `agentfare init`, a one-minute setup that finds the log folders of the three agents, reads them once and prints the total, the biggest saving and what to run next. `agentfare doctor`, a read-only checklist of the install, the config, the log folders, the database, the price table and the environment, with a fix for every line that is not ok. `agentfare demo`, which runs the real commands on 45 invented sessions packaged with AgentFare, with no log of the user's own and no network.
- `agentfare advise`, which scores each past session on five signals and re-prices it on a cheaper model when the session looks small enough, and `agentfare run`, which picks a model for a task from its prompt, the size of the repository and three families of keywords, shows the estimate against the usual model and starts the agent only after a confirmation. Every threshold, keyword and model is in the config.
- A weekly report as GitHub markdown or as one self-contained HTML page with no script and no network reference, in a dark and a light theme and with a phone layout. Project names appear as `Project A`, `Project B` unless `--show-projects` is given.
- `--json` on every reporting command with stable keys. Output that is not a terminal has no colour codes. Tab completion is available on request with `--install-completion`.
- Installation from GitHub with uv, pipx or pip, and from a clone.
- Documentation in English and Brazilian Portuguese (`README.md` and `README.pt-BR.md`), with screenshots, a GIF and a diagram drawn from the demo data, and `docs/` with the architecture, the log formats, the routing rules, the price sources and the demo.
- A test suite of more than 4,000 tests. It covers the parsers on synthetic logs in each agent's real format, the figures of the sample sessions worked out by hand, hostile files and YAML, the rule that no module but `pricefetch.py` can reach the network, and every figure, command and link in the README.
- Continuous integration on Ubuntu and macOS for Python 3.11 to 3.14, a job that installs the oldest supported dependencies, and a job that builds the sdist and the wheel and runs the suite from the unpacked sdist.
- Community files: a security policy, a code of conduct, issue forms for bugs and feature requests, a pull request template and Dependabot updates for GitHub Actions and pip.

[Unreleased]: https://github.com/FernandoSLuz/agentfare/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/FernandoSLuz/agentfare/releases/tag/v0.1.0

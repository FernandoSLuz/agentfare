# Security policy

## Supported versions

AgentFare is alpha software maintained by one person. Only the latest release receives security fixes.

| Version | Supported |
|---|---|
| 0.1.x | yes |
| older than 0.1.0 | no |

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Report it privately through GitHub:

1. Open the [Security tab of the repository](https://github.com/FernandoSLuz/agentfare/security).
2. Choose Report a vulnerability. This opens a private advisory that only you and the maintainer can read.
3. Say what you found, the AgentFare version (`agentfare --version`), your operating system and Python version, and the steps to reproduce it.

A log file that triggers the problem is the most useful thing to send. Do not send logs from your own agents, because they hold your prompts and file names. Describe the shape of the file, or build a small synthetic one in the format of [docs/LOG-FORMATS.md](docs/LOG-FORMATS.md) (the files in `tests/fixtures/` show the idea).

If private vulnerability reporting is not available when you try it, open an issue that says only that you have a security report and asks for a way to send it, and leave out the details.

## What is in scope

AgentFare reads files that other programs write and that can be hostile, and it writes files that should stay private. These are in scope:

- Log parsing. A crafted log file that crashes or hangs AgentFare, uses unbounded memory or time, writes anywhere but the data directory, puts terminal control sequences on your screen, or gets prompt text, replies or file contents into the database or a report.
- File handling. The config and price files (hostile YAML), the database, the report that `-o` writes, file modes, and writes through symbolic links or special files.
- `agentfare prices --refresh`. It is the only code that opens a network connection. Redirects to private or loopback hosts, unbounded downloads and anything that sends more than a plain request for a public pricing page are in scope.
- Any other command that opens a network connection, which the project rules forbid.

## What is out of scope

- Problems that need an attacker who already controls your account or your machine, such as someone who can edit your `~/.agentfare` directory or your shell startup files.
- Wrong cost figures. These are ordinary bugs, so open a regular issue and paste `agentfare doctor --json` after removing anything private.
- Vulnerabilities in Claude Code, Codex CLI, Gemini CLI, Python or the dependencies (Typer, Rich, PyYAML, Jinja2). Report those upstream.
- Pricing pages that change or disappear.

## What to expect

This is a spare time project, so these are hopes and not promises. I aim to reply within a week and to tell you within a month what I plan to do. A confirmed vulnerability is fixed in a new release, and the advisory is published after the fix, with credit to you if you want it. There is no bug bounty.

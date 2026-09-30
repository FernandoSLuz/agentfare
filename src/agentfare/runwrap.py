"""Reading an agent command line without running it: which agent, which prompt, which model, where a model flag goes.

Everything here is pure: an ``argv`` goes in, plain data comes out, nothing is executed or read from disk. The option
tables below were checked against ``--help`` of Claude Code 2.1.284, Codex CLI 0.159.0 (also ``exec``, ``exec resume``,
``exec review``, ``review``, ``resume`` and ``fork``) and Gemini CLI 0.60.0; an option a newer version adds is read as a
flag without a value. An agent that is not one of those three is read generically: only its model flag is recognised and
no prompt is extracted (``agentfare run --prompt`` supplies it).

An option that takes several values (``claude --allowedTools a b``, ``codex -i a b``) swallows the positional that
follows it, exactly as the agent's own parser does, so a prompt written after one is not seen as a prompt.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .config import ConfigError

TIER_ENV, MODEL_ENV = "AGENTFARE_TIER", "AGENTFARE_MODEL"
_OPTION_NAME = re.compile(r"--?[A-Za-z0-9][^\s=]*")


def is_option_name(text: str) -> bool:
    """``--model`` or ``-m``: what can go ahead of a value on a command line. Not a word (it would be read as a subcommand
    or a prompt), not ``-`` or ``--``, not ``--model=x``."""
    return _OPTION_NAME.fullmatch(text) is not None


# ---- which agent ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Agent:
    name: str
    model_flag: str | None  # None: the config says no flag is known, so a model cannot be handed over


def resolve_agent(agents: Any, command: str) -> Agent | None:
    """The configured agent whose ``commands`` list holds ``command`` (compared by basename); None for an unknown command."""
    if not isinstance(agents, Mapping):
        raise ConfigError("agents must map each agent name to its commands and model_flag")
    found: Agent | None = None
    wanted = os.path.basename(command)
    for name, spec in agents.items():
        if not isinstance(spec, Mapping):
            raise ConfigError(f"agents.{name} must be a mapping with commands and model_flag")
        commands = [spec["commands"]] if isinstance(spec.get("commands"), str) else spec.get("commands")
        if not isinstance(commands, list) or not commands or not all(isinstance(c, str) and c.strip() for c in commands):
            raise ConfigError(f"agents.{name}.commands must be a list of command names")
        flag = spec.get("model_flag")
        if flag is not None and not (isinstance(flag, str) and is_option_name(flag.strip())):
            raise ConfigError(f"agents.{name}.model_flag must be an option such as --model or -m, or null")
        if found is None and wanted in {os.path.basename(c.strip()) for c in commands}:
            found = Agent(str(name), flag.strip() if flag else None)
    return found


# ---- what each CLI accepts -----------------------------------------------------------------
@dataclass(frozen=True)
class Command:
    """One level of an agent CLI's command tree."""

    model: bool = True  # False: does not talk to a model (``claude mcp``, ``codex login``) or has no model option
    prompt: bool = False  # takes the task prompt as a positional argument
    commands: Mapping[str, "Command"] = field(default_factory=dict)


@dataclass(frozen=True)
class Syntax:
    root: Command = field(default_factory=Command)
    value: frozenset[str] = frozenset()  # options that take the next argument as their value
    variadic: frozenset[str] = frozenset()  # ... every following argument up to the next option
    optional: frozenset[str] = frozenset()  # ... the next argument, only when it is not an option
    model: frozenset[str] = frozenset()  # spellings of the model option (they take a value)
    model_config: frozenset[str] = frozenset()  # options whose ``model=NAME`` value selects the model (``codex -c``)
    prompt: frozenset[str] = frozenset()  # options whose value is the prompt (``gemini -p``)
    query_words: bool = False  # every positional is part of the prompt (gemini's ``[query..]``); else only the last


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


def _idle(text: str) -> dict[str, Command]:
    return {name: Command(model=False) for name in text.split()}


_CODEX_SESSIONS = {"resume": Command(), "fork": Command(), "review": Command()}  # `codex exec resume|fork|review`
_CODEX_EXEC = Command(prompt=True, commands=_CODEX_SESSIONS)

SYNTAX: dict[str, Syntax] = {
    "claude": Syntax(
        root=Command(prompt=True, commands=_idle(
            "agents attach auth auto-mode doctor gateway help import install logs mcp plugin plugins project respawn rm "
            "setup-token stop kill ultrareview update upgrade")),
        value=_words(
            "--agent --agents --append-system-prompt --append-system-prompt-file --autocompact --client-data-url "
            "--debug-file --effort --environment --fallback-model --input-format --json-schema --max-budget-usd -n --name "
            "--output-format --permission-mode --permission-prompts --plugin-dir --plugin-url "
            "--remote-control-session-name-prefix --session-id --setting-sources --settings --system-prompt "
            "--system-prompt-file --system-prompt-snapshot"),
        variadic=_words("--add-dir --allowedTools --allowed-tools --betas --disallowedTools --disallowed-tools --file --mcp-config --tools"),
        optional=_words("--cloud -d --debug --from-pr --prompt-suggestions --remote-control -r --resume --teleport -w --worktree"),
        model=_words("--model"),
    ),
    "codex": Syntax(
        root=Command(prompt=True, commands={
            "exec": _CODEX_EXEC, "e": _CODEX_EXEC, "resume": Command(), "fork": Command(),
            **_idle(
                "agents review login logout mcp plugin app-server remote-control completion update doctor sandbox debug apply a "
                "queue archive delete migrate-rollouts unarchive cloud exec-server features help"),
        }),
        value=_words(
            "-a --ask-for-approval --add-dir --base -C --cd --color --commit -c --config --disable --enable --local-provider "
            "-o --output-last-message --output-schema -p --profile --remote --remote-auth-token-env -s --sandbox "
            "--thread-source --title"),
        variadic=_words("-i --image"),
        model=_words("-m --model"),
        model_config=_words("-c --config"),
    ),
    "gemini": Syntax(
        root=Command(prompt=True, commands=_idle("mcp extensions extension skills skill hooks hook gemma")),
        value=_words("--approval-mode --delete-session -o --output-format --session-file --session-id"),
        variadic=_words("--admin-policy --allowed-mcp-server-names --allowed-tools -e --extensions --include-directories --policy"),
        optional=_words("-r --resume -w --worktree"),
        model=_words("-m --model"),
        prompt=_words("-p --prompt -i --prompt-interactive"),
        query_words=True,
    ),
}
GENERIC = Syntax()


def _chars(n: int) -> str:
    return f"{n} char" if n == 1 else f"{n} chars"


# ---- reading a command line ----------------------------------------------------------------
@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    words: tuple[str, ...]  # the command and its subcommands, e.g. ("codex", "exec")
    session: bool  # False: this command never talks to a model, so there is nothing to route
    prompt: str | None  # the prompt when it is on the command line
    prompt_parts: tuple[tuple[int, str], ...]  # (argv index, text) of each argument that carries it
    model: str | None  # the model the command already selects: its own choice beats the router
    insert_at: int  # argv index where a model flag goes: right after the command and its subcommands, never past a --
    shown: tuple[str, ...] = ()  # argv as it may be shown, one entry per argument (see ``redacted``)

    def with_model(self, flag: str, model: str) -> "Invocation":
        """The same command with ``flag model`` inserted, ahead of every option and of any ``--`` or prompt after it."""
        at = self.insert_at
        parts = tuple((i + 2 if i >= at else i, text) for i, text in self.prompt_parts)
        return replace(self, argv=(*self.argv[:at], flag, model, *self.argv[at:]), prompt_parts=parts, model=model,
                       shown=(*self.shown[:at], flag, model, *self.shown[at:]))

    def shown_argv(self) -> list[str]:
        """The arguments the agent will get, for showing (``run --dry-run --json`` lists them).

        Only what cannot hold a secret is shown: the program, the subcommand words the agent's CLI knows (``exec``, ``resume``,
        ``mcp``), the model flag with its value, and the names of the other flags. The prompt is shown as its length, and so is
        every other argument (a flag's value, a positional that is not a subcommand, anything after ``--``): ``<arg: 12 chars>``.
        A key or a token given in a flag (``--api-key sk-...``, ``--header=Authorization: ...``) never reaches the screen, a log or
        a script that reads the JSON. The agent itself still gets every argument exactly as it was given."""
        return list(self.shown)

    def display(self) -> str:
        """The command as one shell-quoted line for the terminal (display only: it is never run through a shell),
        with the arguments hidden as ``shown_argv`` says."""
        return shlex.join(self.shown_argv())


_Option = tuple[str, str | None, int, int]  # spelling, value, index of the argument holding the value, index of the flag


def _split(arg: str, valued: frozenset[str]) -> tuple[str, str | None]:
    """``--model=x`` -> (``--model``, ``x``); ``-mx`` and ``-m=x`` -> (``-m``, ``x``) when ``-m`` takes a value."""
    if arg.startswith("--") or len(arg) == 2:
        name, eq, value = arg.partition("=")
        return name, value if eq else None
    if arg[:2] in valued:
        rest = arg[2:]
        return arg[:2], rest[1:] if rest.startswith("=") else rest
    return arg, None  # a bundle of flags such as -dy


def _scan(argv: Sequence[str], syn: Syntax, single: frozenset[str]) -> tuple[list[int], list[_Option], int]:
    """(indexes of the positional arguments, the options that carry a value, index of the ``--`` that ends the options
    or ``len(argv)``), read the way the agent CLIs read them."""
    positional: list[int] = []
    options: list[_Option] = []
    valued = single | syn.variadic | syn.optional
    end = len(argv)
    i = 1
    while i < len(argv):
        arg, at = argv[i], i
        i += 1
        if arg == "--":  # everything after a bare -- is positional
            positional.extend(range(i, len(argv)))
            end = at
            break
        if arg == "-" or not arg.startswith("-"):
            positional.append(at)
            continue
        name, inline = _split(arg, valued)
        if inline is not None:
            options.append((name, inline, at, at))
        elif name in single:
            if i < len(argv):
                options.append((name, argv[i], i, at))
                i += 1
        elif name in syn.variadic:
            while i < len(argv) and not argv[i].startswith("-"):
                i += 1
        elif name in syn.optional and i < len(argv) and not argv[i].startswith("-"):
            i += 1
    return positional, options, end


def redacted(
    argv: Sequence[str], keep: set[int], prompts: Mapping[int, str], end: int, valued: frozenset[str],
) -> tuple[str, ...]:
    """``argv`` as it may be shown: the arguments at the indexes in ``keep`` (program, subcommands, model flag and value) as they
    are, a prompt as its length, the name of every other flag, and everything else as ``<arg: N chars>``.

    A flag keeps its name and loses its value: ``--key=abc`` is ``--key=<arg: 3 chars>``, and so is the argument after ``--key``
    (it reads as a positional). A single-dash argument longer than two characters is an option with its value stuck to it
    (``-ksecret``) or a bundle of flags (``-dy``): it is shown as its first flag and a length. Nothing after ``--`` is shown."""
    out = []
    for i, token in enumerate(argv):
        if i in keep or token == "-" or i == end:  # the program, the words and model flag that are known, stdin's ``-``, the ``--``
            out.append(token)
        elif text := prompts.get(i):  # the prompt is always the end of its argument: the whole of it, or what follows ``--prompt=``
            out.append(f"{token[:len(token) - len(text)]}<prompt: {_chars(len(text))}>")
        elif i < end and token.startswith("-"):
            _, value = _split(token, valued)
            if value is not None:
                out.append(f"{token[:len(token) - len(value)]}<arg: {_chars(len(value))}>")
            elif len(token) > 2 and not token.startswith("--"):
                out.append(f"{token[:2]}<arg: {_chars(len(token) - 2)}>")
            else:
                out.append(token)
        else:
            out.append(f"<arg: {_chars(len(token))}>")
    return tuple(out)


def analyze(argv: Sequence[str], agent: str | None, model_flag: str | None) -> Invocation:
    """Read ``argv`` (the agent command, argv[0] included) as the agent named ``agent`` in the config would.

    ``agent`` is the config key (None for an unknown command); ``model_flag`` is the flag the config, or ``--model-flag``,
    names for handing a model over.
    """
    syn = SYNTAX.get(agent or "", GENERIC)
    model_options = syn.model | ({model_flag} if model_flag else frozenset())
    single = syn.value | syn.prompt | model_options
    positional, options, end = _scan(argv, syn, single)
    node, at, k = syn.root, 1, 0
    while k < len(positional) and (sub := node.commands.get(argv[positional[k]])):
        node, at, k = sub, positional[k] + 1, k + 1
    model = None
    keep = {0, *positional[:k]}  # shown as they are: the program, the subcommand words and the model flag with its value
    for name, value, at_value, at_flag in options:
        if name in model_options and value:
            model = value
            keep |= {at_value, at_flag}
        elif name in syn.model_config and value and value.startswith("model="):
            model = value[len("model="):].strip("\"'")
            keep |= {at_value, at_flag}
    parts = [(i, value) for name, value, i, _ in options if name in syn.prompt and value is not None]
    if node.prompt:
        rest = [i for i in positional[k:] if argv[i] != "-"]  # a lone "-" means "read the prompt from stdin"
        parts += [(i, argv[i]) for i in (rest if syn.query_words else rest[-1:])]
    return Invocation(
        argv=tuple(argv),
        words=(argv[0], *(argv[i] for i in positional[:k])),
        session=node.model,
        prompt=" ".join(text for _, text in parts) if parts else None,
        prompt_parts=tuple(parts),
        model=model,
        insert_at=min(at, end),  # a flag after -- is text, even when the word next to it reads as a subcommand
        shown=redacted(argv, keep, dict(parts), end, single | syn.variadic | syn.optional),
    )


def child_env(base: Mapping[str, str], tier: str | None, model: str | None) -> dict[str, str]:
    """The environment for the agent: ``base`` plus AGENTFARE_TIER and AGENTFARE_MODEL; a value that does not apply is removed,
    so a run nested in another one never sees the outer run's choice."""
    env = dict(base)
    for key, value in ((TIER_ENV, tier), (MODEL_ENV, model)):
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return env

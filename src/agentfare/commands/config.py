"""`agentfare config`: show or create the configuration file."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from ..config import DEFAULT_CONFIG_YAML, Config
from ..render import clean_errors, clean_text


def register(app: typer.Typer) -> None:
    @app.command("config")
    def config_cmd(
        ctx: typer.Context,
        init: Annotated[bool, typer.Option("--init", help="Write the default config file to ~/.agentfare/config.yaml (or to the file named with --config).")] = False,
        force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config file with --init, even one that is not valid YAML.")] = False,
        show_default: Annotated[bool, typer.Option("--default", help="Print the built-in default config and exit.")] = False,
    ) -> None:
        """Show where the config lives, or write a commented default file with --init."""
        st = ctx.obj
        if show_default:
            typer.echo(DEFAULT_CONFIG_YAML, nl=False)
            return
        with clean_errors():
            if init:
                # A fresh Config that never reads the file: one that is broken must still be replaceable with --force.
                fresh = Config.for_init(st.config_path, st.home)
                path = fresh.write_default(fresh.default_init_target, overwrite=force)
                st.out.print(f"wrote {escape(clean_text(str(path)))}", soft_wrap=True)
                return
            cfg = st.cfg
            shown = lambda p: escape(clean_text(str(p)))  # noqa: E731 - paths come from the config file
            st.out.print(f"config file : {shown(cfg.config_path) if cfg.config_path else '(none, using built-in defaults)'}", soft_wrap=True)
            st.out.print(f"data dir    : {shown(cfg.home)}", soft_wrap=True)
            st.out.print(f"database    : {shown(cfg.db_path)}", soft_wrap=True)
            override = cfg.pricing_override_path
            if override.exists():
                pricing = shown(override)
            elif cfg.data.get("pricing_file"):  # a path you wrote must exist: every command that prices stops with an error until it does
                pricing = f"{shown(override)}  [red](missing)[/red]"
            else:
                pricing = "built-in table"
            st.out.print(f"pricing     : {pricing}", soft_wrap=True)
            for agent in ("claude", "codex", "gemini"):
                for p in cfg.sources(agent):
                    st.out.print(f"{agent:<11} : {shown(p)}{'' if p.exists() else '  [red](missing)[/red]'}", soft_wrap=True)

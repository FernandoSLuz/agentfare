# Demo, demo data and images

AgentFare ships with a history that never happened: 45 invented sessions of Claude Code, Codex CLI and Gemini CLI. `agentfare demo` runs the product on it, and every image of the README (the screenshots, the report, the diagram and the GIF) is drawn from it by one script. None of it reads your own logs, and none of it is a recording of a real person's work.

| File | Role |
|---|---|
| `src/agentfare/demo_data/` | The synthetic logs, in the real on-disk format of each agent, and a `config.yaml` that points at them. About 1.2 MB, and part of the installed package |
| [`scripts/make_demo_data.py`](../scripts/make_demo_data.py) | Writes those files, deterministically. Its `PLANS` table is the token table of every session |
| [`src/agentfare/commands/demo.py`](../src/agentfare/commands/demo.py) | The `agentfare demo` command |
| [`scripts/make_assets.py`](../scripts/make_assets.py) | Draws every file of `docs/images/` from the demo data |
| `docs/images/` | The images the README shows |
| [`scripts/demo.sh`](../scripts/demo.sh) and [`demo.tape`](../demo.tape) | A scripted tour for people who want to record a real terminal session instead |

* [The demo data](#the-demo-data)
* [agentfare demo](#agentfare-demo)
* [The images](#the-images)
* [init and doctor](#init-and-doctor)
* [Run the tour by hand](#run-the-tour-by-hand)
* [Record it with VHS](#record-it-with-vhs)
* [Or record it with asciinema and agg](#or-record-it-with-asciinema-and-agg)
* [Make the GIF smaller](#make-the-gif-smaller)
* [When to draw it again](#when-to-draw-it-again)

## The demo data

The history covers 2026-09-21 to 2026-09-30 in UTC: the ISO week 2026-W39, complete, and 2026-W40, which is still going at the frozen clock of the demo (`AGENTFARE_NOW=2026-09-30T18:00:00Z`, 18:00 UTC on a Wednesday). It has five invented projects (`storefront-api`, `notes-app`, `infra-terraform`, `data-pipeline` and `docs-site`) and tells one story:

* Claude Code has seven long Opus sessions (42 to 54 tool calls, 7 to 11 files, retries) that the router keeps. One of them is written mostly with 1-hour cache writes, one has two Sonnet sub-agents, one runs across midnight and one is still running at 17:52. A medium and a light Opus session are ones a cheaper model would have handled, and nine short Sonnet sessions are already cheap.
* Codex CLI has light and medium sessions on the flagship `gpt-6-astra`, which `advise` moves to `gpt-6.1-sol` for about a fifth of the price. A heavy astra session stays where it is, some sessions already run on `gpt-6.1-sol` and `gpt-6-sol`, and two sub-agent threads and two auto-review threads have no prompt. The auto-review threads use `codex-auto-review`, a model with no published price, so the demo also shows the warning for an unverified model and the dollar totals that leave it out.
* Gemini CLI has `gemini-2.5-pro` and `gemini-3.1-pro-preview` sessions, which are already cheaper than `gpt-6.1-sol` or too close to it to be worth a switch.

The headline numbers, which `tests/test_demo_data.py` works out by hand from the token table and `pricing.yaml` and compares with the real commands:

| | |
|---|---|
| Sessions | 45 (18 Claude Code, 18 Codex CLI, 9 Gemini CLI) in 5 projects |
| Spend | $193.96, with the 106k tokens of `codex-auto-review` left out |
| By week | 2026-W39 $142.91, 2026-W40 $51.05 |
| What the router would have spent | $152.42 |
| Saved | $41.55, 21.4% of the spend, by 14 advised switches |
| Where the saving comes from | $37.92 from sessions a person prompted and $3.63 from sub-agent threads with no prompt |

Everything in the logs is made up: the projects, the paths (all under `/home/demo/projects`), the session ids (hashes of invented labels) and the text. Every text field of every log carries the marker `PRIVACY-CANARY-DEMO`, and a test checks that the marker never reaches the database, as the fixtures' markers do. No name or path of the machine that generated the files is in them, and a test checks that too.

```sh
python scripts/make_demo_data.py            # rewrite src/agentfare/demo_data
python scripts/make_demo_data.py --check    # exit 1 when the committed files differ from a fresh generation
```

The generator uses a fixed seed and its own random number generator, so every Python writes the same bytes. Change a session in `PLANS`, run it, and run `pytest tests/test_demo_data.py`: the test fails until the documented numbers in `DOCUMENTED` (and the figures in this page) say what the new table makes.

The files are listed in `pyproject.toml` under `package-data`. A glob does not match a file whose name starts with a dot, so the `.project_root` file that Gemini CLI keeps next to its chats has a pattern of its own. `tests/test_demo_cmd.py` checks that every demo file is covered.

## agentfare demo

```sh
agentfare demo                          # the banner, the summary, the top 3 pieces of advice, what to try next
agentfare demo --report week.html       # also write the HTML report of the latest demo week (2026-W40)
agentfare demo --keep ./agentfare-demo  # keep the data and the database, and print the flags to use them
agentfare demo --json                   # totals and savings as data
```

The demo copies the packaged data to a temporary directory, reads it into a temporary database with the clock frozen at the demo's, runs the real `summary` and `advise` on it and deletes both when it ends. It never reads your agent logs or `~/.agentfare`, it ignores the global `--config` and `--home`, and it uses no network. The first line of the output says the data is synthetic.

With `--keep DIR` the directory must be new or empty (the demo refuses anything else, so that nothing of yours is overwritten) and gets two folders: `demo_data` with the logs and the config, and `home` with the database. The output then lists the commands to run on them, for example:

```sh
export AGENTFARE_NOW=2026-09-30T18:00:00Z
agentfare --config ./agentfare-demo/demo_data/config.yaml --home ./agentfare-demo/home summary --week
agentfare --config ./agentfare-demo/demo_data/config.yaml --home ./agentfare-demo/home advise --all
```

The global options go before the command. `AGENTFARE_NOW` makes `--week` mean the demo week. `run` reads the database that `agentfare demo --keep` already filled, so it can estimate from the history.

`--json` prints one object and nothing else. Its keys are `synthetic`, `now`, `sessions`, `projects`, `models`, `tokens`, `total` and `excluded_tokens` (the same figures as `summary --json`), `weeks` (the spend of each ISO week), `savings` (`sessions`, `switched`, `actual`, `router`, `saved`, `saved_bp`, `prompted` and `background`), `top_advice` (the three sessions of `advise --limit 3`), `warnings`, `kept` (the directories of `--keep`, or null) and `report` (the file of `--report`, or null). Money is `{"cents": ..., "ucents": ...}` as everywhere else.

## The images

`scripts/make_assets.py` writes every file of `docs/images/`. The terminal screenshots and the GIF show the real output of the real commands, run on the demo data in a scratch directory with the clock frozen at the demo's, so that every run draws the same pictures (a browser may differ from one run to the next by a few pixels of anti-aliasing).

| Name for `--only` | File | What it shows |
|---|---|---|
| `hero` | `hero.png` | The banner at the top of the README, 1280 by 640, with the cost of the demo sessions with and without the router |
| `social-preview` | `social-preview.png` | The same design with wide margins, for the social preview of the repository (Settings, Social preview) |
| `summary` | `summary.png` | `agentfare summary`, the tables by model and by project |
| `models` | `models.png` | `agentfare models` |
| `advise` | `advise.png` | `agentfare advise --limit 3` |
| `run` | `run.png` | `agentfare run --dry-run -- claude -p "refactor the auth module and migrate the database"`, a heavy task that keeps the heavy model |
| `run-light` | `run-light.png` | `agentfare run --dry-run -- codex exec "fix the typo in the README"`, a light task that moves from `gpt-6-astra` to `gpt-6.1-sol` |
| `sessions` | `sessions.png` | `agentfare sessions -n 14` |
| `prices` | `prices.png` | `agentfare prices`, with the unverified models in amber |
| `init` | `init.png` | `agentfare init` as a new user sees it: the folders of the three agents, 45 log files read, the total and the next steps. See [init and doctor](#init-and-doctor) |
| `doctor` | `doctor.png` | `agentfare doctor` on the same machine before the first `init`: 18 checks, all ok |
| `report-dark`, `report-light` | `report-dark.png`, `report-light.png` | The weekly HTML report in both themes, 1080 pixels wide (the card of a 1200 pixel window) |
| `report-mobile` | `report-mobile.png` | The same report on a 390 pixel phone |
| `how-it-works` | `how-it-works.svg`, `how-it-works.png` | The diagram: logs, parsers, local database, prices, commands, and the line around them that says nothing leaves your machine |
| `demo` | `demo.gif` | The terminal recording look: 900 pixels wide, about 11 seconds, a loop of `init`, `summary`, `advise` and a heavy `run`, with a pause after each output |

Run it from the repository root:

```sh
python scripts/make_assets.py                         # every image
python scripts/make_assets.py --only summary advise   # only these
python scripts/make_assets.py --list                  # the names and the files they make
```

The script needs a few development tools. None of them is a dependency of AgentFare. `chromium` (or `google-chrome`) draws the PNGs, Pillow (`pip install pillow`) draws the frames of the GIF, `ffmpeg` assembles the GIF with a palette of its own, `rsvg-convert` turns the diagram into a PNG (ImageMagick's `magick` is the fallback), `fc-match` finds a monospace font (JetBrains Mono, else DejaVu Sans Mono, Liberation Mono or Noto Sans Mono) and `uv` installs AgentFare for the `init` and `doctor` pictures. Run it with a Python that can import AgentFare. The list is also at the top of the script.

The screenshots come from a table at the top of the script, one line each:

```python
SHOTS = (
    Shot("summary", ("summary",), lines=30),
    Shot("advise", ("advise", "--limit", "3")),
    ...
)
```

A new screenshot is one more line: a name and the words typed after `agentfare`, and `lines=N` when only the first N lines of the output belong in a picture. The command runs on the demo data, and `python scripts/make_assets.py --only <name>` draws it. A line with `own_home=True` is drawn differently, as described under [init and doctor](#init-and-doctor). `tests/test_assets.py` reads the same table and then expects the file in `docs/images/`, 1000 pixels wide and under 2.5 MB. The GIF has its own short table (`GIF_SCENES`): a command, how many lines of its output to show and how long to rest on it, and `own_home=True` for the first scene, `init`. Every image is limited to 2.5 MB, and the script stops when one is bigger.

The paths of the machine that draws the pictures never reach them. The scratch directory is shown as `~/agentfare-demo`, the checkout as `~/agentfare` and the built-in price file as `…/agentfare/data/pricing.yaml`, and the script stops when a home directory, a temporary path or the user name is left in a text that it is about to draw. The report and the diagram contain only the invented projects.

### init and doctor

`init` and `doctor` are about the machine they run on, so they are not run on the demo config. The script installs the checkout with `uv tool install` into a scratch home (the install is made from a copy of the sources, so nothing is built inside the checkout), copies the invented logs to the default folders of that home (`~/.claude/projects`, `~/.codex/sessions`, `~/.gemini/tmp`), puts stand-ins for the three agents in `~/.local/bin`, and runs the installed `agentfare` with no config file and no data directory yet. That is what a person sees right after installing, so the pictures show `~/...` paths and the real clock (`doctor` says how old the price check is). Each of these shots starts from a home that has never run AgentFare, so `doctor` is the picture of a machine that is ready and has not read anything yet. Standard output and standard error are drawn together in the order they were written, as a terminal shows them. The step needs `uv`, and the network or a warm uv cache for Typer, Rich, PyYAML and Jinja2.

## Run the tour by hand

`scripts/demo.sh` runs a short tour in your terminal and prints each command before its output. Nothing in it reads your real agent logs or writes to `~/.agentfare`: the data directory is a temporary one that the script deletes on exit, and `AGENTFARE_NOW` is frozen.

```sh
bash scripts/demo.sh                    # on the three sample sessions of tests/fixtures
DEMO_SET=demo bash scripts/demo.sh      # on the 45 sessions of `agentfare demo`
DEMO_PAUSE=0 bash scripts/demo.sh       # no pauses, for a quick look
```

The first tour is the one `tests/test_readme.py` runs: it shows the summary, the sessions table, the advice, two `run` decisions and the start of the weekly report, and its numbers are the ones of the README. The second is the story of the GIF: the first 30 lines of the summary, the top 3 pieces of advice, a heavy `run` and the start of the report, through `sed` and `fold`, which read everything they are given.

| Variable | Effect |
|---|---|
| `DEMO_SET` | `fixtures` (the default) or `demo` |
| `DEMO_PAUSE` | Seconds to wait after each step. Default 2 in a terminal, 0 otherwise |
| `DEMO_CLEAR` | `1` clears the screen before each step, so that a recording shows one screen per step. Default 0 |
| `DEMO_TYPE` | `1` types each command out slowly, `0` prints it at once. Default 1 in a terminal, 0 otherwise |
| `COLUMNS` | Output width. Default: the terminal width, at most 100, and 80 when there is no terminal |
| `AGENTFARE_CMD` | The command that starts AgentFare. Default `agentfare` when it is on `PATH`, else `$PYTHON -m agentfare` |

To run the same commands yourself, one at a time, on the fixtures:

```sh
export AGENTFARE_HOME=$(mktemp -d)
export AGENTFARE_NOW=2026-09-30T12:00:00Z
export AGENTFARE_CONFIG=$PWD/tests/fixtures/config.yaml
export COLUMNS=100
cd tests/fixtures
agentfare summary
agentfare run --dry-run -- claude -p "fix the typo in the README"
```

The `run` decisions count the files under the current directory, which is why the script changes into `tests/fixtures` (or, for the demo set, into an empty directory) first: with fewer than 200 files the repository rule adds no points and the tiers stay the same everywhere.

The order matters for `run`. It never reads your logs and never creates the database, so it only knows the history that an earlier command stored. In the tours `agentfare summary` is the first step and fills the temporary database, which is why the `run` panels name your most used model of the last 30 days as the baseline and carry no `no usage history yet` warning. Run `agentfare run --dry-run ...` first, on a data directory that has never been used, and the baseline is the agent's heavy-tier model and the panel adds that warning.

## Record it with VHS

A script draws the committed GIF and no terminal is involved. To record a real terminal session of the demo tour instead, use [VHS](https://github.com/charmbracelet/vhs). `demo.tape` records `DEMO_SET=demo bash scripts/demo.sh`.

1. Install VHS. On macOS, and on Linux with Homebrew, `brew install vhs` also installs what it needs. Other installs (`go install github.com/charmbracelet/vhs@latest`, a distribution package) need `ttyd` and `ffmpeg` on the `PATH` as well. The VHS README lists the current options.
2. Install AgentFare in a virtual environment and activate it, so that `agentfare` is on the `PATH` of the shell VHS starts:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -e .
   ```

3. Record from the repository root:

   ```sh
   vhs demo.tape
   ```

   The tape types `bash scripts/demo.sh`, waits for the closing line of the script (`Wait@120s /own logs/`) and writes `docs/demo.gif`. That file is outside `docs/images/`, which holds only what `make_assets.py` makes, so nothing commits it by accident.

4. Open the GIF and check it: each step should fill one screen, and no line should be cut off.

The tape sets `DEMO_SET=demo`, `DEMO_CLEAR=1`, `DEMO_PAUSE=3` and `DEMO_TYPE=1` through `Env`, so the recording shows one screen per step with three seconds to read each. The terminal settings are these:

| Setting | Value | Why |
|---|---|---|
| `FontSize` | 15 | Readable when GitHub scales the GIF down to the width of the page |
| `Width` and `Height` | 1100 and 800 | An estimate of 120 columns and 40 rows, not measured. The script uses 100 columns, and its longest step is 31 lines, so raise `Height` if the recording cuts it off |
| `Padding` | 16 | Space around the text |
| `Theme` | `Catppuccin Mocha` | A dark theme that keeps the box drawing of the `run` panel readable. `vhs themes` lists the others |
| `TypingSpeed` | 45ms | Fast enough not to bore, slow enough to read the command |
| `Framerate` | 24 | Smooth typing. Lower it (15 or 12) to shrink the file |

What has been run: `scripts/demo.sh` in both sets, in its plain mode and in a pseudo terminal with typing and clearing on, and `make_assets.py` for every image. What has not: `demo.tape` and the commands of VHS, asciinema, agg and gifsicle, because none of them was installed when this page was written. Treat those steps as a starting point, and expect to adjust a flag or a size.

## Or record it with asciinema and agg

[asciinema](https://asciinema.org) records a terminal session to a `.cast` file and [agg](https://github.com/asciinema/agg) turns it into a GIF. Size the terminal window to about 100 columns and 36 rows before you start, because the recording takes the size of the terminal:

```sh
DEMO_SET=demo DEMO_CLEAR=1 DEMO_PAUSE=3 asciinema rec --command "bash scripts/demo.sh" demo.cast
agg --theme dracula --font-size 16 demo.cast docs/demo.gif
```

`asciinema rec --overwrite` replaces an earlier `demo.cast`, and `agg --speed 1.5` plays a slow recording faster. The `.cast` file is text and small, so you can keep it for re-rendering with other themes, but it does not need to be committed.

The recording tools are not part of AgentFare and their options change between releases. If a flag is rejected, check `vhs --help`, `asciinema rec --help` or `agg --help`.

## Make the GIF smaller

`make_assets.py` writes a GIF of about 400 KB with a 96 colour palette that ffmpeg builds from the frames. A recording can run to several megabytes, and a file that is committed stays in the history for good. Lower the frame rate first (`Set Framerate 15` in the tape, or `--fps-cap 15` for agg). Then optimise the result.

With gifsicle:

```sh
gifsicle -O3 --colors 128 docs/demo.gif -o docs/demo-small.gif
mv docs/demo-small.gif docs/demo.gif
```

With ffmpeg, which rebuilds the palette for the smaller frame size:

```sh
ffmpeg -i docs/demo.gif -vf "fps=12,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" docs/demo-small.gif
```

An mp4 is much smaller than a GIF. Use it where a video player is available, on a release page or a web page for example, and keep the GIF for the README. The conversion rounds the width and height to even numbers:

```sh
ffmpeg -i docs/demo.gif -movflags faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" docs/demo.mp4
```

A target of under 2 MB keeps the README quick to load. `tests/test_assets.py` fails for any image over 2.5 MB.

## When to draw it again

Any change to the output of `summary`, `sessions`, `models`, `advise`, `run`, `prices` or `report`, to the price table, to the router rules or to the config defaults changes what the pictures show. After such a change run `python tests/test_readme.py --write` to update the README, and `python scripts/make_assets.py` so that the images do not show old numbers. A change to the demo data needs `python scripts/make_demo_data.py` first, then the same two steps. Embed an image with a relative path, which renders on the repository page:

```markdown
![AgentFare demo](docs/images/demo.gif)
```

If the README is also shown somewhere that does not serve the repository (another site that copies it, for example), use the raw URL instead: `https://raw.githubusercontent.com/FernandoSLuz/agentfare/main/docs/images/demo.gif`.

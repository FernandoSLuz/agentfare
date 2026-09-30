# Recording the demo GIF

The demo runs AgentFare on the three synthetic sessions in `tests/fixtures/` and shows the summary, the sessions table, the advice, two `run` decisions and the start of the weekly report (its first 13 lines: the headline and the router saving table, up to the first heading). The pieces:

| File | Role |
|---|---|
| `scripts/demo.sh` | Runs the tour deterministically and prints each command before its output. It works live and as the script a recorder captures |
| `demo.tape` | The [VHS](https://github.com/charmbracelet/vhs) script that records `scripts/demo.sh` into `docs/demo.gif` |
| `docs/demo.gif` | The recording. It is not in the repository until someone records it |

Nothing in the demo reads your real agent logs or writes to `~/.agentfare`: the data directory is a temporary one that the script deletes on exit, `AGENTFARE_NOW` is frozen at `2026-09-30T12:00:00Z`, and the config is `tests/fixtures/config.yaml`. The numbers are the same on every run and every machine, and `tests/test_readme.py` checks that the script runs, that it prints each command before its output, and that its summary, advice and `run` output match the blocks of the README.

What has been run: `scripts/demo.sh`, in its plain mode and in a pseudo terminal with typing and clearing on, and the ffmpeg conversions under [Make the GIF smaller](#make-the-gif-smaller), on a generated test GIF. What has not: `demo.tape` and the commands of VHS, asciinema, agg and gifsicle, because none of them was installed when this page was written. Treat those steps as a starting point, and expect to adjust a flag or a size.

## Run the demo by hand

From the repository root, with AgentFare installed (`pip install -e .`) and the virtual environment active:

```sh
bash scripts/demo.sh
```

In a terminal the script types each command out and waits two seconds after its output. Without a terminal it prints everything at once. These environment variables change that:

| Variable | Effect |
|---|---|
| `DEMO_PAUSE` | Seconds to wait after each step. Default 2 in a terminal, 0 otherwise |
| `DEMO_CLEAR` | `1` clears the screen before each step, so that a recording shows one screen per step. Default 0 |
| `DEMO_TYPE` | `1` types each command out slowly, `0` prints it at once. Default 1 in a terminal, 0 otherwise |
| `COLUMNS` | Output width. Default: the terminal width, at most 100, and 80 when there is no terminal |
| `AGENTFARE_CMD` | The command that starts AgentFare. Default `agentfare` when it is on `PATH`, else `$PYTHON -m agentfare` |

```sh
DEMO_PAUSE=0 bash scripts/demo.sh                 # instant, for a quick look
DEMO_PAUSE=0 bash scripts/demo.sh | less -R       # page through it
```

To run the same commands yourself, one at a time, use the settings the script uses:

```sh
export AGENTFARE_HOME=$(mktemp -d)
export AGENTFARE_NOW=2026-09-30T12:00:00Z
export AGENTFARE_CONFIG=$PWD/tests/fixtures/config.yaml
export COLUMNS=100
cd tests/fixtures
agentfare summary
agentfare sessions
agentfare advise
agentfare run --dry-run -- claude -p "fix the typo in the README"
agentfare run --dry-run -- claude -p "refactor the billing module and migrate it to the new API"
agentfare report --format md | sed -n 1,13p
```

The `run` decisions count the files under the current directory, which is why the script changes into `tests/fixtures` first: with fewer than 200 files the repository rule adds no points and the sample tiers stay the same everywhere.

The order matters for `run`. It never reads your logs and never creates the database, so it only knows the history that an earlier command stored. In the demo `agentfare summary` is the first step and fills the temporary database, which is why the two `run` panels name your own most used claude model of the last 30 days as the baseline (`your most used claude model in the last 30 days, since 2026-08-31`) and carry no `no usage history yet` warning. Run `agentfare run --dry-run ...` first, on a data directory that has never been used, and the baseline is the agent's heavy-tier model (`what you would run without the router`) and the panel adds that warning.

## Record it with VHS

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

   The tape types `bash scripts/demo.sh`, waits for the closing line of the script (`Wait@120s /own logs/`) and writes `docs/demo.gif`.

4. Open the GIF and check it: each step should fill one screen, and no line should be cut off.

The tape sets `DEMO_CLEAR=1`, `DEMO_PAUSE=3` and `DEMO_TYPE=1` through `Env`, so the recording shows one screen per step with three seconds to read each. The terminal settings are these:

| Setting | Value | Why |
|---|---|---|
| `FontSize` | 15 | Readable when GitHub scales the GIF down to the width of the page |
| `Width` and `Height` | 1100 and 800 | An estimate of 120 columns and 40 rows, not measured. The script uses 100 columns, and its longest step (the summary) is 37 lines with the two progress lines of the first scan, so raise `Height` if the recording cuts it off |
| `Padding` | 16 | Space around the text |
| `Theme` | `Catppuccin Mocha` | A dark theme that keeps the box drawing of the `run` panel readable. `vhs themes` lists the others |
| `TypingSpeed` | 45ms | Fast enough not to bore, slow enough to read the command |
| `Framerate` | 24 | Smooth typing. Lower it (15 or 12) to shrink the file |

A longer or shorter pause is one edit: change `DEMO_PAUSE` in the tape. A different width needs both a new `Width` and, if it exceeds 100 columns, `COLUMNS`, because the script never goes past 100.

## Or record it with asciinema and agg

[asciinema](https://asciinema.org) records a terminal session to a `.cast` file and [agg](https://github.com/asciinema/agg) turns it into a GIF. Size the terminal window to about 100 columns and 36 rows before you start, because the recording takes the size of the terminal:

```sh
DEMO_CLEAR=1 DEMO_PAUSE=3 asciinema rec --command "bash scripts/demo.sh" demo.cast
agg --theme dracula --font-size 16 demo.cast docs/demo.gif
```

`asciinema rec --overwrite` replaces an earlier `demo.cast`, and `agg --speed 1.5` plays a slow recording faster. The `.cast` file is text and small, so you can keep it for re-rendering with other themes, but it does not need to be committed.

The recording tools are not part of AgentFare and their options change between releases. If a flag is rejected, check `vhs --help`, `asciinema rec --help` or `agg --help`.

## Make the GIF smaller

A GIF of six screens can run to several megabytes, and a file that is committed stays in the history for good. Lower the frame rate first (`Set Framerate 15` in the tape, or `--fps-cap 15` for agg). Then optimise the result.

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

A target of under 2 MB keeps the README quick to load.

## Where the file goes and how to embed it

Save it as `docs/demo.gif`, which is where `demo.tape` writes it. Put the image right under the two sentences that open the README:

```markdown
![AgentFare demo](docs/demo.gif)
```

A relative path renders on the repository page. If the README is also shown somewhere that does not serve the repository (another site that copies it, for example), use the raw URL instead: `https://raw.githubusercontent.com/FernandoSLuz/agentfare/main/docs/demo.gif`.

## When to record it again

Any change to the output of `summary`, `sessions`, `advise`, `run` or `report`, to the price table, or to the config defaults changes what the demo shows. After such a change run `python tests/test_readme.py --write` to update the README, and record the GIF again so that it does not show old numbers.

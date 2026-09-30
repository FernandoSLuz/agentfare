# How the router decides

AgentFare's router has two jobs, and both run on rules you can read and change. There is no model, no
learning and no hidden weight: a handful of thresholds in `router:` of your `config.yaml`, some integer
arithmetic, and one line of explanation for every decision.

| Command | Question it answers | Input |
|---|---|---|
| `agentfare advise` | Looking back: which of my sessions would a cheaper model likely have handled, and how much would that have saved? | Sessions already in the local database (token counts and counters, never text) |
| `agentfare run` | Looking ahead: which model should this task start on? | The prompt string, the number of files in the repo and, for the estimate, the usage history already in the database |

Everything here is local. Neither job touches the network, stores a prompt, or reads the contents of a file. The only
network access in AgentFare is `agentfare prices --refresh`.

* [1. `advise`: scoring past sessions](#1-advise-scoring-past-sessions)
* [2. `advise`: what it prints](#2-advise-what-it-prints)
* [3. Worked example](#3-worked-example)
* [4. `run`: picking a model before you start](#4-run-picking-a-model-before-you-start)
* [5. Every config key](#5-every-config-key)
* [6. How the defaults were calibrated](#6-how-the-defaults-were-calibrated)
* [7. Limits: read this before believing a number](#7-limits-read-this-before-believing-a-number)
* [8. Tuning](#8-tuning)

---

## 1. `advise`: scoring past sessions

### The five signals

Each session is measured on five numbers that the log parsers already extract (see the parser docs for
exactly how each agent's logs map onto them):

| Signal (config key) | What is measured | medium | heavy | Why it says "big task" |
|---|---|---|---|---|
| `input_tokens` | every input token of the session, all models added up: uncached + cache reads + cache writes (5-minute and 1-hour). Output tokens are **not** included | 2,000,000 | 25,000,000 | An agent re-sends its context on every turn, so total input grows with the number of turns times the size of the context: long, context-heavy work |
| `tool_calls` | tool invocations | 25 | 180 | Reading, searching, editing and running commands: the amount of work the agent did |
| `files_touched` | distinct files written or edited (a count, no names) | 1 | 6 | Edits spread over several files are a bigger change than a question or a one-file fix |
| `error_retries` | tool calls that came back as an error | 2 | 8 | Errors and retries mean the task pushed back |
| `active_minutes` | active seconds / 60. Gaps longer than `idle_gap_minutes` (default 30) are not counted | 10 | 80 | Time actually spent working, not time the window stayed open |

Two tiny details that matter when you tune. The comparison is `value >= threshold`, so a session with exactly
10.0 active minutes is `medium` on that signal. And thresholds are read as the decimal you wrote (`1.15` means
exactly 1.15, i.e. 69 seconds for `active_minutes`), never as a binary float.

### Points and tiers

A signal is worth **0** below its `medium` threshold, **1** from `medium`, and **2** from `heavy`. The five
points add up to a **score from 0 to 10**, and `router.tiers` cuts the score into a tier:

| Score | Tier (defaults) |
|---|---|
| 0 to 2 (`<= light_max`) | `light` |
| 3 to 6 (`<= medium_max`) | `medium` |
| 7 to 10 | `heavy` |

Every signal counts the same. To make one signal matter less, raise its thresholds; to switch it off, set both
above anything you will ever see. A heavy session is one that is heavy on most signals at once, for example
three signals at `heavy` and one at `medium` (7 points).

### From tier to recommendation

`router.recommend` names the model to compare against, per tier. `null` means "keep the model you used":

```yaml
recommend: {light: gpt-6.1-sol, medium: gpt-6.1-sol, heavy: null}
```

So by default heavy sessions are never second-guessed, and light and medium ones are compared with
`gpt-6.1-sol`. The recommended cost is **the session's token usage re-priced on that model**, one 15-minute slot at a
time (see "Money is attributed to when it was spent" below): in each slot every model's tokens are added together, each
bucket (uncached input, cache read, cache write, 1-hour cache write, output) at the recommended model's own rate for that
bucket (`cost.usage_cost_ucents`), and the slots are added up.

A switch is **advised** only when the re-priced cost is at least `min_saving_ratio` times lower (default
`1.2`). The rule, in integers:

1. `recommended >= actual` -> not advised, note `already cheaper than <model>`.
2. else `actual * den < recommended * num` -> not advised, note `<model> is only 1.1x cheaper (minimum 1.2x)`
   (the ratio in the note is rounded **down** to one decimal, so a session that is 1.198 times cheaper reads
   `1.1x` and never `1.2x` next to a minimum of `1.2x`).
3. else -> advised.

`num/den` is `min_saving_ratio` converted exactly (the YAML float `1.2` becomes `Decimal("1.2")`, which is
`6/5`), so `1.2` means exactly 1.2 and a session that is precisely 1.2 times cheaper is advised. No float
touches a comparison or an amount. Sessions whose usage is entirely on the recommended model get the note
`already on <model>`; a tier with no recommendation gets `<tier>: keep` (`heavy: keep` by default); a session
whose usage costs nothing (no tokens at all, or a free model) gets `no cost`, and a session with no tokens
reads `you spent $0.00 (no token usage recorded)`.

### Sessions without a verified price

If any part of a session's usage is on a model whose price is `unverified` or missing from the table, the
session is **not advised on** (note `no verified price`), stays **out of every savings total**, and is
counted separately ("N sessions with an unverified price left out of these totals"). The priced part of its
spend is still shown on its own line, marked `(priced part only)`. Every unpriced model is named in a
`warning:` line, and so is a recommended model without a verified price. This is the same rule as every
other AgentFare command, with one difference to keep in mind: `agentfare summary` adds up the priced part
of a partly priced session, `advise` leaves the whole session out because a savings percentage on half a
session would be meaningless.

### Money is attributed to when it was spent

The database keeps each session's tokens per model **and per UTC 15-minute slot**: the slot of the moment each response
was made (`models.slot_of`), not one lump at the time the session started. `advise` uses that three ways.

* **A period holds the money spent inside it.** With `--week`, `--week-of`, `--since` or `--until`, a session that
  runs over the edge of the period is listed with only what it spent inside it, and its detail line says
  `(the part inside this period)` (`"clipped": true` in `--json`). Its start day, tool calls, files, errors, prompts and
  active time are still those of the whole session.
* **The tier belongs to the whole session**, in `advise` and in the router figures of `report` alike, so it does not change with the period you asked for: the counters are
  whole-session already, and the `input_tokens` signal is read from the session's tokens over its whole life
  (`Store.whole_usage`), not from the part inside the period. A long session that is `heavy` (kept) stays `heavy` when you
  look at one of its weeks, instead of turning `light` and being advised a switch because one week of it is small.
* **The decision to switch belongs to the whole session as well.** The recommended model, the `already cheaper` and `already
  on` checks and the `min_saving_ratio` gate are taken on the session's tokens over its whole life (`Store.whole_usage`, per
  model), like its tier. Only then are the actual and the recommended cost split by slot and cut to the period. So the weekly
  rows add up across periods: the rows of `advise --all` are the rows of `advise --week-of` for each of those weeks, and of
  `report`. The price of this is that a slice can show a saving below zero: a session that ran on an expensive model in
  week 1 and on a model cheaper than the recommended one in week 2 is moved as a whole, and week 2 alone reads "Router" above
  "Actual". The signature line then says `not cheaper for the part inside this period`. A session with a model without a
  verified price **outside** the period is decided on what is inside it, because that part is fully priced.
* **The weekly table sums slot by slot.** Each slot is in the ISO week of its own time in the configured timezone, so a
  session that began on Sunday night and went on after midnight adds what it spent before midnight to one week and the rest
  to the next. It counts as a session in both rows and once in the Total row.

Actual and Router come from the same per-slot integers: each (session, model, slot) is priced on its own and rounded
half up, exactly as `summary`, `sessions` and `models` do, so for the same period the Actual total of the four commands
is one integer, and `Saved = Actual - Router` to the micro-cent.

---

## 2. `advise`: what it prints

```
agentfare advise [--week | --week-of YYYY-Www] [--since D] [--until D] [--project NAME]
                 [--agent A] [--limit N] [--all] [--json]
```

The filters are the ones `summary`, `sessions` and `models` have, with the same meaning and the same errors
(exit code 2 and `error: ...`): `--week` is the ISO week that contains today (`AGENTFARE_NOW` if set),
`--week-of 2026-W39` a given one, `--since`/`--until` are inclusive local days (weeks and days follow
`timezone` in the config), and `--week` cannot be combined with `--since`/`--until`; `--project` matches
case-insensitively. New log lines are ingested first unless you pass the global `--no-ingest`.

### The default view

1. **The list**: the top `--limit` sessions (default 15) where a switch is advised, biggest saving first (ties:
   bigger spend, then key), each as the signature line plus a dim line saying why:

   ```
   1. session 7c8d9e0f: you spent $0.76 on gpt-6-astra → gpt-6.1-sol would likely
      have handled this for ~$0.1460 (5.2x cheaper)
      codex, fake-repo, 2026-09-29: score 2/10 (light): files_touched 2 +1,
      active_minutes 10 +1
   ```

   The arrow is `->` when the console the text goes to cannot write `→` (`PYTHONIOENCODING=ascii`, an old terminal or
   pipe), so the command never stops on it; `--json` always has the `→`. A character of a project name that the console
   cannot write shows as `?`.

   `session 7c8d9e0f` is the **last 8 characters** of the agent's session id, the same short id `agentfare
   sessions` prints. The first 8 are useless for Codex, whose ids are UUIDv7 and begin with a timestamp (on the
   author's 2,275 Codex files only 1,518 first-8 prefixes are distinct; the last 8 are all distinct). The full id
   is in `--json`. Amounts follow the other commands: whole cents from $1 up (`$4.20`), and below $1 two
   decimals when the amount is a whole cent (`$0.76`) or four when it is not (`$0.1460`). A session that used
   more than one model says `(+1 more model)` and its cost is the sum over all of them. The model named is the one that wrote the
   most **inside the period you asked for** (most output tokens, then most tokens, then the name), and the count is of the
   other models with usage inside it, so a day of a session that ran on another model before is named after what ran that day.

   When the recommended model belongs to **another provider** than the models of the session (providers come from the
   price table; a model with no provider named there is never called foreign), the detail line says so, and the signature
   line keeps its format. For example:

   ```
   1. session 3f2a91c4: you spent $4.20 on claude-opus-5-5 → gpt-6.1-sol would likely
      have handled this for ~$0.84 (5.0x cheaper)
      claude, acme-shop, 2026-09-29: score 1/10 (light): files_touched 2 +1. gpt-6.1-sol
      is another provider's model: a price comparison, not something this agent can run
   ```

   The comparison is about prices: Claude Code cannot run `gpt-6.1-sol`. Point `router.recommend` at a model your agent
   supports to get advice it can follow (section 8).

2. **Sessions that are counted but not listed.** A session is listed on its own only when a person drove it
   (`prompts >= 1`) **and** it cost at least `router.advise.min_cost_cents` (default 10 cents). Everything else
   still counts in the totals and is folded into one summary line each:

   ```
   1904 sub-agent or background sessions with 0 prompts: $... spent, 327 switchable for $...; included in the totals, --all lists them.
   78 sessions under $0.10: $... spent, 0 switchable for $0.00; included in the totals, --all lists them.
   211 more sessions where no switch is advised (heavy, already cheaper, no verified price): $... spent; --all lists them.
   ```

   A session whose price is unknown is never called small (its cost is not known to be below anything): it goes
   to the last line. The "spent" amounts count fully priced sessions only, like the totals.

   Why: on the author's machine 87% of the 2,170 Codex sessions are sub-agent and auto-review threads with 0
   prompts (their model is picked by the parent agent, not by you), and a listing of 2,000 one-cent sessions
   would bury the ten that matter. Their money is not hidden: it is in the totals and in the summary lines,
   with the savings that would apply to them.

   `--all` removes the folding and lists every session, switches first (biggest saving), then the rest by
   spend, including `heavy: keep`, `already cheaper` and `no verified price` ones. Like in `agentfare sessions`,
   `--all` overrides `--limit`.

3. **Router savings**, one row per ISO week and a total row, then the split of the saving:

   ```
   Week      Sess  Switched   Actual   Router    Saved  Saved %
   ────────────────────────────────────────────────────────────
   2026-W40     3         1  $3.6310  $3.0170  $0.6140    16.9%
   ────────────────────────────────────────────────────────────
   Total        3         1  $3.6310  $3.0170  $0.6140    16.9%
   Of the saving, $0.6140 (100%) comes from sessions with prompts and $0.00 (0.0%)
   from sub-agent or background threads with 0 prompts.
   ```

   `Sess` counts fully priced sessions with usage in the week, `Switched` those with an advised switch, `Actual` is what
   they cost in that week, `Router` is the same spend with every advised switch applied (the others stay as they were),
   `Saved` is the difference and `Saved %` is `Saved / Actual` to one decimal, from integers, rounded half up. A session
   that spans two weeks is in both rows with the money of each, and once in the Total row.

   **One precision per row.** If any of the three amounts of a row is not a whole cent, all three show four decimals
   (`$3.6310`, as above); a row of whole cents keeps the compact form (`$136.05`). A column that mixed `$3.63` with `$0.6140` could not be read as one set of figures.

   **Where the saving comes from.** The line under the table splits `Saved` into what sessions with at least one prompt
   contributed and what sessions with 0 prompts (sub-agent, auto-review and other background threads) did. Both parts are
   sums of the same per-slot integers as the table, so they add up to its Saved. `router.savings_split(advice)` returns
   them as `(prompted_ucents, background_ucents)`, and the weekly report uses the same function; `--json` has them as
   `saved_split`. The line is left out when nothing is saved.

   **The figure to quote.** `agentfare report` leads with the saving in the sessions **you prompted yourself** (`In sessions you
   prompted yourself, a cost router would have saved X% ($Y) of the $Z they cost`) and gives the all-sessions figure second,
   labelled `across all sessions, including sub-agent and background threads`: a person chose the model of the first ones and
   nobody chose that of the others. When only some sessions could be compared (a session with a model without a verified price
   is left out), the percentage states its base (`of the $48.00 that could be compared`) instead of sitting next to a larger
   "spent" figure. When no session you prompted could be compared, the report says so and gives the all-sessions figure alone.

4. The **unverified-price warnings** (the same block `summary`, `sessions` and `models` print) and the
   **estimate caveat**, printed once, under the table.

The output has no ANSI codes when it is not a terminal, and every line fits in 80 columns. On a terminal too
narrow for the weekly table (about 60 columns with amounts in the thousands of dollars) the `Sess` and
`Switched` columns are left out first, with a line saying so, so that no amount is ever cut; `--json` always
has everything.

### `--json`

The same report the text is drawn from, so they cannot disagree: the listed sessions (not all of them, unless
`--all`), with stable keys and the conventions of the other commands: money is always
`{"cents": int, "ucents": int}` (1 cent = 1,000,000 ucents, `cents` is `ucents` rounded half up) and shares are
integer basis points.

```
period {label, since, until}     filters {project, agent}     counts {sessions, listed, candidates}
sessions[] {key, agent, session_id, short_id, project, started_at, day, prompts, clipped, model, models,
            tier, score, points{signal: 0|1|2}, signals{signal: value}, explanation,
            actual_complete, actual{cents, ucents},
            switch, recommended_model, recommended{..}|null, saved{..}, ratio, note, other_provider, line, other_models}
not_listed {background, small, no_switch}   each {sessions, switchable, actual, saved}; small also has below{..}
weeks[] and total {week, sessions, switched, skipped, actual, recommended, saved, saved_bp}
saved_split {prompted{..}, background{..}}
warnings[] {model, status, message, excluded_tokens}     caveat
```

`counts.candidates` is how many sessions the view could list before `--limit`. In a session, `actual`, `recommended` and
`saved` are the money inside the period asked for, `clipped` says that part of the session lies outside it, `model` is the
model that wrote the most inside the period and `other_models` how many more have usage inside it, and
`other_provider` that the recommended model is another provider's (section 2). `ratio` is the actual cost over
the recommended one, to two decimals, and null when no switch is advised; `saved_bp` is null when there was no
spend. A recommended model without a verified price is in `warnings` too, with `excluded_tokens: 0`. `status` is
`unverified` or `unknown` for a model that is not priced, and `partial` for one that is priced but has an
unverified optional price (cached input, cache write): its tokens are billed at a fallback rate and still counted.

---

## 3. Worked example

These are the three synthetic sessions in `tests/fixtures/` (the ones the README demo uses). Run them yourself:

```
AGENTFARE_HOME=$(mktemp -d) AGENTFARE_NOW=2026-09-30T12:00:00Z \
  agentfare --config tests/fixtures/config.yaml advise --all
```

| Session | input_tokens | tool_calls | files_touched | error_retries | active_minutes | Score | Tier | Outcome |
|---|---|---|---|---|---|---|---|---|
| Claude Code, opus-5-5 + haiku-4-5 | 3,416,900 (+1) | 46 (+1) | 10 (+2) | 5 (+1) | 98.8 (+2) | 7 | heavy | `heavy: keep` |
| Codex, gpt-6-astra | 100,000 (0) | 5 (0) | 2 (+1) | 1 (0) | 10.0 (+1) | 2 | light | switch to gpt-6.1-sol, 5.2x cheaper |
| Gemini, gemini-2.5-pro | 172,000 (0) | 7 (0) | 2 (+1) | 1 (0) | 9.9 (0) | 1 | light | `already cheaper than gpt-6.1-sol` |

The numbers behind the Codex line. The session used 40,000 uncached input, 60,000 cache-read and 6,000 output
tokens on `gpt-6-astra` (per million tokens: $10 input, $1 cached, $50 output):

```
actual         40,000 x $10 + 60,000 x $1    + 6,000 x $50 = $0.40 + $0.06  + $0.30 = $0.76
on gpt-6.1-sol 40,000 x $2  + 60,000 x $0.10 + 6,000 x $10 = $0.08 + $0.006 + $0.06 = $0.146
ratio          0.76 / 0.146 = 5.2x          0.76 x 5 >= 0.146 x 6  ->  advised
```

The Gemini session shows the guard: 92,000 uncached + 80,000 cached + 9,500 output tokens cost $0.22 on
`gemini-2.5-pro`, but $0.287 on `gpt-6.1-sol`, so nothing is advised. Its 9.9 active minutes (595 seconds)
just miss the 10-minute `medium` line, which is why `active_minutes` scores 0 while the Codex session's exact
600 seconds scores 1.

The same three sessions with `--json` give `"tier": "heavy"`, `"tier": "light"` and `"tier": "light"`, and for
the Codex one `"actual": {"cents": 76, "ucents": 76000000}`, `"recommended": {"cents": 15, "ucents": 14600000}`,
`"saved": {"cents": 61, "ucents": 61400000}` and `"ratio": 5.21`.

---

## 4. `run`: picking a model before you start

`agentfare run` has to choose a model before there is any history for the task, so it scores what it can see
without reading anything: the length of the prompt string, the number of files in the repo, and three keyword
families. `router.pick_for_run` produces a score, a tier, a model and **one human-readable line for every rule
that fired**.

| Rule | Points | Config | Default |
|---|---|---|---|
| Prompt length in characters | +1 from `medium`, +2 from `heavy` | `run.prompt_chars` | 400 / 1500 |
| Files in the repo (`repo_path`) | +1 from `medium`, +2 from `heavy` | `run.repo_files` | 200 / 1200 |
| Heavy keywords in the prompt | `+keyword_points` for the first distinct keyword, +1 for each further one, at most +2 more (2 to 4 points) | `run.heavy_keywords`, `run.keyword_points` | `refactor, architect, migrat, redesign, rewrit, overhaul, security, concurrency, refator, arquitet, migr, reescrev, reescrit, seguranc, concorrenc`; 2 |
| Light keywords in the prompt | the same, negative | `run.light_keywords` | `typo, renam, docstring, comment, format, lint, bump, readme, changelog, renomear, renome, comentar, comentario, formatar, erro de digitacao` |
| Medium keywords in the prompt | +1 once for the whole family, however many of them match | `run.medium_keywords` | `implement, feature, debug, bug, fix, test, optimi, perform, investigat, design, integrat, endpoint, api, schema, upgrade, funcionalidade, depur, teste, otimiz, desempenho, investig, projeto, integr, corrig` |

* **Keywords** match the start of a word, in any case and **with or without accents**: both the prompt and the stem are
  lower-cased and stripped of accents (Unicode NFKD, combining marks removed) before they are compared, so `migr` matches
  `migrar`, `migração` and `MIGRACAO`, and `erro de digitacao` matches `erro de digitação` (a phrase reads across any white
  space). `refactor` matches `Refactor`, `(refactor)`, `refactoring` and `refactored`; `architect` matches `architecture`
  and `architectural`; `format` matches `formatting` but not `information`. Nothing matches in the middle of a word
  (`prerefactor`). A verb that changes its ending is listed as a stem, so that one entry finds every form: `migrat` matches
  `migrate`, `migrated`, `migrating` and `migration`, `rewrit` matches `rewrite`, `rewriting` and `rewritten`, and `renam`
  matches `rename` and `renaming` (`migrate` alone would miss `migrating`). A prefix is coarse on purpose: `comment` also
  matches `commentary`, `typo` also matches `typography`, `format` also matches `formation` and `fix` also matches
  `fixture`, so write the longer stem when that matters. A word starts after anything that is not a letter, a digit or `_`,
  so `pre-refactor` and `lint-staged` match and `do_refactor` does not; text without spaces, such as Chinese or Japanese, is
  one long word. Characters like `+` are literal, so `c++` works (and matches `c++20`). Only the words from your config are
  ever printed, never text from the prompt.
* **English and Portuguese.** The defaults hold the stems of both languages; add the words you actually write. A Portuguese
  stem is written without accents and long enough to leave other words alone: `seguranc` finds `segurança` and `concorrenc`
  finds `concorrência` (the shorter `segur` and `concorr` also found `seguro` and `concorrer`), `projeto` finds `projeto` and
  `projetos`, and `corrig` (to fix) finds `corrigir` and `corrigido` but not `corrija` or `correção`, which start differently:
  add a stem for the forms you write.
* **Keywords that start the same word count once.** `migrate` starts with `migrat` and with `migr` (Portuguese), `renomear`
  with `renomear` and `renome`, `formatar` with `format` and `formatar`: the first keyword of the list that matches a word
  takes it, and the others that match that same word add nothing. The reason line names that first one, so `migrate the
  tables` is `heavy keyword migrat: +2`, not two keywords.
* **Keyword points.** A keyword counts once however many of its forms are in the prompt (`refactor` and
  `refactoring` together are one keyword) and however often the list holds it. For the heavy and the light family the first
  distinct keyword scores `keyword_points` (2), each further one adds 1, and no more than 2 further ones count: a family is
  worth 2, 3 or 4 points, heavy added and light subtracted. The medium family is worth +1 once, however many of its keywords
  are in the prompt. If words of several families are in the prompt, they add up (`refactor` and `migrate` with `typo` is
  +3 - 2 = +1, and `implement` adds 1 more). With `keyword_points: 0` every family is off.
* **Repo size** is a walk over directory listings that stops as soon as the "heavy" count is reached, so it is
  cheap in any repo. It skips `.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`, `build`, `dist`, `target`,
  `__pycache__` and the usual tool caches (`.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.next`,
  `.nuxt`, `.svelte-kit`, `.turbo`, `.gradle`, `.terraform`, `.pnpm-store`) at any depth, does not follow
  symlinks, counts every other file (source or not), never opens one, and treats an unreadable directory as
  empty. A count that hit the cap is shown as `1200+`. Without a usable `repo_path` the rule simply does not
  fire. The repo is the directory `agentfare run` is started in.
* **Score and tier.** The score is the sum of the points, never below 0; **the maximum is 9** with the defaults
  (prompt 2 + repo 2 + a heavy family of three or more keywords 4 + the medium family 1; in general `7 + keyword_points`).
  `run.tiers` (default `{light_max: 0, medium_max: 2}`) cuts it: 0 light, 1 and 2 medium, 3 and up heavy. This scale is
  separate from `router.tiers` because this score comes from a few coarse rules instead of five signals (10).
* **Model.** `run.models[agent][tier]`. An agent with no entry (a custom one from your `agents:` list, whose
  `commands` are matched by basename) gets no model and runs unchanged, with the warning
  `no model configured for agent 'X' (router.run.models): run it with its own default`; a `null` entry gets
  `no <tier>-tier model configured for <agent>`. So a custom agent needs `router.run.models.<agent>` entries as
  well as its `agents:` entry to be routed. The pick carries its warnings as a list, and `run` prints each one on
  its own line.
  Shipped: Claude `claude-sonnet-5-5` / `claude-sonnet-5-5` / `claude-opus-5-5`, Codex `gpt-6.1-sol` /
  `gpt-6.1-sol` / `gpt-6-astra`, Gemini `gemini-3.1-flash-lite` / `gemini-3-flash-preview` /
  `gemini-3.1-pro-preview` (all accepted by Gemini CLI 0.60 and all with a verified price).
* **Model aliases.** Claude Code takes short names (`claude --model opus`) that are not in the price table.
  `router.run.model_aliases` maps them to the model each one stands for, so that the estimate can price them: the defaults
  are `opus: claude-opus-5-5`, `sonnet: claude-sonnet-5-5`, `haiku: claude-haiku-4-5` and `fable: claude-fable-5-1`. Map
  them to what you actually run; Claude Code resolves the alias itself, so the command keeps `--model opus` as you wrote it
  and only the estimate changes. The panel says `opus (alias for claude-opus-5-5)`, and a router pick that is the model an
  alias stands for counts as agreeing with it. A mapping you write is merged over the shipped one (an alias you do not
  mention keeps its default), names are matched in any case, and an alias of a model without a verified price warns about
  that model.
* **Estimated cost.** The token budget is the **median of your own past sessions** of the same agent and tier
  (per token bucket, integer midpoint for an even count) once there are **at least 5**; the tier of a past
  session is the one `advise` gives it (section 1), and only sessions a person drove (`prompts >= 1`) count,
  because sub-agent threads would drag the median to nothing. Below 5 it is
  `run.default_tokens[tier]`. The panel says which one it used: `median of 7 past claude/light sessions`, or
  `typical light task (no history yet)`. That budget is priced on the picked model.
* **History is read, never written.** `run` (with or without `--dry-run`) never reads your agent logs and never creates
  the database, so the decision is on screen at once, even on a first run. It opens the existing database read-only for the
  history. Without a database, or with one that holds no session, it uses `run.default_tokens` and says
  `no usage history yet (run agentfare ingest to personalize estimates)` as a warning. A database it cannot read (a damaged
  file, one that is locked by a writer, one written by an older version, which the next `ingest` or report upgrades) gets the default budgets
  too, with `usage history unavailable, so estimates use the default token budgets (...)` and the reason.
* **Baseline.** What you would run without the router, which is **the model you run now**: the one with the most spend for
  this agent over the last 30 days of your history (priced spend, canonical model ids; models without a verified price are not
  compared), priced on the same token budget. The 30 days are counted back from the newest spend in the database (of any
  agent), never from the clock, so the same database always gives the same answer. The panel names it and the day the window
  starts: `baseline ~$0.5050 on claude-opus-5-5 (your most used claude model in the last 30 days, since 2026-08-31)`. With
  no priced spend of this agent in that window it is the model with the most spend overall (`your most used claude model
  overall, none in the last 30 days`), and with no priced history at all it is the agent's configured heavy-tier model
  (`what you would run without the router`). `--json` says which in `baseline_basis` (`recent`, `all_time` or `configured`)
  and gives the first day of the window in `baseline_since`. When the router runs the baseline itself there is nothing to
  save and the panel has **no saving line** (an alias counts as the model it stands for, so `--model opus` against a baseline of
  `claude-opus-5-5` is the baseline itself); when a different model costs the same it says `none: same cost as the baseline`,
  and when the pick costs more, the saving line says `none: ~$X MORE than the baseline`. A model without a verified price gets
  `est_ucents = None` and the warning `no verified price for X: cost estimate unavailable` (and `baseline cost unavailable`
  for the baseline).
* **`--force-model`.** The score and reasons are still computed and shown, but the model is yours (`forced` is
  true) and the estimate is priced on it.
* **`--dry-run --json`** prints the decision as one JSON object instead of the panel, for scripts (`--json` without
  `--dry-run` is a usage error, exit code 2):

  ```
  agent, tier, score, reasons[], model, forced, estimate{cents, ucents}|null, baseline{cents, ucents}|null,
  baseline_model, baseline_basis, baseline_since, history_sessions, warnings[], argv[]
  ```

  `model` is the model the agent will run with (null when it keeps its own default), `estimate` and `baseline` are null when
  there is no price, or when the command has no way to receive the model, and `argv` is the command the agent would get with
  the prompt replaced by `<prompt: N chars>` (as in the panel, every other argument is `<arg: N chars>`, except the program, the subcommand words it knows, the model flag with its value and the names of the flags).
  A command that never talks to a model (`claude mcp list`) has `tier`, `score`, `model` and the money null and no reasons.
  Nothing in it is ever prompt text, only counts and the words of your own config.

### Which tier a prompt gets with the shipped defaults

Seven prompts, each on its own in a repo of a few files (`agentfare run --dry-run -- claude -p "..."`):

| Prompt | What fires | Score | Tier |
|---|---|---|---|
| `fix a typo in the README` | light `typo, readme` -3, medium `fix` +1 | 0 | **light** |
| `add a unit test for parse()` | medium `test` +1 | 1 | **medium** |
| `implement a feature to export CSV` | medium `implement, feature` +1 | 1 | **medium** |
| `rewrite the auth layer` | heavy `rewrit` +2 | 2 | **medium** |
| `refactor the architecture and migrate the database` | heavy `refactor, architect, migrat` +4 | 4 | **heavy** |
| `refatorar a arquitetura e migrar o banco de dados` | heavy `refator, arquitet, migr` +4 | 4 | **heavy** |
| `explain what this function does` | nothing | 0 | **light** |

`tests/test_router.py` pins this table.

### How `run` reaches heavy with the shipped defaults

Medium starts at 1 point and heavy at 3. One heavy keyword is worth 2, and so is the biggest prompt or the biggest repo,
so none of them reaches heavy alone: it takes a second reason. That is a second heavy keyword, a size or a medium keyword on
top of a heavy one, or a big size on top of a smaller reason:

| What the prompt and the repo hold | Score | Tier |
|---|---|---|
| Nothing that fires, or light keywords that cancel what does | 0 | light |
| Only one +1 reason: a medium keyword, a prompt of 400+ characters, or a repo of 200+ files | 1 | medium |
| One heavy keyword (+2) | 2 | medium |
| Only a prompt of 1,500+ characters (+2), or only a repo of 1,200+ files (+2) | 2 | medium |
| Two +1 reasons, for example a prompt of 400+ characters and a repo of 200+ files (+1 +1) | 2 | medium |
| **Two heavy keywords** (+2 +1) | 3 | **heavy** |
| **One heavy keyword and a medium keyword** (+2 +1) | 3 | **heavy** |
| **One heavy keyword and a prompt of 400+ characters** (+2 +1) | 3 | **heavy** |
| **One heavy keyword and a repo of 200+ files** (+2 +1) | 3 | **heavy** |
| A prompt of 1,500+ characters and a repo of 200+ files, or a prompt of 400+ characters and a repo of 1,200+ files, no keyword (+2 +1) | 3 | **heavy** |
| A prompt of 1,500+ characters and a repo of 1,200+ files, no keyword (+2 +2) | 4 | **heavy** |
| Two heavy keywords and one light keyword (+3 -2), no size | 1 | medium |
| Two heavy keywords and two light ones (+3 -3), no size | 0 | light |

One example for each outcome, as `agentfare run --dry-run` prints it (the synthetic fixtures' config, `claude`, the
built-in prices of 2026-09-30, a fresh data directory, a working directory of a few files, so the repo rule does not fire):

```
AGENTFARE_HOME=$(mktemp -d) AGENTFARE_NOW=2026-09-30T12:00:00Z \
  agentfare --config tests/fixtures/config.yaml run --dry-run -- claude -p "fix the typo in the README"
```

Light, `fix the typo in the README`: two light keywords, -3, and the medium keyword `fix`, +1, floored at 0.

```
╭─ agentfare run (dry run) ────────────────────────────────────────────────────╮
│ agent     claude                                                             │
│ prompt    26 chars, from the command line                                    │
│ tier      light (score 0)                                                    │
│           - light keywords typo, readme: -3                                  │
│           - medium keyword fix: +1                                           │
│ model     claude-sonnet-5-5 (router pick)                                    │
│ estimate  ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet) │
│ baseline  ~$0.5050 on claude-opus-5-5 (what you would run without the        │
│           router)                                                            │
│ saving    ~$0.1925 (38.1% less than the baseline)                            │
│ command   claude --model claude-sonnet-5-5 -p '<prompt: 26 chars>'           │
│ warning   no usage history yet (run agentfare ingest to personalize          │
│           estimates)                                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Medium, `refactor the billing module`: one heavy keyword, +2, and nothing else.

```
╭─ agentfare run (dry run) ────────────────────────────────────────────────────╮
│ agent     claude                                                             │
│ prompt    27 chars, from the command line                                    │
│ tier      medium (score 2)                                                   │
│           - heavy keyword refactor: +2                                       │
│ model     claude-sonnet-5-5 (router pick)                                    │
│ estimate  ~$3.01 on claude-sonnet-5-5, typical medium task (no history yet)  │
│ baseline  ~$4.25 on claude-opus-5-5 (what you would run without the router)  │
│ saving    ~$1.25 (29.3% less than the baseline)                              │
│ command   claude --model claude-sonnet-5-5 -p '<prompt: 27 chars>'           │
│ warning   no usage history yet (run agentfare ingest to personalize          │
│           estimates)                                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Heavy, `refactor the billing module and migrate it to the new API`: two distinct heavy keywords, +3, and the medium keyword
`api`, +1. The router picks the agent's heavy model, which is also the baseline, so there is nothing to save and the panel has
no saving line.

```
╭─ agentfare run (dry run) ────────────────────────────────────────────────────╮
│ agent     claude                                                             │
│ prompt    57 chars, from the command line                                    │
│ tier      heavy (score 4)                                                    │
│           - heavy keywords refactor, migrat: +3                              │
│           - medium keyword api: +1                                           │
│ model     claude-opus-5-5 (router pick)                                      │
│ estimate  ~$43.78 on claude-opus-5-5, typical heavy task (no history yet)    │
│ baseline  ~$43.78 on claude-opus-5-5 (what you would run without the router) │
│ command   claude --model claude-opus-5-5 -p '<prompt: 57 chars>'             │
│ warning   no usage history yet (run agentfare ingest to personalize          │
│           estimates)                                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

After `agentfare ingest` has put your logs in the database, the warning is gone and the baseline is named after your own
use (`baseline ~$0.5050 on claude-opus-5-5 (your most used claude model in the last 30 days, since 2026-08-31)`). With five or more past sessions of the agent and
tier the `estimate` line ends in `median of N past claude/light sessions` instead. A model the command already selects
can be one of Claude Code's aliases:

```
agentfare run --dry-run -- claude -p "fix the typo in the README" --model opus
│ model     opus (the command already selects it, the router pick below is     │
│           advisory)                                                          │
│ router    would pick claude-sonnet-5-5 instead: ~$0.3125 on                  │
│           claude-sonnet-5-5, typical light task (no history yet)             │
│ estimate  ~$0.5050 on opus (alias for claude-opus-5-5), typical light task   │
│           (no history yet)                                                   │
```

The rules can also add up to more than the minimum: a Codex session with
a 1,559-character prompt that starts with "Refactor the billing module and migrate it to the new API." and has no other
keyword, in a repo of 1,300 files (`agentfare run --dry-run --prompt-file prompt.txt -- codex`) gets

```
│ tier      heavy (score 8)                                                    │
│           - prompt is 1559 chars (>= 1500): +2                               │
│           - repo has 1200+ files (>= 1200): +2                               │
│           - heavy keywords refactor, migrat: +3                              │
│           - medium keyword api: +1                                           │
│ model     gpt-6-astra (router pick)                                          │
```

and `fix the typo in the README` in that same repo scores `repo has 1200+ files (>= 1200): +2`, `light keywords typo,
readme: -3` and `medium keyword fix: +1` = 0, tier light, model `gpt-6.1-sol` (`claude-sonnet-5-5` for Claude). When
nothing fires the single line is `no rule fired: score 0`.

---

## 5. Every config key

`agentfare config --init` writes this file with comments; a key you delete gets its default, and a `router:`
block emptied by comments keeps all defaults.

| Key | Default | Meaning |
|---|---|---|
| `router.signals.input_tokens` | `{medium: 2000000, heavy: 25000000}` | Total input tokens of the session (all models; uncached + cache read + cache write) |
| `router.signals.tool_calls` | `{medium: 25, heavy: 180}` | Tool invocations |
| `router.signals.files_touched` | `{medium: 1, heavy: 6}` | Distinct files written or edited |
| `router.signals.error_retries` | `{medium: 2, heavy: 8}` | Tool calls that returned an error |
| `router.signals.active_minutes` | `{medium: 10, heavy: 80}` | Active seconds / 60 (idle gaps removed) |
| `router.tiers` | `{light_max: 2, medium_max: 6}` | Score 0-10 to tier: `<= light_max` light, `<= medium_max` medium, else heavy |
| `router.recommend` | `{light: gpt-6.1-sol, medium: gpt-6.1-sol, heavy: null}` | Model to compare against per tier; `null` keeps yours. Must be in the price table with a verified price |
| `router.min_saving_ratio` | `1.2` | Advise a switch only when it is at least this many times cheaper. At least 1 |
| `router.advise.min_cost_cents` | `10` | A session with prompts is listed on its own only above this cost (cents, decimals allowed). Sessions with 0 prompts are always summarised |
| `router.run.tiers` | `{light_max: 0, medium_max: 2}` | Same as `router.tiers` for the `run` score (0-9 with the defaults): `<= light_max` light, `<= medium_max` medium, else heavy |
| `router.run.prompt_chars` | `{medium: 400, heavy: 1500}` | Prompt length thresholds |
| `router.run.repo_files` | `{medium: 200, heavy: 1200}` | File-count thresholds |
| `router.run.heavy_keywords` | see section 4 | Word stems that add points; a keyword matches every word that starts with it, in any case and with or without accents. English and Portuguese |
| `router.run.light_keywords` | see section 4 | Word stems that subtract points, matched the same way |
| `router.run.medium_keywords` | see section 4 | Word stems of ordinary feature and bug work: the family adds 1 once |
| `router.run.keyword_points` | `2` | Points for the first matching keyword of the heavy and light families; each further distinct one adds 1, at most 2 more (a whole number; 0 turns every keyword family off) |
| `router.run.model_aliases` | `{opus: claude-opus-5-5, sonnet: claude-sonnet-5-5, haiku: claude-haiku-4-5, fable: claude-fable-5-1}` | Short model names of Claude Code and what each stands for, for the estimate. Map them to what you actually run; Claude Code resolves the alias itself |
| `router.run.models.<agent>.<tier>` | see section 4 | Model to run per agent and tier; a custom agent from `agents:` needs its own row, or it runs unchanged with a warning |
| `router.run.default_tokens.<tier>` | see below | Token budget for the cost estimate until 5 past sessions exist |

`default_tokens` (`input`, `cache_read`, `cache_write`, `output`, and optionally `cache_write_1h`, per tier):

| Tier | input | cache_read | cache_write | output |
|---|---|---|---|---|
| light | 30,000 | 600,000 | 25,000 | 7,000 |
| medium | 135,000 | 8,800,000 | 130,000 | 65,000 |
| heavy | 770,000 | 118,000,000 | 1,500,000 | 480,000 |

A bad value (a threshold that is not a number, `medium` above `heavy`, an unknown tier, a `min_saving_ratio` below 1) stops
the command with `error: ...` (exit code 2) naming the key. A name under `router.signals` that is not one of the five signals
does not stop anything: it is ignored, and the config warning below names it with the closest real signal, so a typo is never
silent (the signal it was meant to be keeps its default). The thresholds of the five real signals are still checked.

A key that does not exist anywhere in the config (the top level, `router`, `router.signals`, `router.tiers`, `router.run`,
`agents`, `sources`) is warned about on stderr by every command, once, with the closest real key when there is one:
`warning: /home/dev/.agentfare/config.yaml: unknown key router.min_saving_ratioo (did you mean min_saving_ratio?)`. Every warning starts with the config file as you gave it (`--config` or `AGENTFARE_CONFIG`), else the path where it was found, else `built-in config`. The agent names under
`agents`, `router.run.models` and the alias names under `router.run.model_aliases` are free-form. A model named in
`router.recommend` or `router.run.models` that has no verified price in the price table is warned about too (`router.recommend.light:
gpt-not-a-model has no verified price, so advise cannot use it`), unless it is still the shipped default.

---

## 6. How the defaults were calibrated

The shipped thresholds are not guesses about "what a big task is"; they are the rounded terciles of real
sessions, so that each signal gives roughly a third of sessions 0, 1 and 2 points, and the tiers split
roughly a third each. Measured on 2026-09-30 on the author's machine: 2,374 sessions (204 Claude Code, 2,170
Codex CLI; no real Gemini logs exist there, so Gemini is only covered by the synthetic fixtures).

**Which sessions.** Only the 470 that have at least one prompt (185 Claude, 285 Codex). The other 1,904 have 0
prompts: sub-agent and auto-review threads (1,885 Codex) and a few automated Claude sessions. They are small
(the Codex ones have a median of 4 active minutes and 14 tool calls) and would pull every percentile down;
nobody picks their model by looking at a session, so the thresholds describe the sessions a person actually runs.

**Signals**, pooled over those 470 (Claude and Codex together, because thresholds are not per agent):

| Signal | 33rd percentile | 67th percentile | Shipped medium / heavy | Share of sessions scoring 0 / 1 / 2 |
|---|---|---|---|---|
| `input_tokens` | 1.8M | 24.6M | 2M / 25M | 35% / 32% / 33% |
| `tool_calls` | 25 | 180 | 25 / 180 | 32% / 35% / 33% |
| `files_touched` | 0 | 6 | 1 / 6 | 38% / 26% / 36% |
| `error_retries` | 1 | 8 | 2 / 8 | 33% / 33% / 34% |
| `active_minutes` | 8.8 | 81.5 | 10 / 80 | 36% / 31% / 34% |

`files_touched` and `error_retries` are small integers with a long run of zeros and ones, so `medium` is
"at least one file" and "at least two errors" (the 40th percentile) rather than the 33rd. The
placeholder thresholds these replaced (300k and 3M input tokens, 40 and 200 tool calls, ...) scored 61% of
real sessions at `heavy` on input tokens alone (and 90% at `medium` or above), because a real agent session
re-reads its context on every turn: the median Claude session has 12M input tokens.

**Tiers.** The score histogram of the 470 sessions (scores 0 to 10) is `86 45 35 22 35 45 30 19 15 30 108`:
bimodal, because the signals are correlated (a long session is long on every count). Cutting at
`light_max: 2`, `medium_max: 6` gives:

| | light | medium | heavy |
|---|---|---|---|
| all 470 | 35% | 28% | 37% |
| Claude (185) | 36% | 22% | 43% |
| Codex (285) | 35% | 32% | 33% |

Claude sessions come out heavier than Codex ones because one set of thresholds serves both agents (the median
Claude session has 93 tool calls against 60 for Codex, and far larger caches); if you use one agent mostly,
tune to that agent.

**Sanity check: how much does `advise` call switchable?** On the same machine, with the shipped defaults and
the price table of 2026-09-30 (its `gpt-5.6-*` rows were still unverified when the thresholds above were
measured): 85% of sessions have a fully verified price (the other 362 are `codex-auto-review` threads, whose
price is unverified). Of those, `advise` marks **33% of the sessions** as switchable, worth **8.8% of their
list-price spend**. Restricted to sessions with prompts it is 44% of the sessions and **4.4% of the spend**; more
than half of the saving comes from sub-agent threads. The heavy tier is only 14% of fully priced sessions but
89% of the spend, and it is always kept, which is why the saving is a small share of the bill. 74% of the total
saving is one pair: Codex sessions on `gpt-6-astra` re-priced on `gpt-6.1-sol` (typically 7x cheaper: $10 vs $2
input, $50 vs $10 output, $1 vs $0.10 cached input). Across all advised switches the ratio has a median of 6.2x
and a 10th percentile of 1.6x (sessions on `gpt-5.6-terra`, whose prices are close to `gpt-6.1-sol`'s). That is
plausible: the switchable sessions are the small and medium ones, and they are mostly the ones that ran on the
most expensive model, so per session the saving is large while per dollar of the whole bill it is modest.
Whether a cheaper model would really have done the job is another matter (section 7).

**Run thresholds.** `repo_files` (200 / 1200) is the rounded terciles of the file counts of the 17 git repos
on that machine (33rd percentile 189, 67th 1,191): a small sample, so treat it as a starting point.
`prompt_chars` (400 / 1500), `keyword_points`, the keyword lists and `run.tiers` are judgment values, not fitted: prompts are
never stored, so there is nothing to fit them to. The cut points `{light_max: 0, medium_max: 2}` are chosen so that any one
reason is enough for medium (so a prompt about a feature or a test is not treated as a typo fix), while one heavy
keyword, a big prompt or a big repo alone stays medium, and heavy needs a second reason (section 4). The medium keywords
and the Portuguese stems were added after the first audit of the defaults, which had no rule for ordinary feature work. `default_tokens`
is the midpoint of the Claude and Codex medians of the
sessions with prompts in each advise tier; against those agents' own medians it is within 10% for light,
within 1.0x to 1.6x for medium and within 0.7x to 2.7x for heavy, which is why the estimate switches to your
own median as soon as you have 5 sessions of that agent and tier.

---

## 7. Limits: read this before believing a number

* **The estimate re-prices the same tokens on another model.** It does not know whether that model would have
  needed more turns, more retries or more thinking to do the same job. Tokenizers differ between vendors (the
  same text is a different number of tokens), reasoning models spend different amounts of thinking, and a
  weaker model may loop. The real cost on another model can be higher or lower; the line says "would likely
  have handled this", and the caveat is printed under every table.
* **Quality is not measured.** The router only knows what a session cost and how big it looked. "Light" is a
  claim about size, not about difficulty: a one-line change to a subtle bug is light on every signal and
  still hard.
* **Caching is assumed to behave the same.** Cache-read tokens are re-priced at the recommended model's
  cached-input rate, cache writes at its cache-write rate (input rate when the model has none). A model with a
  worse cache discount, a shorter cache lifetime or no caching would cost more than the estimate. Google's
  cache *storage* fee is not modelled.
* **The recommendation ignores what your tool can run.** `advise` compares prices; it cannot make Claude Code
  run `gpt-6.1-sol`. When the recommended model is another provider's than the session's, the detail line says so. If you
  only use one vendor, set `router.recommend` to that vendor's cheaper models (any model in the price table with a
  verified price).
* **Sessions are the unit, not tasks.** One session that handled five unrelated chores looks heavy and is
  kept; five short sessions look light. Sub-agent threads are their own sessions with 0 prompts, so a
  parent that fans out into many sub-agents shows up as many small sessions.
* **Sub-agent and auto-review sessions are counted, but you did not choose their model.** They are in the
  totals and in the summary line; changing what they cost means changing the parent tool's configuration.
* **Thresholds are pooled across agents**, so an agent whose sessions are systematically bigger lands in
  heavier tiers (section 6).
* **Prices are a snapshot** (`src/agentfare/data/pricing.yaml`, sources in `docs/PRICING-SOURCES.md`). Models
  with an unverified price are left out and warned about; long-context surcharges, fast mode, batch and
  regional pricing are not modelled.
* **Weeks are in your configured timezone**, and only fully priced sessions add up in the weekly table, so
  its totals can differ from `agentfare summary` for the same period (which also adds up the priced part of a partly
  priced session). Money is attributed to the 15-minute slot it was spent in, so a session that spans two weeks adds to both.
* **The tier and the decision to switch belong to the whole session that spans a period edge**, but its money is only what was
  spent inside the period, so a period can show a switch advised on a slice of a session whose other part is elsewhere, and
  that slice can cost less than the advised model would have (a saving below zero, section 1).
* **`run` is a guess about a task nobody has run yet.** Prompt length, repo size and keywords are weak
  signals, and a keyword that matches the start of a word can be wrong (`comment` matches `commentary`, `fix` matches
  `fixture`); the point is that the guess is visible, one line per rule, and that you can override it. A prompt in a
  language other than English or Portuguese only scores on its length and the repo, until you add its stems.
* **The baseline of `run` is your own most used model of the last 30 days**, which is not always what you would have run for
  this task; the panel names it and the day its window starts so that the comparison can be judged. `run` never ingests, so the
  window ends at the newest spend of your last `agentfare ingest`, and the token budget of the estimate comes from the history
  of that ingest as well.

---

## 8. Tuning

Look at your own distribution first (needs `jq`; `--all` lists every session):

```
# how many sessions of each tier, among those a person drove
agentfare advise --all --json | jq '[.sessions[] | select(.prompts > 0) | .tier] | group_by(.) | map({(.[0]): length}) | add'

# your own terciles of a signal (here tool_calls)
agentfare advise --all --json | jq '[.sessions[] | select(.prompts > 0) | .signals.tool_calls] | sort | [.[length/3|floor], .[length*2/3|floor]]'
```

Then edit `~/.agentfare/config.yaml`:

* **Too many sessions "heavy" (or too few):** move the `router.signals` thresholds to your own terciles, or move
  `router.tiers`. Raising `medium_max` by one point moves the sessions that score exactly that many from heavy to medium.
* **You want fewer, safer suggestions:** raise `router.min_saving_ratio` (`2` advises only switches that at least
  halve the cost) or set `recommend.medium: null` so only light sessions are compared.
* **You only use one vendor:** point `recommend` at that vendor's cheaper models, for example
  `recommend: {light: claude-sonnet-5-5, medium: claude-sonnet-5-5, heavy: null}`; the model must exist in the
  price table (`agentfare prices` lists it) or the session gets `<model> has no verified price`.
* **The list is still noisy:** raise `router.advise.min_cost_cents`.
* **A signal misleads you:** raise both of its thresholds far beyond anything you will see to switch it off
  (`files_touched: {medium: 1000000, heavy: 1000000}`); there are no weights to tune, on purpose.
* **`run` picks heavy too often:** heavy needs 3 points by default; set `run.tiers.medium_max` to 3 and it needs 4
  (two heavy keywords, or a heavy and a medium keyword, are then medium; a big prompt and a big repo are still heavy).
  **Too rarely:** lower `run.prompt_chars` and `run.repo_files`, or add keywords; every further distinct keyword of a
  family adds a point.
* **`run` picks medium for a short, plain prompt:** any one reason (+1) is medium by default. Set `run.tiers.light_max` to 1
  and a medium keyword, a prompt of 400+ characters or a repo of 200+ files alone are light again.
* **`run` picks the wrong tier for another reason:** change `run.prompt_chars`, `run.repo_files`, the keyword lists,
  `keyword_points` or `run.tiers`; add your own agent under `run.models` (and under `agents:`). Keywords match the
  start of a word, in any case and with or without accents, so the stem is enough and you add the words you actually
  write: `refator` covers `refatorar` and `refatoração`, `migr` covers `migrar` and `migração` (both are already there).
* **Claude Code's `--model` aliases cost the wrong model:** set `run.model_aliases` to what each alias is for you
  (`opus: claude-opus-5-5`); `run` only prices the alias, Claude Code still resolves it.

After any change, `agentfare advise --all` shows the new score and its one-line explanation for every session, so
the effect of a change is visible per session instead of hidden in a total.

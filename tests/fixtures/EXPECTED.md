# Expected numbers for the synthetic fixtures

Hand-verified expectations for the three synthetic sessions in `tests/fixtures/` (one per agent). Every dollar
below is a whole number of tokens times a price per 1M tokens, written out line by line so that a person can
audit it with a pencil. `tests/test_e2e_fixtures.py` asserts these numbers, as literals, against the real CLI.

* **Verified on 2026-09-30**, with the prices of `src/agentfare/data/pricing.yaml` (also reachable as
  `pricing.yaml` in the repository root) as they were checked on that date.
* **Three independent derivations agree.** (1) `scripts/verify_fixtures.py` reads the raw JSONL fixtures and the
  raw `pricing.yaml` with its own minimal log readers and exact `fractions.Fraction` arithmetic, and never imports
  `agentfare`. The tables between `<!-- BEGIN generated -->` markers in this file are its output, and
  `python scripts/verify_fixtures.py --check` (also a test) fails if this file drifts from it. (2) The Claude
  fixture builder (`tests/fixtures/claude/build_fixture.py`) keeps its own running totals per model and writes
  them into the log's `cost-state` record: for Opus they equal the counts below exactly (it is bookkeeping made while the
  data is written, so a cross-check of the tokens rather than a derivation of the dollars). (3) The CLI itself,
  through `tests/test_e2e_fixtures.py`. No figure differed between them; the one deliberate difference is
  explained in "Cross-check log".
* Regenerate the tables after a fixture, price or default change with
  `python scripts/verify_fixtures.py --markdown`, re-do the review below, and update the literals in the test.

```
python scripts/verify_fixtures.py            # the derivation as a plain report
python scripts/verify_fixtures.py --check    # is this file current?
AGENTFARE_HOME=$(mktemp -d) AGENTFARE_NOW=2026-09-30T12:00:00Z \
  agentfare --config tests/fixtures/config.yaml summary      # or sessions, models, advise, report
pytest tests/test_e2e_fixtures.py
scripts/verify_fresh_clone.sh                # fresh clone (of the last commit), fresh venv per interpreter, the demo, the suite
scripts/verify_fresh_clone.sh --worktree     # the same on a copy of the working tree, to rehearse before committing
```

Every command below runs with `--config tests/fixtures/config.yaml` (the fixture logs are the only sources,
`timezone: UTC`), a throwaway `AGENTFARE_HOME`, `AGENTFARE_NOW=2026-09-30T12:00:00Z` (a Wednesday, ISO week
2026-W40, Monday 2026-09-28 to Sunday 2026-10-04) and `COLUMNS=100`.

## 1. Prices used

USD per 1M tokens, as billed (a table entry that is left out is billed at the input price, and a missing 1-hour
cache write at the 5-minute cache-write price: that is why the Gemini and OpenAI rows repeat a number). All rows
are `verified` in `pricing.yaml`, source and date per row in that file (`checked: 2026-09-30`). The fixtures spend on
`claude-opus-5-5`, `claude-haiku-4-5`, `gpt-6-astra` and `gemini-2.5-pro`; `gpt-6.1-sol` is what `advise` compares with;
the other rows are the models `run` picks by default (with `claude-opus-5-5` and `gpt-6-astra` as the heavy tier).

<!-- BEGIN generated: prices -->
| model | input | cache read | cache write 5m | cache write 1h | output | checked |
|---|---:|---:|---:|---:|---:|---|
| `claude-haiku-4-5` | $1.00 | $0.10 | $1.25 | $2.00 | $5.00 | 2026-09-30 |
| `claude-opus-5-5` | $4.00 | $0.20 | $5.00 | $8.00 | $20.00 | 2026-09-30 |
| `claude-sonnet-5-5` | $2.00 | $0.20 | $2.50 | $4.00 | $10.00 | 2026-09-30 |
| `gemini-2.5-pro` | $1.25 | $0.125 | $1.25 | $1.25 | $10.00 | 2026-09-30 |
| `gemini-3-flash-preview` | $0.50 | $0.05 | $0.50 | $0.50 | $3.00 | 2026-09-30 |
| `gemini-3.1-flash-lite` | $0.25 | $0.025 | $0.25 | $0.25 | $1.50 | 2026-09-30 |
| `gemini-3.1-pro-preview` | $2.00 | $0.20 | $2.00 | $2.00 | $12.00 | 2026-09-30 |
| `gpt-6-astra` | $10.00 | $1.00 | $12.50 | $12.50 | $50.00 | 2026-09-30 |
| `gpt-6.1-sol` | $2.00 | $0.10 | $2.50 | $2.50 | $10.00 | 2026-09-30 |
<!-- END generated: prices -->

`tests/test_e2e_fixtures.py::TestPriceGuard` compares this table with the built-in `pricing.yaml` (and with
`agentfare prices --json`): **a price change fails it on purpose**, so that the fixtures' expected dollars are
re-verified instead of silently following the table. Update this file and the literals at the top of the test
after redoing the arithmetic.

## 2. What the log parsers must count

Each agent's log is reduced to four token buckets per model (the CLI shows the 5-minute and 1-hour cache writes
added together as "cache write"; the split matters for the price): **input** (uncached), **cache read**,
**cache write** and **output**, where output includes reasoning/thinking tokens. Each billed response also carries the
time of its own record (the last record of a Claude message id; the record that billed a Codex response; the message
of a Gemini turn), which puts its money on a UTC 15-minute slot: money is attributed to when it was spent, so the
per-slot usage of a session always adds up to its usage. The Codex and Gemini fixtures each sit in a single slot
(Codex 10:00, Gemini 09:00 UTC); the Claude one spreads over 8 slots on 2026-09-28.

### 2.1 Claude Code: `tests/fixtures/claude/projects/-home-dev-acme-shop/2c9f6d1e-…e402.jsonl` and two sub-agent files

One session on 2026-09-28 in `/home/dev/acme-shop` (project `acme-shop`), 09:00:00 to 11:27:20 UTC. Rules applied,
one per trap the fixture sets:

* **One API response is several records.** A response is written once per content block, all sharing
  `message.id`; the early records carry the message_start snapshot (`output_tokens` 2) and the last one the final
  counts. Counted once, from the last record: 43 distinct responses (36 on Opus, 7 on Haiku).
* **Sub-agent files belong to the session.** `subagents/agent-a1b2c3d4e5f60789.jsonl` and
  `subagents/workflows/wf_demo-001/agent-b2c3d4e5f6071890.jsonl` hold real spend (Haiku and Opus responses) and
  are merged in.
* **Cache writes have two prices.** `cache_creation_input_tokens` is the total; `cache_creation.ephemeral_1h_input_tokens`
  is the 1-hour part, priced at 2x input; the rest (also the whole write of a record with no `cache_creation`
  object, as in older logs) is 5-minute. That is why "cache write 5m" and "cache write 1h" are separate below.
* **Skipped:** two copies of another session's history (`sessionId` or `session_id` names `9d1b7e40-…`), whose
  every counter is 9,999,999; the `<synthetic>` message (zero tokens); a truncated JSON line; the records that are not
  conversation events (`permission-mode`, `attachment`, `queue-operation`, `future-widget-state`, `system`,
  `ai-title`, `custom-title`, `last-prompt`, `cost-state`).
* **The model is priced under its alias:** the log says `claude-haiku-4-5-20251001`, `pricing.yaml` prices it as
  `claude-haiku-4-5`.

<!-- BEGIN generated: responses-claude -->
| model | responses | input | cache read | cache write 5m | cache write 1h | output |
|---|---:|---:|---:|---:|---:|---:|
| `claude-opus-5-5` | 31 x each | 1,200 | 80,000 | 3,000 | 2,000 | 900 |
| `claude-opus-5-5` | 5 x each | 400 | 120,000 | 0 | 0 | 3,000 |
| `claude-opus-5-5` | **sum of 36** | **39,200** | **3,080,000** | **93,000** | **62,000** | **42,900** |
| `claude-haiku-4-5` | 4 x each | 800 | 15,000 | 0 | 3,000 | 400 |
| `claude-haiku-4-5` | 3 x each | 500 | 20,000 | 2,000 | 0 | 200 |
| `claude-haiku-4-5` | **sum of 7** | **4,700** | **120,000** | **6,000** | **12,000** | **2,200** |
<!-- END generated: responses-claude -->

<!-- BEGIN generated: cost-claude -->
`claude-opus-5-5` in the claude session `a1c7e402`:

| bucket | tokens | USD per 1M | tokens x price / 1,000,000 | dollars |
|---|---:|---:|---|---:|
| input (uncached) | 39,200 | $4.00 | 39,200 x 4.00 / 1,000,000 | $0.1568 |
| cache read | 3,080,000 | $0.20 | 3,080,000 x 0.20 / 1,000,000 | $0.616 |
| cache write, 5 minutes | 93,000 | $5.00 | 93,000 x 5.00 / 1,000,000 | $0.465 |
| cache write, 1 hour | 62,000 | $8.00 | 62,000 x 8.00 / 1,000,000 | $0.496 |
| output (incl. thinking) | 42,900 | $20.00 | 42,900 x 20.00 / 1,000,000 | $0.858 |
| **total** | 3,317,100 |  |  | **$2.5918** = 259,180,000 ucents |

`claude-haiku-4-5` in the claude session `a1c7e402`:

| bucket | tokens | USD per 1M | tokens x price / 1,000,000 | dollars |
|---|---:|---:|---|---:|
| input (uncached) | 4,700 | $1.00 | 4,700 x 1.00 / 1,000,000 | $0.0047 |
| cache read | 120,000 | $0.10 | 120,000 x 0.10 / 1,000,000 | $0.012 |
| cache write, 5 minutes | 6,000 | $1.25 | 6,000 x 1.25 / 1,000,000 | $0.0075 |
| cache write, 1 hour | 12,000 | $2.00 | 12,000 x 2.00 / 1,000,000 | $0.024 |
| output (incl. thinking) | 2,200 | $5.00 | 2,200 x 5.00 / 1,000,000 | $0.011 |
| **total** | 144,900 |  |  | **$0.0592** = 5,920,000 ucents |
<!-- END generated: cost-claude -->

Activity (from the same log): 5 prompts. The typed ones are the plain text at 09:00:00, the text block that follows an
injected `<ide_opened_file>` block at 09:13:30, the record written twice at 10:22:40 (one `uuid`: counted once), the image
prompt at 10:55:30 and `continue` at 11:25:00. These user records are not prompts: a skill expansion (`isMeta`), a
slash-command echo and its stdout, an injected task notification (`origin.kind` is not `human`), a compaction summary
and an interrupt marker. 46 tool calls (distinct `tool_use` ids, sub-agents included), 10 distinct
files written (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`), 5 tool errors (the two calls the user rejected or a
permission rule blocked are not errors, and a result written twice counts once), 43 responses. Wall time 8,840 s
(09:00:00 to 11:27:20) of which 2,910 s (09:34:10 to 10:22:40) are an idle gap longer than 30 minutes: **5,930 s
active** (98 min 50 s).

### 2.2 Codex CLI: `tests/fixtures/codex/sessions/2026/09/29/rollout-2026-09-29T10-00-00-01a0d8b0-….jsonl`

One thread on 2026-09-29, project `fake-repo` (the repository name of `git.repository_url`), model `gpt-6-astra`
(from `turn_context`), 10:00:00 to 10:10:00 UTC. `input_tokens` **includes** `cached_input_tokens` and
`output_tokens` **includes** reasoning, so uncached input = input - cached and nothing is added for reasoning.
Each response counts once:

* `resp_fake_0003` appears twice as a `token_usage_record` (10:05:31.000 and 10:05:31.500): one response.
* Every `token_count` event repeats the usage record just before it (same counts): no new response. One more
  repeats a running total that did not move (10:02:16, a rate-limit update) and one has `info: null` (10:08:07).
* A `future_record_kind` record carries 999,999 input and output tokens: unknown record types are ignored.
* One line (10:03:00) is truncated JSON and is skipped.

Cross-check inside the log: the last running total (`total_token_usage` at 10:10:00.500) is 100,000 input of
which 60,000 cached, and 6,000 output of which 1,600 reasoning: 40,000 + 60,000 = 100,000 and 6,000, as below.

<!-- BEGIN generated: responses-codex -->
| response | input_tokens (with cached) | cached_input_tokens | output_tokens (with reasoning) | uncached input = input - cached | cache read | output |
|---|---:|---:|---:|---:|---:|---:|
| `resp_fake_0001` | 10,000 | 0 | 500 | 10,000 - 0 = **10,000** | 0 | 500 |
| `resp_fake_0002` | 12,000 | 8,000 | 1,000 | 12,000 - 8,000 = **4,000** | 8,000 | 1,000 |
| `resp_fake_0003` | 15,000 | 12,000 | 500 | 15,000 - 12,000 = **3,000** | 12,000 | 500 |
| `resp_fake_0004` | 18,000 | 15,000 | 1,000 | 18,000 - 15,000 = **3,000** | 15,000 | 1,000 |
| `resp_fake_0005` | 20,000 | 12,000 | 1,000 | 20,000 - 12,000 = **8,000** | 12,000 | 1,000 |
| `resp_fake_0006` | 25,000 | 13,000 | 2,000 | 25,000 - 13,000 = **12,000** | 13,000 | 2,000 |
| **sum** |  |  |  | **40,000** | **60,000** | **6,000** |
<!-- END generated: responses-codex -->

<!-- BEGIN generated: cost-codex -->
`gpt-6-astra` in the codex session `7c8d9e0f`:

| bucket | tokens | USD per 1M | tokens x price / 1,000,000 | dollars |
|---|---:|---:|---|---:|
| input (uncached) | 40,000 | $10.00 | 40,000 x 10.00 / 1,000,000 | $0.40 |
| cache read | 60,000 | $1.00 | 60,000 x 1.00 / 1,000,000 | $0.06 |
| output (incl. thinking) | 6,000 | $50.00 | 6,000 x 50.00 / 1,000,000 | $0.30 |
| **total** | 106,000 |  |  | **$0.76** = 76,000,000 ucents |
<!-- END generated: cost-codex -->

Activity: 1 prompt (the `UserMessage` item), 5 tool calls (`call_fake_1` to `call_fake_5`), 2 files touched (one
`FileChange` item with an update and an add), 1 error (`call_fake_3`'s output starts with `Script failed`), 6
responses, 600 s wall and active (no gap over 30 minutes) = **10 min 0 s exactly**.

### 2.3 Gemini CLI: `tests/fixtures/gemini/tmp/demo-notes-app-1/chats/session-2026-09-30T09-00-9f3c2a71.jsonl`

One session on 2026-09-30, project `demo-notes-app` (from `.project_root`), model `gemini-2.5-pro`, 09:00:00 to
09:09:55 UTC. `input = promptTokenCount - cached + tool`, `cache_read = cached`, `output = candidates + thoughts`.
The CLI re-appends a message whenever its tokens or tool calls arrive, so **the last record of each id is the message**:

* `g1` and `g2` are written twice; `g2`'s first record has `tokens: null`.
* `g4` is written twice with different counts: the first says 50,000 input / 1,000 output / 0 thoughts, the last
  (the one that counts) 40,000 / 500 / 500.
* `g-cut` is a truncated line (skipped). `$set`, `$rewindTo` and `$telemetry` records, the `info` message and the
  `ctx-0` message inside `$set` are not billed messages.
* `g3` sits in the span that the `$rewindTo` record (to `u2`) rewinds: the CLI's own loader would drop `u2`, `g3` and
  `ur3` from the conversation. The response was still billed, so it counts (40,000 / 20,000 cached / 1,000 / 1,000),
  as does the prompt `u2` ("billing view" in the parser docstring). Reading the file as the CLI loads it would give 4 billed
  responses instead of 5 and a cheaper session; that is a deliberate modelling choice, not an oversight.

<!-- BEGIN generated: responses-gemini -->
| message id | input | cached | tool | output | thoughts | uncached input = input - cached + tool | cache read | output + thoughts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `g1` | 10,000 | 0 | 0 | 500 | 500 | 10,000 - 0 + 0 = **10,000** | 0 | 500 + 500 = **1,000** |
| `g2` | 30,000 | 20,000 | 2,000 | 1,000 | 500 | 30,000 - 20,000 + 2,000 = **12,000** | 20,000 | 1,000 + 500 = **1,500** |
| `g3` | 40,000 | 20,000 | 0 | 1,000 | 1,000 | 40,000 - 20,000 + 0 = **20,000** | 20,000 | 1,000 + 1,000 = **2,000** |
| `g4` | 40,000 | 20,000 | 0 | 500 | 500 | 40,000 - 20,000 + 0 = **20,000** | 20,000 | 500 + 500 = **1,000** |
| `g5` | 50,000 | 20,000 | 0 | 3,000 | 1,000 | 50,000 - 20,000 + 0 = **30,000** | 20,000 | 3,000 + 1,000 = **4,000** |
| **sum** |  |  |  |  |  | **92,000** | **80,000** | **9,500** |
<!-- END generated: responses-gemini -->

<!-- BEGIN generated: cost-gemini -->
`gemini-2.5-pro` in the gemini session `4a6e8f10`:

| bucket | tokens | USD per 1M | tokens x price / 1,000,000 | dollars |
|---|---:|---:|---|---:|
| input (uncached) | 92,000 | $1.25 | 92,000 x 1.25 / 1,000,000 | $0.115 |
| cache read | 80,000 | $0.125 | 80,000 x 0.125 / 1,000,000 | $0.01 |
| output (incl. thinking) | 9,500 | $10.00 | 9,500 x 10.00 / 1,000,000 | $0.095 |
| **total** | 181,500 |  |  | **$0.22** = 22,000,000 ucents |
<!-- END generated: cost-gemini -->

Activity: 3 prompts (`u1`, `u2`, `u3`; the four `functionResponse` messages are tool results), 7 tool calls, 2 files
touched (`replace` on `notes.py` and `write_file` of the storage module; the `write_file` on `config.py` has status
`error`), 1 error (that write), 5 billed responses, 595 s = **9 min 55 s** (just under the 10-minute `medium` line).

## 3. Money, all sessions

<!-- BEGIN generated: cost-summary -->
| session | model | input | cache read | cache write 5m | cache write 1h | output | dollars | ucents |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| claude `a1c7e402` | `claude-opus-5-5` | 39,200 | 3,080,000 | 93,000 | 62,000 | 42,900 | $2.5918 | 259,180,000 |
| claude `a1c7e402` | `claude-haiku-4-5` | 4,700 | 120,000 | 6,000 | 12,000 | 2,200 | $0.0592 | 5,920,000 |
| codex `7c8d9e0f` | `gpt-6-astra` | 40,000 | 60,000 | 0 | 0 | 6,000 | $0.76 | 76,000,000 |
| gemini `4a6e8f10` | `gemini-2.5-pro` | 92,000 | 80,000 | 0 | 0 | 9,500 | $0.22 | 22,000,000 |
<!-- END generated: cost-summary -->

Totals by model, project, day, ISO week and agent. Money belongs to when it was spent: a response is counted on the
day and week of its own record time (the UTC 15-minute slot it falls in, in the configured timezone), not of the
session's start. Each fixture session happens inside one UTC day, so on the UTC clock the day of a response is the
day the session started (`scripts/verify_fixtures.py` derives the day and week totals from the responses, and they
equal the start-day figures below), and all three fall in week 2026-W40. The total is 363,100,000 ucents = $3.631 = **363 cents** when rounded half up to the cent; the
rounded model rows (259 + 6 + 76 + 22) and project rows (265 + 76 + 22) add up to the same 363.

<!-- BEGIN generated: totals -->
| group | key | dollars | ucents | cents (half up) |
|---|---|---:|---:|---:|
| **total** | all sessions | $3.6310 | 363,100,000 | 363 |
| model | `claude-opus-5-5` | $2.5918 | 259,180,000 | 259 |
| model | `claude-haiku-4-5` | $0.0592 | 5,920,000 | 6 |
| model | `gpt-6-astra` | $0.7600 | 76,000,000 | 76 |
| model | `gemini-2.5-pro` | $0.2200 | 22,000,000 | 22 |
| project | `acme-shop` | $2.6510 | 265,100,000 | 265 |
| project | `fake-repo` | $0.7600 | 76,000,000 | 76 |
| project | `demo-notes-app` | $0.2200 | 22,000,000 | 22 |
| day | `2026-09-28` | $2.6510 | 265,100,000 | 265 |
| day | `2026-09-29` | $0.7600 | 76,000,000 | 76 |
| day | `2026-09-30` | $0.2200 | 22,000,000 | 22 |
| week | `2026-W40` | $3.6310 | 363,100,000 | 363 |
| agent | `claude` | $2.6510 | 265,100,000 | 265 |
| agent | `codex` | $0.7600 | 76,000,000 | 76 |
| agent | `gemini` | $0.2200 | 22,000,000 | 22 |
<!-- END generated: totals -->

"To the cent": the total is computed once in micro-cents and rounded once at the end. `agentfare summary` prints
`$3.63`, and a row below one dollar with a fraction of a cent keeps four decimals (`$0.0592`).

## 4. What the commands print

### 4.1 `agentfare summary`

```
AgentFare summary: all time
3 sessions, 3 projects, 4 models, total spend $3.63
claude claude-opus-5-5 1 39.2k 3.1M 155k 42.9k $2.59 71.4%
codex gpt-6-astra 1 40.0k 60.0k 0 6.0k $0.76 20.9%
gemini gemini-2.5-pro 1 92.0k 80.0k 0 9.5k $0.22 6.1%
claude claude-haiku-4-5 1 4.7k 120k 18.0k 2.2k $0.0592 1.6%
Total 3 176k 3.3M 173k 60.6k $3.63 100%
acme-shop 1 43.9k 3.2M 173k 45.1k $2.65 73.0%
fake-repo 1 40.0k 60.0k 0 6.0k $0.76 20.9%
demo-notes-app 1 92.0k 80.0k 0 9.5k $0.22 6.1%
2026-09-28 1 43.9k 3.2M 173k 45.1k $2.65 73.0%
2026-09-29 1 40.0k 60.0k 0 6.0k $0.76 20.9%
2026-09-30 1 92.0k 80.0k 0 9.5k $0.22 6.1%
```

Under the by-model table the CLI adds the footnote "a session that used several models counts once per model; the Total
row counts it once" (the Claude session used two). Token columns are abbreviated by the CLI (`176k` is 175,900). With `--json` money is `{"cents", "ucents"}` and shares
are basis points: Opus 7138, Codex 2093, Gemini 606, Haiku 163 (the sum is 10,000); acme-shop and 2026-09-28
7301. The token totals are input 175,900, cache read 3,340,000, cache write 173,000, output 60,600 (3,749,500).

### 4.2 `agentfare sessions`

<!-- BEGIN generated: sessions-table -->
| day | agent | id | project | model | active | prompts | tool calls | files | errors | tokens | cost |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-09-30 | gemini | `4a6e8f10` | demo-notes-app | `gemini-2.5-pro` | 595 s | 3 | 7 | 2 | 1 | 181,500 | $0.2200 |
| 2026-09-29 | codex | `7c8d9e0f` | fake-repo | `gpt-6-astra` | 600 s | 1 | 5 | 2 | 1 | 106,000 | $0.7600 |
| 2026-09-28 | claude | `a1c7e402` | acme-shop | `claude-opus-5-5` +1 | 5930 s | 5 | 46 | 10 | 5 | 3,462,000 | $2.6510 |
<!-- END generated: sessions-table -->

```
AgentFare sessions: all time
09-30 gemini 4a6e8f10 demo-notes-app gemini-2.5-pro 9m55s 3 7 2 1 182k $0.22
09-29 codex 7c8d9e0f fake-repo gpt-6-astra 10m00s 1 5 2 1 106k $0.76
09-28 claude a1c7e402 acme-shop claude-opus-5-5 +1 1h38m 5 46 10 5 3.5M $2.65
showing 3 of 3 sessions, total $3.63
```

### 4.3 `agentfare models`

Blended $/1M is spend divided by tokens times 1M (the `ucents per 1M tokens` column, rounded half up); the CLI prints
whole cents from $1 up (`$7.17`) and four decimals below.

<!-- BEGIN generated: models-table -->
| # | model | tokens | spend | share (basis points) | blended ucents per 1M tokens | list in $/1M | list out $/1M |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | `claude-opus-5-5` | 3,317,100 | $2.5918 | 7138 | 78,134,515 | $4.00 | $20.00 |
| 2 | `gpt-6-astra` | 106,000 | $0.7600 | 2093 | 716,981,132 | $10.00 | $50.00 |
| 3 | `gemini-2.5-pro` | 181,500 | $0.2200 | 606 | 121,212,121 | $1.25 | $10.00 |
| 4 | `claude-haiku-4-5` | 144,900 | $0.0592 | 163 | 40,855,763 | $1.00 | $5.00 |
<!-- END generated: models-table -->

```
4 models, 3 sessions, total spend $3.63
1 claude-opus-5-5 anthropic 1 3.3M $2.59 71.4% $0.7813 $4.00 $20.00
2 gpt-6-astra openai 1 106k $0.76 20.9% $7.17 $10.00 $50.00
3 gemini-2.5-pro google 1 182k $0.22 6.1% $1.21 $1.25 $10.00
4 claude-haiku-4-5 anthropic 1 145k $0.0592 1.6% $0.4086 $1.00 $5.00
```

### 4.4 `agentfare summary` with filters, on the frozen clock

| Command | Sessions | Total |
|---|---:|---:|
| `--week` (now 2026-09-30, week 2026-09-28 .. 2026-10-04) | 3 | $3.631 |
| `--week` with now = 2026-10-05T12:00:00Z (W41) or 2026-09-27T12:00:00Z (W39) | 0 | none |
| `--week-of 2026-W40` / `2026-W39` / `2026-W41` | 3 / 0 / 0 | $3.631 / none / none |
| `--since 2026-09-29` (codex + gemini) | 2 | $0.98 (98,000,000 ucents) |
| `--until 2026-09-28` (claude) | 1 | $2.651 |
| `--since 2026-09-29 --until 2026-09-29` | 1 | $0.76 |
| `--agent codex` / `--agent claude` | 1 / 1 | $0.76 / $2.651 |
| `--project ACME-SHOP` (case-insensitive) | 1 | $2.651 |

The timezone decides the buckets, and money goes where it was spent. With `timezone: Pacific/Honolulu` (UTC-10, local
midnight is 10:00 UTC) the Claude session (Monday 2026-09-28 09:00 to 11:27 UTC) straddles local midnight: what it
spent before 10:00 UTC happened on Sunday 2026-09-27 and the rest on Monday 2026-09-28. Its 43 responses fall in 8
slots; the responses before 10:00 UTC cost 162,040,000 ucents ($1.6204) and the later ones 103,060,000 ($1.0306), which
add up to the session's 265,100,000. Days become 2026-09-27 ($1.6204), 2026-09-28 ($1.0306) and 2026-09-29 ($0.98: the
Codex session is Tuesday 00:00 local and the Gemini one Tuesday 23:00 local), weeks 2026-W39 ($1.6204) and 2026-W40
($2.0106). `--week` on the frozen clock (Wednesday 02:00 local, the week starts Monday 10:00 UTC) finds all three
sessions, but the Claude one shows only what it spent inside the week, $1.0306, so the total is $2.0106;
`agentfare sessions --week` marks it as clipped (`*`, `"clipped": true` in JSON) and keeps its tool calls, files and
times whole-session. `advise` then has two weekly rows: W39 with 1 session, 0 switched, $1.6204 actual and router;
W40 with 3 sessions, 1 switched, $2.0106 actual, $1.3966 with the router, $0.614 saved (30.5%, 3054 basis points).
The Claude session is in both rows and once in the Total row (3 sessions, $3.631, $0.614 saved, 16.9%). The figures
were re-derived independently by `scripts/verify_fixtures.py` (exact prices, the local day of each response).
With `idle_gap_minutes: 60` the 2,910 s gap is no longer idle: the Claude active time becomes its wall time, 8,840 s.

### 4.5 Logs that grow

Agents append to their logs while you work, and the next command ingests the growth (a file whose size or time changed
is parsed again, in full, and replaces its session: nothing is added twice). On a copy of the Codex fixture, a seventh
response 10 minutes after the last event (`resp_fake_0007`: 20,000 input of which 10,000 cached, 1,000 output) adds
10,000 x $10 + 10,000 x $1 + 1,000 x $50 = 100,000 + 10,000 + 50,000 = $0.16: the total goes from $0.76 (76,000,000 ucents) to
**$0.92 (92,000,000 ucents)**, the tokens to 50,000 / 70,000 / 0 / 7,000, and the wall and active time from 600 s to 1,200 s (the
10-minute gap is below the 30-minute idle limit). Writing the same response again, and a line cut off in the middle,
change nothing.

## 5. The router (`agentfare advise`)

Rules (docs/ROUTING.md, defaults of `config.py`): five signals, each worth 0 below `medium`, 1 from `medium`, 2 from
`heavy`; score 0-2 light, 3-6 medium, 7-10 heavy; light and medium sessions are compared with `gpt-6.1-sol`, heavy ones
are kept (`recommend.heavy: null`); a switch is advised when the same tokens re-priced there cost at least 1.2x less
(`min_saving_ratio`). Active minutes are seconds / 60 compared exactly (600 s is 10.0 minutes, which reaches `medium`;
595 s does not).

<!-- BEGIN generated: router-signals -->
| session | `input_tokens` (medium 2,000,000, heavy 25,000,000) | `tool_calls` (medium 25, heavy 180) | `files_touched` (medium 1, heavy 6) | `error_retries` (medium 2, heavy 8) | `active_minutes` (medium 10, heavy 80) | score | tier |
|---|---:|---:|---:|---:|---:|---:|---|
| claude `a1c7e402` | 3,416,900 -> **1** | 46 -> **1** | 10 -> **2** | 5 -> **1** | 5930 s = 98 min 50 s -> **2** | **7** | heavy |
| codex `7c8d9e0f` | 100,000 -> **0** | 5 -> **0** | 2 -> **1** | 1 -> **0** | 600 s = 10 min 0 s -> **1** | **2** | light |
| gemini `4a6e8f10` | 172,000 -> **0** | 7 -> **0** | 2 -> **1** | 1 -> **0** | 595 s = 9 min 55 s -> **0** | **1** | light |
<!-- END generated: router-signals -->

The recommended cost is the session's whole usage (all models, each bucket) at `gpt-6.1-sol`'s rates: uncached input
$2, cache read $0.10, cache write $2.50, output $10 per 1M.

* Codex: 40,000 x 2 + 60,000 x 0.10 + 6,000 x 10 = 80,000 + 6,000 + 60,000 = 146,000, all / 1,000,000 = **$0.146**
  (14,600,000 ucents). $0.76 / $0.146 = 5.2055x, at least 1.2x, so the switch is **advised**: it would have saved
  $0.76 - $0.146 = **$0.614** (61,400,000 ucents). The check in integers is 76,000,000 x 5 >= 14,600,000 x 6.
* Gemini: 92,000 x 2 + 80,000 x 0.10 + 9,500 x 10 = 184,000 + 8,000 + 95,000 = 287,000, all / 1,000,000 = **$0.287**,
  more than the $0.22 it cost on `gemini-2.5-pro`: "already cheaper than gpt-6.1-sol", nothing advised.
* Claude: heavy, kept ("heavy: keep"); no re-pricing is shown.

<!-- BEGIN generated: router-advice -->
| session | actual | tier -> compared with | same tokens re-priced | actual / re-priced | advised | saved |
|---|---:|---|---:|---:|---|---:|
| claude `a1c7e402` | $2.6510 | heavy | n/a | n/a | no (heavy: keep) | $0 |
| codex `7c8d9e0f` | $0.7600 | light -> `gpt-6.1-sol` | $0.1460 | 76,000,000 / 14,600,000 = 5.2055 | **yes** | $0.6140 |
| gemini `4a6e8f10` | $0.2200 | light -> `gpt-6.1-sol` | $0.2870 | 22,000,000 / 28,700,000 = 0.7666 | no (already cheaper than gpt-6.1-sol) | $0 |
<!-- END generated: router-advice -->

Weekly router savings (`advise`, and the headline of `report`): sessions that are fully priced, sessions switched, what
they cost, what they would have cost with every advised switch applied, and the difference.

<!-- BEGIN generated: router-week -->
| week | sessions | switched | actual | router | saved | saved % |
|---|---:|---:|---:|---:|---:|---:|
| 2026-W40 | 3 | 1 | $3.6310 | $3.0170 | $0.6140 | 16.9% |
<!-- END generated: router-week -->

Displayed, 16.9% is 61,400,000 / 363,100,000 = 16.91% (1691 basis points in `--json`). The JSON `ratio` of the Codex
session is 5.21 (5.2055 to two decimals); the text says 5.2x (one decimal).

```
3 sessions, 1 with a switch advised
1. session 7c8d9e0f: you spent $0.76 on gpt-6-astra → gpt-6.1-sol would likely have handled this for ~$0.1460 (5.2x cheaper)
codex, fake-repo, 2026-09-29: score 2/10 (light): files_touched 2 +1, active_minutes 10 +1
2 more sessions where no switch is advised (heavy, already cheaper, no verified price): $2.87 spent; --all lists them.
2026-W40 3 1 $3.6310 $3.0170 $0.6140 16.9%
Total 3 1 $3.6310 $3.0170 $0.6140 16.9%
Of the saving, $0.6140 (100%) comes from sessions with prompts and $0.00 (0.0%) from sub-agent or background threads with 0 prompts.
```

A weekly row shows all three amounts with the same precision: four decimals as soon as one of them is not a whole cent
($3.6310, $3.0170, $0.6140), so the columns cross-foot on screen. The last line splits the saving between sessions a
person drove (at least one prompt: all three fixtures) and background threads (none here).

With `--all` the other two sessions are listed as well:

```
2. session a1c7e402: you spent $2.65 on claude-opus-5-5 (+1 more model): no switch advised, heavy: keep
claude, acme-shop, 2026-09-28: score 7/10 (heavy): input_tokens 3.4M +1, tool_calls 46 +1, files_touched 10 +2, error_retries 5 +1, active_minutes 98 +2
3. session 4a6e8f10: you spent $0.22 on gemini-2.5-pro: no switch advised, already cheaper than gpt-6.1-sol
gemini, demo-notes-app, 2026-09-30: score 1/10 (light): files_touched 2 +1
```

Not listed by default: 2 sessions where no switch is advised ($2.651 + $0.22 = $2.871 = "$2.87 spent"); the
listing rule is `prompts >= 1` and a cost of at least 10 cents. A `router.min_saving_ratio` of 6 turns the Codex advice
into "gpt-6.1-sol is only 5.2x cheaper (minimum 6x)" (5.2055 is rounded down to one decimal there).

## 6. The weekly report (`agentfare report`)

Same numbers as `summary` and `advise` for the ISO week (whole cents from a cent up, project names hidden
as Project A, B, C unless `--show-projects`; `fake-repo` is Project B).

```
# AgentFare weekly report: 2026-W40
Sep 28 to Oct 4, 2026 (in progress). 3 sessions, 3 agents, 4 models, 3 projects.
**I spent $3.63 on AI coding agents this week. In sessions you prompted yourself, a cost router would have saved 16.9% ($0.61) of the $3.63 they cost; across all sessions, including sub-agent and background threads: 16.9% ($0.61).**
| Actual | $3.63 |
| With the router | $3.02 |
| Saved | $0.61 (16.9%) |
| claude-opus-5-5 | 1 | $2.59 | 71.4% |
| gpt-6-astra | 1 | $0.76 | 20.9% |
| gemini-2.5-pro | 1 | $0.22 | 6.1% |
| claude-haiku-4-5 | 1 | $0.06 | 1.6% |
| claude | 1 | $2.65 | 73.0% |
| Mon Sep 28 | 1 | $2.65 |
| Tue Sep 29 | 1 | $0.76 |
| Wed Sep 30 | 1 | $0.22 |
| Light | 2 | $0.98 |
| Heavy | 1 | $2.65 |
| `7c8d9e0f` | codex | Project B | gpt-6-astra | gpt-6.1-sol | $0.76 | $0.15 | $0.61 (5.2x cheaper) |
```

The claim in the headline (`$3.63`, `16.9%`, `$0.61`) is the same as the `advise` week row rounded to cents. The
per-model rows are rounded to whole cents on their own: the Haiku row shows $0.06 (5.92 cents), and $2.59 + $0.76 + $0.22 +
$0.06 = $3.63. `report --json` equals `summary --json` on sessions, tokens, total, by model and by day, and its
`router` object equals the `advise` total. The HTML page is one file with no script, no external reference and the
same figures.

## 7. `agentfare run --dry-run`

Fixture config, the fixtures ingested (`run` itself never ingests and never creates the database: it only reads one that
exists), no past sessions of the agent and tier (the median of past sessions needs five, the fixtures hold one per agent),
a working directory of two files (the repo-size rule stays silent). The estimate is the default token budget of the tier
(`router.run.default_tokens`) priced on the picked model. The baseline is what you would have run without the router:
the model with the most spend for that agent in the last 30 days of history (Opus for Claude, `gpt-6-astra` for Codex,
`gemini-2.5-pro` for Gemini), priced on the same budget. The 30 days are counted back from the newest session in the
database, the Gemini one of 2026-09-30, so the window starts on 2026-08-31 for all three agents. With no priced spend in
the window it is the most used model overall, and with no priced history the agent's heavy-tier model. A tier whose pick
costs more than the baseline says "none: ... MORE than the baseline" (Gemini heavy below), and a tier whose pick is the
baseline itself (Claude and Codex heavy) has no `saving` row at all: there is nothing to save (the `none` in the table above
is the arithmetic, $0.0000).

<!-- BEGIN generated: run-estimates -->
| agent | tier | model | estimate | baseline (the agent's most used model) | saving |
|---|---|---|---:|---|---:|
| claude | light | `claude-sonnet-5-5` | $0.3125 | $0.5050 (`claude-opus-5-5`) | $0.1925 (38.1%) |
| claude | medium | `claude-sonnet-5-5` | $3.0050 | $4.2500 (`claude-opus-5-5`) | $1.2450 (29.3%) |
| claude | heavy | `claude-opus-5-5` | $43.7800 | $43.7800 (`claude-opus-5-5`) | $0.0000 (none) |
| codex | light | `gpt-6.1-sol` | $0.2525 | $1.5625 (`gpt-6-astra`) | $1.3100 (83.8%) |
| codex | medium | `gpt-6.1-sol` | $2.1250 | $15.0250 (`gpt-6-astra`) | $12.9000 (85.9%) |
| codex | heavy | `gpt-6-astra` | $168.4500 | $168.4500 (`gpt-6-astra`) | $0.0000 (none) |
| gemini | light | `gemini-3.1-flash-lite` | $0.03925 | $0.21375 (`gemini-2.5-pro`) | $0.1745 (81.6%) |
| gemini | medium | `gemini-3-flash-preview` | $0.7675 | $2.08125 (`gemini-2.5-pro`) | $1.31375 (63.1%) |
| gemini | heavy | `gemini-3.1-pro-preview` | $33.9000 | $22.3875 (`gemini-2.5-pro`) | -$11.5125 (costs more) |
<!-- END generated: run-estimates -->

The tier comes from the prompt: `fix the typo in the README` (26 characters) scores the light keywords `typo` and `readme`
(-3) and the medium keyword `fix` (+1), -2, never below 0 = 0, light; `refactor the billing module` (27) one heavy keyword = 2,
medium (the tiers are light at 0, medium at 1 and 2, heavy from 3: one reason is enough for medium); `refactor the billing
module and migrate it to the new API` (57) two distinct heavy keywords, 2 + 1 = 3, and the medium keyword `api`, +1 = 4,
heavy. Rows as printed (panel border removed, whitespace squeezed), for the cases the tests run:

```
claude, "fix the typo in the README":
  agent claude
  prompt 26 chars, from the command line
  tier light (score 0)
  - light keywords typo, readme: -3
  - medium keyword fix: +1
  model claude-sonnet-5-5 (router pick)
  estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)
  baseline ~$0.5050 on claude-opus-5-5 (your most used claude model in the last 30 days, since 2026-08-31)
  saving ~$0.1925 (38.1% less than the baseline)
  command claude --model claude-sonnet-5-5 -p '<prompt: 26 chars>'

claude, "refactor the billing module":
  prompt 27 chars, from the command line
  tier medium (score 2)
  - heavy keyword refactor: +2
  model claude-sonnet-5-5 (router pick)
  estimate ~$3.01 on claude-sonnet-5-5, typical medium task (no history yet)
  baseline ~$4.25 on claude-opus-5-5 (your most used claude model in the last 30 days, since 2026-08-31)
  saving ~$1.25 (29.3% less than the baseline)
  command claude --model claude-sonnet-5-5 -p '<prompt: 27 chars>'

claude, "refactor the billing module and migrate it to the new API":
  prompt 57 chars, from the command line
  tier heavy (score 4)
  - heavy keywords refactor, migrat: +3
  - medium keyword api: +1
  model claude-opus-5-5 (router pick)
  estimate ~$43.78 on claude-opus-5-5, typical heavy task (no history yet)
  baseline ~$43.78 on claude-opus-5-5 (your most used claude model in the last 30 days, since 2026-08-31)
  command claude --model claude-opus-5-5 -p '<prompt: 57 chars>'

codex, "fix the typo in the README":
  tier light (score 0)
  model gpt-6.1-sol (router pick)
  estimate ~$0.2525 on gpt-6.1-sol, typical light task (no history yet)
  baseline ~$1.56 on gpt-6-astra (your most used codex model in the last 30 days, since 2026-08-31)
  saving ~$1.31 (83.8% less than the baseline)

codex, "refactor the billing module and migrate it to the new API":
  tier heavy (score 4)
  model gpt-6-astra (router pick)
  estimate ~$168.45 on gpt-6-astra, typical heavy task (no history yet)

gemini, "fix the typo in the README":
  tier light (score 0)
  model gemini-3.1-flash-lite (router pick)
  estimate ~$0.0393 on gemini-3.1-flash-lite, typical light task (no history yet)
  baseline ~$0.2138 on gemini-2.5-pro (your most used gemini model in the last 30 days, since 2026-08-31)
  saving ~$0.1745 (81.6% less than the baseline)

gemini, "refactor the billing module":
  tier medium (score 2)
  model gemini-3-flash-preview (router pick)
  estimate ~$0.7675 on gemini-3-flash-preview, typical medium task (no history yet)
```

**History.** From five past sessions of the same agent and tier (with at least one prompt) on, the estimate is the median
of their token buckets instead of the default budget, and the panel says so. Five renamed copies of the Codex fixture
(identical light sessions of 40,000 / 60,000 / 0 / 6,000 tokens on `gpt-6-astra`) give, for `fix the typo in the README`:
`estimate ~$0.1460 on gpt-6.1-sol, median of 5 past codex/light sessions` (40,000 x 2 + 60,000 x 0.10 + 6,000 x 10 =
$0.146), `baseline ~$0.76 on gpt-6-astra (your most used codex model in the last 30 days, since 2026-08-30)` (the newest session is the original fixture, 2026-09-29) and `saving ~$0.6140 (80.8% less than the baseline)`; with four copies the
default budget is still used (`~$0.2525 ... typical light task (no history yet)`). `advise` on the five copies: 5 x $0.76 =
$3.80 actual, 5 x $0.146 = $0.73 with the router, $3.07 saved (8079 basis points).

Money is shown to whole cents from $1 up (`$3.005` is `$3.01`, `$1.5625` is `$1.56`) and to four decimals below
(`$0.03925` is `$0.0393`, `$0.27475` is `$0.2748`), both half up. `--force-model gpt-6.1-sol` on the light Claude
prompt prices the same budget there: 60,000 + 60,000 + 62,500 + 70,000 = $0.2525, 50.0% below the $0.5050
baseline. `--dry-run --json` prints the same decision as data. Without `--dry-run` the decision is printed (on stderr) before anything starts, and with no terminal and no
`--yes` nothing runs (exit code 3).

## 8. Privacy

Every text field of the fixtures carries a marker: `PRIVACY-CANARY-CLAUDE`, `PRIVACY-CANARY-CODEX` and
`PRIVACY-CANARY-GEMINI` (several hundred occurrences in all: prompts, replies, tool inputs and outputs, file names such
as `PRIVACY-CANARY-CLAUDE-analysis.ipynb`). None of them, and none of the working-directory or file paths or the
repository URL that the logs mention (`/home/dev/acme-shop/src/cart.py`, `/home/tester/src/fake-repo-checkout`,
`https://github.com/example/fake-repo.git`, ...), may appear in the raw bytes of the SQLite file or in any output.
What is kept: the project name (a basename: `acme-shop`, `fake-repo`, `demo-notes-app`), session ids, model names, token
counts, timestamps, counters, and the path of the log file each session was read from (an audit trail).

## 9. Cross-check log (2026-09-30)

* Every number in sections 2 to 7 was compared with the CLI's JSON (`summary`, `sessions`, `models`, `advise --all`,
  `report`, `prices`) for the fixtures, and every text line above with the text output: **no mismatch**.
* Slots (added later the same day): the script now reads the time of each billed response and derives the day and
  week totals from it; they equal the start-day figures, so nothing moved (total 363,100,000 ucents, saving
  61,400,000). The Honolulu split of section 4.4 and the run baselines of section 7 were derived the same way and
  agree with the CLI exactly.
* Tokens, ucents, cents, shares, blended prices, signals (values, points, score, tier), advice, weekly rows, `run`
  estimates, and the day, week and timezone buckets agree exactly.
* The Claude log's own `cost-state` record lists, per model, `inputTokens`, `outputTokens`, `cacheReadInputTokens`
  and `cacheCreationInputTokens`. Opus agrees exactly (39,200 / 42,900 / 3,080,000 / 155,000). Haiku lists 6,550 input
  and 2,231 output tokens against 4,700 and 2,200 here: the fixture builder adds one title-generation call (1,850
  input, 31 output) that is billed and counted in `cost-state` but is not in any transcript. AgentFare reads
  transcripts, so it reads low by exactly that call, as its Claude parser documents. It is not a mismatch.
* On these fixtures the Claude parser's rule (per-bucket maximum over the records of one message id) and the plain
  "last record of the message id" rule of `scripts/verify_fixtures.py` give the same tokens, because the final record
  of every streamed response carries the largest counts.

## 10. Machine-readable expectations

Parsed by `scripts/verify_fresh_clone.sh` (the demo total and the savings must match these) and checked against the
literals of `tests/test_e2e_fixtures.py`:

<!-- BEGIN machine-readable -->
summary.sessions = 3
summary.total_ucents = 363100000
summary.total_cents = 363
summary.total_usd = $3.63
advise.switched = 1
advise.saved_ucents = 61400000
advise.saved_usd = $0.6140
advise.switch_line = session 7c8d9e0f: you spent $0.76 on gpt-6-astra → gpt-6.1-sol would likely have handled this for ~$0.1460 (5.2x cheaper)
<!-- END machine-readable -->

# Price table sources

`src/agentfare/data/pricing.yaml` holds USD prices per 1M tokens. Every value was read on **2026-09-30**
from the provider's own pricing or model page; a price that could not be read there is the literal string
`unverified`. No third-party aggregator was used. This file records what was read where, and what is
ambiguous, so the next refresh can repeat the work. A second full pass on the same day (see "Second pass")
re-read every page and changed no price that was already in the table, and an independent review pass (see
"Review pass") changed no price either.

## The official pages

| Provider | Pages read | Used for |
|---|---|---|
| Anthropic | <https://platform.claude.com/docs/en/about-claude/pricing> (same page as docs.claude.com/en/docs/about-claude/pricing, which redirects) | all Claude prices, cache multipliers, fast mode, long context |
| | <https://claude.com/pricing> | cross-check: the same numbers for every Claude model tabled (it quotes the 5-minute cache-write rate only) |
| | <https://platform.claude.com/docs/en/models/overview> (redirect target of the older `.../about-claude/models/overview`), <https://platform.claude.com/docs/en/about-claude/model-deprecations> | exact API ids and their aliases (Bedrock and Google Cloud spellings), retirement dates |
| | <https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost> (redirect target of the older `.../adaptive-thinking`) | thinking tokens are billed as output tokens |
| OpenAI | <https://developers.openai.com/api/docs/pricing> and its Markdown view <https://developers.openai.com/api/docs/pricing.md> | Standard, Batch, Flex and Fast tables with short- and long-context columns for `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-sol`, `gpt-5.6-sol`, `gpt-5.6-terra` and `gpt-5.6-luna`; the "Cyber models" table, which repeats `gpt-5.6-sol`; tier multipliers |
| | `https://developers.openai.com/api/docs/models/<id>` for `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-luna`, `gpt-6-sol`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` | per-model price block (input, cached input, cache writes for the GPT-6 models, output) and long-context rule; agrees with the pricing page on every row |
| | <https://developers.openai.com/api/docs/models/all> | which model ids OpenAI lists; `codex-auto-review` is not one of them |
| | <https://developers.openai.com/api/docs/guides/reasoning> | reasoning tokens are billed as output tokens |
| | <https://developers.openai.com/codex/pricing> (answers HTTP 308 to <https://learn.chatgpt.com/docs/pricing>) | Codex credit rates, used only as a cross-check of the GPT-5.6 prices, and the statement that Codex credit billing has no cache-write charge |
| | <https://learn.chatgpt.com/docs/sandboxing/auto-review> (`https://developers.openai.com/codex/auto-review` answers HTTP 308 to it), <https://developers.openai.com/codex/config-reference>, <https://developers.openai.com/codex/models> | what the `codex-auto-review` alias is |
| Google | <https://ai.google.dev/gemini-api/docs/pricing> | all Gemini prices (Standard tier, paid) |
| | <https://ai.google.dev/gemini-api/docs/models>, <https://ai.google.dev/gemini-api/docs/deprecations> | which ids are current, which were shut down, announced shutdown dates |
| | <https://ai.google.dev/gemini-api/docs/thinking> | thinking tokens are part of the output price |
| xAI | <https://docs.x.ai/developers/models/grok-4.7> (the page and its Markdown view `.md`) | Grok 4.7 prices and the 200k tier |
| | <https://docs.x.ai/developers/pricing> | cross-check, priority processing, batch rules |
| | <https://docs.x.ai/developers/model-capabilities/text/reasoning> | reasoning tokens are billed as part of consumption |

How the pages were read: raw HTML fetched with `curl`, converted to text and searched for the model rows;
the Markdown views of the xAI pages were read directly. The OpenAI URL that the product owner seeded,
`https://openai.com/api/pricing/`, answers scripted requests with HTTP 403 and in a browser redirects to
`https://openai.com/business/pricing/#api`, which only links to the developer pricing page. The price
tables live at `developers.openai.com`, so that is the `source` recorded for every OpenAI model (including
the three seeded ones; their **price values are unchanged**).

On the developer pricing page the Standard, Batch, Flex and Fast tables are JavaScript components that show
only their first three rows (`gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna`; the component's
`collapsedLatestRowCount` is 3). A plain-text reading of the HTML, which is what `prices --refresh` does, sees
those three rows and the complete "Cyber models" table; the other rows (`gpt-6-sol`, the three GPT-5.6 models,
GPT-5.5 and older) are in the page's embedded data and in its Markdown view (append `.md` to the URL, served as
`text/markdown`). That is why the three GPT-5.6 rows cite `pricing.md`: it is the one page that shows every price
they hold, cache writes included, so `--refresh` confirms them. Their model pages show no cache-write row (only
the 1.25x rule), so `--refresh` would report those rows as changed there.

## What each column means

* `input`: uncached input, base tier. `output`: output including reasoning/thinking tokens (all four
  providers bill them at the output rate or as part of it; see the per-provider notes).
* `cached_input`: the cache-read (hit) rate.
* `cache_write`: the provider's cache-write rate when it publishes one. Anthropic's is the **5-minute**
  rate. OpenAI publishes one (1.25x input). Google and xAI have none (Google bills cache *storage* per
  hour, which is not modelled), so the field is left out and the input rate applies.
* `cache_write_1h`: the **1-hour** cache-write rate. Only Anthropic publishes one (2x input), so only the
  Anthropic rows have it. Where it is missing, 1-hour writes are billed at `cache_write`, then at `input`.
* The table stores the **base tier**: standard processing, prompts under the long-context threshold, no
  data-residency uplift. Tier rules are recorded in each model's `note` and repeated below.
* A rule that only has a date (an introductory price, a promotion, a shutdown) has no column: the date is in
  the row's `note`, and `tests/test_pricing_table.py` fails if a refresh drops it. Rows with a date:
  `gpt-5.6-sol` (2026-11-21), the three newest Gemini Flash models (2026-12-31), `gemini-3.1-flash-lite`
  (2027-05-07) and `claude-haiku-4-5` (2026-10-15).

## Anthropic

Source for every row: <https://platform.claude.com/docs/en/about-claude/pricing>, table "Model pricing".
Columns read: base input, base output, 5m writes, 1h writes, hits and refreshes.

| Model id | Input | Output | 5m write (`cache_write`) | 1h write (`cache_write_1h`) | Cache read | Status |
|---|---|---|---|---|---|---|
| `claude-fable-5-1` | 10 | 50 | 12.50 | 20 | 0.25 | verified |
| `claude-fable-5` | 10 | 50 | 12.50 | 20 | 1.00 | verified |
| `claude-opus-5-5` | 4 | 20 | 5 | 8 | 0.20 | verified |
| `claude-opus-5` | 5 | 25 | 6.25 | 10 | 0.50 | verified |
| `claude-opus-4-8` | 5 | 25 | 6.25 | 10 | 0.50 | verified |
| `claude-sonnet-5-5` | 2 | 10 | 2.50 | 4 | 0.20 | verified |
| `claude-sonnet-5` | 2 | 10 | 2.50 | 4 | 0.20 | verified |
| `claude-haiku-4-5` | 1 | 5 | 1.25 | 2 | 0.10 | verified |

Ids were matched against the model overview and deprecation pages (`claude-haiku-4-5-20251001` is the
dated API id of `claude-haiku-4-5`). The Bedrock/Google Cloud spellings listed on the overview page are
aliases: `anthropic.claude-<model>` and `claude-haiku-4-5@20251001`.

Ambiguities and how they are handled:

* **Cache reads are not always 0.1x input.** The page states 0.025x on Fable 5.1 (and Mythos 5.1) and
  0.05x on Opus 5.5; every other model tabled uses 0.1x. The table stores the absolute per-model numbers.
* **1-hour cache writes cost 2x input and have their own column.** The page's multiplier table reads
  "5-minute cache write 1.25x, 1-hour cache write 2x, cache read 0.1x", and the model table lists both write
  columns. Claude Code logs split cache creation into `ephemeral_5m` and `ephemeral_1h` tokens, and `Usage`
  keeps them in `cache_write` and `cache_write_1h`, so each is priced at its own rate.
* **Long context.** "Claude 4.6 and later models include the full 1M token context window at standard
  pricing." No surcharge, so `claude-opus-5[1m]` in a log resolves to `claude-opus-5` at the same price.
  Haiku 4.5 has a 200K window (overview page).
* **Fast mode** (`speed: fast`) costs 2x on Opus 5.5 ($8 / $40) and Opus 5 / 4.8 ($10 / $50). Cache
  multipliers stack on top. Not modelled; the logs on the development machine contain no fast-mode requests.
* **US-only inference** (`inference_geo: us`) is 1.1x on every price category, for Claude 4.6 and later
  models (Haiku 4.5 does not support the parameter). **Batch** is 50%. Neither is modelled.
* **Thinking tokens** are billed as output tokens (adaptive-thinking page), so `output` includes them.
* **Tokenizer.** Claude 4.7 and later use a tokenizer that produces about 30% more tokens for the same
  text. This changes token counts, not the per-token price.
* **Lifecycle.** All eight models are "Active" on the deprecations page, with a tentative retirement "not
  sooner than" 2026-10-15 for `claude-haiku-4-5-20251001`, 2027-05-28 for Opus 4.8, 2027-06-09 for Fable 5,
  2027-06-30 for Sonnet 5, 2027-07-24 for Opus 5, then September 2027 for Fable 5.1, Sonnet 5.5 and
  Opus 5.5. Only Haiku's is close; its note says to re-check then.
* Claude Mythos 5 and 5.1 are on the pricing page (same numbers as Fable 5 and 5.1) but are not tabled: they
  appear in none of the logs on the development machine.
* `<synthetic>` is the model name Claude Code writes for locally generated messages; they carry zero
  tokens, so it needs no price.

## OpenAI

| Model id | Input | Cached | Cache write | Output | Where | Status |
|---|---|---|---|---|---|---|
| `gpt-6.1-sol` | 2.00 | 0.10 | 2.50 | 10.00 | pricing page, Standard, short context; model page agrees | verified, matches seed |
| `gpt-6-astra` | 10.00 | 1.00 | 12.50 | 50.00 | pricing page, Standard, short context; model page agrees | verified, matches seed |
| `gpt-6-luna` | 0.10 | 0.01 | 0.125 | 0.50 | pricing page, Standard, short context; model page agrees | verified |
| `gpt-6-sol` | 2.00 | 0.20 | 2.50 | 10.00 | pricing page, Standard, short context (Markdown view); model page agrees and is the cited `source` | verified, matches seed |
| `gpt-5.6-sol` | 4.00 | 0.40 | 5.00 | 20.00 | pricing page, Standard, short context (Markdown view) and "Cyber models" table; model page agrees | verified, promotional until 2026-11-21 at least |
| `gpt-5.6-terra` | 2.00 | 0.20 | 2.50 | 12.00 | pricing page, Standard, short context (Markdown view); model page agrees | verified |
| `gpt-5.6-luna` | 0.20 | 0.02 | 0.25 | 1.20 | pricing page, Standard, short context (Markdown view); model page agrees | verified |
| `codex-auto-review` | unverified | unverified | | unverified | | unverified |

Notes:

* **No seeded price is contradicted.** All three match the official pages. Two details differ from the
  seed: the seeded source URL (see above), and `cache_write`, which OpenAI publishes and which the seed
  left out (2.50 and 12.50). Codex logs carry a `cache_write_input_tokens` counter, but it is 0 in every
  event on the development machine, and the Codex pricing page says "Codex credit billing has no separate
  cache-write charge. API-key usage follows API pricing", so `cache_write` only matters for API-key usage.
* **`gpt-6-sol` is in the Standard table** (2.00 / 0.20 / 2.50 / 10.00, next to its successor `gpt-6.1-sol`),
  in the collapsed part of it that only the Markdown view shows. Its model page carries the same price block
  and says "See GPT-6.1 Sol for the newer Sol model"; that page is the row's `source` and `--refresh` confirms
  the row there.
* **`gpt-5.6-*` are not `gpt-6-*`.** They are separate models with separate prices: `gpt-6-luna` is 0.10 / 0.01 /
  0.125 / 0.50 while `gpt-5.6-luna` is 0.20 / 0.02 / 0.25 / 1.20, and `gpt-5.6-sol` costs twice `gpt-6-sol` and
  `gpt-6.1-sol` on input and output. All of them are rows of the same Standard table.
* **`gpt-5.6-sol` has a promotional price.** Both the model page and the pricing page say "GPT-5.6 Sol's
  promotional pricing is available at least through November 21, 2026"; the model page calls it a 20% cut on
  input and 33% on output, and its comparison table shows GPT-5.5 at $5 input and $30 output. Neither page says
  what the price is afterwards, so the table keeps the current price and its note carries the date. **Re-check
  the row on 2026-11-21.** The pricing page lists it in the Standard table and again in its "Cyber models"
  table, next to `gpt-5.6-cyber` (not tabled: it is listed as a Daybreak model and no log on the development
  machine uses it); both rows give 4.00 / 0.40 / 5.00 / 20.00 and long-context rates of 8.00 / 0.80 / 10.00 /
  30.00.
* **Cache writes of the GPT-5.6 models are published in the pricing table**, not only as a rule: 5.00 for Sol,
  2.50 for Terra and 0.25 for Luna, each 1.25x its input price. Their model pages show no cache-write row but
  state "Cache writes are billed at 1.25x the uncached input token rate", which gives the same numbers.
* **Cross-check with the Codex credit table.** <https://developers.openai.com/codex/pricing> lists GPT-5.6
  Sol at 100 / 10 / 500 credits per 1M input / cached / output tokens, Terra at 50 / 5 / 300 and Luna at
  5 / 0.5 / 30. Those are exactly 25 times the USD prices above, as are the rows for GPT-6 Astra, 6.1 Sol,
  6 Sol and 6 Luna. This is a consistency check only; credits are not a price source.
* **The bare `gpt-5.6` id** is documented as an alias that "routes requests to GPT-5.6 Sol". It is not in
  the alias list: Codex logs on the development machine carry `gpt-5.6-sol`, never the bare id, and an
  alias that OpenAI can re-point would misprice silently. If your logs contain it, add
  `gpt-5.6: gpt-5.6-sol` under `aliases:` in your own price file (see "Overriding or adding prices").
* **Long context:** prompts above 272K input tokens cost 2x input and cache rates and 1.5x output for the
  whole request (the pricing table's long-context columns show it for every row above; the GPT-5.6 model
  pages state only "2x input and 1.5x output" but their pricing-table cells double the cache rates too). **Batch and Flex** are 50%, **Fast** (formerly Priority) is 2x, **Ultrafast** (astra
  only) is 6x, regional processing adds 10%. None is modelled.
* **Reasoning tokens** are billed as output tokens (reasoning guide), so `output` includes them.
* **`codex-auto-review`** is the model name Codex logs for its Auto-review feature, a separate reviewer
  agent used when `approvals_reviewer = "auto_review"`. It stays `unverified`. On 2026-09-30 OpenAI still
  publishes no model page (`https://developers.openai.com/api/docs/models/codex-auto-review` returns 404;
  the id is absent from the model catalogue at `.../models/all`) and no price for it, and none of the
  Auto-review page, the Codex config reference (`approvals_reviewer`, `auto_review.policy`; `review_model`
  belongs to `/review`), the Codex models page or the Codex token-rate table names the model that runs it.
  The only trace is the model catalog that the Codex CLI itself downloads (`~/.codex/models_cache.json`,
  client 0.159.0), which lists the id as hidden, described as "Automatic approval review model for Codex",
  with no price and no base model. That is not a pricing page, so it is not the row's source and no number
  is derived from it. All three price fields are `unverified`, so the CLI excludes its spend and warns. On the
  development machine the alias carries 362 of 2,181 Codex sessions and about 2% of Codex tokens
  (360M of 17.1B), most of them cache reads. To count it anyway, price it yourself (below).

## Google (Gemini)

Source for every row: <https://ai.google.dev/gemini-api/docs/pricing>, "Standard" tables, paid tier, per 1M
tokens. The ids are the ones in the installed Gemini CLI 0.60.0 bundle (`packages/core/src/config/models.ts`:
`gemini-3.5-flash`, `gemini-3-flash`, `gemini-3.1-pro-preview`, `gemini-3.1-pro-preview-customtools`,
`gemini-3-pro-preview`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite`, `gemini-2.5-pro`,
`gemini-2.5-flash`) plus the newer Flash models the pricing page lists as current.

| Model id | Input | Cached | Output | Status |
|---|---|---|---|---|
| `gemini-3.8-flash` | 0.75 | 0.075 | 3.75 | verified (introductory, see below) |
| `gemini-3.7-flash` | 0.75 | 0.075 | 3.75 | verified (introductory) |
| `gemini-3.6-flash` | 0.75 | 0.075 | 3.75 | verified (introductory) |
| `gemini-3.5-flash` | 1.50 | 0.15 | 9.00 | verified |
| `gemini-3.5-flash-lite` | 0.30 | 0.03 | 2.50 | verified |
| `gemini-3.1-flash-lite` | 0.25 | 0.025 | 1.50 | verified (text/image/video rate; shutdown 2027-05-07 at the earliest) |
| `gemini-3.1-pro-preview` | 2.00 | 0.20 | 12.00 | verified (prompts up to 200k) |
| `gemini-3-flash-preview` | 0.50 | 0.05 | 3.00 | verified (text/image/video rate) |
| `gemini-2.5-pro` | 1.25 | 0.125 | 10.00 | verified (prompts up to 200k) |
| `gemini-2.5-flash` | 0.30 | 0.03 | 2.50 | verified (text/image/video rate) |
| `gemini-2.5-flash-lite` | 0.10 | 0.01 | 0.40 | verified (text/image/video rate) |
| `gemini-3-flash` | unverified | unverified | unverified | not on the pricing page |
| `gemini-3-pro-preview` | unverified | unverified | unverified | shut down 2026-03-09 |

`gemini-3.1-pro-preview-customtools` shares one pricing section with `gemini-3.1-pro-preview`, so it is an
alias of it.

Ambiguities and how they are handled:

* **The three newest Flash prices change on 2027-01-01.** For `gemini-3.6-flash`, `-3.7-flash` and
  `-3.8-flash` the page reads "$0.75 through December 31, 2026. $1.50 starting January 1, 2027." (cached
  0.075 then 0.15, output 3.75 then 7.50). The table holds the price in force on the check date. **Update
  these three rows on 2027-01-01** (the note says so).
* **Two Pro models charge more above 200k prompt tokens** (`gemini-3.1-pro-preview`: 4.00 / 0.40 / 18.00;
  `gemini-2.5-pro`: 2.50 / 0.25 / 15.00). The table holds the up-to-200k rate.
* **Announced shutdown of `gemini-3.1-flash-lite`.** The deprecations page lists 2027-05-07 as its earliest
  shutdown date, with `gemini-3.5-flash-lite` as the replacement. The price holds until then; the note says
  to re-check.
* **The 2.5 models have limited access.** The deprecations page says access to them is limited to users who
  used them before, that they are not deprecated and that no shutdown date is announced. They stay tabled
  because the Gemini CLI still names them.
* **`gemini-3-flash-preview`** has no announced shutdown date and the deprecations page recommends
  `gemini-3.6-flash` as its replacement. `gemini-3.1-pro-preview` has none either.
* **Audio input** is priced higher on the Flash and Flash-Lite models (for example 1.00 on 2.5 Flash);
  coding sessions are text, so the text rate is used.
* **Thinking tokens** are included in the output price ("Output price (including thinking tokens)").
* **Cache storage** ($/1M tokens/hour) is a separate charge that the logs do not expose; not modelled.
* **`gemini-3-flash`** is the CLI's `SECONDARY_GEMINI_3_5_FLASH_MODEL`. The pricing and models pages list
  `gemini-3-flash-preview` and no `gemini-3-flash`; mapping one to the other would be a guess, so it is
  `unverified`.
* **`gemini-3-pro-preview`** was shut down on 2026-03-09 (replacement `gemini-3.1-pro-preview`, deprecations
  page) and is no longer priced, so its last price is `unverified`.
* Not tabled: routing aliases (`auto`, `pro`, `flash`, `flash-lite`, `auto-gemini-2.5`, `auto-gemini-3`),
  the internal `*-base` ids, embeddings, image/audio/live/TTS/transcription models, the Omni, robotics and
  computer-use models (none is an id the Gemini CLI bundle uses), and Gemma 4 (free tier only; the paid
  tier reads "Not available"). Also not tabled, because the deprecations page lists them as shut down and
  the pricing page no longer prices them, although the CLI bundle still names them:
  `gemini-3.1-flash-lite-preview` (shut down 2026-05-25, replacement `gemini-3.1-flash-lite`),
  `gemini-2.0-flash` and `gemini-2.0-flash-001` (shut down 2026-06-01).
* The Gemini CLI logs on the development machine hold no usage records, so these rows were not
  cross-checked against real token counts.

## xAI (Grok)

| Model id | Input | Cached | Output | Status |
|---|---|---|---|---|
| `grok-4.7` | 2.00 | 0.50 | 6.00 | verified (prompts under 200k) |

Read on <https://docs.x.ai/developers/models/grok-4.7> and its Markdown view, cross-checked on
<https://docs.x.ai/developers/pricing> (both say the docs are now branded "SpaceXAI Docs"; the domain is
still `docs.x.ai`). The models overview shows only the base rates (2.00 / 6.00); the Markdown view and
pricing page give the full table.

* **200k tier:** "Requests whose prompt reaches 200k tokens are billed at the higher rate for all tokens
  in the request": 4.00 / 1.00 / 12.00. The table holds the under-200k rate.
* **Priority processing** is 2x; the Batch API is not supported for `grok-4.7`.
* **Reasoning tokens:** the reasoning guide says they "are billed as part of your total consumption" and
  the pricing page lists them as a billed token type, but neither states their per-token rate. AgentFare
  counts them as output (see `models.Usage`), the usual convention; this is the one place where that is
  an assumption rather than a quote.
* xAI's `-latest` alias is stripped automatically by the price lookup (`grok-4.7-latest` resolves).

## Not verified, and why

| Model id | Reason |
|---|---|
| `codex-auto-review` | Internal Codex reviewer alias; OpenAI publishes no price or model page and does not name the model behind it |
| `gemini-3-flash` | Id used by the Gemini CLI, absent from Google's pricing page |
| `gemini-3-pro-preview` | Shut down 2026-03-09, no longer on the pricing page |

## Second pass (2026-09-30, after the first table)

Every page in the tables above was fetched again and compared with the table. Result: **no price that was
already in the table changed.**

* Anthropic: all eight rows, including the 1-hour column, match the pricing page; `claude.com/pricing`
  shows the same input, output, 5-minute write and read numbers; the model overview still lists the same
  ids and Bedrock spellings.
* OpenAI: `gpt-6-astra`, `gpt-6.1-sol` and `gpt-6-luna` match the pricing-page Standard table (short and
  long context); the model pages for those three, `gpt-6-sol` and `gpt-5.6-luna` match the table.
* Google: all eleven priced rows match the Standard tables, including the introductory prices of
  `gemini-3.6-flash`, `-3.7-flash` and `-3.8-flash` and their 2027-01-01 step.
* xAI: `grok-4.7` matches both the model page and the pricing page.

What is new in this pass: the 1-hour cache-write column, `gpt-5.6-sol` and `gpt-5.6-terra` (they appeared
in real Codex logs, 100 and 80 sessions, and were reported as "not in the price table"), the dated notes
(GPT-5.6 Sol promotion, `gemini-3.1-flash-lite` shutdown, the Gemini 2.5 access limit, Haiku 4.5
retirement) and the re-check of `codex-auto-review`, which is still unpriced.

## Review pass (2026-09-30, independent re-read)

Every price was read again from the pages themselves (raw HTML, and the Markdown view where a page has one), not
from the earlier notes. **No price value changed**: all eight Anthropic rows including the 1-hour column, the
seven OpenAI rows, the Gemini rows and `grok-4.7` match. The Codex credit table is still exactly 25 times the
USD prices, and the real-log figures quoted here (362 of 2,181 Codex sessions, 360M of 17.1B tokens for
`codex-auto-review`) were recomputed from the scratch database. What the review corrected:

* The pricing page lists `gpt-6-sol` and the three GPT-5.6 models in its Standard table, with cache writes and
  long-context columns; the earlier text put them on model pages only, or only in the "Cyber models" table.
  The HTML view collapses the table to three rows, which is why a plain-text reading missed them.
* The three GPT-5.6 rows now cite the Markdown view of the pricing page. On their model pages `--refresh`
  reported the rows without a cache-write cell (Terra and Luna) as changed; against `pricing.md` all seven
  OpenAI rows in the Standard table are confirmed.
* The long-context note of `gpt-5.6-terra` and `gpt-5.6-luna` now says that the cache rates double too, as the
  pricing table's long-context cells show; the model pages mention only input and output.

## Overriding or adding prices

The built-in table is the file above. To use different prices, add a model the table lacks or price a model
that is `unverified`, put a full copy in your data directory as `~/.agentfare/pricing.yaml` (or
`$AGENTFARE_HOME/pricing.yaml`, or the path in the `pricing_file:` key of `config.yaml`) and edit it.

* **The file replaces the built-in table, it is not merged with it.** A model missing from your file is
  reported as "not in the price table" and its spend is left out. So start from a copy:

  ```sh
  mkdir -p ~/.agentfare
  table=$(agentfare prices --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["pricing"]["path"])')
  cp "$table" ~/.agentfare/pricing.yaml
  ```

  (`agentfare prices --json` names the file in use under `pricing.path`. This works for every install, also for
  uv and pipx, whose environments are not the `python` on your PATH.)

  (In a source checkout, `cp pricing.yaml ~/.agentfare/pricing.yaml` does the same.)
  `agentfare prices --refresh --write` also creates the file from the built-in table, but `--refresh` is the
  one command that uses the network.
* **Because it is a copy, it does not follow later releases.** After upgrading AgentFare, compare your file
  with the new built-in one, or delete it to go back to the packaged prices. `agentfare prices` names the
  file in use on its first line (`price table: override file ...`).
* **Add a model** with a block under `models:`. Prices are USD per 1M tokens. `input`, `cached_input` and
  `output` must be present, each a number or the word `unverified`; `cache_write` (5-minute writes) and
  `cache_write_1h` (1-hour writes) are optional and default to the input price (1-hour writes to
  `cache_write` first). A second spelling of a model id goes under `aliases:`.

  ```yaml
  models:
    my-model:
      provider: openai
      input: 1.0
      cached_input: 0.10
      output: 4.0
      source: https://example.com/my-price-page
      checked: 2026-09-30
  aliases:
    my-model-latest: my-model
  ```

* **Price an unverified model** by replacing the three `unverified` values of its row with numbers. For
  `codex-auto-review`, OpenAI publishes no price, so any figure is your own assumption; say so in `note`:

  ```yaml
    codex-auto-review:
      provider: openai
      input: 0.20
      cached_input: 0.02
      output: 1.20
      source: https://developers.openai.com/api/docs/models/gpt-5.6-luna
      checked: 2026-09-30
      note: assumed to cost the same as gpt-5.6-luna; OpenAI publishes no price for this id
  ```

  With that row, `agentfare models` counted 360M more tokens and about $13 more on the development
  machine's logs, and the warning disappeared.
* **Until it is priced the CLI warns.** Every command that shows money names each unpriced model, for
  example `codex-auto-review: price is unverified, its spend is not counted (see pricing.yaml)`, and keeps
  it out of the dollar totals. A model with a number for `input` and `output` but `unverified` for
  `cached_input` or a cache-write column is still counted, with those tokens billed at the fallback rate,
  and the warning says so.
* A file that cannot be read (a price that is neither a number nor `unverified`, a duplicate model id, invalid
  YAML) stops the command with an error that names the model and field, for example
  `error: m.input: expected a number or 'unverified', got 'cheap'`.

## Refreshing the table

1. Re-read the pages above and update the values and the `checked` date.
2. Update the rows with a date in their note: the three 2027-01-01 Gemini rows, `gpt-5.6-sol` after
   2026-11-21, `claude-haiku-4-5` after 2026-10-15, `gemini-3.1-flash-lite` before 2027-05-07.
3. Keep every row's `source` a page you read; use `unverified` for anything you could not read.
4. Run `pytest tests/test_pricing_table.py`; it checks provenance, the number-versus-`unverified`
   rule (an unpriced row holds no number at all), the seeded prices, the GPT-5.6 prices, the published cache
   multipliers (Anthropic 1.25x write, 2x one-hour write and 0.1x read with two exceptions, OpenAI 1.25x
   write, no 1-hour rate elsewhere), the dated notes, the YAML round trip and that the pricing page's Standard
   table (kept in the test as the Markdown view lists it) confirms every OpenAI row it lists with the same check
   `prices --refresh` runs.

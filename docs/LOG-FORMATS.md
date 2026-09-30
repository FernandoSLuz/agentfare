# Log formats

What each parser reads, how it maps the agent's counters onto the five `Usage` buckets, and which quirks it handles. The parsers in `src/agentfare/parsers/` are the source of truth and each module's docstring has the measurements behind every rule, so read the code before you rely on a detail here.

| Agent | Version checked | Parser |
|---|---|---|
| Claude Code | 2.1.284 | `parsers/claude.py` |
| Codex CLI | 0.159.0 (records from 0.151 to 0.159 were seen) | `parsers/codex.py` |
| Gemini CLI | 0.60.0 | `parsers/gemini.py` |

* [Rules every parser follows](#rules-every-parser-follows)
* [Claude Code](#claude-code)
* [Codex CLI](#codex-cli)
* [Gemini CLI](#gemini-cli)
* [The quirks side by side](#the-quirks-side-by-side)
* [Adding an agent](#adding-an-agent)

## Rules every parser follows

The contract is in the docstring of `parsers/base.py` and the parser tests check it.

- Logs are read line by line. A malformed or truncated line, and a record type the parser does not know, are skipped: agents add new record types with every release.
- Only regular files are read (`base.is_regular_file` and `base.regular_files`, or `Path.is_file`). A named pipe, socket or device among the logs, or a link to one, is skipped, because opening a pipe blocks forever and a scan must not hang.
- Only counts, ids, model names, timestamps and a project name leave the parser. Prompt text, replies, tool inputs and outputs, and file contents are never copied into a `SessionRecord`.
- Tokens go into the five buckets of `models.Usage` and are attributed to the model that served each response, so a session that used two models has two usage rows.
- A response that the agent logs more than once (streaming chunks, replays after a resume, compaction or fork) is counted once.
- Money belongs to when it was spent. Each response's tokens also go into `SessionRecord.slots`, keyed by the UTC 15-minute slot (`models.slot_of`) of that response's own timestamp and by model, so a session that runs across midnight or a week boundary can be split between the days and weeks. Per model the slots add up to `usage`, because a parser builds both from the same responses in one pass. A response whose record has no usable timestamp goes to the slot of the session's first timestamp. A parser that records no timing leaves `slots` empty, which means all of `usage` counts at the session's start. The timestamp that dates a response is the one of the record that carries its final usage, and [the quirks below](#the-quirks-side-by-side) say which record that is for each agent.
- `tool_calls` counts tool invocations, `files_touched` distinct files written or edited, `error_retries` tool calls that failed, `prompts` what a person typed and `turns` the model responses after de-duplication. Only counts are kept, never names.
- Timestamps are normalised to `YYYY-MM-DDTHH:MM:SSZ` and cut to whole seconds (the slot of a response uses the same instant). `active_seconds` sums the gaps between consecutive events that are no longer than `idle_gap_minutes` (default 30). `wall_seconds` is the whole span.
- One row per session id. Ingesting a session again replaces its row, and a file whose `signature()` did not change is not read again. When the same session id is found in two files that no parser merges, `ingest` keeps the copy with the most tokens (on a tie the later path), counts such sessions in `IngestReport.duplicates` and warns once. The stored signature also holds the idle gap and `parsers.PARSERS_VERSION`, so changing `idle_gap_minutes` or installing a version that parses differently re-reads every file without `--full`. Bump `PARSERS_VERSION` whenever a parser's output for the same log changes.
- After parsing, `ingest` drops every model that has zero tokens (Claude's `<synthetic>` messages) and every session with no billed tokens at all.

## Claude Code

### Files

```text
~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl                        the session
~/.claude/projects/<encoded-cwd>/<session-uuid>/subagents/**/agent-*.jsonl   sub-agent and workflow transcripts
~/.claude/projects/<encoded-cwd>/<session-uuid>/{tool-results,workflows}/    spill files and scripts, not sessions
```

A session is its main file plus every sub-agent transcript below it. Those requests are real spend, so they are merged into the parent. Discovery lists the main files only and does not descend into `subagents`, `tool-results` or `workflows`. `signature()` covers the main file and a digest of every sub-agent file, so a change in any of them re-parses the session.

### Records that matter

| `type` | Used for |
|---|---|
| `assistant` | `message.id`, `message.model`, `message.usage` (tokens), `tool_use` content blocks |
| `user` | Prompts (string or text blocks), `tool_result` blocks (`is_error`), the flags `isMeta`, `isCompactSummary`, `isSidechain`, `origin.kind`, `promptSource` and `toolDenialKind` |
| `cost-state` | Ignored as input. Claude's own per-model totals, used only to cross-check the parser |

Every other record type (`permission-mode`, summaries, and so on) is ignored. `sessionId` and `session_id` are read to spot replayed history.

### Token mapping

| Bucket | Comes from |
|---|---|
| `input` | `usage.input_tokens` |
| `cache_read` | `usage.cache_read_input_tokens` |
| `cache_write` | `usage.cache_creation_input_tokens` minus the 1-hour part |
| `cache_write_1h` | `usage.cache_creation.ephemeral_1h_input_tokens`, capped at the total above |
| `output` | `usage.output_tokens` (thinking tokens are billed as output and included) |

The flat `cache_creation_input_tokens` is the total and the `cache_creation` split only says how much of it is 1-hour, so a record without a split (older logs) is all 5-minute and no token is lost or invented. Claude reports the cache buckets separately from `input_tokens`, so no subtraction is needed.

### De-duplication

- One API response is written as several `assistant` records that share `message.id` (one per content block). In newer logs the early records carry a `message_start` snapshot and only the last one has the final counts. Each bucket is reduced with `max` over the records of one id, which equals the last record, does not depend on line order, and keeps the counts of a stream that was cut short.
- `usage.iterations` lists the sampling calls behind a response. Each bucket takes the larger of the top-level counter and the sum of the iterations served by the message's own model, which fixes the rare records whose top-level counters are all zero. An iteration served by another model (the first attempt of a `fallback_message` retry) is not added.
- A response is dated by its last record, the one that carries the final usage (the latest usable timestamp among its records, so the result does not depend on line order or on copies of the file). A sub-agent's responses use their own timestamps. A response whose records have no usable timestamp counts at the session's first timestamp.
- Records whose `sessionId` or `session_id` names another session are history replayed into a forked or resumed file. The original file counts them, so they are skipped here. Both fields are checked because one fork rewrote `sessionId` on its copied records and kept the parent in `session_id`.
- A session moved to another project folder is copied there and the copies grow apart. Ingestion keeps one row per session id, so every copy parses to the union of all copies and of all sub-agent transcripts.

### What counts

| Field | Rule |
|---|---|
| `tool_calls` | Distinct `tool_use` blocks by id (streaming repeats the block) |
| `files_touched` | Distinct paths given to `Edit`, `Write`, `MultiEdit` and `NotebookEdit` (`file_path`, `notebook_path` or `path`) |
| `error_retries` | Distinct `tool_result` blocks with `is_error`, except those of a record that has `toolDenialKind`: a call the user or a permission rule refused is not a model failure |
| `prompts` | `user` records of the main file that a person typed. Not tool-result carriers, not `isMeta`, `isCompactSummary`, `isVisibleInTranscriptOnly` or sidechain records, not `origin.kind` other than `human`, not `promptSource: system`, and not text that only holds an injected wrapper (slash-command echoes, `<system-reminder>`, `<paseo-system>`, task notifications, interrupt markers) |
| `turns` | Distinct responses after de-duplication |
| Time | The `user` and `assistant` records, sub-agents included |
| `project` | Basename of the first `cwd`. Claude Code worktrees (`<repo>/.claude/worktrees/<name>`) and `<repo>-wt/<branch>` or `<repo>-wt-<branch>` folders are folded back onto the repo |

### Quirks

- The parser reads low on short sessions. Claude bills calls it never writes to the transcript (away summaries, title generation, prompt suggestions, compaction), and `server_tool_use` web search requests cost $0.01 each, which AgentFare does not price.
- A copy of another session's history that names no other session in any field cannot be told from new work. None exists on the machine the parser was built on.

## Codex CLI

### Files

```text
~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-uuid>.jsonl
~/.codex/archived_sessions/rollout-<timestamp>-<thread-uuid>.jsonl          flat, threads archived in the app
```

On the machine the parser was built on the two directories hold different threads (no id is in both), and both are scanned by default. A thread found in both would be stored once, from the copy with the most tokens. One file is one thread and one session. A sub-agent thread and an auto-review ("guardian") thread are files of their own, and their requests are real spend. Every line is `{"timestamp", "ordinal", "type", "payload"}`.

### Records that matter

| `type` and payload `type` | Used for |
|---|---|
| `session_meta` | Thread `id`, `cwd`, `git.repository_url`, `source` (a `subagent` key marks a sub-agent thread), `forked_from_id`, `subagent_history_start_ordinal` |
| `turn_context` | `turn_id` and `model`: the model of a response is the model of its turn |
| `token_usage_record` | `usage` of one response, its unique `response_id`, `thread_id` and `turn_id` |
| `event_msg` / `token_count` | `info.last_token_usage` (one response) and `info.total_token_usage` (the thread's running total) |
| `event_msg` / `item_completed` | `UserMessage` (prompts), `FileChange` (files), `CommandExecution` and `McpToolCall` (failures) |
| `event_msg` / `user_message`, `task_started` | Prompts in logs without `UserMessage` items, and the current turn |
| `response_item` / `function_call`, `custom_tool_call`, `local_shell_call` and their `..._output` | Tool calls and their results |
| `compacted` | Never read. It holds a replay of the history and a copy of the last usage record |

### Token mapping

OpenAI's convention holds on every record seen: `input_tokens` includes `cached_input_tokens`, `output_tokens` includes `reasoning_output_tokens`, and `total_tokens` equals input plus output. `cache_write_input_tokens` is read as one more part of `input_tokens` (it is 0 in every log seen).

| Bucket | Comes from |
|---|---|
| `input` | `input_tokens` minus `cached_input_tokens` minus `cache_write_input_tokens` |
| `cache_read` | `cached_input_tokens` |
| `cache_write` | `cache_write_input_tokens` |
| `cache_write_1h` | Always 0 |
| `output` | `output_tokens` (reasoning included) |

### De-duplication

- Usage is the sum of per-response counts, never a difference of running totals. Totals restart when a thread is resumed, and a fork's first `token_count` carries its parent's total with an empty `last_token_usage` (inherited state that bills nothing). A `token_count` whose total equals the previous one is a rate-limit update only.
- A `token_usage_record` and a `token_count` with the same counts are one response, in whichever order they come. A response seen in only one of them is still billed: context-compaction requests exist only as usage records, and logs from before usage records have only `token_count`.
- A repeated `response_id`, and a usage record of another thread, are dropped.
- Each billed response, a compaction request included, is dated by the timestamp of the record that billed it, so a thread that runs for days spreads its money over those days. A response whose record has no usable timestamp counts at the thread's first timestamp.
- A forked thread (`forked_from_id`) opens with a copy of its parent's history, the records before `subagent_history_start_ordinal`. The parent's file already counted everything in them, so they are skipped whole. Newer forks keep no copy.

### What counts

| Field | Rule |
|---|---|
| `tool_calls` | Distinct `function_call`, `custom_tool_call` and `local_shell_call` items by `call_id`. The model writes scripts for a single `exec` tool, so that is one call however many commands the script runs |
| `error_retries` | A tool call is counted once when its output starts with `Script failed`, or has an `Exit code:` or `Process exited with code` line or a JSON `exit_code` other than 0, or `success: false`, or when a command (non-zero exit code), file change or MCP call that ran during the call reports `status: failed`. A refused (`declined`) file change is not an error. A non-zero exit that is no real failure (`grep` found nothing) still counts, because the log cannot tell |
| `files_touched` | Distinct paths of `FileChange` items that did not fail or get declined (both ends of a move), or in logs without such items the `*** Add/Update/Delete File:` and `*** Move to:` headers of `apply_patch` text. Names only de-duplicate in memory and are never kept. Files written by a shell command (`cat > f`, `sed -i`) leave no trace and are not counted |
| `prompts` | `UserMessage` items, else `user_message` events. A message from the Paseo orchestrator (`<paseo-system>`) is not a prompt. A sub-agent thread has 0 prompts: its user messages are task hand-offs and review requests |
| `turns` | Responses billed |
| Time | Every record is an event |
| `project` | Repository name of `git.repository_url` when the thread has one, else the basename of `cwd` |

### Quirks

- Sub-agent and auto-review threads are separate sessions with 0 prompts, and they are most of the sessions in a busy history. Their auto-review model is `codex-auto-review`, which has no published price.
- A few versions write a whole thread in one instant (identical record timestamps): such a thread shows no duration.
- The session id is the thread id from `session_meta`, else the UUID in the file name. Codex ids are UUIDv7, so the first 8 characters are a timestamp that repeats, and `models.short_id` uses the last 8.

## Gemini CLI

### Files

```text
~/.gemini/tmp/<slug>/.project_root                                          working directory of the project, one line
~/.gemini/tmp/<slug>/chats/session-<UTC minute>-<first 8 chars of sessionId>.jsonl   one session
~/.gemini/tmp/<slug>/chats/<sessionId>/<agent id>.jsonl                     sub-agent transcripts ("kind": "subagent")
```

Sub-agent transcripts are merged into the parent session, and `signature()` covers them. Directories such as `logs` and `tool-outputs` are not sessions. Older CLIs wrote one `session-*.json` object `{sessionId, ..., messages: [...]}`; a session stored in both forms is read from the JSONL.

A session file starts with a header `{sessionId, projectHash, startTime, lastUpdated, kind}`, then messages `{id, timestamp, type, content}` where `type` is `user`, `gemini` or `info`. A `gemini` message also has `model`, `tokens`, `thoughts` and `toolCalls`. `{"$set": {...}}` records patch the metadata and `{"$rewindTo": "<id>"}` records rewind the chat.

### Token mapping

The counters are the Gemini API's `usageMetadata`: `input` includes `cached`, `output` excludes `thoughts`, and `tool` (tool-use prompt tokens) is extra prompt.

| Bucket | Comes from |
|---|---|
| `input` | `tokens.input` minus `cached`, plus `tool` |
| `cache_read` | `tokens.cached` |
| `cache_write` | Always 0 (Gemini has no write fee) |
| `cache_write_1h` | Always 0 |
| `output` | `tokens.output` plus `thoughts` (thinking is billed as output) |

The model is `message.model`. The CLI records none for a reply with no text, thoughts or tool calls (billed like any other reply), so that reply is attributed to the model of the reply before it in the same file, and to `unknown` (unpriced, hence reported) when there is none.

### De-duplication

- The CLI re-appends a message whenever it changes (tokens and tool calls arrive later), so the last record with an id is that message. The CLI stamps a message once, when it creates it, and every rewrite keeps that stamp, so the last record's timestamp equals the first's: that is the time of the response. A sub-agent's responses use their own timestamps, and a message with no parseable timestamp counts at the session's first timestamp.
- The CLI's own loader drops messages after `$rewindTo` and replaces the list on `$set.messages`, but those responses were still billed, and the compacted ones are the expensive part of long sessions. The billing view therefore ignores both records: every message ever recorded counts once, by id. The `messages` inside `$set` are ignored too, because each is a message already appended or a token-less copy under a fresh id.
- `gemini --session-file` starts a new session whose first message is an `import-<ms>` notice, followed by the source session's messages copied verbatim. They were billed in the source session, so a message older than the notice is skipped everywhere, the time span included.

### What counts

| Field | Rule |
|---|---|
| `turns` | `gemini` messages with token counts |
| `tool_calls` | The `toolCalls` entries of `gemini` messages |
| `error_retries` | Calls with status `error` or `cancelled`, plus shell commands recorded as `success` whose output ends with a non-zero `Exit Code:` or a `Signal:` line, or begins with the timeout notice. Other statuses (validating, scheduled, executing, awaiting approval, success) are not errors |
| `files_touched` | Distinct `file_path` values of `write_file` and `replace` calls that succeeded. Shell commands cannot be attributed to files |
| `prompts` | `user` messages of the main file that a person typed: not tool results (`functionResponse` parts), and not text the CLI treats as non-conversation (empty, or starting with `/`, `?`, `<session_context>`, `<hook_context>`, `<state_snapshot>`). A sub-agent's user message is a task hand-off, not a prompt |
| Time | Header times, message timestamps and tool-call timestamps |
| `project` | Basename of `.project_root`, else the name of the slug directory |

### Quirks

- The parser follows the CLI's `ChatRecordingService` and its loader. The only real Gemini logs available while it was written held no conversations, so token handling is checked against that source and the synthetic fixtures only.
- `prompts` can run high. The CLI records turns it injects itself (loop-recovery feedback, IDE context) and replays the whole history as plain `user` messages when it masks old tool output or restores a checkpoint. Only `prompts` is affected, because replayed `gemini` messages carry no tokens, tool calls or files.
- Calls the CLI makes outside the chat (model router, classifier, compaction summaries, web tools) are not in these files and so not in the totals.

## The quirks side by side

| | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|
| Sub-agents | Merged into the parent session | A file and a session of their own, 0 prompts | Merged into the parent session |
| Fork, resume or import replays | Records that name another session id are skipped | A fork's copied history is skipped by ordinal, running totals are never differenced | Messages older than the `import-<ms>` notice are skipped |
| Rewound or compacted history | Compaction summaries are not prompts | `compacted` records are never read, compaction requests are billed from usage records | `$rewindTo` and `$set.messages` are ignored, every message counts once by id |
| Archived sessions | None | `~/.codex/archived_sessions` is scanned as well | None |
| Same session in several files | Copies in other project folders are merged | None seen; a thread in both Codex directories is stored from the copy with the most tokens | The JSONL wins over a legacy `.json` |
| The time of a response | The last record of its `message.id` | The record that billed it (`token_usage_record` or `token_count`) | The message's own `timestamp` |
| Costs the log does not show | Unlogged calls, web search requests | Compaction is only partly in the running totals (billed from usage records) | Calls outside the chat |

## Adding an agent

An agent is one module in `src/agentfare/parsers/` that exposes a module-level `PARSER` with the `LogParser` shape from `parsers/base.py`:

```python
class LogParser(Protocol):
    agent: str

    def discover(self, root: Path) -> Iterator[Path]: ...
    def signature(self, path: Path) -> str: ...
    def parse(self, path: Path, *, idle_gap_seconds: int = 1800) -> list[SessionRecord]: ...
```

- `discover(root)` yields the files under `root` that each describe one session, recursively and in a stable order, and only regular files (`base.regular_files` or `Path.is_file`). Skip sub-agent and spill directories that you merge or ignore.
- `signature(path)` is a cheap change detector for the file and anything merged into it. `base.stat_signature` (size and modification time) is the default, and a parser that merges other files adds a digest of theirs.
- `parse(path, idle_gap_seconds=...)` returns zero or more `SessionRecord`s. It must tolerate malformed lines and unknown record types, never copy text into a record, fill the five `Usage` buckets, de-duplicate repeated responses, fill `slots` from each response's own timestamp (per model they must add up to `usage`), and follow the counting rules above. `base.project_name` and `base.active_seconds` do two of the chores.

To wire it in:

1. Write `parsers/<agent>.py`. `get_parser` imports the module by the agent's name.
2. Add the name to `models.AGENTS` (it drives `get_parser`, the order of ingestion and the `--agent` validation) and to the tuple in `commands/config.py` that lists the sources.
3. In `DEFAULT_CONFIG_YAML` add `sources.<agent>`. For `agentfare run` add `agents.<agent>` (its commands and model flag) and `router.run.models.<agent>`, and teach `runwrap.SYNTAX` the agent's command line if its prompt and model options should be read.
4. Make sure every model the agent logs has a row in `pricing.yaml`, or it shows up as unpriced.
5. Add synthetic logs in the agent's real format under `tests/fixtures/<agent>/`, with a `PRIVACY-CANARY-*` marker in every content field, and write the parser tests: token mapping, de-duplication, malformed lines, unknown records, and the marker in neither the record nor the database bytes. Extend `tests/fixtures/config.yaml` only if the sample demo should include the agent, since that changes the README output blocks (rebuild them with `python tests/test_readme.py --write`).
6. Bump `parsers.PARSERS_VERSION`, so that logs already in the database are read again with the new parser.
7. Document the format here and add a row to the log formats table of the README.

Custom agents in the `agents:` config are a different thing: they exist for `agentfare run` and need no parser. Their logs are not ingested.

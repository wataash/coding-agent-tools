# coding-agent-tools

Inspect running Claude Code / Codex sessions, wait for agents to finish, analyze
Claude Code transcript usage, and record subscription usage limits. The repository
provides these Python command-line tools:

- `agent_activity.py` reports sessions that are currently working.
- `waitall.py` waits until agents and load average are idle.
- `agent_limit_history.py` stores usage observations in SQLite and plots them.
- `claude_turn_usage.py` summarizes tokens and cost by turn or session.
- `claude_plot_usage.py` plots token usage and cost over a session.

None of the tools starts inference turns. They use only the Python standard
library except that plotting uses matplotlib.

## Requirements

- Python 3.10 or later.
- Claude Code and/or Codex CLI with existing local authentication.
- Linux with access to the host's `/proc/` for Codex activity detection.
- matplotlib for plotting limit history; tkinter for `--show`.

Run commands from the repository directory. Use the provider-selection options
when only one CLI is available.

## Running-session activity

```sh
# fish / bash
python3 agent_activity.py
python3 agent_activity.py --no-claude
python3 agent_activity.py --no-codex
python3 agent_activity.py -n
python3 agent_activity.py --command-timeout 20 -q
```

The command prints one JSON object with `codex` and `claude` arrays containing
running entries. A disabled provider has an empty array. Diagnostics go to stderr.
Exit status is 0 on successful inspection, whether or not agents are running;
inspection failures return 1 without printing a misleading empty result.
`-n` / `--dry_run` prints inspection commands without reading sessions or running
commands. It does not enumerate local databases or rollout files. `-q` lowers
verbosity.

### Python API

Place `agent_activity.py` alongside the caller or add this repository to
`PYTHONPATH`. The module does not configure logging when imported.

```python
import agent_activity

codex = agent_activity.read_codex_running(command_timeout=10.0)
claude = agent_activity.read_claude_running(command_timeout=10.0)
commands = agent_activity.inspection_commands(codex=True, claude=False)
```

The readers return lists of entry dictionaries. Codex entries have an `id` and
status; local rollout entries also contain `pid`, `processUuid`, `cwd`, `name`,
and `detection='rollout'`. Claude entries preserve the CLI's fields. Callers
should use `.get()` for optional display fields. Protocol and database errors
raise exceptions instead of indicating that all work is finished.

### Activity detection

- Codex CLI: map live Codex PIDs to thread IDs using `logs_2.sqlite`. Read each
  process's start time from `/proc/` to exclude logs from earlier uses of its PID.
  Read each thread's `rollout_path` from `state_5.sqlite`, then scan its JSONL
  history from the end. `task_started` means running; `task_complete` and
  `turn_aborted` stop it.
  Incomplete appended JSONL records are ignored.
- Codex daemon: when its control socket exists, query `thread/list` through
  `codex app-server proxy`. Active sessions waiting on approval or user input are
  excluded. Results are deduplicated by thread ID.
- Claude: query `claude agents --json`. Background entries require
  `state=working`; interactive entries require `status=busy`. Older live entries
  without activity fields are conservatively considered running.

Codex data is read from `CODEX_HOME`, defaulting to `~/.codex/`. SQLite databases
are opened read-only. Codex CLI rollout detection includes the interval between
task start and task end, including approval waits. PID namespaces that hide host
processes are unsuitable for inspection. Internal database schemas, rollout
events, and CLI protocols may change and require updates. Session contents and
credentials are not bundled or copied into this repository.

Load average checks, repeated polling, keyboard input, and terminal presentation
belong to callers such as `waitall.py`, not this module.

## Waiting for idle agents

`waitall.py` repeatedly checks Claude Code, Codex, and Linux load average, then
exits when every enabled condition is idle. Pressing a key triggers an immediate
check. See [waitall.md](waitall.md) for options and detection details.

```sh
# fish / bash
python3 ./waitall.py
python3 ./waitall.py --interval 30 --threshold 0.5 --loadavg-minutes 5
python3 ./waitall.py --no-claude --no-loadavg
```

## Claude transcript usage

`claude_turn_usage.py` summarizes token usage and estimated USD cost from a
Claude Code transcript. `claude_plot_usage.py` uses the same parsing and pricing
to plot per-turn and cumulative usage. Rates are hard-coded estimates; unknown
models are clearly marked. Subagent transcripts are separate and are not folded
into their parent transcript automatically.

```sh
# fish / bash
python3 ./claude_turn_usage.py ~/.claude/projects/<project>/<session>.jsonl
python3 ./claude_turn_usage.py --both --by_model <session>.jsonl
python3 ./claude_plot_usage.py <session-id>
```

See [claude_turn_usage.md](claude_turn_usage.md),
[claude_plot_usage.md](claude_plot_usage.md), and
[claude_long_context_pricing.md](claude_long_context_pricing.md) for details.

## Subscription limit history

Periodically fetch Claude Code / Codex subscription usage and store it in SQLite.
Usage is not estimated from token counts or equivalent API costs.

![Claude and Codex subscription usage over time, with five-hour and weekly limits](assets/limits.webp)

Collection uses the current Python interpreter for Claude and launches
`codex app-server` directly for Codex. Use `--providers claude` or
`--providers codex` to collect just one service.

To run collectors through a sandbox or another launcher, set `--claude-command`
and/or `--codex-command`. Each value is split with Python's `shlex.split` and
executed without a shell; quote paths containing spaces inside the value. The
Claude command must emit one JSON object with `observed_at` (UTC epoch seconds)
and `windows` (arrays of `[bucket, window_minutes, used_percent, resets_at]`), or
an `error` string on failure. The built-in `_fetch-claude` command implements
this format. The Codex command must start an app-server speaking the stdio RPC
protocol. `collect -n` prints commands without executing them or creating the DB.

### Collection and storage

- Claude sends a GET request to `https://api.anthropic.com/api/oauth/usage`,
  using the existing OAuth access token from `~/.claude/.credentials.json` and
  the `anthropic-beta: oauth-2025-04-20` header. It stores `five_hour` and
  `seven_day*`. This is an internal CLI endpoint and may change.
- Codex starts the installed CLI's app-server over stdio, then sends
  `initialize` → `initialized` → `account/rateLimits/read`. It creates no threads
  or turns. Limits come from `rateLimitsByLimitId`, with a fallback to
  `rateLimits`, and are classified by `windowDurationMins`.
- Missing usage values are not replaced with 0%. Credentials, response bodies,
  prompts, and conversation history are never stored in the database or logs.
  Collection exceptions are recorded by type only; HTTP and RPC errors by code.
  Invalid collector observations are recorded as failures without partial window
  rows, and collection continues with the next service.
- The default database is `data/history.sqlite3`; override it with `--db`.
  Observations contain UTC epoch time, service, limit identifier, window duration,
  usage percentage, and reset time. Failures are also recorded, and a failure in
  one service does not stop the other. New files use umask 0077. SQLite uses WAL
  and transactions.
- Cached values are not recorded again as current observations. Each successful
  query is saved.

This script does not refresh Claude tokens. Normal Claude Code use handles token
refresh. If HTTP 401 errors persist, check authentication on the collection host.
Rate limiting, network failures, and expired credentials are recorded as failed
observations; a later `collect` retries them.

### Limit-history commands

```sh
# fish / bash
python3 ./agent_limit_history.py -q collect
python3 ./agent_limit_history.py collect -n
python3 ./agent_limit_history.py -q plot --days 14 --output /tmp/agent-limits.png
python3 ./agent_limit_history.py -q plot --days 14 --show
python3 ./agent_limit_history.py -q snapshot --output /tmp/agent-limits.sqlite3
```

`--show` opens a matplotlib TkAgg window with zoom and pan controls. All four
panels share axes. It requires tkinter and a display; no file is written unless
`--output` is also provided.

The plot has four panels: Claude / Codex × 5h / weekly. Additional model-specific
limits are plotted separately by identifier. The horizontal axis defaults to UTC
and can be changed with `--timezone`. Failed or missing observations become NaN.
Lines break at observation gaps longer than 15 minutes and at actual window
transitions. Windows of unknown duration remain in the database but are omitted
from the four-panel plot.

## Tests

```sh
# Activity, waiting, and transcript tests require pytest; limit-history uses unittest.
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider --doctest-modules test_agent_activity.py waitall.py claude_turn_usage.py claude_plot_usage.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ./ -p agent_limit_history_test.py
```

Tests use synthetic databases, process directories, rollout records, and protocol
responses. They do not require live agent sessions.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).

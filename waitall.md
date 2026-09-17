# waitall.py

[waitall.py](waitall.py)

A waiting script that checks Codex, Claude Code, and the load average at a fixed
interval and exits once every enabled condition is satisfied.

- No running Codex session
- No running Claude Code session
- The selected load average is below the threshold

Each check prints a timestamp, the running counts, the load average, and a
summary of the running entries. While waiting in a terminal, pressing any key
fetches and prints the latest state without waiting for the next scheduled
check. Terminal stdout is colored like syntax highlighting: keys in cyan,
numbers in yellow, strings/paths/IDs in green, keywords in magenta,
operators/separators in white, and timestamps and hints in gray. Colors do not
change based on whether the state is good or bad. Colors are disabled
automatically when stdout is redirected or piped.

## Usage

```sh
waitall.py
waitall.py --interval 30 --threshold 0.5 --loadavg-minutes 5
waitall.py --no-codex
waitall.py --no-claude --no-loadavg
```

## Structure and running detection

Agent detection is delegated to [agent_activity.py](agent_activity.py) in the
same repository. `waitall.py` handles the waiting loop, the load average, key
input, and colored output.

Keep `waitall.py` and `agent_activity.py` in the same directory. Only the Python
standard library is used; no package installation is required.

- Codex CLI: live PIDs are matched against conversation history, and a session
  is running from `task_started` until `task_complete` / `turn_aborted`.
- Codex daemon: active sessions, excluding those waiting for approval or user
  input.
- Claude: background sessions are running when `state=working`, interactive
  sessions when `status=busy`. If an older CLI does not report a state, live
  entries are treated as running.

Run it where the host's PIDs are visible. See the coding-agent-tools README for
detection methods, DB/CLI dependencies, and detection tests. Command or DB
errors cause an abnormal exit and are never treated as satisfied conditions.

## Options

| Option | Description |
| --- | --- |
| `--interval` | Check interval in seconds (default `60`). |
| `--threshold` | Load average threshold (default `1.0`). |
| `--loadavg-minutes` | Which load average to compare: `1` / `5` / `15` minutes (default `1`). |
| `--command-timeout` | Timeout in seconds for each session-listing command (default `10`). |
| `--color` | Color for stdout: `auto` / `always` / `never` (default `auto`). |
| `--no-codex` | Disable the Codex condition. `--ignore-codex` is also accepted. |
| `--no-claude` | Disable the Claude Code condition. `--ignore-claude` is also accepted. |
| `--no-loadavg` | Disable the load average condition. `--ignore-loadavg` is also accepted. |
| `-n`, `--dry_run` | Print the commands used to check sessions and exit. |
| `-q`, `--quiet` | Suppress debug logs such as internal commands. |

The load average is read from Linux's `/proc/loadavg`, so this is Linux-only.

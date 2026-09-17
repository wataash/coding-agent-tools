# claude_plot_usage.py

[claude_plot_usage.py](claude_plot_usage.py)

Plots a Claude Code session's **token usage and USD cost over turns** as a PNG:
x-axis = turn number, four panels —

|                | tokens                                   | cost (USD)      |
| -------------- | ---------------------------------------- | --------------- |
| **per turn**   | in / out / cache read / cache write      | one line        |
| **cumulative** | the same four series, running totals (Σ) | running total   |

Turn splitting, `message.id` de-duplication, and per-model pricing are imported
from the sibling [claude_turn_usage.py](claude_turn_usage.md) (the two files
must stay in the same directory): a *turn* starts at a genuine user prompt (a
`user` JSONL entry carrying `promptSource`) and runs to the next one; entries
before the first prompt fold into turn 1. Cost is priced per model, so
mixed-model sessions are summed correctly.

The token panels default to a **symlog** y scale (linear below 1k, log above):
cache reads run in the millions per turn while `in` runs in the hundreds, so a
linear axis flattens everything but cache reads to zero. Use `--linear` if the
magnitudes in your session are comparable.

> **Not counted: subagent usage** — same caveat as `claude_turn_usage.py`;
> subagent transcripts (`<proj>/<session-id>/subagents/agent-*.jsonl`) are
> separate files. Plot one by passing its path explicitly.

## Usage

```sh
# Writes <session>.usage.png into the current directory, prints the path,
# and opens it with xdg-open (suppress with --no-open)
python claude_plot_usage.py ~/.claude/projects/<proj>/<session>.jsonl

# A bare session id (or unique prefix) is searched in ~/.claude/projects/*/
python claude_plot_usage.py <session-id>

# Explicit output path, linear token axes, interactive window
python claude_plot_usage.py session.jsonl -o usage.png --linear --show --no-open

# statusLine-protocol stdin (same as claude_turn_usage.py)
echo '{"transcript_path":"session.jsonl"}' | python claude_plot_usage.py
```

## Options

| Option        | Description                                                                 |
| ------------- | --------------------------------------------------------------------------- |
| `transcript`  | Session `.jsonl` path, or a session id (prefix ok) searched in `~/.claude/projects/*/`; if omitted, read `transcript_path` from stdin. |
| `-o, --output`| Output PNG path (default: `<transcript basename>.usage.png` in the cwd).    |
| `-n, --dry_run` | Print the `xdg-open` command instead of executing it.                     |
| `--no-open`   | Do not `xdg-open` the PNG after writing it.                                  |
| `--dpi`       | PNG resolution (default 120).                                               |
| `--linear`    | Linear y scale on the token panels instead of the default symlog.           |
| `--show`      | Also open an interactive matplotlib window.                                 |
| `-q, --quiet` | Decrease verbosity (`-q` info, `-qq` warning, `-qqq` error).                |
| `-h, --help`  | Show help and exit.                                                          |

## Requirements

- `matplotlib`
- `claude_turn_usage.py` in the same directory (parsing + pricing)

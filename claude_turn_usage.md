# claude_turn_usage.py

[claude_turn_usage.py](claude_turn_usage.py)

Prints the token usage and USD cost of the **most recent prompt round-trip
(turn)** in a Claude Code session, in a compact one-line form:

```
⟳ 12.5k in, 54.4k out, 5.9m cr, 552.4k cw ($7.83)
```

A *turn* starts at the last genuine user prompt (a `user` entry in the session
`.jsonl` carrying a `promptSource` key — tool-result entries lack it) and runs
to the end of the transcript. A single turn can trigger several assistant API
calls via tool-use loops, so their input (`in`) / output (`out`) / cache read
(`cr`) / cache write (`cw`) token counts are summed. Duplicate JSONL lines that Claude Code
writes for one streamed message are de-duplicated by `message.id`, so each
message is counted once (the last occurrence wins). With `--all` the whole
session is summed instead of the last turn (prefix `Σ` instead of `⟳`), and
`--both` prints the `⟳` and `Σ` lines from a single transcript read; the `Σ`
cost matches `ccusage`'s session cost (unless subagents ran — see below). Cost is computed per model from
hard-coded per-million-token rates (Fable 5, Mythos 5, Opus 5 / 4.8 / 4.7 / 4.6,
Sonnet 5 / 4.6, Haiku 4.5), splitting 5-minute vs 1-hour cache-write TTLs. A
trailing variant suffix like the 1M-context `claude-fable-5[1m]` is stripped
before the rate lookup. Stripping is also economically correct: the 1M context
window bills at standard rates, with no long-context premium — see
[claude_long_context_pricing.md](claude_long_context_pricing.md).

A model with no rate row of its own is priced at Opus 4.8 rates and its name is
suffixed with `?` in the `--by_model` breakdown. The fallback is a guess, not a
rate — a model priced above Opus (Fable 5 is 2x) would be under-reported — so
the marker exists to keep a guessed cost from passing as a known one. Seeing a
`?` means adding the model to `PRICES`.

> **Not counted: subagent usage.** Subagent (Task/Agent tool) API calls are
> logged to separate transcripts —
> `<proj>/<session-id>/subagents/agent-*.jsonl`, next to the session `.jsonl`
> — which this script does not read, so neither `⟳` nor `Σ` includes them.
> When a session ran subagents, `Σ` is lower than `ccusage`'s session cost by
> exactly the subagents' usage (`ccusage` aggregates the per-session
> directory too). To cost a subagent transcript, pass its path explicitly:
> `python claude_turn_usage.py --all <proj>/<session-id>/subagents/agent-*.jsonl`.

## Usage

```sh
# Explicit transcript path — most recent turn
python claude_turn_usage.py ~/.claude/projects/<proj>/<session>.jsonl

# Whole-session cumulative (Σ)
python claude_turn_usage.py --all ~/.claude/projects/<proj>/<session>.jsonl

# Both ⟳ and Σ lines from a single transcript read
python claude_turn_usage.py --both ~/.claude/projects/<proj>/<session>.jsonl

# /usage-style block (total cost + per-model); combines with --all / --both
python claude_turn_usage.py --by_model --all ~/.claude/projects/<proj>/<session>.jsonl

# Claude Code statusLine hook: JSON with transcript_path arrives on stdin
echo '{"transcript_path":"session.jsonl"}' | python claude_turn_usage.py
```

`--by_model` prints a total-cost line plus one indented line per model; when
only one model was used, the two collapse into a single `|`-joined line:

```
Σ $20.25 | claude-opus-4-8: 7.8k in, 60.7k out, 21.5m cr, 795.9k cw ($20.2455)
```

As a Claude Code status line (shows the last turn's usage, refreshed each
turn), in `~/.claude/settings.json`:

```json
"statusLine": {
  "type": "command",
  "command": "python3 /path/to/claude_turn_usage.py"
}
```

When no positional argument is given, the script reads a JSON object from stdin
and uses its `transcript_path` field, matching the Claude Code statusLine hook
protocol.

These lines can also be combined with `ccusage` and subscription rate-limit
bars from a separate Claude Code `statusLine` wrapper.

## Options

| Option        | Description                                                            |
| ------------- | --------------------------------------------------------------------- |
| `transcript`     | Session `.jsonl` path; if omitted, read `transcript_path` from stdin. |
| `-a, --all`      | Sum the whole session (cumulative, `Σ`) instead of the last turn.  |
| `-b, --both`     | Print both the `⟳` and `Σ` lines from a single transcript read (supersedes `--all`). |
| `-m, --by_model` | Expand to a `/usage`-style block: a total-cost line + one indented per-model breakdown line (collapsed to one line when a single model was used). |
| `-q, --quiet`    | Decrease verbosity (`-q` info, `-qq` warning, `-qqq` error).       |
| `-h, --help`  | Show help and exit.                                                    |

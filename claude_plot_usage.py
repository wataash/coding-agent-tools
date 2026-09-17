#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 Wataru Ashihara <wataash0607@gmail.com>
# SPDX-License-Identifier: Apache-2.0
epilog = r"""
claude_plot_usage.py -h
claude_plot_usage.py ~/.claude/projects/<proj>/<session>.jsonl
claude_plot_usage.py <session-id>            # searched in ~/.claude/projects/*/
claude_plot_usage.py session.jsonl -o usage.png --no-open
echo '{"transcript_path":"session.jsonl"}' | claude_plot_usage.py
pytest -v --doctest-modules ~/src/coding-agent-tools/claude_plot_usage.py  # @pl
"""[1:]

import argparse
import glob
import itertools
import json
import logging
import os
import shlex
import subprocess
import sys

# Token parsing / pricing live in the sibling claude_turn_usage.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_turn_usage as ctu  # noqa: E402

logger = ctu.logger

# Light-mode categorical slots 1-4 + chart chrome (dataviz reference palette).
# Marker shapes double as the CVD-relief secondary encoding for the sub-3:1
# slots (aqua, yellow).
SERIES = [
    # (key, label, hex, marker)
    ("in", "in", "#2a78d6", "o"),
    ("out", "out", "#1baf7a", "s"),
    ("read", "cache read", "#eda100", "^"),
    ("write", "cache write", "#008300", "D"),
]
COST_COLOR = "#2a78d6"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"


def main() -> int:
    parser = argparse.ArgumentParser(formatter_class=ctu.ArgumentDefaultsRawTextHelpFormatter, epilog=epilog)
    parser.add_argument("-q", "--quiet", action="count", default=0,
                        help="decrease verbosity; default: debug, -q: info, -qq: warning, -qqq: error")
    parser.add_argument("transcript", nargs="?",
                        help="path to a Claude Code session .jsonl, or a session id\n"
                             "(prefix ok) searched in ~/.claude/projects/*/;\n"
                             "if omitted, read {\"transcript_path\": ...} JSON from stdin\n"
                             "(the Claude Code statusLine hook protocol)")
    parser.add_argument("-o", "--output",
                        help="output PNG path; default: <transcript basename>.usage.png\n"
                             "in the current directory")
    parser.add_argument("-n", "--dry_run", action="store_true",
                        help="print the xdg-open command instead of executing it")
    parser.add_argument("--no-open", dest="open", action="store_false",
                        help="do not xdg-open the PNG after writing it")
    parser.add_argument("--dpi", type=int, default=120, help="PNG resolution")
    parser.add_argument("--linear", action="store_true",
                        help="linear y scale on the token panels (default: symlog,\n"
                             "since cache reads dwarf in/out by ~3 orders of magnitude)")
    parser.add_argument("--show", action="store_true",
                        help="also open an interactive matplotlib window")
    args = parser.parse_args()
    logger.setLevel({0: logging.DEBUG, 1: logging.INFO, 2: logging.WARNING}.get(args.quiet, logging.ERROR))
    logger.debug(f"{args=}")
    return run(args)


def run(args: argparse.Namespace) -> int:
    path = args.transcript
    if path is None:
        try:
            # bytes in, so decoding is UTF-8 regardless of locale
            path = json.loads(sys.stdin.buffer.read()).get("transcript_path")
        except (json.JSONDecodeError, ValueError, AttributeError):
            # AttributeError: valid JSON but not an object (e.g. null) -> no .get
            path = None
    if not path:
        logger.error("no transcript path (argv or stdin transcript_path)")
        return 1
    path = resolve_transcript(path)
    if path is None:
        return 1
    try:
        # errors="replace": tolerate a transcript truncated mid-write; the
        # mangled line just fails JSON parsing and is skipped.
        # NOTE: subagent usage is NOT counted -- see claude_turn_usage.md.
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError as e:
        logger.error(f"cannot read transcript: {e}")
        return 1

    series = turn_series(ctu._parse_lines(lines))
    n = len(series["cost"])
    if n == 0:
        logger.error("no turns found in transcript")
        return 1
    logger.info(f"{n} turns, total ${sum(series['cost']):.2f}")

    output = args.output
    if output is None:
        output = os.path.splitext(os.path.basename(path))[0] + ".usage.png"
    plot(series, title=os.path.basename(path), output=output, dpi=args.dpi,
         linear=args.linear, show=args.show)
    logger.info(f"wrote {output}")
    print(output)
    if args.open:
        cmd = ["xdg-open", output]
        if args.dry_run:
            print(shlex.join(cmd))
        else:
            logger.info(shlex.join(cmd))
            try:
                subprocess.run(cmd, check=False)
            except OSError as e:
                logger.error(f"xdg-open failed: {e}")
    return 0


def resolve_transcript(arg: str) -> str | None:
    """Resolve the transcript argument to a readable path.

    An existing path is returned as-is; otherwise the argument is taken as a
    session id (a prefix suffices) and searched as
    ``~/.claude/projects/*/<arg>*.jsonl``. Ambiguity or no match logs an error
    and returns None.
    """
    if os.path.exists(arg):
        return arg
    matches = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{glob.escape(arg)}*.jsonl"))
    if len(matches) == 1:
        logger.debug(f"resolved {arg!r} -> {matches[0]}")
        return matches[0]
    if not matches:
        logger.error(f"no such file, and no ~/.claude/projects/*/{arg}*.jsonl")
        return None
    logger.error(f"ambiguous session id {arg!r}:\n" + "\n".join(sorted(matches)))
    return None


def split_turns(objs: list[dict]) -> list[list[dict]]:
    """Split parsed transcript entries into per-turn segments.

    A turn starts at a genuine user prompt (a ``user`` entry carrying a
    ``promptSource`` key -- tool-result user entries lack it) and runs to the
    next one. Entries before the first prompt (e.g. a resumed-session summary)
    fold into turn 1; with no prompt at all, everything is one turn.

    >>> split_turns([])
    []
    >>> p = {"type": "user", "promptSource": "user"}
    >>> split_turns([{"a": 1}, p, {"b": 2}])
    [[{'a': 1}, {'type': 'user', 'promptSource': 'user'}, {'b': 2}]]
    >>> [len(seg) for seg in split_turns([p, {"a": 1}, {"a": 2}, p, {"b": 1}])]
    [3, 2]
    """
    starts = [i for i, o in enumerate(objs) if o.get("type") == "user" and "promptSource" in o]
    if not starts:
        return [objs] if objs else []
    starts[0] = 0
    return [objs[s:e] for s, e in zip(starts, starts[1:] + [len(objs)])]


def turn_series(objs: list[dict]) -> dict[str, list]:
    """Per-turn token counts and USD cost, as parallel lists (index = turn - 1).

    Keys: ``in``, ``out``, ``read`` (cache read), ``write`` (cache write,
    5m + 1h TTLs merged), ``cost``. Cost is priced per model via
    claude_turn_usage.turn_cost.

    >>> u = {"input_tokens": 10, "output_tokens": 20,
    ...      "cache_read_input_tokens": 30,
    ...      "cache_creation": {"ephemeral_5m_input_tokens": 40,
    ...                         "ephemeral_1h_input_tokens": 5}}
    >>> p = {"type": "user", "promptSource": "user"}
    >>> def a(mid, usage=u):
    ...     return {"type": "assistant",
    ...             "message": {"id": mid, "model": "claude-opus-4-8", "usage": usage}}
    >>> s = turn_series([p, a("m1"), p, a("m2"), a("m3")])
    >>> s["in"], s["out"], s["read"], s["write"]  # turn 2 sums m2 + m3
    ([10, 20], [20, 40], [30, 60], [45, 90])
    >>> [round(c, 6) for c in s["cost"]]
    [0.000865, 0.00173]
    """
    series: dict[str, list] = {"in": [], "out": [], "read": [], "write": [], "cost": []}
    for seg in split_turns(objs):
        by = ctu._tokens_by_model(seg)
        tok = ctu._merge_tokens(by)
        series["in"].append(tok["in"])
        series["out"].append(tok["out"])
        series["read"].append(tok["read"])
        series["write"].append(tok["w5m"] + tok["w1h"])
        series["cost"].append(sum(ctu.turn_cost(t, m) for m, t in by.items()))
    return series


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.title.set_color(INK2)
    ax.title.set_fontsize(10)


def plot(series: dict[str, list], title: str, output: str, dpi: int = 120,
         linear: bool = False, show: bool = False) -> None:
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    n = len(series["cost"])
    x = range(1, n + 1)
    markevery = max(1, n // 40)
    tok_fmt = FuncFormatter(lambda v, _: ctu.human(int(v)) if v >= 0 else "")
    usd_fmt = FuncFormatter(lambda v, _: f"${v:,.2f}")

    fig, axes = plt.subplots(2, 2, figsize=(12, 7), sharex=True)
    fig.set_facecolor(SURFACE)
    (ax_tok, ax_cost), (ax_ctok, ax_ccost) = axes

    for ax, cumulative in ((ax_tok, False), (ax_ctok, True)):
        for key, label, color, marker in SERIES:
            y = list(itertools.accumulate(series[key])) if cumulative else series[key]
            ax.plot(x, y, color=color, linewidth=2, marker=marker, markersize=4,
                    markevery=markevery, label=label)
        if not linear:
            ax.set_yscale("symlog", linthresh=1000)
            ax.set_ylim(bottom=0)  # keep the symlog axis out of the negative region
        ax.yaxis.set_major_formatter(tok_fmt)
        ax.legend(frameon=False, fontsize=8, labelcolor=INK)
    ax_tok.set_title("Per-turn tokens")
    ax_ctok.set_title("Cumulative tokens (Σ)")

    for ax, cumulative in ((ax_cost, False), (ax_ccost, True)):
        y = list(itertools.accumulate(series["cost"])) if cumulative else series["cost"]
        ax.plot(x, y, color=COST_COLOR, linewidth=2, marker="o", markersize=4,
                markevery=markevery)
        ax.yaxis.set_major_formatter(usd_fmt)
    ax_cost.set_title("Per-turn cost (USD)")
    ax_ccost.set_title("Cumulative cost (USD, Σ)")

    for ax in (ax_ctok, ax_ccost):
        ax.set_xlabel("turn", color=MUTED, fontsize=9)
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    for ax in axes.flat:
        _style(ax)

    tok = {k: sum(series[k]) for k in ("in", "out", "read", "write")}
    fig.suptitle(title, color=INK, fontsize=12)
    fig.text(0.5, 0.925,
             f"{n} turns — Σ {ctu.human(tok['in'])} in, {ctu.human(tok['out'])} out, "
             f"{ctu.human(tok['read'])} cr, {ctu.human(tok['write'])} cw "
             f"(${sum(series['cost']):.2f})",
             ha="center", color=INK2, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(output, dpi=dpi, facecolor=fig.get_facecolor())
    if show:
        plt.show()
    plt.close(fig)


def test_split_turns_no_prompt():
    objs = [{"type": "assistant", "message": {"usage": {"input_tokens": 1}}}]
    assert split_turns(objs) == [objs]  # no genuine prompt: everything is one turn


if __name__ == "__main__":
    raise SystemExit(main())

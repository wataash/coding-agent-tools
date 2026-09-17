#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 Wataru Ashihara <wataash0607@gmail.com>
# SPDX-License-Identifier: Apache-2.0
epilog = r"""
claude_turn_usage.py -h
claude_turn_usage.py ~/.claude/projects/<proj>/<session>.jsonl
echo '{"transcript_path":"session.jsonl"}' | claude_turn_usage.py
pytest -v --doctest-modules ~/src/coding-agent-tools/claude_turn_usage.py  # @pl
"""[1:]

import argparse
import json
import logging
import sys

try:
    from _colorize import get_colors  # type: ignore[import-not-found]  # Python 3.13+ private API
except ImportError:
    class _NoColors:
        RED = YELLOW = BLUE = WHITE = RESET = ""

    def get_colors(colorize=False, *, file=None):  # type: ignore[misc]
        return _NoColors


class MyFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        c = get_colors(file=sys.stderr)
        color = {
            logging.CRITICAL: c.RED,
            logging.ERROR: c.RED,
            logging.WARNING: c.YELLOW,
            logging.INFO: c.BLUE,
            logging.DEBUG: c.WHITE,
        }[record.levelno]
        fn = "" if record.funcName == "<module>" else f" {record.funcName}()"
        fmt = f"{color}[%(levelname)1.1s %(asctime)s %(filename)s:%(lineno)d{fn}] %(message)s{c.RESET}"
        return logging.Formatter(fmt=fmt, datefmt="%T").format(record)


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger_handler = logging.StreamHandler()
logger_handler.setFormatter(MyFormatter())
logger.addHandler(logger_handler)


class ArgumentDefaultsRawTextHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter):
    pass


# Per-million-token USD rates.
#   in:   input; out: output; read: cache read (~0.1x input);
#   w5m:  cache write, 5-minute TTL (1.25x input);
#   w1h:  cache write, 1-hour TTL (2x input).
PRICES = {
    "claude-fable-5":    {"in": 10.0, "out": 50.0, "read": 1.00, "w5m": 12.50, "w1h": 20.0},
    "claude-mythos-5":   {"in": 10.0, "out": 50.0, "read": 1.00, "w5m": 12.50, "w1h": 20.0},
    "claude-opus-5":     {"in": 5.0,  "out": 25.0, "read": 0.50, "w5m": 6.25,  "w1h": 10.0},
    "claude-opus-4-8":   {"in": 5.0,  "out": 25.0, "read": 0.50, "w5m": 6.25,  "w1h": 10.0},
    "claude-opus-4-7":   {"in": 5.0,  "out": 25.0, "read": 0.50, "w5m": 6.25,  "w1h": 10.0},
    "claude-opus-4-6":   {"in": 5.0,  "out": 25.0, "read": 0.50, "w5m": 6.25,  "w1h": 10.0},
    "claude-sonnet-5":   {"in": 2.0,  "out": 10.0, "read": 0.20, "w5m": 2.50,  "w1h": 4.0},  # TODO: $3/$15 from 2026-09-01
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0, "read": 0.30, "w5m": 3.75,  "w1h": 6.0},
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0,  "read": 0.10, "w5m": 1.25,  "w1h": 2.0},
}
DEFAULT_MODEL = "claude-opus-4-8"


def _base_model(model: str) -> str:
    """Model id without a trailing bracketed variant.

    Claude Code may tag a model with a bracketed variant (e.g. the 1M-context
    "claude-fable-5[1m]"); strip it before looking up the rate. Base rate ==
    1M-context rate (no long-context premium since 2026-03-13), so stripping
    prices correctly — see claude_long_context_pricing.md.
    """
    return model.split("[", 1)[0]


def is_priced(model: str) -> bool:
    """Whether ``model`` has its own rate row (vs. falling back to DEFAULT_MODEL).

    >>> is_priced("claude-opus-5"), is_priced("claude-fable-5[1m]")
    (True, True)
    >>> is_priced("claude-fable-6")
    False
    """
    return _base_model(model) in PRICES


def _price(model: str) -> dict[str, float]:
    """Pricing row for a model id, falling back to DEFAULT_MODEL rates.

    The fallback is a guess, not a rate: a model priced above Opus (Fable 5 is
    2x) would be silently under-reported. Callers surface it — see the "?"
    marker in format_by_model.
    """
    return PRICES.get(_base_model(model), PRICES[DEFAULT_MODEL])


def main() -> int:
    parser = argparse.ArgumentParser(formatter_class=ArgumentDefaultsRawTextHelpFormatter, epilog=epilog)
    parser.add_argument("-q", "--quiet", action="count", default=0,
                        help="decrease verbosity; default: debug, -q: info, -qq: warning, -qqq: error")
    parser.add_argument("transcript", nargs="?",
                        help="path to a Claude Code session .jsonl;\n"
                             "if omitted, read {\"transcript_path\": ...} JSON from stdin\n"
                             "(the Claude Code statusLine hook protocol)")
    parser.add_argument("-a", "--all", action="store_true",
                        help="sum the whole session (cumulative) instead of only\n"
                             "the most recent turn; prefixes the line with Σ")
    parser.add_argument("-b", "--both", action="store_true",
                        help="print both the last-turn (⟳) and the cumulative (Σ)\n"
                             "lines from a single transcript read (supersedes --all)")
    parser.add_argument("-m", "--by_model", action="store_true",
                        help="expand into a /usage-style block: a total-cost line\n"
                             "followed by one indented per-model breakdown line")
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
        print("no transcript")
        return 1
    try:
        # errors="replace": the statusLine can race Claude Code mid-write and
        # see a truncated multibyte sequence; the mangled line just fails JSON
        # parsing and is skipped.
        # NOTE: subagent usage is NOT counted -- it is logged to separate
        # transcripts (<dir>/<session-id>/subagents/agent-*.jsonl), so ⟳/Σ
        # run lower than ccusage's session cost when subagents ran.
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError as e:
        logger.error(f"cannot read transcript: {e}")
        print("no transcript")
        return 1

    objs = _parse_lines(lines)
    outs = []
    if args.both or not args.all:
        outs.append((_tokens_by_model(objs[_turn_start(objs):]), "⟳"))
    if args.both or args.all:
        outs.append((_tokens_by_model(objs), "Σ"))
    for by_model, prefix in outs:
        print(format_by_model(by_model, prefix) if args.by_model else format_summary(by_model, prefix))
    return 0


def last_turn_by_model(lines: list[str]) -> dict[str, dict[str, int]]:
    """Per-model token usage over the most recent turn.

    A turn starts at the last genuine user prompt -- a ``user`` JSONL entry
    carrying a ``promptSource`` key (tool-result user entries lack it) -- and
    runs to the end of the transcript. Tool-use loops emit several assistant
    entries per turn, so their usages are summed.

    >>> import json
    >>> u = {"input_tokens": 10, "output_tokens": 20,
    ...      "cache_read_input_tokens": 30,
    ...      "cache_creation": {"ephemeral_5m_input_tokens": 40,
    ...                         "ephemeral_1h_input_tokens": 5}}
    >>> lines = [
    ...     json.dumps({"type": "user", "promptSource": "user"}),
    ...     json.dumps({"type": "assistant",
    ...                 "message": {"model": "claude-opus-4-8", "usage": u}}),
    ... ]
    >>> tok = last_turn_by_model(lines)["claude-opus-4-8"]
    >>> tok["in"], tok["out"], tok["read"], tok["w5m"], tok["w1h"]
    (10, 20, 30, 40, 5)
    """
    objs = _parse_lines(lines)
    return _tokens_by_model(objs[_turn_start(objs):])


def all_by_model(lines: list[str]) -> dict[str, dict[str, int]]:
    """Per-model token usage over the whole session (cumulative)."""
    return _tokens_by_model(_parse_lines(lines))


def _parse_lines(lines: list[str]) -> list[dict]:
    objs = []
    for line in lines:
        try:
            objs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return objs


def _turn_start(objs: list[dict]) -> int:
    start = 0
    for i, o in enumerate(objs):
        if o.get("type") == "user" and "promptSource" in o:
            start = i
    return start


def _tokens_by_model(objs: list[dict]) -> dict[str, dict[str, int]]:
    """Group assistant token usage by model, de-duplicating by message.id.

    Claude Code writes several JSONL lines per streamed assistant message,
    each repeating the message.id and usage; a message is counted once, from
    its last line — safe both when the duplicates repeat the same final usage
    (the observed behavior, matching how ccusage accounts cost) and if they
    were ever partial streaming snapshots.

    >>> u = {"input_tokens": 10, "output_tokens": 20,
    ...      "cache_read_input_tokens": 30,
    ...      "cache_creation": {"ephemeral_5m_input_tokens": 40,
    ...                         "ephemeral_1h_input_tokens": 5}}
    >>> def a(mid, model, usage=u):
    ...     return {"type": "assistant",
    ...             "message": {"id": mid, "model": model, "usage": usage}}
    >>> by = _tokens_by_model([a("m1", "claude-opus-4-8"),
    ...                        a("m1", "claude-opus-4-8", dict(u, input_tokens=12)),
    ...                        a("m2", "claude-haiku-4-5")])
    >>> sorted(by)
    ['claude-haiku-4-5', 'claude-opus-4-8']
    >>> by["claude-opus-4-8"]["in"]  # counted once; the last duplicate wins
    12
    """
    usages: list[tuple[str, dict]] = []  # (model, usage) per counted message
    by_id: dict[str, int] = {}  # message.id -> index into usages
    for o in objs:
        if o.get("type") != "assistant":
            continue
        msg = o.get("message", {})
        u = msg.get("usage")
        if not u:
            continue
        model = msg.get("model", DEFAULT_MODEL)
        if model == "<synthetic>":  # Claude Code's injected non-API messages
            continue
        mid = msg.get("id")
        if mid is None:
            usages.append((model, u))
        elif mid in by_id:
            usages[by_id[mid]] = (model, u)
        else:
            by_id[mid] = len(usages)
            usages.append((model, u))
    by: dict[str, dict[str, int]] = {}
    for model, u in usages:
        tok = by.setdefault(model, {"in": 0, "out": 0, "read": 0, "w5m": 0, "w1h": 0})
        tok["in"] += u.get("input_tokens", 0)
        tok["out"] += u.get("output_tokens", 0)
        tok["read"] += u.get("cache_read_input_tokens", 0)
        cc = u.get("cache_creation", {})
        tok["w5m"] += cc.get("ephemeral_5m_input_tokens", 0)
        tok["w1h"] += cc.get("ephemeral_1h_input_tokens", 0)
    return by


def _merge_tokens(by_model: dict[str, dict[str, int]]) -> dict[str, int]:
    tok = {"in": 0, "out": 0, "read": 0, "w5m": 0, "w1h": 0}
    for t in by_model.values():
        for k in tok:
            tok[k] += t[k]
    return tok


def turn_cost(tok: dict[str, int], model: str) -> float:
    """USD cost of a turn's tokens under ``model`` (falls back to Opus 4.8 rates).

    >>> round(turn_cost({"in": 1_000_000, "out": 0, "read": 0, "w5m": 0, "w1h": 0},
    ...                  "claude-opus-4-8"), 2)
    5.0
    >>> round(turn_cost({"in": 0, "out": 1_000_000, "read": 0, "w5m": 0, "w1h": 0},
    ...                  "claude-sonnet-5"), 2)
    10.0
    >>> round(turn_cost({"in": 1_000_000, "out": 0, "read": 0, "w5m": 0, "w1h": 0},
    ...                  "claude-fable-5[1m]"), 2)
    10.0
    """
    p = _price(model)
    return (
        tok["in"] * p["in"]
        + tok["out"] * p["out"]
        + tok["read"] * p["read"]
        + tok["w5m"] * p["w5m"]
        + tok["w1h"] * p["w1h"]
    ) / 1_000_000


def human(n: int) -> str:
    """Compact token count: 999 -> '999', 12500 -> '12.5k', 5_900_000 -> '5.9m'.

    >>> [human(n) for n in (0, 999, 12500, 552400, 5_900_000)]
    ['0', '999', '12.5k', '552.4k', '5.9m']
    >>> human(999_950)  # the k branch would render this as '1000.0k'
    '1.0m'
    """
    if n >= 1_000_000 or (n >= 1_000 and round(n / 1_000, 1) >= 1000):
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _tok_phrase(tok: dict[str, int]) -> str:
    write = tok["w5m"] + tok["w1h"]
    return (
        f"{human(tok['in'])} in, {human(tok['out'])} out, "
        f"{human(tok['read'])} cr, {human(write)} cw"
    )


def format_summary(by_model: dict[str, dict[str, int]], prefix: str = "⟳") -> str:
    """Compact one-line summary: all models merged, cost priced per model.

    >>> by = {"claude-opus-4-8":  {"in": 1_000_000, "out": 0, "read": 0, "w5m": 0, "w1h": 0},
    ...       "claude-haiku-4-5": {"in": 1_000_000, "out": 0, "read": 0, "w5m": 0, "w1h": 0}}
    >>> format_summary(by, "Σ")
    'Σ 2.0m in, 0 out, 0 cr, 0 cw ($6.00)'
    """
    tok = _merge_tokens(by_model)
    cost = sum(turn_cost(t, m) for m, t in by_model.items())
    return f"{prefix} {_tok_phrase(tok)} (${cost:.2f})"


def format_by_model(by_model: dict[str, dict[str, int]], prefix: str = "⟳") -> str:
    """/usage-style block: a total-cost header + one indented line per model.

    A single-model session collapses to one line (header | breakdown):

    >>> by = {"claude-opus-4-8": {"in": 9800, "out": 33700, "read": 13_100_000,
    ...                           "w5m": 0, "w1h": 412400}}
    >>> print(format_by_model(by, "Σ"))
    Σ $11.57 | claude-opus-4-8: 9.8k in, 33.7k out, 13.1m cr, 412.4k cw ($11.5655)
    >>> by["claude-haiku-4-5"] = {"in": 1000, "out": 0, "read": 0, "w5m": 0, "w1h": 0}
    >>> print(format_by_model(by, "Σ"))
    Σ $11.57
      claude-haiku-4-5: 1.0k in, 0 out, 0 cr, 0 cw ($0.0010)
      claude-opus-4-8: 9.8k in, 33.7k out, 13.1m cr, 412.4k cw ($11.5655)

    A model with no rate row is priced at DEFAULT_MODEL rates and marked "?",
    so a guessed cost never passes for a known one:

    >>> print(format_by_model({"claude-fable-6": {"in": 1_000_000, "out": 0,
    ...                                           "read": 0, "w5m": 0, "w1h": 0}}, "Σ"))
    Σ $5.00 | claude-fable-6?: 1.0m in, 0 out, 0 cr, 0 cw ($5.0000)
    """
    total = sum(turn_cost(t, m) for m, t in by_model.items())
    header = f"{prefix} ${total:.2f}"
    per_model = [f"{m}{'' if is_priced(m) else '?'}: {_tok_phrase(by_model[m])} "
                 f"(${turn_cost(by_model[m], m):.4f})"
                 for m in sorted(by_model)]
    if len(per_model) == 1:
        return f"{header} | {per_model[0]}"
    return "\n".join([header] + [f"  {p}" for p in per_model])


def test_empty_transcript():
    assert last_turn_by_model([]) == {}
    assert format_summary({}) == "⟳ 0 in, 0 out, 0 cr, 0 cw ($0.00)"


def test_last_turn_excludes_previous_turns():
    def a(mid, n):
        return json.dumps({"type": "assistant", "message": {
            "id": mid, "model": "claude-opus-4-8", "usage": {"input_tokens": n}}})
    prompt = json.dumps({"type": "user", "promptSource": "user"})
    tool_result = json.dumps({"type": "user"})
    lines = [prompt, a("m1", 1), tool_result, prompt, a("m2", 10), tool_result]
    assert last_turn_by_model(lines)["claude-opus-4-8"]["in"] == 10
    assert last_turn_by_model([tool_result, a("m3", 7), tool_result])["claude-opus-4-8"]["in"] == 7


def test_synthetic_model_skipped():
    lines = [json.dumps({"type": "assistant", "message": {
        "model": "<synthetic>", "usage": {"input_tokens": 5}}})]
    assert last_turn_by_model(lines) == {}


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 Wataru Ashihara <wataash0607@gmail.com>
# SPDX-License-Identifier: Apache-2.0
epilog = r'''
waitall.py
waitall.py --interval 30 --threshold 0.5 --loadavg-minutes 5
waitall.py --no-codex
waitall.py --ignore-claude --ignore-loadavg

pytest -v --doctest-modules ~/src/coding-agent-tools/waitall.py  # @pl
cd ~/src/coding-agent-tools/ && mypy  # @pl
'''[1:]

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import logging
import os
import re
import select
import subprocess
import sys
import termios
import time
import tty
from typing import Any

import agent_activity


class _NoColors:
    RED = YELLOW = BLUE = WHITE = GREEN = CYAN = GREY = MAGENTA = ''
    BOLD_RED = BOLD_YELLOW = BOLD_GREEN = BOLD_CYAN = RESET = ''


try:
    from _colorize import get_colors  # type: ignore[import-not-found]  # Python 3.13+ private API
except ImportError:
    def get_colors(colorize=False, *, file=None):
        return _NoColors()


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
        fn = '' if record.funcName == '<module>' else f' {record.funcName}()'
        fmt = f'{color}[%(levelname)1.1s %(asctime)s %(filename)s:%(lineno)d{fn}] %(message)s{c.RESET}'
        return logging.Formatter(fmt=fmt, datefmt='%T').format(record)


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger_handler = logging.StreamHandler()
logger_handler.setFormatter(MyFormatter())
logger.addHandler(logger_handler)


class ArgumentDefaultsRawTextHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(formatter_class=ArgumentDefaultsRawTextHelpFormatter, epilog=epilog)
    parser.add_argument('-q', '--quiet', action='count', default=0,
                        help='decrease verbosity; default: debug, -q: info, -qq: warning, -qqq: error')
    parser.add_argument('-n', '--dry_run', action='store_true',
                        help='print commands used to inspect sessions, then exit')
    parser.add_argument('--interval', type=float, default=60.0, help='seconds between checks')
    parser.add_argument('--threshold', type=float, default=1.0,
                        help='load average must be below this value')
    parser.add_argument('--loadavg-minutes', type=int, choices=(1, 5, 15), default=1,
                        help='which load average to compare against --threshold (1/5/15 min)')
    parser.add_argument('--command-timeout', type=float, default=10.0,
                        help='timeout in seconds for each session-list command')
    parser.add_argument('--color', choices=('auto', 'always', 'never'), default='auto',
                        help='colorize monitoring output on stdout')
    parser.add_argument('--codex', action=argparse.BooleanOptionalAction, default=True,
                        help='wait for Codex running sessions (--no-codex disables it)')
    parser.add_argument('--claude', action=argparse.BooleanOptionalAction, default=True,
                        help='wait for Claude Code running sessions (--no-claude disables it)')
    parser.add_argument('--loadavg', action=argparse.BooleanOptionalAction, default=True,
                        help='wait for low load average (--no-loadavg disables it)')
    parser.add_argument('--ignore-codex', dest='codex', action='store_false', help=argparse.SUPPRESS)
    parser.add_argument('--ignore-claude', dest='claude', action='store_false', help=argparse.SUPPRESS)
    parser.add_argument('--ignore-loadavg', dest='loadavg', action='store_false', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.interval < 0:
        parser.error(f'--interval must be >= 0: {args.interval}')
    if args.command_timeout <= 0:
        parser.error(f'--command-timeout must be > 0: {args.command_timeout}')
    logger.setLevel({0: logging.DEBUG, 1: logging.INFO, 2: logging.WARNING}.get(args.quiet, logging.ERROR))
    agent_activity.logger.handlers = [logger_handler]
    agent_activity.logger.setLevel(logger.level)
    agent_activity.logger.propagate = False
    logger.debug(f'{args=}')

    if args.dry_run:
        print_dry_run_commands(args)
        return 0
    try:
        return waitall(args)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as e:
        logger.error(str(e))
        return 1


def print_dry_run_commands(args: argparse.Namespace) -> None:
    for command in agent_activity.inspection_commands(codex=args.codex, claude=args.claude):
        print(command)


def read_loadavg() -> tuple[float, float, float]:
    with open('/proc/loadavg') as f:
        load1, load5, load15 = (float(x) for x in f.read().split()[:3])
    return load1, load5, load15


def compact(value: Any, limit: int = 100) -> str:
    text = ''.join(character if character.isprintable() else ' ' for character in str(value))
    return text if len(text) <= limit else text[:limit - 1] + '…'


def stdout_colors(mode: str) -> Any:
    if mode == 'never':
        return _NoColors()
    return get_colors(colorize=mode == 'always', file=sys.stdout)


def highlight_assignment(c: Any, key: str, value: Any, value_color: str) -> str:
    return f'{c.CYAN}{key}{c.RESET}{c.WHITE}={c.RESET}{value_color}{value}{c.RESET}'


def highlight_loadavg(c: Any, args: argparse.Namespace, loads: tuple[float, float, float]) -> str:
    separator = f'{c.WHITE}/{c.RESET}'
    values = separator.join(f'{c.YELLOW}{load:.2f}{c.RESET}' for load in loads)
    condition = (
        f'{c.WHITE}({c.RESET}'
        f'{c.YELLOW}{args.loadavg_minutes}{c.RESET}{c.MAGENTA}m{c.RESET}'
        f'{c.WHITE} < {c.RESET}{c.YELLOW}{args.threshold:g}{c.RESET}'
        f'{c.WHITE}){c.RESET}'
    )
    return f'{c.CYAN}loadavg{c.RESET}{c.WHITE}={c.RESET}{values} {condition}'


def highlight_codex_entry(c: Any, entry: dict[str, Any]) -> str:
    fields = [
        f'  {c.MAGENTA}codex{c.RESET}',
        highlight_assignment(c, 'id', entry.get('id', '?'), c.GREEN),
    ]
    if 'pid' in entry:
        fields.append(highlight_assignment(c, 'pid', entry['pid'], c.YELLOW))
    fields.extend([
        highlight_assignment(c, 'cwd', compact(entry.get('cwd', '?')), c.GREEN),
        highlight_assignment(c, 'title', compact(entry.get('name') or entry.get('preview') or '?'), c.GREEN),
    ])
    return ' '.join(fields)


def highlight_claude_entry(c: Any, entry: dict[str, Any]) -> str:
    session_id = entry.get('id') or entry.get('sessionId') or '?'
    activity = entry.get('state') or entry.get('status') or 'unknown'
    return ' '.join([
        f'  {c.MAGENTA}claude{c.RESET}',
        highlight_assignment(c, 'id', session_id, c.GREEN),
        highlight_assignment(c, 'kind', entry.get('kind', '?'), c.MAGENTA),
        highlight_assignment(c, 'activity', activity, c.MAGENTA),
        highlight_assignment(c, 'cwd', compact(entry.get('cwd', '?')), c.GREEN),
        highlight_assignment(c, 'name', compact(entry.get('name', '?')), c.GREEN),
    ])


def print_sample(args: argparse.Namespace, codex_running: list[dict[str, Any]],
                 claude_running: list[dict[str, Any]], loads: tuple[float, float, float] | None) -> None:
    c = stdout_colors(args.color)
    parts = [f'{c.GREY}{time.strftime("%F %T")}{c.RESET}']
    if args.codex:
        parts.append(highlight_assignment(c, 'codex_running', len(codex_running), c.YELLOW))
    else:
        parts.append(highlight_assignment(c, 'codex', 'off', c.MAGENTA))
    if args.claude:
        parts.append(highlight_assignment(c, 'claude_running', len(claude_running), c.YELLOW))
    else:
        parts.append(highlight_assignment(c, 'claude', 'off', c.MAGENTA))
    if args.loadavg:
        assert loads is not None
        parts.append(highlight_loadavg(c, args, loads))
    else:
        parts.append(highlight_assignment(c, 'loadavg', 'off', c.MAGENTA))
    print(' '.join(parts), flush=True)
    for entry in codex_running:
        print(highlight_codex_entry(c, entry), flush=True)
    for entry in claude_running:
        print(highlight_claude_entry(c, entry), flush=True)


@contextmanager
def cbreak_stdin() -> Iterator[bool]:
    if not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def wait_for_key(timeout: float, keyboard_enabled: bool) -> bool:
    if not keyboard_enabled:
        time.sleep(timeout)
        return False
    readable, _, _ = select.select([sys.stdin], [], [], timeout)
    if not readable:
        return False
    os.read(sys.stdin.fileno(), 4096)
    return True


def waitall(args: argparse.Namespace) -> int:
    loadavg_index = {1: 0, 5: 1, 15: 2}[args.loadavg_minutes]
    c = stdout_colors(args.color)
    print(f'{c.GREY}waiting; press any key to check and print running entries now{c.RESET}', flush=True)
    with cbreak_stdin() as keyboard_enabled:
        while True:
            codex_running = agent_activity.read_codex_running(args.command_timeout) if args.codex else []
            claude_running = agent_activity.read_claude_running(args.command_timeout) if args.claude else []
            loads = read_loadavg() if args.loadavg else None
            print_sample(args, codex_running, claude_running, loads)

            loadavg_done = not args.loadavg or (loads is not None and loads[loadavg_index] < args.threshold)
            if not codex_running and not claude_running and loadavg_done:
                print(f'{c.GREY}all enabled conditions satisfied{c.RESET}', flush=True)
                return 0
            if wait_for_key(args.interval, keyboard_enabled):
                logger.debug('key pressed; checking now')


# ------------------------------------------------------------------------------
# tests (pytest)

def test_waitall(monkeypatch, capsys):
    mod = sys.modules[__name__]
    codex_samples = iter([[{'id': 'c1', 'status': {'type': 'active'}}], []])
    claude_samples = iter([[{'id': 'a1', 'state': 'working'}], []])
    load_samples = iter([(2.0, 2.0, 2.0), (0.5, 1.0, 1.0)])
    waits = []
    monkeypatch.setattr(agent_activity, 'read_codex_running', lambda timeout: next(codex_samples))
    monkeypatch.setattr(agent_activity, 'read_claude_running', lambda timeout: next(claude_samples))
    monkeypatch.setattr(mod, 'read_loadavg', lambda: next(load_samples))
    def fake_wait_for_key(timeout, enabled):
        waits.append((timeout, enabled))
        return False

    @contextmanager
    def fake_cbreak_stdin():
        yield False

    monkeypatch.setattr(mod, 'wait_for_key', fake_wait_for_key)
    monkeypatch.setattr(mod, 'cbreak_stdin', fake_cbreak_stdin)
    args = argparse.Namespace(codex=True, claude=True, loadavg=True, color='auto', command_timeout=10.0,
                              interval=30.0, threshold=1.0, loadavg_minutes=1)
    assert waitall(args) == 0
    assert waits == [(30.0, False)]
    out = capsys.readouterr().out
    assert 'codex_running=1 claude_running=1 loadavg=2.00/2.00/2.00' in out
    assert 'codex_running=0 claude_running=0 loadavg=0.50/1.00/1.00' in out
    assert out.rstrip().endswith('all enabled conditions satisfied')


def test_syntax_colors_are_value_based(capsys):
    args = argparse.Namespace(codex=True, claude=True, loadavg=True, color='always',
                              threshold=1.0, loadavg_minutes=1)
    print_sample(args, [{'id': 'c'}], [], (2.0, 1.0, 0.5))
    out = capsys.readouterr().out
    assert '\033[' in out
    plain = re.sub(r'\033\[[0-9;]*m', '', out)
    assert 'loadavg=2.00/1.00/0.50' in plain
    c = get_colors(colorize=True)
    assert f'{c.CYAN}codex_running{c.RESET}{c.WHITE}={c.RESET}{c.YELLOW}1{c.RESET}' in out
    assert f'{c.CYAN}claude_running{c.RESET}{c.WHITE}={c.RESET}{c.YELLOW}0{c.RESET}' in out


if __name__ == '__main__':
    raise SystemExit(main())

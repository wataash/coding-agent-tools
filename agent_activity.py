#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 Wataru Ashihara <wataash0607@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Read running Codex and Claude Code sessions without starting agent turns."""

import argparse
from collections.abc import Iterator, Sequence
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

CODEX_COMMAND = ('codex', 'app-server', 'proxy')
CLAUDE_COMMAND = ('claude', 'agents', '--json')


def codex_request_input() -> str:
    messages = [
        {
            'id': 1,
            'method': 'initialize',
            'params': {'clientInfo': {'name': 'coding-agent-tools', 'title': 'Coding agent tools', 'version': '1'}},
        },
        {'method': 'initialized', 'params': {}},
        {
            'id': 2,
            'method': 'thread/list',
            'params': {'archived': False, 'limit': 1000, 'useStateDbOnly': True},
        },
    ]
    return ''.join(json.dumps(message, separators=(',', ':')) + '\n' for message in messages)


def command_text(command: Sequence[str], *, stdin_text: str | None = None) -> str:
    command_str = shlex.join(command)
    if stdin_text is None:
        return command_str
    lines = stdin_text.splitlines()
    return f'{shlex.join(["printf", "%s\\n", *lines])} | {command_str}'


def run_command(command: Sequence[str], *, timeout: float, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    display = command_text(command, stdin_text=stdin_text)
    logger.debug(f'running command: {display}')
    try:
        return subprocess.run(command, input=stdin_text, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise RuntimeError(f'command not found: {command[0]}') from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'command timed out after {timeout:g}s: {display}') from e


def inspection_commands(*, codex: bool = True, claude: bool = True) -> list[str]:
    commands = []
    if codex:
        commands.append(command_text(CODEX_COMMAND, stdin_text=codex_request_input()))
    if claude:
        commands.append(command_text(CLAUDE_COMMAND))
    return commands

def parse_json_lines(text: str, *, response_id: int) -> dict[str, Any]:
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get('id') == response_id:
            if 'error' in value:
                raise RuntimeError(f'Codex app-server error: {value["error"]}')
            result = value.get('result')
            if not isinstance(result, dict):
                raise ValueError(f'invalid Codex app-server response: {line}')
            return result
    raise ValueError('Codex app-server did not return a thread/list response')


def is_missing_codex_daemon(completed: subprocess.CompletedProcess[str]) -> bool:
    error = completed.stderr.lower()
    return completed.returncode != 0 and 'failed to connect to socket' in error and (
        'no such file or directory' in error or 'connection refused' in error
    )


def read_codex_daemon_sessions(command_timeout: float) -> list[dict[str, Any]]:
    stdin_text = codex_request_input()
    completed = run_command(CODEX_COMMAND, timeout=command_timeout, stdin_text=stdin_text)
    if is_missing_codex_daemon(completed):
        return []
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f'exit {completed.returncode}'
        raise RuntimeError(f'{shlex.join(CODEX_COMMAND)} failed: {detail}')
    result = parse_json_lines(completed.stdout, response_id=2)
    entries = result.get('data')
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError('Codex thread/list result.data is not an array of objects')
    return entries


def codex_session_is_running(entry: dict[str, Any]) -> bool:
    status = entry.get('status')
    if not isinstance(status, dict):
        raise ValueError(f'Codex session has invalid status: {entry!r}')
    if status.get('type') != 'active':
        return False
    flags = status.get('activeFlags', [])
    if not isinstance(flags, list):
        raise ValueError(f'Codex session has invalid activeFlags: {entry!r}')
    return not ({'waitingOnApproval', 'waitingOnUserInput'} & set(flags))


def codex_home() -> Path:
    configured = os.environ.get('CODEX_HOME')
    return Path(configured).expanduser() if configured else Path.home() / '.codex'


def codex_process_pid(process_uuid: str) -> int | None:
    match = re.fullmatch(r'pid:(\d+):.+', process_uuid)
    return int(match.group(1)) if match else None


def codex_process_is_live(pid: int, proc_root: Path = Path('/proc')) -> bool:
    try:
        command = (proc_root / str(pid) / 'cmdline').read_bytes().split(b'\0', 1)[0]
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False
    return Path(os.fsdecode(command)).name == 'codex'


def open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    return connection


def reversed_jsonl_lines(path: Path) -> Iterator[bytes]:
    """Read complete records newest first, ignoring a concurrent partial append."""
    with path.open('rb') as stream:
        position = stream.seek(0, os.SEEK_END)
        remainder = b''
        skip_partial = True
        while position:
            size = min(position, 65536)
            position -= size
            stream.seek(position)
            lines = (stream.read(size) + remainder).split(b'\n')
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if skip_partial:
                    skip_partial = False
                    continue
                if line:
                    yield line
        if remainder and not skip_partial:
            yield remainder


def codex_rollout_is_running(path: Path) -> bool:
    for line in reversed_jsonl_lines(path):
        event = json.loads(line)
        if event.get('type') != 'event_msg':
            continue
        kind = event.get('payload', {}).get('type')
        if kind in ('task_started', 'task_complete', 'turn_aborted'):
            return kind == 'task_started'
    return False


def codex_thread_metadata(state_db: Path, thread_id: str | None) -> dict[str, Any]:
    if thread_id is None or not state_db.exists():
        return {}
    with open_sqlite_read_only(state_db) as connection:
        row = connection.execute(
            'SELECT id, cwd, title, preview, rollout_path FROM threads WHERE id = ?',
            (thread_id,),
        ).fetchone()
    return dict(row) if row is not None else {}


def read_codex_log_running(*, logs_db: Path | None = None, state_db: Path | None = None,
                           proc_root: Path = Path('/proc')) -> list[dict[str, Any]]:
    home = codex_home()
    logs_db = logs_db or home / 'logs_2.sqlite'
    state_db = state_db or home / 'state_5.sqlite'
    if not logs_db.exists():
        return []
    try:
        with open_sqlite_read_only(logs_db) as connection:
            rows = connection.execute(
                '''
                SELECT process_uuid, thread_id, MAX(ts) AS ts
                FROM logs
                WHERE thread_id IS NOT NULL AND process_uuid IS NOT NULL
                GROUP BY process_uuid, thread_id
                ORDER BY ts
                ''',
            ).fetchall()
            entries = []
            for row in rows:
                process_uuid = str(row['process_uuid'])
                pid = codex_process_pid(process_uuid)
                if pid is None or not codex_process_is_live(pid, proc_root):
                    continue
                thread_id = str(row['thread_id'])
                metadata = codex_thread_metadata(state_db, thread_id)
                if not metadata:
                    continue
                if not codex_rollout_is_running(Path(metadata['rollout_path'])):
                    continue
                entries.append({
                    'id': metadata.get('id') or thread_id or process_uuid,
                    'pid': pid,
                    'processUuid': process_uuid,
                    'cwd': metadata.get('cwd', '?'),
                    'name': metadata.get('title') or metadata.get('preview') or 'Codex turn',
                    'status': {'type': 'active', 'activeFlags': []},
                    'detection': 'rollout',
                })
            return entries
    except sqlite3.Error as e:
        raise RuntimeError(f'failed to read Codex activity database {logs_db}: {e}') from e


def read_codex_running(command_timeout: float) -> list[dict[str, Any]]:
    entries = read_codex_log_running()
    by_id = {str(entry['id']): entry for entry in entries}
    control_socket = codex_home() / 'app-server-control' / 'app-server-control.sock'
    if control_socket.exists():
        daemon_entries = [
            entry for entry in read_codex_daemon_sessions(command_timeout)
            if codex_session_is_running(entry)
        ]
        by_id.update({str(entry.get('id')): entry for entry in daemon_entries})
    return list(by_id.values())


def read_claude_sessions(command_timeout: float) -> list[dict[str, Any]]:
    completed = run_command(CLAUDE_COMMAND, timeout=command_timeout)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f'exit {completed.returncode}'
        raise RuntimeError(f'{shlex.join(CLAUDE_COMMAND)} failed: {detail}')
    try:
        entries = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f'invalid JSON from {shlex.join(CLAUDE_COMMAND)}: {e}') from e
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError('Claude agents output is not an array of objects')
    return entries


def claude_session_is_running(entry: dict[str, Any]) -> bool:
    state = entry.get('state')
    if state is not None:
        return state == 'working'
    status = entry.get('status')
    if status is not None:
        return status == 'busy'
    # Older Claude Code versions list only live processes and omit their activity status.
    return True


def read_claude_running(command_timeout: float) -> list[dict[str, Any]]:
    return [entry for entry in read_claude_sessions(command_timeout) if claude_session_is_running(entry)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--claude', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--command-timeout', type=float, default=10.0)
    parser.add_argument('-n', '--dry_run', action='store_true',
                        help='print inspection commands without reading sessions')
    parser.add_argument('-q', '--quiet', action='count', default=0,
                        help='decrease verbosity: debug, info, warning, error')
    args = parser.parse_args()
    if args.command_timeout <= 0:
        parser.error('--command-timeout must be > 0')
    logging.basicConfig(level={0: logging.DEBUG, 1: logging.INFO, 2: logging.WARNING}.get(args.quiet, logging.ERROR),
                        format='[%(levelname)s] %(message)s')
    if args.dry_run:
        for command in inspection_commands(codex=args.codex, claude=args.claude):
            print(command)
        return 0
    try:
        result = {
            'codex': read_codex_running(args.command_timeout) if args.codex else [],
            'claude': read_claude_running(args.command_timeout) if args.claude else [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as e:
        logger.error(str(e))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

import json
import sqlite3

import agent_activity as activity


def test_codex_session_is_running():
    assert activity.codex_session_is_running({'status': {'type': 'active', 'activeFlags': []}})
    assert not activity.codex_session_is_running(
        {'status': {'type': 'active', 'activeFlags': ['waitingOnUserInput']}})
    assert not activity.codex_session_is_running({'status': {'type': 'idle'}})



def test_claude_session_is_running():
    assert activity.claude_session_is_running({'state': 'working', 'status': 'idle'})
    assert not activity.claude_session_is_running({'state': 'blocked', 'status': 'busy'})
    assert activity.claude_session_is_running({'status': 'busy'})
    assert not activity.claude_session_is_running({'status': 'waiting'})
    assert activity.claude_session_is_running({})  # old CLI: an entry itself means a live process



def test_parse_json_lines():
    text = ('{"id":1,"result":{}}\n'
            '{"method":"thread/status/changed","params":{}}\n'
            '{"id":2,"result":{"data":[{"id":"abc"}]}}\n')
    assert activity.parse_json_lines(text, response_id=2) == {'data': [{'id': 'abc'}]}



def test_read_codex_log_running(tmp_path):
    logs_db = tmp_path / 'logs.sqlite'
    state_db = tmp_path / 'state.sqlite'
    proc_root = tmp_path / 'proc'
    # The start log has been pruned; a different thread in the same process
    # completed later. Neither should hide the running thread.
    with sqlite3.connect(logs_db) as connection:
        connection.execute(
            'CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, '
            'target TEXT, feedback_log_body TEXT, thread_id TEXT, process_uuid TEXT)',
        )
        connection.executemany('INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?)', [
            (1, 100, 'codex_core', 'tool progress', 'running', 'pid:101:live'),
            (2, 101, 'codex_core', 'tool progress', 'idle', 'pid:101:live'),
            (3, 102, 'codex_app_server::outgoing_message',
             'app-server event: turn/completed targeted_connections=1', None, 'pid:101:live'),
            (4, 103, 'codex_core', 'tool progress', 'aborted', 'pid:101:live'),
            (5, 104, 'codex_core', 'tool progress', 'dead', 'pid:102:dead'),
        ])
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            'CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, '
            'preview TEXT, rollout_path TEXT)',
        )
        for thread_id, events in {
            'running': ['task_started', 'item_completed'],
            'idle': ['task_started', 'task_complete'],
            'aborted': ['task_started', 'turn_aborted'],
            'dead': ['task_started'],
        }.items():
            rollout = tmp_path / f'{thread_id}.jsonl'
            rollout.write_text(''.join(
                json.dumps({'type': 'event_msg', 'payload': {'type': event}}) + '\n'
                for event in events
            ))
            connection.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?)',
                               (thread_id, '/work', thread_id, 'preview', str(rollout)))
    process_dir = proc_root / '101'
    process_dir.mkdir(parents=True)
    (process_dir / 'cmdline').write_bytes(b'/usr/bin/codex\0')
    assert activity.read_codex_log_running(
        logs_db=logs_db, state_db=state_db, proc_root=proc_root,
    ) == [{
        'id': 'running',
        'pid': 101,
        'processUuid': 'pid:101:live',
        'cwd': '/work',
        'name': 'running',
        'status': {'type': 'active', 'activeFlags': []},
        'detection': 'rollout',
    }]



def test_codex_rollout_is_running(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    started = json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started'}}) + '\n'
    completed = json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n'
    # Cross read-block boundaries and ignore an unfinished JSON record.
    progress = json.dumps({'type': 'response_item', 'payload': 'x' * 140000}) + '\n'
    path.write_text(started + progress + '{"type":')
    assert activity.codex_rollout_is_running(path)
    path.write_text(started + progress + completed)
    assert not activity.codex_rollout_is_running(path)
    path.write_text(started + completed + started)
    assert activity.codex_rollout_is_running(path)
    path.write_text('')
    assert not activity.codex_rollout_is_running(path)



def test_cli_json_and_provider_selection(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['agent_activity.py', '--no-claude'])
    monkeypatch.setattr(activity, 'read_codex_running', lambda timeout: [{'id': 'running'}])
    def unexpected(timeout):
        raise AssertionError('disabled provider was queried')
    monkeypatch.setattr(activity, 'read_claude_running', unexpected)
    assert activity.main() == 0
    assert json.loads(capsys.readouterr().out) == {'codex': [{'id': 'running'}], 'claude': []}


def test_cli_dry_run_and_failure(monkeypatch, capsys):
    def failed(timeout):
        raise RuntimeError('inspection failed')
    monkeypatch.setattr(activity, 'read_codex_running', failed)
    monkeypatch.setattr('sys.argv', ['agent_activity.py', '-n', '--no-claude'])
    assert activity.main() == 0
    assert 'codex app-server proxy' in capsys.readouterr().out
    monkeypatch.setattr('sys.argv', ['agent_activity.py', '--no-claude'])
    assert activity.main() == 1
    assert capsys.readouterr().out == ''

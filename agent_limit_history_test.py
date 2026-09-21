#!/usr/bin/env python3
"""Run: python3 -m unittest discover -s ./ -p agent_limit_history_test.py"""
import argparse
import contextlib
import io
import json
from pathlib import Path
import shlex
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent_limit_history as history


class LimitHistoryTest(unittest.TestCase):
    def test_custom_collector_cli_with_spaces_in_path(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "custom collector.py"
            script.write_text('print(\'{"observed_at":123,"windows":[["five_hour",300,42,456]]}\')\n')
            db = Path(temp) / "history.sqlite3"
            command = shlex.join([history.sys.executable, str(script)])
            with patch.object(history.sys, "argv", ["agent_limit_history.py", "collect",
                              "--providers", "claude", "--db", str(db), "--claude-command", command]):
                self.assertEqual(history.main(), 0)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT provider, observed_at, error FROM observations").fetchall(),
                                 [("claude", 123, None)])
                self.assertEqual(conn.execute("SELECT bucket, used_percent FROM windows").fetchall(),
                                 [("five_hour", 42)])

    def test_codex_durations_and_buckets(self):
        window = {"windowDurationMins": 10080, "usedPercent": 68, "resetsAt": 1800000000}
        payload = {"rateLimits": {"primary": window}, "rateLimitsByLimitId": {
            "codex": {"primary": window, "secondary": None},
            "other": {"primary": {**window, "windowDurationMins": 300, "usedPercent": 0}}}}
        self.assertEqual(history.normalize("codex", payload), [
            ("codex", 10080, 68, 1800000000), ("other", 300, 0, 1800000000)])

    def test_claude_timezone_and_unavailable(self):
        rows = history.normalize("claude", {"five_hour": None,
            "seven_day": {"utilization": 0, "resets_at": "1970-01-01T09:00:00+09:00"},
            "seven_day_model": {"utilization": 30, "resets_at": None},
            "extra_usage": {"is_enabled": False}})
        self.assertEqual(rows, [("seven_day", 10080, 0, 0), ("seven_day_model", 10080, 30, None)])
        with self.assertRaises(ValueError):
            history.normalize("claude", {"five_hour": {"utilization": float("nan")}})

    def test_claude_weekly_breakdown_is_not_a_window(self):
        payload = {"five_hour": {"utilization": 12},
                   "seven_day": {"utilization": 34},
                   "seven_day_sonnet": {"utilization": 56},
                   "seven_day_breakdown": {"as_of": "example", "rows": [], "window_started_at": "example"}}
        self.assertEqual(history.normalize("claude", payload), [
            ("five_hour", 300, 12, None), ("seven_day", 10080, 34, None),
            ("seven_day_sonnet", 10080, 56, None)])
        with self.assertRaises(ValueError):
            history.normalize("claude", {"seven_day_breakdown": payload["seven_day_breakdown"]})
        with self.assertRaises(KeyError):
            history.normalize("claude", {"seven_day": {}})

    def test_failure_is_recorded_and_next_provider_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "history.sqlite3"
            args = argparse.Namespace(db=db, providers=["claude", "codex"], dry_run=False)
            failure = argparse.Namespace(stdout=b'{"error":"HTTP 401"}', returncode=1)
            with patch.object(history.subprocess, "run", return_value=failure), patch.object(
                history, "codex_snapshot", return_value={"observed_at": 123, "windows": [("codex", 10080, 50, 456)]}):
                self.assertEqual(history.collect(args), 1)
                self.assertEqual(history.collect(args), 1)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT provider, error FROM observations ORDER BY id").fetchall(),
                                 [("claude", "HTTP 401"), ("codex", None)] * 2)
                self.assertEqual(conn.execute("SELECT count(*) FROM windows").fetchone()[0], 2)
            output = Path(temp) / "snapshot.sqlite3"
            history.snapshot(argparse.Namespace(db=db, output=output, dry_run=False))
            with contextlib.closing(sqlite3.connect(output)) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM observations").fetchone()[0], 4)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_invalid_collector_results_are_recorded_and_do_not_stop_next_provider(self):
        invalid_results = [
            ({"observed_at": 123, "windows": [["five_hour", 300, None, 456]]}, "ValueError"),
            ({"observed_at": None, "windows": [["five_hour", 300, 42, 456]]}, "ValueError"),
            ({"observed_at": float("nan"), "windows": [["five_hour", 300, 42, 456]]}, "ValueError"),
            ({"observed_at": 123, "windows": [["five_hour", 300, 42]]}, "ValueError"),
            ({"observed_at": 123, "windows": [["five_hour", 300, 42, 456], ["seven_day", 10080, None, 456]]}, "ValueError"),
            ({"windows": [["five_hour", 300, 42, 456]]}, "KeyError"),
            ({"observed_at": 123}, "KeyError"),
            ({"observed_at": 123, "windows": [["five_hour", 300, 42, 456],
                                                  ["five_hour", 300, 43, 456]]}, "ValueError"),
        ]
        for result, error in invalid_results:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as temp:
                db = Path(temp) / "history.sqlite3"
                args = argparse.Namespace(db=db, providers=["claude", "codex"], dry_run=False)
                response = argparse.Namespace(stdout=json.dumps(result).encode(), returncode=0)
                with patch.object(history.subprocess, "run", return_value=response), patch.object(
                    history, "codex_snapshot", return_value={"observed_at": 124, "windows": [("codex", 10080, 50, 456)]}):
                    self.assertEqual(history.collect(args), 1)
                with contextlib.closing(sqlite3.connect(db)) as conn:
                    self.assertEqual(conn.execute("SELECT provider, error FROM observations ORDER BY id").fetchall(),
                                     [("claude", error), ("codex", None)])
                    self.assertEqual(conn.execute("SELECT bucket, used_percent FROM windows").fetchall(),
                                     [("codex", 50)])

    def test_save_failure_rolls_back_and_next_provider_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "history.sqlite3"
            args = argparse.Namespace(db=db, providers=["claude", "codex"], dry_run=False)
            original_open_database = history.open_database

            def open_database_with_rejecting_trigger(path):
                conn = original_open_database(path)
                conn.execute("CREATE TRIGGER reject_bad_window BEFORE INSERT ON windows WHEN NEW.bucket = 'bad' BEGIN SELECT RAISE(ABORT, 'rejected'); END")
                return conn

            response = argparse.Namespace(stdout=b'{"observed_at":123,"windows":[["good",300,42,456],["bad",10080,43,456]]}', returncode=0)
            with patch.object(history, "open_database", side_effect=open_database_with_rejecting_trigger), patch.object(
                history.subprocess, "run", return_value=response), patch.object(
                history, "codex_snapshot", return_value={"observed_at": 124, "windows": [("codex", 10080, 50, 456)]}):
                self.assertEqual(history.collect(args), 1)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT provider, error FROM observations ORDER BY id").fetchall(),
                                 [("claude", "IntegrityError"), ("codex", None)])
                self.assertEqual(conn.execute("SELECT bucket, used_percent FROM windows").fetchall(),
                                 [("codex", 50)])

    def test_codex_rpc_handshake(self):
        # A small peer asserts there is no thread/turn/inference request.
        peer = """
import json, sys
first = json.loads(sys.stdin.readline())
assert first['method'] == 'initialize'
print(json.dumps({'id': 1, 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
assert json.loads(sys.stdin.readline())['method'] == 'account/rateLimits/read'
print(json.dumps({'id': 2, 'result': {'rateLimits': {'primary': {
    'windowDurationMins': 10080, 'usedPercent': 5, 'resetsAt': 123}}}}), flush=True)
sys.stdin.read()
"""
        result = history.codex_snapshot([history.sys.executable, "-c", peer])
        self.assertEqual(result["windows"], [("codex", 10080, 5, 123)])

    def test_dry_run_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "absent" / "db"
            with contextlib.redirect_stdout(io.StringIO()):
                history.collect(argparse.Namespace(db=db, providers=["codex"], dry_run=True))
            self.assertFalse(db.parent.exists())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Wataru Ashihara <wataash0607@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Subscription limit observations; see README.md."""
import argparse
from contextlib import closing
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import selectors
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

epilog = "agent_limit_history.py collect -h\nagent_limit_history.py plot -h"
logger = logging.getLogger(__name__)
logger_handler = logging.StreamHandler()
logger_handler.setFormatter(logging.Formatter("[%(levelname).1s %(asctime)s] %(message)s", datefmt="%T"))
logger.addHandler(logger_handler)


class ArgumentDefaultsRawTextHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter):
    pass


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("invalid numeric field")
    return value


def normalize(provider, payload):
    """Keep unavailable windows absent, and derive Codex windows from duration."""
    rows = []
    if provider == "claude":
        for key, value in payload.items():
            # Weekly breakdown metadata is not a utilization window.
            if key == "seven_day_breakdown":
                continue
            if not (key == "five_hour" or key.startswith("seven_day")) or value is None:
                continue
            used = number(value["utilization"])
            reset = value.get("resets_at")
            if reset is not None:
                parsed = dt.datetime.fromisoformat(reset.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("reset timestamp lacks timezone")
                reset = parsed.timestamp()
            rows.append((key, 300 if key == "five_hour" else 10080, used, reset))
    else:
        limits = payload.get("rateLimitsByLimitId")
        if not limits:
            snap = payload["rateLimits"]
            limits = {snap.get("limitId") or "codex": snap}
        for key, snap in limits.items():
            if not snap:
                continue
            for slot in ("primary", "secondary"):
                value = snap.get(slot)
                if value is None:
                    continue
                minutes = number(value["windowDurationMins"])
                if minutes <= 0 or int(minutes) != minutes:
                    raise ValueError("invalid window duration")
                reset = value.get("resetsAt")
                if reset is not None:
                    reset = number(reset)
                rows.append((key, int(minutes), number(value["usedPercent"]), reset))
    if not rows:
        raise ValueError("no supported limit windows in response")
    return rows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_claude(args):
    # Never print tokens, request headers, response bodies, or credential contents.
    try:
        credentials = json.loads(Path.home().joinpath(".claude/.credentials.json").read_text())
        token = credentials["claudeAiOauth"]["accessToken"]
        request = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "agent-limit-history/1.0",
            "Accept": "application/json",
        })
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            payload = json.load(response)
        rows = normalize("claude", payload)
        print(json.dumps({"observed_at": time.time(), "windows": rows}))
        return 0
    except urllib.error.HTTPError as exc:
        print(json.dumps({"error": "HTTP " + str(exc.code)}))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}))
    return 1


def collector_command(provider, args):
    override = getattr(args, provider + "_command", None)
    if override is not None:
        command = shlex.split(override)
        if not command:
            raise ValueError("collector command must not be empty")
        return command
    if provider == "claude":
        return [sys.executable, str(Path(__file__).resolve()), "_fetch-claude"]
    return ["codex", "app-server", "--listen", "stdio://"]


def codex_snapshot(cmd):
    logger.debug("%s", shlex.join(cmd))
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        pending = b""
        deadline = time.monotonic() + 45

        def send(message):
            proc.stdin.write(json.dumps(message).encode() + b"\n")
            proc.stdin.flush()

        def receive(request_id):
            nonlocal pending
            while True:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == request_id:
                        if "error" in message:
                            # RPC error messages can contain account details; store only code.
                            raise RuntimeError("RPC " + str(message["error"].get("code")))
                        return message["result"]
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Codex rate limit read timed out")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex app-server exited without a response")
                pending += chunk
                if len(pending) > 4_000_000:
                    raise ValueError("oversized protocol response")

        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "agent_limit_history", "version": "1.0"}}})
            receive(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            payload = receive(2)
            return {"observed_at": time.time(), "windows": normalize("codex", payload)}
        finally:
            selector.close()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def open_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, observed_at REAL NOT NULL,
            provider TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS windows (
            observation_id INTEGER NOT NULL REFERENCES observations(id),
            bucket TEXT NOT NULL, window_minutes INTEGER NOT NULL,
            used_percent REAL NOT NULL, resets_at REAL,
            PRIMARY KEY (observation_id, bucket, window_minutes));
        CREATE INDEX IF NOT EXISTS observation_time ON observations(observed_at);
    """)
    return conn


def collect(args):
    if args.dry_run:
        for provider in args.providers:
            print(shlex.join(collector_command(provider, args)))
        return 0
    os.umask(0o077)
    conn = open_database(args.db)
    failed = False
    try:
        for provider in args.providers:
            observed = time.time()
            rows, error = [], None
            try:
                cmd = collector_command(provider, args)
                if provider == "codex":
                    result = codex_snapshot(cmd)
                else:
                    logger.debug("%s", shlex.join(cmd))
                    response = subprocess.run(cmd, capture_output=True, timeout=40, check=False)
                    if not response.stdout:
                        raise RuntimeError("Claude collector exited " + str(response.returncode))
                    result = json.loads(response.stdout)
                    if "error" in result:
                        raise RuntimeError(result["error"])
                    if response.returncode:
                        raise RuntimeError("Claude fetch exited " + str(response.returncode))
                observed, rows = result["observed_at"], result["windows"]
                logger.info("%s: %s", provider, ", ".join(f"{r[0]} {r[1]}min {r[2]}%" for r in rows))
            except Exception as exc:
                # Only explicitly constructed RuntimeError messages are safe to persist.
                error = str(exc) if type(exc) is RuntimeError else type(exc).__name__
                logger.error("%s: %s", provider, error)
                failed = True
            with conn:
                obs_id = conn.execute("INSERT INTO observations(observed_at, provider, error) VALUES(?,?,?)",
                                      (observed, provider, error)).lastrowid
                conn.executemany("INSERT INTO windows VALUES(?,?,?,?,?)", [(obs_id, *row) for row in rows])
    finally:
        conn.close()
    return int(failed)


def plot(args):
    if args.dry_run:
        print(f"Read {args.db}; " + ("show a window" if args.show else "") + ("; " if args.show and args.output else "") + (f"write {args.output}" if args.output else ""))
        return 0
    import matplotlib
    # Zooming and panning need a GUI backend; Tk ships with CPython.
    matplotlib.use("TkAgg" if args.show else "Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(args.timezone)
    since = time.time() - args.days * 86400
    with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        observations = conn.execute("SELECT id, observed_at, provider FROM observations WHERE observed_at >= ? ORDER BY observed_at", (since,)).fetchall()
        rows = conn.execute("SELECT w.* FROM windows w JOIN observations o ON o.id=w.observation_id WHERE o.observed_at>=?", (since,)).fetchall()
    values = {(row[0], row[1], row[2]): row[3:] for row in rows}
    fig, axes = plt.subplots(2, 2, figsize=(13, 7), sharex=True, sharey=True, layout="constrained")
    for i, provider in enumerate(("claude", "codex")):
        obs = [(oid, stamp) for oid, stamp, p in observations if p == provider]
        observation_ids = {oid for oid, _ in obs}
        for j, minutes in enumerate((300, 10080)):
            ax = axes[i, j]
            buckets = sorted({bucket for oid, bucket, mins in values if mins == minutes and oid in observation_ids})
            for bucket in buckets:
                xs, ys = [], []
                previous_reset, previous_stamp = None, None
                for oid, stamp in obs:
                    used, reset = values.get((oid, bucket, minutes), (math.nan, None))
                    date = dt.datetime.fromtimestamp(stamp, tz)
                    # Claude's reset ISO strings have changing subsecond fractions;
                    # unused Codex buckets can advertise now + duration every time.
                    reset_crossed = (previous_reset is not None and reset is not None
                                     and reset - previous_reset >= 60 and previous_reset <= stamp)
                    if previous_stamp is not None and (stamp - previous_stamp > args.max_gap_minutes * 60 or reset_crossed):
                        xs.append(date)
                        ys.append(math.nan)
                    xs.append(date)
                    ys.append(used)
                    previous_reset, previous_stamp = reset, stamp
                ax.plot(xs, ys, ".-", markersize=3, linewidth=1, label=bucket)
            ax.set_title(f"{provider.title()} / {'5h' if minutes == 300 else 'Weekly'}")
            ax.set_ylim(-2, 102)
            ax.grid(alpha=.2)
            if buckets:
                ax.legend(fontsize=8)
            else:
                ax.text(.5, .5, "No observations", ha="center", va="center", transform=ax.transAxes)
            locator = mdates.AutoDateLocator(tz=tz)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=tz))
            if j == 0:
                ax.set_ylabel("Used (%)")
    fig.suptitle(f"Subscription limits — {args.timezone} — last {args.days:g} days")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(args.output, dpi=160)
        logger.info("Saved %s (%d observations)", args.output, len(observations))
    if args.show:
        logger.info("Showing %d observations; the toolbar's magnifier and hand zoom and pan all four panels", len(observations))
        plt.show()
    plt.close(fig)
    return 0


def snapshot(args):
    if args.db.resolve() == args.output.resolve():
        raise ValueError("snapshot output must differ from the source database")
    if args.dry_run:
        print(f"SQLite online backup: {args.db} -> {args.output}")
        return 0
    os.umask(0o077)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(args.output)) as target:
            source.backup(target)
            # Portable, self-contained snapshot; readers need no WAL/SHM files.
            target.execute("PRAGMA journal_mode=DELETE")
    logger.info("Saved snapshot %s", args.output)
    return 0


def main():
    parser = argparse.ArgumentParser(formatter_class=ArgumentDefaultsRawTextHelpFormatter, epilog=epilog)
    parser.add_argument("-q", "--quiet", action="count", default=0)
    subparsers = parser.add_subparsers(dest="subcommand_name", required=True)
    default_db = Path(__file__).resolve().parent / "data/history.sqlite3"
    for name, func in (("collect", collect), ("plot", plot), ("snapshot", snapshot)):
        sub = subparsers.add_parser(name, formatter_class=ArgumentDefaultsRawTextHelpFormatter)
        sub.set_defaults(func=func)
        sub.add_argument("--db", type=Path, default=default_db)
        sub.add_argument("-n", "--dry_run", "--dry-run", dest="dry_run", action="store_true")
        if name == "collect":
            sub.add_argument("--providers", nargs="+", choices=["claude", "codex"], default=["claude", "codex"])
            sub.add_argument("--claude-command", help="custom collector command, split with shlex (no shell); must return observation JSON")
            sub.add_argument("--codex-command", help="custom app-server command, split with shlex (no shell); must speak the stdio RPC protocol")
        elif name == "plot":
            sub.add_argument("--days", type=float, default=14)
            sub.add_argument("--timezone", default="UTC")
            sub.add_argument("--max-gap-minutes", type=float, default=15)
            sub.add_argument("--show", action="store_true", help="open an interactive window (zoom, pan) instead of writing a file")
            sub.add_argument("--output", type=Path, default=None, help="image to write; extension picks the format (default: agent-limits.png unless --show)")
        else:
            sub.add_argument("--output", type=Path, required=True)
    subparsers.add_parser("_fetch-claude", help=argparse.SUPPRESS).set_defaults(func=fetch_claude)
    args = parser.parse_args()
    if args.subcommand_name == "plot" and args.output is None and not args.show:
        args.output = Path("agent-limits.png")
    if args.subcommand_name == "plot" and (not math.isfinite(args.days) or args.days <= 0 or not math.isfinite(args.max_gap_minutes) or args.max_gap_minutes <= 0):
        parser.error("days and max-gap-minutes must be positive finite numbers")
    logger.setLevel({0: logging.DEBUG, 1: logging.INFO, 2: logging.WARNING}.get(args.quiet, logging.ERROR))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

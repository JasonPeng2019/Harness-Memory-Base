"""Cwd-independent, burst, repeated, and abrupt watcher lifecycle qualification."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from harness_common.process_identity import exact_process_identity
from orchestrator_harness.bootstrap import _remove_owned_tree
from harness_watcher_implementation.config import WatcherConfig
from harness_watcher_implementation.poller import poll


HARNESS = Path(__file__).resolve().parents[2]
PRODUCT = HARNESS.parent


class WatcherOperationalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix=".watcher-operational-", dir=HARNESS))
        self.source = self.root / "source.jsonl"
        self.source.write_text("", encoding="utf-8")
        relative_runtime = str((self.root / "runtime").relative_to(HARNESS))
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "runtime_root": relative_runtime,
            "observed_sources": [{
                "path": str(self.source), "role": "subagent", "source_id": "lane-burst",
            }],
            "poll_interval_seconds": 1,
            "no_progress_seconds": 60,
            "max_tail_bytes": 262144,
            "evaluator_enabled": False,
        }), encoding="utf-8")
        self.env = {
            **os.environ,
            "PYTHONPATH": str(HARNESS),
        }
        self.live_pids: set[int] = set()

    def tearDown(self) -> None:
        try:
            self._stop()
            for pid in tuple(self.live_pids):
                if exact_process_identity(pid) is not None:
                    os.kill(pid, signal.SIGTERM)
        finally:
            # Retry only this exact owned root after the exact watcher PID is
            # absent; NFS can retain a just-closed append briefly.
            _remove_owned_tree(self.root, timeout_seconds=5.0)
            self.assertFalse(self.root.exists())

    def _command(self, action: str, *, cwd: Path = HARNESS) -> dict:
        command = [
            sys.executable, "-m", "harness_watcher_implementation",
            "--config", str(self.config), action,
        ]
        if action == "start":
            command.extend(["--owner-pid", str(os.getpid())])
        completed = subprocess.run(
            command, cwd=cwd, env=self.env, text=True, capture_output=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def _start(self, *, cwd: Path = HARNESS) -> int:
        result = self._command("start", cwd=cwd)
        self.assertTrue(result["started"])
        pid = int(result["pid"])
        self.live_pids.add(pid)
        self.assertIsNotNone(exact_process_identity(pid))
        return pid

    def _stop(self, *, cwd: Path = HARNESS) -> None:
        try:
            self._command("stop", cwd=cwd)
        except (AssertionError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return
        deadline = time.monotonic() + 5
        state = {"running": True}
        while time.monotonic() < deadline:
            state = self._command("status", cwd=cwd)
            if not state["running"]:
                break
            time.sleep(0.05)
        self.assertFalse(state["running"])
        watcher = (state.get("state") or {}).get("watcher") or {}
        pid = watcher.get("pid")
        if isinstance(pid, int):
            deadline = time.monotonic() + 5
            while exact_process_identity(pid) is not None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNone(exact_process_identity(pid))
            self.live_pids.discard(pid)

    def test_twenty_start_stop_cycles_reap_exact_pid_from_both_cwds(self) -> None:
        started = time.monotonic()
        seen: set[tuple[int, str]] = set()
        for cycle in range(20):
            cwd = PRODUCT if cycle % 2 else HARNESS
            pid = self._start(cwd=cwd)
            identity = exact_process_identity(pid)
            self.assertIsNotNone(identity)
            key = (pid, str(identity["created_utc"]))
            self.assertNotIn(key, seen)
            seen.add(key)
            self._stop(cwd=cwd)
        self.assertLess(time.monotonic() - started, 35)
        self.assertEqual(set(), self.live_pids)

    def test_five_hundred_event_burst_is_fully_ingested_without_evaluator(self) -> None:
        rows = [
            {"lane_id": f"lane-{number % 8}", "sequence": number, "event": "progress"}
            for number in range(500)
        ]
        self.source.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        cfg = WatcherConfig(
            repository_root=HARNESS, runtime_root=self.root / "burst-runtime",
            observed_log_roots=(self.source,), poll_interval_seconds=1,
            no_progress_seconds=60, max_tail_bytes=262144,
            observed_sources=(), evaluator_enabled=False,
        )
        result = poll(cfg, None)
        self.assertTrue(result["skipped"])
        self.assertEqual("diagnostic-only", result["mode"])
        cursor = json.loads((cfg.runtime_root / "watcher/cursor.json").read_text())
        self.assertEqual(self.source.stat().st_size, cursor["sources"][str(self.source)]["offset"])
        ingested = 0
        for path in (cfg.runtime_root / "subagents").rglob("events.jsonl"):
            ingested += path.read_text(encoding="utf-8").count("PROGRESS_INGESTED")
        self.assertEqual(500, ingested)
        self.assertIn("EVALUATOR_SKIPPED", (cfg.runtime_root / "watcher/events.jsonl").read_text())

    @unittest.skipIf(os.name == "nt", "POSIX abrupt-stop signal qualification")
    def test_abrupt_service_death_leaves_stale_identity_but_restart_recovers(self) -> None:
        first = self._start()
        os.kill(first, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while exact_process_identity(first) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(exact_process_identity(first))
        self.live_pids.discard(first)
        stale = self._command("status")
        self.assertFalse(stale["running"])
        self.assertEqual(first, stale["state"]["watcher"]["pid"])

        second = self._start(cwd=PRODUCT)
        self.assertNotEqual(first, second)
        current = self._command("status", cwd=PRODUCT)
        self.assertTrue(current["running"])
        self.assertEqual(second, current["state"]["watcher"]["pid"])
        self._stop(cwd=PRODUCT)

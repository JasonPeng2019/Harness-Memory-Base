"""Bounded real-process and concurrency pressure at durable publication seams."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memory_harness import contracts, store
from orchestrator_harness import manager_queue
from orchestrator_harness.epochs import (
    CURRENT_EPOCH_SCHEMA, MANAGER_QUEUE_SCHEMA, current_epoch_path, manager_queue_path,
)
from orchestrator_harness.records import atomic_write_json, read_record


def test_thirty_two_concurrent_idempotent_usage_retries_create_one_call(tmp_path: Path) -> None:
    database = tmp_path / "usage.sqlite3"
    initial = store.MemoryStore(database)
    initial.initialize()
    initial.close()
    record = contracts.make_native_usage_start(
        source="provider:codex", invocation_id="one-native-call",
        objective_id="objective-stress", decision_id=None,
        maintenance_operation_id=None, stage="execution", category="online_execution",
        window_id="run-stress",
        binding={"requested": "model", "resolved": "model", "native": None},
    )

    def replay(_: int) -> dict:
        handle = store.MemoryStore(database)
        handle.initialize()
        try:
            return handle.start_native_usage(record)
        finally:
            handle.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(replay, range(32)))
    assert all(item == results[0] for item in results)
    final = store.MemoryStore(database)
    final.initialize()
    try:
        usages = final.list_native_usage(objective_id="objective-stress")
        assert len(usages) == 1
        assert usages[0]["invocation_id"] == "one-native-call"
    finally:
        final.close()


def test_manager_queue_pressure_serializes_all_delivery_receipts(tmp_path: Path) -> None:
    atomic_write_json(current_epoch_path(tmp_path), {
        "schema": CURRENT_EPOCH_SCHEMA, "epoch_id": "epoch-stress", "queue_id": "queue-stress",
    })
    atomic_write_json(manager_queue_path(tmp_path), {
        "schema": MANAGER_QUEUE_SCHEMA, "epoch_id": "epoch-stress", "queue_id": "queue-stress",
        "events": [{
            "event_id": "event-stress", "state": "ACKNOWLEDGED", "summary": "review",
            "history": [{"state": "ACKNOWLEDGED", "at": "before"}], "delivery_history": [],
        }],
    })
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: manager_queue.append_delivery_history(tmp_path, "event-stress"), range(32)))
    queue = manager_queue.read_manager_queue(tmp_path)
    event = queue["events"][0]
    assert len(event["delivery_history"]) == 32
    assert all(entry["outcome"] == "DELIVERED" for entry in event["delivery_history"])
    assert all(entry["source"] == "hook" for entry in event["delivery_history"])
    assert all(entry["event_id"] == "event-stress" for entry in event["delivery_history"])
    assert all(entry["queue_id"] == "queue-stress" for entry in event["delivery_history"])
    assert all(entry.get("at") for entry in event["delivery_history"])


def test_abrupt_child_death_cannot_publish_partial_record_or_touch_sentinel(tmp_path: Path) -> None:
    target = tmp_path / "published.json"
    partial = tmp_path / ".published.child.tmp"
    ready = tmp_path / "ready"
    sentinel = tmp_path / "unrelated-sentinel"
    sentinel.write_text("untouched", encoding="utf-8")
    script = (
        "import os,pathlib,sys,time;"
        "p=pathlib.Path(sys.argv[1]); f=p.open('wb'); f.write(b'{\\\"schema\\\":');"
        "f.flush(); os.fsync(f.fileno()); pathlib.Path(sys.argv[2]).write_text('ready'); time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(partial), str(ready)], cwd=tmp_path)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child.kill()
        child.wait(timeout=5)
        assert not target.exists()
        assert partial.read_bytes().startswith(b'{"schema":')

        expected = {"schema": "stress-record/v1", "value": "complete"}
        atomic_write_json(target, expected)
        assert json.loads(target.read_text(encoding="utf-8")) == expected
        assert sentinel.read_text(encoding="utf-8") == "untouched"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        partial.unlink(missing_ok=True)


def test_concurrent_atomic_publish_is_always_one_complete_writer(tmp_path: Path) -> None:
    target = tmp_path / "atomic.json"
    sentinel = tmp_path / "other.json"
    sentinel.write_text('{"owner":"unrelated"}', encoding="utf-8")

    def publish(number: int) -> None:
        atomic_write_json(target, {
            "schema": "atomic-stress/v1", "writer": number,
            "payload": "x" * 4096,
        })

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(publish, range(32)))
    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["schema"] == "atomic-stress/v1"
    assert record["writer"] in range(32)
    assert record["payload"] == "x" * 4096
    assert sentinel.read_text(encoding="utf-8") == '{"owner":"unrelated"}'
    assert not list(tmp_path.glob(".atomic.json.*.tmp"))

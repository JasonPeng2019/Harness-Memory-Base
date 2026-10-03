"""Operational snapshot qualification beyond isolated schema unit checks."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from memory_harness import contracts, store
from memory_harness.snapshot import SnapshotError, SnapshotService


SCOPE = {
    "application": "harness", "project": "qualification",
    "namespace": "snapshot", "owner": "ROOT",
}


def _service(*, ready: bool = False) -> SnapshotService:
    return SnapshotService(
        authorizer=lambda action, scope, path, credential: (
            credential == "one-use-operator" and scope == SCOPE
        ),
        dependency_verifier=(lambda capability, reference, scope: ready),
    )


def _start(state: store.MemoryStore, number: int) -> None:
    state.start_native_usage(contracts.make_native_usage_start(
        source="provider:codex", invocation_id=f"call-{number}",
        objective_id="objective-snapshot", decision_id=None,
        maintenance_operation_id=None, stage="execution",
        category="online_execution", window_id="run-snapshot",
        binding={"requested": "model", "resolved": "model", "native": None},
    ))


def test_concurrent_backup_is_one_consistent_quiescent_sqlite_view(tmp_path: Path) -> None:
    source = store.MemoryStore(tmp_path / "source" / "state.sqlite3")
    source.initialize()
    artifact = tmp_path / "artifact"
    writer_started = threading.Event()
    stop_writer = threading.Event()
    done = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        other = store.MemoryStore(source.path)
        try:
            other.initialize()
            number = 0
            while not stop_writer.is_set():
                _start(other, number)
                if number == 4:
                    writer_started.set()
                number += 1
                time.sleep(0.001)
        except BaseException as exc:  # surfaced in the owning test thread
            errors.append(exc)
        finally:
            other.close()
            done.set()

    thread = threading.Thread(target=writer, name="snapshot-writer")
    thread.start()
    try:
        assert writer_started.wait(5)
        before = len(source.list_native_usage(objective_id="objective-snapshot"))
        manifest = _service().export(
            source, artifact, scope=SCOPE, credential="one-use-operator",
        )
        # This is the concurrency oracle: export completed while the writer
        # was still live, rather than after a gate released it.
        assert thread.is_alive()
        stop_writer.set()
        assert done.wait(5)
        thread.join(5)
        assert errors == []
        assert manifest["database_sha256"]

        restored = store.MemoryStore(tmp_path / "restore" / "state.sqlite3")
        result = _service().restore(
            artifact, restored, scope=SCOPE, credential="one-use-operator",
        )
        restored.initialize()
        try:
            captured = restored.list_native_usage(objective_id="objective-snapshot")
            final_count = len(source.list_native_usage(objective_id="objective-snapshot"))
            assert before <= len(captured) <= final_count
            assert len({item["invocation_id"] for item in captured}) == len(captured)
            assert result["restored"]["capabilities"]["local"] is True
            # The backup is a valid transaction boundary, not a partially inserted row.
            assert all(item["coverage"] == "incomplete" for item in captured)
        finally:
            restored.close()
    finally:
        stop_writer.set()
        thread.join(5)
        source.close()


def test_pending_remote_dependency_and_corruption_never_publish_target(tmp_path: Path) -> None:
    source = store.MemoryStore(tmp_path / "source" / "state.sqlite3")
    source.initialize()
    artifact = tmp_path / "artifact"
    target = store.MemoryStore(tmp_path / "target" / "state.sqlite3")
    sentinel = tmp_path / "unrelated" / "sentinel"
    sentinel.parent.mkdir()
    sentinel.write_text("untouched", encoding="utf-8")
    try:
        manifest = _service().export(
            source, artifact, scope=SCOPE, credential="one-use-operator",
            dependencies=[{"capability": "atlas", "reference": "atlas://disposable/partition"}],
        )
        assert manifest["completeness"] == "incomplete"
        with pytest.raises(SnapshotError, match="dependency"):
            _service().restore(
                artifact, target, scope=SCOPE, credential="one-use-operator",
                required_capabilities=("atlas",),
            )
        assert not target.path.exists()

        database = artifact / "state.sqlite3"
        database.write_bytes(database.read_bytes() + b"corruption")
        with pytest.raises(SnapshotError, match="digest"):
            _service().restore(
                artifact, target, scope=SCOPE, credential="one-use-operator",
            )
        assert not target.path.exists()
        assert sentinel.read_text(encoding="utf-8") == "untouched"
    finally:
        source.close()
        target.close()


def test_restore_collision_and_scope_mismatch_leave_existing_bytes_unchanged(tmp_path: Path) -> None:
    source = store.MemoryStore(tmp_path / "source" / "state.sqlite3")
    source.initialize()
    artifact = tmp_path / "artifact"
    target = store.MemoryStore(tmp_path / "target" / "state.sqlite3")
    try:
        _service().export(source, artifact, scope=SCOPE, credential="one-use-operator")
        target.path.parent.mkdir(parents=True)
        target.path.write_bytes(b"existing-owner")
        with pytest.raises(SnapshotError, match="nonexistent|collision"):
            _service().restore(
                artifact, target, scope=SCOPE, credential="one-use-operator",
            )
        assert target.path.read_bytes() == b"existing-owner"
        target.path.unlink()
        with pytest.raises(SnapshotError, match="authorization|scope"):
            _service().restore(
                artifact, target, scope={**SCOPE, "owner": "another-root"},
                credential="one-use-operator",
            )
        assert not target.path.exists()
    finally:
        source.close()
        target.close()

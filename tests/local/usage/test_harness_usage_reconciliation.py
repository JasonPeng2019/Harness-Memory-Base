"""Joined controller-attempt to durable native-usage accounting regressions."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from memory_harness import contracts, store
from orchestrator_harness import attempt_attestation, memory_handoff
from orchestrator_harness.records import append_jsonl, atomic_write_json, read_jsonl


def _domain(worktree: Path) -> tuple[store.MemoryStore, dict, dict]:
    worktree.mkdir(parents=True, exist_ok=True)
    plan = contracts.make_plan(
        plan_id="plan-usage", objective_id="objective-usage", route="ordinary",
        state="accepted", accepted_by="ROOT", content={"steps": ["execute"]},
    )
    card = contracts.make_task_card(
        task="exercise usage bridge", base_commit="base",
        memory_handoff=contracts.make_memory_handoff(
            objective_id="objective-usage", route="ordinary", plan=plan,
            checkpoint="checkpoint-usage",
        ),
    )
    decision = contracts.make_decision(card, plan, configuration={"strategy": "standard"})
    path, _ = memory_handoff.memory_paths(worktree)
    state = store.MemoryStore(path)
    state.initialize()
    state.record_decision(decision)
    evidence = {
        "lane_id": "lane-usage", "run_id": "run-usage",
        "objective_id": "objective-usage", "decision_id": decision["decision_id"],
    }
    return state, decision, evidence


def _attempt(
    worktree: Path, observations: list[dict], *, decision_id: str,
    provider_id: str = "codex", attempt: int = 1, **changes: object,
) -> Path:
    transcript = worktree / ".agent-workspace/provider-transcript.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_bytes(b"x" * 100)
    record = {
        "schema": attempt_attestation.ATTEMPT_SCHEMA,
        "lane_id": "lane-usage", "run_id": "run-usage", "attempt": attempt,
        "provider": {"id": provider_id, "model": "requested-model", "launch_config": {}},
        "argv": [provider_id, "exec"], "session_id": "native-session",
        "transcript_path": str(transcript),
        "transcript_start_byte": 0, "transcript_end_byte": 100,
        "transcript_sha256": hashlib.sha256(b"x" * 100).hexdigest(),
        "provider_started": True, "result_state": "valid", "cleanup_proven": True,
        "dispatch_binding": {"decision_id": decision_id},
        "native_usage_state": "observed" if observations else "incomplete",
        "native_usage_observations": observations,
        "native_usage_capture_error": None,
    }
    record.update(changes)
    key = b"usage-reconciliation-controller-key"
    record = attempt_attestation.sign_attempt(record, key)
    attempts_path = worktree.parent / f"{worktree.name}-runtime/controller.attempts.jsonl"
    append_jsonl(
        attempts_path, record, header=attempt_attestation.JOURNAL_HEADER,
    )
    rows = [
        row for row in read_jsonl(attempts_path)[1:]
        if row.get("lane_id") == "lane-usage" and row.get("run_id") == "run-usage"
    ]
    atomic_write_json(
        attempt_attestation.attestation_path(attempts_path, "run-usage"),
        attempt_attestation.make_attestation(
            lane_id="lane-usage", run_id="run-usage", key=key,
            row_content_hashes=[row["content_hash"] for row in rows],
        ),
    )
    return attempts_path


def _codex(count: int, *, turn: str = "turn-1", line: int = 1) -> dict:
    return {
        "line_number": line, "byte_offset": (line - 1) * 40,
        "event_type": "turn.completed", "turn_id": turn,
        "usage_mode": "cumulative", "usage_complete": True,
        "usage": {"input_tokens": count, "cached_input_tokens": 2, "output_tokens": 3},
    }


def _reconcile(worktree: Path, attempts: Path, evidence: dict) -> list[dict]:
    return memory_handoff.reconcile_native_usage(
        worktree_path=worktree, attempts_path=attempts, evidence=evidence,
    )


def test_attempt_receipts_reach_store_and_cumulative_replay_counts_once(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    state, decision, evidence = _domain(worktree)
    try:
        attempts = _attempt(
            worktree, [_codex(4), _codex(4, line=2), _codex(9, turn="turn-2", line=3)],
            decision_id=decision["decision_id"],
        )
        first = _reconcile(worktree, attempts, evidence)
        replay = _reconcile(worktree, attempts, evidence)

        assert replay == first
        assert len(first) == 1
        usage = first[0]
        assert usage["coverage"] == "complete"
        assert usage["receipt_count"] == 2  # duplicate turn-1 source event is idempotent
        assert usage["measures"]["input_tokens|tokens|included"]["value"] == 9
        assert state.aggregate_native_usage(objective_id="objective-usage")["invocations"] == 1
    finally:
        state.close()


def test_started_empty_attempt_is_incomplete_and_late_receipt_completes_it(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    state, decision, evidence = _domain(worktree)
    try:
        attempts = _attempt(worktree, [], decision_id=decision["decision_id"])
        [incomplete] = _reconcile(worktree, attempts, evidence)
        assert incomplete["coverage"] == "incomplete"
        assert incomplete["measures"] == {}

        _attempt(worktree, [_codex(7)], decision_id=decision["decision_id"])
        [complete] = _reconcile(worktree, attempts, evidence)
        assert complete["coverage"] == "complete"
        assert complete["measures"]["input_tokens|tokens|included"]["value"] == 7
    finally:
        state.close()


def test_conflicting_source_event_fails_closed(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    state, decision, evidence = _domain(worktree)
    try:
        attempts = _attempt(worktree, [_codex(4)], decision_id=decision["decision_id"])
        _reconcile(worktree, attempts, evidence)
        _attempt(worktree, [_codex(99)], decision_id=decision["decision_id"])
        with pytest.raises(memory_handoff.MemoryHandoffError, match="usage"):
            _reconcile(worktree, attempts, evidence)
        usage = state.list_native_usage(objective_id="objective-usage")[0]
        assert usage["measures"]["input_tokens|tokens|included"]["value"] == 4
    finally:
        state.close()


def test_wrong_dispatch_binding_is_not_attributed(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    state, _decision, evidence = _domain(worktree)
    try:
        attempts = _attempt(worktree, [_codex(4)], decision_id="different-decision")
        with pytest.raises(memory_handoff.MemoryHandoffError, match="dispatch binding"):
            _reconcile(worktree, attempts, evidence)
        assert state.list_native_usage(objective_id="objective-usage") == []
    finally:
        state.close()


def test_usage_reconciliation_never_changes_fixed_outcome_quality(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    state, decision, evidence = _domain(worktree)
    try:
        outcome = contracts.make_outcome(
            decision_id=decision["decision_id"], plan_id=decision["plan_id"],
            plan_digest=decision["plan_digest"], status="FAIL", evidence_digest="evidence",
            linked_run_id="run-usage", task_card_digest=decision["task_card_digest"],
            objective_id=decision["objective_id"],
        )
        state.record_outcome(outcome)
        before = state.get_outcome(decision["decision_id"])
        attempts = _attempt(worktree, [_codex(4)], decision_id=decision["decision_id"])
        _reconcile(worktree, attempts, evidence)
        assert state.get_outcome(decision["decision_id"]) == before
        assert before["status"] == "FAIL"
    finally:
        state.close()


@pytest.mark.parametrize("provider_id", ["claude-code", "qwen-code"])
def test_incremental_messages_sum_without_terminal_rollup(
    tmp_path: Path, provider_id: str,
) -> None:
    worktree = tmp_path / "worktree"
    state, decision, evidence = _domain(worktree)
    observations = [
        {
            "event_type": "assistant", "usage_mode": "incremental",
            "usage_complete": False, "message_id": f"message-{index}",
            "message_model": "requested-model", "usage": {"input_tokens": value},
        }
        for index, value in enumerate((4, 7), start=1)
    ]
    try:
        attempts = _attempt(
            worktree, observations, decision_id=decision["decision_id"],
            provider_id=provider_id,
        )
        [usage] = _reconcile(worktree, attempts, evidence)
        assert usage["coverage"] == "incomplete"
        assert usage["receipt_count"] == 2
        assert usage["measures"]["input_tokens|tokens|included"]["value"] == 11
    finally:
        state.close()


@pytest.mark.parametrize("provider_id", ["claude-code", "qwen-code"])
def test_terminal_rollup_supersedes_overlapping_incremental_messages(
    tmp_path: Path, provider_id: str,
) -> None:
    worktree = tmp_path / "worktree"
    state, decision, evidence = _domain(worktree)
    observations = [
        {
            "event_type": "assistant", "usage_mode": "incremental",
            "usage_complete": False, "message_id": "message-1",
            "message_model": "requested-model", "usage": {"input_tokens": 4},
        },
        {
            "event_type": "assistant", "usage_mode": "incremental",
            "usage_complete": False, "message_id": "message-2",
            "message_model": "requested-model", "usage": {"input_tokens": 7},
        },
        {
            "event_type": "result", "usage_mode": "cumulative",
            "usage_complete": True, "uuid": "result-1",
            "usage": {"input_tokens": 11},
        },
    ]
    try:
        attempts = _attempt(
            worktree, observations, decision_id=decision["decision_id"],
            provider_id=provider_id,
        )
        [usage] = _reconcile(worktree, attempts, evidence)
        assert usage["coverage"] == "complete"
        assert usage["receipt_count"] == 1
        assert usage["measures"]["input_tokens|tokens|included"]["value"] == 11
        # Qwen/Claude terminal result events need not repeat the assistant
        # message's native model.  Pruning the overlapping assistant counters
        # must not prune that binding evidence.
        assert usage["effective_binding"]["native"] == "requested-model"
    finally:
        state.close()


@pytest.mark.parametrize("provider_id", ["claude-code", "qwen-code"])
def test_terminal_rollup_rejects_conflicting_pruned_native_models(
    tmp_path: Path, provider_id: str,
) -> None:
    worktree = tmp_path / "worktree"
    state, decision, evidence = _domain(worktree)
    observations = [
        {
            "event_type": "assistant", "usage_mode": "incremental",
            "usage_complete": False, "message_id": "message-1",
            "message_model": "native-model-a", "usage": {"input_tokens": 4},
        },
        {
            "event_type": "result", "usage_mode": "cumulative",
            "usage_complete": True, "uuid": "result-1",
            "modelUsage": {"native-model-b": {"input_tokens": 4}},
            "usage": {"input_tokens": 4},
        },
    ]
    try:
        attempts = _attempt(
            worktree, observations, decision_id=decision["decision_id"],
            provider_id=provider_id,
        )
        with pytest.raises(memory_handoff.MemoryHandoffError, match="native model"):
            _reconcile(worktree, attempts, evidence)
        assert state.list_native_usage(objective_id="objective-usage") == []
    finally:
        state.close()


def test_worker_writable_or_tampered_attempt_evidence_is_rejected(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    state, decision, evidence = _domain(worktree)
    try:
        trusted = _attempt(worktree, [_codex(4)], decision_id=decision["decision_id"])
        worker_journal = worktree / ".agent-workspace/controller.attempts.jsonl"
        worker_journal.write_bytes(trusted.read_bytes())
        with pytest.raises(memory_handoff.MemoryHandoffError, match="outside"):
            _reconcile(worktree, worker_journal, evidence)

        transcript = worktree / ".agent-workspace/provider-transcript.jsonl"
        transcript.write_bytes(b"z" * 100)
        with pytest.raises(memory_handoff.MemoryHandoffError, match="digest mismatch"):
            _reconcile(worktree, trusted, evidence)
    finally:
        state.close()

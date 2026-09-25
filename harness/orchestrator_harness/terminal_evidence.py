"""Durable, worktree-independent native evidence for one enhanced parent review.

The lane-1 outcome transaction can read this record after worktree retirement.
This module validates the supplied evidence; it does not fix an outcome or run
follow-on effects. Missing evidence returns ``None`` from the reader, while a
present but contradictory record raises ``TerminalEvidenceError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .core import content_hash, read_json, sha256_hex
from .epochs import lane_record_dir

TERMINAL_EVIDENCE_SCHEMA = "native-terminal-evidence/v1"
TERMINAL_EVIDENCE_NAME = "NATIVE_TERMINAL_EVIDENCE.json"


class TerminalEvidenceError(ValueError):
    """A present native terminal evidence record is invalid or contradictory."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TerminalEvidenceError(message)


def _hashed(record: Any, schema: str, name: str) -> dict[str, Any]:
    _require(isinstance(record, dict), f"{name} is not an object")
    _require(record.get("schema") == schema, f"{name} schema mismatch")
    _require(record.get("content_hash") == content_hash(record), f"{name} hash mismatch")
    return record


def validate_terminal_evidence(
    record: Mapping[str, Any], *, lane_id: str | None = None, run_id: str | None = None,
) -> None:
    """Validate v1 structure, hashes, and exact internal source links.

    This checks the self-contained durable record. The reader additionally
    checks that its sibling review and acceptance files still match.
    """
    evidence = _hashed(record, TERMINAL_EVIDENCE_SCHEMA, "terminal evidence")
    lane = evidence.get("lane_id")
    run = evidence.get("run_id")
    _require(isinstance(evidence.get("epoch_id"), str) and bool(evidence["epoch_id"]), "epoch identity missing")
    _require(isinstance(lane, str) and bool(lane), "lane identity missing")
    _require(isinstance(run, str) and bool(run), "run identity missing")
    _require(lane_id is None or lane == lane_id, "terminal evidence lane mismatch")
    _require(run_id is None or run == run_id, "terminal evidence run mismatch")

    card = _hashed(evidence.get("task_card"), "project-task-card/v1", "task card")
    plan = _hashed(evidence.get("accepted_plan"), "memory-plan/v1", "accepted plan")
    handoff = card.get("memory_handoff")
    _require(isinstance(card.get("task"), str) and bool(card["task"].strip()), "parent task missing")
    _require(isinstance(handoff, dict) and handoff.get("plan_state") == "execution_accepted", "not an accepted parent handoff")
    _require(handoff.get("plan") == plan, "accepted plan does not match task card")
    _require(plan.get("state") == "accepted" and plan.get("accepted_by") == "ROOT", "plan lacks ROOT acceptance")
    objective = evidence.get("objective_id")
    decision = evidence.get("decision_id")
    _require(isinstance(objective, str) and bool(objective), "objective identity missing")
    _require(isinstance(decision, str) and bool(decision), "decision identity missing")
    _require(handoff.get("objective_id") == objective == plan.get("objective_id"), "objective mismatch")
    _require(handoff.get("route") == plan.get("route"), "route mismatch")

    configuration = evidence.get("configuration")
    _require(isinstance(configuration, dict), "resolved configuration missing")
    configuration_digest = evidence.get("configuration_digest")
    _require(configuration_digest == sha256_hex(configuration), "configuration digest mismatch")
    dispatch = evidence.get("dispatch")
    _require(isinstance(dispatch, dict), "dispatch evidence missing")
    operation = dispatch.get("operation")
    _require(isinstance(operation, dict), "dispatch operation missing")
    _require(dispatch.get("operation_digest") == sha256_hex(operation), "dispatch receipt digest mismatch")
    observed = dispatch.get("observed_invocation")
    _require(isinstance(observed, dict), "native invocation missing")
    _require(operation.get("kind") == "dispatch" and operation.get("status") == "delivered", "dispatch was not delivered")
    _require(operation.get("observed_invocation") == observed, "operation invocation mismatch")
    envelope_digest = dispatch.get("envelope_digest")
    _require(isinstance(envelope_digest, str) and bool(envelope_digest), "envelope digest missing")
    _require(operation.get("envelope_digest") == envelope_digest, "dispatch envelope mismatch")
    _require(operation.get("operation_id") == sha256_hex({"kind": "dispatch", "envelope_digest": envelope_digest}), "dispatch operation identity mismatch")
    _require(operation.get("decision_id") == decision and operation.get("run_id") == run, "dispatch decision or run mismatch")
    expected_observation = {
        "task_card_digest": card["content_hash"],
        "decision_id": decision,
        "plan_id": plan.get("plan_id"),
        "plan_digest": plan["content_hash"],
        "envelope_digest": envelope_digest,
        "lane_id": lane,
        "run_id": run,
        "base_commit": card.get("base_commit"),
        "route": plan.get("route"),
        "configuration_digest": configuration_digest,
    }
    for field, value in expected_observation.items():
        _require(observed.get(field) == value, f"native observation {field} mismatch")
    _require(isinstance(observed.get("context_id"), str) and bool(observed["context_id"]), "native context identity missing")
    _require(isinstance(observed.get("context_digest"), str) and bool(observed["context_digest"]), "native context digest missing")
    pid = observed.get("pid")
    creation = observed.get("creation_time")
    _require(isinstance(pid, int) and not isinstance(pid, bool) and pid > 0, "native process ID missing")
    _require(isinstance(creation, str) and bool(creation), "native creation time missing")
    _require(observed.get("invocation_id") == f"controller:{pid}:{creation}", "native invocation identity mismatch")

    review = _hashed(evidence.get("review"), "completion-review/v1", "review")
    acceptance = _hashed(evidence.get("acceptance"), "orchestrator-acceptance/v1", "acceptance")
    _require(review.get("lane_id") == lane == acceptance.get("lane_id"), "review lane mismatch")
    _require(review.get("run_id") == run == acceptance.get("run_id"), "review run mismatch")
    _require(acceptance.get("review_ref") == review["content_hash"], "acceptance review link mismatch")
    _require(acceptance.get("accepted_by") == "ROOT", "ROOT acceptance identity missing")
    _require(acceptance.get("approval") in {"ACCEPTED", "REJECTED"}, "approval invalid")
    task_card_id = str(card.get("card_id") or card.get("id") or card["content_hash"])
    for field, value in (
        ("task_card_id", task_card_id), ("task_card_hash", card["content_hash"]),
    ):
        _require(review.get(field) == value == acceptance.get(field), f"{field} mismatch")
    for field in ("result_id", "result_hash", "commit"):
        _require(review.get(field) == acceptance.get(field), f"{field} mismatch")
    _require(isinstance(review.get("commit"), str) and bool(review["commit"]), "review commit missing")

    result = evidence.get("result")
    proof = evidence.get("terminal_proof")
    outcome = review.get("review_outcome")
    if outcome == "UNKNOWN":
        _require(result is None, "UNKNOWN cannot carry a result")
        _require(review.get("result_id") is None and review.get("result_hash") is None, "UNKNOWN has a result identity")
        _require(acceptance.get("approval") == "ACCEPTED", "UNKNOWN lacks exceptional ROOT acceptance")
        reason = acceptance.get("force_accept_reason")
        _require(isinstance(reason, str) and bool(reason.strip()), "UNKNOWN lacks exceptional acceptance reason")
        _require(isinstance(proof, dict) and proof.get("schema") == "controller-status/v1", "UNKNOWN lacks controller proof")
        _require(proof.get("lane_id") == lane and proof.get("run_id") == run, "controller proof run mismatch")
        _require(proof.get("controller_state") == "exited", "controller exit unproven")
        _require(isinstance(proof.get("provider_state"), dict) and proof["provider_state"].get("state") == "exited", "provider exit unproven")
        _require(proof.get("result_state") in {"absent", "invalid"}, "no-result state unproven")
        _require(proof.get("recorded_status") == "provider_exited_no_result", "exact no-result status unproven")
        _require(proof.get("cleanup_proven") is True, "cleanup unproven")
        proof_digest = sha256_hex(proof)
        _require(review.get("terminal_proof_digest") == proof_digest == acceptance.get("terminal_proof_digest"), "controller proof digest mismatch")
    else:
        _require(outcome in {"PASS", "FAIL", "BLOCKED"}, "review outcome invalid")
        result = _hashed(result, "result/v1", "result")
        _require(result.get("lane_id") == lane and result.get("run_id") == run, "result run mismatch")
        _require(result.get("outcome") in {"PASS", "FAIL", "BLOCKED"}, "result outcome invalid")
        _require(review.get("result_id") == run and review.get("result_hash") == result["content_hash"], "review result link mismatch")
        _require(proof is None, "ordinary result carries UNKNOWN proof")
        _require("terminal_proof_digest" not in review and "terminal_proof_digest" not in acceptance, "ordinary result carries UNKNOWN digest")
        if acceptance["approval"] == "ACCEPTED" and outcome != "PASS":
            reason = acceptance.get("force_accept_reason")
            _require(isinstance(reason, str) and bool(reason.strip()), "forced acceptance reason missing")


def make_terminal_evidence(
    *, epoch_id: str, lane_id: str, run_id: str, task_card: dict[str, Any],
    accepted_plan: dict[str, Any], objective_id: str, decision_id: str,
    dispatch_operation: dict[str, Any], envelope_digest: str,
    observed_invocation: dict[str, Any], configuration: dict[str, Any],
    configuration_digest: str, result: dict[str, Any] | None,
    review: dict[str, Any], acceptance: dict[str, Any],
    terminal_proof: dict[str, Any] | None,
) -> dict[str, Any]:
    record = {
        "schema": TERMINAL_EVIDENCE_SCHEMA,
        "epoch_id": epoch_id, "lane_id": lane_id, "run_id": run_id,
        "task_card": task_card, "objective_id": objective_id,
        "decision_id": decision_id, "accepted_plan": accepted_plan,
        "dispatch": {
            "operation": dispatch_operation,
            "operation_digest": sha256_hex(dispatch_operation),
            "envelope_digest": envelope_digest,
            "observed_invocation": observed_invocation,
        },
        "configuration": configuration,
        "configuration_digest": configuration_digest,
        "result": result, "review": review, "acceptance": acceptance,
        "terminal_proof": terminal_proof,
    }
    record["content_hash"] = content_hash(record)
    validate_terminal_evidence(record, lane_id=lane_id, run_id=run_id)
    return record


def read_terminal_evidence(
    rt: Path, epoch_id: str, lane_id: str, *, run_id: str | None = None,
) -> dict[str, Any] | None:
    """Read one retained lane record, or ``None`` when it was never emitted."""
    folder = lane_record_dir(rt, epoch_id, lane_id)
    path = folder / TERMINAL_EVIDENCE_NAME
    if not path.exists():
        return None
    try:
        record = read_json(path)
        validate_terminal_evidence(record, lane_id=lane_id, run_id=run_id)
        _require(record["epoch_id"] == epoch_id, "terminal evidence epoch mismatch")
        for name, field in (
            ("COMPLETION_REVIEW.json", "review"),
            ("ORCHESTRATOR_ACCEPTANCE.json", "acceptance"),
        ):
            sibling = read_json(folder / name)
            _require(sibling == record[field], f"{name} conflicts with terminal evidence")
    except (OSError, ValueError) as exc:
        if isinstance(exc, TerminalEvidenceError):
            raise
        raise TerminalEvidenceError(f"cannot validate terminal evidence at {path}: {exc}") from exc
    return record

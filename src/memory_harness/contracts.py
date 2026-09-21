"""Canonical record schemas, serialization, and integrity contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import uuid4

TASK_CARD_SCHEMA = "project-task-card/v1"
MEMORY_HANDOFF_SCHEMA = "memory-handoff/v1"
PLAN_SCHEMA = "memory-plan/v1"
DECISION_SCHEMA = "memory-decision/v1"
ENVELOPE_SCHEMA = "memory-dispatch/v1"
OPERATION_SCHEMA = "memory-operation/v1"
OUTCOME_SCHEMA = "memory-outcome/v1"
REVIEW_RECEIPT_SCHEMA = "memory-review-receipt/v1"
REVIEWED_TRAJECTORY_SCHEMA = "reviewed-trajectory/v1"
EXPERIENCE_INGESTION_SCHEMA = "reviewed-experience-ingestion/v1"
CASE_RECEIPT_SCHEMA = "reviewed-case-receipt/v1"
GENERATED_SKILL_SCHEMA = "generated-skill-candidate/v1"
SKILL_APPROVAL_SCHEMA = "generated-skill-approval/v1"
APC_REQUEST_SCHEMA = "apc-request/v1"
APC_RESULT_SCHEMA = "apc-result/v1"

PLAN_STATES = frozenset({"candidate", "accepted", "proposed", "fresh"})
ROUTES = frozenset({"ordinary", "problem_focused", "deeper"})
OUTCOME_STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "UNKNOWN"})
OPERATION_STATUSES = frozenset({"pending", "ambiguous", "delivered"})
REVIEW_STATES = frozenset({"reviewed", "accepted"})
REVIEWED_TRAJECTORY_STATUSES = frozenset({"reviewed_success", "reviewed_failure"})
GENERATED_SKILL_STATES = frozenset({"proposed"})
EXPERIENCE_INGESTION_STATUSES = frozenset({
    "pending",
    "uncertain",
    "confirmed",
    "blocked",
})


class ContractError(ValueError):
    """A product-owned record is malformed or violates an integrity contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def content_hash(record: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "content_hash"}
    return sha256_hex(payload)


def validate_record(record: Mapping[str, Any], schema: str) -> None:
    if not isinstance(record, Mapping):
        raise ContractError("record must be a JSON object")
    if record.get("schema") != schema:
        raise ContractError(f"record schema mismatch: expected {schema!r}, got {record.get('schema')!r}")
    if "content_hash" not in record:
        raise ContractError("record has no content hash")
    if record["content_hash"] != content_hash(record):
        raise ContractError("record content hash mismatch")


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a nonempty string")
    return value


def _validate_content(value: Any, field: str = "content") -> None:
    if not isinstance(value, (dict, list)):
        raise ContractError(f"{field} must be a JSON object or array")


def make_task_card(
    *,
    task: str,
    base_commit: str,
    branch: str | None = None,
    memory_handoff: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    task_text = _require_nonempty_str(task, "task")
    base = _require_nonempty_str(base_commit, "base_commit")
    record: dict[str, Any] = {
        "schema": TASK_CARD_SCHEMA,
        "task": task_text,
        "base_commit": base,
    }
    if branch is not None:
        record["branch"] = _require_nonempty_str(branch, "branch")
    if memory_handoff is not None:
        if not isinstance(memory_handoff, Mapping):
            raise ContractError("memory_handoff must be an object")
        record["memory_handoff"] = dict(memory_handoff)
    record["content_hash"] = content_hash(record)
    validate_task_card(record)
    return record


def validate_task_card(record: Mapping[str, Any]) -> None:
    validate_record(record, TASK_CARD_SCHEMA)
    _require_nonempty_str(record.get("task"), "task")
    _require_nonempty_str(record.get("base_commit"), "base_commit")
    if "memory_handoff" in record:
        validate_memory_handoff(record["memory_handoff"])


def make_memory_handoff(
    *,
    objective_id: str,
    route: str,
    plan: Mapping[str, Any],
    configuration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    objective = _require_nonempty_str(objective_id, "objective_id")
    selected_route = _require_nonempty_str(route, "route")
    if selected_route not in ROUTES:
        raise ContractError(f"unknown route: {selected_route!r}")
    validate_plan(plan)
    record: dict[str, Any] = {
        "schema": MEMORY_HANDOFF_SCHEMA,
        "objective_id": objective,
        "route": selected_route,
        "plan": dict(plan),
        "configuration": dict(configuration or {}),
    }
    record["content_hash"] = content_hash(record)
    validate_memory_handoff(record)
    return record


def validate_memory_handoff(record: Mapping[str, Any]) -> None:
    validate_record(record, MEMORY_HANDOFF_SCHEMA)
    objective = _require_nonempty_str(record.get("objective_id"), "objective_id")
    route = _require_nonempty_str(record.get("route"), "route")
    if route not in ROUTES:
        raise ContractError(f"unknown route: {route!r}")
    plan = record.get("plan")
    if not isinstance(plan, Mapping):
        raise ContractError("memory handoff plan must be an object")
    validate_plan(plan, expected_objective_id=objective, expected_route=route)
    if not isinstance(record.get("configuration", {}), Mapping):
        raise ContractError("configuration must be an object")


def make_plan(
    *,
    plan_id: str,
    objective_id: str,
    route: str,
    state: str,
    content: Any,
    revision: int = 1,
    accepted_by: str | None = None,
    supersedes: str | None = None,
    source: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    plan = _require_nonempty_str(plan_id, "plan_id")
    objective = _require_nonempty_str(objective_id, "objective_id")
    selected_route = _require_nonempty_str(route, "route")
    plan_state = _require_nonempty_str(state, "state")
    if selected_route not in ROUTES:
        raise ContractError(f"unknown route: {selected_route!r}")
    if plan_state not in PLAN_STATES:
        raise ContractError(f"unknown plan state: {plan_state!r}")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ContractError("revision must be a positive integer")
    _validate_content(content)
    if plan_state == "accepted":
        accepted_by = _require_nonempty_str(accepted_by, "accepted_by")
    elif accepted_by is not None:
        raise ContractError("accepted_by is only valid for an accepted plan")
    if supersedes is not None:
        supersedes = _require_nonempty_str(supersedes, "supersedes")
        if supersedes == plan:
            raise ContractError("a plan cannot supersede itself")
    if source is not None and not isinstance(source, Mapping):
        raise ContractError("source must be an object")
    record: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "plan_id": plan,
        "objective_id": objective,
        "route": selected_route,
        "state": plan_state,
        "revision": revision,
        "content": content,
        "accepted_by": accepted_by,
        "supersedes": supersedes,
        **({"source": dict(source)} if source is not None else {}),
        "created_at": created_at or utc_now(),
    }
    record["content_hash"] = content_hash(record)
    validate_plan(record)
    return record


def validate_plan(
    record: Mapping[str, Any],
    *,
    expected_objective_id: str | None = None,
    expected_route: str | None = None,
    expected_state: str | None = None,
) -> None:
    plan_id = _require_nonempty_str(record.get("plan_id"), "plan_id")
    objective_id = _require_nonempty_str(record.get("objective_id"), "objective_id")
    route = _require_nonempty_str(record.get("route"), "route")
    state = _require_nonempty_str(record.get("state"), "state")
    validate_record(record, PLAN_SCHEMA)
    if route not in ROUTES:
        raise ContractError(f"unknown route: {route!r}")
    if state not in PLAN_STATES:
        raise ContractError(f"unknown plan state: {state!r}")
    revision = record.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ContractError("revision must be a positive integer")
    _validate_content(record.get("content"))
    if state == "accepted":
        if record.get("accepted_by") != "ROOT":
            raise ContractError("only ROOT may accept a plan")
    elif record.get("accepted_by") is not None:
        raise ContractError("accepted_by is only valid for an accepted plan")
    supersedes = record.get("supersedes")
    if supersedes is not None:
        if not isinstance(supersedes, str) or not supersedes:
            raise ContractError("supersedes must be a nonempty string")
        if supersedes == plan_id:
            raise ContractError("a plan cannot supersede itself")
    source = record.get("source")
    if source is not None and not isinstance(source, Mapping):
        raise ContractError("source must be an object")
    if expected_objective_id is not None and objective_id != expected_objective_id:
        raise ContractError(f"plan objective mismatch: expected {expected_objective_id!r}, got {objective_id!r}")
    if expected_route is not None and route != expected_route:
        raise ContractError(f"plan route mismatch: expected {expected_route!r}, got {route!r}")
    if expected_state is not None and state != expected_state:
        raise ContractError(f"plan state mismatch: expected {expected_state!r}, got {state!r}")


def revise_plan(
    old_plan: Mapping[str, Any],
    *,
    new_plan_id: str,
    new_content: Any,
    accepted_by: str = "ROOT",
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validate_plan(old_plan)
    if accepted_by != "ROOT":
        raise ContractError("only ROOT may accept a revised plan")
    _validate_content(new_content)
    return make_plan(
        plan_id=new_plan_id,
        objective_id=old_plan["objective_id"],
        route=old_plan["route"],
        state="accepted",
        content=new_content,
        revision=int(old_plan["revision"]) + 1,
        accepted_by=accepted_by,
        supersedes=old_plan["plan_id"],
        source=source if source is not None else old_plan.get("source"),
    )


def validate_task_plan_binding(
    task_card: Mapping[str, Any], plan: Mapping[str, Any]
) -> None:
    """Require an enhanced task card to name the same exact current plan."""

    validate_task_card(task_card)
    validate_plan(plan)
    handoff = task_card.get("memory_handoff")
    if handoff is None:
        return
    bound_plan = handoff["plan"]
    if bound_plan["content_hash"] != plan["content_hash"]:
        raise ContractError("task card plan does not match the supplied plan")


def make_decision(
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    strategy: str = "standard",
    configuration: Mapping[str, Any] | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    validate_task_plan_binding(task_card, plan)
    resolved_configuration = dict(configuration or {"strategy": strategy})
    if resolved_configuration.get("strategy") != strategy:
        raise ContractError("decision strategy does not match its configuration")
    configuration_digest = sha256_hex(resolved_configuration)
    identity = decision_id or sha256_hex(
        {
            "task_card_digest": task_card["content_hash"],
            "objective_id": plan["objective_id"],
            "route": plan["route"],
            "plan_id": plan["plan_id"],
            "strategy": strategy,
            "configuration_digest": configuration_digest,
        }
    )
    record: dict[str, Any] = {
        "schema": DECISION_SCHEMA,
        "decision_id": identity,
        "task_card_digest": task_card["content_hash"],
        "objective_id": plan["objective_id"],
        "route": plan["route"],
        "plan_id": plan["plan_id"],
        "plan_state": plan["state"],
        "plan_digest": plan["content_hash"],
        "strategy": strategy,
        "configuration": resolved_configuration,
        "configuration_digest": configuration_digest,
        "state": "prepared" if plan["state"] == "accepted" else plan["state"],
        "created_at": utc_now(),
    }
    record["content_hash"] = content_hash(record)
    validate_decision(record)
    return record


def validate_decision(record: Mapping[str, Any]) -> None:
    validate_record(record, DECISION_SCHEMA)
    for field in (
        "decision_id",
        "task_card_digest",
        "objective_id",
        "route",
        "plan_id",
        "plan_state",
        "plan_digest",
        "strategy",
        "configuration_digest",
        "state",
    ):
        _require_nonempty_str(record.get(field), field)
    if record["route"] not in ROUTES:
        raise ContractError(f"unknown route: {record['route']!r}")
    configuration = record.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ContractError("configuration must be an object")
    if configuration.get("strategy") != record["strategy"]:
        raise ContractError("decision strategy does not match its configuration")
    if record["configuration_digest"] != sha256_hex(configuration):
        raise ContractError("decision configuration digest mismatch")


def _normalize_content_list(value: Iterable[Mapping[str, Any]] | None, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContractError(f"{field} must be a list")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ContractError(f"{field} entries must be objects")
        normalized = dict(item)
        if "id" not in normalized or not isinstance(normalized["id"], str) or not normalized["id"]:
            raise ContractError(f"{field} entries require a nonempty id")
        result.append(normalized)
    return result


def _digest_content_list(value: list[dict[str, Any]]) -> str:
    return sha256_hex(value)


def make_envelope(
    *,
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    decision_id: str,
    lane_id: str,
    run_id: str,
    worktree_path: str,
    base_commit: str,
    mandatory_content: list[Mapping[str, Any]] | None = None,
    optional_content: list[Mapping[str, Any]] | None = None,
    omitted_content: list[str] | None = None,
    strategy: str = "standard",
    configuration: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    validate_task_plan_binding(task_card, plan)
    validate_plan(plan, expected_state="accepted")
    validate_decision_id = _require_nonempty_str(decision_id, "decision_id")
    lane = _require_nonempty_str(lane_id, "lane_id")
    run = _require_nonempty_str(run_id, "run_id")
    worktree = _require_nonempty_str(str(worktree_path), "worktree_path")
    base = _require_nonempty_str(base_commit, "base_commit")
    resolved_configuration = dict(configuration or {"strategy": strategy})
    if resolved_configuration.get("strategy") != strategy:
        raise ContractError("envelope strategy does not match its configuration")
    if task_card["base_commit"] != base:
        raise ContractError(
            f"task card base mismatch: expected {task_card['base_commit']!r}, got {base!r}"
        )
    mandatory = _normalize_content_list(mandatory_content, "mandatory_content")
    optional = _normalize_content_list(optional_content, "optional_content")
    omitted = omitted_content or []
    if not isinstance(omitted, list) or any(not isinstance(item, str) or not item for item in omitted):
        raise ContractError("omitted_content must be a list of nonempty strings")
    record: dict[str, Any] = {
        "schema": ENVELOPE_SCHEMA,
        "lane_id": lane,
        "run_id": run,
        "decision_id": validate_decision_id,
        "strategy": strategy,
        "configuration": resolved_configuration,
        "configuration_digest": sha256_hex(resolved_configuration),
        "task_card_digest": task_card["content_hash"],
        "objective_id": plan["objective_id"],
        "route": plan["route"],
        "plan_id": plan["plan_id"],
        "plan_state": plan["state"],
        "plan_digest": plan["content_hash"],
        "base_commit": base,
        "worktree_path": worktree,
        "mandatory_content": mandatory,
        "optional_content": optional,
        "mandatory_digest": _digest_content_list(mandatory),
        "optional_digest": _digest_content_list(optional),
        "delivery": {
            "mandatory": [item["id"] for item in mandatory],
            "optional": [item["id"] for item in optional],
            "omitted": list(omitted),
        },
        "dispatch_state": "finalized",
        "created_at": created_at or utc_now(),
    }
    record["content_hash"] = content_hash(record)
    validate_envelope(
        record,
        task_card=task_card,
        plan=plan,
        lane_id=lane,
        run_id=run,
        base_commit=base,
        worktree_path=worktree,
    )
    return record


def validate_envelope(
    record: Mapping[str, Any],
    *,
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    lane_id: str,
    run_id: str,
    base_commit: str,
    worktree_path: str,
    decision_id: str | None = None,
) -> None:
    validate_record(record, ENVELOPE_SCHEMA)
    validate_task_card(task_card)
    validate_task_plan_binding(task_card, plan)
    validate_plan(plan, expected_state="accepted")
    expected = {
        "lane_id": lane_id,
        "run_id": run_id,
        "task_card_digest": task_card["content_hash"],
        "objective_id": plan["objective_id"],
        "route": plan["route"],
        "plan_id": plan["plan_id"],
        "plan_state": plan["state"],
        "plan_digest": plan["content_hash"],
        "base_commit": base_commit,
        "worktree_path": str(worktree_path),
        "decision_id": decision_id
        or make_decision(
            task_card,
            plan,
            strategy=str(record.get("strategy")),
            configuration=record.get("configuration"),
        )["decision_id"],
    }
    for field, expected_value in expected.items():
        actual = record.get(field)
        if actual != expected_value:
            raise ContractError(f"envelope {field.replace('_', ' ')} mismatch: expected {expected_value!r}, got {actual!r}")
    strategy = _require_nonempty_str(record.get("strategy"), "strategy")
    configuration = record.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ContractError("envelope configuration must be an object")
    if configuration.get("strategy") != strategy:
        raise ContractError("envelope strategy does not match its configuration")
    if record.get("configuration_digest") != sha256_hex(configuration):
        raise ContractError("envelope configuration digest mismatch")
    mandatory = _normalize_content_list(record.get("mandatory_content"), "mandatory_content")
    optional = _normalize_content_list(record.get("optional_content"), "optional_content")
    if record.get("mandatory_digest") != _digest_content_list(mandatory):
        raise ContractError("mandatory digest mismatch")
    if record.get("optional_digest") != _digest_content_list(optional):
        raise ContractError("optional digest mismatch")
    delivery = record.get("delivery")
    if not isinstance(delivery, Mapping):
        raise ContractError("delivery must be an object")
    expected_delivery = {
        "mandatory": [item["id"] for item in mandatory],
        "optional": [item["id"] for item in optional],
        "omitted": delivery.get("omitted", []),
    }
    if dict(delivery) != expected_delivery:
        raise ContractError("delivery trace does not match the rendered content")
    if record.get("dispatch_state") != "finalized":
        raise ContractError("envelope is not finalized")


def make_operation(
    *,
    kind: str,
    envelope: Mapping[str, Any],
    status: str = "pending",
    observed_invocation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_kind = _require_nonempty_str(kind, "kind")
    validate_envelope_identity = _require_nonempty_str(envelope.get("content_hash"), "envelope digest")
    operation_id = sha256_hex({"kind": selected_kind, "envelope_digest": validate_envelope_identity})
    record: dict[str, Any] = {
        "schema": OPERATION_SCHEMA,
        "operation_id": operation_id,
        "kind": selected_kind,
        "decision_id": envelope["decision_id"],
        "envelope_digest": validate_envelope_identity,
        "run_id": envelope["run_id"],
        "status": status,
        "observed_invocation": dict(observed_invocation) if observed_invocation is not None else None,
        "created_at": utc_now(),
    }
    record["content_hash"] = content_hash(record)
    validate_operation(record)
    return record


def validate_operation(record: Mapping[str, Any]) -> None:
    validate_record(record, OPERATION_SCHEMA)
    for field in ("operation_id", "kind", "decision_id", "envelope_digest", "run_id", "status"):
        _require_nonempty_str(record.get(field), field)
    if record["status"] not in OPERATION_STATUSES:
        raise ContractError(f"unknown operation status: {record['status']!r}")
    if record["status"] == "delivered" and record.get("observed_invocation") is None:
        raise ContractError("delivered operation requires an observed invocation")
    if record.get("observed_invocation") is not None and not isinstance(record["observed_invocation"], Mapping):
        raise ContractError("observed_invocation must be an object or null")


def make_outcome(
    *,
    decision_id: str,
    plan_id: str,
    plan_digest: str,
    status: str,
    evidence_digest: str,
    linked_run_id: str,
    task_card_digest: str,
    objective_id: str,
) -> dict[str, Any]:
    decision = _require_nonempty_str(decision_id, "decision_id")
    plan = _require_nonempty_str(plan_id, "plan_id")
    digest = _require_nonempty_str(plan_digest, "plan_digest")
    outcome_status = _require_nonempty_str(status, "status")
    if outcome_status not in OUTCOME_STATUSES:
        raise ContractError(f"unknown outcome status: {outcome_status!r}")
    evidence = _require_nonempty_str(evidence_digest, "evidence_digest")
    run = _require_nonempty_str(linked_run_id, "linked_run_id")
    task_digest = _require_nonempty_str(task_card_digest, "task_card_digest")
    objective = _require_nonempty_str(objective_id, "objective_id")
    outcome_id = sha256_hex(
        {
            "decision_id": decision,
            "plan_id": plan,
            "plan_digest": digest,
            "status": outcome_status,
            "evidence_digest": evidence,
            "linked_run_id": run,
            "task_card_digest": task_digest,
            "objective_id": objective,
        }
    )
    record: dict[str, Any] = {
        "schema": OUTCOME_SCHEMA,
        "outcome_id": outcome_id,
        "decision_id": decision,
        "plan_id": plan,
        "plan_digest": digest,
        "status": outcome_status,
        "evidence_digest": evidence,
        "linked_run_id": run,
        "task_card_digest": task_digest,
        "objective_id": objective,
        "observed_at": utc_now(),
    }
    record["content_hash"] = content_hash(record)
    validate_outcome(record)
    return record


def validate_outcome(record: Mapping[str, Any]) -> None:
    validate_record(record, OUTCOME_SCHEMA)
    for field in (
        "outcome_id", "decision_id", "task_card_digest", "objective_id",
        "plan_id", "plan_digest", "status", "evidence_digest", "linked_run_id",
    ):
        _require_nonempty_str(record.get(field), field)
    if record["status"] not in OUTCOME_STATUSES:
        raise ContractError(f"unknown outcome status: {record['status']!r}")


def normalize_experience_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    """Validate the exact application/project/namespace/owner boundary."""

    if not isinstance(scope, Mapping):
        raise ContractError("experience scope must be an object")
    return {
        field: _require_nonempty_str(scope.get(field), f"scope.{field}")
        for field in ("application", "project", "namespace", "owner")
    }


def _normalize_string_refs(
    values: Iterable[str], field: str, *, sort_values: bool = True
) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ContractError(f"{field} must be a list of strings")
    result: list[str] = []
    for value in values:
        normalized = _require_nonempty_str(value, field)
        if normalized in result:
            raise ContractError(f"{field} must not contain duplicate values")
        result.append(normalized)
    return sorted(result) if sort_values else result


def _validate_outcome_provenance(
    *,
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    decision: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> None:
    validate_task_plan_binding(task_card, plan)
    validate_decision(decision)
    validate_outcome(outcome)
    expected = {
        "decision_id": decision["decision_id"],
        "task_card_digest": task_card["content_hash"],
        "objective_id": plan["objective_id"],
        "plan_id": plan["plan_id"],
        "plan_digest": plan["content_hash"],
    }
    for field, value in expected.items():
        if outcome.get(field) != value:
            raise ContractError(f"outcome {field.replace('_', ' ')} does not match provenance")
    if decision["task_card_digest"] != task_card["content_hash"]:
        raise ContractError("decision task card digest does not match provenance")
    if decision["objective_id"] != plan["objective_id"]:
        raise ContractError("decision objective does not match provenance")
    if decision["plan_id"] != plan["plan_id"]:
        raise ContractError("decision plan does not match provenance")
    if decision["plan_digest"] != plan["content_hash"]:
        raise ContractError("decision plan digest does not match provenance")
    if plan.get("state") != "accepted":
        raise ContractError("reviewed trajectory requires an accepted plan")


def make_review_receipt(
    *,
    review_id: str,
    outcome: Mapping[str, Any],
    decision: Mapping[str, Any],
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    reviewed_by: str,
    evidence_refs: Iterable[str],
    state: str = "reviewed",
    protected_source_refs: Iterable[str] = (),
) -> dict[str, Any]:
    """Bind existing review evidence to one exact terminal product outcome.

    This record retains references to ROOT/harness review evidence.  It does
    not create a replacement review workflow or treat a worker narrative as
    authoritative evidence.
    """

    _validate_outcome_provenance(
        task_card=task_card, plan=plan, decision=decision, outcome=outcome
    )
    selected_review_id = _require_nonempty_str(review_id, "review_id")
    reviewer = _require_nonempty_str(reviewed_by, "reviewed_by")
    if reviewer != "ROOT":
        raise ContractError("only ROOT may review a reusable trajectory")
    if state not in REVIEW_STATES:
        raise ContractError(f"unknown review state: {state!r}")
    references = _normalize_string_refs(evidence_refs, "evidence_refs")
    if not references:
        raise ContractError("evidence_refs must not be empty")
    protected = _normalize_string_refs(
        protected_source_refs, "protected_source_refs"
    )
    receipt_id = sha256_hex(
        {
            "review_id": selected_review_id,
            "outcome_id": outcome["outcome_id"],
            "decision_id": decision["decision_id"],
            "task_card_digest": task_card["content_hash"],
            "objective_id": plan["objective_id"],
            "run_id": outcome["linked_run_id"],
            "plan_id": plan["plan_id"],
            "plan_digest": plan["content_hash"],
            "reviewed_by": reviewer,
            "state": state,
            "evidence_refs": references,
            "protected_source_refs": protected,
        }
    )
    record: dict[str, Any] = {
        "schema": REVIEW_RECEIPT_SCHEMA,
        "review_receipt_id": receipt_id,
        "review_id": selected_review_id,
        "outcome_id": outcome["outcome_id"],
        "decision_id": decision["decision_id"],
        "task_card_digest": task_card["content_hash"],
        "objective_id": plan["objective_id"],
        "run_id": outcome["linked_run_id"],
        "plan_id": plan["plan_id"],
        "plan_digest": plan["content_hash"],
        "reviewed_by": reviewer,
        "state": state,
        "evidence_refs": references,
        "protected_source_refs": protected,
        "reviewed_at": outcome["observed_at"],
    }
    record["content_hash"] = content_hash(record)
    validate_review_receipt(record)
    return record


def validate_review_receipt(record: Mapping[str, Any]) -> None:
    validate_record(record, REVIEW_RECEIPT_SCHEMA)
    for field in (
        "review_receipt_id",
        "review_id",
        "outcome_id",
        "decision_id",
        "task_card_digest",
        "objective_id",
        "run_id",
        "plan_id",
        "plan_digest",
        "reviewed_by",
        "state",
        "reviewed_at",
    ):
        _require_nonempty_str(record.get(field), field)
    if record["state"] not in REVIEW_STATES:
        raise ContractError(f"unknown review state: {record['state']!r}")
    if record["reviewed_by"] != "ROOT":
        raise ContractError("only ROOT may review a reusable trajectory")
    evidence_refs = record.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise ContractError("evidence_refs must be a nonempty list")
    normalized_evidence_refs = _normalize_string_refs(evidence_refs, "evidence_refs")
    if evidence_refs != normalized_evidence_refs:
        raise ContractError("evidence_refs must use canonical ordering")
    protected = record.get("protected_source_refs")
    if not isinstance(protected, list):
        raise ContractError("protected_source_refs must be a list")
    normalized_protected = _normalize_string_refs(protected, "protected_source_refs")
    if protected != normalized_protected:
        raise ContractError("protected_source_refs must use canonical ordering")
    expected_receipt_id = sha256_hex(
        {
            "review_id": record["review_id"],
            "outcome_id": record["outcome_id"],
            "decision_id": record["decision_id"],
            "task_card_digest": record["task_card_digest"],
            "objective_id": record["objective_id"],
            "run_id": record["run_id"],
            "plan_id": record["plan_id"],
            "plan_digest": record["plan_digest"],
            "reviewed_by": record["reviewed_by"],
            "state": record["state"],
            "evidence_refs": normalized_evidence_refs,
            "protected_source_refs": normalized_protected,
        }
    )
    if record["review_receipt_id"] != expected_receipt_id:
        raise ContractError("review receipt identity does not match its provenance")


def make_reviewed_trajectory(
    *,
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    decision: Mapping[str, Any],
    outcome: Mapping[str, Any],
    review_receipt: Mapping[str, Any],
    scope: Mapping[str, Any],
    raw_evidence: str,
    failed_hypotheses: Iterable[str] = (),
    protected_source_refs: Iterable[str] = (),
) -> dict[str, Any]:
    """Create one immutable reviewed trajectory from linked terminal evidence."""

    _validate_outcome_provenance(
        task_card=task_card, plan=plan, decision=decision, outcome=outcome
    )
    validate_review_receipt(review_receipt)
    for field, expected in {
        "outcome_id": outcome["outcome_id"],
        "decision_id": decision["decision_id"],
        "task_card_digest": task_card["content_hash"],
        "objective_id": plan["objective_id"],
        "run_id": outcome["linked_run_id"],
        "plan_id": plan["plan_id"],
        "plan_digest": plan["content_hash"],
    }.items():
        if review_receipt.get(field) != expected:
            raise ContractError(
                f"review receipt {field.replace('_', ' ')} does not match terminal outcome"
            )
    if outcome["status"] == "PASS":
        trajectory_status = "reviewed_success"
    elif outcome["status"] in {"FAIL", "BLOCKED"}:
        trajectory_status = "reviewed_failure"
    else:
        raise ContractError("terminal UNKNOWN outcome cannot become reviewed experience")
    normalized_scope = normalize_experience_scope(scope)
    evidence = _require_nonempty_str(raw_evidence, "raw_evidence")
    hypotheses = _normalize_string_refs(
        failed_hypotheses, "failed_hypotheses", sort_values=False
    )
    protected = _normalize_string_refs(
        protected_source_refs, "protected_source_refs"
    )
    all_protected = _normalize_string_refs(
        [*review_receipt["protected_source_refs"], *protected],
        "protected_source_refs",
    )
    trajectory_id = sha256_hex(
        {
            "outcome_id": outcome["outcome_id"],
            "review_receipt_id": review_receipt["review_receipt_id"],
            "scope": normalized_scope,
        }
    )
    record: dict[str, Any] = {
        "schema": REVIEWED_TRAJECTORY_SCHEMA,
        "trajectory_id": trajectory_id,
        "outcome_id": outcome["outcome_id"],
        "task_card_digest": task_card["content_hash"],
        "task_text": task_card["task"],
        "objective_id": plan["objective_id"],
        "decision_id": decision["decision_id"],
        "run_id": outcome["linked_run_id"],
        "accepted_plan_id": plan["plan_id"],
        "accepted_plan_digest": plan["content_hash"],
        "route": plan["route"],
        "scope": normalized_scope,
        "scope_digest": sha256_hex(normalized_scope),
        "status": trajectory_status,
        "review_receipt_id": review_receipt["review_receipt_id"],
        "review_id": review_receipt["review_id"],
        "review_receipt_digest": review_receipt["content_hash"],
        "review_state": review_receipt["state"],
        "reviewed_by": review_receipt["reviewed_by"],
        "reviewed_at": review_receipt["reviewed_at"],
        "evidence_refs": list(review_receipt["evidence_refs"]),
        "protected_source_refs": all_protected,
        "failed_hypotheses": hypotheses,
        "raw_evidence": evidence,
        "evidence_digest": outcome["evidence_digest"],
        "recorded_at": outcome["observed_at"],
    }
    record["content_hash"] = content_hash(record)
    validate_reviewed_trajectory(record)
    return record


def validate_reviewed_trajectory(record: Mapping[str, Any]) -> None:
    validate_record(record, REVIEWED_TRAJECTORY_SCHEMA)
    for field in (
        "trajectory_id",
        "outcome_id",
        "task_card_digest",
        "task_text",
        "objective_id",
        "decision_id",
        "run_id",
        "accepted_plan_id",
        "accepted_plan_digest",
        "route",
        "scope_digest",
        "status",
        "review_receipt_id",
        "review_id",
        "review_receipt_digest",
        "review_state",
        "reviewed_by",
        "reviewed_at",
        "raw_evidence",
        "evidence_digest",
        "recorded_at",
    ):
        _require_nonempty_str(record.get(field), field)
    if record["route"] not in ROUTES:
        raise ContractError(f"unknown route: {record['route']!r}")
    if record["status"] not in REVIEWED_TRAJECTORY_STATUSES:
        raise ContractError(f"unknown reviewed trajectory status: {record['status']!r}")
    if record["review_state"] not in REVIEW_STATES:
        raise ContractError(f"unknown review state: {record['review_state']!r}")
    scope = normalize_experience_scope(record.get("scope"))
    if record["scope_digest"] != sha256_hex(scope):
        raise ContractError("reviewed trajectory scope digest mismatch")
    expected_trajectory_id = sha256_hex(
        {
            "outcome_id": record["outcome_id"],
            "review_receipt_id": record["review_receipt_id"],
            "scope": scope,
        }
    )
    if record["trajectory_id"] != expected_trajectory_id:
        raise ContractError("reviewed trajectory identity does not match its provenance")
    for field in ("evidence_refs", "protected_source_refs", "failed_hypotheses"):
        values = record.get(field)
        if not isinstance(values, list):
            raise ContractError(f"{field} must be a list")
        _normalize_string_refs(values, field, sort_values=field != "failed_hypotheses")


def make_experience_ingestion(
    *,
    trajectory: Mapping[str, Any],
    destination: str,
    session_id: str,
    payload_digest: str,
) -> dict[str, Any]:
    """Create the durable intent for one optional EverOS representation write."""

    validate_reviewed_trajectory(trajectory)
    selected_destination = _require_nonempty_str(destination, "destination")
    selected_session = _require_nonempty_str(session_id, "session_id")
    selected_payload_digest = _require_nonempty_str(payload_digest, "payload_digest")
    ingestion_id = sha256_hex(
        {
            "trajectory_id": trajectory["trajectory_id"],
            "destination": selected_destination,
            "session_id": selected_session,
        }
    )
    record: dict[str, Any] = {
        "schema": EXPERIENCE_INGESTION_SCHEMA,
        "ingestion_id": ingestion_id,
        "trajectory_id": trajectory["trajectory_id"],
        "scope": dict(trajectory["scope"]),
        "scope_digest": trajectory["scope_digest"],
        "destination": selected_destination,
        "session_id": selected_session,
        "payload_digest": selected_payload_digest,
        "status": "pending",
        "case_ids": [],
        "error": None,
        "created_at": trajectory["recorded_at"],
    }
    record["content_hash"] = content_hash(record)
    validate_experience_ingestion(record)
    return record


def validate_experience_ingestion(record: Mapping[str, Any]) -> None:
    validate_record(record, EXPERIENCE_INGESTION_SCHEMA)
    for field in (
        "ingestion_id",
        "trajectory_id",
        "scope_digest",
        "destination",
        "session_id",
        "payload_digest",
        "status",
        "created_at",
    ):
        _require_nonempty_str(record.get(field), field)
    scope = normalize_experience_scope(record.get("scope"))
    if record["scope_digest"] != sha256_hex(scope):
        raise ContractError("experience ingestion scope digest mismatch")
    expected_ingestion_id = sha256_hex(
        {
            "trajectory_id": record["trajectory_id"],
            "destination": record["destination"],
            "session_id": record["session_id"],
        }
    )
    if record["ingestion_id"] != expected_ingestion_id:
        raise ContractError("experience ingestion identity does not match its receipt")
    if record["status"] not in EXPERIENCE_INGESTION_STATUSES:
        raise ContractError(f"unknown experience ingestion status: {record['status']!r}")
    case_ids = record.get("case_ids")
    if not isinstance(case_ids, list):
        raise ContractError("case_ids must be a list")
    _normalize_string_refs(case_ids, "case_ids")
    if record.get("error") is not None and not isinstance(record["error"], str):
        raise ContractError("experience ingestion error must be a string or null")
    if record["status"] == "confirmed" and not case_ids:
        raise ContractError("confirmed experience ingestion requires case_ids")


def make_case_receipt(
    *,
    trajectory: Mapping[str, Any],
    ingestion: Mapping[str, Any],
    source_case: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one exact returned EverOS case to its reviewed local receipt."""

    validate_reviewed_trajectory(trajectory)
    validate_experience_ingestion(ingestion)
    if ingestion["trajectory_id"] != trajectory["trajectory_id"]:
        raise ContractError("case receipt ingestion does not belong to trajectory")
    if ingestion["scope_digest"] != trajectory["scope_digest"]:
        raise ContractError("case receipt ingestion scope does not match trajectory")
    if not isinstance(source_case, Mapping):
        raise ContractError("source_case must be an object")
    case_id = _require_nonempty_str(source_case.get("id"), "source_case.id")
    case_receipt_id = sha256_hex(
        {
            "case_id": case_id,
            "trajectory_id": trajectory["trajectory_id"],
            "scope_digest": trajectory["scope_digest"],
        }
    )
    record: dict[str, Any] = {
        "schema": CASE_RECEIPT_SCHEMA,
        "case_receipt_id": case_receipt_id,
        "case_id": case_id,
        "trajectory_id": trajectory["trajectory_id"],
        "ingestion_id": ingestion["ingestion_id"],
        "scope": dict(trajectory["scope"]),
        "scope_digest": trajectory["scope_digest"],
        "review_receipt_id": trajectory["review_receipt_id"],
        "review_receipt_digest": trajectory["review_receipt_digest"],
        "source_case": dict(source_case),
        "source_case_digest": sha256_hex(source_case),
        "created_at": trajectory["recorded_at"],
    }
    record["content_hash"] = content_hash(record)
    validate_case_receipt(record)
    return record


def validate_case_receipt(record: Mapping[str, Any]) -> None:
    validate_record(record, CASE_RECEIPT_SCHEMA)
    for field in (
        "case_receipt_id",
        "case_id",
        "trajectory_id",
        "ingestion_id",
        "scope_digest",
        "review_receipt_id",
        "review_receipt_digest",
        "source_case_digest",
        "created_at",
    ):
        _require_nonempty_str(record.get(field), field)
    scope = normalize_experience_scope(record.get("scope"))
    if record["scope_digest"] != sha256_hex(scope):
        raise ContractError("case receipt scope digest mismatch")
    source_case = record.get("source_case")
    if not isinstance(source_case, Mapping):
        raise ContractError("source_case must be an object")
    if source_case.get("id") != record["case_id"]:
        raise ContractError("case receipt source case id mismatch")
    if record["source_case_digest"] != sha256_hex(source_case):
        raise ContractError("case receipt source case digest mismatch")
    expected_case_receipt_id = sha256_hex(
        {
            "case_id": record["case_id"],
            "trajectory_id": record["trajectory_id"],
            "scope_digest": record["scope_digest"],
        }
    )
    if record["case_receipt_id"] != expected_case_receipt_id:
        raise ContractError("case receipt identity does not match its provenance")


def make_generated_skill_candidate(
    *,
    scope: Mapping[str, Any],
    skill_id: str,
    content: str,
    source_cases: Iterable[Mapping[str, Any]],
    metadata: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Record generated EverOS guidance as non-authoritative evidence."""

    normalized_scope = normalize_experience_scope(scope)
    selected_skill_id = _require_nonempty_str(skill_id, "skill_id")
    selected_content = _require_nonempty_str(content, "content")
    seen_case_ids: set[str] = set()
    normalized_sources: list[dict[str, str]] = []
    for source in source_cases:
        if not isinstance(source, Mapping):
            raise ContractError("generated skill source_cases must contain objects")
        normalized = {
            field: _require_nonempty_str(source.get(field), f"source_case.{field}")
            for field in (
                "case_id",
                "case_receipt_id",
                "trajectory_id",
                "review_receipt_id",
                "review_receipt_digest",
            )
        }
        if normalized["case_id"] in seen_case_ids:
            raise ContractError("generated skill source case ids must resolve uniquely")
        seen_case_ids.add(normalized["case_id"])
        normalized_sources.append(normalized)
    if not normalized_sources:
        raise ContractError("generated skill requires at least one source case")
    normalized_sources.sort(key=lambda source: source["case_id"])
    content_digest = sha256_hex({"content": selected_content})
    candidate_id = sha256_hex(
        {
            "scope": normalized_scope,
            "skill_id": selected_skill_id,
            "content_digest": content_digest,
            "source_cases": normalized_sources,
            "state": "proposed",
        }
    )
    record: dict[str, Any] = {
        "schema": GENERATED_SKILL_SCHEMA,
        "candidate_id": candidate_id,
        "skill_id": selected_skill_id,
        "origin": "generated",
        "state": "proposed",
        "scope": normalized_scope,
        "scope_digest": sha256_hex(normalized_scope),
        "content": selected_content,
        "content_digest": content_digest,
        "source_cases": normalized_sources,
        "metadata": dict(metadata or {}),
        "created_at": created_at or utc_now(),
    }
    record["content_hash"] = content_hash(record)
    validate_generated_skill_candidate(record)
    return record


def validate_generated_skill_candidate(record: Mapping[str, Any]) -> None:
    validate_record(record, GENERATED_SKILL_SCHEMA)
    for field in (
        "candidate_id",
        "skill_id",
        "origin",
        "state",
        "scope_digest",
        "content",
        "content_digest",
        "created_at",
    ):
        _require_nonempty_str(record.get(field), field)
    if record["origin"] != "generated":
        raise ContractError("generated skill candidate origin must remain generated")
    if record["state"] not in GENERATED_SKILL_STATES:
        raise ContractError("generated skill candidate must remain proposed")
    scope = normalize_experience_scope(record.get("scope"))
    if record["scope_digest"] != sha256_hex(scope):
        raise ContractError("generated skill candidate scope digest mismatch")
    if record["content_digest"] != sha256_hex({"content": record["content"]}):
        raise ContractError("generated skill candidate content digest mismatch")
    source_cases = record.get("source_cases")
    if not isinstance(source_cases, list) or not source_cases:
        raise ContractError("generated skill candidate requires source cases")
    if not isinstance(record.get("metadata"), Mapping):
        raise ContractError("generated skill candidate metadata must be an object")
    seen_case_ids: set[str] = set()
    normalized_sources: list[dict[str, str]] = []
    for source in source_cases:
        if not isinstance(source, Mapping):
            raise ContractError("generated skill source_cases must contain objects")
        case_id = _require_nonempty_str(source.get("case_id"), "source_case.case_id")
        if case_id in seen_case_ids:
            raise ContractError("generated skill source case ids must resolve uniquely")
        seen_case_ids.add(case_id)
        normalized_sources.append(
            {
                "case_id": case_id,
                "case_receipt_id": _require_nonempty_str(
                    source.get("case_receipt_id"), "source_case.case_receipt_id"
                ),
                "trajectory_id": _require_nonempty_str(
                    source.get("trajectory_id"), "source_case.trajectory_id"
                ),
                "review_receipt_id": _require_nonempty_str(
                    source.get("review_receipt_id"), "source_case.review_receipt_id"
                ),
                "review_receipt_digest": _require_nonempty_str(
                    source.get("review_receipt_digest"),
                    "source_case.review_receipt_digest",
                ),
            }
        )
    canonical_sources = sorted(normalized_sources, key=lambda source: source["case_id"])
    if source_cases != canonical_sources:
        raise ContractError("generated skill source cases must use canonical ordering")
    expected_candidate_id = sha256_hex(
        {
            "scope": scope,
            "skill_id": record["skill_id"],
            "content_digest": record["content_digest"],
            "source_cases": canonical_sources,
            "state": record["state"],
        }
    )
    if record["candidate_id"] != expected_candidate_id:
        raise ContractError("generated skill candidate identity does not match its provenance")


def make_skill_approval(
    *,
    approval_id: str,
    candidate: Mapping[str, Any],
    issuer: str,
    recipients: Iterable[str],
    approved_at: str,
) -> dict[str, Any]:
    """Bind explicit trusted approval to one immutable generated candidate."""

    validate_generated_skill_candidate(candidate)
    selected_approval_id = _require_nonempty_str(approval_id, "approval_id")
    selected_issuer = _require_nonempty_str(issuer, "issuer")
    if selected_issuer != "ROOT":
        raise ContractError("only ROOT may approve a generated skill candidate")
    selected_recipients = _normalize_string_refs(recipients, "recipients")
    if not selected_recipients:
        raise ContractError("approval recipients must not be empty")
    selected_approved_at = _require_nonempty_str(approved_at, "approved_at")
    record: dict[str, Any] = {
        "schema": SKILL_APPROVAL_SCHEMA,
        "approval_id": selected_approval_id,
        "candidate_id": candidate["candidate_id"],
        "skill_id": candidate["skill_id"],
        "origin": candidate["origin"],
        "scope": dict(candidate["scope"]),
        "scope_digest": candidate["scope_digest"],
        "content_digest": candidate["content_digest"],
        "issuer": selected_issuer,
        "recipients": selected_recipients,
        "source_cases": [dict(item) for item in candidate["source_cases"]],
        "approved_at": selected_approved_at,
    }
    record["content_hash"] = content_hash(record)
    validate_skill_approval(record)
    return record


def validate_skill_approval(record: Mapping[str, Any]) -> None:
    validate_record(record, SKILL_APPROVAL_SCHEMA)
    for field in (
        "approval_id",
        "candidate_id",
        "skill_id",
        "origin",
        "scope_digest",
        "content_digest",
        "issuer",
        "approved_at",
    ):
        _require_nonempty_str(record.get(field), field)
    if record["origin"] != "generated":
        raise ContractError("approval cannot rewrite generated origin")
    if record["issuer"] != "ROOT":
        raise ContractError("only ROOT may approve a generated skill candidate")
    scope = normalize_experience_scope(record.get("scope"))
    if record["scope_digest"] != sha256_hex(scope):
        raise ContractError("approval scope digest mismatch")
    recipients = record.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        raise ContractError("approval recipients must be a nonempty list")
    _normalize_string_refs(recipients, "recipients")
    source_cases = record.get("source_cases")
    if not isinstance(source_cases, list) or not source_cases:
        raise ContractError("approval source cases must be a nonempty list")
    seen_case_ids: set[str] = set()
    for source in source_cases:
        if not isinstance(source, Mapping):
            raise ContractError("approval source cases must contain objects")
        case_id = _require_nonempty_str(source.get("case_id"), "source_case.case_id")
        if case_id in seen_case_ids:
            raise ContractError("approval source case ids must resolve uniquely")
        seen_case_ids.add(case_id)
        for field in (
            "case_receipt_id",
            "trajectory_id",
            "review_receipt_id",
            "review_receipt_digest",
        ):
            _require_nonempty_str(source.get(field), f"source_case.{field}")


__all__ = [
    "ContractError",
    "TASK_CARD_SCHEMA",
    "MEMORY_HANDOFF_SCHEMA",
    "PLAN_SCHEMA",
    "DECISION_SCHEMA",
    "ENVELOPE_SCHEMA",
    "OPERATION_SCHEMA",
    "OUTCOME_SCHEMA",
    "REVIEW_RECEIPT_SCHEMA",
    "REVIEWED_TRAJECTORY_SCHEMA",
    "EXPERIENCE_INGESTION_SCHEMA",
    "CASE_RECEIPT_SCHEMA",
    "GENERATED_SKILL_SCHEMA",
    "SKILL_APPROVAL_SCHEMA",
    "APC_REQUEST_SCHEMA",
    "APC_RESULT_SCHEMA",
    "PLAN_STATES",
    "ROUTES",
    "OUTCOME_STATUSES",
    "OPERATION_STATUSES",
    "REVIEW_STATES",
    "REVIEWED_TRAJECTORY_STATUSES",
    "GENERATED_SKILL_STATES",
    "EXPERIENCE_INGESTION_STATUSES",
    "canonical_json",
    "sha256_hex",
    "content_hash",
    "validate_record",
    "make_task_card",
    "validate_task_card",
    "make_memory_handoff",
    "validate_memory_handoff",
    "make_plan",
    "validate_plan",
    "revise_plan",
    "validate_task_plan_binding",
    "make_decision",
    "validate_decision",
    "make_envelope",
    "validate_envelope",
    "make_operation",
    "validate_operation",
    "make_outcome",
    "validate_outcome",
    "normalize_experience_scope",
    "make_review_receipt",
    "validate_review_receipt",
    "make_reviewed_trajectory",
    "validate_reviewed_trajectory",
    "make_experience_ingestion",
    "validate_experience_ingestion",
    "make_case_receipt",
    "validate_case_receipt",
    "make_generated_skill_candidate",
    "validate_generated_skill_candidate",
    "make_skill_approval",
    "validate_skill_approval",
]

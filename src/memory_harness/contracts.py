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
APC_REQUEST_SCHEMA = "apc-request/v1"
APC_RESULT_SCHEMA = "apc-result/v1"

PLAN_STATES = frozenset({"candidate", "accepted", "proposed", "fresh"})
ROUTES = frozenset({"ordinary", "problem_focused", "deeper"})
OUTCOME_STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "UNKNOWN"})
OPERATION_STATUSES = frozenset({"pending", "ambiguous", "delivered"})


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


__all__ = [
    "ContractError",
    "TASK_CARD_SCHEMA",
    "MEMORY_HANDOFF_SCHEMA",
    "PLAN_SCHEMA",
    "DECISION_SCHEMA",
    "ENVELOPE_SCHEMA",
    "OPERATION_SCHEMA",
    "OUTCOME_SCHEMA",
    "APC_REQUEST_SCHEMA",
    "APC_RESULT_SCHEMA",
    "PLAN_STATES",
    "ROUTES",
    "OUTCOME_STATUSES",
    "OPERATION_STATUSES",
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
]

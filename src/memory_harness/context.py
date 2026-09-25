"""Final context: bind mandatory state and eligible optional memory safely.

The finalizer renders exact mandatory task/plan/base/route/checkpoint/security
state first, packs or omits whole optional items inside the configured allowance,
and binds the rendered content and delivery trace to a ready execution identity.
Ready means context-delivered, not that a worker was invoked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import contracts
from .config import PreparationLimits
from .privacy import (
    PrivacyPolicy,
    detect_secrets,
    guard_mandatory,
    sanitize_payload,
)


class ContextError(RuntimeError):
    """The final execution context cannot be finalized safely."""


class MandatoryOverflowError(ContextError):
    """Mandatory task/plan state exceeds the known usable context."""


class PlanAffectingFreshnessError(ContextError):
    """A plan-affecting optional item is no longer fresh; ROOT must decide."""


class OptionalItemError(ContextError):
    """An optional item is malformed and cannot be packed."""


ROLE_SEPARATION = contracts.FINAL_CONTEXT_SECURITY


@dataclass(frozen=True)
class FinalizedContext:
    envelope: dict[str, Any]
    context: dict[str, Any]
    omissions: list[dict[str, Any]]


def _render_size(items: Sequence[Mapping[str, Any]]) -> int:
    return len(contracts.canonical_json([dict(item) for item in items]))


def _normalize_optional_item(item: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise OptionalItemError("optional items must be objects")
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise OptionalItemError("optional items require a nonempty id")
    normalized = dict(item)
    normalized.setdefault("kind", "memory")
    normalized.setdefault("origin", "optional")
    return normalized


def finalize_context(
    *,
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    decision_id: str,
    lane_id: str,
    run_id: str,
    worktree_path: str,
    base_commit: str,
    strategy: str,
    configuration: Mapping[str, Any],
    checkpoint: str,
    execution_role: str,
    invocation_target: str,
    recipient: str,
    mandatory_content: Iterable[Mapping[str, Any]],
    optional_items: Iterable[Mapping[str, Any]] = (),
    omitted: Iterable[str | Mapping[str, Any]] = (),
    privacy_policy: PrivacyPolicy | None = None,
    limits: PreparationLimits | None = None,
    freshness_check: Callable[[Mapping[str, Any]], bool] | None = None,
) -> FinalizedContext:
    """Render one exact, safe, dispatch-ready execution context."""

    policy = privacy_policy or PrivacyPolicy()
    resolved = limits or PreparationLimits()
    contracts.validate_task_card(task_card)
    contracts.validate_plan(plan, expected_state="accepted")
    contracts.validate_task_plan_binding(task_card, plan)
    if task_card["base_commit"] != base_commit:
        raise ContextError("task card base does not match the finalization base")

    mandatory = [dict(item) for item in mandatory_content]
    for item in mandatory:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ContextError("mandatory content entries require a nonempty id")
    contracts._validate_final_mandatory(
        mandatory, task=task_card["task"], plan_content=plan["content"],
        base_commit=base_commit, route=plan["route"], checkpoint=checkpoint,
    )
    guard_mandatory(mandatory, policy)

    selected: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    for raw in omitted:
        item = _normalize_optional_item(raw) if isinstance(raw, Mapping) else {
            "id": str(raw), "kind": "unavailable", "origin": "preparation", "content": None,
        }
        if detect_secrets({key: value for key, value in item.items() if key != "content"}, policy):
            raise OptionalItemError("optional provenance contains prohibited secret")
        descriptor = contracts._optional_descriptor(item)
        selected.append(descriptor)
        omissions.append({**descriptor, "reason": "omitted before finalization"})
    optional: list[dict[str, Any]] = []
    selected_by_id: dict[str, dict[str, Any]] = {}
    for raw in optional_items:
        item = _normalize_optional_item(raw)
        item_id = item["id"]
        if detect_secrets({key: value for key, value in item.items() if key != "content"}, policy):
            raise OptionalItemError("optional provenance contains prohibited secret")
        affects_plan = bool(item.get("plan_affecting", False))
        descriptor = contracts._optional_descriptor(item)
        selected_by_id[item_id] = descriptor
        selected.append(descriptor)
        item.pop("plan_affecting", None)
        fresh = True
        if freshness_check is not None:
            fresh = bool(freshness_check(item))
        if not fresh:
            if affects_plan:
                raise PlanAffectingFreshnessError(
                    f"plan-affecting optional item is no longer fresh: {item_id}"
                )
            omissions.append({**descriptor, "reason": "not fresh"})
            continue
        if detect_secrets(item, policy):
            omissions.append({**descriptor, "reason": "prohibited secret"})
            continue
        optional.append(sanitize_payload(item, policy))

    mandatory_render = _render_size(mandatory)
    if mandatory_render > resolved.context_char_limit:
        raise MandatoryOverflowError(
            "mandatory task and accepted-plan state exceeds the known usable context"
        )
    remaining = resolved.context_char_limit - mandatory_render
    packed: list[dict[str, Any]] = []
    for item in optional:
        size = _render_size([item])
        if size > remaining:
            omissions.append({**selected_by_id[item["id"]], "reason": "exceeds the optional allowance"})
            continue
        remaining -= size
        packed.append(item)

    trace = {
        "selected": selected,
        "packed": [contracts._optional_descriptor(item) for item in packed],
        "omitted": omissions,
        "context_delivered": [contracts._optional_descriptor(item) for item in packed],
    }
    context = contracts.make_finalized_context(
        lane_id=lane_id,
        run_id=run_id,
        decision_id=decision_id,
        task=task_card["task"],
        task_card_digest=task_card["content_hash"],
        objective_id=plan["objective_id"],
        route=plan["route"],
        plan_id=plan["plan_id"],
        plan_revision=plan["revision"],
        accepted_by=plan["accepted_by"],
        accepted_plan_content=plan["content"],
        plan_digest=plan["content_hash"],
        base_commit=base_commit,
        worktree_path=str(worktree_path),
        checkpoint=checkpoint,
        strategy=strategy,
        configuration=configuration,
        execution_role=execution_role,
        invocation_target=invocation_target,
        recipient=recipient,
        mandatory_content=mandatory,
        optional_content=packed,
        delivery_trace=trace,
        role_separation=ROLE_SEPARATION,
        freshness={"mode": "rechecked" if freshness_check is not None else "not-required"},
        context_limit=resolved.context_char_limit,
    )
    envelope = contracts.make_envelope(
        task_card=task_card, plan=plan, decision_id=decision_id,
        lane_id=lane_id, run_id=run_id, worktree_path=str(worktree_path),
        base_commit=base_commit, mandatory_content=mandatory, optional_content=packed,
        omitted_content=[entry["id"] for entry in omissions],
        strategy=strategy, configuration=configuration, final_context=context,
    )
    validate_final_context(
        context, envelope=envelope, task_card=task_card, plan=plan,
        lane_id=lane_id, run_id=run_id, base_commit=base_commit,
        worktree_path=str(worktree_path), checkpoint=checkpoint,
        execution_role=execution_role, invocation_target=invocation_target,
        recipient=recipient,
    )
    return FinalizedContext(envelope=envelope, context=context, omissions=omissions)


def validate_final_context(
    context: Mapping[str, Any],
    *,
    envelope: Mapping[str, Any],
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    lane_id: str,
    run_id: str,
    base_commit: str,
    worktree_path: str,
    checkpoint: str | None = None,
    execution_role: str | None = None,
    invocation_target: str | None = None,
    recipient: str | None = None,
) -> None:
    """Validate the finalized context against the actual dispatch target.

    Every mismatch ? a different task, base, plan, decision, run, or worktree ?
    fails here as one explicit ``ContextError`` before any launch is attempted.
    """

    try:
        contracts.validate_finalized_context(context)
        contracts.validate_envelope(
            envelope,
            task_card=task_card,
            plan=plan,
            lane_id=lane_id,
            run_id=run_id,
            base_commit=base_commit,
            worktree_path=str(worktree_path),
            require_final_context=True,
        )
    except contracts.ContractError as exc:
        raise ContextError(f"finalized envelope does not match the dispatch target: {exc}") from exc
    expected = {
        "lane_id": lane_id,
        "run_id": run_id,
        "decision_id": envelope["decision_id"],
        "task": task_card["task"],
        "task_card_digest": task_card["content_hash"],
        "objective_id": plan["objective_id"],
        "route": plan["route"],
        "plan_id": plan["plan_id"],
        "plan_revision": plan["revision"],
        "accepted_by": plan["accepted_by"],
        "accepted_plan_content": plan["content"],
        "plan_digest": plan["content_hash"],
        "base_commit": base_commit,
        "worktree_path": str(worktree_path),
        "mandatory_digest": envelope["mandatory_digest"],
        "optional_digest": envelope["optional_digest"],
        "mandatory_content": envelope["mandatory_content"],
        "optional_content": envelope["optional_content"],
        "delivery_trace": envelope.get("delivery_trace"),
        "bound_record": envelope.get("final_context"),
        "context_id": envelope.get("final_context_id"),
        "integrity": envelope.get("final_context_integrity"),
    }
    for field in ("checkpoint", "execution_role", "invocation_target", "recipient"):
        expected[field] = envelope.get(field)
    for field, value in (
        ("checkpoint", checkpoint), ("execution_role", execution_role),
        ("invocation_target", invocation_target), ("recipient", recipient),
    ):
        if value is not None and envelope.get(field) != value:
            raise ContextError(f"finalized context {field.replace('_', ' ')} does not match the dispatch target")
    for field, value in expected.items():
        if field == "bound_record":
            if context != value:
                raise ContextError("finalized context record does not match its envelope")
            continue
        if context.get(field) != value:
            raise ContextError(
                f"finalized context {field.replace('_', ' ')} does not match the dispatch target"
            )


def recheck_plan_affecting(
    items: Iterable[Mapping[str, Any]],
    checker: Callable[[Mapping[str, Any]], bool],
) -> list[dict[str, Any]]:
    """Recheck plan-affecting guidance; raise when the plan depends on it."""

    stale: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise OptionalItemError("optional items must be objects")
        if not bool(item.get("plan_affecting", False)):
            continue
        if not bool(checker(item)):
            stale.append(dict(item))
    if stale:
        raise PlanAffectingFreshnessError(
            "plan-affecting optional guidance is no longer fresh: "
            + ", ".join(str(item.get("id")) for item in stale)
        )
    return stale


__all__ = [
    "ContextError",
    "FinalizedContext",
    "MandatoryOverflowError",
    "OptionalItemError",
    "PlanAffectingFreshnessError",
    "ROLE_SEPARATION",
    "finalize_context",
    "recheck_plan_affecting",
    "validate_final_context",
]

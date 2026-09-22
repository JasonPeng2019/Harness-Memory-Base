"""Final context: bind mandatory state and eligible optional memory safely.

The finalizer renders mandatory task/accepted-plan state first, packs or omits
whole optional items inside the configured allowance, rechecks plan-affecting
freshness immediately before finalization, and binds one exact integrity over
the actual task, objective, repository base, accepted plan, rendered items, and
role separation.
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


ROLE_SEPARATION = {
    "execution_role": "worker",
    "control_plane": "excluded",
    "memory_authority": "none",
    "may_approve": False,
    "may_publish": False,
    "may_execute_parent": False,
}


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
    mandatory_content: Iterable[Mapping[str, Any]],
    optional_items: Iterable[Mapping[str, Any]] = (),
    omitted: Iterable[str] = (),
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
    guard_mandatory(mandatory, policy)

    omissions: list[dict[str, Any]] = [
        {"id": str(item), "reason": "omitted before finalization"} for item in omitted
    ]
    optional: list[dict[str, Any]] = []
    for raw in optional_items:
        item = _normalize_optional_item(raw)
        item_id = item["id"]
        affects_plan = bool(item.pop("plan_affecting", False))
        fresh = True
        if freshness_check is not None:
            fresh = bool(freshness_check(item))
        if not fresh:
            if affects_plan:
                raise PlanAffectingFreshnessError(
                    f"plan-affecting optional item is no longer fresh: {item_id}"
                )
            omissions.append({"id": item_id, "reason": "not fresh"})
            continue
        if detect_secrets(item, policy):
            omissions.append({"id": item_id, "reason": "prohibited secret"})
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
            omissions.append({"id": item["id"], "reason": "exceeds the optional allowance"})
            continue
        remaining -= size
        packed.append(item)

    envelope = contracts.make_envelope(
        task_card=task_card,
        plan=plan,
        decision_id=decision_id,
        lane_id=lane_id,
        run_id=run_id,
        worktree_path=str(worktree_path),
        base_commit=base_commit,
        mandatory_content=mandatory,
        optional_content=packed,
        omitted_content=[str(entry["id"]) for entry in omissions],
        strategy=strategy,
        configuration=configuration,
    )
    context = contracts.make_finalized_context(
        lane_id=lane_id,
        run_id=run_id,
        decision_id=decision_id,
        task_card_digest=task_card["content_hash"],
        objective_id=plan["objective_id"],
        route=plan["route"],
        plan_id=plan["plan_id"],
        plan_digest=plan["content_hash"],
        base_commit=base_commit,
        worktree_path=str(worktree_path),
        strategy=strategy,
        configuration=configuration,
        mandatory_items=mandatory,
        optional_items=packed,
        omitted=[str(entry["id"]) for entry in omissions],
        role_separation=ROLE_SEPARATION,
        freshness={"mode": "rechecked" if freshness_check is not None else "not-required"},
        context_limit=resolved.context_char_limit,
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
) -> None:
    """Validate the finalized context against the actual dispatch target.

    Every mismatch ? a different task, base, plan, decision, run, or worktree ?
    fails here as one explicit ``ContextError`` before any launch is attempted.
    """

    contracts.validate_finalized_context(context)
    try:
        contracts.validate_envelope(
            envelope,
            task_card=task_card,
            plan=plan,
            lane_id=lane_id,
            run_id=run_id,
            base_commit=base_commit,
            worktree_path=str(worktree_path),
        )
    except contracts.ContractError as exc:
        raise ContextError(f"finalized envelope does not match the dispatch target: {exc}") from exc
    expected = {
        "lane_id": lane_id,
        "run_id": run_id,
        "decision_id": envelope["decision_id"],
        "task_card_digest": task_card["content_hash"],
        "objective_id": plan["objective_id"],
        "route": plan["route"],
        "plan_id": plan["plan_id"],
        "plan_digest": plan["content_hash"],
        "base_commit": base_commit,
        "worktree_path": str(worktree_path),
        "mandatory_digest": envelope["mandatory_digest"],
        "optional_digest": envelope["optional_digest"],
    }
    for field, value in expected.items():
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

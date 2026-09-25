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
    authorize_recipient,
    detect_secrets,
    guard_mandatory,
    guard_worker_authority,
    has_worker_authority,
    sanitize_payload,
)


class ContextError(RuntimeError):
    """The final execution context cannot be finalized safely."""


class MandatoryOverflowError(ContextError):
    """Mandatory task/plan state exceeds the known usable context."""


class PlanAffectingFreshnessError(ContextError):
    """A dependent source or approved representation changed; ROOT must replan."""


class OptionalItemError(ContextError):
    """An optional item is malformed and cannot be packed."""


class TaskCredentialChannelError(ContextError):
    """A required task-only worker channel is unavailable or unsafe."""


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


def _optional_worker_authority(item: Mapping[str, Any]) -> bool:
    """Inspect delivered content and provenance, excluding trusted procedure proof."""
    content = item.get("content")
    if item.get("kind") == "procedure" and isinstance(content, Mapping) and isinstance(content.get("procedure"), Mapping):
        content = content["procedure"].get("behavior")
    provenance = {key: value for key, value in item.items()
                  if key not in {"content", "compact_approval", "compact_representation", "final_recheck", "frozen_contract"}}
    compact = item.get("compact_representation")
    compact_content = compact.get("content") if isinstance(compact, Mapping) else None
    return (has_worker_authority(content) or has_worker_authority(compact_content)
            or has_worker_authority(provenance))


def _mandatory_worker_authority(
    items: Iterable[Mapping[str, Any]], *, task: str, plan_content: Any,
    base_commit: str, route: str, checkpoint: str,
) -> bool:
    expected = {"task": task, "accepted-plan": plan_content, "base": base_commit,
                "route": route, "checkpoint": checkpoint, "security": ROLE_SEPARATION}
    for item in items:
        identifier = item.get("id")
        if (isinstance(identifier, str) and identifier in expected
                and item.get("kind", identifier) == identifier
                and item.get("content") == expected[identifier]):
            # Exact domain-owned baseline is checked by the final context
            # contract; only extra worker-bound item fields need this guard.
            if has_worker_authority({key: value for key, value in item.items() if key != "content"}):
                return True
        elif has_worker_authority(item):
            return True
    return False


def _compact_variant(item: Mapping[str, Any]) -> dict[str, Any] | None:
    representation = item.get("compact_representation")
    approval = item.get("compact_approval")
    if representation is None and approval is None:
        return None
    content = item.get("content")
    if not isinstance(content, Mapping) or not all(
        isinstance(content.get(field), Mapping) for field in ("procedure", "approval")
    ) or not isinstance(representation, Mapping) or not isinstance(approval, Mapping):
        return None
    procedure = content["procedure"]
    full_approval = content["approval"]
    try:
        contracts.validate_procedure_approval(full_approval, procedure=procedure)
        contracts.validate_procedure_compact_representation(representation, procedure=procedure)
        contracts.validate_procedure_compact_approval(
            approval, procedure=procedure, full_approval=full_approval,
            representation=representation,
        )
    except contracts.ContractError:
        return None
    if item.get("revision_id") != procedure["revision_id"]:
        return None
    compact = {key: value for key, value in item.items()
               if key not in {"compact_representation", "compact_approval"}}
    compact.update({
        "content": representation["content"],
        "delivery_representation": "compact",
        "full_content_digest": contracts.sha256_hex(content),
        "full_procedure_digest": procedure["content_hash"],
        "full_approval_digest": full_approval["content_hash"],
        "compact_representation": dict(representation),
        "compact_approval": dict(approval),
    })
    return compact


def _plan_depends_on(
    plan: Mapping[str, Any], item: Mapping[str, Any], *, trusted_dependency: bool = False,
) -> bool:
    if trusted_dependency:
        return True
    source = plan.get("source")
    if not isinstance(source, Mapping):
        return False
    if source.get("candidate_id") == item.get("id"):
        return True
    logical_id = item.get("logical_id")
    revision_id = item.get("revision_id")
    if (isinstance(source.get("source_id"), str) and source["source_id"]
            and source.get("source_id") == item.get("source_id")
            and source.get("revision_id") == revision_id
            and isinstance(revision_id, str) and revision_id):
        return True
    return bool(logical_id and revision_id and (
        (source.get("logical_id") == logical_id and source.get("revision_id") == revision_id)
        or (source.get("template_id") == logical_id and source.get("template_version") == revision_id)
        or (source.get("procedure_id") == logical_id and source.get("procedure_revision_id") == revision_id)
    ))


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
    task_credential_channel: Any = None,
    mandatory_content: Iterable[Mapping[str, Any]],
    optional_items: Iterable[Mapping[str, Any]] = (),
    selected_provenance: Mapping[str, Mapping[str, Any]] | None = None,
    omitted: Iterable[str | Mapping[str, Any]] = (),
    privacy_policy: PrivacyPolicy | None = None,
    limits: PreparationLimits | None = None,
    freshness_check: Callable[[Mapping[str, Any]], bool] | None = None,
    source_recheck: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    source_owner_recheck: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> FinalizedContext:
    """Render one exact, safe, dispatch-ready execution context."""

    policy = privacy_policy or PrivacyPolicy()
    resolved = limits or PreparationLimits()
    recipient = authorize_recipient(recipient, policy)
    mandatory = [dict(item) for item in mandatory_content]
    guard_mandatory({
        "task_card": task_card, "plan": plan, "configuration": configuration,
        "mandatory": mandatory, "destination": {
            "lane_id": lane_id, "run_id": run_id, "worktree_path": worktree_path,
            "base_commit": base_commit, "checkpoint": checkpoint,
            "execution_role": execution_role, "invocation_target": invocation_target,
            "recipient": recipient,
        },
    }, policy)
    guard_worker_authority(configuration)
    if _mandatory_worker_authority(mandatory, task=task_card["task"],
                                   plan_content=plan["content"], base_commit=base_commit,
                                   route=plan["route"], checkpoint=checkpoint):
        guard_worker_authority(mandatory)
    contracts.validate_task_card(task_card)
    contracts.validate_plan(plan, expected_state="accepted")
    contracts.validate_task_plan_binding(task_card, plan)
    if task_card["base_commit"] != base_commit:
        raise ContextError("task card base does not match the finalization base")

    try:
        contracts.validate_task_credential_channel(
            task_credential_channel, task_card_digest=task_card["content_hash"],
            recipient=recipient, requirement=task_card.get("task_credential_requirement"),
        )
    except contracts.ContractError as exc:
        raise TaskCredentialChannelError(str(exc)) from None
    if task_credential_channel is not None and detect_secrets(task_credential_channel, policy):
        raise TaskCredentialChannelError("required task credential channel contains a protected credential")
    for item in mandatory:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ContextError("mandatory content entries require a nonempty id")
    contracts._validate_final_mandatory(
        mandatory, task=task_card["task"], plan_content=plan["content"],
        base_commit=base_commit, route=plan["route"], checkpoint=checkpoint,
    )
    guard_mandatory(mandatory, policy)

    selected: list[dict[str, Any] | None] = []
    omissions: list[dict[str, Any]] = []

    def omit_sensitive(item: Mapping[str, Any]) -> None:
        # The whole item, including its provenance, is outside the delivered
        # context. Only an independently safe identity and fixed reason remain.
        item_id = item.get("id")
        if (not isinstance(item_id, str) or not item_id.strip()
                or item_id != item_id.strip()
                or any(ord(char) < 32 for char in item_id)
                or detect_secrets(item_id, policy) or has_worker_authority(item_id)):
            item_id = f"omitted-sensitive-{len(selected)}"
        descriptor = contracts._optional_descriptor({
            "id": item_id, "kind": "unavailable", "origin": "privacy", "content": None,
        })
        selected.append(descriptor)
        omissions.append({**descriptor, "reason": "prohibited credential"})

    for raw in omitted:
        item = _normalize_optional_item(raw) if isinstance(raw, Mapping) else {
            "id": str(raw), "kind": "unavailable", "origin": "preparation", "content": None,
        }
        sensitive_provenance = detect_secrets(
            {key: value for key, value in item.items() if key != "content"}, policy,
        ) or _optional_worker_authority(item)
        if _plan_depends_on(plan, item):
            if sensitive_provenance:
                raise PlanAffectingFreshnessError(
                    "selected source was omitted before final recheck; ROOT must replan"
                )
            raise PlanAffectingFreshnessError(
                f"selected source {item.get('source_id', item['id'])} revision "
                f"{item.get('revision_id', 'unknown')} was omitted before final recheck; ROOT must replan"
            )
        for claim in ("plan_affecting", "frozen_contract", "compact_representation", "compact_approval"):
            item.pop(claim, None)
        if sensitive_provenance:
            omit_sensitive(item)
            continue
        descriptor = contracts._optional_descriptor(item)
        selected.append(descriptor)
        omissions.append({**descriptor, "reason": "omitted before finalization"})
    optional: list[tuple[dict[str, Any], dict[str, Any], bool, int]] = []
    selected_by_id: dict[str, dict[str, Any]] = {}
    for raw in optional_items:
        item = _normalize_optional_item(raw)
        item_id = item["id"]
        source = (selected_provenance or {}).get(item_id, {})
        if not isinstance(source, Mapping) or any(
            key in {"id", "content", "payload"} or (key in item and item[key] != value)
            for key, value in source.items()
        ):
            raise OptionalItemError("selected provenance conflicts with rendered optional content")
        selected_item = {**item, **source}
        # These raw fields can identify a candidate, but cannot authorize a
        # frozen readback, compact approval, or plan dependency.
        for claim in ("frozen_contract", "compact_representation", "compact_approval", "plan_affecting"):
            selected_item.pop(claim, None)
        requires_source = selected_item.get("kind") == "procedure" or selected_item.get("freshness") == "frozen"
        missing_identity = requires_source and not selected_item.get("source_id")
        if requires_source:
            selected_item.setdefault("source_id", item_id)
        missing_identity = missing_identity or (
            bool(selected_item.get("source_id"))
            and (not isinstance(selected_item.get("revision_id"), str)
                 or not selected_item.get("revision_id"))
        )
        if missing_identity:
            selected_item["revision_id"] = "unversioned"
        affects_plan = _plan_depends_on(
            plan, selected_item, trusted_dependency=source.get("plan_affecting") is True,
        )
        if affects_plan:
            selected_item["plan_affecting"] = True
        if (detect_secrets({key: value for key, value in selected_item.items()
                            if key != "content"}, policy)
                or _optional_worker_authority({key: value for key, value in selected_item.items()
                                               if key != "content"})):
            if affects_plan:
                raise PlanAffectingFreshnessError(
                    "plan-affecting optional guidance contains a prohibited credential; ROOT must replan"
                )
            omit_sensitive(selected_item)
            continue
        if detect_secrets(item.get("content"), policy) or _optional_worker_authority(item):
            descriptor = contracts._optional_descriptor(selected_item)
            selected.append(descriptor)
            if affects_plan:
                raise PlanAffectingFreshnessError(
                    "plan-affecting optional guidance contains a prohibited credential; ROOT must replan"
                )
            omissions.append({**descriptor, "reason": "prohibited credential"})
            continue
        # A live source must return one exact observation. The Boolean legacy
        # checker remains a freshness-only fallback for caller-owned items.
        original = dict(selected_item)
        recheck: Mapping[str, Any] | None = None
        if missing_identity:
            recheck = contracts.make_final_source_recheck(item=original, status="ineligible")
        elif selected_item.get("freshness") == "frozen" and source_owner_recheck is None:
            # A legacy generic callback can recheck live guidance, but cannot
            # establish an immutable source-owner contract for frozen guidance.
            recheck = contracts.make_final_source_recheck(item=original, status="unavailable")
        elif (source_owner_recheck is not None or source_recheck is not None) and selected_item.get("source_id"):
            try:
                callback = source_owner_recheck or source_recheck
                observed = callback(dict(original))
            except Exception:
                observed = contracts.make_final_source_recheck(item=original, status="unavailable")
            owner_proof: Mapping[str, Any] = {}
            if source_owner_recheck is not None and isinstance(observed, Mapping) and "recheck" in observed:
                owner_proof = observed.get("owner_proof", {})
                recheck = observed["recheck"]
            else:
                recheck = observed
            if not isinstance(recheck, Mapping):
                if selected_item.get("freshness") != "frozen":
                    raise OptionalItemError(f"final source recheck is invalid for {item_id}")
                recheck = contracts.make_final_source_recheck(item=original, status="ineligible")
            try:
                contracts.validate_final_source_recheck(recheck, item=original)
            except (contracts.ContractError, TypeError, ValueError):
                if selected_item.get("freshness") != "frozen":
                    raise OptionalItemError("final source recheck is invalid") from None
                recheck = contracts.make_final_source_recheck(item=original, status="ineligible")
            if selected_item.get("freshness") == "frozen" and recheck["status"] == "frozen":
                expected = {"source_id": original["source_id"],
                            "revision_id": original["revision_id"],
                            "content_digest": contracts.sha256_hex(original["content"])}
                if not isinstance(owner_proof, Mapping) or owner_proof.get("frozen_contract") != expected:
                    recheck = contracts.make_final_source_recheck(
                        item=original, status="ineligible",
                        observed_revision_id=recheck["observed_revision_id"],
                        observed_content_digest=recheck["observed_content_digest"],
                    )
                else:
                    selected_item["frozen_contract"] = expected
            elif selected_item.get("freshness") == "frozen" and recheck["status"] == "eligible":
                recheck = contracts.make_final_source_recheck(
                    item=original, status="ineligible",
                    observed_revision_id=recheck["observed_revision_id"],
                    observed_content_digest=recheck["observed_content_digest"],
                )
            if recheck["status"] in {"eligible", "frozen"} and isinstance(owner_proof, Mapping):
                representation = owner_proof.get("compact_representation")
                approval = owner_proof.get("compact_approval")
                if representation is not None and approval is not None:
                    selected_item["compact_representation"] = representation
                    selected_item["compact_approval"] = approval
                    item["compact_representation"] = representation
                    item["compact_approval"] = approval
        elif selected_item.get("source_id"):
            recheck = contracts.make_final_source_recheck(item=original, status="unavailable")
        if recheck is not None:
            selected_item["final_recheck"] = dict(recheck)
        if selected_item.get("kind") == "historical_evidence":
            selected_item["authority"] = "historical_evidence_only"
            item["authority"] = "historical_evidence_only"
            historical = {"procedural_authority": False, "evidence": item.get("content")}
            selected_item["content"] = historical
            item["content"] = historical
        if (detect_secrets({key: value for key, value in selected_item.items()
                            if key != "content"}, policy)
                or _optional_worker_authority({key: value for key, value in selected_item.items()
                                               if key != "content"})):
            if affects_plan:
                raise PlanAffectingFreshnessError(
                    "plan-affecting optional guidance contains a prohibited credential; ROOT must replan"
                )
            omit_sensitive(selected_item)
            continue
        if detect_secrets(item, policy) or _optional_worker_authority(item):
            descriptor = contracts._optional_descriptor(selected_item)
            selected.append(descriptor)
            if affects_plan:
                raise PlanAffectingFreshnessError(
                    "plan-affecting optional guidance contains a prohibited credential; ROOT must replan"
                )
            omissions.append({**descriptor, "reason": "prohibited credential"})
            continue
        fresh = bool(freshness_check(original)) if recheck is None and freshness_check is not None else True
        status = recheck["status"] if recheck is not None else ("eligible" if fresh else "stale")
        descriptor = contracts._optional_descriptor(selected_item)
        slot = len(selected)
        selected.append(descriptor)
        selected_by_id[item_id] = descriptor
        if status not in {"eligible", "frozen"}:
            if affects_plan:
                raise PlanAffectingFreshnessError(
                    f"selected source {selected_item.get('source_id', item_id)} "
                    f"revision {selected_item.get('revision_id', 'unknown')} changed ({status}); ROOT must replan"
                )
            omissions.append({**descriptor, "reason": f"final recheck: {status}" if recheck is not None else "not fresh"})
            continue
        for claim in ("plan_affecting", "frozen_contract", "compact_representation", "compact_approval"):
            if claim not in {"compact_representation", "compact_approval"} or claim not in selected_item:
                item.pop(claim, None)
        optional.append((sanitize_payload(item, policy), selected_item, affects_plan, slot))

    mandatory_render = _render_size(mandatory)
    if mandatory_render > resolved.context_char_limit:
        raise MandatoryOverflowError(
            "mandatory task and accepted-plan state exceeds the known usable context"
        )
    remaining = resolved.context_char_limit - mandatory_render
    packed: list[dict[str, Any]] = []
    for item, selected_item, affects_plan, slot in optional:
        compact = None
        if item.get("kind") == "procedure":
            # Compact evidence is retained only if the full body cannot fit.
            full = {key: value for key, value in item.items()
                    if key not in {"compact_representation", "compact_approval"}}
        else:
            full = item
        size = _render_size([full])
        if size > remaining:
            if item.get("kind") == "procedure":
                compact = _compact_variant(item)
            if compact is not None and _render_size([compact]) <= remaining:
                full = compact
                size = _render_size([full])
                selected_item = {**selected_item, **compact}
                if affects_plan:
                    selected_item["plan_affecting"] = True
                descriptor = contracts._optional_descriptor(selected_item)
                selected[slot] = descriptor
                selected_by_id[item["id"]] = descriptor
            else:
                if affects_plan:
                    raise PlanAffectingFreshnessError(
                        f"selected source {item.get('source_id', item['id'])} revision "
                        f"{item.get('revision_id', 'unknown')} cannot fit as approved guidance; ROOT must replan"
                    )
                reason = (
                    "no approved procedure representation fits the optional allowance"
                    if item.get("kind") == "procedure"
                    else "exceeds the optional allowance"
                )
                omissions.append({**selected_by_id[item["id"]], "reason": reason})
                continue
        remaining -= size
        packed.append(full)

    trace = {
        "selected": selected,
        "packed": [contracts._packed_descriptor(item, selected_by_id[item["id"]]) for item in packed],
        "omitted": omissions,
        "context_delivered": [contracts._packed_descriptor(item, selected_by_id[item["id"]]) for item in packed],
    }
    guard_mandatory(trace, policy)
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
        freshness={"mode": "rechecked" if any((freshness_check, source_recheck, source_owner_recheck)) else "not-required"},
        context_limit=resolved.context_char_limit,
        task_credential_channel=task_credential_channel,
    )
    envelope = contracts.make_envelope(
        task_card=task_card, plan=plan, decision_id=decision_id,
        lane_id=lane_id, run_id=run_id, worktree_path=str(worktree_path),
        base_commit=base_commit, mandatory_content=mandatory, optional_content=packed,
        omitted_content=[entry["id"] for entry in omissions],
        strategy=strategy, configuration=configuration, final_context=context,
    )
    guard_mandatory({"context": context, "envelope": envelope}, policy)
    validate_final_context(
        context, envelope=envelope, task_card=task_card, plan=plan,
        lane_id=lane_id, run_id=run_id, base_commit=base_commit,
        worktree_path=str(worktree_path), checkpoint=checkpoint,
        execution_role=execution_role, invocation_target=invocation_target,
        recipient=recipient,
        privacy_policy=policy,
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
    privacy_policy: PrivacyPolicy | None = None,
) -> None:
    """Validate the finalized context against the actual dispatch target.

    Every mismatch ? a different task, base, plan, decision, run, or worktree ?
    fails here as one explicit ``ContextError`` before any launch is attempted.
    """

    policy = privacy_policy or PrivacyPolicy()
    authorize_recipient(context.get("recipient"), policy)
    if recipient is not None:
        authorize_recipient(recipient, policy)
    if detect_secrets({"context": context, "envelope": envelope}, policy):
        raise ContextError("finalized context contains a prohibited credential")
    guard_worker_authority(context.get("configuration"))
    guard_worker_authority(envelope.get("configuration"))
    if _mandatory_worker_authority(context.get("mandatory_content", ()), task=task_card["task"],
                                   plan_content=plan["content"], base_commit=base_commit,
                                   route=plan["route"], checkpoint=checkpoint):
        raise ContextError("finalized context contains prohibited worker authority")
    if _mandatory_worker_authority(envelope.get("mandatory_content", ()), task=task_card["task"],
                                   plan_content=plan["content"], base_commit=base_commit,
                                   route=plan["route"], checkpoint=checkpoint):
        raise ContextError("finalized envelope contains prohibited worker authority")
    if any(_optional_worker_authority(item) for item in context.get("optional_content", ())):
        raise ContextError("finalized context contains prohibited worker authority")
    if any(_optional_worker_authority(item) for item in envelope.get("optional_content", ())):
        raise ContextError("finalized envelope contains prohibited worker authority")
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
    except contracts.ContractError:
        raise ContextError("finalized envelope does not match the dispatch target") from None
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
        raise PlanAffectingFreshnessError("plan-affecting optional guidance is no longer fresh")
    return stale


__all__ = [
    "ContextError",
    "FinalizedContext",
    "MandatoryOverflowError",
    "OptionalItemError",
    "TaskCredentialChannelError",
    "PlanAffectingFreshnessError",
    "ROLE_SEPARATION",
    "finalize_context",
    "recheck_plan_affecting",
    "validate_final_context",
]

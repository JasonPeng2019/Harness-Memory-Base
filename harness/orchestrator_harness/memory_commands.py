"""ROOT-only public memory administration commands.

The CLI in :mod:`operator_launch` passes identities and JSON values here, but
never passes a database, product configuration, worktree, credential, issuer,
claimant, or remote-service location.  Those authority-bearing values are
derived from the active harness and its trusted ``memory-product-config.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import (
    memory_admin,
    memory_handoff,
    memory_product_config,
    processes,
    product_composition,
    resume,
)
from .config import (
    compute_config_identity,
    find_harness_root,
    load_config,
    load_resource_manifest,
)
from .epochs import current_epoch_path, read_current_epoch, read_epoch_state
from .core import read_json
from .lanes import find_active_lane
from .records import RecordLock, atomic_write_json

ADMIN_SCHEMA = memory_admin.ADMIN_RESULT_SCHEMA


class MemoryCommandError(RuntimeError):
    """Safe public command failure."""

    def __init__(self, code: str, message: str, *, action: str, next_step: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.action = action
        self.next_step = next_step

    def as_admin_result(self) -> dict[str, Any]:
        return {
            "schema": ADMIN_SCHEMA,
            "action": self.action,
            "status": "rejected",
            "evidence": {"code": self.code, "reason": self.safe_message},
            "next_step": self.next_step,
        }


@dataclass
class _Session:
    root: Path
    harness_config: Any
    product_config: memory_product_config.MemoryProductConfig
    store: Any
    policy: Any
    admin: memory_admin.MemoryAdmin
    lane_id: str | None = None
    epoch_id: str | None = None
    lane: dict[str, Any] | None = None
    task_card_path: Path | None = None

    def close(self) -> None:
        self.store.close()


def _public(result: Mapping[str, Any], *, ok: bool | None = None) -> dict[str, Any]:
    """Adapt an administration result to the launcher's common result shape."""

    status = str(result.get("status") or "rejected")
    evidence = result.get("evidence")
    rejected_error = (
        status == "rejected"
        and isinstance(evidence, Mapping)
        and isinstance(evidence.get("code"), str)
    )
    success = status != "unavailable" and not rejected_error if ok is None else ok
    action = str(result.get("action") or "memory")
    code_hint = evidence.get("code") if isinstance(evidence, Mapping) else None
    code = str(code_hint or f"MEMORY_{action}_{status}").upper().replace("-", "_")
    return {
        "ok": success,
        "code": code,
        "summary": f"memory {action}: {status}",
        "evidence_paths": [],
        "next_action": str(result.get("next_step") or "none"),
        **dict(result),
    }


def _result(action: str, status: str, evidence: Mapping[str, Any], next_step: str) -> dict[str, Any]:
    return {
        "schema": ADMIN_SCHEMA,
        "action": action,
        "status": status,
        "evidence": dict(evidence),
        "next_step": next_step,
    }


def _configuration() -> tuple[Path, Any, memory_product_config.MemoryProductConfig]:
    root = find_harness_root()
    harness_config = load_config(root)
    product_config = memory_product_config.load_memory_product_config(root)
    if harness_config.memory_product_config_identity != product_config.config_digest:
        raise MemoryCommandError(
            "stale_configuration",
            "the harness configuration does not bind the current memory product configuration",
            action="initialize",
            next_step="Shut down and set up a fresh epoch after the configuration change.",
        )
    marker = read_current_epoch(harness_config.runtime_root)
    if marker is None and current_epoch_path(harness_config.runtime_root).exists():
        raise MemoryCommandError(
            "invalid_epoch",
            "the active epoch marker is unreadable",
            action="initialize",
            next_step="Reconcile or shut down the active runtime before memory administration.",
        )
    if marker is not None:
        try:
            state = read_epoch_state(
                harness_config.runtime_root, str(marker["epoch_id"])
            )
            expected = compute_config_identity(
                harness_config, load_resource_manifest(root)
            )
        except Exception as exc:
            raise MemoryCommandError(
                "invalid_epoch",
                "the active epoch identity cannot be verified",
                action="initialize",
                next_step="Reconcile or shut down the active runtime before memory administration.",
            ) from exc
        if state.get("lifecycle") != "active" or state.get("config_identity") != expected:
            raise MemoryCommandError(
                "stale_configuration",
                "the active epoch does not bind the current trusted configuration",
                action="initialize",
                next_step="Shut down the old epoch before changing memory product configuration.",
            )
    return root, harness_config, product_config


def _snapshot_credential(configuration: memory_product_config.MemoryProductConfig) -> tuple[str, str, str]:
    """Return a non-request, configuration-bound authorization capability."""

    return ("memory-product-config/v1", configuration.config_digest, configuration.policy_generation)


def _derived_snapshot_path(configuration: memory_product_config.MemoryProductConfig, snapshot_id: str) -> Path:
    _identity(snapshot_id, "snapshot_id")
    return configuration.store_root / "snapshots" / snapshot_id


def _derived_restore_path(harness_config: Any, restore_id: str) -> Path:
    _identity(restore_id, "restore_id")
    return harness_config.runtime_root / "operator-only" / "memory-restores" / f"{restore_id}.sqlite3"


def _identity(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 33 for character in value)
    ):
        raise MemoryCommandError(
            "invalid_identity",
            f"{name} must be a canonical path-free identity",
            action="initialize",
            next_step=f"Supply one canonical exact {name}.",
        )
    return value


def _open_session(
    *,
    lane_id: str | None = None,
    snapshot_artifact: Path | None = None,
    restore_target: Path | None = None,
) -> _Session:
    from memory_harness.privacy import PrivacyPolicy
    from memory_harness.snapshot import SnapshotService
    from memory_harness.store import MemoryStore

    root, harness_config, configuration = _configuration()
    try:
        secrets = memory_product_config.read_known_secrets(configuration)
    except Exception as exc:
        raise MemoryCommandError(
            "privacy_configuration_unavailable",
            f"the trusted privacy policy is unavailable: {exc}",
            action="initialize",
            next_step="Initialize or explicitly rotate the secret state before administering memory.",
        ) from exc
    policy = PrivacyPolicy(known_secrets=secrets)
    epoch_id: str | None = None
    lane: dict[str, Any] | None = None
    task_path: Path | None = None
    if lane_id is not None:
        try:
            epoch_id, lane = find_active_lane(harness_config.runtime_root, lane_id)
        except Exception as exc:
            raise MemoryCommandError(
                "lane_not_found",
                f"the active lane does not exist: {lane_id}",
                action="initialize",
                next_step="Inspect the active epoch and use its exact lane identity.",
            ) from exc
        worktree = Path(str(lane.get("worktree_path") or ""))
        task_path = worktree / ".agent-workspace" / "task-card.json"
        store_path = memory_handoff.memory_paths(worktree)[0]
        if not worktree.is_dir() or not task_path.is_file() or not store_path.is_file():
            raise MemoryCommandError(
                "lane_memory_unavailable",
                "the active lane does not have complete durable memory administration state",
                action="initialize",
                next_step="Reconcile or bootstrap the exact enhanced lane before administration.",
            )
    else:
        store_path = product_composition.central_store_path(configuration)
    store = MemoryStore(store_path)
    store.initialize()
    credential = _snapshot_credential(configuration)
    snapshots_root = configuration.store_root / "snapshots"
    restores_root = harness_config.runtime_root / "operator-only" / "memory-restores"

    def authorize(action: str, scope: dict[str, str], path: Path, supplied: object) -> bool:
        if supplied != credential or scope != configuration.scope:
            return False
        exact = path.resolve()
        if action == "export":
            return exact == Path(store.path).resolve()
        if action == "inspect":
            return snapshot_artifact is not None and exact == snapshot_artifact.resolve()
        if action == "restore":
            return (
                restore_target is not None
                and exact == restore_target.resolve()
                and restore_target.parent.resolve() == restores_root.resolve()
            )
        return False

    snapshots = SnapshotService(authorizer=authorize)
    context = memory_admin.RootAdminContext(
        store=store,
        scope=configuration.scope,
        # The same trusted owner identity is used by normal product
        # composition.  Administration must never mint records under a second
        # hard-coded issuer that the normal search path will then reject.
        trusted_issuers=(configuration.owner,),
        privacy_policy=policy,
        snapshots=snapshots,
        # There is no sharing-policy field in memory-product-config/v1.  The
        # public surface therefore remains context-bound and default-deny.
        sharing_policy=None,
    )
    return _Session(
        root=root,
        harness_config=harness_config,
        product_config=configuration,
        store=store,
        policy=policy,
        admin=memory_admin.MemoryAdmin(context),
        lane_id=lane_id,
        epoch_id=epoch_id,
        lane=lane,
        task_card_path=task_path,
    )


def _expected_config(args: Any, configuration: memory_product_config.MemoryProductConfig, action: str) -> None:
    if getattr(args, "expected_config_digest", None) != configuration.config_digest:
        raise MemoryCommandError(
            "stale_identity",
            "the durable configuration digest no longer matches the operator observation",
            action=action,
            next_step="Read `memory config status` and submit a new exact operation.",
        )
    if getattr(args, "expected_policy_generation", None) != configuration.policy_generation:
        raise MemoryCommandError(
            "stale_identity",
            "the durable policy generation no longer matches the operator observation",
            action=action,
            next_step="Read `memory config status` and submit a new exact operation.",
        )


def _expected_lane(session: _Session, args: Any, action: str) -> None:
    assert session.lane is not None
    if session.lane.get("run_id") != args.expected_run_id:
        raise MemoryCommandError(
            "stale_identity",
            "the active lane run identity no longer matches the operator observation",
            action=action,
            next_step="Re-read the active lane and its exact pending plan.",
        )
    if session.lane.get("lifecycle") != "prepared" or session.lane.get("dispatchable") is not False:
        raise MemoryCommandError(
            "lane_not_pending",
            "plan administration requires an exact prepared, non-dispatchable lane",
            action=action,
            next_step="Use the lifecycle command appropriate to the lane's current state.",
        )


def _read_card(session: _Session) -> dict[str, Any]:
    assert session.task_card_path is not None
    try:
        card = read_json(session.task_card_path)
        memory_handoff.validate_task_card(card)
    except Exception as exc:
        raise MemoryCommandError(
            "invalid_durable_state",
            "the lane's durable task card is missing or invalid",
            action="plan",
            next_step="Reconcile the lane; do not copy a task card from another lane.",
        ) from exc
    scope = card.get("memory_scope", session.product_config.scope)
    try:
        memory_product_config.assert_exact_scope(session.product_config, scope)
    except Exception as exc:
        raise MemoryCommandError(
            "scope_denied",
            "the lane task card does not bind the configured four-field scope",
            action="plan",
            next_step="Use the exact deployment scope captured by trusted configuration.",
        ) from exc
    return card


def _disposition_plan(disposition: Mapping[str, Any]) -> dict[str, Any]:
    field = {
        "candidate_review": "preserved_plan",
        "direct_fill": "proposal",
        "apc_proposal": "proposal",
        "fresh": "fresh",
    }.get(str(disposition.get("branch")))
    plan = disposition.get(field) if field is not None else None
    if not isinstance(plan, Mapping):
        raise MemoryCommandError(
            "invalid_durable_state",
            "the exact disposition has no reviewable plan",
            action="plan",
            next_step="Inspect the durable preparation decision before retrying.",
        )
    return dict(plan)


def _rewrite_card(session: _Session, card: Mapping[str, Any], plan: Mapping[str, Any], *, checkpoint: str | None) -> dict[str, Any]:
    from memory_harness import contracts

    old = card.get("memory_handoff")
    if not isinstance(old, Mapping):
        raise MemoryCommandError(
            "invalid_durable_state", "the lane has no enhanced memory handoff",
            action="plan", next_step="Bootstrap an enhanced lane before plan administration."
        )
    handoff = contracts.make_memory_handoff(
        objective_id=str(old["objective_id"]),
        route=str(old["route"]),
        plan=plan,
        configuration=old.get("configuration"),
        checkpoint=checkpoint,
    )
    updated = dict(card)
    updated["memory_handoff"] = handoff
    updated["content_hash"] = contracts.content_hash(updated)
    contracts.validate_task_card(updated)
    return updated


def _card_matches_expected(card: Mapping[str, Any], args: Any) -> None:
    if card.get("content_hash") != args.expected_task_card_digest:
        raise MemoryCommandError(
            "stale_identity", "the durable task card digest changed",
            action="plan", next_step="Re-read the current exact lane task card."
        )
    handoff = card.get("memory_handoff")
    if not isinstance(handoff, Mapping):
        raise MemoryCommandError(
            "invalid_durable_state", "the lane has no enhanced memory handoff",
            action="plan", next_step="Bootstrap an enhanced lane before plan administration."
        )
    if handoff.get("objective_id") != args.expected_objective_id:
        raise MemoryCommandError(
            "stale_identity", "the durable handoff objective changed",
            action="plan", next_step="Re-read the current exact handoff."
        )
    current = handoff.get("plan")
    expected_plan_id = getattr(args, "expected_plan_id", None)
    expected_plan_digest = getattr(args, "expected_plan_digest", None)
    if expected_plan_id is not None and current is not None and (
        not isinstance(current, Mapping)
        or current.get("plan_id") != expected_plan_id
        or current.get("content_hash") != expected_plan_digest
    ):
        raise MemoryCommandError(
            "stale_identity", "the durable handoff plan changed",
            action="plan", next_step="Re-read the current exact handoff."
        )


def _run_plan(args: Any) -> dict[str, Any]:
    session = _open_session(lane_id=args.lane_id)
    try:
        _expected_lane(session, args, f"{args.plan_command}_plan")
        common = {
            "disposition_id": args.disposition_id,
            "expected_disposition_digest": args.expected_disposition_digest,
            "expected_decision_id": args.expected_decision_id,
            "expected_objective_id": args.expected_objective_id,
        }
        if args.plan_command == "inspect":
            assert session.task_card_path is not None
            with RecordLock(session.task_card_path):
                _card_matches_expected(_read_card(session), args)
                return _public(session.admin.inspect_pending_plan(**common))
        common.update({
            "expected_plan_id": args.expected_plan_id,
            "expected_plan_digest": args.expected_plan_digest,
        })
        assert session.task_card_path is not None
        with RecordLock(session.task_card_path):
            card = _read_card(session)
            _card_matches_expected(card, args)
            if args.plan_command in {"propose", "correct"}:
                content = args.plan_content
                if session.policy.detect(content):
                    raise MemoryCommandError(
                        "privacy_violation", "plan content contains configured secret material",
                        action=f"{args.plan_command}_plan",
                        next_step="Remove secret material and submit a new exact plan revision."
                    )
                if args.plan_command == "propose":
                    result = session.admin.propose_plan(
                        **common, proposed_plan_id=args.new_plan_id, proposed_content=content
                    )
                else:
                    result = session.admin.correct_plan(
                        **common, corrected_plan_id=args.new_plan_id, corrected_content=content
                    )
                disposition = session.store.get_plan_disposition(args.disposition_id)
                updated = _rewrite_card(session, card, _disposition_plan(disposition), checkpoint=None)
                atomic_write_json(session.task_card_path, updated)
                return _public(result)
            if args.plan_command == "reject":
                result = session.admin.reject_plan(
                    **common, reason=args.reason, fresh_plan_id=args.fresh_plan_id
                )
                disposition = session.store.get_plan_disposition(args.disposition_id)
                updated = _rewrite_card(session, card, _disposition_plan(disposition), checkpoint=None)
                atomic_write_json(session.task_card_path, updated)
                return _public(result)
            if args.plan_command == "accept":
                _identity(args.checkpoint, "checkpoint")
                result = session.admin.accept_plan(**common)
                accepted = result["evidence"]["accepted_plan"]
                updated = _rewrite_card(session, card, accepted, checkpoint=args.checkpoint)
            else:  # pragma: no cover - parser closes the action set
                raise ValueError(f"unknown memory plan command: {args.plan_command}")
        # The worktree card remains the exact pending-state witness until the
        # guarded native activation has finalized every new-run artifact.  The
        # accepted card is staged only in a ROOT-owned runtime directory; on
        # success activate_pending_plan atomically publishes it into the lane.
        # A stale/racing refusal therefore cannot partially grant lane authority.
        activation_dir = (
            session.harness_config.runtime_root
            / "operator-only"
            / "memory-plan-activation"
            / str(session.epoch_id)
            / args.lane_id
        )
        activation_path = activation_dir / f"{updated['content_hash']}.json"
        activation_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(activation_path, updated)
        session.close()
        try:
            resumed = resume.activate_pending_plan(
                lane_id=args.lane_id,
                accepted_task_card=str(activation_path),
                rationale="ROOT accepted the exact memory plan",
            )
        finally:
            activation_path.unlink(missing_ok=True)
        combined = dict(result)
        combined["evidence"] = {
            **dict(result["evidence"]),
            "local_root_issuer": session.product_config.owner,
            "reprepare_code": resumed.get("code"),
            "dispatchable": bool(resumed.get("ok")),
        }
        if not resumed.get("ok"):
            combined["status"] = "accepted_pending_reprepare"
            combined["next_step"] = str(resumed.get("next_action") or "Retry exact lane resume.")
            return _public(combined, ok=False)
        combined["status"] = "accepted_dispatchable"
        combined["next_step"] = "Launch the prepared lane through `lane launch`."
        return _public(combined, ok=True)
    finally:
        try:
            session.close()
        except Exception:
            pass


def _partition(scope: Mapping[str, str]) -> dict[str, Any]:
    return {
        "scope": "project",
        "application": scope["application"],
        "project": scope["project"],
        "namespace": scope["namespace"],
        "recipients": [dict(scope)],
    }


class _UnavailableRemoteAdapter:
    metric = "cosine"


def _rich_current(configuration: memory_product_config.MemoryProductConfig) -> dict[str, Any]:
    from memory_harness import config

    availability = {
        "shared_publication": {
            "available": configuration.services["atlas"].available,
            "reason": configuration.services["atlas"].reason,
        },
        "atlas_shared_retrieval": {
            "available": configuration.services["atlas"].available,
            "reason": configuration.services["atlas"].reason,
        },
        "generated_skill_creation": {
            "available": configuration.services["everos"].available,
            "reason": configuration.services["everos"].reason,
        },
    }
    return config.configuration_record(config.resolve_config(availability=availability))


def _run_procedure(args: Any) -> dict[str, Any]:
    _root, _harness, observed = _configuration()
    _expected_config(args, observed, f"{args.procedure_command}_procedure")
    session = _open_session()
    opened_remote: Any | None = None
    try:
        scope = session.product_config.scope
        issuer = session.product_config.owner
        if args.procedure_command == "approve":
            if session.policy.detect(args.procedure):
                raise MemoryCommandError(
                    "privacy_violation", "procedure content contains configured secret material",
                    action="approve_procedure", next_step="Remove secret material and create a new exact revision."
                )
            result = session.admin.approve_procedure(
                procedure=args.procedure,
                expected_logical_id=args.expected_logical_id,
                expected_revision_id=args.expected_revision_id,
                expected_revision_digest=args.expected_revision_digest,
                expected_origin=args.expected_origin,
                approval_id=args.approval_id,
                issuer=issuer,
                recipients=[scope],
                authority_evidence={
                    "schema": "local-root-authority/v1",
                    "configuration_digest": session.product_config.config_digest,
                    "policy_generation": session.product_config.policy_generation,
                    "issuer": issuer,
                },
            )
        elif args.procedure_command == "represent":
            result = session.admin.create_procedure_representation(
                logical_id=args.logical_id,
                revision_id=args.revision_id,
                expected_revision_digest=args.expected_revision_digest,
                expected_origin=args.expected_origin,
                search_text=args.search_text,
                vector=product_composition.deterministic_representation_vector(
                    args.search_text
                ),
            )
        elif args.procedure_command == "designate":
            result = session.admin.designate_procedure(
                logical_id=args.logical_id,
                revision_id=args.revision_id,
                expected_revision_digest=args.expected_revision_digest,
                approval_id=args.approval_id,
                expected_approval_digest=args.expected_approval_digest,
                expected_origin=args.expected_origin,
                partition=_partition(scope),
                issuer=issuer,
            )
        elif args.procedure_command == "publish":
            service = session.product_config.services["atlas"]
            if not service.enabled or not service.available:
                return _public(_result(
                    "publish_procedure", "unavailable",
                    {
                        "code": "atlas_unavailable",
                        "reason": service.reason,
                        "local_root_issuer": issuer,
                    },
                    "Enable Atlas in trusted memory-product-config.json with its referenced credential and prerequisites, then start a fresh epoch.",
                ), ok=False)
            from memory_harness import config as memory_config
            adapter = product_composition._atlas_adapter(session.product_config, session.policy)
            opened_remote = adapter
            rich = _rich_current(session.product_config)
            network = memory_config.resolve_network_mode("normal")
            atlas_scope = {
                "database": service.database,
                "collection": service.collection,
                "index": service.index,
                "namespace": scope["namespace"],
                "recipients": [scope],
            }
            identity = processes.process_identity(os.getpid())
            if identity is None:
                raise MemoryCommandError(
                    "claimant_identity_unavailable",
                    "the local ROOT publication claimant identity cannot be proven",
                    action="publish_procedure",
                    next_step="Retry only when exact process creation identity is available.",
                )
            claimant = {
                "schema": "external-effect-claimant/v1",
                "native_invocation_id": f"memory-admin:{args.designation_id}",
                "pid": identity["pid"],
                "process_created_at": identity["creation_time"],
            }
            from memory_harness import contracts
            claimant["content_hash"] = contracts.content_hash(claimant)
            result = session.admin.publish_procedure(
                logical_id=args.logical_id,
                revision_id=args.revision_id,
                expected_revision_digest=args.expected_revision_digest,
                approval_id=args.approval_id,
                expected_approval_digest=args.expected_approval_digest,
                representation_id=args.representation_id,
                expected_representation_digest=args.expected_representation_digest,
                designation_id=args.designation_id,
                expected_designation_digest=args.expected_designation_digest,
                adapter=adapter,
                captured_config=rich,
                current_config=rich,
                atlas_scope=atlas_scope,
                claimant=claimant,
                network_resolution=network,
                shared_publication_enabled=True,
            )
        elif args.procedure_command == "withdraw":
            from memory_harness import config as memory_config
            adapter: Any = _UnavailableRemoteAdapter()
            network = memory_config.resolve_network_mode("restricted_local")
            if session.product_config.services["atlas"].available:
                adapter = product_composition._atlas_adapter(session.product_config, session.policy)
                opened_remote = adapter
                network = memory_config.resolve_network_mode("normal")
            result = session.admin.withdraw_procedure(
                logical_id=args.logical_id,
                partition=_partition(scope),
                expected_designation_id=args.expected_designation_id,
                expected_revision_id=args.expected_revision_id,
                expected_generation=args.expected_generation,
                expected_designation_digest=args.expected_designation_digest,
                issuer=issuer,
                adapter=adapter,
                network_resolution=network,
            )
        elif args.procedure_command == "revoke":
            from memory_harness import config as memory_config
            adapter = _UnavailableRemoteAdapter()
            network = memory_config.resolve_network_mode("restricted_local")
            if session.product_config.services["atlas"].available:
                adapter = product_composition._atlas_adapter(session.product_config, session.policy)
                opened_remote = adapter
                network = memory_config.resolve_network_mode("normal")
            result = session.admin.revoke_procedure(
                revision_id=args.revision_id,
                expected_revision_digest=args.expected_revision_digest,
                expected_origin=args.expected_origin,
                issuer=issuer,
                reason=args.reason,
                adapter=adapter,
                network_resolution=network,
            )
        else:  # pragma: no cover
            raise ValueError(f"unknown memory procedure command: {args.procedure_command}")
        result = dict(result)
        result["evidence"] = {**dict(result["evidence"]), "local_root_issuer": issuer}
        return _public(result)
    finally:
        if opened_remote is not None:
            collection = getattr(opened_remote, "collection", None)
            database = getattr(collection, "database", None)
            client = getattr(database, "client", None)
            close = getattr(client, "close", None)
            if callable(close):
                close()
        session.close()


def _run_effects(args: Any) -> dict[str, Any]:
    if args.effects_command != "list":
        _root, _harness, observed = _configuration()
        _expected_config(args, observed, "reconcile_effect")
    session = _open_session()
    try:
        if args.effects_command == "list":
            return _public(session.admin.list_pending_effects(outcome_id=args.outcome_id))
        if session.policy.detect(args.evidence):
            raise MemoryCommandError(
                "privacy_violation", "effect evidence contains configured secret material",
                action="reconcile_effect", next_step="Use sanitized exact adapter readback evidence."
            )
        return _public(session.admin.reconcile_effect(
            operation_id=args.operation_id,
            expected_status=args.expected_status,
            expected_version=args.expected_version,
            expected_kind=args.expected_kind,
            expected_source_digest=args.expected_source_digest,
            evidence=args.evidence,
            result=args.result,
            claim_id=args.claim_id,
        ))
    finally:
        session.close()


def _run_snapshot(args: Any) -> dict[str, Any]:
    root, harness, configuration = _configuration()
    del root
    _expected_config(args, configuration, f"{args.snapshot_command}_snapshot")
    artifact = _derived_snapshot_path(configuration, args.snapshot_id)
    target = (
        _derived_restore_path(harness, args.restore_id)
        if args.snapshot_command == "import"
        else None
    )
    if args.snapshot_command == "export":
        artifact.parent.mkdir(parents=True, exist_ok=True)
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
    session = _open_session(snapshot_artifact=artifact, restore_target=target)
    try:
        _expected_config(args, session.product_config, f"{args.snapshot_command}_snapshot")
        credential = _snapshot_credential(session.product_config)
        if args.snapshot_command == "export":
            result = session.admin.export_snapshot(
                artifact=artifact, credential=credential, dependencies=()
            )
        elif args.snapshot_command == "readiness":
            result = session.admin.snapshot_readiness(
                artifact=artifact,
                expected_snapshot_digest=args.expected_snapshot_digest,
                credential=credential,
            )
        elif args.snapshot_command == "import":
            from memory_harness.store import MemoryStore
            assert target is not None
            result = session.admin.import_snapshot(
                artifact=artifact,
                target=MemoryStore(target),
                credential=credential,
                expected_snapshot_digest=args.expected_snapshot_digest,
                required_capabilities=tuple(args.required_capability or ["local"]),
            )
        else:  # pragma: no cover
            raise ValueError(f"unknown memory snapshot command: {args.snapshot_command}")
        return _public(result)
    finally:
        session.close()


def _run_config(args: Any) -> dict[str, Any]:
    _root, harness, configuration = _configuration()
    if args.config_command == "status":
        try:
            state = memory_product_config.verify_secret_rotation_state(configuration)
            secret_status = "verified"
            state_generation: str | None = state.policy_generation
        except Exception:
            secret_status = "uninitialized_or_drifted"
            state_generation = None
        return _public(_result(
            "config_status", "ready" if secret_status == "verified" else "attention_required",
            {
                "configuration_digest": configuration.config_digest,
                "active_epoch_configuration_digest": harness.memory_product_config_identity,
                "policy_generation": configuration.policy_generation,
                "secret_state": secret_status,
                "secret_state_policy_generation": state_generation,
                "known_secret_count": len(configuration.known_secret_files),
                "scope": configuration.scope,
                "services": {
                    name: {
                        "enabled": service.enabled,
                        "available": service.available,
                        "reason": service.reason,
                    }
                    for name, service in configuration.services.items()
                },
                "local_root_issuer": configuration.owner,
                "sharing_policy": "default-deny",
            },
            "Initialize or rotate secret state if attention is required; otherwise no action is needed.",
        ))
    _expected_config(args, configuration, f"{args.config_command}_secret_state")
    if args.config_command == "init-secret-state":
        state = memory_product_config.initialize_secret_rotation_state(configuration)
        status = "initialized"
    elif args.config_command == "rotate-secret-state":
        state_path = configuration.store_root / memory_product_config.SECRET_STATE_FILE_NAME
        try:
            prior = read_json(state_path)
        except Exception as exc:
            raise MemoryCommandError(
                "secret_state_unavailable",
                "the prior secret rotation state is unavailable",
                action="rotate-secret-state",
                next_step="Initialize the secret state before requesting rotation.",
            ) from exc
        if prior.get("policy_generation") != args.expected_prior_policy_generation:
            raise MemoryCommandError(
                "stale_identity",
                "the prior policy generation no longer matches the operator observation",
                action="rotate-secret-state",
                next_step="Read config status and submit the exact prior and current generations.",
            )
        if args.expected_prior_policy_generation == configuration.policy_generation:
            raise MemoryCommandError(
                "generation_not_advanced",
                "secret rotation requires a new policy generation",
                action="rotate-secret-state",
                next_step="Advance policy_generation in trusted configuration, then start a fresh epoch.",
            )
        state = memory_product_config.record_secret_rotation_state(configuration)
        status = "rotated"
    else:  # pragma: no cover
        raise ValueError(f"unknown memory config command: {args.config_command}")
    return _public(_result(
        f"{args.config_command}", status,
        {
            "configuration_digest": state.config_digest,
            "policy_generation": state.policy_generation,
            "local_root_issuer": configuration.owner,
        },
        "Use `memory config status` to verify the trusted privacy state.",
    ))


def run(args: Any) -> dict[str, Any]:
    """Dispatch one parsed ``memory`` command and return a safe result."""

    try:
        if args.memory_command == "config":
            return _run_config(args)
        if args.memory_command == "plan":
            return _run_plan(args)
        if args.memory_command == "procedure":
            return _run_procedure(args)
        if args.memory_command == "effects":
            return _run_effects(args)
        if args.memory_command == "snapshot":
            return _run_snapshot(args)
        raise ValueError(f"unknown memory command: {args.memory_command}")
    except memory_admin.MemoryAdminError as exc:
        return _public(exc.as_dict(), ok=False)
    except MemoryCommandError as exc:
        return _public(exc.as_admin_result(), ok=False)
    except Exception as exc:
        # Do not echo request bodies or credential values from arbitrary lower
        # exceptions.  Known configuration/admin errors are already converted
        # above; the generic boundary is deliberately terse.
        result = _result(
            "memory_command", "rejected",
            {"code": "operation_failed", "reason": type(exc).__name__},
            "Inspect trusted configuration and durable identities, then retry.",
        )
        return _public(result, ok=False)


__all__ = ["run"]

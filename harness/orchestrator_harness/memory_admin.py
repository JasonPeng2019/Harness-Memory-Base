"""ROOT-only administration over an already-authorized memory context.

This module deliberately does not discover a store from task or worker input.
Composition creates :class:`RootAdminContext` from trusted operator
configuration, and this surface then requires the exact identities observed by
the operator before every mutation.  Returned evidence is either an identity
summary or privacy-sanitized review material; procedure bodies, effect
payloads, credentials, and store paths are never echoed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypedDict

from memory_harness import config as memory_config
from memory_harness import atlas, contracts, preparation, procedures
from memory_harness.privacy import PrivacyPolicy, sanitize_payload
from memory_harness.snapshot import SnapshotService
from memory_harness.store import (
    MemoryStore,
    PlanDispositionConflictError,
    StoreError,
)


ADMIN_RESULT_SCHEMA = "memory-admin-result/v1"


class AdminResult(TypedDict):
    schema: str
    action: str
    status: str
    evidence: dict[str, Any]
    next_step: str


class MemoryAdminError(RuntimeError):
    """Fail-closed administration error with a safe machine-readable result."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        action: str = "admin",
        evidence: Mapping[str, Any] | None = None,
        next_step: str = "Re-read the current exact record and submit a new ROOT operation.",
    ) -> None:
        super().__init__(message)
        self.safe_message = message
        self.code = code
        self.action = action
        self.evidence = dict(evidence or {})
        self.next_step = next_step

    def as_dict(self) -> AdminResult:
        return _result(
            self.action,
            "rejected",
            {"code": self.code, "reason": self.safe_message, **self.evidence},
            self.next_step,
        )


def _result(
    action: str, status: str, evidence: Mapping[str, Any], next_step: str
) -> AdminResult:
    return {
        "schema": ADMIN_RESULT_SCHEMA,
        "action": action,
        "status": status,
        "evidence": dict(evidence),
        "next_step": next_step,
    }


def _invalid_context(message: str) -> MemoryAdminError:
    return MemoryAdminError(
        "invalid_context",
        message,
        action="initialize",
        next_step="Build the administration context from trusted ROOT configuration.",
    )


@dataclass(frozen=True)
class RootAdminContext:
    """Trusted dependencies selected before any administration request.

    ``store`` and ``snapshots`` are objects, not request-selected paths.  The
    caller is responsible for deriving them from authenticated ROOT-side
    configuration.  This class only verifies their structural and exact scope
    invariants before exposing mutations.
    """

    store: MemoryStore
    scope: Mapping[str, str]
    trusted_issuers: Sequence[str]
    privacy_policy: PrivacyPolicy
    snapshots: SnapshotService
    sharing_policy: Callable[
        [str, dict[str, str], dict[str, str]], bool
    ] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.store, MemoryStore) or self.store.connection is None:
            raise _invalid_context("the trusted MemoryStore must already be initialized")
        try:
            normalized = contracts.normalize_experience_scope(self.scope)
        except (TypeError, ValueError) as exc:
            raise _invalid_context("the ROOT administration scope is invalid") from exc
        if set(self.scope) != set(normalized) or dict(self.scope) != normalized:
            raise _invalid_context("the ROOT administration scope must contain exactly four identities")
        if isinstance(self.trusted_issuers, (str, bytes)):
            raise _invalid_context("trusted issuers must be an explicit identity sequence")
        issuers = tuple(dict.fromkeys(self.trusted_issuers))
        if not issuers or any(not isinstance(item, str) or not item for item in issuers):
            raise _invalid_context("at least one exact trusted procedure issuer is required")
        if not isinstance(self.privacy_policy, PrivacyPolicy):
            raise _invalid_context("a trusted privacy policy is required")
        if not isinstance(self.snapshots, SnapshotService):
            raise _invalid_context("an authorized SnapshotService is required")
        if self.sharing_policy is not None and not callable(self.sharing_policy):
            raise _invalid_context("the trusted sharing policy must be callable")
        object.__setattr__(self, "scope", normalized)
        object.__setattr__(self, "trusted_issuers", issuers)


class MemoryAdmin:
    """Typed ROOT administration facade over one fixed authorized context."""

    def __init__(self, context: RootAdminContext) -> None:
        if not isinstance(context, RootAdminContext):
            raise _invalid_context("MemoryAdmin requires a RootAdminContext")
        self.context = context
        self._store = context.store
        # Plan transforms are produced by the canonical preparation service,
        # but this facade owns the single durable write after adding its exact
        # administration evidence.  That avoids an intermediate acceptance or
        # rejection record if the process stops between two writes.
        self._planning = preparation.PreparationService(
            store=None, privacy_policy=context.privacy_policy
        )
        self._procedures = procedures.TrustedProcedureService(
            context.store,
            trusted_issuers=context.trusted_issuers,
            privacy_policy=context.privacy_policy,
        )
        self._snapshots = context.snapshots

    def _result(
        self, action: str, status: str, evidence: Mapping[str, Any], next_step: str
    ) -> AdminResult:
        sanitized = sanitize_payload(dict(evidence), self.context.privacy_policy)
        assert isinstance(sanitized, dict)
        return _result(action, status, sanitized, next_step)

    def _authorize_scope(
        self,
        action: str,
        candidate: Mapping[str, Any],
        *,
        require_sharing_policy: bool = False,
    ) -> dict[str, str]:
        try:
            normalized = contracts.normalize_experience_scope(candidate)
        except (TypeError, ValueError) as exc:
            raise MemoryAdminError(
                "invalid_scope",
                "the requested administration scope is malformed",
                action=action,
            ) from exc
        if set(candidate) != set(normalized) or dict(candidate) != normalized:
            raise MemoryAdminError(
                "invalid_scope",
                "the requested administration scope must contain exactly four identities",
                action=action,
            )
        source = dict(self.context.scope)
        if normalized == source and not require_sharing_policy:
            return normalized
        policy = self.context.sharing_policy
        if policy is None:
            raise MemoryAdminError(
                "scope_denied",
                "cross-scope or shared administration requires a trusted context-bound sharing policy",
                action=action,
                next_step="Use the exact ROOT context scope or configure an explicit trusted sharing policy.",
            )
        try:
            permitted = policy(action, source, dict(normalized))
        except Exception as exc:
            raise MemoryAdminError(
                "scope_denied",
                "the trusted sharing policy could not authorize the requested scope",
                action=action,
            ) from exc
        if permitted is not True:
            raise MemoryAdminError(
                "scope_denied",
                "the trusted sharing policy denied the requested scope",
                action=action,
            )
        return normalized

    def _authorize_recipients(
        self,
        action: str,
        recipients: Iterable[Mapping[str, Any]],
        *,
        require_sharing_policy: bool = False,
    ) -> list[dict[str, str]]:
        if isinstance(recipients, (str, bytes)):
            raise MemoryAdminError(
                "invalid_scope", "procedure recipients must be explicit scopes", action=action
            )
        normalized = [
            self._authorize_scope(
                action,
                recipient,
                require_sharing_policy=require_sharing_policy,
            )
            for recipient in recipients
        ]
        if not normalized:
            raise MemoryAdminError(
                "invalid_scope", "procedure recipients must not be empty", action=action
            )
        return normalized

    def _authorize_procedure(
        self, action: str, procedure: Mapping[str, Any]
    ) -> None:
        scope = procedure.get("origin_scope")
        if not isinstance(scope, Mapping):
            raise MemoryAdminError(
                "invalid_scope", "procedure origin scope is missing", action=action
            )
        self._authorize_scope(action, scope)

    def _authorize_partition(
        self, action: str, partition: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            normalized = contracts.normalize_procedure_partition(partition)
        except contracts.ContractError as exc:
            raise MemoryAdminError(
                "invalid_scope", "the procedure partition is invalid", action=action
            ) from exc
        if dict(partition) != normalized:
            raise MemoryAdminError(
                "invalid_scope", "the procedure partition must be canonical", action=action
            )
        top_scope = {
            "application": normalized["application"],
            "project": normalized["project"],
            "namespace": normalized["namespace"],
            "owner": normalized.get("owner", self.context.scope["owner"]),
        }
        sharing = normalized["scope"] == "shared"
        self._authorize_scope(
            action, top_scope, require_sharing_policy=sharing
        )
        self._authorize_recipients(
            action,
            normalized["recipients"],
            require_sharing_policy=sharing,
        )
        return normalized

    def _authorize_approval(
        self,
        action: str,
        approval: Mapping[str, Any],
        procedure: Mapping[str, Any],
    ) -> None:
        self._authorize_procedure(action, procedure)
        recipients = approval.get("recipients")
        if not isinstance(recipients, list):
            raise MemoryAdminError(
                "invalid_scope", "procedure approval recipients are missing", action=action
            )
        self._authorize_recipients(action, recipients)

    def _authorize_effect_source(
        self,
        action: str,
        operation: Mapping[str, Any],
        *,
        allow_unbound: bool = False,
    ) -> None:
        source = operation.get("source_record")
        if not isinstance(source, Mapping):
            if allow_unbound and source is None:
                return
            raise MemoryAdminError(
                "scope_unresolved",
                "the pending effect has no exact source scope yet",
                action=action,
                next_step="Bind the reviewed source through its source-specific settlement coordinator.",
            )
        if source.get("schema") == contracts.PROCEDURE_PUBLICATION_SCHEMA:
            try:
                contracts.validate_procedure_publication(source)
            except contracts.ContractError as exc:
                raise MemoryAdminError(
                    "invalid_durable_state",
                    "the procedure publication effect source is not internally exact",
                    action=action,
                ) from exc
            procedure = source.get("procedure")
            approval = source.get("approval")
            designation = source.get("designation")
            if not all(isinstance(item, Mapping) for item in (
                procedure, approval, designation
            )):
                raise MemoryAdminError(
                    "scope_unresolved", "procedure publication scope is incomplete", action=action
                )
            self._authorize_approval(action, approval, procedure)  # type: ignore[arg-type]
            self._authorize_partition(action, designation["partition"])  # type: ignore[index]
            return
        if source.get("schema") == contracts.PROCEDURE_REVISION_SCHEMA:
            try:
                contracts.validate_procedure_revision(source)
            except contracts.ContractError as exc:
                raise MemoryAdminError(
                    "invalid_durable_state",
                    "the procedure effect source is not internally exact",
                    action=action,
                ) from exc
            self._authorize_procedure(action, source)
            return
        if source.get("schema") == contracts.REVIEWED_TRAJECTORY_SCHEMA:
            try:
                contracts.validate_reviewed_trajectory(source)
            except contracts.ContractError as exc:
                raise MemoryAdminError(
                    "invalid_durable_state",
                    "the reviewed effect source is not internally exact",
                    action=action,
                ) from exc
        scope = source.get("scope", source.get("origin_scope"))
        if isinstance(scope, Mapping):
            self._authorize_scope(action, scope)
            return
        raise MemoryAdminError(
            "scope_unresolved",
            "the effect source has no exact authorized scope",
            action=action,
        )

    @staticmethod
    def _rich_configuration(
        action: str, label: str, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or not {
            "feature_states", "configuration_identity"
        } <= set(value):
            raise MemoryAdminError(
                "invalid_configuration",
                f"{label} must be a complete rich feature-resolution record",
                action=action,
            )
        try:
            resolved = memory_config.resolve_config(value)
            canonical = memory_config.configuration_record(resolved)
        except (TypeError, ValueError) as exc:
            raise MemoryAdminError(
                "invalid_configuration",
                f"{label} feature resolution is invalid",
                action=action,
            ) from exc
        if dict(value) != canonical:
            raise MemoryAdminError(
                "invalid_configuration",
                f"{label} feature resolution is not canonical",
                action=action,
            )
        return canonical

    # -- exact identity helpers -----------------------------------------

    @staticmethod
    def _require_equal(
        action: str, field: str, actual: Any, expected: Any
    ) -> None:
        if not isinstance(expected, (str, int)) or isinstance(expected, bool) or expected == "":
            raise MemoryAdminError(
                "missing_expected_identity",
                f"{field} must be supplied as an exact expected identity",
                action=action,
                evidence={"field": field},
            )
        if actual != expected:
            raise MemoryAdminError(
                "stale_identity",
                f"the durable {field} no longer matches the operator observation",
                action=action,
                evidence={"field": field},
            )

    def _disposition(
        self,
        action: str,
        *,
        disposition_id: str,
        expected_disposition_digest: str,
        expected_decision_id: str,
        expected_objective_id: str,
    ) -> dict[str, Any]:
        try:
            disposition = self._store.get_plan_disposition(disposition_id)
        except StoreError as exc:
            raise MemoryAdminError(
                "not_found",
                "the exact plan disposition does not exist",
                action=action,
                evidence={"record": "plan_disposition"},
            ) from exc
        self._require_equal(
            action, "disposition_digest", disposition["content_hash"], expected_disposition_digest
        )
        self._require_equal(action, "decision_id", disposition["decision_id"], expected_decision_id)
        self._require_equal(
            action, "objective_id", disposition["objective_id"], expected_objective_id
        )
        return disposition

    @staticmethod
    def _pending_plan(disposition: Mapping[str, Any], *, action: str) -> tuple[str, dict[str, Any]]:
        field = {
            "candidate_review": "preserved_plan",
            "direct_fill": "proposal",
            "apc_proposal": "proposal",
            "fresh": "fresh",
        }.get(str(disposition.get("branch")))
        if field is None or disposition.get("root_acceptance") is not None:
            raise MemoryAdminError(
                "not_pending",
                "the disposition has no pending plan for ROOT review",
                action=action,
                evidence={"disposition_id": disposition.get("disposition_id")},
                next_step="Inspect the accepted handoff or begin a new exact planning decision.",
            )
        candidate = disposition.get(field)
        try:
            contracts.validate_plan(candidate)
        except (AttributeError, TypeError, ValueError) as exc:
            raise MemoryAdminError(
                "invalid_durable_state",
                "the pending plan record is not a valid exact plan",
                action=action,
                evidence={"disposition_id": disposition.get("disposition_id")},
            ) from exc
        if candidate["state"] == "accepted":
            raise MemoryAdminError(
                "not_pending",
                "an accepted plan cannot be changed through pending-plan administration",
                action=action,
            )
        return field, dict(candidate)

    def _exact_pending_plan(
        self,
        action: str,
        *,
        disposition_id: str,
        expected_disposition_digest: str,
        expected_decision_id: str,
        expected_objective_id: str,
        expected_plan_id: str,
        expected_plan_digest: str,
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        disposition = self._disposition(
            action,
            disposition_id=disposition_id,
            expected_disposition_digest=expected_disposition_digest,
            expected_decision_id=expected_decision_id,
            expected_objective_id=expected_objective_id,
        )
        field, plan = self._pending_plan(disposition, action=action)
        self._require_equal(action, "plan_id", plan["plan_id"], expected_plan_id)
        self._require_equal(action, "plan_digest", plan["content_hash"], expected_plan_digest)
        if plan["objective_id"] != disposition["objective_id"] or plan["route"] != disposition["route"]:
            raise MemoryAdminError(
                "invalid_durable_state",
                "the pending plan does not bind the disposition objective and route",
                action=action,
            )
        return disposition, field, plan

    def _replace_disposition(
        self,
        action: str,
        disposition: Mapping[str, Any],
        *,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        try:
            return self._store.replace_plan_disposition(
                disposition, expected_content_hash=expected_content_hash
            )
        except PlanDispositionConflictError as exc:
            raise MemoryAdminError(
                "stale_identity",
                "another ROOT operation changed the exact plan disposition first",
                action=action,
                evidence={"field": "disposition_digest"},
            ) from exc

    # -- ROOT plan review ------------------------------------------------

    def inspect_pending_plan(
        self,
        *,
        disposition_id: str,
        expected_disposition_digest: str,
        expected_decision_id: str,
        expected_objective_id: str,
    ) -> AdminResult:
        action = "inspect_plan"
        disposition = self._disposition(
            action,
            disposition_id=disposition_id,
            expected_disposition_digest=expected_disposition_digest,
            expected_decision_id=expected_decision_id,
            expected_objective_id=expected_objective_id,
        )
        field, plan = self._pending_plan(disposition, action=action)
        return self._result(
            action,
            "pending_review",
            {
                "disposition_id": disposition["disposition_id"],
                "disposition_digest": disposition["content_hash"],
                "decision_id": disposition["decision_id"],
                "objective_id": disposition["objective_id"],
                "route": disposition["route"],
                "branch": disposition["branch"],
                "plan_field": field,
                "plan_id": plan["plan_id"],
                "plan_digest": plan["content_hash"],
                "revision": plan["revision"],
                "state": plan["state"],
                "review_content": sanitize_payload(
                    plan["content"], self.context.privacy_policy
                ),
            },
            "ROOT may correct, accept, or reject this exact plan identity.",
        )

    def _replace_pending_plan(
        self,
        action: str,
        *,
        disposition_id: str,
        expected_disposition_digest: str,
        expected_decision_id: str,
        expected_objective_id: str,
        expected_plan_id: str,
        expected_plan_digest: str,
        replacement_plan_id: str,
        replacement_content: Any,
    ) -> AdminResult:
        disposition, field, plan = self._exact_pending_plan(
            action,
            disposition_id=disposition_id,
            expected_disposition_digest=expected_disposition_digest,
            expected_decision_id=expected_decision_id,
            expected_objective_id=expected_objective_id,
            expected_plan_id=expected_plan_id,
            expected_plan_digest=expected_plan_digest,
        )
        if replacement_plan_id == plan["plan_id"]:
            raise MemoryAdminError(
                "copied_identity",
                "a corrected proposal must use a distinct exact plan identity",
                action=action,
                evidence={"field": "replacement_plan_id"},
            )
        try:
            replacement = contracts.make_plan(
                plan_id=replacement_plan_id,
                objective_id=plan["objective_id"],
                route=plan["route"],
                state=plan["state"],
                content=replacement_content,
                revision=int(plan["revision"]) + 1,
                supersedes=plan["plan_id"],
                source=plan.get("source"),
            )
        except contracts.ContractError as exc:
            raise MemoryAdminError(
                "invalid_plan",
                "the replacement could not form a valid exact plan",
                action=action,
            ) from exc
        revised = dict(disposition)
        revised[field] = replacement
        revised["reason"] = f"ROOT {action.replace('_', ' ')} created a new exact review revision"
        revised["root_acceptance"] = None
        revised["content_hash"] = contracts.content_hash(revised)
        try:
            contracts.validate_plan_disposition(revised)
            durable = self._replace_disposition(
                action,
                revised,
                expected_content_hash=expected_disposition_digest,
            )
        except (contracts.ContractError, StoreError) as exc:
            raise MemoryAdminError(
                "operation_failed",
                "the replacement plan could not be durably recorded",
                action=action,
            ) from exc
        return self._result(
            action,
            "pending_review",
            {
                "disposition_id": durable["disposition_id"],
                "disposition_digest": durable["content_hash"],
                "decision_id": durable["decision_id"],
                "objective_id": durable["objective_id"],
                "plan_id": replacement["plan_id"],
                "plan_digest": replacement["content_hash"],
                "revision": replacement["revision"],
                "supersedes": replacement["supersedes"],
            },
            "ROOT must inspect and explicitly accept or reject the new exact revision.",
        )

    def propose_plan(
        self,
        *,
        disposition_id: str,
        expected_disposition_digest: str,
        expected_decision_id: str,
        expected_objective_id: str,
        expected_plan_id: str,
        expected_plan_digest: str,
        proposed_plan_id: str,
        proposed_content: Any,
    ) -> AdminResult:
        return self._replace_pending_plan(
            "propose_plan",
            disposition_id=disposition_id,
            expected_disposition_digest=expected_disposition_digest,
            expected_decision_id=expected_decision_id,
            expected_objective_id=expected_objective_id,
            expected_plan_id=expected_plan_id,
            expected_plan_digest=expected_plan_digest,
            replacement_plan_id=proposed_plan_id,
            replacement_content=proposed_content,
        )

    def correct_plan(
        self,
        *,
        disposition_id: str,
        expected_disposition_digest: str,
        expected_decision_id: str,
        expected_objective_id: str,
        expected_plan_id: str,
        expected_plan_digest: str,
        corrected_plan_id: str,
        corrected_content: Any,
    ) -> AdminResult:
        return self._replace_pending_plan(
            "correct_plan",
            disposition_id=disposition_id,
            expected_disposition_digest=expected_disposition_digest,
            expected_decision_id=expected_decision_id,
            expected_objective_id=expected_objective_id,
            expected_plan_id=expected_plan_id,
            expected_plan_digest=expected_plan_digest,
            replacement_plan_id=corrected_plan_id,
            replacement_content=corrected_content,
        )

    def accept_plan(
        self,
        *,
        disposition_id: str,
        expected_disposition_digest: str,
        expected_decision_id: str,
        expected_objective_id: str,
        expected_plan_id: str,
        expected_plan_digest: str,
    ) -> AdminResult:
        action = "accept_plan"
        disposition, _field, plan = self._exact_pending_plan(
            action,
            disposition_id=disposition_id,
            expected_disposition_digest=expected_disposition_digest,
            expected_decision_id=expected_decision_id,
            expected_objective_id=expected_objective_id,
            expected_plan_id=expected_plan_id,
            expected_plan_digest=expected_plan_digest,
        )
        try:
            if self.context.privacy_policy.detect(plan):
                raise MemoryAdminError(
                    "privacy_violation",
                    "the pending plan contains configured secret material and cannot be accepted",
                    action=action,
                    next_step="Correct the exact plan to remove secret material before acceptance.",
                )
            revised, accepted = self._planning.accept(
                disposition=disposition, proposal=plan
            )
            root_acceptance = dict(revised["root_acceptance"])
            root_acceptance.update(
                {
                    "source_plan_id": plan["plan_id"],
                    "source_plan_digest": plan["content_hash"],
                    # This is the durable exact record the CLI must bind into
                    # the handoff.  Acceptance refuses configured secrets
                    # above, so the administration result never becomes a
                    # secret exfiltration path.
                    "accepted_plan": accepted,
                }
            )
            revised["root_acceptance"] = root_acceptance
            revised["content_hash"] = contracts.content_hash(revised)
            durable = self._replace_disposition(
                action,
                revised,
                expected_content_hash=expected_disposition_digest,
            )
        except MemoryAdminError:
            raise
        except (preparation.PreparationError, contracts.ContractError, StoreError) as exc:
            raise MemoryAdminError(
                "operation_failed",
                "ROOT acceptance could not be durably recorded",
                action=action,
            ) from exc
        return self._result(
            action,
            "accepted",
            {
                "disposition_id": durable["disposition_id"],
                "disposition_digest": durable["content_hash"],
                "decision_id": durable["decision_id"],
                "objective_id": durable["objective_id"],
                "plan_id": accepted["plan_id"],
                "plan_digest": accepted["content_hash"],
                "revision": accepted["revision"],
                "accepted_by": accepted["accepted_by"],
                "source_plan_digest": plan["content_hash"],
                "accepted_plan": accepted,
            },
            "Bind this exact accepted plan record into the ROOT-owned handoff before dispatch.",
        )

    def reject_plan(
        self,
        *,
        disposition_id: str,
        expected_disposition_digest: str,
        expected_decision_id: str,
        expected_objective_id: str,
        expected_plan_id: str,
        expected_plan_digest: str,
        reason: str,
        fresh_plan_id: str,
    ) -> AdminResult:
        action = "reject_plan"
        disposition, _field, plan = self._exact_pending_plan(
            action,
            disposition_id=disposition_id,
            expected_disposition_digest=expected_disposition_digest,
            expected_decision_id=expected_decision_id,
            expected_objective_id=expected_objective_id,
            expected_plan_id=expected_plan_id,
            expected_plan_digest=expected_plan_digest,
        )
        if not isinstance(reason, str) or not reason.strip():
            raise MemoryAdminError("invalid_reason", "ROOT rejection requires a reason", action=action)
        try:
            fresh = contracts.make_plan(
                plan_id=fresh_plan_id,
                objective_id=plan["objective_id"],
                route=plan["route"],
                state="fresh",
                content={"steps": []},
                revision=int(plan["revision"]) + 1,
                supersedes=plan["plan_id"],
            )
            staged = dict(disposition)
            staged["fresh"] = fresh
            rejected = self._planning.reject(disposition=staged, reason=reason.strip())
            rejected = self._replace_disposition(
                action,
                rejected,
                expected_content_hash=expected_disposition_digest,
            )
        except (preparation.PreparationError, contracts.ContractError, StoreError) as exc:
            raise MemoryAdminError(
                "operation_failed",
                "ROOT rejection could not be durably recorded",
                action=action,
            ) from exc
        return self._result(
            action,
            "rejected",
            {
                "disposition_id": rejected["disposition_id"],
                "disposition_digest": rejected["content_hash"],
                "decision_id": rejected["decision_id"],
                "objective_id": rejected["objective_id"],
                "rejected_plan_id": plan["plan_id"],
                "rejected_plan_digest": plan["content_hash"],
                "fresh_plan_id": fresh["plan_id"],
                "fresh_plan_digest": fresh["content_hash"],
            },
            "ROOT may author and review the exact fresh fallback; nothing is dispatchable yet.",
        )

    # -- trusted procedure governance -----------------------------------

    def _procedure_revision(
        self,
        action: str,
        *,
        revision_id: str,
        expected_revision_digest: str,
        expected_origin: str,
        expected_logical_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            revision = self._store.get_procedure_revision(revision_id)
        except StoreError as exc:
            raise MemoryAdminError(
                "not_found", "the exact procedure revision does not exist", action=action
            ) from exc
        self._require_equal(
            action, "revision_digest", revision["content_hash"], expected_revision_digest
        )
        self._require_equal(action, "origin", revision["origin"], expected_origin)
        if expected_logical_id is not None:
            self._require_equal(
                action, "logical_id", revision["logical_id"], expected_logical_id
            )
        self._authorize_procedure(action, revision)
        return revision

    def approve_procedure(
        self,
        *,
        procedure: Mapping[str, Any],
        expected_logical_id: str,
        expected_revision_id: str,
        expected_revision_digest: str,
        expected_origin: str,
        approval_id: str,
        issuer: str,
        recipients: Iterable[Mapping[str, Any]],
        authority_evidence: Mapping[str, Any],
        approved_at: str | None = None,
    ) -> AdminResult:
        action = "approve_procedure"
        try:
            contracts.validate_procedure_revision(procedure)
        except contracts.ContractError as exc:
            raise MemoryAdminError(
                "invalid_procedure", "the proposed procedure revision is invalid", action=action
            ) from exc
        for field, actual, expected in (
            ("logical_id", procedure["logical_id"], expected_logical_id),
            ("revision_id", procedure["revision_id"], expected_revision_id),
            ("revision_digest", procedure["content_hash"], expected_revision_digest),
            ("origin", procedure["origin"], expected_origin),
        ):
            self._require_equal(action, field, actual, expected)
        self._authorize_procedure(action, procedure)
        authorized_recipients = self._authorize_recipients(action, recipients)
        try:
            approval = self._procedures.approve_revision(
                procedure=procedure,
                approval_id=approval_id,
                issuer=issuer,
                recipients=authorized_recipients,
                authority_evidence=authority_evidence,
                approved_at=approved_at,
            )
        except procedures.ProcedureError as exc:
            raise MemoryAdminError(
                "operation_failed", "the exact procedure revision was not approved", action=action
            ) from exc
        return self._result(
            action,
            "approved",
            {
                "logical_id": procedure["logical_id"],
                "revision_id": procedure["revision_id"],
                "revision_digest": procedure["content_hash"],
                "origin": procedure["origin"],
                "approval_id": approval["approval_id"],
                "approval_digest": approval["content_hash"],
                "issuer": approval["issuer"],
            },
            "ROOT may explicitly designate this approved revision for one exact partition.",
        )

    def _approval(
        self,
        action: str,
        *,
        approval_id: str,
        expected_approval_digest: str,
        revision: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            approval = self._store.get_procedure_approval(approval_id)
            contracts.validate_procedure_approval(approval, procedure=revision)
        except (StoreError, contracts.ContractError) as exc:
            raise MemoryAdminError(
                "invalid_durable_state",
                "the approval is absent or does not bind the exact procedure revision",
                action=action,
            ) from exc
        self._require_equal(
            action, "approval_digest", approval["content_hash"], expected_approval_digest
        )
        self._authorize_approval(action, approval, revision)
        return approval

    def create_procedure_representation(
        self,
        *,
        logical_id: str,
        revision_id: str,
        expected_revision_digest: str,
        expected_origin: str,
        search_text: str,
        vector: Sequence[float],
    ) -> AdminResult:
        """Create the canonical local/Atlas search projection for one revision.

        Representation identity is product-owned rather than request-owned:
        callers supply only the reviewed search text and its vector values.
        The model, dimensions, metric, and sanitizer version are the exact
        identity used by normal product composition.  This keeps a procedure
        created through the supported administration API discoverable by that
        same product and prevents a request from silently selecting a second
        embedding/index contract.
        """

        action = "represent_procedure"
        revision = self._procedure_revision(
            action,
            revision_id=revision_id,
            expected_revision_digest=expected_revision_digest,
            expected_origin=expected_origin,
            expected_logical_id=logical_id,
        )
        if not isinstance(search_text, str) or not search_text.strip():
            raise MemoryAdminError(
                "invalid_representation",
                "procedure representation search text must be nonempty",
                action=action,
            )
        if self.context.privacy_policy.detect(search_text):
            raise MemoryAdminError(
                "privacy_violation",
                "procedure representation search text contains configured secret material",
                action=action,
                next_step="Remove secret material and create a new exact representation.",
            )
        identity = memory_config.PreparationLimits().representation_identity
        try:
            representation = contracts.make_procedure_representation(
                procedure=revision,
                model=identity["model"],
                dimensions=identity["dimensions"],
                metric=identity["metric"],
                sanitizer_version=identity["sanitizer_version"],
                search_text=search_text,
                vector=vector,
            )
            durable = self._procedures.record_representation(representation)
        except (contracts.ContractError, procedures.ProcedureError, TypeError) as exc:
            raise MemoryAdminError(
                "invalid_representation",
                "the canonical procedure representation could not be durably recorded",
                action=action,
            ) from exc
        return self._result(
            action,
            "represented",
            {
                "logical_id": revision["logical_id"],
                "revision_id": revision["revision_id"],
                "revision_digest": revision["content_hash"],
                "origin": revision["origin"],
                "representation_id": durable["representation_id"],
                "representation_digest": durable["content_hash"],
                **identity,
            },
            "ROOT may designate this represented approved revision for an exact partition.",
        )

    def designate_procedure(
        self,
        *,
        logical_id: str,
        revision_id: str,
        expected_revision_digest: str,
        approval_id: str,
        expected_approval_digest: str,
        expected_origin: str,
        partition: Mapping[str, Any],
        issuer: str,
    ) -> AdminResult:
        action = "designate_procedure"
        revision = self._procedure_revision(
            action,
            revision_id=revision_id,
            expected_revision_digest=expected_revision_digest,
            expected_origin=expected_origin,
            expected_logical_id=logical_id,
        )
        approval = self._approval(
            action,
            approval_id=approval_id,
            expected_approval_digest=expected_approval_digest,
            revision=revision,
        )
        authorized_partition = self._authorize_partition(action, partition)
        try:
            designation = self._procedures.designate(
                procedure=revision,
                approval=approval,
                partition=authorized_partition,
                issuer=issuer,
            )
        except procedures.ProcedureError as exc:
            raise MemoryAdminError(
                "operation_failed", "the exact procedure designation was not recorded", action=action
            ) from exc
        return self._result(
            action,
            "designated",
            {
                "logical_id": revision["logical_id"],
                "revision_id": revision["revision_id"],
                "revision_digest": revision["content_hash"],
                "origin": revision["origin"],
                "approval_id": approval["approval_id"],
                "designation_id": designation["designation_id"],
                "designation_digest": designation["content_hash"],
                "partition_id": designation["partition_id"],
                "generation": designation["generation"],
            },
            "ROOT may publish this exact current designation when the configured network permits it.",
        )

    def publish_procedure(
        self,
        *,
        logical_id: str,
        revision_id: str,
        expected_revision_digest: str,
        approval_id: str,
        expected_approval_digest: str,
        representation_id: str,
        expected_representation_digest: str,
        designation_id: str,
        expected_designation_digest: str,
        adapter: Any,
        captured_config: Mapping[str, Any],
        current_config: Mapping[str, Any],
        atlas_scope: Mapping[str, Any],
        claimant: Mapping[str, Any],
        network_resolution: Mapping[str, Any] | Any,
        claim_fence: Mapping[str, Any] | None = None,
        shared_publication_enabled: bool = True,
    ) -> AdminResult:
        action = "publish_procedure"
        captured = self._rich_configuration(action, "captured_config", captured_config)
        current_configuration = self._rich_configuration(
            action, "current_config", current_config
        )
        if network_resolution is None:
            raise MemoryAdminError(
                "invalid_network_resolution",
                "governed publication requires a captured network resolution",
                action=action,
            )
        try:
            atlas.atlas_task_network_allowed(network_resolution)
        except (atlas.AtlasProcedureError, TypeError, ValueError) as exc:
            raise MemoryAdminError(
                "invalid_network_resolution",
                "the captured publication network resolution is invalid",
                action=action,
            ) from exc
        try:
            revision = self._store.get_procedure_revision(revision_id)
        except StoreError as exc:
            raise MemoryAdminError("not_found", "procedure revision not found", action=action) from exc
        self._require_equal(action, "logical_id", revision["logical_id"], logical_id)
        self._require_equal(
            action, "revision_digest", revision["content_hash"], expected_revision_digest
        )
        self._authorize_procedure(action, revision)
        approval = self._approval(
            action,
            approval_id=approval_id,
            expected_approval_digest=expected_approval_digest,
            revision=revision,
        )
        try:
            representation = self._store.get_procedure_representation(representation_id)
            designation = self._store.get_procedure_designation(designation_id)
        except StoreError as exc:
            raise MemoryAdminError(
                "not_found", "publication inputs are not durably present", action=action
            ) from exc
        self._require_equal(
            action,
            "representation_digest",
            representation["content_hash"],
            expected_representation_digest,
        )
        self._require_equal(
            action,
            "designation_digest",
            designation["content_hash"],
            expected_designation_digest,
        )
        for field in ("logical_id", "revision_id"):
            self._require_equal(action, field, representation[field], revision[field])
            self._require_equal(action, field, designation[field], revision[field])
        self._authorize_partition(action, designation["partition"])
        if not isinstance(atlas_scope, Mapping):
            raise MemoryAdminError(
                "invalid_scope", "the exact Atlas publication scope is missing", action=action
            )
        atlas_recipients = atlas_scope.get("recipients")
        if not isinstance(atlas_recipients, list):
            raise MemoryAdminError(
                "invalid_scope", "Atlas scope recipients are missing", action=action
            )
        self._authorize_recipients(
            action,
            atlas_recipients,
            require_sharing_policy=designation["partition"]["scope"] == "shared",
        )
        current = self._store.get_current_procedure_designation(logical_id, designation["partition"])
        if (
            current is None
            or current.get("state") != "active"
            or current.get("designation_id") != designation_id
            or current.get("generation") != designation["generation"]
        ):
            raise MemoryAdminError(
                "stale_identity",
                "the requested designation is no longer exact current",
                action=action,
                evidence={"field": "designation_id"},
            )
        try:
            publication = self._procedures.publish(
                procedure=revision,
                approval=approval,
                representation=representation,
                designation=designation,
                adapter=adapter,
                captured_config=captured,
                current_config=current_configuration,
                atlas_scope=atlas_scope,
                claimant=claimant,
                claim_fence=claim_fence,
                network_resolution=network_resolution,
                shared_publication_enabled=shared_publication_enabled,
            )
        except procedures.ProcedureRemoteAmbiguityError as exc:
            raise MemoryAdminError(
                "remote_ambiguous",
                "procedure publication acknowledgement is unresolved",
                action=action,
                next_step="Retry only with the same exact governed inputs so authoritative readback can reconcile it.",
            ) from exc
        except procedures.ProcedureError as exc:
            raise MemoryAdminError(
                "operation_failed",
                "procedure publication did not reach an acknowledged exact state",
                action=action,
                next_step="Inspect the durable remote operation and reconcile exact readback before retrying.",
            ) from exc
        return self._result(
            action,
            "published" if publication.get("status") == "acknowledged" else "publication_pending",
            {
                "logical_id": revision["logical_id"],
                "revision_id": revision["revision_id"],
                "origin": revision["origin"],
                "designation_id": designation["designation_id"],
                "designation_digest": designation["content_hash"],
                "publication_id": publication["publication_id"],
                "publication_digest": publication["content_hash"],
                "publication_status": publication["status"],
            },
            "Retain this identity evidence; reconcile rather than blindly retry any uncertain publication.",
        )

    def withdraw_procedure(
        self,
        *,
        logical_id: str,
        partition: Mapping[str, Any],
        expected_designation_id: str,
        expected_revision_id: str,
        expected_generation: int,
        expected_designation_digest: str,
        issuer: str,
        adapter: Any,
        network_resolution: Mapping[str, Any] | Any | None = None,
    ) -> AdminResult:
        action = "withdraw_procedure"
        authorized_partition = self._authorize_partition(action, partition)
        current = self._store.get_current_procedure_designation(
            logical_id, authorized_partition
        )
        if current is None or current.get("state") != "active":
            raise MemoryAdminError(
                "stale_identity", "the partition has no exact active designation", action=action
            )
        for field, expected in (
            ("designation_id", expected_designation_id),
            ("revision_id", expected_revision_id),
            ("generation", expected_generation),
            ("content_hash", expected_designation_digest),
        ):
            self._require_equal(action, field, current[field], expected)
        try:
            designation = self._store.get_procedure_designation(
                expected_designation_id
            )
            revision = self._store.get_procedure_revision(expected_revision_id)
        except StoreError as exc:
            raise MemoryAdminError(
                "invalid_durable_state",
                "the active designation cannot resolve its procedure revision",
                action=action,
            ) from exc
        self._require_equal(
            action,
            "designation_digest",
            designation["content_hash"],
            expected_designation_digest,
        )
        self._require_equal(
            action,
            "revision_digest",
            revision["content_hash"],
            designation["procedure_digest"],
        )
        self._authorize_procedure(action, revision)
        try:
            outcome = self._procedures.withdraw_partition(
                logical_id=logical_id,
                partition=authorized_partition,
                issuer=issuer,
                adapter=adapter,
                network_resolution=network_resolution,
            )
        except (procedures.ProcedureError, StoreError) as exc:
            raise MemoryAdminError(
                "operation_failed", "the exact procedure partition was not withdrawn", action=action
            ) from exc
        withdrawal = outcome["withdrawal"]
        complete = outcome["managed_complete"] is True
        return self._result(
            action,
            "withdrawn" if complete else "withdrawn_pending_remote",
            {
                "logical_id": withdrawal["logical_id"],
                "revision_id": withdrawal["revision_id"],
                "withdrawal_id": withdrawal["withdrawal_id"],
                "withdrawal_digest": withdrawal["content_hash"],
                "partition_id": withdrawal["partition_id"],
                "generation": withdrawal["generation"],
                "managed_complete": complete,
            },
            (
                "Withdrawal is complete for the managed copies."
                if complete
                else "Reconcile the exact remote withdrawal when its configured network is available."
            ),
        )

    def revoke_procedure(
        self,
        *,
        revision_id: str,
        expected_revision_digest: str,
        expected_origin: str,
        issuer: str,
        reason: str,
        adapter: Any,
        network_resolution: Mapping[str, Any] | Any | None = None,
    ) -> AdminResult:
        action = "revoke_procedure"
        revision = self._procedure_revision(
            action,
            revision_id=revision_id,
            expected_revision_digest=expected_revision_digest,
            expected_origin=expected_origin,
        )
        try:
            outcome = self._procedures.revoke(
                procedure=revision,
                issuer=issuer,
                reason=reason,
                adapter=adapter,
                network_resolution=network_resolution,
            )
        except procedures.ProcedureError as exc:
            raise MemoryAdminError(
                "operation_failed", "the exact procedure revision was not revoked", action=action
            ) from exc
        revocation = outcome["revocation"]
        complete = outcome["managed_complete"] is True
        return self._result(
            action,
            "revoked" if complete else "revoked_pending_remote",
            {
                "logical_id": revocation["logical_id"],
                "revision_id": revocation["revision_id"],
                "origin": revision["origin"],
                "revocation_id": revocation["revocation_id"],
                "revocation_digest": revocation["content_hash"],
                "managed_complete": complete,
                "known_exposure_count": len(outcome["exposures"]),
            },
            (
                "Revocation is complete for all managed copies; historical exposures remain evidence."
                if complete
                else "Reconcile the exact remote revocation before claiming managed completion."
            ),
        )

    # -- pending effects -------------------------------------------------

    @staticmethod
    def _effect_summary(operation: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "operation_id": operation["operation_id"],
            "outcome_id": operation["outcome_id"],
            "decision_id": operation["decision_id"],
            "kind": operation["kind"],
            "scope_key": operation["scope_key"],
            "status": operation["status"],
            "version": operation["version"],
            "source_id": operation["source_id"],
            "source_digest": operation["source_digest"],
            "payload_digest": operation["payload_digest"],
            "configuration_digest": operation["configuration_digest"],
            "claim_generation": operation["claim_generation"],
            "active_claim_id": operation["active_claim_id"],
        }

    def list_pending_effects(self, *, outcome_id: str | None = None) -> AdminResult:
        operations = self._store.list_effect_operations(outcome_id, actionable_only=True)
        for operation in operations:
            self._authorize_effect_source(
                "list_pending_effects", operation, allow_unbound=True
            )
        return self._result(
            "list_pending_effects",
            "pending_effects" if operations else "settled",
            {"count": len(operations), "effects": [self._effect_summary(item) for item in operations]},
            (
                "Use the source-specific adapter to obtain exact readback, then reconcile one observed version."
                if operations
                else "No effect operation currently requires settlement."
            ),
        )

    def reconcile_effect(
        self,
        *,
        operation_id: str,
        expected_status: str,
        expected_version: int,
        expected_kind: str,
        expected_source_digest: str,
        evidence: Mapping[str, Any],
        result: str,
        claim_id: str | None = None,
    ) -> AdminResult:
        action = "reconcile_effect"
        try:
            operation = self._store.get_effect_operation(operation_id)
        except StoreError as exc:
            raise MemoryAdminError("not_found", "effect operation not found", action=action) from exc
        for field, expected in (
            ("status", expected_status),
            ("version", expected_version),
            ("kind", expected_kind),
            ("source_digest", expected_source_digest),
        ):
            self._require_equal(action, field, operation[field], expected)
        self._authorize_effect_source(action, operation)
        if operation["outcome_id"] is not None:
            raise MemoryAdminError(
                "source_specific_reconciliation_required",
                "outcome-owned effects must be settled by their source-specific coordinator",
                action=action,
                evidence={"operation_id": operation_id, "kind": operation["kind"]},
                next_step="Run the reviewed-outcome settlement coordinator for this exact effect.",
            )
        try:
            reconciled = self._store.reconcile_external_effect_operation(
                operation_id,
                evidence=evidence,
                result=result,
                claim_id=claim_id,
            )
        except (contracts.ContractError, StoreError) as exc:
            raise MemoryAdminError(
                "operation_failed",
                "the exact effect evidence did not authorize reconciliation",
                action=action,
                next_step="Obtain exact adapter readback bound to the current operation and claim identity.",
            ) from exc
        return self._result(
            action,
            "reconciled",
            {
                **self._effect_summary(reconciled),
                "effect_status": reconciled["status"],
                "reconciliation_result": result,
            },
            (
                "The effect is settled."
                if reconciled["status"] == "confirmed"
                else "Use the retained exact reconciliation state before any same-ID retry."
            ),
        )

    # -- snapshots -------------------------------------------------------

    @staticmethod
    def _read_snapshot_manifest(artifact: str | Path) -> dict[str, Any]:
        try:
            value = json.loads((Path(artifact).resolve() / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MemoryAdminError(
                "invalid_snapshot", "the snapshot manifest is missing or invalid", action="snapshot_readiness"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != "snapshot/v1"
            or value.get("content_hash") != contracts.content_hash(value)
        ):
            raise MemoryAdminError(
                "invalid_snapshot", "the snapshot manifest integrity check failed", action="snapshot_readiness"
            )
        return value

    def export_snapshot(
        self,
        *,
        artifact: str | Path,
        credential: object,
        dependencies: Iterable[Mapping[str, str]] = (),
    ) -> AdminResult:
        try:
            manifest = self._snapshots.export(
                self._store,
                artifact,
                scope=self.context.scope,
                credential=credential,
                dependencies=dependencies,
            )
        except RuntimeError as exc:
            raise MemoryAdminError(
                "operation_failed", "the authorized snapshot export failed", action="export_snapshot"
            ) from exc
        return self._result(
            "export_snapshot",
            "exported",
            {
                "snapshot_digest": manifest["content_hash"],
                "database_digest": manifest["database_sha256"],
                "store_schema_digest": manifest["store_schema_digest"],
                "completeness": manifest["completeness"],
                "dependency_count": len(manifest["dependencies"]),
                "pending_effect_count": len(manifest["pending_effects"]),
                "pending_dispatch_count": len(manifest["pending_dispatches"]),
                "pending_procedure_remote_count": len(manifest["pending_procedure_remote"]),
            },
            "Inspect readiness and preserve the exact snapshot digest before restore.",
        )

    def snapshot_readiness(
        self,
        *,
        artifact: str | Path,
        expected_snapshot_digest: str,
        credential: object,
    ) -> AdminResult:
        action = "snapshot_readiness"
        try:
            inspected = self._snapshots.inspect(
                artifact,
                scope=self.context.scope,
                credential=credential,
            )
        except RuntimeError as exc:
            raise MemoryAdminError(
                "invalid_snapshot",
                "snapshot bytes, schema, recovery facts, or authorization are invalid",
                action=action,
                next_step="Produce a new authorized snapshot; do not restore this artifact.",
            ) from exc
        manifest = inspected["capture"]
        self._require_equal(
            action, "snapshot_digest", manifest["content_hash"], expected_snapshot_digest
        )
        state = inspected["inspection"]
        current = state["dependencies"]
        capabilities = state["capabilities"]
        ready = state["completeness"] == "complete"
        return self._result(
            action,
            "ready" if ready else "incomplete",
            {
                "snapshot_digest": manifest["content_hash"],
                "capture_completeness": manifest["completeness"],
                "capabilities": capabilities,
                "dependencies": current,
                "pending_effects": dict(manifest.get("pending_effects", {})),
                "pending_dispatches": dict(manifest.get("pending_dispatches", {})),
                "pending_procedure_remote": dict(manifest.get("pending_procedure_remote", {})),
            },
            (
                "The captured dependencies remain ready; ROOT may request restore."
                if ready
                else "Restore only with an explicit reduced capability set or make dependencies ready."
            ),
        )

    def import_snapshot(
        self,
        *,
        artifact: str | Path,
        target: MemoryStore,
        credential: object,
        expected_snapshot_digest: str,
        required_capabilities: Iterable[str] = ("local",),
    ) -> AdminResult:
        action = "import_snapshot"
        if not isinstance(target, MemoryStore):
            raise MemoryAdminError(
                "invalid_context",
                "restore target must be a caller-supplied trusted MemoryStore object",
                action=action,
            )
        manifest = self._read_snapshot_manifest(artifact)
        self._require_equal(
            action, "snapshot_digest", manifest["content_hash"], expected_snapshot_digest
        )
        try:
            restored = self._snapshots.restore(
                artifact,
                target,
                scope=self.context.scope,
                credential=credential,
                required_capabilities=required_capabilities,
            )
        except RuntimeError as exc:
            raise MemoryAdminError(
                "operation_failed", "the authorized snapshot import failed", action=action
            ) from exc
        state = restored["restored"]
        return self._result(
            action,
            "imported",
            {
                "snapshot_digest": restored["capture"]["content_hash"],
                "database_digest": restored["capture"]["database_sha256"],
                "completeness": state["completeness"],
                "capabilities": dict(state["capabilities"]),
                "dependencies": [dict(item) for item in state["dependencies"]],
            },
            "Open the restored store through trusted composition and reconcile retained pending effects explicitly.",
        )


__all__ = [
    "ADMIN_RESULT_SCHEMA",
    "AdminResult",
    "MemoryAdmin",
    "MemoryAdminError",
    "RootAdminContext",
]

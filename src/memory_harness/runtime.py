"""Runtime coordination for the smallest Stage-A vertical slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import contracts
from .config import MemoryConfig, resolve_config
from .privacy import PrivacyPolicy, guard_mandatory, sanitize_payload
from .store import MemoryStore


class RuntimeError(ValueError):
    """A Stage-A runtime invariant was violated."""


class DispatchAmbiguityError(RuntimeError):
    """A launch acknowledgement was ambiguous and cannot be safely duplicated."""


class PlanNotAcceptedError(RuntimeError):
    """A plan that is not ROOT-accepted was treated as execution authority."""


@dataclass(frozen=True)
class PreparedMemory:
    decision: dict[str, Any]
    envelope: dict[str, Any] | None


class MemoryRuntime:
    """Coordinate exact preparation, dispatch, omission, and outcomes."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        privacy_policy: PrivacyPolicy | None = None,
        config: MemoryConfig | None = None,
    ) -> None:
        self.store = store
        self.privacy_policy = privacy_policy or PrivacyPolicy()
        self.config = config or resolve_config()

    def prepare(
        self,
        *,
        plan: Mapping[str, Any],
        task_card: Mapping[str, Any],
        lane_id: str,
        run_id: str,
        worktree_path: str,
        base_commit: str,
        mandatory_content: list[Mapping[str, Any]] | None = None,
        optional_content: list[Mapping[str, Any]] | None = None,
    ) -> PreparedMemory:
        contracts.validate_task_card(task_card)
        contracts.validate_plan(plan)
        decision = contracts.make_decision(task_card, plan, strategy=self.config.strategy)
        self.store.record_decision(decision)

        if plan["state"] != "accepted":
            return PreparedMemory(decision=decision, envelope=None)

        mandatory = list(mandatory_content or [])
        guard_mandatory(mandatory, self.privacy_policy)
        sanitized_optional = sanitize_payload(list(optional_content or []), self.privacy_policy)
        if not isinstance(sanitized_optional, list):
            raise RuntimeError("sanitized optional content must remain a list")
        envelope = contracts.make_envelope(
            task_card=task_card,
            plan=plan,
            decision_id=decision["decision_id"],
            lane_id=lane_id,
            run_id=run_id,
            worktree_path=str(worktree_path),
            base_commit=base_commit,
            mandatory_content=mandatory,
            optional_content=sanitized_optional,
        )
        return PreparedMemory(decision=decision, envelope=envelope)

    def dispatch(
        self,
        envelope: Mapping[str, Any],
        launcher: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        if not isinstance(envelope, Mapping):
            raise RuntimeError("dispatch envelope must be an object")
        if envelope.get("plan_state") != "accepted":
            raise PlanNotAcceptedError("only a ROOT-accepted plan may dispatch")
        operation = contracts.make_operation(
            kind="dispatch",
            envelope=envelope,
            status="pending",
        )
        existing = self._try_get_operation(operation["operation_id"])
        if existing is not None:
            if existing["status"] == "delivered":
                return existing
            if existing["status"] == "ambiguous":
                raise DispatchAmbiguityError(
                    "ambiguous dispatch acknowledgement is unresolved; reconcile before retrying"
                )
        self.store.record_operation(operation)
        observed = launcher(envelope)
        if observed is None:
            ambiguous = contracts.make_operation(
                kind="dispatch",
                envelope=envelope,
                status="ambiguous",
            )
            self.store.record_operation(ambiguous)
            raise DispatchAmbiguityError(
                "ambiguous launch acknowledgement was lost; reconcile the exact existing invocation before retrying"
            )
        if not isinstance(observed, Mapping):
            raise RuntimeError("observed harness invocation must be an object or null")
        invocation_id = observed.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise RuntimeError("observed harness invocation requires an invocation_id")
        delivered = contracts.make_operation(
            kind="dispatch",
            envelope=envelope,
            status="delivered",
            observed_invocation=observed,
        )
        return self.store.record_operation(delivered)

    def reconcile_ambiguous_dispatch(
        self,
        envelope: Mapping[str, Any],
        observed_invocation: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(observed_invocation, Mapping):
            raise RuntimeError("observed harness invocation must be an object")
        invocation_id = observed_invocation.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise RuntimeError("observed harness invocation requires an invocation_id")
        operation = contracts.make_operation(
            kind="dispatch",
            envelope=envelope,
            status="ambiguous",
        )
        existing = self._try_get_operation(operation["operation_id"])
        if existing is None:
            raise DispatchAmbiguityError("no ambiguous dispatch exists to reconcile")
        if existing["status"] == "delivered":
            return existing
        if existing["status"] != "ambiguous":
            raise DispatchAmbiguityError("dispatch is not in an ambiguous state")
        delivered = contracts.make_operation(
            kind="dispatch",
            envelope=envelope,
            status="delivered",
            observed_invocation=observed_invocation,
        )
        return self.store.record_operation(delivered)

    def omit_optional_content(
        self, envelope: Mapping[str, Any], item_id: str
    ) -> dict[str, Any]:
        if not isinstance(envelope, Mapping):
            raise RuntimeError("dispatch envelope must be an object")
        optional = list(envelope.get("optional_content", []))
        if not any(item.get("id") == item_id for item in optional):
            raise RuntimeError(f"optional content item is not packed: {item_id}")
        remaining = [dict(item) for item in optional if item.get("id") != item_id]
        omitted = list(envelope.get("delivery", {}).get("omitted", []))
        if item_id not in omitted:
            omitted.append(item_id)
        revised = dict(envelope)
        revised["optional_content"] = remaining
        revised["optional_digest"] = contracts.sha256_hex(remaining)
        revised["delivery"] = {
            "mandatory": list(envelope.get("delivery", {}).get("mandatory", [])),
            "optional": [item["id"] for item in remaining],
            "omitted": omitted,
        }
        revised["content_hash"] = contracts.content_hash(revised)
        return revised

    def record_outcome(
        self,
        *,
        decision_id: str,
        plan_id: str,
        plan_digest: str,
        status: str,
        evidence_digest: str,
        linked_run_id: str,
    ) -> dict[str, Any]:
        outcome = contracts.make_outcome(
            decision_id=decision_id,
            plan_id=plan_id,
            plan_digest=plan_digest,
            status=status,
            evidence_digest=evidence_digest,
            linked_run_id=linked_run_id,
        )
        return self.store.record_outcome(outcome)

    def _try_get_operation(self, operation_id: str) -> dict[str, Any] | None:
        try:
            return self.store.get_operation(operation_id)
        except Exception:
            return None


__all__ = [
    "MemoryRuntime",
    "PreparedMemory",
    "RuntimeError",
    "DispatchAmbiguityError",
    "PlanNotAcceptedError",
]

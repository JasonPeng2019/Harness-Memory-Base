"""Idempotent accepted-outcome settlement into reviewed product memory.

Native review remains the authority.  This coordinator merely joins that
already accepted evidence to the existing review-receipt, reviewed-trajectory,
optional EverOS-ingestion, and usage ledgers.  It never approves or publishes a
procedure and never retries a remote write without the source service's exact
reconciliation path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping


class OutcomeSettlementError(RuntimeError):
    """Accepted native evidence could not be settled consistently."""


def _outcome(evidence: Mapping[str, Any]) -> dict[str, Any]:
    from memory_harness import contracts

    decision = evidence["decision"]
    plan = evidence["accepted_plan"]
    return contracts.make_outcome(
        decision_id=decision["decision_id"],
        plan_id=plan["plan_id"],
        plan_digest=plan["content_hash"],
        status=evidence["review"]["review_outcome"],
        evidence_digest=evidence["content_hash"],
        linked_run_id=evidence["run_id"],
        task_card_digest=evidence["task_card"]["content_hash"],
        objective_id=evidence["objective_id"],
        observed_at=evidence["review"]["reviewed_at"],
    )


def _review_receipt(
    evidence: Mapping[str, Any], outcome: Mapping[str, Any]
) -> dict[str, Any]:
    from memory_harness import contracts

    review = evidence["review"]
    references = [
        f"native-review:{review['content_hash']}",
        f"root-acceptance:{evidence['acceptance']['content_hash']}",
    ]
    result = evidence.get("result")
    if isinstance(result, Mapping):
        references.append(f"native-result:{result['content_hash']}")
    else:
        references.append(
            f"terminal-proof:{evidence['terminal_proof']['content_hash']}"
        )
    raw_evidence = review.get("review_summary")
    if not isinstance(raw_evidence, str) or not raw_evidence.strip():
        raw_evidence = (
            f"Native ROOT review {review['review_outcome']} for run "
            f"{evidence['run_id']} at commit {review['commit']}"
        )
    failed = review.get("failed_hypotheses", [])
    if not isinstance(failed, list) or any(
        not isinstance(item, str) or not item for item in failed
    ):
        raise OutcomeSettlementError("review failed_hypotheses must be a string list")
    return contracts.make_review_receipt(
        review_id=review["content_hash"],
        outcome=outcome,
        decision=evidence["decision"],
        task_card=evidence["task_card"],
        plan=evidence["accepted_plan"],
        reviewed_by="ROOT",
        evidence_refs=references,
        protected_source_refs=(f"native-terminal:{evidence['content_hash']}",),
        raw_evidence=raw_evidence,
        failed_hypotheses=failed,
    )


def _settle_store(
    memory_store: Any,
    *,
    evidence: Mapping[str, Any],
    outcome: Mapping[str, Any],
    receipt: Mapping[str, Any],
    scope: Any,
    privacy_policy: Any,
    replicate_authority: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from memory_harness import experience

    if replicate_authority:
        memory_store.record_decision(evidence["decision"])
        memory_store.record_outcome(outcome)
    service = experience.ReviewedExperienceService(
        memory_store, privacy_policy=privacy_policy
    )
    trajectory = service.capture(
        task_card=evidence["task_card"],
        plan=evidence["accepted_plan"],
        decision=evidence["decision"],
        outcome=outcome,
        review_receipt=receipt,
        scope=scope,
    )
    return memory_store.get_review_receipt(receipt["review_receipt_id"]), trajectory


def _run(coroutine: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    close = getattr(coroutine, "close", None)
    if callable(close):
        close()
    raise OutcomeSettlementError(
        "synchronous native closeout cannot run inside an active event loop"
    )


def settle_accepted_outcome(
    *,
    worktree_path: str | Path,
    evidence: Mapping[str, Any],
    local_store: Any,
    composition: Any,
) -> dict[str, Any]:
    """Settle one accepted native outcome locally and into central memory.

    Replaying the exact evidence returns the same identities.  Conflicting
    evidence is rejected by the underlying immutable stores.  EverOS is called
    only when the trusted product config explicitly enabled and successfully
    composed it; Atlas has no automatic outcome publication in the current
    product contract.
    """

    if evidence.get("acceptance", {}).get("approval") != "ACCEPTED":
        raise OutcomeSettlementError("only a ROOT-accepted outcome may be settled")
    expected_store = (
        Path(worktree_path) / ".agent-workspace" / "memory-state.sqlite3"
    ).resolve()
    local_path = getattr(local_store, "path", None)
    if local_path is None or Path(local_path).resolve() != expected_store:
        raise OutcomeSettlementError(
            "local settlement store does not belong to the reviewed worktree"
        )
    try:
        outcome = _outcome(evidence)
        receipt = _review_receipt(evidence, outcome)
        local_receipt, local_trajectory = _settle_store(
            local_store,
            evidence=evidence,
            outcome=outcome,
            receipt=receipt,
            scope=composition.scope,
            privacy_policy=composition.privacy_policy,
            replicate_authority=False,
        )
        central_receipt, central_trajectory = _settle_store(
            composition.store,
            evidence=evidence,
            outcome=outcome,
            receipt=receipt,
            scope=composition.scope,
            privacy_policy=composition.privacy_policy,
            replicate_authority=True,
        )
        ingestion = None
        generated_candidates: list[dict[str, Any]] = []
        if composition.everos_adapter is not None:
            from memory_harness import experience as experience_module

            ingestion = _run(
                composition.experience_service.extract_trajectory(
                    central_trajectory["trajectory_id"],
                    composition.everos_adapter,
                    experience_write=True,
                    current_config=composition.resolved_memory_config,
                )
            )
            if ingestion is not None and ingestion.get("status") == "confirmed":
                # EverOS may derive skills as a side effect of the configured
                # extraction.  Discover only this exact scoped/query surface;
                # the service then rejoins each hit to confirmed case receipts.
                # The returned object remains a proposed candidate and is not
                # approved, designated, executed, or published here.
                skills = _run(
                    composition.everos_adapter.search_skill_candidates(
                        query=central_trajectory["task_text"]
                    )
                )
                for skill in skills:
                    try:
                        candidate = composition.experience_service.resolve_generated_skill_candidate(
                            skill,
                            composition.everos_adapter,
                            current_config=composition.resolved_memory_config,
                        )
                    except experience_module.ProvenanceError:
                        # A remote hit without an exact local provenance join
                        # is not a product candidate and cannot block settling
                        # the reviewed trajectory itself.
                        continue
                    if candidate is not None:
                        generated_candidates.append(dict(candidate))
        effects: dict[str, str] = {}
        for operation in local_store.list_effect_operations(outcome["outcome_id"]):
            effects[str(operation["kind"])] = str(operation["status"])
        return {
            "schema": "memory-outcome-settlement/v1",
            "status": "settled",
            "outcome_id": outcome["outcome_id"],
            "review_receipt_id": local_receipt["review_receipt_id"],
            "trajectory_id": local_trajectory["trajectory_id"],
            "central_review_receipt_id": central_receipt["review_receipt_id"],
            "central_trajectory_id": central_trajectory["trajectory_id"],
            "everos": (
                {"status": "not_configured", "called": False}
                if composition.everos_adapter is None
                else {
                    "status": None if ingestion is None else ingestion.get("status"),
                    "called": True,
                    "ingestion_id": (
                        None if ingestion is None else ingestion.get("ingestion_id")
                    ),
                }
            ),
            "atlas_telemetry": {
                "status": "not_configured",
                "called": False,
            },
            "generated_skill": {
                "status": (
                    "candidate_retained"
                    if generated_candidates
                    else "no_candidate_returned"
                ),
                "candidate_ids": [
                    item["candidate_id"] for item in generated_candidates
                ],
                "approved": False,
                "published": False,
            },
            "local_effects": effects,
        }
    except OutcomeSettlementError:
        raise
    except Exception as exc:
        raise OutcomeSettlementError(
            f"accepted outcome settlement failed: {exc}"
        ) from exc


__all__ = [
    "OutcomeSettlementError",
    "settle_accepted_outcome",
]

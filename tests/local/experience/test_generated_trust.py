from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import (
    config,
    contracts,
    experience,
    local_procedure_adapters,
    privacy,
    procedures,
    store,
    templates,
)


VERIFICATION_ORACLE_BINDINGS = {
    "GeneratedSkillTrustTests::test_revoked_and_stale_replay_cannot_regain_trust_after_restart": {
        "requirement_id": "T09",
        "evidence_class": "LOCAL",
        "scenario_id": "T09.LOCAL.trust-replay-fence",
        "positive_oracle_id": "T09.LOCAL.current-trust-survives-restart",
        "negative_oracle_id": "T09.LOCAL.revoked-stale-replay-denied",
    },
    "GeneratedSkillTrustTests::test_feature_dependency_and_deferred_gates_make_zero_calls_or_optional_writes": {
        "requirement_id": "T16",
        "evidence_class": "LOCAL",
        "scenario_id": "T16.LOCAL.feature-dependency-gate",
        "positive_oracle_id": "T16.LOCAL.valid-dependencies-resolve",
        "negative_oracle_id": "T16.LOCAL.zero-call-zero-write-on-disabled-dependency",
    },
}


class _EverOSResults:
    def __init__(self) -> None:
        self.case_results: list[dict] = []
        self.memorize_calls: list[dict] = []
        self.search_calls: list[dict] = []

    async def memorize(self, payload: dict, **__: object) -> dict:
        self.memorize_calls.append(dict(payload))
        return {"status": "extracted", "message_count": 1}

    def make_search_request(self, **kwargs: object) -> dict:
        return dict(kwargs)

    async def search(self, request: object) -> dict:
        self.search_calls.append(dict(request) if isinstance(request, dict) else {})
        return {
            "request_id": "search-1",
            "data": {"agent_cases": list(self.case_results), "agent_skills": []},
        }


class _TrustedRootApprovalVerifier:
    """A test-only configured trust policy, not caller-provided authority."""

    def verify_generated_skill_approval(
        self,
        *,
        issuer: str,
        candidate: dict,
        scope: dict,
        recipients: tuple[str, ...],
    ) -> object:
        if issuer != "ROOT":
            raise experience.ApprovalError("issuer is not authenticated by this policy")
        if recipients != (scope["owner"],):
            raise experience.ApprovalError("recipient is outside this policy's owner")
        return experience.VerifiedApproval(
            issuer=issuer,
            candidate_id=candidate["candidate_id"],
            scope=scope,
            recipients=recipients,
            authority_evidence={
                "policy_id": "test-root-approval/v1",
                "authenticated_subject": "root-operator",
            },
        )


class GeneratedSkillTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.memory_store = store.MemoryStore(self.root / "memory.sqlite3")
        self.memory_store.initialize()
        self.policy = privacy.PrivacyPolicy(
            known_secrets=("synthetic-secret-alpha-1234567890",),
        )
        self.scope = experience.ExperienceScope(
            application="harness",
            project="product",
            namespace="isolated",
            owner="root-agent",
        )
        self.service = experience.ReviewedExperienceService(
            self.memory_store, privacy_policy=self.policy
        )
        self.results = _EverOSResults()
        self.adapter = experience.EverOSAdapter(
            scope=self.scope,
            base_root=self.root / "everos",
            surface=experience.EverOSPublicSurface.from_object(
                self.results,
                memory_root=experience.EverOSAdapter.memory_root_for_scope(
                    self.root / "everos", self.scope
                ),
                resolve_memory_root=lambda: experience.EverOSAdapter.memory_root_for_scope(
                    self.root / "everos", self.scope
                ),
            ),
            privacy_policy=self.policy,
        )

    def tearDown(self) -> None:
        self.memory_store.close()
        self.temporary.cleanup()

    def _reviewed_trajectory(self, suffix: str) -> dict:
        plan = contracts.make_plan(
            plan_id=f"plan-{suffix}",
            objective_id=f"objective-{suffix}",
            route="ordinary",
            state="accepted",
            content={"steps": ["inspect", "repair", "verify"]},
            accepted_by="ROOT",
        )
        task_card = contracts.make_task_card(
            task="Repair parser failure",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id=f"objective-{suffix}", route="ordinary", plan=plan
            ),
        )
        decision = contracts.make_decision(task_card, plan)
        self.memory_store.record_decision(decision)
        outcome = contracts.make_outcome(
            decision_id=decision["decision_id"],
            plan_id=plan["plan_id"],
            plan_digest=plan["content_hash"],
            status="PASS",
            evidence_digest=f"evidence-{suffix}",
            linked_run_id=f"run-{suffix}",
            task_card_digest=task_card["content_hash"],
            objective_id=f"objective-{suffix}",
        )
        self.memory_store.record_outcome(outcome)
        review = contracts.make_review_receipt(
            review_id=f"review-{suffix}",
            outcome=outcome,
            decision=decision,
            task_card=task_card,
            plan=plan,
            reviewed_by="ROOT",
            evidence_refs=(f"review://run-{suffix}",),
            raw_evidence="The parser repair passed after the local lock was released.",
            failed_hypotheses=("network timeout",),
        )
        trajectory = self.service.capture(
            task_card=task_card,
            plan=plan,
            decision=decision,
            outcome=outcome,
            review_receipt=review,
            scope=self.scope,
        )
        return trajectory

    def _confirmed_case(self) -> dict:
        trajectory = self._reviewed_trajectory("1")
        pending = asyncio.run(
            self.service.extract_trajectory(trajectory["trajectory_id"], self.adapter)
        )
        self.results.case_results = [
            {
                "id": "case-1",
                "agent_id": self.adapter.everos_owner_id,
                "app_id": self.adapter.everos_application_id,
                "project_id": self.adapter.everos_project_id,
                "session_id": pending["session_id"],
                "task_intent": "repair parser failure",
                "approach": "release local lock",
                "quality_score": 1.0,
                "key_insight": "network timeout was disproved",
                "timestamp": "2026-09-21T00:00:00Z",
                "score": 1.0,
            }
        ]
        confirmed = asyncio.run(
            self.service.reconcile_extraction(trajectory["trajectory_id"], self.adapter)
        )
        self.assertEqual("confirmed", confirmed["status"])
        return trajectory

    def _curated_procedure(
        self,
        service: procedures.TrustedProcedureService,
        *,
        logical_name: str,
        body: str,
        approval_id: str,
        created_at: str,
    ) -> tuple[dict, dict, dict]:
        receiver = self.scope.to_record()
        partition = {
            "scope": "project",
            "application": receiver["application"],
            "project": receiver["project"],
            "namespace": receiver["namespace"],
            "recipients": [receiver],
        }
        procedure = contracts.make_procedure_revision(
            logical_name=logical_name,
            origin="curated",
            origin_scope=receiver,
            body=body,
            references=[
                {"id": f"guide://{logical_name}", "content": "Retain exact trust evidence."}
            ],
            predicates={
                "applicability": {},
                "conflicts": {},
                "capabilities": {},
                "routes": {
                    "all": [
                        {"field": "route", "operator": "equals", "value": "ordinary"}
                    ]
                },
            },
            source={
                "kind": "curated_authoring",
                "provenance_ref": f"curation://root/{logical_name}",
            },
            created_at=created_at,
        )
        approval = contracts.make_procedure_approval(
            approval_id=approval_id,
            procedure=procedure,
            issuer="ROOT",
            recipients=[receiver],
            authority_evidence={"policy_id": "trusted-root/v1", "subject": "root"},
            approved_at=created_at,
        )
        approval = service.record_approved_revision(procedure, approval)
        identity = templates.representation_identity()
        service.record_representation(
            contracts.make_procedure_representation(
                procedure=procedure,
                model=identity["model"],
                dimensions=identity["dimensions"],
                metric=identity["metric"],
                sanitizer_version=identity["sanitizer_version"],
                search_text="parser lock replay trust evidence",
                vector=[0.01] * int(identity["dimensions"]),
                created_at=created_at,
            )
        )
        designation = service.designate(
            procedure=procedure,
            approval=approval,
            partition=partition,
            issuer="ROOT",
        )
        return procedure, approval, designation

    def _curated_candidates(
        self, service: procedures.TrustedProcedureService
    ) -> list[dict]:
        curated = next(
            item
            for item in local_procedure_adapters.make_local_procedure_search_stores(
                procedure_service=service,
                memory_store=self.memory_store,
                receiver=self.scope.to_record(),
                facts={},
                route="ordinary",
            )
            if item.store_id == local_procedure_adapters.CURATED_STORE_ID
        )
        objective = templates.objective_representation(
            "parser lock replay trust evidence", route="ordinary"
        )
        payload = {
            "representation": {
                key: objective[key]
                for key in ("model", "dimensions", "metric", "sanitizer_version")
            },
            "tokens": objective["tokens"],
            "route": "ordinary",
        }
        return list(curated.query(payload))

    def _skill(self, **updates: object) -> dict:
        skill = {
            "id": "skill-1",
            "agent_id": self.adapter.everos_owner_id,
            "app_id": self.adapter.everos_application_id,
            "project_id": self.adapter.everos_project_id,
            "name": "parser-lock-repair",
            "description": "Use the recorded lock investigation as historical evidence.",
            "content": (
                "Investigate the local lock before network work. "
                "Never execute helper scripts automatically. "
                "synthetic-secret-alpha-1234567890"
            ),
            "confidence": 0.9,
            "maturity_score": 0.8,
            "source_case_ids": ["case-1"],
        }
        skill.update(updates)
        return skill

    def test_generated_candidate_requires_exact_reviewed_sources_and_explicit_approval(
        self,
    ) -> None:
        trajectory = self._confirmed_case()
        candidate = self.service.resolve_generated_skill_candidate(
            self._skill(), self.adapter
        )

        self.assertEqual("generated", candidate["origin"])
        self.assertEqual("proposed", candidate["state"])
        self.assertEqual(["case-1"], [item["case_id"] for item in candidate["source_cases"]])
        self.assertEqual(
            trajectory["review_receipt_id"],
            candidate["source_cases"][0]["review_receipt_id"],
        )
        self.assertEqual(
            trajectory["review_receipt_digest"],
            candidate["source_cases"][0]["review_receipt_digest"],
        )
        self.assertNotIn("synthetic-secret-alpha-1234567890", candidate["content"])
        self.assertEqual(
            candidate,
            self.service.resolve_generated_skill_candidate(self._skill(), self.adapter),
        )

        with self.assertRaisesRegex(experience.ApprovalError, "not configured"):
            self.service.approve_generated_skill(
                candidate_id=candidate["candidate_id"],
                scope=self.scope,
                approval_id="self-asserted-approval",
                issuer="ROOT",
                recipients=("root-agent",),
                approved_at="2026-09-21T00:00:00Z",
            )

        trusted_service = experience.ReviewedExperienceService(
            self.memory_store,
            privacy_policy=self.policy,
            approval_verifier=_TrustedRootApprovalVerifier(),
        )
        approval = trusted_service.approve_generated_skill(
            candidate_id=candidate["candidate_id"],
            scope=self.scope,
            approval_id="approval-1",
            issuer="ROOT",
            recipients=("root-agent",),
            approved_at="2026-09-21T00:00:00Z",
        )
        self.assertEqual("generated", approval["origin"])
        self.assertEqual(candidate["content_digest"], approval["content_digest"])
        self.assertEqual(("root-agent",), tuple(approval["recipients"]))
        self.assertEqual(
            {
                "policy_id": "test-root-approval/v1",
                "authenticated_subject": "root-operator",
            },
            approval["authority_evidence"],
        )

        with self.assertRaisesRegex(experience.ApprovalError, "issuer is not authenticated"):
            trusted_service.approve_generated_skill(
                candidate_id=candidate["candidate_id"],
                scope=self.scope,
                approval_id="untrusted-approval",
                issuer="worker",
                recipients=("root-agent",),
                approved_at="2026-09-21T00:00:00Z",
            )
        with self.assertRaisesRegex(experience.ApprovalError, "outside"):
            trusted_service.approve_generated_skill(
                candidate_id=candidate["candidate_id"],
                scope=self.scope,
                approval_id="cross-owner-approval",
                issuer="ROOT",
                recipients=("other-agent",),
                approved_at="2026-09-21T00:00:00Z",
            )

        # The persistence boundary repeats the join rather than trusting a
        # caller that presents a candidate id beside a different content hash.
        forged_candidate = contracts.make_generated_skill_candidate(
            scope=candidate["scope"],
            skill_id=candidate["skill_id"],
            content="A different generated candidate must not inherit this approval.",
            source_cases=candidate["source_cases"],
            metadata=candidate["metadata"],
            created_at=candidate["created_at"],
        )
        forged_approval = contracts.make_skill_approval(
            approval_id="durable-candidate-mismatch",
            candidate=forged_candidate,
            issuer="ROOT",
            recipients=("root-agent",),
            approved_at="2026-09-21T00:00:00Z",
            authority_evidence={
                "policy_id": "test-root-approval/v1",
                "authenticated_subject": "root-operator",
            },
        )
        forged_approval["candidate_id"] = candidate["candidate_id"]
        forged_approval["content_hash"] = contracts.content_hash(forged_approval)
        with self.assertRaisesRegex(store.ExperienceConflictError, "exact durable candidate"):
            self.memory_store.record_skill_approval(forged_approval)

        with self.assertRaisesRegex(experience.ProvenanceError, "cannot be resolved"):
            self.service.resolve_generated_skill_candidate(
                self._skill(source_case_ids=["missing-case"]), self.adapter
            )
        with self.assertRaisesRegex(experience.ProvenanceError, "cannot be resolved"):
            self.service.resolve_generated_skill_candidate(
                self._skill(source_case_ids=["case-1", "unreviewed-case"]), self.adapter
            )
        with self.assertRaisesRegex(experience.ProvenanceError, "ambiguous"):
            self.service.resolve_generated_skill_candidate(
                self._skill(source_case_ids=["case-1", "case-1"]), self.adapter
            )
        with self.assertRaisesRegex(experience.ScopeBoundaryError, "outside"):
            self.service.resolve_generated_skill_candidate(
                self._skill(agent_id="other-owner"), self.adapter
            )

    def test_curated_selection_is_explicit_and_excludes_unlisted_items(self) -> None:
        visible = [
            {
                "id": "curated-unlisted",
                "origin": "curated",
                "scope": self.scope.to_record(),
                "content": "Do not inject me unless selected.",
            },
            {
                "id": "curated-selected",
                "origin": "curated",
                "scope": self.scope.to_record(),
                "content": "Selected guidance.",
            },
        ]
        selected = experience.select_curated_guidance(
            visible, scope=self.scope, selected_ids=("curated-selected",)
        )
        self.assertEqual(["curated-selected"], [item["id"] for item in selected])
        with self.assertRaisesRegex(experience.ApprovalError, "not available"):
            experience.select_curated_guidance(
                visible, scope=self.scope, selected_ids=("missing-curated",)
            )
        with self.assertRaises(experience.ScopeBoundaryError):
            experience.select_curated_guidance(
                [
                    {
                        "id": "curated-unscoped",
                        "origin": "curated",
                        "content": "Scope is required for governing guidance.",
                    }
                ],
                scope=self.scope,
                selected_ids=("curated-unscoped",),
            )

    def test_verified_approval_evidence_survives_store_restart(self) -> None:
        self._confirmed_case()
        candidate = self.service.resolve_generated_skill_candidate(
            self._skill(), self.adapter
        )
        trusted_service = experience.ReviewedExperienceService(
            self.memory_store,
            privacy_policy=self.policy,
            approval_verifier=_TrustedRootApprovalVerifier(),
        )
        approval = trusted_service.approve_generated_skill(
            candidate_id=candidate["candidate_id"],
            scope=self.scope,
            approval_id="restart-approval",
            issuer="ROOT",
            recipients=("root-agent",),
            approved_at="2026-09-21T00:00:00Z",
        )

        self.memory_store.close()
        self.memory_store = store.MemoryStore(self.root / "memory.sqlite3")
        self.memory_store.initialize()
        restored = self.memory_store.get_skill_approval(approval["approval_id"])
        self.assertEqual(approval, restored)
        self.assertEqual("generated", restored["origin"])

    def test_revoked_and_stale_replay_cannot_regain_trust_after_restart(self) -> None:
        procedure_service = procedures.TrustedProcedureService(
            self.memory_store,
            trusted_issuers={"ROOT"},
            privacy_policy=self.policy,
        )
        revoked, _revoked_approval, revoked_designation = self._curated_procedure(
            procedure_service,
            logical_name="revoked-replay",
            body="Inspect the revoked parser-lock procedure.",
            approval_id="approval-revoked",
            created_at="2026-09-21T00:00:01Z",
        )
        self.memory_store.record_procedure_revocation(
            contracts.make_procedure_revocation(
                procedure=revoked,
                issuer="ROOT",
                reason="the reviewed procedure was revoked",
                created_at="2026-09-21T00:00:02Z",
            )
        )

        stale, _stale_approval, stale_designation = self._curated_procedure(
            procedure_service,
            logical_name="stale-replay",
            body="Inspect the first parser-lock procedure.",
            approval_id="approval-stale-1",
            created_at="2026-09-21T00:00:03Z",
        )
        current, _current_approval, current_designation = self._curated_procedure(
            procedure_service,
            logical_name="stale-replay",
            body="Inspect the revised parser-lock procedure and verify it.",
            approval_id="approval-stale-2",
            created_at="2026-09-21T00:00:04Z",
        )
        self.assertEqual(stale["logical_id"], current["logical_id"])
        self.assertEqual(1, stale_designation["generation"])
        self.assertEqual(2, current_designation["generation"])

        self.memory_store.close()
        self.memory_store = store.MemoryStore(self.root / "memory.sqlite3")
        self.memory_store.initialize()
        procedure_service = procedures.TrustedProcedureService(
            self.memory_store,
            trusted_issuers={"ROOT"},
            privacy_policy=self.policy,
        )

        with self.assertRaisesRegex(store.ProcedureConflictError, "revoked"):
            self.memory_store.record_procedure_designation(revoked_designation)
        replayed_stale = self.memory_store.record_procedure_designation(stale_designation)
        self.assertEqual(stale_designation, replayed_stale)
        durable_current = self.memory_store.get_current_procedure_designation(
            current["logical_id"], current_designation["partition"]
        )
        self.assertIsNotNone(durable_current)
        self.assertEqual(current["revision_id"], durable_current["revision_id"])
        self.assertEqual(2, durable_current["generation"])

        candidates = self._curated_candidates(procedure_service)
        self.assertEqual(
            [(current["logical_id"], current["revision_id"])],
            [(item["logical_id"], item["revision_id"]) for item in candidates],
        )
        self.assertNotIn(
            revoked["revision_id"], {item["revision_id"] for item in candidates}
        )
        self.assertNotIn(
            stale["revision_id"], {item["revision_id"] for item in candidates}
        )

    def test_feature_dependency_and_deferred_gates_make_zero_calls_or_optional_writes(
        self,
    ) -> None:
        enabled = config.resolve_config(
            {"experience_write": True, "generated_skill_creation": True}
        )
        self.assertTrue(enabled.experience_write)
        self.assertTrue(enabled.generated_skill_creation)

        blocked = config.resolve_config(
            {"experience_write": False, "generated_skill_creation": True}
        )
        generated = blocked.feature_state_by_name["generated_skill_creation"]
        self.assertTrue(generated.requested)
        self.assertFalse(generated.effective)
        self.assertEqual(("experience_write",), generated.prerequisites)

        trajectory = self._reviewed_trajectory("dependency-gate")
        connection = self.memory_store._require_connection()
        optional_tables = (
            "effect_operations",
            "experience_ingestions",
            "experience_case_receipts",
            "generated_skill_candidates",
            "generated_skill_approvals",
        )
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in optional_tables
        }
        self.results.memorize_calls.clear()
        self.results.search_calls.clear()
        self.assertIsNone(
            asyncio.run(
                self.service.extract_trajectory(
                    trajectory["trajectory_id"],
                    self.adapter,
                    current_config=blocked,
                )
            )
        )
        self.assertEqual([], self.results.memorize_calls)
        self.assertEqual([], self.results.search_calls)
        self.assertIsNone(
            self.memory_store.get_experience_ingestion_for_trajectory(
                trajectory["trajectory_id"]
            )
        )
        after_dependency = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in optional_tables
        }
        self.assertEqual(before, after_dependency)

        with self.assertRaisesRegex(
            config.DeferredCapabilityError, "deferred/not implemented"
        ):
            config.resolve_config(
                {"strategy": "learned", "policy_load": True, "policy_update": True}
            )
        self.assertEqual([], self.results.memorize_calls)
        self.assertEqual([], self.results.search_calls)
        after_deferred = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in optional_tables
        }
        self.assertEqual(before, after_deferred)


if __name__ == "__main__":
    unittest.main()

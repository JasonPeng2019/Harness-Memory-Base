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

from memory_harness import contracts, experience, privacy, store


class _EverOSResults:
    def __init__(self) -> None:
        self.case_results: list[dict] = []

    async def memorize(self, _: dict, **__: object) -> dict:
        return {"status": "extracted", "message_count": 1}

    def make_search_request(self, **kwargs: object) -> dict:
        return dict(kwargs)

    async def search(self, _: object) -> dict:
        return {
            "request_id": "search-1",
            "data": {"agent_cases": list(self.case_results), "agent_skills": []},
        }


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
            surface=experience.EverOSPublicSurface.from_object(self.results),
            privacy_policy=self.policy,
        )

    def tearDown(self) -> None:
        self.memory_store.close()
        self.temporary.cleanup()

    def _confirmed_case(self) -> dict:
        plan = contracts.make_plan(
            plan_id="plan-1",
            objective_id="objective-1",
            route="ordinary",
            state="accepted",
            content={"steps": ["inspect", "repair", "verify"]},
            accepted_by="ROOT",
        )
        task_card = contracts.make_task_card(
            task="Repair parser failure",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1", route="ordinary", plan=plan
            ),
        )
        decision = contracts.make_decision(task_card, plan)
        self.memory_store.record_decision(decision)
        outcome = contracts.make_outcome(
            decision_id=decision["decision_id"],
            plan_id=plan["plan_id"],
            plan_digest=plan["content_hash"],
            status="PASS",
            evidence_digest="evidence-1",
            linked_run_id="run-1",
            task_card_digest=task_card["content_hash"],
            objective_id="objective-1",
        )
        self.memory_store.record_outcome(outcome)
        review = contracts.make_review_receipt(
            review_id="review-1",
            outcome=outcome,
            decision=decision,
            task_card=task_card,
            plan=plan,
            reviewed_by="ROOT",
            evidence_refs=("review://run-1",),
        )
        trajectory = self.service.capture(
            task_card=task_card,
            plan=plan,
            decision=decision,
            outcome=outcome,
            review_receipt=review,
            scope=self.scope,
            raw_evidence="The parser repair passed after the local lock was released.",
            failed_hypotheses=("network timeout",),
        )
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

        approval = self.service.approve_generated_skill(
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

        with self.assertRaisesRegex(contracts.ContractError, "only ROOT may approve"):
            self.service.approve_generated_skill(
                candidate_id=candidate["candidate_id"],
                scope=self.scope,
                approval_id="untrusted-approval",
                issuer="worker",
                recipients=("root-agent",),
                approved_at="2026-09-21T00:00:00Z",
            )

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


if __name__ == "__main__":
    unittest.main()

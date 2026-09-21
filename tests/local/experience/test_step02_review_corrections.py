from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import contracts, experience, store


class _FakeEverOS:
    async def memorize(self, _: dict, **__: object) -> dict:
        return {"status": "extracted"}

    def make_search_request(self, **kwargs: object) -> dict:
        return dict(kwargs)

    async def search(self, _: object) -> dict:
        return {"data": {"agent_cases": [], "agent_skills": []}}


class EverOSRootBindingRegressionTests(unittest.TestCase):
    def test_loader_fails_closed_when_root_is_absent_or_mismatched(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        expected_root = Path(temporary.name) / "expected"
        wrong_root = Path(temporary.name) / "wrong"

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(experience.ScopeBoundaryError, "EVEROS_ROOT"):
                experience.load_vendored_everos_public_surface(memory_root=expected_root)
        with patch.dict(os.environ, {"EVEROS_ROOT": str(wrong_root)}, clear=True):
            with self.assertRaisesRegex(experience.ScopeBoundaryError, "namespace"):
                experience.load_vendored_everos_public_surface(memory_root=expected_root)

    def test_surface_root_is_captured_and_cannot_be_rebound_by_later_environment(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base_root = Path(temporary.name) / "everos"
        first_scope = experience.ExperienceScope(
            application="harness",
            project="product",
            namespace="one",
            owner="root-agent",
        )
        stale_scope = experience.ExperienceScope(
            application="harness",
            project="product",
            namespace="two",
            owner="root-agent",
        )
        first_root = experience.EverOSAdapter.memory_root_for_scope(base_root, first_scope)
        stale_root = experience.EverOSAdapter.memory_root_for_scope(base_root, stale_scope)
        surface = experience.EverOSPublicSurface.from_object(
            _FakeEverOS(), memory_root=first_root
        )

        with patch.dict(os.environ, {"EVEROS_ROOT": str(stale_root)}, clear=True):
            bound = experience.EverOSAdapter(
                scope=first_scope,
                base_root=base_root,
                surface=surface,
            )
            self.assertEqual(first_root, bound.memory_root)
            with self.assertRaisesRegex(experience.ScopeBoundaryError, "captured"):
                experience.EverOSAdapter(
                    scope=stale_scope,
                    base_root=base_root,
                    surface=surface,
                )


class ReviewReceiptBindingRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.memory_store = store.MemoryStore(self.root / "memory.sqlite3")
        self.memory_store.initialize()
        self.scope = experience.ExperienceScope(
            application="harness",
            project="product",
            namespace="receipt-binding",
            owner="root-agent",
        )

    def tearDown(self) -> None:
        self.memory_store.close()
        self.temporary.cleanup()

    def _records(self) -> tuple[dict, dict, dict, dict]:
        plan = contracts.make_plan(
            plan_id="receipt-plan",
            objective_id="receipt-objective",
            route="ordinary",
            state="accepted",
            content={"steps": ["inspect", "verify"]},
            accepted_by="ROOT",
        )
        card = contracts.make_task_card(
            task="Bind review evidence before capture",
            base_commit="receipt-base",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="receipt-objective", route="ordinary", plan=plan
            ),
        )
        decision = contracts.make_decision(card, plan)
        self.memory_store.record_decision(decision)
        outcome = contracts.make_outcome(
            decision_id=decision["decision_id"],
            plan_id=plan["plan_id"],
            plan_digest=plan["content_hash"],
            status="FAIL",
            evidence_digest="receipt-outcome-evidence",
            linked_run_id="receipt-run",
            task_card_digest=card["content_hash"],
            objective_id=plan["objective_id"],
        )
        self.memory_store.record_outcome(outcome)
        return card, plan, decision, outcome

    def test_receipt_durably_binds_exact_evidence_and_hypotheses_before_capture(self) -> None:
        card, plan, decision, outcome = self._records()
        review = contracts.make_review_receipt(
            review_id="receipt-review",
            outcome=outcome,
            decision=decision,
            task_card=card,
            plan=plan,
            reviewed_by="ROOT",
            evidence_refs=("review://receipt-run",),
            protected_source_refs=("evidence://protected/receipt",),
            raw_evidence="The first hypothesis failed; retain this exact protected narrative.",
            failed_hypotheses=("the remote service was the cause",),
        )
        self.memory_store.record_review_receipt(review)
        expected = contracts.make_reviewed_trajectory(
            task_card=card,
            plan=plan,
            decision=decision,
            outcome=outcome,
            review_receipt=review,
            scope=self.scope.to_record(),
        )
        substituted = dict(expected)
        substituted["raw_evidence"] = "A later caller substituted a different narrative."
        substituted["failed_hypotheses"] = ["a later caller changed the hypothesis"]
        substituted["content_hash"] = contracts.content_hash(substituted)
        with self.assertRaisesRegex(store.TrajectoryConflictError, "review receipt"):
            self.memory_store.record_reviewed_trajectory(substituted)

        service = experience.ReviewedExperienceService(self.memory_store)
        captured = service.capture(
            task_card=card,
            plan=plan,
            decision=decision,
            outcome=outcome,
            review_receipt=review,
            scope=self.scope,
        )
        persisted = self.memory_store.get_review_receipt(review["review_receipt_id"])
        self.assertEqual(review, persisted)
        self.assertEqual(review["raw_evidence"], captured["raw_evidence"])
        self.assertEqual(review["failed_hypotheses"], captured["failed_hypotheses"])

        self.memory_store.close()
        self.memory_store = store.MemoryStore(self.root / "memory.sqlite3")
        self.memory_store.initialize()
        restored = self.memory_store.get_review_receipt(review["review_receipt_id"])
        self.assertEqual(review, restored)
        self.assertEqual(captured, self.memory_store.get_reviewed_trajectory(captured["trajectory_id"]))


if __name__ == "__main__":
    unittest.main()

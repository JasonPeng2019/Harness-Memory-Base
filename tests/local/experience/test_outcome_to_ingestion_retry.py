"""Faulted reviewed-trajectory ingestion at the existing EverOS boundary.

These fixtures use the accepted local outcome and review contracts. Joined
lane-1 effect scheduling is outside this selector.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import contracts, experience, privacy, store


SECRET = "synthetic-secret-alpha-1234567890"


class _EverOSSurface:
    def __init__(self) -> None:
        self.memorize_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.case_results: list[dict] = []
        self.intent_reader: Callable[[], dict | None] | None = None
        self.intent_at_call: dict | None = None
        self.lose_ack = False
        self.readback_unavailable = False
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def memorize(self, payload: dict, **_: object) -> dict:
        self.memorize_calls.append(dict(payload))
        if self.intent_reader is not None:
            self.intent_at_call = self.intent_reader()
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.lose_ack:
            raise ConnectionError(f"acknowledgement lost after commit: {SECRET}")
        return {"status": "extracted"}

    def make_search_request(self, **kwargs: object) -> dict:
        return dict(kwargs)

    async def search(self, request: dict) -> dict:
        self.search_calls.append(request)
        if self.readback_unavailable:
            raise ConnectionError("EverOS readback is unavailable")
        return {"data": {"agent_cases": list(self.case_results), "agent_skills": []}}


class OutcomeToIngestionRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store_path = self.root / "memory.sqlite3"
        self.memory_store = store.MemoryStore(self.store_path)
        self.memory_store.initialize()
        self.addCleanup(lambda: self.memory_store.close())
        self.policy = privacy.PrivacyPolicy(known_secrets=(SECRET,))
        self.scope = experience.ExperienceScope(
            application="harness",
            project="product",
            namespace="retry-fixture",
            owner="root-agent",
        )
        self.service = experience.ReviewedExperienceService(
            self.memory_store, privacy_policy=self.policy
        )

    def _capture(self) -> dict:
        plan = contracts.make_plan(
            plan_id="retry-plan",
            objective_id="retry-objective",
            route="ordinary",
            state="accepted",
            content={"steps": ["inspect", "repair", "verify"]},
            accepted_by="ROOT",
        )
        card = contracts.make_task_card(
            task="Repair parser failure",
            base_commit="retry-base",
            memory_handoff=contracts.make_memory_handoff(
                objective_id=plan["objective_id"], route="ordinary", plan=plan
            ),
        )
        decision = contracts.make_decision(card, plan)
        self.memory_store.record_decision(decision)
        outcome = contracts.make_outcome(
            decision_id=decision["decision_id"],
            plan_id=plan["plan_id"],
            plan_digest=plan["content_hash"],
            status="FAIL",
            evidence_digest="retry-evidence",
            linked_run_id="retry-run",
            task_card_digest=card["content_hash"],
            objective_id=plan["objective_id"],
        )
        self.memory_store.record_outcome(outcome)
        review = contracts.make_review_receipt(
            review_id="retry-review",
            outcome=outcome,
            decision=decision,
            task_card=card,
            plan=plan,
            reviewed_by="ROOT",
            evidence_refs=("review://retry-run",),
            protected_source_refs=("evidence://protected/parser",),
            raw_evidence=f"The parser repair failed; the trace contains {SECRET}.",
            failed_hypotheses=("the network caused the parser failure",),
        )
        return self.service.capture(
            task_card=card,
            plan=plan,
            decision=decision,
            outcome=outcome,
            review_receipt=review,
            scope=self.scope,
        )

    def _adapter(self, surface: _EverOSSurface) -> experience.EverOSAdapter:
        base_root = self.root / "everos"
        memory_root = experience.EverOSAdapter.memory_root_for_scope(
            base_root, self.scope
        )
        return experience.EverOSAdapter(
            scope=self.scope,
            base_root=base_root,
            surface=experience.EverOSPublicSurface.from_object(
                surface,
                memory_root=memory_root,
                resolve_memory_root=lambda: memory_root,
            ),
            privacy_policy=self.policy,
        )

    def _restart(self) -> None:
        self.memory_store.close()
        self.memory_store = store.MemoryStore(self.store_path)
        self.memory_store.initialize()
        self.service = experience.ReviewedExperienceService(
            self.memory_store, privacy_policy=self.policy
        )

    @staticmethod
    def _case(adapter: experience.EverOSAdapter, session_id: str) -> dict:
        return {
            "id": "retry-case-1",
            "agent_id": adapter.everos_owner_id,
            "app_id": adapter.everos_application_id,
            "project_id": adapter.everos_project_id,
            "session_id": session_id,
            "task_intent": "repair parser failure",
            "approach": "inspect the failed parser check",
            "quality_score": 0.0,
            "key_insight": "the network hypothesis was disproved",
            "timestamp": "2026-09-21T00:00:00Z",
            "score": 1.0,
        }

    def test_intent_precedes_adapter_call_and_identity_survives_restart(self) -> None:
        trajectory = self._capture()
        surface = _EverOSSurface()
        surface.intent_reader = lambda: self.memory_store.get_experience_ingestion_for_trajectory(
            trajectory["trajectory_id"]
        )
        adapter = self._adapter(surface)

        first = asyncio.run(self.service.extract_trajectory(trajectory["trajectory_id"], adapter))

        self.assertEqual("pending", first["status"])
        self.assertEqual("pending", surface.intent_at_call["status"])
        self.assertEqual(0, surface.intent_at_call["version"])
        self.assertEqual(first["ingestion_id"], surface.intent_at_call["ingestion_id"])
        self.assertEqual(first["session_id"], surface.memorize_calls[0]["session_id"])
        self.assertEqual(first["payload_digest"], contracts.sha256_hex(surface.memorize_calls[0]))
        self.assertNotIn(SECRET, str(surface.memorize_calls[0]))

        self._restart()
        restored = self.service.get_trajectory(trajectory["trajectory_id"])
        recovery_surface = _EverOSSurface()
        recovery_adapter = self._adapter(recovery_surface)
        rebuilt_payload = recovery_adapter.add_payload(
            restored, session_id=first["session_id"]
        )
        replay = asyncio.run(
            self.service.extract_trajectory(trajectory["trajectory_id"], recovery_adapter)
        )
        self.assertEqual(first, replay)
        self.assertEqual(first["payload_digest"], contracts.sha256_hex(rebuilt_payload))
        self.assertEqual([], recovery_surface.memorize_calls)

    def test_lost_acknowledgement_is_reconciled_by_replay_readback(self) -> None:
        trajectory = self._capture()
        failed_surface = _EverOSSurface()
        failed_surface.lose_ack = True
        uncertain = asyncio.run(
            self.service.extract_trajectory(
                trajectory["trajectory_id"], self._adapter(failed_surface)
            )
        )
        self.assertEqual("uncertain", uncertain["status"])
        self.assertEqual(1, len(failed_surface.memorize_calls))
        self.assertNotIn(SECRET, uncertain["error"])

        self._restart()
        recovery_surface = _EverOSSurface()
        recovery_adapter = self._adapter(recovery_surface)
        recovery_surface.case_results = [self._case(recovery_adapter, "different-session")]
        unresolved = asyncio.run(
            self.service.extract_trajectory(trajectory["trajectory_id"], recovery_adapter)
        )
        self.assertEqual("uncertain", unresolved["status"])
        self.assertEqual(1, len(recovery_surface.search_calls))
        self.assertEqual([], recovery_surface.memorize_calls)

        recovery_surface.case_results = [
            self._case(recovery_adapter, uncertain["session_id"])
        ]
        confirmed = asyncio.run(
            self.service.extract_trajectory(trajectory["trajectory_id"], recovery_adapter)
        )
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual(uncertain["ingestion_id"], confirmed["ingestion_id"])
        self.assertEqual(uncertain["session_id"], confirmed["session_id"])
        self.assertEqual(uncertain["payload_digest"], confirmed["payload_digest"])
        self.assertEqual(["retry-case-1"], confirmed["case_ids"])
        self.assertEqual(2, len(recovery_surface.search_calls))
        self.assertEqual([], recovery_surface.memorize_calls)
        receipt = self.memory_store.get_case_receipt(self.scope.to_record(), "retry-case-1")
        self.assertEqual(trajectory["review_receipt_id"], receipt["review_receipt_id"])
        self.assertEqual([], self.service.search_recent_evidence(self.scope, "parser"))

    def test_concurrent_and_confirmed_replays_submit_once(self) -> None:
        trajectory = self._capture()
        source = _EverOSSurface()
        adapter = self._adapter(source)

        async def exercise() -> None:
            source.entered = asyncio.Event()
            source.release = asyncio.Event()
            first_task = asyncio.create_task(
                self.service.extract_trajectory(trajectory["trajectory_id"], adapter)
            )
            await source.entered.wait()
            concurrent = await self.service.extract_trajectory(
                trajectory["trajectory_id"], adapter
            )
            self.assertEqual("pending", concurrent["status"])
            self.assertEqual(1, len(source.memorize_calls))
            source.release.set()
            first = await first_task
            self.assertEqual(concurrent["ingestion_id"], first["ingestion_id"])

            source.case_results = [self._case(adapter, first["session_id"])]
            confirmed, replayed = await asyncio.gather(
                self.service.extract_trajectory(trajectory["trajectory_id"], adapter),
                self.service.extract_trajectory(trajectory["trajectory_id"], adapter),
            )
            self.assertEqual("confirmed", confirmed["status"])
            self.assertEqual(confirmed, replayed)
            self.assertEqual(1, len(source.memorize_calls))

        asyncio.run(exercise())

    def test_local_sanitized_reviewed_evidence_survives_everos_outage(self) -> None:
        trajectory = self._capture()
        unavailable = _EverOSSurface()
        unavailable.lose_ack = True
        unavailable.readback_unavailable = True
        uncertain = asyncio.run(
            self.service.extract_trajectory(
                trajectory["trajectory_id"], self._adapter(unavailable)
            )
        )
        self.assertEqual("uncertain", uncertain["status"])

        self._restart()
        recovery = _EverOSSurface()
        recovery.readback_unavailable = True
        replay = asyncio.run(
            self.service.extract_trajectory(trajectory["trajectory_id"], self._adapter(recovery))
        )
        self.assertEqual("uncertain", replay["status"])
        self.assertEqual([], recovery.memorize_calls)
        evidence = self.service.search_recent_evidence(self.scope, "parser")
        self.assertEqual(1, len(evidence))
        self.assertEqual(trajectory["trajectory_id"], evidence[0]["id"])
        self.assertEqual("historical_evidence", evidence[0]["kind"])
        self.assertEqual(trajectory["review_receipt_id"], evidence[0]["review_receipt_id"])
        self.assertIn("parser repair failed", evidence[0]["content"])
        self.assertNotIn(SECRET, str(evidence))


if __name__ == "__main__":
    unittest.main()

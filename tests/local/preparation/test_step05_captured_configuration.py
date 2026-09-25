"""STEP-05-1: one durable logical decision survives policy and process drift."""

from __future__ import annotations

import concurrent.futures
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from memory_harness import config, contracts, preparation, search, store


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class CapturedConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "memory.sqlite3"
        self.state = store.MemoryStore(self.path)
        self.state.initialize()
        self.clock = Clock()
        self.limits = config.resolve_limits({"default_deadline_seconds": 300.0})
        self.card = contracts.make_task_card(task="Fix parser", base_commit="base-1")
        self.plan = contracts.make_plan(
            plan_id="plan-1", objective_id="objective-1", route="ordinary",
            state="candidate", content={"steps": ["repair"]},
        )

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def prepare(self, service: preparation.PreparationService, **changes):
        arguments = {
            "task_card": self.card, "plan": self.plan, "objective_id": "objective-1",
            "deadline": 1300.0,
        }
        arguments.update(changes)
        return service.prepare(**arguments)

    def service(self, state=None, **raw):
        return preparation.PreparationService(
            store=state or self.state, config=config.resolve_config(raw),
            limits=self.limits, clock=self.clock,
        )

    def first(self):
        return self.prepare(
            self.service(strategy="problem_focused", template_memory=False,
                         atlas_shared_retrieval=False, experience_read=False),
            failure_context="parser failed", network_mode="restricted_local",
        )

    def test_restart_reuses_decision_configuration_network_and_spent_budget(self) -> None:
        first = self.first()
        self.state.close()
        self.clock.value += 45.0
        self.state = store.MemoryStore(self.path)
        self.state.initialize()
        optional_calls: list[object] = []
        restarted = self.service(strategy="deeper", template_memory=True,
                                 atlas_shared_retrieval=True, experience_read=True)
        second = self.prepare(
            restarted, deadline=None,
            stores=[search.SearchStore(
                store_id="templates", kind="template",
                query=lambda query: optional_calls.append(query) or [],
            )],
        )
        self.assertEqual(first.decision["decision_id"], second.decision["decision_id"])
        self.assertEqual(first.decision, second.decision)
        self.assertEqual(first.preparation["configuration"], second.preparation["configuration"])
        self.assertEqual("problem_focused", second.preparation["requested_strategy"])
        self.assertEqual("problem_focused", second.preparation["strategy"])
        self.assertEqual("restricted_local", second.preparation["network_mode"])
        self.assertEqual(1300.0, second.preparation["deadline_monotonic"])
        self.assertEqual(255.0, second.preparation["remaining_seconds"])
        self.assertEqual(45.0, second.preparation["spent_seconds"])
        self.assertEqual(2, second.preparation["attempt"])
        self.assertEqual([], optional_calls)

    def test_explicit_conflicting_policy_fails_without_writes_or_optional_calls(self) -> None:
        first = self.first()
        calls: list[object] = []
        template_store = search.SearchStore(
            store_id="templates", kind="template",
            query=lambda query: calls.append(query) or [],
        )
        for override in ({"request": {"template_memory": True}},
                         {"request": {"all_features": True}},
                         {"network_mode": "normal"}):
            with self.subTest(override=override):
                with self.assertRaisesRegex(preparation.MandatoryStateFailure,
                                            "recover|new decision"):
                    self.prepare(self.service(), stores=[template_store], **override)
                self.assertEqual([], calls)
                self.assertEqual([first.preparation], self.state.list_preparations(
                    first.decision["decision_id"]
                ))
                self.assertEqual(1, self.state.connection.execute(
                    "SELECT COUNT(*) FROM decisions"
                ).fetchone()[0])

    def test_exact_mandatory_identity_separates_decisions(self) -> None:
        first = self.first()
        changed = [
            {"task_card": contracts.make_task_card(task="Fix another parser", base_commit="base-1")},
            {"task_card": contracts.make_task_card(task="Fix parser", base_commit="base-2")},
            {"plan": contracts.make_plan(
                plan_id="plan-1", objective_id="objective-2", route="ordinary",
                state="candidate", content={"steps": ["repair"]},
            ), "objective_id": "objective-2"},
            {"plan": contracts.make_plan(
                plan_id="plan-1", objective_id="objective-1", route="deeper",
                state="candidate", content={"steps": ["repair"]},
            ), "route": "deeper"},
            {"plan": contracts.make_plan(
                plan_id="plan-2", objective_id="objective-1", route="ordinary",
                state="candidate", content={"steps": ["repair"]},
            )},
            {"plan": contracts.make_plan(
                plan_id="plan-1", objective_id="objective-1", route="ordinary",
                state="candidate", content={"steps": ["different"]},
            )},
            {"plan": contracts.make_plan(
                plan_id="plan-1", objective_id="objective-1", route="ordinary",
                state="accepted", accepted_by="ROOT", content={"steps": ["repair"]},
            )},
        ]
        for change in changed:
            with self.subTest(change=change):
                outcome = self.prepare(
                    self.service(strategy="problem_focused", template_memory=False,
                                 atlas_shared_retrieval=False, experience_read=False),
                    failure_context="parser failed", network_mode="restricted_local",
                    **change,
                )
                self.assertNotEqual(first.decision["decision_id"], outcome.decision["decision_id"])

    def test_unreadable_or_ambiguous_lookup_fails_closed(self) -> None:
        first = self.first()

        class FailedLookup:
            def __init__(self, inner):
                self.inner = inner

            def find_logical_decision(self, identity):
                raise OSError("read failed")

            def __getattr__(self, name):
                return getattr(self.inner, name)

        failing = self.service(state=FailedLookup(self.state))
        with self.assertRaises(preparation.MandatoryStateFailure):
            self.prepare(failing)
        second_decision = contracts.make_decision(
            self.card, self.plan, strategy="deeper",
            configuration={"strategy": "deeper"},
        )
        self.state.record_decision(second_decision)
        with self.assertRaisesRegex(preparation.MandatoryStateFailure, "ambiguous"):
            self.prepare(self.service())
        self.assertEqual([first.preparation], self.state.list_preparations(
            first.decision["decision_id"]
        ))

    def test_malformed_durable_decision_fails_closed(self) -> None:
        first = self.first()
        self.state.connection.execute(
            "UPDATE decisions SET configuration = ? WHERE decision_id = ?",
            ("{broken", first.decision["decision_id"]),
        )
        self.state.connection.commit()
        with self.assertRaises(preparation.MandatoryStateFailure):
            self.prepare(self.service())
        self.assertEqual(1, len(self.state.list_preparations(first.decision["decision_id"])))

    def test_corrupt_identity_column_cannot_mint_a_drifted_second_budget(self) -> None:
        first = self.first()
        self.state.connection.execute(
            "UPDATE decisions SET plan_digest = ? WHERE decision_id = ?",
            ("corrupt-digest", first.decision["decision_id"]),
        )
        self.state.connection.commit()
        with self.assertRaises(preparation.MandatoryStateFailure):
            self.prepare(self.service(strategy="deeper"))
        self.assertEqual(1, self.state.connection.execute(
            "SELECT COUNT(*) FROM decisions"
        ).fetchone()[0])
        self.assertEqual([first.preparation], self.state.list_preparations(
            first.decision["decision_id"]
        ))

    def test_unrelated_corrupt_decision_does_not_block_exact_owner(self) -> None:
        first = self.first()
        other_plan = contracts.make_plan(
            plan_id="another-plan", objective_id="another-objective",
            route="ordinary", state="candidate", content={"steps": ["other"]},
        )
        other = self.prepare(
            self.service(), plan=other_plan, objective_id="another-objective"
        )
        self.state.connection.execute(
            "UPDATE decisions SET configuration = ? WHERE decision_id = ?",
            ("{broken", other.decision["decision_id"]),
        )
        self.state.connection.commit()
        self.clock.value += 10.0
        retry = self.prepare(self.service(strategy="deeper"))
        self.assertEqual(first.decision["decision_id"], retry.decision["decision_id"])
        self.assertEqual(2, retry.preparation["attempt"])

    def test_concurrent_first_writers_share_captured_policy_and_attempt_sequence(self) -> None:
        barrier = threading.Barrier(2)

        class RacingStore:
            def __init__(self, inner):
                self.inner = inner
                self.first_lookup = True

            def find_logical_decision(self, identity):
                result = self.inner.find_logical_decision(identity)
                if self.first_lookup:
                    self.first_lookup = False
                    barrier.wait(timeout=5)
                return result

            def __getattr__(self, name):
                return getattr(self.inner, name)

        def worker(template_memory: bool):
            own_store = store.MemoryStore(self.path)
            own_store.initialize()
            try:
                service = self.service(state=RacingStore(own_store),
                                       template_memory=template_memory)
                return self.prepare(service)
            finally:
                own_store.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(worker, False)
            future_b = pool.submit(worker, True)
            results = [future_a.result(timeout=10), future_b.result(timeout=10)]
        ids = {outcome.decision["decision_id"] for outcome in results}
        self.assertEqual(1, len(ids))
        self.assertEqual(1, self.state.connection.execute(
            "SELECT COUNT(*) FROM decisions"
        ).fetchone()[0])
        self.assertEqual({1, 2}, {item.preparation["attempt"] for item in results})
        self.assertEqual(1, len({item.preparation["configuration"]["template_memory"]
                                 for item in results}))
        self.assertEqual(2, len(self.state.list_preparations(results[0].decision["decision_id"])))

    def test_concurrent_conflicting_network_first_writer_fails_closed(self) -> None:
        barrier = threading.Barrier(2)

        class RacingStore:
            def __init__(self, inner):
                self.inner = inner
                self.wait_once = True

            def find_logical_decision(self, identity):
                result = self.inner.find_logical_decision(identity)
                if self.wait_once:
                    self.wait_once = False
                    barrier.wait(timeout=5)
                return result

            def __getattr__(self, name):
                return getattr(self.inner, name)

        def worker(network_mode: str):
            own_store = store.MemoryStore(self.path)
            own_store.initialize()
            try:
                service = self.service(state=RacingStore(own_store))
                return self.prepare(service, network_mode=network_mode)
            finally:
                own_store.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, mode) for mode in
                       ("normal", "restricted_local")]
            results = []
            failures = []
            for future in futures:
                try:
                    results.append(future.result(timeout=10))
                except preparation.MandatoryStateFailure as exc:
                    failures.append(str(exc))
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(failures))
        self.assertIn("network policy", failures[0])
        self.assertEqual(1, self.state.connection.execute(
            "SELECT COUNT(*) FROM decisions"
        ).fetchone()[0])
        self.assertEqual([results[0].preparation], self.state.list_preparations(
            results[0].decision["decision_id"]
        ))


if __name__ == "__main__":
    unittest.main()

"""BEHAVIOR-01: bounded preparation selects only eligible optional memory."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from memory_harness import (
    config,
    contracts,
    preparation,
    privacy,
    search,
    store,
    templates,
)


class FakeClock:
    """One injected monotonic clock shared by the service and its adapters."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


def representation(limits: config.PreparationLimits, tokens: list[str]) -> dict:
    return templates.representation_identity(limits=limits) | {
        "tokens": tokens,
        "route": "ordinary",
    }


# Only ROOT may replace or extend a current plan, so template selection is
# exercised behind one explicit ROOT replan request.
ROOT_REPLAN = {
    "requested_by": "ROOT",
    "reason": "the tests exercise the explicit ROOT replan path",
}


class BoundedPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()
        self.limits = config.resolve_limits(
            {
                "default_deadline_seconds": 300.0,
                "execution_reserve_seconds": 60.0,
                "store_seconds": 8.0,
                "minimum_optional_slice_seconds": 1.0,
            }
        )
        self.memory_store = store.MemoryStore(self.root / "memory-state.sqlite3")
        self.memory_store.initialize()
        self.service = preparation.PreparationService(
            store=self.memory_store, limits=self.limits, clock=self.clock
        )
        self.card = contracts.make_task_card(
            task="Fix the regression failure in the parser test",
            base_commit="base-1",
        )
        self.plan = contracts.make_plan(
            plan_id="candidate-plan",
            objective_id="objective-1",
            route="ordinary",
            state="candidate",
            content={"steps": ["draft"]},
        )

    def tearDown(self) -> None:
        self.memory_store.close()
        self.temporary.cleanup()

    def _store(self, store_id: str, kind: str, items, *, scope=None, freshness="live"):
        return search.SearchStore(
            store_id=store_id,
            kind=kind,
            query=lambda query, items=items: items,
            scope=scope,
            freshness=freshness,
        )

    def _evidence(self, logical_id="case-1", revision="r1", **overrides):
        item = {
            "kind": "historical_evidence",
            "logical_id": logical_id,
            "revision_id": revision,
            "payload": {"summary": "prior regression evidence"},
            "scope": {"app": "demo", "project": "p", "namespace": "reviewed", "owner": "owner-1"},
            "origin": "everos",
            "representation": representation(
                self.limits, ["regression", "failure", "parser"]
            ),
            "score": 0.5,
            "freshness": "live",
        }
        item.update(overrides)
        return item

    def _prepare(self, stores=(), **overrides):
        arguments = {
            "task_card": self.card,
            "plan": self.plan,
            "objective_id": "objective-1",
            "route": "ordinary",
            "stores": list(stores),
        }
        arguments.update(overrides)
        return self.service.prepare(**arguments)

    def test_all_off_performs_no_memory_initialization_or_optional_call(self) -> None:
        calls: list[object] = []
        calls.append(None)

        class ExplodingQuery:
            def __call__(self, query):
                raise AssertionError("the all-off path must not query any store")

        service = preparation.PreparationService(
            store=self.memory_store,
            config=config.all_off(),
            limits=self.limits,
            clock=self.clock,
        )
        outcome = service.prepare(
            task_card=self.card,
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            stores=[
                search.SearchStore(
                    store_id="everos", kind="historical_evidence", query=ExplodingQuery()
                )
            ],
        )
        self.assertEqual("inherited", outcome.mode)
        self.assertIsNone(outcome.decision)
        self.assertIsNone(outcome.preparation)
        self.assertIsNone(outcome.envelope)
        self.assertEqual("candidate", outcome.plan["state"])

    def test_deadline_and_reserve_are_resolved_before_optional_calls(self) -> None:
        outcome = self._prepare(stores=[self._store("everos", "historical_evidence", [])])
        preparation_record = outcome.preparation
        self.assertEqual("trusted_deadline", preparation_record["budget_source"])
        self.assertEqual(300.0, preparation_record["remaining_seconds"])
        self.assertEqual(60.0, preparation_record["execution_reserve_seconds"])
        # The stage allowance never consumes the positive execution reserve.
        self.assertEqual(
            min(self.limits.standard_stage_seconds, 300.0 - 60.0),
            preparation_record["stage_allowance_seconds"],
        )

    def test_unknown_time_permits_only_the_cheap_fixed_pass(self) -> None:
        outcome = self._prepare(
            stores=[self._store("everos", "historical_evidence", [self._evidence()])],
            unknown_time=True,
        )
        self.assertEqual("unknown_time", outcome.preparation["budget_source"])
        self.assertIsNone(outcome.preparation["remaining_seconds"])
        self.assertEqual(1, outcome.trace["rounds"])

    def test_slow_store_is_isolated_and_other_stores_still_deliver(self) -> None:
        def slow(query):
            self.clock.advance(500.0)
            return [self._evidence(logical_id="case-slow")]

        outcome = self.service.prepare(
            task_card=self.card,
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            stores=[
                search.SearchStore(store_id="slow", kind="historical_evidence", query=slow),
                self._store("everos", "historical_evidence", [self._evidence()]),
            ],
        )
        attempts = {entry["store_id"]: entry for entry in outcome.trace["attempts"]}
        self.assertEqual("timed-out", attempts["slow"]["status"])
        self.assertEqual("completed", attempts["everos"]["status"])
        selected = [item for item in outcome.trace["candidates"] if item["disposition"] == "selected"]
        self.assertEqual(1, len(selected))
        self.assertEqual("case-1", selected[0]["logical_id"])

    def test_skill_and_template_capacity_are_independent(self) -> None:
        evidence = [
            self._evidence(logical_id=f"case-{index}", revision=f"r{index}")
            for index in range(3)
        ]
        template_items = [
            {
                "kind": "template",
                "logical_id": "template-1",
                "revision_id": "1",
                "payload": {"family": "Regression repair"},
                "scope": {"app": "demo"},
                "origin": "local-table",
                "representation": representation(self.limits, ["regression"]),
                "score": 0.4,
            }
        ]
        limits = config.resolve_limits({"candidate_capacity": {"historical_evidence": 1, "template": 1}})
        service = preparation.PreparationService(
            store=self.memory_store, limits=limits, clock=self.clock
        )
        outcome = service.prepare(
            task_card=self.card,
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            # Template selection only runs behind an explicit ROOT replan
            # request, so this capacity test models one.
            root_replan=ROOT_REPLAN,
            stores=[
                self._store("everos", "historical_evidence", evidence),
                self._store("templates", "template", template_items),
            ],
        )
        selected = [item for item in outcome.trace["candidates"] if item["disposition"] == "selected"]
        kinds = sorted(item["kind"] for item in selected)
        self.assertEqual(["historical_evidence", "template"], kinds)
        rejected = [item for item in outcome.trace["candidates"] if item["disposition"] == "rejected"]
        self.assertTrue(any("capacity" in " ".join(item["reasons"]) for item in rejected))

    def test_one_logical_revision_is_deduplicated_without_a_ranking_bonus(self) -> None:
        first = self._evidence(score=0.4)
        second = self._evidence(score=0.4, origin="local-cache")
        outcome = self._prepare(
            stores=[
                self._store("everos", "historical_evidence", [first]),
                self._store("local", "historical_evidence", [second]),
            ]
        )
        matches = [
            item for item in outcome.trace["candidates"] if item["logical_id"] == "case-1"
        ]
        self.assertEqual(1, len(matches))
        self.assertEqual(0.4, matches[0]["score"])
        self.assertEqual(2, len(matches[0]["provenance"]))

    def test_ineligible_candidate_does_not_discard_an_eligible_one(self) -> None:
        revoked = self._evidence(logical_id="case-revoked", revoked=True)
        invalid = self._evidence(logical_id="case-invalid", payload_digest="not-the-payload-digest")
        good = self._evidence(logical_id="case-good")
        outcome = self._prepare(
            stores=[self._store("everos", "historical_evidence", [revoked, invalid, good])]
        )
        selected = [item["logical_id"] for item in outcome.trace["candidates"] if item["disposition"] == "selected"]
        self.assertEqual(["case-good"], selected)

    def test_unauthorized_recipient_is_rejected(self) -> None:
        unauthorized = self._evidence(scope={"app": "other"})
        outcome = self._prepare(
            stores=[
                self._store(
                    "everos",
                    "historical_evidence",
                    [unauthorized],
                    scope={"app": "demo", "project": "p", "namespace": "reviewed", "owner": "owner-1"},
                )
            ]
        )
        self.assertEqual("no_optional_memory", outcome.trace["outcome"])

    def test_incomparable_representation_is_rejected(self) -> None:
        incomparable = self._evidence(
            representation={"model": "other/v2", "dimensions": 1, "metric": "cosine", "sanitizer_version": "v9", "tokens": ["regression"]}
        )
        outcome = self._prepare(
            stores=[self._store("everos", "historical_evidence", [incomparable])]
        )
        self.assertEqual("no_optional_memory", outcome.trace["outcome"])
        rejected = [
            item for item in outcome.trace["candidates"] if item["disposition"] == "rejected"
        ]
        self.assertTrue(any("incomparable" in " ".join(item["reasons"]) for item in rejected))

    def test_accepted_same_objective_plan_bypasses_template_scoring(self) -> None:
        accepted = contracts.make_plan(
            plan_id="accepted-plan",
            objective_id="objective-1",
            route="ordinary",
            state="accepted",
            accepted_by="ROOT",
            content={"steps": ["execute"]},
        )
        scored: list[object] = []

        class RecordingQuery:
            def __call__(self, query):
                scored.append(query)
                return []

        outcome = self.service.prepare(
            task_card=self.card,
            plan=accepted,
            objective_id="objective-1",
            route="ordinary",
            stores=[
                search.SearchStore(
                    store_id="templates", kind="template", query=RecordingQuery()
                )
            ],
        )
        self.assertEqual([], scored)
        self.assertEqual("preserved_accepted", outcome.disposition["branch"])
        attempts = {entry["store_id"]: entry for entry in outcome.trace["attempts"]}
        self.assertEqual("disabled", attempts["templates"]["status"])

    def test_nonempty_plan_for_another_objective_is_a_mandatory_failure(self) -> None:
        other = contracts.make_plan(
            plan_id="other-plan",
            objective_id="objective-other",
            route="ordinary",
            state="accepted",
            accepted_by="ROOT",
            content={"steps": ["other"]},
        )
        with self.assertRaisesRegex(preparation.MandatoryStateFailure, "another objective"):
            self.service.prepare(
                task_card=self.card,
                plan=other,
                objective_id="objective-1",
                route="ordinary",
            )

    def test_late_level_zero_supersedes_once_and_never_replenishes_time(self) -> None:
        first = self._prepare(stores=[self._store("everos", "historical_evidence", [])])
        self.clock.advance(120.0)
        revised = self.service.apply_level_zero(
            preparation=first.preparation,
            decision=first.decision,
            task_card=self.card,
            plan=self.plan,
            objective_id="objective-1",
        )
        self.assertEqual("superseded", revised.superseded["status"])
        self.assertEqual(first.preparation["preparation_id"], revised.preparation["supersedes"])
        self.assertLess(revised.preparation["remaining_seconds"], 300.0)
        self.assertFalse(revised.dispatchable)
        again = self.service.apply_level_zero(
            preparation=revised.preparation,
            decision=revised.decision,
            task_card=self.card,
            plan=revised.plan,
            objective_id="objective-1",
        )
        self.assertEqual("no_memory_continuation", again.mode)

    def test_preparation_and_trace_are_durable(self) -> None:
        outcome = self._prepare(
            stores=[self._store("everos", "historical_evidence", [self._evidence()])]
        )
        durable = self.memory_store.get_preparation(outcome.preparation["preparation_id"])
        self.assertEqual("objective-1", durable["objective_id"])
        trace = self.memory_store.get_search_trace(outcome.preparation["preparation_id"])
        self.assertEqual("optional_memory", trace["outcome"])
        candidates = self.memory_store.list_search_candidates(
            outcome.preparation["preparation_id"]
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual("selected", candidates[0]["disposition"])

    def test_problem_focused_without_real_failure_context_falls_back_to_standard(
        self,
    ) -> None:
        """A problem-focused request needs real failure context; otherwise Standard gates apply."""

        without = self._prepare(
            stores=[self._store("everos", "historical_evidence", [self._evidence()])],
            request={"strategy": "problem_focused"},
        )
        self.assertEqual("problem_focused", without.preparation["requested_strategy"])
        self.assertEqual("standard", without.preparation["strategy"])
        self.assertEqual("standard", without.decision["strategy"])
        self.assertEqual("standard", without.trace["strategy"])
        self.assertEqual(1, without.trace["rounds"])
        self.assertEqual(
            min(self.limits.standard_stage_seconds, 300.0 - 60.0),
            without.preparation["stage_allowance_seconds"],
        )

        with_context = self._prepare(
            stores=[self._store("everos", "historical_evidence", [self._evidence()])],
            request={"strategy": "problem_focused"},
            failure_context="the parser raises IndexError on an empty token stream",
        )
        self.assertEqual("problem_focused", with_context.preparation["strategy"])
        self.assertEqual(
            min(self.limits.problem_focused_stage_seconds, 300.0 - 60.0),
            with_context.preparation["stage_allowance_seconds"],
        )

    def test_unknown_time_masks_deeper_to_the_cheap_fixed_pass(self) -> None:
        """Unknown time admits only the configured cheap fixed pass, never Deeper."""

        limits = config.resolve_limits(
            {
                "default_deadline_seconds": 300.0,
                "execution_reserve_seconds": 60.0,
                "unknown_time_budget_seconds": 120.0,
            }
        )
        service = preparation.PreparationService(
            store=self.memory_store, limits=limits, clock=self.clock
        )
        outcome = service.prepare(
            task_card=self.card,
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            request={"strategy": "deeper"},
            unknown_time=True,
            stores=[self._store("everos", "historical_evidence", [self._evidence()])],
        )
        self.assertEqual("deeper", outcome.preparation["requested_strategy"])
        self.assertEqual("standard", outcome.preparation["strategy"])
        self.assertEqual("standard", outcome.trace["strategy"])
        self.assertEqual(1, outcome.trace["rounds"])
        self.assertLess(limits.deeper_stage_seconds, limits.unknown_time_budget_seconds)
        self.assertEqual(
            min(limits.standard_stage_seconds, limits.unknown_time_budget_seconds),
            outcome.preparation["stage_allowance_seconds"],
        )

    def test_deeper_restricted_local_stays_a_healthy_local_path(self) -> None:
        """A local-only Deeper recipe remains valid without any Atlas participation."""

        outcome = self._prepare(
            stores=[self._store("everos", "historical_evidence", [self._evidence()])],
            request={"strategy": "deeper", "atlas_shared_retrieval": False},
        )
        self.assertEqual("deeper", outcome.preparation["strategy"])
        self.assertEqual(self.limits.deeper_rounds, outcome.trace["rounds"])
        self.assertEqual("optional_memory", outcome.trace["outcome"])

    def test_requested_recipe_that_does_not_fit_demotes_once_to_standard(self) -> None:
        """Known time admits a recipe only when its full bound and reserve fit."""

        limits = config.resolve_limits(
            {
                "default_deadline_seconds": 300.0,
                "execution_reserve_seconds": 60.0,
                "standard_stage_seconds": 20.0,
                "problem_focused_stage_seconds": 40.0,
                "deeper_stage_seconds": 60.0,
            }
        )
        service = preparation.PreparationService(
            store=self.memory_store, limits=limits, clock=self.clock
        )
        stores = [self._store("everos", "historical_evidence", [self._evidence()])]

        admitted = service.prepare(
            task_card=self.card,
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            deadline=self.clock() + 300.0,
            request={"strategy": "deeper"},
            stores=stores,
        )
        # An admitted recipe keeps its full configured bound; it is never
        # truncated to whatever time happens to remain.
        self.assertEqual("deeper", admitted.preparation["strategy"])
        self.assertEqual(
            limits.deeper_stage_seconds, admitted.preparation["stage_allowance_seconds"]
        )

        demoted = service.prepare(
            task_card=self.card,
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            # Deeper's 60s bound plus the 60s reserve does not fit; Standard's
            # 20s bound plus the reserve does.
            deadline=self.clock() + 100.0,
            request={"strategy": "deeper"},
            stores=stores,
        )
        self.assertEqual("deeper", demoted.preparation["requested_strategy"])
        self.assertEqual("standard", demoted.preparation["strategy"])
        self.assertEqual("standard", demoted.decision["strategy"])
        self.assertEqual("standard", demoted.trace["strategy"])
        self.assertEqual(
            limits.standard_stage_seconds, demoted.preparation["stage_allowance_seconds"]
        )
        self.assertEqual("optional_memory", demoted.trace["outcome"])
        self.assertEqual(1, demoted.trace["rounds"])

    def test_no_recipe_fits_makes_no_optional_call(self) -> None:
        """Nothing fits the trusted remaining time: no call, honest budget trace."""

        limits = config.resolve_limits(
            {
                "default_deadline_seconds": 300.0,
                "execution_reserve_seconds": 60.0,
                "standard_stage_seconds": 20.0,
                "deeper_stage_seconds": 60.0,
            }
        )
        queried: list[object] = []

        def recording(query):
            queried.append(query)
            return []

        service = preparation.PreparationService(
            store=self.memory_store, limits=limits, clock=self.clock
        )
        outcome = service.prepare(
            task_card=self.card,
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            # Neither Deeper (120s) nor Standard (80s) fits the remaining time.
            deadline=self.clock() + 79.0,
            request={"strategy": "deeper"},
            stores=[
                search.SearchStore(
                    store_id="everos", kind="historical_evidence", query=recording
                )
            ],
        )
        self.assertEqual("deeper", outcome.preparation["requested_strategy"])
        # Standard stays the final fallback recipe; there is no "none" strategy.
        self.assertEqual("standard", outcome.preparation["strategy"])
        self.assertEqual(0.0, outcome.preparation["stage_allowance_seconds"])
        self.assertEqual([], queried)
        self.assertEqual("no_optional_memory", outcome.trace["outcome"])
        self.assertEqual(0, outcome.trace["rounds"])
        attempts = {entry["store_id"]: entry for entry in outcome.trace["attempts"]}
        self.assertEqual("unattempted-by-budget", attempts["everos"]["status"])
        self.assertTrue(attempts["everos"]["reason"])

    def test_known_secret_is_absent_from_the_store_query(self) -> None:
        policy = privacy.PrivacyPolicy(known_secrets=("super-secret-value",))
        captured: list[object] = []

        def recording(query):
            captured.append(query)
            return []

        service = preparation.PreparationService(
            store=self.memory_store,
            limits=self.limits,
            privacy_policy=policy,
            clock=self.clock,
        )
        outcome = service.prepare(
            task_card=contracts.make_task_card(
                task="Fix the regression that prints super-secret-value",
                base_commit="base-1",
            ),
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            stores=[
                search.SearchStore(
                    store_id="everos", kind="historical_evidence", query=recording
                )
            ],
        )
        self.assertTrue(captured)
        self.assertNotIn("super-secret-value", contracts.canonical_json(captured).decode("utf-8"))
        self.assertEqual("no_optional_memory", outcome.trace["outcome"])


if __name__ == "__main__":
    unittest.main()

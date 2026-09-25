"""STEP-08-2: exact native evidence, before the lane-1 outcome join exists."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memory_harness import contracts
from orchestrator_harness import memory_handoff, monitor, operator_launch, review, terminal_evidence
from orchestrator_harness.core import content_hash
from orchestrator_harness.epochs import lane_record_dir
from orchestrator_harness.manager_queue import ManagerQueueError
from orchestrator_harness.records import atomic_write_json


class NativeReviewEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rt = self.root / "runtime"
        self.worktree = self.root / "worktree"
        (self.worktree / ".agent-workspace").mkdir(parents=True)
        self.epoch_id = "epoch-1"
        self.lane_id = "lane-1"
        self.run_id = "run-1"
        self.folder = lane_record_dir(self.rt, self.epoch_id, self.lane_id)
        self.folder.mkdir(parents=True)
        self.plan = contracts.make_plan(
            plan_id="plan-1", objective_id="objective-1", route="ordinary",
            state="accepted", content={"steps": ["implement"]}, accepted_by="ROOT",
        )
        self.card = contracts.make_task_card(
            task="Complete the parent task", base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1", route="ordinary", plan=self.plan,
            ),
        )
        atomic_write_json(self.worktree / ".agent-workspace" / "task-card.json", self.card)
        self.lane = {
            "schema": "lane/v1", "lane_id": self.lane_id, "run_id": self.run_id,
            "worktree_path": str(self.worktree), "lifecycle": "review_pending",
            "memory_plan_state": "execution_accepted",
            "controller_status_path": str(self.worktree / ".agent-workspace" / "controller.status.json"),
            "process": {"pid": 123, "creation_time": "incarnation-1"},
        }
        self.result_path = self.worktree / "RESULT.json"
        self._result("PASS")
        self.envelope = memory_handoff.prepare_bootstrap_envelope(
            task_card=self.card, lane_id=self.lane_id, run_id=self.run_id,
            worktree_path=self.worktree, base_commit="base-1",
        )
        assert self.envelope is not None
        context = memory_handoff.load_final_context(
            worktree_path=self.worktree, envelope=self.envelope,
        )
        self.observed = memory_handoff.native_observation(
            envelope=self.envelope, context=context,
            controller_identity=self.lane["process"],
        )
        memory_handoff.record_dispatch_intent(
            worktree_path=self.worktree, envelope=self.envelope,
        )
        self.operation = memory_handoff.record_observed_invocation(
            worktree_path=self.worktree, envelope=self.envelope,
            observed_invocation=self.observed,
        )

    def _result(self, outcome: str) -> None:
        value = {
            "schema": "result/v1", "lane_id": self.lane_id, "run_id": self.run_id,
            "outcome": outcome, "summary": "native work completed", "evidence": [],
            "completed_at": "2026-09-25T00:00:00Z",
        }
        value["content_hash"] = content_hash(value)
        atomic_write_json(self.result_path, value)

    def _review(self, *, outcome: str = "PASS", approval: str = "ACCEPTED",
                force_reason: str | None = None, managed: bool = False,
                close_side_effect: Exception | None = None,
                event_state: str = "ACKNOWLEDGED") -> dict:
        event = {"event_id": "event-1", "state": event_state}
        with (
            patch.object(review, "find_harness_root", return_value=self.root),
            patch.object(review, "load_config", return_value=SimpleNamespace(runtime_root=self.rt)),
            patch.object(review, "find_active_lane", return_value=(self.epoch_id, self.lane)),
            patch.object(review, "_resolve_lane_managed", return_value=(self.epoch_id, self.lane, event)),
            patch.object(review, "_worktree_commit", return_value="commit-1"),
            patch.object(review, "close_event", side_effect=close_side_effect),
        ):
            return review.run_completion_review(
                event_id="event-1" if managed else None,
                lane_id=None if managed else self.lane_id,
                review_outcome=outcome, approval=approval,
                review_summary="ROOT reviewed the native result", evidence=["review-note"],
                force_accept=force_reason is not None, force_reason=force_reason,
            )

    def _evidence(self) -> dict:
        value = terminal_evidence.read_terminal_evidence(
            self.rt, self.epoch_id, self.lane_id, run_id=self.run_id,
        )
        assert value is not None
        return value

    def test_pass_binds_exact_parent_dispatch_result_review_and_acceptance(self) -> None:
        response = self._review()
        self.assertTrue(response["ok"], response)
        evidence = self._evidence()
        self.assertEqual("native-terminal-evidence/v1", evidence["schema"])
        self.assertEqual(self.card, evidence["task_card"])
        self.assertEqual(self.plan, evidence["accepted_plan"])
        self.assertEqual(self.envelope["decision_id"], evidence["decision_id"])
        self.assertEqual(self.operation, evidence["dispatch"]["operation"])
        self.assertEqual(self.observed, evidence["dispatch"]["observed_invocation"])
        self.assertEqual(self.envelope["content_hash"], evidence["dispatch"]["envelope_digest"])
        self.assertEqual(self.envelope["configuration_digest"], evidence["configuration_digest"])
        self.assertEqual("PASS", evidence["result"]["outcome"])
        self.assertEqual("PASS", evidence["review"]["review_outcome"])
        self.assertEqual("ACCEPTED", evidence["acceptance"]["approval"])
        self.assertIsNone(evidence["terminal_proof"])
        self.assertIn(str(self.folder / "NATIVE_TERMINAL_EVIDENCE.json"), response["evidence_paths"])

    def test_fail_blocked_and_forced_acceptance_remain_distinct(self) -> None:
        for result_outcome, finding, approval, reason in (
            ("FAIL", "FAIL", "REJECTED", None),
            ("BLOCKED", "BLOCKED", "ACCEPTED", "ROOT accepts blocked work"),
        ):
            with self.subTest(finding=finding):
                self._result(result_outcome)
                response = self._review(outcome=finding, approval=approval, force_reason=reason)
                self.assertTrue(response["ok"], response)
                evidence = self._evidence()
                self.assertEqual(result_outcome, evidence["result"]["outcome"])
                self.assertEqual(finding, evidence["review"]["review_outcome"])
                self.assertEqual(approval, evidence["acceptance"]["approval"])
                self.assertEqual(reason, evidence["acceptance"].get("force_accept_reason"))
                (self.folder / "NATIVE_TERMINAL_EVIDENCE.json").unlink()
                (self.folder / "ORCHESTRATOR_ACCEPTANCE.json").unlink()
                (self.folder / "COMPLETION_REVIEW.json").unlink()

    def test_no_delivered_observation_or_wrong_run_is_refused_without_pair(self) -> None:
        for change in ("pending", "wrong-run", "wrong-invocation"):
            with self.subTest(change=change):
                operation = copy.deepcopy(self.operation)
                if change == "pending":
                    operation["status"] = "pending"
                    operation["observed_invocation"] = None
                elif change == "wrong-run":
                    operation["run_id"] = "other-run"
                else:
                    operation["observed_invocation"]["creation_time"] = "other-incarnation"
                operation["content_hash"] = content_hash(operation)
                with patch.object(memory_handoff, "get_dispatch_operation", return_value=operation):
                    response = self._review()
                self.assertFalse(response["ok"], response)
                self.assertFalse((self.folder / "COMPLETION_REVIEW.json").exists())

    def test_exact_replay_and_partial_publication_reuse_original_identity(self) -> None:
        real_write = review.atomic_write_json

        def fail_after_review(path: Path, value: dict) -> None:
            real_write(path, value)
            if path.name == "COMPLETION_REVIEW.json":
                raise OSError("crash after review write")

        with patch.object(review, "atomic_write_json", side_effect=fail_after_review):
            first = self._review()
        self.assertFalse(first["ok"])
        before = (self.folder / "COMPLETION_REVIEW.json").read_bytes()
        self.assertFalse((self.folder / "ORCHESTRATOR_ACCEPTANCE.json").exists())
        self.assertFalse(monitor._recover_broken_review_pair(self.rt, self.epoch_id, self.lane))
        self.assertEqual(before, (self.folder / "COMPLETION_REVIEW.json").read_bytes())
        self.assertTrue(self._review()["ok"])
        evidence = self._evidence()
        self.assertTrue(self._review()["ok"])
        self.assertEqual(before, (self.folder / "COMPLETION_REVIEW.json").read_bytes())
        self.assertEqual(evidence, self._evidence())

    def test_crash_after_evidence_before_acceptance_recovers_without_advancement(self) -> None:
        real_write = review.atomic_write_json

        def fail_after_evidence(path: Path, value: dict) -> None:
            real_write(path, value)
            if path.name == "NATIVE_TERMINAL_EVIDENCE.json":
                raise OSError("crash after terminal evidence write")

        with patch.object(review, "atomic_write_json", side_effect=fail_after_evidence):
            first = self._review()
        self.assertFalse(first["ok"])
        self.assertFalse((self.folder / "ORCHESTRATOR_ACCEPTANCE.json").exists())
        before = (self.folder / "NATIVE_TERMINAL_EVIDENCE.json").read_bytes()
        self.assertFalse(monitor._recover_broken_review_pair(self.rt, self.epoch_id, self.lane))
        self.assertTrue(self._review()["ok"])
        self.assertEqual(before, (self.folder / "NATIVE_TERMINAL_EVIDENCE.json").read_bytes())
        self.assertEqual("ACCEPTED", self._evidence()["acceptance"]["approval"])

    def test_post_publication_manager_close_failure_retries_same_record(self) -> None:
        first = self._review(managed=True, close_side_effect=ManagerQueueError("QUEUE_UNAVAILABLE", "queue unavailable"))
        self.assertFalse(first["ok"])
        before = self._evidence()
        self.lane["lifecycle"] = "accepted"
        second = self._review(managed=True)
        self.assertTrue(second["ok"], second)
        self.assertEqual(before, self._evidence())
        completed = self._review(managed=True, event_state="COMPLETE")
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(before, self._evidence())

    def test_conflicting_acceptance_and_evidence_stay_visible(self) -> None:
        self.assertTrue(self._review()["ok"])
        acceptance_path = self.folder / "ORCHESTRATOR_ACCEPTANCE.json"
        changed = copy.deepcopy(self._evidence()["acceptance"])
        changed["approval"] = "REJECTED"
        changed["content_hash"] = content_hash(changed)
        atomic_write_json(acceptance_path, changed)
        before = acceptance_path.read_bytes()
        response = self._review()
        self.assertFalse(response["ok"])
        self.assertEqual(before, acceptance_path.read_bytes())
        self.assertTrue((self.folder / "NATIVE_TERMINAL_EVIDENCE.json").exists())

    def test_conflicting_review_and_terminal_file_are_preserved(self) -> None:
        self.assertTrue(self._review()["ok"])
        review_path = self.folder / "COMPLETION_REVIEW.json"
        changed = copy.deepcopy(self._evidence()["review"])
        changed["review_summary"] = "contradictory finding"
        changed["content_hash"] = content_hash(changed)
        atomic_write_json(review_path, changed)
        before = review_path.read_bytes()
        self.assertFalse(self._review()["ok"])
        self.assertEqual(before, review_path.read_bytes())
        terminal_path = self.folder / "NATIVE_TERMINAL_EVIDENCE.json"
        original = terminal_path.read_bytes()
        terminal_path.write_text('{"schema":"native-terminal-evidence/v1","wrong":true}', encoding="utf-8")
        corrupt = terminal_path.read_bytes()
        self.assertFalse(self._review()["ok"])
        self.assertEqual(corrupt, terminal_path.read_bytes())
        self.assertNotEqual(original, corrupt)

    def test_read_survives_worktree_retirement(self) -> None:
        self.assertTrue(self._review()["ok"])
        expected = self._evidence()
        import shutil

        shutil.rmtree(self.worktree)
        self.assertEqual(expected, self._evidence())

    def test_true_unknown_requires_same_run_cleanup_and_root_exception(self) -> None:
        self.result_path.unlink()
        status = {
            "schema": "controller-status/v1", "lane_id": self.lane_id, "run_id": self.run_id,
            "controller_state": "exited", "provider_state": {"state": "exited", "exit_code": 1},
            "result_state": "invalid", "recorded_status": "provider_exited_no_result",
            "cleanup_proven": True, "updated_at": "2026-09-25T00:00:00Z",
        }
        status_path = Path(self.lane["controller_status_path"])
        for change in ("missing", "wrong-run", "no-cleanup", "mere-exit", "no-reason"):
            with self.subTest(change=change):
                candidate = copy.deepcopy(status)
                if change == "wrong-run":
                    candidate["run_id"] = "other-run"
                elif change == "no-cleanup":
                    candidate["cleanup_proven"] = False
                elif change == "mere-exit":
                    candidate["recorded_status"] = None
                if change == "missing":
                    status_path.unlink(missing_ok=True)
                else:
                    atomic_write_json(status_path, candidate)
                response = self._review(outcome="UNKNOWN", force_reason=None if change == "no-reason" else "ROOT accepts terminal uncertainty")
                self.assertFalse(response["ok"], response)
                self.assertFalse((self.folder / "COMPLETION_REVIEW.json").exists())
        atomic_write_json(status_path, status)
        response = self._review(outcome="UNKNOWN", force_reason="ROOT accepts terminal uncertainty")
        self.assertTrue(response["ok"], response)
        evidence = self._evidence()
        self.assertIsNone(evidence["result"])
        self.assertEqual("UNKNOWN", evidence["review"]["review_outcome"])
        self.assertEqual(status, evidence["terminal_proof"])
        self.assertEqual("ROOT accepts terminal uncertainty", evidence["acceptance"]["force_accept_reason"])

    def test_managed_no_result_status_event_is_reviewable_only_for_exact_status(self) -> None:
        event = {
            "event_id": "event-1", "type": "LANE_STATUS_CHANGED",
            "actionable_status": "provider_exited_no_result",
            "state": "ACKNOWLEDGED", "lane_id": self.lane_id, "run_id": self.run_id,
        }
        with (
            patch.object(review, "read_manager_queue", return_value={"events": [event]}),
            patch.object(review, "find_active_lane", return_value=(self.epoch_id, self.lane)),
        ):
            self.assertEqual(event, review._resolve_lane_managed(self.rt, "event-1")[2])
            event["actionable_status"] = "controller_exited"
            with self.assertRaises(review.ReviewError):
                review._resolve_lane_managed(self.rt, "event-1")

    def test_cli_exposes_explicit_unknown_finding(self) -> None:
        args = operator_launch._build_parser().parse_args([
            "lane", "completion-review", "--lane-id", self.lane_id,
            "--review-outcome", "UNKNOWN", "--approval", "ACCEPTED",
            "--review-summary", "ROOT exceptional acceptance", "--force-accept",
            "--force-reason", "provider exited without a result after cleanup",
        ])
        self.assertEqual("UNKNOWN", args.review_outcome)

    def test_apc_child_legacy_and_all_off_take_ordinary_path_without_optional_store(self) -> None:
        child = contracts.make_task_card(task="Draft a plan for the parent", base_commit="base-1")
        all_off = contracts.make_task_card(
            task="Ordinary all-off work", base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1", route="ordinary", plan=self.plan,
                configuration={"all_features": False},
            ),
        )
        for card in (child, all_off):
            with self.subTest(task=card["task"]):
                atomic_write_json(self.worktree / ".agent-workspace" / "task-card.json", card)
                self.lane.pop("memory_plan_state", None)
                with patch.object(memory_handoff, "get_dispatch_operation", side_effect=AssertionError("optional store opened")):
                    response = self._review()
                self.assertTrue(response["ok"], response)
                self.assertIsNone(terminal_evidence.read_terminal_evidence(
                    self.rt, self.epoch_id, self.lane_id,
                ))
                (self.folder / "ORCHESTRATOR_ACCEPTANCE.json").unlink()
                (self.folder / "COMPLETION_REVIEW.json").unlink()

    def test_fixture_separates_quality_replay_conflict_and_effect_progress(self) -> None:
        self.assertTrue(self._review()["ok"])
        evidence = self._evidence()
        consumer = FaithfulConsumerFixture()
        first = consumer.consume(evidence)
        consumer.effects[evidence["decision_id"]] = "failed-retryable"
        self.assertEqual(first, consumer.consume(copy.deepcopy(evidence)))
        self.assertEqual("failed-retryable", consumer.effects[evidence["decision_id"]])
        conflict = copy.deepcopy(evidence)
        conflict["review"]["review_outcome"] = "FAIL"
        conflict["review"]["content_hash"] = content_hash(conflict["review"])
        conflict["acceptance"]["review_ref"] = conflict["review"]["content_hash"]
        conflict["acceptance"]["force_accept_reason"] = "ROOT accepts failed finding"
        conflict["acceptance"]["content_hash"] = content_hash(conflict["acceptance"])
        conflict["content_hash"] = content_hash(conflict)
        terminal_evidence.validate_terminal_evidence(conflict)
        with self.assertRaises(ValueError):
            consumer.consume(conflict)
        self.assertEqual(first, consumer.fixed[evidence["decision_id"]])


class FaithfulConsumerFixture:
    """Model only one immutable quality decision and separately retryable effects."""

    def __init__(self) -> None:
        self.fixed: dict[str, dict] = {}
        self.effects: dict[str, str] = {}

    def consume(self, evidence: dict) -> dict:
        terminal_evidence.validate_terminal_evidence(evidence)
        decision_id = evidence["decision_id"]
        prior = self.fixed.get(decision_id)
        if prior is not None:
            if prior["content_hash"] != evidence["content_hash"]:
                raise ValueError("conflicting terminal evidence")
            return prior
        self.fixed[decision_id] = copy.deepcopy(evidence)
        self.effects[decision_id] = "pending"
        return self.fixed[decision_id]


if __name__ == "__main__":
    unittest.main()

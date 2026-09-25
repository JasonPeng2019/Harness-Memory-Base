"""STEP-04 execution-boundary tests through the real harness entry points.

The contract/preparation half lives in ``tests/local``; this file proves the
product-harness execution boundary itself.  The real ``run_bootstrap``,
``run_resume``, and ``run_launch`` entry points must consume the enhanced
handoff's explicit ``absent``, ``candidate_review``, and
``execution_accepted`` states without dereferencing a missing plan, must keep
any enabled enhanced lane that has no ROOT-accepted plan out of dispatch, and
must bind the durable finalized context to the real task card, lane, run,
worktree, and base before recording any dispatch intent.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator_harness import bootstrap, launch, lanes, memory_handoff, resume, setup
from memory_harness import config, contracts, store


BINDING_SOURCE = (
    "PROVIDER_ID = 'codex'\n"
    "ADAPTER_VERSION = 'test-v1'\n"
    "def validate_launch_config(*, model, launch_config): return dict(launch_config)\n"
    "def build_argv(**kwargs): return ['codex']\n"
    "def parse_line(line): return None\n"
)


def accepted_plan(plan_id: str = "plan-1", *, objective: str = "objective-1",
                  content: dict | None = None) -> dict:
    return contracts.make_plan(
        plan_id=plan_id,
        objective_id=objective,
        route="ordinary",
        state="accepted",
        content=content or {"steps": ["inspect", "implement", "verify"]},
        accepted_by="ROOT",
    )


def candidate_plan(plan_id: str = "candidate-plan", *,
                   objective: str = "objective-1") -> dict:
    return contracts.make_plan(
        plan_id=plan_id,
        objective_id=objective,
        route="ordinary",
        state="candidate",
        content={"steps": ["draft"]},
    )


def accepted_card(*, task: str = "Fix the regression and verify it",
                  base: str = "test-base", objective: str = "objective-1",
                  configuration: dict | None = None) -> tuple[dict, dict]:
    plan = accepted_plan(f"{objective}-plan", objective=objective)
    handoff = contracts.make_memory_handoff(
        objective_id=objective,
        route="ordinary",
        plan=plan,
        configuration=configuration,
    )
    card = contracts.make_task_card(
        task=task,
        base_commit=base,
        branch=f"lane/{objective}",
        memory_handoff=handoff,
    )
    return card, plan


def candidate_card(*, task: str = "Draft a reviewable fix",
                   base: str = "test-base", objective: str = "objective-1",
                   configuration: dict | None = None) -> tuple[dict, dict]:
    plan = candidate_plan(f"{objective}-candidate", objective=objective)
    handoff = contracts.make_memory_handoff(
        objective_id=objective,
        route="ordinary",
        plan=plan,
        configuration=configuration,
    )
    card = contracts.make_task_card(
        task=task,
        base_commit=base,
        branch=f"lane/{objective}",
        memory_handoff=handoff,
    )
    return card, plan


def absent_card(*, task: str = "Begin fresh ROOT planning",
                base: str = "test-base",
                objective: str = "objective-1") -> tuple[dict, dict]:
    handoff = contracts.make_memory_handoff(
        objective_id=objective,
        route="ordinary",
        plan=None,
    )
    card = contracts.make_task_card(
        task=task,
        base_commit=base,
        branch=f"lane/{objective}",
        memory_handoff=handoff,
    )
    return card, handoff


def legacy_card(*, task: str = "Ordinary inherited task",
                base: str = "test-base") -> dict:
    return contracts.make_task_card(
        task=task,
        base_commit=base,
        branch="lane/legacy",
    )


class LaunchBoundaryFixture:
    """A minimal but real harness root that bootstrap/resume/launch accept."""

    EPOCH = "epoch-1"

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.harness = self.root / "harness"
        self.root_workspace = self.root / "root-workspace"
        self.harness.mkdir()
        self.root_workspace.mkdir()
        self.write_json(
            self.harness / "harness-config.json",
            {
                "root_workspace": str(self.root_workspace),
                "managed_coordination": "enabled",
            },
        )
        self.write_json(
            self.harness / "resource-manifest.json",
            {"schema": "resource-manifest/v1", "resources": []},
        )
        workspace = self.harness / "super-cache" / "workspace" / ".agent-workspace"
        for name, contents in {
            "README.md": "base workspace\n",
            "lane-queue.py": "# lane queue\n",
            "manager-notify.py": "# manager notify\n",
            "result-stop-check.py": "# result stop check\n",
        }.items():
            self.write_text(workspace / name, contents)
        self.write_text(
            self.harness / "adapters" / "codex" / "super-cache" / ".codex" / "worker.txt",
            "codex worker payload\n",
        )
        self.write_text(
            self.harness
            / "orchestrator_harness"
            / "provider_adapters"
            / "codex"
            / "launcher_binding.py",
            BINDING_SOURCE,
        )
        self.runtime = self.root_workspace / ".harness-runtime"
        for source, relative in setup._plan_active_cache(self.harness):
            target = self.runtime / "super-cache" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self.launch_config = {
            "reasoning_effort": "high",
            "service_tier": "priority",
        }

    def close(self) -> None:
        self.temporary.cleanup()

    def write_text(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def write_json(self, path: Path, value: object) -> None:
        self.write_text(path, json.dumps(value, indent=2) + "\n")

    # -- real entry points -------------------------------------------------

    def run_bootstrap(self, *, lane_id: str, card: dict) -> tuple[dict, Path]:
        task_card_path = self.root / f"{lane_id}-task-card.json"
        self.write_json(task_card_path, card)
        worktree = self.runtime / "worktrees" / self.EPOCH / lane_id

        def fake_git_add(
            root: Path, branch: str, target: Path, base_commit: str
        ) -> None:
            target.mkdir(parents=True, exist_ok=True)

        with (
            patch(
                "orchestrator_harness.config.find_harness_root",
                return_value=self.harness,
            ),
            patch.object(
                bootstrap, "open_epoch", return_value={"epoch_id": self.EPOCH}
            ),
            patch.object(bootstrap, "read_active_lanes", return_value=[]),
            patch.object(bootstrap, "_git_worktree_add", side_effect=fake_git_add),
            patch.object(bootstrap.subprocess, "run"),
        ):
            result = bootstrap.run_bootstrap(
                lane_id=lane_id,
                provider="codex",
                model="test-model",
                launch_config=dict(self.launch_config),
                exclusive_resources=[],
                task_card_path=str(task_card_path),
            )
        return result, worktree

    def run_resume(self, *, lane_id: str, card: dict) -> dict:
        task_card_path = self.root / f"{lane_id}-resume-card.json"
        self.write_json(task_card_path, card)
        with (
            patch.object(resume, "find_harness_root", return_value=self.harness),
            patch.object(
                resume, "find_active_lane", side_effect=self._read_active_lane
            ),
        ):
            return resume.run_resume(
                lane_id=lane_id, resume_task_card=str(task_card_path)
            )

    def run_launch(self, *, lane_id: str) -> tuple[dict, MagicMock]:
        child = MagicMock(pid=41)
        child.poll.return_value = 0
        terminal = {
            "schema": "controller-status/v1",
            "lane_id": lane_id,
            "controller_state": "exited",
            "provider_state": {"state": "exited", "exit_code": 0},
            "cleanup_proven": True,
            "recorded_status": "review_pending",
        }
        with (
            patch.object(launch, "find_harness_root", return_value=self.harness),
            patch.object(launch, "read_runtime_state", return_value={"state": "OPEN"}),
            patch.object(launch, "find_active_lane", side_effect=self._read_active_lane),
            patch.object(launch.processes, "spawn_detached", return_value=child) as spawn,
            patch.object(
                launch.processes,
                "process_identity",
                return_value={"pid": 41, "creation_time": "ct-1"},
            ),
            patch.object(launch, "_read_controller_status", return_value=terminal),
        ):
            result = launch.run_launch(lane_id)
        return result, spawn

    def _read_active_lane(self, rt: Path, lane_id: str) -> tuple[str, dict]:
        return self.EPOCH, lanes.read_lane(rt, self.EPOCH, lane_id)

    # -- durable state readers ---------------------------------------------

    def lane_record(self, lane_id: str) -> dict:
        return lanes.read_lane(self.runtime, self.EPOCH, lane_id)

    def write_lane_fields(self, lane_id: str, **fields: object) -> dict:
        return lanes.update_lane(
            self.runtime,
            self.EPOCH,
            lane_id,
            lambda current: {**current, **fields},
        )

    def make_resumable(self, lane_id: str, *, session_id: str = "session-1") -> None:
        self.write_lane_fields(
            lane_id,
            lifecycle="review_pending",
            session={"session_id": session_id},
        )

    def memory_paths(self, worktree: Path) -> tuple[Path, Path]:
        return memory_handoff.memory_paths(worktree)

    def open_store(self, worktree: Path):
        store_path, _ = self.memory_paths(worktree)
        memory_store = store.MemoryStore(store_path)
        memory_store.initialize()
        return memory_store


class Step04BootstrapBoundaryTests(unittest.TestCase):
    """run_bootstrap must honor each explicit enhanced plan state."""

    def setUp(self) -> None:
        self.fixture = LaunchBoundaryFixture()
        self.addCleanup(self.fixture.close)

    def test_absent_plan_keeps_fresh_disposition_and_creates_no_worker(self) -> None:
        card, handoff = absent_card()
        result, worktree = self.fixture.run_bootstrap(lane_id="absent-lane", card=card)
        self.assertFalse(result["ok"], result)
        self.assertEqual(bootstrap.BOOTSTRAP_PLAN_PENDING, result["code"])
        self.assertIn("no worker was created or launched", result["next_action"])
        self.assertIn("no current plan exists", result["summary"])
        self.assertIn("fresh ROOT-planning disposition", result["summary"])

        workspace = worktree / ".agent-workspace"
        self.assertTrue((workspace / "task-card.json").is_file())
        for artifact in ("worker-prompt.md", "result-template.json", "invocation.json"):
            self.assertFalse((workspace / artifact).exists(), artifact)

        lane = self.fixture.lane_record("absent-lane")
        self.assertEqual("prepared", lane["lifecycle"])
        self.assertEqual("absent", lane["memory_plan_state"])
        self.assertFalse(lane["dispatchable"])
        self.assertIn("fresh ROOT-planning disposition", lane["memory_pending_reason"])

        store_path, envelope_path = self.fixture.memory_paths(worktree)
        self.assertTrue(store_path.is_file(), "fresh disposition must be durable")
        self.assertFalse(envelope_path.exists())
        resolved = config.resolve_config(handoff.get("configuration"))
        decision = contracts.make_decision(
            card,
            contracts.make_plan(
                plan_id="fresh:objective-1",
                objective_id="objective-1",
                route="ordinary",
                state="fresh",
                content={"steps": []},
            ),
            strategy=resolved.strategy,
            configuration=asdict(resolved),
        )
        memory_store = self.fixture.open_store(worktree)
        try:
            preparations = memory_store.list_preparations(decision["decision_id"])
            self.assertEqual(1, len(preparations))
            self.assertEqual("absent", preparations[0]["current_plan_state"])
            dispositions = memory_store.list_plan_dispositions(decision["decision_id"])
            self.assertEqual("fresh", dispositions[-1]["branch"])
            self.assertIsNone(
                memory_store.get_final_context_for_decision(decision["decision_id"])
            )
        finally:
            memory_store.close()

    def test_candidate_review_retains_review_and_cannot_execute(self) -> None:
        card, candidate = candidate_card()
        result, worktree = self.fixture.run_bootstrap(
            lane_id="candidate-lane", card=card
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(bootstrap.BOOTSTRAP_PLAN_PENDING, result["code"])
        self.assertIn("ROOT review", result["summary"])

        workspace = worktree / ".agent-workspace"
        for artifact in ("worker-prompt.md", "result-template.json", "invocation.json"):
            self.assertFalse((workspace / artifact).exists(), artifact)

        lane = self.fixture.lane_record("candidate-lane")
        self.assertEqual("candidate_review", lane["memory_plan_state"])
        self.assertFalse(lane["dispatchable"])

        store_path, envelope_path = self.fixture.memory_paths(worktree)
        self.assertTrue(store_path.is_file())
        self.assertFalse(envelope_path.exists())
        resolved = config.resolve_config(None)
        decision = contracts.make_decision(
            card, candidate, strategy=resolved.strategy, configuration=asdict(resolved)
        )
        memory_store = self.fixture.open_store(worktree)
        try:
            preparations = memory_store.list_preparations(decision["decision_id"])
            self.assertEqual(1, len(preparations))
            self.assertEqual("candidate_review", preparations[0]["current_plan_state"])
            self.assertEqual(candidate["content_hash"], preparations[0]["plan_digest"])
            dispositions = memory_store.list_plan_dispositions(decision["decision_id"])
            self.assertEqual("candidate_review", dispositions[-1]["branch"])
            trace = memory_store.get_search_trace(preparations[0]["preparation_id"])
            # Candidate review must run no template work at all: the durable
            # trace records no template attempt, no template candidate, and
            # nothing delivered for a template search.
            self.assertEqual(
                [],
                [item for item in trace["attempts"] if item["kind"] == "template"],
            )
            self.assertEqual(
                [],
                [item for item in trace["candidates"] if item["kind"] == "template"],
            )
            self.assertEqual([], trace["delivered"])
            self.assertEqual("no_optional_memory", trace["outcome"])
        finally:
            memory_store.close()

    def test_accepted_plan_finalizes_exact_dispatch_envelope(self) -> None:
        card, plan = accepted_card()
        result, worktree = self.fixture.run_bootstrap(
            lane_id="accepted-lane", card=card
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual("BOOTSTRAP_OK", result["code"])

        lane = self.fixture.lane_record("accepted-lane")
        self.assertEqual("execution_accepted", lane["memory_plan_state"])
        self.assertTrue(lane["dispatchable"])
        workspace = worktree / ".agent-workspace"
        for artifact in ("worker-prompt.md", "result-template.json", "invocation.json"):
            self.assertTrue((workspace / artifact).is_file(), artifact)

        envelope = memory_handoff.load_envelope(worktree)
        self.assertIsNotNone(envelope)
        self.assertEqual(lane["run_id"], envelope["run_id"])
        self.assertEqual("accepted-lane", envelope["lane_id"])
        self.assertEqual(str(worktree), envelope["worktree_path"])
        self.assertEqual(card["base_commit"], envelope["base_commit"])
        self.assertEqual(plan["plan_id"], envelope["plan_id"])
        self.assertEqual(plan["content_hash"], envelope["plan_digest"])
        self.assertEqual(plan["objective_id"], envelope["objective_id"])
        self.assertEqual(card["content_hash"], envelope["task_card_digest"])
        self.assertEqual("finalized", envelope["dispatch_state"])
        memory_handoff.validate_envelope_for_launch(
            envelope=envelope,
            task_card=card,
            lane_id="accepted-lane",
            run_id=lane["run_id"],
            worktree_path=worktree,
            base_commit=card["base_commit"],
        )
        memory_store = self.fixture.open_store(worktree)
        try:
            context = memory_store.get_final_context_for_decision(envelope["decision_id"])
            self.assertIsNotNone(context)
            memory_handoff.validate_final_context_for_launch(
                context=context,
                envelope=envelope,
                task_card=card,
                lane_id="accepted-lane",
                run_id=lane["run_id"],
                worktree_path=worktree,
                base_commit=card["base_commit"],
            )
        finally:
            memory_store.close()

    def test_legacy_card_keeps_the_ordinary_path_without_memory(self) -> None:
        card = legacy_card()
        result, worktree = self.fixture.run_bootstrap(lane_id="legacy-lane", card=card)
        self.assertTrue(result["ok"], result)
        self.assertEqual("BOOTSTRAP_OK", result["code"])
        lane = self.fixture.lane_record("legacy-lane")
        self.assertNotIn("memory_plan_state", lane)
        self.assertNotIn("dispatchable", lane)
        store_path, envelope_path = self.fixture.memory_paths(worktree)
        self.assertFalse(store_path.exists())
        self.assertFalse(envelope_path.exists())
        prompt = (worktree / ".agent-workspace" / "worker-prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Authoritative task and accepted plan", prompt)

    def test_all_off_enhanced_card_keeps_the_ordinary_path_without_memory(self) -> None:
        card, _ = candidate_card(configuration={"all_features": False})
        result, worktree = self.fixture.run_bootstrap(lane_id="all-off-lane", card=card)
        self.assertTrue(result["ok"], result)
        self.assertEqual("BOOTSTRAP_OK", result["code"])
        lane = self.fixture.lane_record("all-off-lane")
        self.assertNotIn("memory_plan_state", lane)
        store_path, envelope_path = self.fixture.memory_paths(worktree)
        self.assertFalse(store_path.exists())
        self.assertFalse(envelope_path.exists())

    def test_absent_plan_lane_cannot_launch_a_worker(self) -> None:
        card, _ = absent_card()
        result, _worktree = self.fixture.run_bootstrap(
            lane_id="absent-launch-lane", card=card
        )
        self.assertEqual(bootstrap.BOOTSTRAP_PLAN_PENDING, result["code"])
        launched, spawn = self.fixture.run_launch(lane_id="absent-launch-lane")
        self.assertFalse(launched["ok"], launched)
        self.assertIn(
            launched["code"],
            {launch.LAUNCH_PLAN_PENDING, launch.LAUNCH_INVOCATION_INVALID},
        )
        spawn.assert_not_called()

    def test_candidate_review_lane_cannot_launch_a_worker(self) -> None:
        card, _ = candidate_card()
        result, _worktree = self.fixture.run_bootstrap(
            lane_id="candidate-launch-lane", card=card
        )
        self.assertEqual(bootstrap.BOOTSTRAP_PLAN_PENDING, result["code"])
        launched, spawn = self.fixture.run_launch(lane_id="candidate-launch-lane")
        self.assertFalse(launched["ok"], launched)
        self.assertIn(
            launched["code"],
            {launch.LAUNCH_PLAN_PENDING, launch.LAUNCH_INVOCATION_INVALID},
        )
        spawn.assert_not_called()


class Step04LaunchBoundaryTests(unittest.TestCase):
    """run_launch must bind the durable context before any dispatch intent."""

    def setUp(self) -> None:
        self.fixture = LaunchBoundaryFixture()
        self.addCleanup(self.fixture.close)
        self.card, self.plan = accepted_card()
        result, self.worktree = self.fixture.run_bootstrap(
            lane_id="launch-lane", card=self.card
        )
        self.assertTrue(result["ok"], result)
        self.envelope = memory_handoff.load_envelope(self.worktree)
        self.assertIsNotNone(self.envelope)

    def test_accepted_lane_records_dispatch_intent_and_launches(self) -> None:
        store_path, _ = self.fixture.memory_paths(self.worktree)
        decision_id = self.envelope["decision_id"]
        observed: dict[str, object] = {}

        def record_before_spawn(*args: object, **kwargs: object) -> MagicMock:
            memory_store = store.MemoryStore(store_path)
            memory_store.initialize()
            try:
                observed["operations"] = memory_store.list_operations(decision_id)
            finally:
                memory_store.close()
            observed["env"] = kwargs.get("env")
            return MagicMock(pid=41)

        with (
            patch.object(launch, "find_harness_root", return_value=self.fixture.harness),
            patch.object(launch, "read_runtime_state", return_value={"state": "OPEN"}),
            patch.object(
                launch, "find_active_lane", side_effect=self.fixture._read_active_lane
            ),
            patch.object(
                launch.processes, "spawn_detached", side_effect=record_before_spawn
            ) as spawn,
            patch.object(
                launch.processes,
                "process_identity",
                return_value={"pid": 41, "creation_time": "ct-1"},
            ),
            patch.object(
                launch,
                "_read_controller_status",
                return_value={
                    "schema": "controller-status/v1",
                    "lane_id": "launch-lane",
                    "controller_state": "exited",
                    "provider_state": {"state": "exited", "exit_code": 0},
                    "cleanup_proven": True,
                    "recorded_status": "review_pending",
                },
            ),
        ):
            result = launch.run_launch("launch-lane")

        self.assertTrue(result["ok"], result)
        self.assertEqual("LAUNCH_OK", result["code"])
        spawn.assert_called_once()
        self.assertEqual(str(self.fixture.harness), spawn.call_args.kwargs["cwd"])
        self.assertIsInstance(observed["env"], dict)

        intent_operations = observed["operations"]
        self.assertEqual(1, len(intent_operations))
        self.assertEqual("pending", intent_operations[0]["status"])
        self.assertEqual("dispatch", intent_operations[0]["kind"])
        self.assertEqual(decision_id, intent_operations[0]["decision_id"])

        memory_store = self.fixture.open_store(self.worktree)
        try:
            operations = memory_store.list_operations(decision_id)
            self.assertEqual(1, len(operations))
            self.assertEqual("delivered", operations[0]["status"])
            self.assertEqual(41, operations[0]["observed_invocation"]["pid"])
        finally:
            memory_store.close()
        lane = self.fixture.lane_record("launch-lane")
        self.assertEqual("running", lane["lifecycle"])
        self.assertFalse(lane["launch_pending"])

    def test_removed_envelope_is_pending_plan_and_never_spawns(self) -> None:
        store_path, envelope_path = self.fixture.memory_paths(self.worktree)
        decision_id = self.envelope["decision_id"]
        envelope_path.unlink()
        result, spawn = self.fixture.run_launch(lane_id="launch-lane")
        self.assertFalse(result["ok"], result)
        self.assertEqual(launch.LAUNCH_PLAN_PENDING, result["code"])
        self.assertIn("no finalized dispatch envelope", result["summary"])
        spawn.assert_not_called()
        memory_store = self.fixture.open_store(self.worktree)
        try:
            self.assertEqual([], memory_store.list_operations(decision_id))
        finally:
            memory_store.close()

    def test_missing_durable_final_context_is_invalid_and_never_spawns(self) -> None:
        store_path, _ = self.fixture.memory_paths(self.worktree)
        store_path.unlink()
        result, spawn = self.fixture.run_launch(lane_id="launch-lane")
        self.assertFalse(result["ok"], result)
        self.assertEqual(launch.LAUNCH_INVOCATION_INVALID, result["code"])
        spawn.assert_not_called()

    def test_copied_envelope_from_another_lane_fails_closed(self) -> None:
        other_card, _ = accepted_card(task="A different accepted task")
        other_result, other_worktree = self.fixture.run_bootstrap(
            lane_id="other-lane", card=other_card
        )
        self.assertTrue(other_result["ok"], other_result)
        other_envelope = memory_handoff.load_envelope(other_worktree)
        self.assertIsNotNone(other_envelope)
        _, envelope_path = self.fixture.memory_paths(self.worktree)
        self.fixture.write_json(envelope_path, other_envelope)

        result, spawn = self.fixture.run_launch(lane_id="launch-lane")
        self.assertFalse(result["ok"], result)
        self.assertEqual(launch.LAUNCH_INVOCATION_INVALID, result["code"])
        spawn.assert_not_called()
        memory_store = self.fixture.open_store(self.worktree)
        try:
            self.assertEqual(
                [], memory_store.list_operations(self.envelope["decision_id"])
            )
        finally:
            memory_store.close()

    def test_stale_envelope_after_a_new_plan_fails_closed(self) -> None:
        replacement, _ = accepted_card(
            task="A replacement task the envelope does not cover",
            base="a-different-base",
        )
        task_card_path = self.worktree / ".agent-workspace" / "task-card.json"
        self.fixture.write_json(task_card_path, replacement)
        result, spawn = self.fixture.run_launch(lane_id="launch-lane")
        self.assertFalse(result["ok"], result)
        self.assertEqual(launch.LAUNCH_INVOCATION_INVALID, result["code"])
        spawn.assert_not_called()

    def test_missing_durable_task_card_for_enhanced_lane_is_pending(self) -> None:
        task_card_path = self.worktree / ".agent-workspace" / "task-card.json"
        task_card_path.unlink()
        result, spawn = self.fixture.run_launch(lane_id="launch-lane")
        self.assertFalse(result["ok"], result)
        self.assertEqual(launch.LAUNCH_PLAN_PENDING, result["code"])
        spawn.assert_not_called()

    def test_legacy_lane_launches_without_memory_validation(self) -> None:
        card = legacy_card()
        result, worktree = self.fixture.run_bootstrap(
            lane_id="legacy-launch-lane", card=card
        )
        self.assertTrue(result["ok"], result)
        launched, spawn = self.fixture.run_launch(lane_id="legacy-launch-lane")
        self.assertTrue(launched["ok"], launched)
        self.assertEqual("LAUNCH_OK", launched["code"])
        spawn.assert_called_once()
        self.assertNotIn("env", spawn.call_args.kwargs)
        store_path, envelope_path = self.fixture.memory_paths(worktree)
        self.assertFalse(store_path.exists())
        self.assertFalse(envelope_path.exists())

    def test_all_off_lane_launches_without_memory_validation(self) -> None:
        card, _ = accepted_card(configuration={"all_features": False})
        result, worktree = self.fixture.run_bootstrap(
            lane_id="all-off-launch-lane", card=card
        )
        self.assertTrue(result["ok"], result)
        launched, spawn = self.fixture.run_launch(lane_id="all-off-launch-lane")
        self.assertTrue(launched["ok"], launched)
        self.assertEqual("LAUNCH_OK", launched["code"])
        spawn.assert_called_once()
        self.assertNotIn("env", spawn.call_args.kwargs)
        store_path, _ = self.fixture.memory_paths(worktree)
        self.assertFalse(store_path.exists())


class Step04ScrubbedWorkerEnvironmentTests(unittest.TestCase):
    """A prepared scrubbed lane fails closed without its durable card."""

    def setUp(self) -> None:
        self.fixture = LaunchBoundaryFixture()
        self.addCleanup(self.fixture.close)
        self.card = contracts.make_task_card(
            task="Draft only inside the bounded allowance",
            base_commit="test-base",
            branch="lane/scrubbed-lane",
            worker_environment="scrubbed",
        )
        result, self.worktree = self.fixture.run_bootstrap(
            lane_id="scrubbed-lane", card=self.card
        )
        self.assertTrue(result["ok"], result)

    def test_durable_scrubbed_requirement_is_recorded_at_bootstrap(self) -> None:
        lane = self.fixture.lane_record("scrubbed-lane")
        self.assertEqual("scrubbed", lane.get("worker_environment"))

    def test_missing_copied_card_fails_closed_without_inheriting(self) -> None:
        task_card_path = self.worktree / ".agent-workspace" / "task-card.json"
        task_card_path.unlink()
        result, spawn = self.fixture.run_launch(lane_id="scrubbed-lane")
        self.assertFalse(result["ok"], result)
        spawn.assert_not_called()

    def test_altered_copied_card_fails_closed_without_inheriting(self) -> None:
        task_card_path = self.worktree / ".agent-workspace" / "task-card.json"
        altered = {
            key: value
            for key, value in self.card.items()
            if key != "worker_environment"
        }
        self.fixture.write_json(task_card_path, altered)
        result, spawn = self.fixture.run_launch(lane_id="scrubbed-lane")
        self.assertFalse(result["ok"], result)
        spawn.assert_not_called()

    def test_intact_scrubbed_card_still_launches_with_a_scrubbed_environment(self) -> None:
        result, spawn = self.fixture.run_launch(lane_id="scrubbed-lane")
        self.assertTrue(result["ok"], result)
        spawn.assert_called_once()
        env = spawn.call_args.kwargs.get("env")
        self.assertIsInstance(env, dict)
        for key in ("MEMORY_HARNESS_CONTROL_TOKEN", "MEMORY_HARNESS_POLICY_TOKEN"):
            self.assertNotIn(key, env)


class Step04ResumeBoundaryTests(unittest.TestCase):
    """run_resume carries the same meaning as bootstrap for every state."""

    def setUp(self) -> None:
        self.fixture = LaunchBoundaryFixture()
        self.addCleanup(self.fixture.close)

    def test_resume_of_candidate_review_is_pending_plan(self) -> None:
        card, _ = candidate_card()
        result, worktree = self.fixture.run_bootstrap(
            lane_id="resume-candidate", card=card
        )
        self.assertEqual(bootstrap.BOOTSTRAP_PLAN_PENDING, result["code"])
        prior_run_id = self.fixture.lane_record("resume-candidate")["run_id"]
        self.fixture.make_resumable("resume-candidate")

        resumed = self.fixture.run_resume(lane_id="resume-candidate", card=card)
        self.assertFalse(resumed["ok"], resumed)
        self.assertEqual(resume.RESUME_PLAN_PENDING, resumed["code"])
        self.assertIn("ROOT review", resumed["summary"])
        self.assertIn("no worker was created or launched", resumed["next_action"])

        workspace = worktree / ".agent-workspace"
        for artifact in ("worker-prompt.md", "result-template.json", "invocation.json"):
            self.assertFalse((workspace / artifact).exists(), artifact)
        current = self.fixture.lane_record("resume-candidate")
        self.assertEqual(prior_run_id, current["run_id"])
        self.assertEqual("candidate_review", current["memory_plan_state"])
        self.assertFalse(current["dispatchable"])

    def test_resume_of_absent_plan_is_pending_plan(self) -> None:
        card, _ = absent_card()
        result, worktree = self.fixture.run_bootstrap(
            lane_id="resume-absent", card=card
        )
        self.assertEqual(bootstrap.BOOTSTRAP_PLAN_PENDING, result["code"])
        self.fixture.make_resumable("resume-absent")
        resumed = self.fixture.run_resume(lane_id="resume-absent", card=card)
        self.assertFalse(resumed["ok"], resumed)
        self.assertEqual(resume.RESUME_PLAN_PENDING, resumed["code"])
        self.assertIn("fresh ROOT-planning disposition", resumed["summary"])
        self.assertFalse((worktree / ".agent-workspace" / "invocation.json").exists())
        current = self.fixture.lane_record("resume-absent")
        self.assertEqual("absent", current["memory_plan_state"])
        self.assertFalse(current["dispatchable"])

    def test_resume_of_accepted_plan_refreshes_the_dispatchable_envelope(self) -> None:
        card, plan = accepted_card()
        result, worktree = self.fixture.run_bootstrap(
            lane_id="resume-accepted", card=card
        )
        self.assertTrue(result["ok"], result)
        first = memory_handoff.load_envelope(worktree)
        prior_run_id = self.fixture.lane_record("resume-accepted")["run_id"]
        self.fixture.make_resumable("resume-accepted")

        resumed = self.fixture.run_resume(lane_id="resume-accepted", card=card)
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual("RESUME_OK", resumed["code"])

        lane = self.fixture.lane_record("resume-accepted")
        self.assertNotEqual(prior_run_id, lane["run_id"])
        self.assertEqual("execution_accepted", lane["memory_plan_state"])
        self.assertTrue(lane["dispatchable"])
        envelope = memory_handoff.load_envelope(worktree)
        self.assertEqual(lane["run_id"], envelope["run_id"])
        self.assertEqual(first["decision_id"], envelope["decision_id"])
        self.assertEqual(plan["content_hash"], envelope["plan_digest"])
        memory_handoff.validate_envelope_for_launch(
            envelope=envelope,
            task_card=card,
            lane_id="resume-accepted",
            run_id=lane["run_id"],
            worktree_path=worktree,
            base_commit=card["base_commit"],
        )
        invocation = json.loads(
            (worktree / ".agent-workspace" / "invocation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(lane["run_id"], invocation["run_id"])
        memory_store = self.fixture.open_store(worktree)
        try:
            preparations = memory_store.list_preparations(envelope["decision_id"])
            self.assertGreaterEqual(len(preparations), 2)
            deadlines = {item["deadline_monotonic"] for item in preparations}
            self.assertEqual(1, len(deadlines))
            context = memory_store.get_final_context_for_decision(
                envelope["decision_id"]
            )
            self.assertEqual(lane["run_id"], context["run_id"])
        finally:
            memory_store.close()

        launched, spawn = self.fixture.run_launch(lane_id="resume-accepted")
        self.assertTrue(launched["ok"], launched)
        self.assertEqual("LAUNCH_OK", launched["code"])
        spawn.assert_called_once()
        memory_store = self.fixture.open_store(worktree)
        try:
            operations = memory_store.list_operations(envelope["decision_id"])
            self.assertEqual(1, len(operations))
            self.assertEqual("delivered", operations[0]["status"])
        finally:
            memory_store.close()

    def test_resume_of_legacy_lane_keeps_the_ordinary_path(self) -> None:
        card = legacy_card()
        result, worktree = self.fixture.run_bootstrap(
            lane_id="resume-legacy", card=card
        )
        self.assertTrue(result["ok"], result)
        self.fixture.make_resumable("resume-legacy")
        resumed = self.fixture.run_resume(lane_id="resume-legacy", card=card)
        self.assertTrue(resumed["ok"], resumed)
        lane = self.fixture.lane_record("resume-legacy")
        self.assertNotIn("memory_plan_state", lane)
        store_path, envelope_path = self.fixture.memory_paths(worktree)
        self.assertFalse(store_path.exists())
        self.assertFalse(envelope_path.exists())
        self.assertTrue((worktree / ".agent-workspace" / "invocation.json").is_file())


if __name__ == "__main__":
    unittest.main()

"""The near-match APC child runs through one real candidate-harness lane.

BEHAVIOR-02 requires the product harness, not a callback-only fake, to own the
one bounded drafting child.  This file drives the explicit near-match
``PreparationService`` path with the native launcher over the real
``run_bootstrap``/``run_launch``/``run_force_stop`` consumers, a deterministic
provider behavior that materializes the child's bounded artifact, and proves
that only the validated proposal is returned while ROOT acceptance stays
separate.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))

from memory_harness import (
    apc,
    config,
    contracts,
    experience,
    harness_child,
    local_adapters,
    preparation,
    store,
    templates,
)
from orchestrator_harness import bootstrap, core as harness_core, lanes, launch, processes, setup


BINDING = {
    "provider": "codex",
    "model": "test-model",
    "cli": "test-cli",
    "effort": "low",
    "source": "explicit",
}

ROOT_REPLAN = {
    "requested_by": "ROOT",
    "reason": "the tests exercise the explicit ROOT replan path",
}

BINDING_SOURCE = (
    "PROVIDER_ID = 'codex'\n"
    "ADAPTER_VERSION = 'test-v1'\n"
    "def validate_launch_config(*, model, launch_config): return dict(launch_config)\n"
    "def build_argv(**kwargs): return ['codex']\n"
    "def parse_line(line): return None\n"
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class QuietEvidenceService:
    """An explicit local evidence seam with no reviewed trajectories yet."""

    def search_recent_evidence(self, scope, query):
        return []


class NativeChildHarness:
    """A minimal but real harness root that bootstrap/launch accept."""

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
        self.launch_options: dict[str, str] = {}
        self.bootstrap_calls: list[str] = []
        self.provider_spawns: list[str] = []

    def close(self) -> None:
        self.temporary.cleanup()

    def write_text(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def write_json(self, path: Path, value: object) -> None:
        self.write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")

    # -- real harness entry points (only OS-level seams are patched) --------

    def bootstrap_patches(self):
        def fake_git_add(root, branch, target, base_commit) -> None:
            target.mkdir(parents=True, exist_ok=True)

        def record_bootstrap(**kwargs):
            self.bootstrap_calls.append(kwargs["lane_id"])
            return real_bootstrap(**kwargs)

        real_bootstrap = bootstrap.run_bootstrap
        return [
            patch("orchestrator_harness.config.find_harness_root", return_value=self.harness),
            patch.object(bootstrap, "open_epoch", return_value={"epoch_id": self.EPOCH}),
            patch.object(bootstrap, "read_active_lanes", return_value=[]),
            patch.object(bootstrap, "_git_worktree_add", side_effect=fake_git_add),
            patch.object(bootstrap.subprocess, "run"),
            patch.object(bootstrap, "run_bootstrap", side_effect=record_bootstrap),
        ]

    def lane_lookup(self, lane_id: str) -> dict:
        return lanes.read_lane(self.runtime, self.EPOCH, lane_id)

    def launch_patches(self, *, provider_behavior):
        def spawn_side_effect(argv, **kwargs):
            lane_id = argv[-1]
            provider_behavior(lane_id)
            return MagicMock(pid=41)

        def provider_spawn(*args, **kwargs):
            self.provider_spawns.append("direct")
            raise AssertionError("no direct provider process may be started")

        return [
            patch.object(launch, "find_harness_root", return_value=self.harness),
            patch.object(launch, "read_runtime_state", return_value={"state": "OPEN"}),
            patch.object(launch, "find_active_lane", side_effect=lambda rt, lane_id: (self.EPOCH, self.lane_lookup(lane_id))),
            patch.object(launch.processes, "spawn_detached", side_effect=spawn_side_effect),
            patch.object(
                launch.processes,
                "process_identity",
                return_value={"pid": 41, "creation_time": "ct-1"},
            ),
            patch.object(launch.processes, "terminate_process", return_value=True),
            patch.object(launch.processes, "cleanup_recorded_process_boundary", return_value=True),
            patch.object(launch.subprocess, "run"),
            patch.object(processes, "spawn_provider", side_effect=provider_spawn),
        ]

    def deterministic_child(self, *, artifact_name: str = "apc-result.json"):
        """The deterministic native child for this fixture.

        It reads the restricted drafting card the adapter queued, drafts only
        the permitted surface, and writes the bounded artifact plus the real
        result/v1 record and the controller's terminal status.
        """

        def behavior(lane_id: str) -> None:
            lane = self.lane_lookup(lane_id)
            worktree = Path(lane["worktree_path"])
            card = json.loads(
                (worktree / ".agent-workspace" / "task-card.json").read_text(encoding="utf-8")
            )
            match = re.search(r"```json\n(.*?)\n```", card["task"], re.DOTALL)
            assert match is not None, "the child card must carry the exact bounded request"
            request = json.loads(match.group(1))
            content = {
                "fixed_steps": list(request["template"]["fixed_steps"]),
                "verification_intent": request["template"]["verification_intent"],
            }
            if "bindings" in request["permitted_edits"]:
                content["bindings"] = {"failure": "parser test fails", "component": "parser"}
            proposal = contracts.make_plan(
                plan_id="native-apc-proposal-1",
                objective_id=request["parent_objective_id"],
                route="ordinary",
                state="proposed",
                content=content,
                source={
                    "template_id": request["template_id"],
                    "template_version": request["template_version"],
                    "branch": "apc_proposal",
                    "apc_request": request["content_hash"],
                },
            )
            artifact = apc.make_apc_result(request, proposal)
            self.write_json(worktree / ".agent-workspace" / artifact_name, artifact)
            result = {
                "schema": "result/v1",
                "lane_id": lane_id,
                "run_id": lane["run_id"],
                "outcome": "PASS",
                "summary": "bounded drafting artifact written for ROOT review",
                "evidence": [str(worktree / ".agent-workspace" / artifact_name)],
                "completed_at": "2026-09-23T12:00:00Z",
            }
            result["content_hash"] = harness_core.content_hash(result)
            self.write_json(worktree / "RESULT.json", result)
            self.write_json(
                Path(lane["controller_status_path"]),
                {
                    "schema": "controller-status/v1",
                    "lane_id": lane_id,
                    "run_id": lane["run_id"],
                    "controller_state": "exited",
                    "provider_state": {"state": "exited", "exit_code": 0, "pid": 41},
                    "cleanup_proven": True,
                    "recorded_status": "review_pending",
                },
            )

        return behavior


class NativeApcChildTests(unittest.TestCase):
    """One near match reaches one real candidate-harness child; ROOT acceptance stays separate."""

    def setUp(self) -> None:
        self.fixture = NativeChildHarness()
        self.addCleanup(self.fixture.close)
        self.clock = FakeClock()
        self.limits = config.resolve_limits(
            {
                "default_deadline_seconds": 300.0,
                "execution_reserve_seconds": 60.0,
                "direct_fill_threshold": 0.5,
                "near_match_threshold": 0.2,
            }
        )
        self.memory_store = store.MemoryStore(self.fixture.root / "memory-state.sqlite3")
        self.memory_store.initialize()
        self.addCleanup(self.memory_store.close)
        self.service = preparation.PreparationService(
            store=self.memory_store, limits=self.limits, clock=self.clock
        )
        self.stores = local_adapters.make_local_search_stores(
            experience_service=QuietEvidenceService(),
            scope=experience.ExperienceScope(
                application="harness",
                project="product",
                namespace="native-apc-child",
                owner="root-agent",
            ),
            registry=templates.load_default_templates(),
            limits=self.limits,
        )
        self.plan = contracts.make_plan(
            plan_id="candidate-plan",
            objective_id="objective-1",
            route="ordinary",
            state="candidate",
            content={"steps": ["draft"]},
        )

    # -- helpers -----------------------------------------------------------

    def _launcher(self, *, launch_options=None):
        return harness_child.make_native_apc_launcher(
            session=harness_child.DraftingChildSession(
                runtime_root=self.fixture.runtime,
                task_card_dir=self.fixture.root / "apc-cards",
                base_commit="test-base",
                provider=BINDING["provider"],
                model=BINDING["model"],
                launch_options=dict(
                    self.fixture.launch_options
                    if launch_options is None
                    else launch_options
                ),
            ),
            deadline=self.clock() + 240.0,
            clock=self.clock,
            lane_lookup=self.fixture.lane_lookup,
            sleep=lambda seconds: self.clock.advance(1.0),
        )

    def _prepare(self, launcher):
        return self.service.prepare(
            task_card=contracts.make_task_card(
                task="Fix the regression failure in the parser test", base_commit="base-1"
            ),
            plan=self.plan,
            objective_id="objective-1",
            route="ordinary",
            stores=list(self.stores),
            root_replan=ROOT_REPLAN,
            apc_binding=BINDING,
            apc_launcher=launcher,
        )

    def _native_stack(self, *, provider_behavior, force_stop=None):
        """Enter the real bootstrap/launch consumers, with OS-level seams patched."""

        stack = contextlib.ExitStack()
        for item in self.fixture.bootstrap_patches():
            stack.enter_context(item)
        for item in self.fixture.launch_patches(provider_behavior=provider_behavior):
            stack.enter_context(item)
        if force_stop is not None:
            stack.enter_context(patch.object(launch, "run_force_stop", return_value=force_stop))
        return stack

    def _operations(self, outcome):
        return self.memory_store.list_apc_child_operations(outcome.decision["decision_id"])

    # -- the native boundary ------------------------------------------------

    def test_near_match_runs_one_native_child_and_returns_only_a_proposal(self) -> None:
        with self._native_stack(provider_behavior=self.fixture.deterministic_child()):
            outcome = self._prepare(self._launcher())

        self.assertEqual("apc_proposal", outcome.disposition["branch"])
        self.assertEqual("proposed", outcome.proposal["state"])
        self.assertEqual("proposed", outcome.plan["state"])
        self.assertIsNone(outcome.disposition.get("root_acceptance"))

        # Exactly one child lane was queued and launched through the real
        # candidate-harness entry points, and no direct provider process ran.
        self.assertEqual(1, len(self.fixture.bootstrap_calls))
        lane_id = self.fixture.bootstrap_calls[0]
        self.assertTrue(lane_id.startswith("apc-child-"), lane_id)
        self.assertEqual([], self.fixture.provider_spawns)
        lane = self.fixture.lane_lookup(lane_id)

        # The queued lane carries the exact explicit binding effort, never a
        # silently inherited or session-substituted launch option.
        self.assertEqual(
            {"reasoning_effort": BINDING["effort"]},
            dict(lane["provider"]["launch_config"]),
        )

        operations = self._operations(outcome)
        self.assertEqual(1, len(operations))
        child = operations[0]
        self.assertEqual("reconciled", child["status"])
        observed = child["observed_invocation"]
        self.assertEqual("controller:41:ct-1", observed["invocation_id"])
        self.assertEqual(lane_id, observed["lane_id"])
        self.assertEqual(lane["run_id"], observed["run_id"])
        self.assertEqual(
            {"reasoning_effort": BINDING["effort"]}, dict(observed["launch_config"])
        )
        self.assertEqual(BINDING, child["binding"])
        self.assertTrue(child["cleanup"]["cleanup_proven"])
        self.assertIn("retired", child["cleanup"]["state"])

        # The child card carried only restricted drafting material: no memory
        # control state, no credentials, and no parent authority.
        workspace = Path(lane["worktree_path"]) / ".agent-workspace"
        card = json.loads((workspace / "task-card.json").read_text(encoding="utf-8"))
        self.assertNotIn("memory_handoff", card)
        self.assertFalse((workspace / "memory-state.sqlite3").exists())
        self.assertFalse((workspace / "memory-dispatch.json").exists())
        card_text = card["task"]
        self.assertIn("Drafting-only", card_text)
        self.assertIn("may_approve", card_text)
        self.assertIn(BINDING["provider"], card_text)
        self.assertIn(f"- effort: {BINDING['effort']}", card_text)

        # The harness itself retired the exact child lane.
        self.assertEqual("retired", self.fixture.lane_lookup(lane_id)["lifecycle"])

    def test_session_launch_options_cannot_override_the_explicit_binding(self) -> None:
        launcher = self._launcher(launch_options={"reasoning_effort": "high"})

        with self._native_stack(provider_behavior=self.fixture.deterministic_child()):
            outcome = self._prepare(launcher)

        # The conflicting session value is refused before anything is queued,
        # so the explicit binding effort is never silently overridden.
        self.assertEqual([], self.fixture.bootstrap_calls)
        self.assertEqual("fresh", outcome.disposition["branch"])
        reason = outcome.disposition["reuse_attempts"][0]["reason"]
        self.assertIn("ApcChildUnavailableError", reason)
        self.assertIn("binding", reason)
        self.assertIsNone(outcome.proposal)
        self.assertEqual([], self.fixture.provider_spawns)
        operations = self._operations(outcome)
        # The refusal is terminal: no child existed, so a later attempt is not
        # blocked by a live owner.
        self.assertEqual(["refused"], [operation["status"] for operation in operations])
        self.assertIn("refused", contracts.APC_CHILD_TERMINAL_STATUSES)

    def test_unproven_cleanup_cannot_reconcile_a_proposal(self) -> None:
        launcher = self._launcher()
        unproven = {
            "ok": False,
            "code": "FORCE_STOP_PROCESS_SURVIVED",
            "summary": "a lane process could not be terminated even forcibly",
            "evidence_paths": [],
            "next_action": "escalate to the operator/host",
        }

        with self._native_stack(
            provider_behavior=self.fixture.deterministic_child(), force_stop=unproven
        ):
            outcome = self._prepare(launcher)

        # The child produced a bounded artifact, but its exact retirement was
        # not proven, so nothing is shown to ROOT as a proposal.
        self.assertIsNone(outcome.proposal)
        self.assertEqual("fresh", outcome.disposition["branch"])
        reason = outcome.disposition["reuse_attempts"][0]["reason"]
        self.assertIn("ApcChildRejectedError", reason)
        self.assertIn("cleanup is not proven", reason)

        lane_id = self.fixture.bootstrap_calls[0]
        operations = self._operations(outcome)
        # One durable record per attempt: the latest exact status persists.
        self.assertEqual(["cleanup_pending"], [operation["status"] for operation in operations])
        pending = operations[-1]
        self.assertFalse(pending["cleanup"]["cleanup_proven"])
        self.assertIn("cleanup_pending", contracts.APC_CHILD_UNRESOLVED_STATUSES)
        self.assertEqual(lane_id, pending["observed_invocation"]["lane_id"])
        # The unproven child keeps its exact identity, and its lane is still
        # live: the unresolved native ownership stays visible, never hidden.
        self.assertEqual("controller:41:ct-1", pending["observed_invocation"]["invocation_id"])
        self.assertEqual("running", self.fixture.lane_lookup(lane_id)["lifecycle"])

    def test_unavailable_native_child_falls_back_without_an_unresolved_claim(self) -> None:
        launcher = self._launcher()

        with self._native_stack(provider_behavior=self.fixture.deterministic_child()):
            with patch.object(
                bootstrap,
                "run_bootstrap",
                return_value={
                    "ok": False,
                    "code": "BOOTSTRAP_ADAPTER_MISSING",
                    "summary": "launcher binding missing for provider codex",
                    "evidence_paths": [],
                    "next_action": "configure the provider adapter",
                },
            ):
                outcome = self._prepare(launcher)

        self.assertEqual("fresh", outcome.disposition["branch"])
        reason = outcome.disposition["reuse_attempts"][0]["reason"]
        self.assertIn("ApcChildUnavailableError", reason)
        self.assertEqual([], self.fixture.provider_spawns)
        operations = self._operations(outcome)
        # A proven-not-launched failure is terminal, so it never blocks a
        # later attempt the way an unresolved live child would.
        self.assertEqual(["refused"], [operation["status"] for operation in operations])
        self.assertIn("refused", contracts.APC_CHILD_TERMINAL_STATUSES)


if __name__ == "__main__":
    unittest.main()

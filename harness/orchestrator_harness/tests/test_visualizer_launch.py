from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from orchestrator_harness import visualizer_launch
from orchestrator_harness.config import HarnessConfig
from orchestrator_harness.epochs import EPOCH_STATE_SCHEMA, epoch_dir
from orchestrator_harness.records import atomic_write_json

EPOCH_ONE = "1" * 32
EPOCH_TWO = "2" * 32
EPOCH_CURRENT = "c" * 32


class VisualizerDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"
        self.harness = Path(self.temp.name) / "harness"
        self.runtime.mkdir()
        self.harness.mkdir()
        self.calls: list[str] = []

    def open_terminal(self) -> dict[str, str]:
        self.calls.append("open")
        return {"terminal": "test-terminal"}

    def decide(self, epoch_id: str = EPOCH_ONE, *, disabled: bool = False):
        return visualizer_launch._decide_for_epoch(
            self.runtime,
            self.harness,
            epoch_id,
            disabled=disabled,
            opener=self.open_terminal,
        )

    def record(self, epoch_id: str = EPOCH_ONE) -> dict[str, object]:
        return json.loads(
            visualizer_launch.decision_path(self.runtime, epoch_id).read_text(
                encoding="utf-8"
            )
        )

    def test_same_epoch_opens_once_and_new_epoch_opens_again(self) -> None:
        first = self.decide()
        second = self.decide()
        third = self.decide(EPOCH_TWO)

        self.assertEqual("opened", first["status"])
        self.assertEqual("already_open", second["status"])
        self.assertEqual("opened", third["status"])
        self.assertEqual(["open", "open"], self.calls)
        self.assertEqual("OPENED", self.record()["state"])
        self.assertEqual(EPOCH_ONE, self.record()["epoch_id"])

    def test_first_disabled_decision_suppresses_every_later_attempt(self) -> None:
        disabled = self.decide(disabled=True)
        later = self.decide()

        self.assertEqual("disabled", disabled["status"])
        self.assertEqual("disabled", later["status"])
        self.assertEqual([], self.calls)
        self.assertEqual("DISABLED", self.record()["state"])
        self.assertRegex(str(self.record()["attempt_id"]), r"^[0-9a-f]{32}$")
        self.assertTrue(disabled["decision_committed"])

    def test_late_disable_does_not_claim_to_close_an_open_window(self) -> None:
        self.decide()
        late = self.decide(disabled=True)

        self.assertEqual("already_open", late["status"])
        self.assertTrue(late["warning"])
        self.assertIn("cannot close", late["summary"])
        self.assertEqual(["open"], self.calls)

    def test_proven_spawn_failure_clears_attempt_and_is_retryable(self) -> None:
        def unavailable() -> dict[str, str]:
            self.calls.append("failed")
            raise visualizer_launch.VisualizerUnavailable("no desktop terminal")

        failed = visualizer_launch._decide_for_epoch(
            self.runtime,
            self.harness,
            EPOCH_ONE,
            disabled=False,
            opener=unavailable,
        )
        self.assertEqual("unavailable", failed["status"])
        self.assertFalse(
            visualizer_launch.decision_path(self.runtime, EPOCH_ONE).exists()
        )

        retried = self.decide()
        self.assertEqual("opened", retried["status"])
        self.assertEqual(["failed", "open"], self.calls)

    def test_untyped_post_spawn_error_stays_ambiguous_and_never_respawns(self) -> None:
        def uncertain() -> dict[str, str]:
            self.calls.append("maybe-opened")
            raise RuntimeError("wait status became unavailable after spawn")

        first = visualizer_launch._decide_for_epoch(
            self.runtime,
            self.harness,
            EPOCH_ONE,
            disabled=False,
            opener=uncertain,
        )
        second = self.decide()

        self.assertEqual("ambiguous", first["status"])
        self.assertEqual("ambiguous", second["status"])
        self.assertEqual(["maybe-opened"], self.calls)
        self.assertEqual("ATTEMPTING", self.record()["state"])
        self.assertTrue(first["decision_committed"])

    def test_opened_publication_failure_stays_ambiguous_and_never_respawns(self) -> None:
        real_write = visualizer_launch.atomic_write_json
        writes = 0

        def fail_second(path: Path, value: object) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("injected opened-record failure")
            real_write(path, value)

        with mock.patch.object(
            visualizer_launch, "atomic_write_json", side_effect=fail_second
        ):
            first = self.decide()

        second = self.decide()
        self.assertEqual("ambiguous", first["status"])
        self.assertEqual("ambiguous", second["status"])
        self.assertEqual(["open"], self.calls)
        self.assertEqual("ATTEMPTING", self.record()["state"])

    def test_malformed_or_unknown_record_is_ambiguous_not_replaced(self) -> None:
        path = visualizer_launch.decision_path(self.runtime, EPOCH_ONE)
        path.parent.mkdir(parents=True)
        original = {"schema": "unknown/v9", "state": "OPENED"}
        path.write_text(json.dumps(original), encoding="utf-8")

        result = self.decide()
        self.assertEqual("ambiguous", result["status"])
        self.assertEqual([], self.calls)
        self.assertEqual(original, json.loads(path.read_text(encoding="utf-8")))
        self.assertFalse(result["decision_committed"])

    def test_incomplete_known_state_record_is_ambiguous_not_committed(self) -> None:
        path = visualizer_launch.decision_path(self.runtime, EPOCH_ONE)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema": visualizer_launch.DECISION_SCHEMA,
                    "epoch_id": EPOCH_ONE,
                    "state": "OPENED",
                }
            ),
            encoding="utf-8",
        )

        result = self.decide()
        self.assertEqual("ambiguous", result["status"])
        self.assertFalse(result["decision_committed"])
        self.assertEqual([], self.calls)

    def test_concurrent_default_attempts_spawn_only_once(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        results: list[dict[str, object]] = []

        def slow_open() -> dict[str, str]:
            self.calls.append("open")
            entered.set()
            self.assertTrue(release.wait(5.0))
            return {"terminal": "test-terminal"}

        def run() -> None:
            results.append(
                visualizer_launch._decide_for_epoch(
                    self.runtime,
                    self.harness,
                    EPOCH_ONE,
                    disabled=False,
                    opener=slow_open,
                )
            )

        first = threading.Thread(target=run)
        second = threading.Thread(target=run)
        first.start()
        self.assertTrue(entered.wait(5.0))
        second.start()
        release.set()
        first.join(5.0)
        second.join(5.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(["open"], self.calls)
        self.assertEqual({"opened", "already_open"}, {r["status"] for r in results})

    def test_concurrent_enabled_then_disabled_keeps_first_committed_open(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        results: list[dict[str, object]] = []

        def slow_open() -> dict[str, str]:
            self.calls.append("open")
            entered.set()
            self.assertTrue(release.wait(5.0))
            return {"terminal": "test-terminal"}

        enabled = threading.Thread(
            target=lambda: results.append(
                visualizer_launch._decide_for_epoch(
                    self.runtime,
                    self.harness,
                    EPOCH_ONE,
                    disabled=False,
                    opener=slow_open,
                )
            )
        )
        disabled = threading.Thread(
            target=lambda: results.append(
                visualizer_launch._decide_for_epoch(
                    self.runtime,
                    self.harness,
                    EPOCH_ONE,
                    disabled=True,
                    opener=self.open_terminal,
                )
            )
        )
        enabled.start()
        self.assertTrue(entered.wait(5.0))
        disabled.start()
        release.set()
        enabled.join(5.0)
        disabled.join(5.0)

        self.assertEqual(["open"], self.calls)
        self.assertEqual({"opened", "already_open"}, {r["status"] for r in results})

    def test_concurrent_disabled_then_enabled_keeps_first_committed_opt_out(self) -> None:
        original_write = visualizer_launch.atomic_write_json
        committed = threading.Event()
        release = threading.Event()
        results: list[dict[str, object]] = []

        def slow_disabled_write(path: Path, value: object) -> None:
            original_write(path, value)
            if isinstance(value, dict) and value.get("state") == "DISABLED":
                committed.set()
                self.assertTrue(release.wait(5.0))

        def decide(disabled: bool) -> None:
            results.append(
                visualizer_launch._decide_for_epoch(
                    self.runtime,
                    self.harness,
                    EPOCH_ONE,
                    disabled=disabled,
                    opener=self.open_terminal,
                )
            )

        with mock.patch.object(
            visualizer_launch, "atomic_write_json", side_effect=slow_disabled_write
        ):
            first = threading.Thread(target=decide, args=(True,))
            second = threading.Thread(target=decide, args=(False,))
            first.start()
            self.assertTrue(committed.wait(5.0))
            second.start()
            release.set()
            first.join(5.0)
            second.join(5.0)

        self.assertEqual([], self.calls)
        self.assertEqual({"disabled"}, {r["status"] for r in results})

    @staticmethod
    def _write_active_epoch(runtime: Path, epoch_id: str) -> None:
        directory = epoch_dir(runtime, epoch_id)
        directory.mkdir(parents=True)
        atomic_write_json(
            directory / "epoch-state.json",
            {
                "schema": EPOCH_STATE_SCHEMA,
                "epoch_id": epoch_id,
                "lifecycle": "active",
            },
        )

    def test_public_entry_binds_the_exact_current_epoch_and_harness_root(self) -> None:
        workspace = Path(self.temp.name) / "project"
        workspace.mkdir()
        config = HarnessConfig(
            harness_root=self.harness,
            root_workspace=workspace,
            suite_root=workspace,
        )
        config.runtime_root.mkdir()
        self._write_active_epoch(config.runtime_root, EPOCH_CURRENT)
        atomic_write_json(
            visualizer_launch.current_epoch_path(config.runtime_root),
            {
                "schema": visualizer_launch.CURRENT_EPOCH_SCHEMA,
                "epoch_id": EPOCH_CURRENT,
                "lane_mode": "managed",
                "opened_at": "2026-10-02T00:00:00Z",
            },
        )
        seen: list[Path] = []
        with (
            mock.patch.object(
                visualizer_launch, "find_harness_root", return_value=self.harness
            ),
            mock.patch.object(visualizer_launch, "load_config", return_value=config),
            mock.patch.object(
                visualizer_launch,
                "_open_visualizer_terminal",
                side_effect=lambda root: (seen.append(root), {"terminal": "test"})[1],
            ),
        ):
            result = visualizer_launch.ensure_for_current_epoch()

        self.assertEqual("opened", result["status"])
        self.assertEqual([self.harness], seen)
        decision = json.loads(
            visualizer_launch.decision_path(
                config.runtime_root, EPOCH_CURRENT
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(EPOCH_CURRENT, decision["epoch_id"])

    def test_public_entry_rejects_traversal_epoch_id_without_writing(self) -> None:
        workspace = Path(self.temp.name) / "project-traversal"
        workspace.mkdir()
        config = HarnessConfig(
            harness_root=self.harness,
            root_workspace=workspace,
            suite_root=workspace,
        )
        config.runtime_root.mkdir()
        atomic_write_json(
            visualizer_launch.current_epoch_path(config.runtime_root),
            {
                "schema": visualizer_launch.CURRENT_EPOCH_SCHEMA,
                "epoch_id": "../../escaped",
                "lane_mode": "managed",
                "opened_at": "2026-10-02T00:00:00Z",
            },
        )
        with (
            mock.patch.object(
                visualizer_launch, "find_harness_root", return_value=self.harness
            ),
            mock.patch.object(visualizer_launch, "load_config", return_value=config),
            mock.patch.object(visualizer_launch, "_open_visualizer_terminal") as opened,
        ):
            result = visualizer_launch.ensure_for_current_epoch()

        self.assertEqual("ambiguous", result["status"])
        opened.assert_not_called()
        self.assertFalse((workspace / "escaped" / "visualizer-auto-launch.json").exists())

    def test_public_entry_requires_matching_active_epoch_state(self) -> None:
        workspace = Path(self.temp.name) / "project-state"
        workspace.mkdir()
        config = HarnessConfig(
            harness_root=self.harness,
            root_workspace=workspace,
            suite_root=workspace,
        )
        config.runtime_root.mkdir()
        atomic_write_json(
            visualizer_launch.current_epoch_path(config.runtime_root),
            {
                "schema": visualizer_launch.CURRENT_EPOCH_SCHEMA,
                "epoch_id": EPOCH_CURRENT,
                "lane_mode": "managed",
                "opened_at": "2026-10-02T00:00:00Z",
            },
        )
        with (
            mock.patch.object(
                visualizer_launch, "find_harness_root", return_value=self.harness
            ),
            mock.patch.object(visualizer_launch, "load_config", return_value=config),
            mock.patch.object(visualizer_launch, "_open_visualizer_terminal") as opened,
        ):
            missing = visualizer_launch.ensure_for_current_epoch()
        self.assertEqual("ambiguous", missing["status"])
        opened.assert_not_called()

        self._write_active_epoch(config.runtime_root, EPOCH_CURRENT)
        state_path = epoch_dir(config.runtime_root, EPOCH_CURRENT) / "epoch-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["epoch_id"] = EPOCH_TWO
        atomic_write_json(state_path, state)
        with (
            mock.patch.object(
                visualizer_launch, "find_harness_root", return_value=self.harness
            ),
            mock.patch.object(visualizer_launch, "load_config", return_value=config),
        ):
            mismatch = visualizer_launch.ensure_for_current_epoch()
        self.assertEqual("ambiguous", mismatch["status"])

    def test_public_entry_rejects_redirected_epoch_directory(self) -> None:
        workspace = Path(self.temp.name) / "project-link"
        workspace.mkdir()
        config = HarnessConfig(
            harness_root=self.harness,
            root_workspace=workspace,
            suite_root=workspace,
        )
        (config.runtime_root / "epochs").mkdir(parents=True)
        outside = Path(self.temp.name) / "outside-epoch"
        outside.mkdir()
        try:
            (config.runtime_root / "epochs" / EPOCH_CURRENT).symlink_to(
                outside, target_is_directory=True
            )
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        atomic_write_json(
            visualizer_launch.current_epoch_path(config.runtime_root),
            {
                "schema": visualizer_launch.CURRENT_EPOCH_SCHEMA,
                "epoch_id": EPOCH_CURRENT,
                "lane_mode": "managed",
                "opened_at": "2026-10-02T00:00:00Z",
            },
        )
        with (
            mock.patch.object(
                visualizer_launch, "find_harness_root", return_value=self.harness
            ),
            mock.patch.object(visualizer_launch, "load_config", return_value=config),
        ):
            result = visualizer_launch.ensure_for_current_epoch()
        self.assertEqual("ambiguous", result["status"])
        self.assertFalse((outside / "visualizer-auto-launch.json").exists())

    def test_public_entry_never_spawns_without_an_active_epoch(self) -> None:
        workspace = Path(self.temp.name) / "project"
        workspace.mkdir()
        config = HarnessConfig(
            harness_root=self.harness,
            root_workspace=workspace,
            suite_root=workspace,
        )
        config.runtime_root.mkdir()
        with (
            mock.patch.object(
                visualizer_launch, "find_harness_root", return_value=self.harness
            ),
            mock.patch.object(visualizer_launch, "load_config", return_value=config),
            mock.patch.object(visualizer_launch, "_open_visualizer_terminal") as opened,
        ):
            result = visualizer_launch.ensure_for_current_epoch()
        self.assertEqual("no_active_epoch", result["status"])
        opened.assert_not_called()

    def test_public_entry_converts_runtime_lock_failure_to_nonfatal_result(self) -> None:
        with mock.patch.object(
            visualizer_launch,
            "_ensure_for_current_epoch",
            side_effect=OSError("runtime is read-only"),
        ):
            result = visualizer_launch.ensure_for_current_epoch()
        self.assertEqual("failed", result["status"])
        self.assertTrue(result["warning"])
        self.assertIn("orchestrator-harness view", result["next_action"])


class VisualizerTerminalTests(unittest.TestCase):
    def test_child_environment_is_an_allowlist_without_credentials(self) -> None:
        environment = visualizer_launch.visualizer_environment(
            {
                "PATH": "/bin",
                "HOME": "/home/operator",
                "DISPLAY": ":4",
                "PYTHONPATH": "/source",
                "LANG": "en_US.UTF-8",
                "LC_TIME": "C",
                "OPENAI_API_KEY": "provider-secret",
                "MEMORY_HARNESS_CONTROL_TOKEN": "control-secret",
                "TASK_ONLY_TOKEN": "task-secret",
                "AWS_SESSION_TOKEN": "cloud-secret",
                "TMP": "provider-secret",
                "LC_OPENAIKEY": "concatenated-secret",
                "Lc_Time_Token": "mixed-secret",
                "UNRELATED_VALUE": "not-needed",
            }
        )

        self.assertEqual(
            {
                "PATH": "/bin",
                "HOME": "/home/operator",
                "DISPLAY": ":4",
                "PYTHONPATH": "/source",
                "LANG": "en_US.UTF-8",
                "LC_TIME": "C",
            },
            environment,
        )
        self.assertNotIn("provider-secret", repr(environment))
        self.assertNotIn("control-secret", repr(environment))
        self.assertNotIn("task-secret", repr(environment))
        self.assertNotIn("concatenated-secret", repr(environment))
        self.assertNotIn("mixed-secret", repr(environment))
        self.assertNotIn("TMP", environment)

    def test_locale_allowlist_is_finite_and_case_normalized(self) -> None:
        environment = visualizer_launch.visualizer_environment(
            {
                "LC_TIME": "C",
                "lc_messages": "en_US.UTF-8",
                "LC_NOT_A_REAL_LOCALE_FIELD": "private-data",
                "LC_OPENAIKEY": "provider-secret",
            }
        )
        self.assertEqual(
            {"LC_TIME": "C", "lc_messages": "en_US.UTF-8"}, environment
        )

    def test_linux_requires_display_and_uses_first_known_terminal(self) -> None:
        with self.assertRaises(visualizer_launch.VisualizerUnavailable):
            visualizer_launch._terminal_invocation(
                ["/venv/python", "-m", "orchestrator_harness.operator_launch", "view"],
                Path("/harness"),
                {"PATH": "/bin"},
                platform_name="linux",
                os_name="posix",
                which=lambda _name: "/bin/xterm",
            )

        found = {"gnome-terminal": "/usr/bin/gnome-terminal", "xterm": "/usr/bin/xterm"}
        invocation = visualizer_launch._terminal_invocation(
            ["/venv/python", "-m", "orchestrator_harness.operator_launch", "view"],
            Path("/harness with spaces"),
            {"PATH": "/bin", "WAYLAND_DISPLAY": "wayland-0"},
            platform_name="linux",
            os_name="posix",
            which=lambda name: found.get(name),
        )
        self.assertEqual("gnome-terminal", invocation.terminal)
        self.assertEqual(
            [
                "/usr/bin/gnome-terminal",
                "--",
                "/venv/python",
                "-m",
                "orchestrator_harness.operator_launch",
                "view",
            ],
            invocation.argv,
        )
        self.assertEqual("/harness with spaces", invocation.options["cwd"])
        self.assertTrue(invocation.options["start_new_session"])

    def test_linux_normalizes_relative_terminal_path_before_changing_cwd(self) -> None:
        invocation = visualizer_launch._terminal_invocation(
            ["/venv/python", "-m", "orchestrator_harness.operator_launch", "view"],
            Path("/different-child-cwd"),
            {"PATH": "relative-bin", "DISPLAY": ":1"},
            platform_name="linux",
            os_name="posix",
            which=lambda name: "relative-bin/xterm" if name == "xterm" else None,
        )
        self.assertTrue(Path(invocation.argv[0]).is_absolute())
        self.assertEqual(
            str(Path("relative-bin/xterm").absolute()), invocation.argv[0]
        )

    def test_macos_uses_fixed_applescript_and_dynamic_argv(self) -> None:
        hostile_root = Path("/tmp/project'; touch OWNED; echo '")
        hostile_python = "/tmp/venv$(touch PWNED)/python"
        invocation = visualizer_launch._terminal_invocation(
            [hostile_python, "-m", "orchestrator_harness.operator_launch", "view"],
            hostile_root,
            {"HOME": "/Users/me", "PATH": "/usr/bin", "OPENAI_API_KEY": "must-not-pass"},
            platform_name="darwin",
            os_name="posix",
            which=lambda name: "/usr/bin/osascript" if name == "osascript" else None,
        )

        script = invocation.argv[2]
        self.assertEqual("osascript-terminal", invocation.terminal)
        self.assertIn("quoted form of", script)
        self.assertIn("env -i", script)
        self.assertNotIn(str(hostile_root), script)
        self.assertNotIn(hostile_python, script)
        self.assertIn(str(hostile_root), invocation.argv[4:])
        self.assertIn(hostile_python, invocation.argv[4:])
        self.assertNotIn("must-not-pass", repr(invocation.argv))
        self.assertNotIn("must-not-pass", repr(invocation.options["env"]))

    def test_windows_uses_new_console_without_shell(self) -> None:
        invocation = visualizer_launch._terminal_invocation(
            [r"C:\venv\python.exe", "-m", "orchestrator_harness.operator_launch", "view"],
            Path(r"C:\harness"),
            {"SYSTEMROOT": r"C:\Windows", "PATH": r"C:\Windows\System32"},
            platform_name="win32",
            os_name="nt",
            which=lambda _name: None,
        )
        self.assertEqual("windows-console", invocation.terminal)
        self.assertEqual(r"C:\venv\python.exe", invocation.argv[0])
        self.assertFalse(invocation.options.get("shell", False))
        self.assertTrue(invocation.options["creationflags"] & 0x00000010)

    def test_immediate_nonzero_exit_is_a_proven_failure(self) -> None:
        process = mock.Mock()
        process.wait.return_value = 7
        invocation = visualizer_launch.TerminalInvocation(
            "test", ["terminal", "--", "viewer"], {"cwd": "/harness", "env": {}}
        )
        with mock.patch.object(visualizer_launch.subprocess, "Popen", return_value=process):
            with self.assertRaises(visualizer_launch.VisualizerLaunchFailed):
                visualizer_launch._spawn_terminal(invocation)
        process.wait.assert_called_once_with(timeout=visualizer_launch.IMMEDIATE_EXIT_SECONDS)

    def test_running_terminal_is_accepted_after_short_bounded_probe(self) -> None:
        process = mock.Mock()
        process.wait.side_effect = subprocess.TimeoutExpired(["terminal"], 0.1)
        invocation = visualizer_launch.TerminalInvocation(
            "test", ["terminal", "--", "viewer"], {"cwd": "/harness", "env": {}}
        )
        with mock.patch.object(visualizer_launch.subprocess, "Popen", return_value=process):
            accepted = visualizer_launch._spawn_terminal(invocation)
        self.assertEqual({"terminal": "test"}, accepted)


if __name__ == "__main__":
    unittest.main()

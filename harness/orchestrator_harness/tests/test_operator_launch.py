from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from orchestrator_harness import operator_launch


class OperatorLaunchV2Tests(unittest.TestCase):
    def test_parser_exposes_only_v2_route(self) -> None:
        parser = operator_launch._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--receipt", "detached.json"])
        parsed = parser.parse_args(["harness", "shutdown"])
        self.assertEqual("harness", parsed.command)
        self.assertEqual("shutdown", parsed.harness_command)

    def test_receipt_dispatch_is_not_reachable(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as raised:
                operator_launch.main(["--receipt", "detached.json"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("invalid choice", output.getvalue())

    def test_force_release_is_only_under_top_level_lease(self) -> None:
        parser = operator_launch._build_parser()
        parsed = parser.parse_args(
            ["lease", "force-release", "--resource-id", "resource-1"]
        )
        self.assertEqual("lease", parsed.command)
        self.assertEqual("force-release", parsed.lease_command)
        self.assertEqual("resource-1", parsed.resource_id)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["lane", "force-release", "--resource-id", "resource-1"]
                )

    def test_manager_close_requires_and_normalizes_summary(self) -> None:
        parser = operator_launch._build_parser()
        for outcome in ("COMPLETE", "BLOCKED"):
            for summary in (None, "", "   "):
                argv = [
                    "manager", "close", "--event-id", "event-1",
                    "--outcome", outcome,
                ]
                if summary is not None:
                    argv.extend(["--summary", summary])
                with self.subTest(outcome=outcome, summary=summary):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            parser.parse_args(argv)
            parsed = parser.parse_args(
                [
                    "manager", "close", "--event-id", "event-1",
                    "--outcome", outcome, "--summary",
                    "  operator decision  ",
                ]
            )
            self.assertEqual("operator decision", parsed.summary)

    def test_bootstrap_collects_explicit_provider_options(self) -> None:
        parser = operator_launch._build_parser()
        parsed = parser.parse_args(
            [
                "lane", "bootstrap", "--lane-id", "lane-1",
                "--provider", "codex", "--model", "configured-model",
                "--provider-option", "reasoning_effort=xhigh",
                "--provider-option", "service_tier=flex",
                "--task-card", "task.json",
            ]
        )
        self.assertEqual(
            {"reasoning_effort": "xhigh", "service_tier": "flex"},
            operator_launch._provider_options(parsed.provider_option),
        )
        with self.assertRaisesRegex(ValueError, "duplicate provider option"):
            operator_launch._provider_options([("effort", "high"), ("effort", "low")])

    def test_lane_launch_visualizer_is_default_on_with_explicit_opt_out(self) -> None:
        parser = operator_launch._build_parser()
        default = parser.parse_args(["lane", "launch", "--lane-id", "lane-1"])
        disabled = parser.parse_args(
            ["lane", "launch", "--lane-id", "lane-1", "--no-visualizer"]
        )
        self.assertFalse(default.no_visualizer)
        self.assertTrue(disabled.no_visualizer)

    def test_lane_launch_decides_visualizer_before_provider_and_decorates_result(self) -> None:
        parser = operator_launch._build_parser()
        parsed = parser.parse_args(["lane", "launch", "--lane-id", "lane-1"])
        order: list[str] = []
        visualizer = {
            "ok": True,
            "code": "VISUALIZER_OPENED",
            "status": "opened",
            "summary": "visualizer opened",
            "evidence_paths": [],
            "next_action": "none",
            "warning": False,
        }

        with (
            mock.patch.object(
                operator_launch.visualizer_launch,
                "ensure_for_current_epoch",
                side_effect=lambda **_kwargs: (order.append("visualizer"), visualizer)[1],
            ) as ensure,
            mock.patch.object(
                operator_launch.launch,
                "run_launch",
                side_effect=lambda _lane: (
                    order.append("provider"),
                    {
                        "ok": True,
                        "code": "LAUNCH_OK",
                        "summary": "lane started",
                        "evidence_paths": [],
                        "next_action": "wait",
                    },
                )[1],
            ),
        ):
            result = operator_launch._dispatch(parsed)

        self.assertEqual(["visualizer", "provider"], order)
        ensure.assert_called_once_with(disabled=False)
        self.assertEqual(visualizer, result["visualizer"])
        self.assertTrue(result["ok"])

    def test_lane_launch_opt_out_is_an_epoch_decision_even_when_launch_fails(self) -> None:
        parser = operator_launch._build_parser()
        parsed = parser.parse_args(
            ["lane", "launch", "--lane-id", "lane-1", "--no-visualizer"]
        )
        disabled = {
            "ok": True,
            "code": "VISUALIZER_DISABLED",
            "status": "disabled",
            "summary": "automatic visualizer disabled",
            "evidence_paths": [],
            "next_action": "run the viewer manually if wanted",
            "warning": False,
            "decision_committed": True,
        }
        failure = {
            "ok": False,
            "code": "LAUNCH_PROVIDER_START_FAILED",
            "summary": "provider failed",
            "evidence_paths": [],
            "next_action": "retry",
        }
        with (
            mock.patch.object(
                operator_launch.visualizer_launch,
                "ensure_for_current_epoch",
                return_value=disabled,
            ) as ensure,
            mock.patch.object(operator_launch.launch, "run_launch", return_value=failure),
        ):
            result = operator_launch._dispatch(parsed)
        ensure.assert_called_once_with(disabled=True)
        self.assertEqual(disabled, result["visualizer"])
        self.assertEqual("LAUNCH_PROVIDER_START_FAILED", result["code"])

    def test_lane_launch_refuses_provider_when_explicit_opt_out_cannot_persist(self) -> None:
        parsed = operator_launch._build_parser().parse_args(
            ["lane", "launch", "--lane-id", "lane-1", "--no-visualizer"]
        )
        failed = {
            "ok": False,
            "code": "VISUALIZER_FAILED",
            "status": "failed",
            "summary": "cannot persist the visualizer opt-out",
            "evidence_paths": ["decision.json"],
            "next_action": "resolve runtime storage",
            "warning": True,
            "decision_committed": False,
        }
        with (
            mock.patch.object(
                operator_launch.visualizer_launch,
                "ensure_for_current_epoch",
                return_value=failed,
            ),
            mock.patch.object(operator_launch.launch, "run_launch") as run_launch,
        ):
            result = operator_launch._dispatch(parsed)

        run_launch.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual("VISUALIZER_OPT_OUT_FAILED", result["code"])
        self.assertEqual(failed, result["visualizer"])

    def test_lane_launch_opt_out_blocks_uncommitted_ambiguous_state(self) -> None:
        parsed = operator_launch._build_parser().parse_args(
            ["lane", "launch", "--lane-id", "lane-1", "--no-visualizer"]
        )
        ambiguous = {
            "ok": False,
            "code": "VISUALIZER_AMBIGUOUS",
            "status": "ambiguous",
            "summary": "active epoch state is invalid",
            "evidence_paths": ["epoch-state.json"],
            "next_action": "repair the runtime",
            "warning": True,
            "decision_committed": False,
        }
        with (
            mock.patch.object(
                operator_launch.visualizer_launch,
                "ensure_for_current_epoch",
                return_value=ambiguous,
            ),
            mock.patch.object(operator_launch.launch, "run_launch") as run_launch,
        ):
            result = operator_launch._dispatch(parsed)
        run_launch.assert_not_called()
        self.assertEqual("VISUALIZER_OPT_OUT_FAILED", result["code"])

    def test_lane_launch_opt_out_allows_verified_prior_attempt_ambiguity(self) -> None:
        parsed = operator_launch._build_parser().parse_args(
            ["lane", "launch", "--lane-id", "lane-1", "--no-visualizer"]
        )
        ambiguous = {
            "ok": False,
            "code": "VISUALIZER_AMBIGUOUS",
            "status": "ambiguous",
            "summary": "a prior launch may have opened the viewer",
            "evidence_paths": ["visualizer-auto-launch.json"],
            "next_action": "inspect or use the manual viewer",
            "warning": True,
            "decision_committed": True,
        }
        launched = {
            "ok": True,
            "code": "LAUNCH_OK",
            "summary": "lane started",
            "evidence_paths": [],
            "next_action": "wait",
        }
        with (
            mock.patch.object(
                operator_launch.visualizer_launch,
                "ensure_for_current_epoch",
                return_value=ambiguous,
            ),
            mock.patch.object(
                operator_launch.launch, "run_launch", return_value=launched
            ) as run_launch,
        ):
            result = operator_launch._dispatch(parsed)
        run_launch.assert_called_once_with("lane-1")
        self.assertTrue(result["ok"])
        self.assertEqual(ambiguous, result["visualizer"])

    def test_human_visualizer_warning_uses_stderr_without_failing_lane(self) -> None:
        result = {
            "ok": True,
            "code": "LAUNCH_OK",
            "summary": "lane started",
            "evidence_paths": [],
            "next_action": "wait",
            "visualizer": {
                "ok": False,
                "code": "VISUALIZER_UNAVAILABLE",
                "status": "unavailable",
                "summary": "no graphical terminal is available",
                "evidence_paths": [],
                "next_action": "run `orchestrator-harness view` manually",
                "warning": True,
            },
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = operator_launch._emit(result, as_json=False)
        self.assertEqual(0, code)
        self.assertEqual("lane started\n", stdout.getvalue())
        self.assertIn("VISUALIZER_UNAVAILABLE", stderr.getvalue())
        self.assertIn("orchestrator-harness view", stderr.getvalue())

    def test_json_visualizer_warning_is_only_in_structured_stdout(self) -> None:
        result = {
            "ok": True,
            "code": "LAUNCH_OK",
            "summary": "lane started",
            "evidence_paths": [],
            "next_action": "wait",
            "visualizer": {
                "ok": False,
                "code": "VISUALIZER_FAILED",
                "status": "failed",
                "summary": "terminal rejected the command",
                "evidence_paths": [],
                "next_action": "run `orchestrator-harness view` manually",
                "warning": True,
            },
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = operator_launch._emit(result, as_json=True)
        self.assertEqual(0, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(result, __import__("json").loads(stdout.getvalue()))

    def test_human_mode_keeps_visualizer_warning_when_lane_also_fails(self) -> None:
        result = {
            "ok": False,
            "code": "LAUNCH_PROVIDER_START_FAILED",
            "summary": "provider failed",
            "evidence_paths": [],
            "next_action": "retry",
            "visualizer": {
                "ok": False,
                "code": "VISUALIZER_AMBIGUOUS",
                "status": "ambiguous",
                "summary": "a viewer may already be open",
                "evidence_paths": [],
                "next_action": "run `orchestrator-harness view` manually",
                "warning": True,
            },
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = operator_launch._emit(result, as_json=False)
        self.assertEqual(1, code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("LAUNCH_PROVIDER_START_FAILED", stderr.getvalue())
        self.assertIn("VISUALIZER_AMBIGUOUS", stderr.getvalue())
        self.assertIn("orchestrator-harness view", stderr.getvalue())

    def test_v2_commands_dispatch_through_native_modules(self) -> None:
        with mock.patch.object(
            operator_launch.setup,
            "run_setup",
            return_value={"ok": True, "summary": "setup"},
        ) as run_setup:
            self.assertEqual(0, operator_launch.main(["harness", "setup"]))
        run_setup.assert_called_once_with(overwrite=False)


if __name__ == "__main__":
    unittest.main()

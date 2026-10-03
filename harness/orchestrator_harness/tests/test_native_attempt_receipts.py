"""Source-shaped usage observations at the controller's attempt boundary."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator_harness import attempt_attestation, controller, memory_handoff
from orchestrator_harness.core import content_hash
from orchestrator_harness.records import read_jsonl


ADAPTERS = Path(__file__).resolve().parents[1] / "provider_adapters"


def binding(provider: str):
    path = ADAPTERS / provider / "launcher_binding.py"
    spec = importlib.util.spec_from_file_location(f"receipt_{provider}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lane_at(root: Path, provider: str) -> dict:
    return {
        "lane_id": "lane-1",
        "run_id": "run-1",
        "worktree_path": str(root),
        "provider": {"id": provider, "model": "requested-model", "launch_config": {}},
        "attempts_path": str(root / "attempts.jsonl"),
        "_controller_attempt_attestation_key": b"test-controller-key" * 2,
        "_controller_attempt_content_hashes": [],
    }


def append_observed_attempt(
    root: Path,
    provider: str,
    transcript: Path,
    *,
    attempt_number: int = 1,
    offset: int = 0,
    exit_code: int = 0,
    parser=None,
) -> dict:
    controller._append_attempt(
        lane_at(root, provider),
        attempt_number=attempt_number,
        argv=[provider, "--resume", "s"],
        session_id="s",
        prompt_path=root / "prompt",
        paths={"transcript": transcript, "stderr": root / "stderr"},
        exit_code=exit_code,
        result_state="invalid" if exit_code else "valid",
        cleanup_proven=True,
        binding=parser or binding(provider),
        transcript_start_byte=offset,
        dispatch_binding={"decision_id": "decision-1"},
    )
    return read_jsonl(root / "attempts.jsonl")[-1]


class NativeAttemptReceipts(unittest.TestCase):
    def test_signed_attempt_requires_every_exact_dispatch_binding_field(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            worktree.mkdir()
            transcript = worktree / "transcript.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "turn.completed",
                    "turn_id": "turn-1",
                    "usage": {"input_tokens": 4},
                }) + "\n",
                encoding="utf-8",
            )
            lane = lane_at(worktree, "codex")
            lane["attempts_path"] = str(root / "controller.attempts.jsonl")
            dispatch = {
                field: f"exact-{field}"
                for field in memory_handoff.DISPATCH_BINDING_FIELDS
            }
            dispatch.update({"lane_id": "lane-1", "run_id": "run-1"})
            controller._append_attempt(
                lane,
                attempt_number=1,
                argv=["codex", "exec"],
                session_id="session-1",
                prompt_path=worktree / "prompt",
                paths={"transcript": transcript, "stderr": worktree / "stderr"},
                exit_code=0,
                result_state="valid",
                cleanup_proven=True,
                binding=binding("codex"),
                dispatch_binding=dispatch,
            )
            controller._publish_attempt_attestation(lane)
            evidence = {
                "lane_id": "lane-1",
                "run_id": "run-1",
                "objective_id": "objective-1",
                "decision_id": dispatch["decision_id"],
                "dispatch": {"observed_invocation": {
                    **dispatch,
                    "invocation_id": "controller:1:test",
                    "pid": 1,
                    "creation_time": "incarnation-1",
                }},
            }
            self.assertEqual(
                1,
                len(memory_handoff._validated_native_attempt_rows(
                    worktree_path=worktree,
                    attempts_path=lane["attempts_path"],
                    evidence=evidence,
                )),
            )
            transcript.unlink()
            self.assertEqual(
                1,
                len(memory_handoff._validated_native_attempt_rows(
                    worktree_path=worktree,
                    attempts_path=lane["attempts_path"],
                    evidence=evidence,
                    validate_transcripts=False,
                )),
            )
            with self.assertRaisesRegex(
                memory_handoff.MemoryHandoffError,
                "transcript is unavailable",
            ):
                memory_handoff._validated_native_attempt_rows(
                    worktree_path=worktree,
                    attempts_path=lane["attempts_path"],
                    evidence=evidence,
                )
            for field in memory_handoff.DISPATCH_BINDING_FIELDS:
                if field == "decision_id":
                    continue
                with self.subTest(field=field):
                    changed = json.loads(json.dumps(evidence))
                    changed["dispatch"]["observed_invocation"][field] = (
                        f"conflicting-{field}"
                    )
                    with self.assertRaisesRegex(
                        memory_handoff.MemoryHandoffError,
                        "conflicting dispatch binding",
                    ):
                        memory_handoff._validated_native_attempt_rows(
                            worktree_path=worktree,
                            attempts_path=lane["attempts_path"],
                            evidence=changed,
                            validate_transcripts=False,
                        )

    def test_untrusted_usage_fields_are_dropped_and_do_not_mark_observed(self):
        cases = (
            ({"latency_ms": 5, "access_token": "TOPSECRET"}, "incomplete", None),
            ({
                "input_tokens": 4,
                "latency_ms": 5,
                "access_token": "TOPSECRET",
                "cached_input_tokens": float("inf"),
                "reasoning_output_tokens": True,
            }, "observed", {"input_tokens": 4}),
        )
        for usage, state, expected in cases:
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                transcript = root / "transcript.jsonl"
                transcript.write_text(json.dumps({
                    "type": "turn.completed",
                    "model": {"access_token": "TOPSECRET"},
                    "turn_id": {"access_token": "TOPSECRET"},
                    "uuid": "x" * 300,
                    "usage": usage,
                }) + "\n", encoding="utf-8")
                row = append_observed_attempt(root, "codex", transcript)
                self.assertEqual(state, row["native_usage_state"])
                if expected is None:
                    self.assertEqual([], row["native_usage_observations"])
                else:
                    self.assertEqual(expected, row["native_usage_observations"][0]["usage"])
                    self.assertNotIn("uuid", row["native_usage_observations"][0])
                    self.assertNotIn("turn_id", row["native_usage_observations"][0])
                self.assertNotIn("TOPSECRET", (root / "attempts.jsonl").read_text())
                self.assertNotIn("latency_ms", (root / "attempts.jsonl").read_text())

    def test_claude_result_retains_independent_model_usage_and_cost(self):
        for generic_usage, keep_models, keep_cost in (
            (None, True, True),
            ({}, True, True),
            (None, True, False),
            (None, False, True),
        ):
            with self.subTest(generic_usage=generic_usage, keep_models=keep_models,
                              keep_cost=keep_cost), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                transcript = root / "transcript.jsonl"
                event = {
                    "type": "result",
                    "uuid": "terminal",
                }
                if keep_models:
                    event["modelUsage"] = {
                        "claude-native": {
                            "inputTokens": 6,
                            "cacheReadInputTokens": 3,
                            "costUSD": 0.006,
                            "latency_ms": 9,
                            "access_token": "TOPSECRET",
                        }
                    }
                if keep_cost:
                    event["total_cost_usd"] = 0.01
                if generic_usage is not None:
                    event["usage"] = generic_usage
                transcript.write_text(json.dumps(event) + "\n", encoding="utf-8")
                row = append_observed_attempt(root, "claude-code", transcript)
                self.assertEqual("observed", row["native_usage_state"])
                self.assertEqual(1, len(row["native_usage_observations"]))
                receipt = row["native_usage_observations"][0]
                self.assertEqual({}, receipt["usage"])
                if keep_models:
                    self.assertEqual(
                        {"claude-native": {
                            "inputTokens": 6,
                            "cacheReadInputTokens": 3,
                            "costUSD": 0.006,
                        }},
                        receipt["modelUsage"],
                    )
                else:
                    self.assertNotIn("modelUsage", receipt)
                if keep_cost:
                    self.assertEqual(0.01, receipt["total_cost_usd"])
                else:
                    self.assertNotIn("total_cost_usd", receipt)
                self.assertNotIn("TOPSECRET", (root / "attempts.jsonl").read_text())

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            transcript = root / "transcript.jsonl"
            transcript.write_text(json.dumps({
                "type": "result",
                "usage": {"latency_ms": 5},
                "total_cost_usd": float("inf"),
            }) + "\n", encoding="utf-8")
            row = append_observed_attempt(root, "claude-code", transcript)
            self.assertEqual("incomplete", row["native_usage_state"])
            self.assertEqual([], row["native_usage_observations"])

    def test_source_events_keep_native_fields_and_distinct_observations(self):
        codex_usage = {
            "input_tokens": 11,
            "cached_input_tokens": 4,
            "output_tokens": 5,
            "reasoning_output_tokens": 2,
        }
        claude_usage = {
            "input_tokens": 3,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 2,
            "output_tokens": 4,
        }
        cases = {
            "codex": [
                {"type": "turn.completed", "turn_id": turn, "usage": codex_usage}
                for turn in ("turn-a", "turn-a", "turn-b")
            ],
            "claude-code": [
                {
                    "type": "assistant",
                    "uuid": event,
                    "session_id": "s",
                    "message": {
                        "id": message,
                        "model": "claude-native",
                        "usage": claude_usage,
                    },
                }
                for event, message in (("event-a", "msg-a"), ("event-b", "msg-b"))
            ] + [{
                "type": "result",
                "uuid": "terminal",
                "session_id": "s",
                "usage": {"input_tokens": 6, "output_tokens": 8},
                "modelUsage": {"claude-native": {"inputTokens": 6}},
                "total_cost_usd": 0.01,
            }],
            "qwen-code": [{
                "type": "assistant",
                "uuid": "partial",
                "session_id": "s",
                "message": {
                    "id": "msg-a",
                    "model": "qwen-native",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 1,
                    },
                },
            }, {
                "type": "result",
                "uuid": "terminal",
                "session_id": "s",
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 2,
                },
            }],
        }
        for provider, events in cases.items():
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                transcript = root / "provider-transcript.jsonl"
                transcript.write_bytes(
                    ("prior run\n" + "\n".join(map(json.dumps, events)) + "\n").encode()
                )
                row = append_observed_attempt(
                    root, provider, transcript,
                    attempt_number=2, offset=len(b"prior run\n"), exit_code=1,
                )
                receipts = row["native_usage_observations"]
                expected_usage = [
                    event.get("usage", event.get("message", {}).get("usage"))
                    for event in events
                ]
                self.assertEqual(("lane-1", "run-1", 2),
                                 (row["lane_id"], row["run_id"], row["attempt"]))
                self.assertEqual({"decision_id": "decision-1"}, row["dispatch_binding"])
                self.assertEqual("observed", row["native_usage_state"])
                self.assertEqual(expected_usage, [item["usage"] for item in receipts])
                self.assertEqual(
                    ["cumulative"] * len(receipts) if provider == "codex"
                    else ["incremental"] * (len(receipts) - 1) + ["cumulative"],
                    [item["usage_mode"] for item in receipts],
                )
                self.assertEqual(
                    [True] * len(receipts) if provider == "codex"
                    else [False] * (len(receipts) - 1) + [True],
                    [item["usage_complete"] for item in receipts],
                )
                self.assertEqual(list(range(1, len(events) + 1)),
                                 [item["line_number"] for item in receipts])
                self.assertEqual([event.get("uuid") for event in events],
                                 [item.get("uuid") for item in receipts])
                self.assertNotIn("total_tokens", row)
                self.assertEqual(attempt_attestation.ATTEMPT_SCHEMA, row["schema"])
                self.assertEqual(content_hash(row), row["content_hash"])
                if provider == "codex":
                    self.assertEqual(["turn-a", "turn-a", "turn-b"],
                                     [item["turn_id"] for item in receipts])
                    self.assertEqual(2, receipts[0]["usage"]["reasoning_output_tokens"])
                else:
                    expected_ids = ["msg-a", "msg-b"] if provider == "claude-code" else ["msg-a"]
                    self.assertEqual(expected_ids,
                                     [item["message_id"] for item in receipts if "message_id" in item])
                    self.assertEqual("terminal", receipts[-1]["uuid"])
                    if provider == "claude-code":
                        self.assertEqual(
                            {"claude-native": {"inputTokens": 6}}, receipts[-1]["modelUsage"]
                        )
                        self.assertEqual(0.01, receipts[-1]["total_cost_usd"])

    def test_missing_and_empty_native_usage_are_incomplete(self):
        for event, expected_receipts in (
            ({"type": "turn.completed"}, []),
            ({"type": "turn.completed", "usage": {}}, []),
        ):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                transcript = root / "transcript.jsonl"
                transcript.write_bytes((json.dumps(event) + "\n").encode())
                row = append_observed_attempt(root, "codex", transcript)
                self.assertEqual("incomplete", row["native_usage_state"])
                self.assertEqual(expected_receipts,
                                 [item["usage"] for item in row["native_usage_observations"]])
                self.assertNotIn("usage", row)

    def test_parser_error_does_not_drop_failed_attempt_or_raw_usage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            transcript = root / "transcript.jsonl"
            transcript.write_bytes(b'{"type":"turn.failed","usage":{"input_tokens":4}}\n')

            def broken_parser(_line):
                raise ValueError("unexpected native field")

            row = append_observed_attempt(
                root, "codex", transcript, exit_code=1,
                parser=SimpleNamespace(parse_line=broken_parser),
            )
            self.assertEqual("invalid", row["result_state"])
            self.assertEqual(4, row["native_usage_observations"][0]["usage"]["input_tokens"])
            self.assertIn("provider parser failed", row["native_usage_capture_error"])

    def test_json_value_and_recursion_errors_still_append_incomplete_attempt(self):
        malformed_lines = (
            (b'{"type":"turn.completed","usage":{"input_tokens":' + b"9" * 5000 + b'}}\n', True),
            (b'{"type":"turn.completed","usage":{"input_tokens":'
             + b"[" * 5000 + b"0" + b"]" * 5000 + b'}}\n', False),
        )
        for line, malformed in malformed_lines:
            with self.subTest(length=len(line)), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                transcript = root / "transcript.jsonl"
                transcript.write_bytes(line)
                row = append_observed_attempt(root, "codex", transcript, exit_code=1)
                self.assertEqual("incomplete", row["native_usage_state"])
                self.assertEqual([], row["native_usage_observations"])
                if malformed:
                    self.assertIn("malformed native JSON", row["native_usage_capture_error"])
                else:
                    self.assertIsNone(row["native_usage_capture_error"])

    def test_self_hashed_outside_worktree_forgery_cannot_be_controller_sealed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            runtime = root / "runtime"
            worktree.mkdir()
            runtime.mkdir()
            transcript = worktree / "transcript.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "turn.completed", "turn_id": "turn-1",
                    "usage": {"input_tokens": 4},
                }) + "\n",
                encoding="utf-8",
            )
            lane = lane_at(worktree, "codex")
            lane["attempts_path"] = str(runtime / "controller.attempts.jsonl")
            controller._append_attempt(
                lane, attempt_number=1, argv=["codex", "exec"], session_id="s",
                prompt_path=worktree / "prompt",
                paths={"transcript": transcript, "stderr": worktree / "stderr"},
                exit_code=0, result_state="valid", cleanup_proven=True,
                binding=binding("codex"), dispatch_binding={"decision_id": "decision-1"},
            )
            records = read_jsonl(Path(lane["attempts_path"]))
            forged = dict(records[1])
            forged["native_usage_observations"] = [{
                "event_type": "turn.completed", "turn_id": "forged",
                "usage_mode": "cumulative", "usage_complete": True,
                "usage": {"input_tokens": 999},
            }]
            # This deliberately has a valid content hash.  It would have
            # passed the former outside-worktree+self-hash check, but it lacks
            # the controller-memory-only HMAC and expected-row identity.
            forged["content_hash"] = content_hash(forged)
            Path(lane["attempts_path"]).write_text(
                "\n".join(
                    json.dumps(item, sort_keys=True)
                    for item in [records[0], forged]
                ) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                controller.ControllerError, "changed before sealing"
            ):
                controller._publish_attempt_attestation(lane)
            self.assertFalse(
                attempt_attestation.attestation_path(
                    lane["attempts_path"], lane["run_id"]
                ).exists()
            )

    def test_late_tail_after_exit_is_captured_for_started_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / ".agent-workspace"
            workspace.mkdir()
            prompt = workspace / "prompt.md"
            prompt.write_text("work", encoding="utf-8")
            transcript = workspace / "provider-transcript.jsonl"
            transcript.write_bytes(b"stale earlier invocation\n")
            lane = {
                **lane_at(root, "codex"),
                "transcript_path": str(transcript),
                "stderr_path": str(workspace / "stderr"),
                "last_message_path": str(workspace / "last-message"),
                "controller_events_path": str(workspace / "events"),
                "controller_status_path": str(workspace / "status"),
            }
            adapter = binding("codex")
            child = MagicMock(pid=123, returncode=0)
            child.take_job_handle.return_value = 45
            boundary = MagicMock(
                root_pid=123, root_creation_time="created",
                process_group_id=123, session_id="boundary",
            )
            boundary.record.return_value = {"root": {"pid": 123}}

            def spawn(argv, *, cwd, stdin, stdout, stderr):
                stdout.write('{"type":["turn.completed"],"usage":{"input_tokens":5}}\n')
                stdout.write('{"type":"thread.started","thread_id":"native-session"}\n')
                stdout.flush()
                return child

            def poll():
                with transcript.open("ab") as handle:
                    handle.write(
                        b'{"type":"turn.completed","usage":'
                        b'{"input_tokens":9,"cached_input_tokens":4,"output_tokens":3}}\n'
                    )
                return 0

            child.poll.side_effect = poll
            invocation = {
                "provider": {
                    "model": "m",
                    "launch_config": {
                        "reasoning_effort": "high", "service_tier": "normal"
                    },
                }
            }
            with (
                patch.object(adapter, "build_argv", return_value=["codex", "exec"]),
                patch.object(controller.processes, "spawn_provider", side_effect=spawn),
                patch.object(controller.processes.ProcessBoundary, "for_process", return_value=boundary),
                patch.object(controller, "update_lane"),
            ):
                execution = controller._run_provider(
                    root, "epoch", lane, invocation, adapter, prompt
                )
            self.assertEqual("native-session", execution.session_id)
            controller._append_attempt(
                lane,
                attempt_number=1,
                argv=list(execution.argv),
                session_id=execution.session_id,
                prompt_path=prompt,
                paths=controller._attempt_paths(lane, 1),
                exit_code=0,
                result_state="invalid",
                cleanup_proven=True,
                binding=adapter,
                transcript_start_byte=execution.transcript_start_byte,
            )
            row = read_jsonl(root / "attempts.jsonl")[-1]
            self.assertEqual(9, row["native_usage_observations"][0]["usage"]["input_tokens"])
            self.assertEqual(3, row["native_usage_observations"][0]["line_number"])
            self.assertIn("malformed", row["native_usage_capture_error"])
            self.assertEqual(len(b"stale earlier invocation\n"), execution.transcript_start_byte)
            self.assertEqual(execution.transcript_start_byte, row["transcript_start_byte"])
            self.assertEqual(transcript.stat().st_size, row["transcript_end_byte"])

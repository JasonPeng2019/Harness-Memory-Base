"""Regression coverage for caller-selected Ollama transport on Codex lanes."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_harness.provider_adapters.codex import launcher_binding as binding


def _control_plane_digest(control_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in control_root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(control_root).as_posix(),
    ):
        relative = path.relative_to(control_root).as_posix().encode("utf-8")
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


class CodexOllamaLaunchTests(unittest.TestCase):
    def argv(self, **overrides):
        options = dict(
            model="requested-model:cloud",
            launch_config={"reasoning_effort": "max", "service_tier": "normal", "launcher": "ollama"},
            worktree=str(Path("worker with spaces").resolve()),
            prompt_path="prompt.txt",
        )
        options.update(overrides)
        return binding.build_argv(**options)

    def test_ollama_owns_model_and_preserves_codex_flags(self):
        argv = self.argv()
        self.assertEqual(["ollama", "launch", "codex", "--model", "requested-model:cloud", "--yes", "--"], argv[:7])
        forwarded = argv[7:]
        self.assertEqual(["exec", "--ignore-user-config"], forwarded[:2])
        self.assertNotIn("-m", forwarded)
        self.assertNotIn("--model", forwarded)
        self.assertIn('model_reasoning_effort="max"', forwarded)
        self.assertIn('service_tier="normal"', forwarded)
        self.assertIn("features.hooks=true", forwarded)
        self.assertIn("--json", forwarded)
        self.assertEqual("-", forwarded[-1])

    def test_resume_keeps_exact_session_and_transport(self):
        argv = self.argv(resume=True, session_id="native-session")
        self.assertEqual(
            ["exec", "--ignore-user-config", "resume", "native-session"],
            argv[7:11],
        )
        self.assertNotIn("--cd", argv)
        with self.assertRaisesRegex(ValueError, "session ID"):
            self.argv(resume=True)

    def test_resume_rejects_worker_tampering_with_isolation_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir) / "worker"
            config_path = worktree / ".codex" / "config.toml"
            config_path.parent.mkdir(parents=True)
            codex_home = Path(temp_dir) / "controller-codex-home"
            filesystem = {
                str(codex_home.resolve()): "deny",
                ":workspace_roots": {
                    ".": "write",
                    ".codex": "read",
                    ".agent-workspace": "read",
                    ".agent-workspace/runtime/**": "write",
                },
            }
            config_path.write_text(
                "[permissions.worker-isolated]\n"
                'extends = ":workspace"\n'
                "[permissions.worker-isolated.filesystem]\n"
                f'{json.dumps(str(codex_home.resolve()))} = "deny"\n'
                '[permissions.worker-isolated.filesystem.":workspace_roots"]\n'
                '"." = "write"\n'
                '".codex" = "read"\n'
                '".agent-workspace" = "read"\n'
                '".agent-workspace/runtime/**" = "write"\n',
                encoding="utf-8",
            )
            expected_digest = hashlib.sha256(
                json.dumps(
                    filesystem,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                control_digest = _control_plane_digest(config_path.parent)
                self.argv(
                    worktree=str(worktree),
                    expected_isolation_sha256=expected_digest,
                    expected_control_plane_sha256=control_digest,
                )
                config_path.write_text(
                    config_path.read_text(encoding="utf-8")
                    + f'{json.dumps(str(Path(temp_dir).anchor))} = "write"\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "isolation profile changed"):
                    self.argv(
                        worktree=str(worktree),
                        resume=True,
                        session_id="native-session",
                        expected_isolation_sha256=expected_digest,
                        expected_control_plane_sha256=control_digest,
                    )

    def test_resume_rejects_worker_tampering_with_hook_control_plane(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir) / "worker"
            control_root = worktree / ".codex"
            hook_path = control_root / "hooks" / "trusted.py"
            hook_path.parent.mkdir(parents=True)
            codex_home = Path(temp_dir) / "controller-codex-home"
            filesystem = {
                str(codex_home.resolve()): "deny",
                ":workspace_roots": {
                    ".": "write",
                    ".codex": "read",
                    ".agent-workspace": "read",
                    ".agent-workspace/runtime/**": "write",
                },
            }
            (control_root / "config.toml").write_text(
                "[permissions.worker-isolated]\n"
                'extends = ":workspace"\n'
                "[permissions.worker-isolated.filesystem]\n"
                f'{json.dumps(str(codex_home.resolve()))} = "deny"\n'
                '[permissions.worker-isolated.filesystem.":workspace_roots"]\n'
                '"." = "write"\n'
                '".codex" = "read"\n'
                '".agent-workspace" = "read"\n'
                '".agent-workspace/runtime/**" = "write"\n',
                encoding="utf-8",
            )
            hook_path.write_text("# trusted hook\n", encoding="utf-8")
            filesystem_digest = hashlib.sha256(
                json.dumps(
                    filesystem,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            control_digest = _control_plane_digest(control_root)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                self.argv(
                    worktree=str(worktree),
                    expected_isolation_sha256=filesystem_digest,
                    expected_control_plane_sha256=control_digest,
                )
                hook_path.write_text("raise SystemExit('tampered')\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "control plane changed"):
                    self.argv(
                        worktree=str(worktree),
                        resume=True,
                        session_id="native-session",
                        expected_isolation_sha256=filesystem_digest,
                        expected_control_plane_sha256=control_digest,
                    )

    def test_direct_codex_uses_workspace_sandbox(self):
        argv = self.argv(launch_config={"reasoning_effort": "high", "service_tier": "normal"})
        worktree = str(Path("worker with spaces").resolve())
        self.assertEqual([
            binding._direct_codex_executable(), "exec", "--ignore-user-config",
            "-c", 'default_permissions="worker-isolated"',
            *(["-c", 'windows.sandbox="elevated"'] if os.name == "nt" else []),
            "--skip-git-repo-check", "-c", 'approval_policy="never"',
            "-m", "requested-model:cloud", "-c", 'model_reasoning_effort="high"',
            "-c", 'service_tier="normal"', "--dangerously-bypass-hook-trust",
            "-c", "features.hooks=true", "-c",
            f'projects.{json.dumps(worktree)}.trust_level="trusted"',
            "--json", "--output-last-message",
            str(Path(worktree) / ".agent-workspace" / "last-message.txt"),
            "--cd", worktree, "-",
        ], argv)
        self.assertEqual(argv, self.argv(launch_config={
            "reasoning_effort": "high", "service_tier": "normal", "launcher": "codex",
        }))
        if os.name == "nt":
            self.assertEqual(Path(argv[0]).name.lower(), "codex.exe")
            self.assertTrue(Path(argv[0]).is_file())

    def test_invalid_transport_is_rejected(self):
        for launcher in ("", "other", None):
            with self.subTest(launcher=launcher), self.assertRaises(ValueError):
                self.argv(launch_config={"reasoning_effort": "max", "service_tier": "normal", "launcher": launcher})

    def test_catalog_and_registered_binding_are_identical(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual((root / "adapters/codex/harness/launcher_binding.py").read_bytes(), Path(binding.__file__).read_bytes())

    def test_codex_event_and_failure_parsing_still_apply(self):
        self.assertEqual({"session_id": "native-session"}, binding.parse_line(json.dumps({"type": "thread.started", "thread_id": "native-session"})))
        self.assertTrue(binding.parse_line('{"type":"turn.failed"}')["non_retryable_failure"])
        self.assertIsNone(binding.parse_line("Launching Codex with Ollama"))


if __name__ == "__main__":
    unittest.main()

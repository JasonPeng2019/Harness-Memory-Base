"""Direct v2 launcher binding for Codex."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import shutil
import tomllib
from pathlib import Path
from typing import Any

PROVIDER_ID = "codex"
ADAPTER_VERSION = "codex-v2"

_LAUNCH_CONFIG_KEYS = frozenset({"reasoning_effort", "service_tier"})
_OPTIONAL_LAUNCH_CONFIG_KEYS = frozenset({"launcher"})


def _direct_codex_executable() -> str:
    """Use the packaged native executable for a Windows suspended launch."""
    if os.name != "nt":
        return "codex"
    shim = shutil.which("codex.cmd")
    if shim:
        arch = platform.machine().lower()
        suffix = "arm64" if arch in ("arm64", "aarch64") else "x64"
        triple = (
            "aarch64-pc-windows-msvc"
            if suffix == "arm64"
            else "x86_64-pc-windows-msvc"
        )
        package = Path(shim).parent / "node_modules" / "@openai" / "codex"
        candidates = (
            package
            / "node_modules"
            / "@openai"
            / f"codex-win32-{suffix}"
            / "vendor"
            / triple
            / "bin"
            / "codex.exe",
            package / "vendor" / triple / "bin" / "codex.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
    standalone = shutil.which("codex.exe")
    if standalone:
        return standalone
    raise FileNotFoundError(
        "native codex.exe not found for suspended Windows worker launch"
    )


def _toml_inline(value: str | dict[str, Any]) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{json.dumps(str(key))} = {_toml_inline(child)}"
            for key, child in value.items()
        ) + "}"
    raise ValueError("unsupported Codex worker permission value")


def _isolation_digest(filesystem: dict[str, Any]) -> str:
    canonical = json.dumps(
        filesystem,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _control_plane_snapshot(control_root: Path) -> tuple[str, dict[str, bytes]]:
    digest = hashlib.sha256()
    files: dict[str, bytes] = {}
    for path in sorted(
        control_root.rglob("*"),
        key=lambda item: item.relative_to(control_root).as_posix(),
    ):
        if path.is_symlink():
            raise ValueError(f"Codex worker control plane contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(control_root).as_posix()
        relative_bytes = relative.encode("utf-8")
        contents = path.read_bytes()
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
        files[relative] = contents
    return digest.hexdigest(), files


def _require_sha256(value: str | None, *, description: str, path: Path) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"trusted Codex worker {description} missing or invalid: {path}")
    return value


def _isolation_overrides(
    worktree: str,
    expected_isolation_sha256: str | None = None,
    expected_control_plane_sha256: str | None = None,
) -> list[str]:
    config_path = Path(worktree) / ".codex" / "config.toml"
    if not config_path.is_file():
        if Path(worktree).exists():
            raise ValueError(f"Codex worker isolation profile missing: {config_path}")
        return []
    expected_control_digest = _require_sha256(
        expected_control_plane_sha256,
        description="control-plane digest",
        path=config_path,
    )
    actual_control_digest, control_files = _control_plane_snapshot(config_path.parent)
    try:
        config = tomllib.loads(control_files["config.toml"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Codex worker isolation profile invalid: {config_path}") from exc
    profile = config.get("permissions", {}).get("worker-isolated")
    if not isinstance(profile, dict) or profile.get("extends") != ":workspace":
        raise ValueError(f"Codex worker isolation profile invalid: {config_path}")
    filesystem = profile.get("filesystem")
    if not isinstance(filesystem, dict) or not filesystem:
        raise ValueError(f"Codex worker filesystem isolation missing: {config_path}")
    expected_isolation_digest = _require_sha256(
        expected_isolation_sha256,
        description="isolation digest",
        path=config_path,
    )
    actual_digest = _isolation_digest(filesystem)
    if not hmac.compare_digest(actual_digest, expected_isolation_digest):
        raise ValueError(f"Codex worker isolation profile changed: {config_path}")
    workspace_rules = filesystem.get(":workspace_roots")
    if (
        not isinstance(workspace_rules, dict)
        or workspace_rules.get(".codex") != "read"
        or workspace_rules.get(".agent-workspace") != "read"
        or workspace_rules.get(".agent-workspace/runtime/**") != "write"
    ):
        raise ValueError(f"Codex worker control plane is not read-only: {config_path}")
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    if filesystem.get(str(codex_home)) != "deny":
        raise ValueError(f"Codex worker auth directory is not denied: {config_path}")
    if not hmac.compare_digest(actual_control_digest, expected_control_digest):
        raise ValueError(f"Codex worker control plane changed: {config_path.parent}")
    return [
        'permissions.worker-isolated.extends=":workspace"',
        "permissions.worker-isolated.filesystem=" + _toml_inline(filesystem),
    ]


def validate_launch_config(*, model: str, launch_config: dict[str, Any]) -> dict[str, str]:
    """Validate every Codex model preference before a provider can start."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Codex model must be configured")
    if not isinstance(launch_config, dict):
        raise ValueError("Codex launch_config must be an object")
    missing = sorted(_LAUNCH_CONFIG_KEYS - set(launch_config))
    if missing:
        raise ValueError(f"Codex launch_config is missing: {', '.join(missing)}")
    unsupported = sorted(set(launch_config) - _LAUNCH_CONFIG_KEYS - _OPTIONAL_LAUNCH_CONFIG_KEYS)
    if unsupported:
        raise ValueError(f"Codex launch_config has unsupported options: {', '.join(unsupported)}")
    normalized: dict[str, str] = {}
    for key in sorted(_LAUNCH_CONFIG_KEYS):
        value = launch_config[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Codex launch_config {key} must be a non-empty string")
        normalized[key] = value.strip()
    if "launcher" in launch_config:
        if launch_config["launcher"] not in ("codex", "ollama"):
            raise ValueError("Codex launch_config launcher must be codex or ollama")
        normalized["launcher"] = launch_config["launcher"]
    return normalized


def build_argv(
    *,
    model: str,
    launch_config: dict[str, Any],
    worktree: str,
    prompt_path: str,
    session_id: str | None = None,
    resume: bool = False,
    expected_isolation_sha256: str | None = None,
    expected_control_plane_sha256: str | None = None,
) -> list[str]:
    """Build the provider-owned, stdin-prompted Codex launch vector."""
    del prompt_path
    configured = validate_launch_config(model=model, launch_config=launch_config)
    isolation_overrides = _isolation_overrides(
        worktree,
        expected_isolation_sha256,
        expected_control_plane_sha256,
    )
    argv = ["codex", "exec", "--ignore-user-config"]
    if resume:
        if not session_id:
            raise ValueError("Codex resume requires a session ID")
        argv.extend(["resume", session_id])
    argv.extend(
        [
            "-c",
            'default_permissions="worker-isolated"',
            *[part for override in isolation_overrides for part in ("-c", override)],
            *(["-c", 'windows.sandbox="elevated"'] if os.name == "nt" else []),
            "--skip-git-repo-check",
            "-c",
            'approval_policy="never"',
            "-m",
            model,
            "-c",
            f"model_reasoning_effort={json.dumps(configured['reasoning_effort'])}",
            "-c",
            f"service_tier={json.dumps(configured['service_tier'])}",
            "--dangerously-bypass-hook-trust",
            "-c",
            "features.hooks=true",
            "-c",
            f'projects.{json.dumps(worktree)}.trust_level="trusted"',
            "--json",
            "--output-last-message",
            str(Path(worktree) / ".agent-workspace" / "last-message.txt"),
        ]
    )
    if not resume:
        argv.extend(["--cd", worktree])
    argv.append("-")
    if configured.get("launcher") == "ollama":
        # Ollama owns the model/profile arguments and forwards stdin unchanged.
        # Passing Codex's -m as an extra argument is rejected by ollama launch.
        model_index = argv.index("-m")
        del argv[model_index:model_index + 2]
        return ["ollama", "launch", "codex", "--model", model, "--yes", "--", *argv[1:]]
    argv[0] = _direct_codex_executable()
    return argv


def parse_line(line: str) -> dict[str, Any] | None:
    """Parse one Codex JSON transcript line into controller facts."""
    try:
        value = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    raw_type = value.get("type")
    if raw_type not in {"thread.started", "turn.completed", "turn.failed", "turn.cancelled"}:
        return None
    session_id = value.get("thread_id") or value.get("threadId")
    parsed: dict[str, Any] = {}
    if isinstance(session_id, str) and session_id:
        parsed["session_id"] = session_id
    if raw_type != "thread.started":
        parsed["message"] = raw_type
    if raw_type in {"turn.failed", "turn.cancelled"}:
        parsed["non_retryable_failure"] = True
    return parsed

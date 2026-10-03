#!/usr/bin/env python3
"""Install MA-Harness for one existing personal Git project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SOURCE_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCHEMA = "ma-harness-install-receipt/v1"
OWNERSHIP_SCHEMA = "ma-harness-install-ownership/v1"
JOURNAL_SCHEMA = "ma-harness-setup-journal/v1"
STATE_SCHEMA = "ma-harness-install-state/v1"
OPERATIONAL_PATHS = (
    Path("adapters"),
    Path("super-cache"),
    Path("orchestrator_harness/provider_adapters"),
)
IGNORED_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "runtime", "local-config", "build", "dist",
}


class InstallError(RuntimeError):
    """The install cannot continue without risking unrelated bytes."""


@dataclass(frozen=True)
class Options:
    project_root: Path
    install_root: Path
    python: str
    managed_coordination: str
    application: str | None
    namespace: str | None
    project_id_override: str | None
    owner: str | None
    memory: bool
    setup: bool
    build_tool_paths: tuple[Path, ...]
    relink_project: bool = False


def new_project_identity() -> str:
    return secrets.token_hex(16)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:40] or "project"


def default_scope(project_name: str, project_identity: str) -> dict[str, str]:
    if re.fullmatch(r"[0-9a-f]{32}", project_identity) is None:
        raise InstallError("persisted project identity must be 128-bit lowercase hexadecimal")
    scoped = f"{_slug(project_name)}-{project_identity}"
    return {
        "application": "ma-harness",
        "namespace": scoped,
        "project": scoped,
        "owner": "ROOT",
    }


def _identity(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _inside(child: Path, parent: Path) -> bool:
    child_id = _identity(child)
    parent_id = _identity(parent)
    return child_id == parent_id or child_id.startswith(parent_id + os.sep)


def _redirected(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_probe is not None and junction_probe())


def validate_layout(*, source_root: Path, project_root: Path, install_root: Path) -> None:
    for label, path in (("source", source_root), ("project", project_root)):
        if not path.is_absolute() or not path.is_dir():
            raise InstallError(f"{label} root must be an existing absolute directory: {path}")
        if _redirected(path):
            raise InstallError(f"{label} root must not be a link or junction: {path}")
    if not install_root.is_absolute():
        raise InstallError("install root must be absolute")
    if _redirected(install_root):
        raise InstallError(f"install root must not be a link or junction: {install_root}")
    roots = (source_root, project_root, install_root)
    for index, left in enumerate(roots):
        for right in roots[index + 1:]:
            if _inside(left, right) or _inside(right, left):
                raise InstallError(
                    "source, project, and install roots must be disjoint: "
                    f"{left} and {right}"
                )


def _run(
    argv: Sequence[str], *, cwd: Path, environment: Mapping[str, str] | None = None,
    timeout: float = 240,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(argv), cwd=cwd, env=None if environment is None else dict(environment),
        text=True, capture_output=True, timeout=timeout,
    )
    if completed.returncode != 0:
        summary = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise InstallError(f"command failed ({completed.returncode}): {argv[0]}: {summary}")
    return completed


def _git_project(path: Path) -> tuple[Path, tuple[str, ...], Path]:
    top = _run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], cwd=path).stdout.strip()
    project = Path(top).resolve()
    roots = _run(
        ["git", "-C", str(project), "rev-list", "--max-parents=0", "HEAD"], cwd=project,
    ).stdout.splitlines()
    if not roots:
        raise InstallError("project Git repository must contain at least one commit")
    common_raw = _run(
        ["git", "-C", str(project), "rev-parse", "--git-common-dir"], cwd=project,
    ).stdout.strip()
    common = Path(common_raw)
    if not common.is_absolute():
        common = project / common
    return project, tuple(sorted(set(roots))), common.resolve()


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(dict(document), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, schema: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read owned record {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise InstallError(f"owned record has the wrong schema: {path}")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_files(root: Path) -> list[Path]:
    selected: list[Path] = []
    roots = (
        Path("pyproject.toml"), Path("src"), Path("harness/pyproject.toml"),
        Path("harness/orchestrator_harness"), Path("harness/harness_common"),
        Path("harness/harness_watcher_implementation"), Path("harness/adapters"),
        Path("harness/super-cache"), Path("scripts/install_product.py"),
        Path("scripts/qualification_preflight.py"),
        Path("scripts/verification_receipt.py"),
    )
    for relative in roots:
        path = root / relative
        candidates = [path] if path.is_file() else path.rglob("*") if path.is_dir() else []
        for candidate in candidates:
            rel = candidate.relative_to(root)
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and not any(part in IGNORED_PARTS or part.endswith(".egg-info") for part in rel.parts)
                and not ("tests" in rel.parts)
                and candidate.suffix not in {".pyc", ".pyo"}
            ):
                selected.append(rel)
    return sorted(set(selected), key=lambda item: item.as_posix())


def _candidate_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in _source_files(root):
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or _redirected(source):
        raise InstallError(f"operational source is not a plain directory: {source}")
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if any(part in IGNORED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        target = destination / relative
        if _redirected(path):
            raise InstallError(f"operational source contains a link or junction: {path}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _write_configs(
    harness: Path, *, options: Options, project_identity: str, store_root: Path,
) -> dict[str, str]:
    _atomic_json(harness / "harness-config.json", {
        "root_workspace": str(options.project_root),
        "managed_coordination": options.managed_coordination,
    })
    _atomic_json(harness / "resource-manifest.json", {
        "schema": "resource-manifest/v1", "resources": [],
    })
    scope = default_scope(options.project_root.name, project_identity)
    for field, value in (
        ("application", options.application),
        ("namespace", options.namespace),
        ("project", options.project_id_override),
        ("owner", options.owner),
    ):
        if value is not None:
            if not value.strip() or value != value.strip() or any(ord(char) < 32 for char in value):
                raise InstallError(f"{field} must be a canonical nonempty string")
            scope[field] = value
    if options.memory:
        _atomic_json(harness / "memory-product-config.json", {
            "schema": "memory-product-config/v1",
            **scope,
            "store_root": str(store_root),
            "policy_generation": f"installer-{project_identity}",
            "known_secret_files": [],
            "services": {
                "local_experience": {"enabled": True},
                "local_procedures": {"enabled": True},
                "everos": {"enabled": False, "credential_env": None},
                "atlas": {
                    "enabled": False, "credential_env": None,
                    "database": None, "collection": None, "index": None,
                },
            },
        })
    return scope


def _owned_entries(harness: Path) -> tuple[dict[str, str], list[str]]:
    redirected = next((path for path in harness.rglob("*") if _redirected(path)), None)
    if redirected is not None:
        raise InstallError(f"installer-owned harness contains a link or junction: {redirected}")
    files = {
        path.relative_to(harness).as_posix(): _sha(path)
        for path in sorted(harness.rglob("*"))
        if path.is_file()
    }
    directories = [
        path.relative_to(harness).as_posix()
        for path in sorted(harness.rglob("*"))
        if path.is_dir()
    ]
    return files, directories


def _verify_owned(install_root: Path, manifest: Mapping[str, Any]) -> None:
    raw = manifest.get("files")
    if not isinstance(raw, dict):
        raise InstallError("ownership manifest files must be an object")
    raw_directories = manifest.get("directories")
    if not isinstance(raw_directories, list) or not all(
        isinstance(item, str) for item in raw_directories
    ):
        raise InstallError("ownership manifest directories must be a string list")
    harness = install_root / "harness"
    if not harness.is_dir() or _redirected(harness):
        raise InstallError(f"installer-owned harness is missing or redirected: {harness}")
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for path in harness.rglob("*"):
        relative = path.relative_to(harness).as_posix()
        if _redirected(path):
            raise InstallError(f"unowned link or junction exists in installer-owned harness: {path}")
        if path.is_file():
            actual_files.add(relative)
        elif path.is_dir():
            actual_directories.add(relative)
        else:
            raise InstallError(f"unowned special entry exists in installer-owned harness: {path}")
    expected_files = set(raw)
    expected_directories = set(raw_directories)
    extra = sorted(actual_files - expected_files | actual_directories - expected_directories)
    missing = sorted(expected_files - actual_files | expected_directories - actual_directories)
    if extra:
        raise InstallError(f"unowned entry exists in installer-owned harness: {harness / extra[0]}")
    if missing:
        raise InstallError(f"installer-owned entry is missing: {harness / missing[0]}")
    for relative, expected in raw.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise InstallError("ownership manifest contains a malformed file entry")
        path = harness / relative
        if not path.is_file() or _redirected(path) or _sha(path) != expected:
            raise InstallError(f"installer-owned file is missing or drifted: {path}")


UPDATE_BACKUP_NAME = ".update-backup"


def _copy_update_backup(install_root: Path) -> Path:
    """Snapshot the live owned install before mutating its venv or harness."""

    backup = install_root / UPDATE_BACKUP_NAME
    if backup.exists():
        raise InstallError(f"unresolved update backup requires inspection: {backup}")
    staging = install_root.parent / (
        f".{install_root.name}.update-backup-stage-{uuid.uuid4().hex}"
    )
    staging.mkdir()
    try:
        for name in (
            "venv", "harness", "install-receipt.json", "ownership-manifest.json",
            "install-state.json", "setup-journal.json",
        ):
            source = install_root / name
            if source.is_dir():
                shutil.copytree(source, staging / name, symlinks=True)
            elif source.is_file():
                shutil.copy2(source, staging / name)
        if not (staging / "install-receipt.json").is_file() or not (
            staging / "ownership-manifest.json"
        ).is_file():
            raise InstallError("cannot create a complete update rollback snapshot")
        os.replace(staging, backup)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return backup


def _restore_update_backup(install_root: Path, backup: Path) -> None:
    """Restore the pre-update venv, harness, and authoritative records."""

    for name in (
        "venv", "harness", "install-receipt.json", "ownership-manifest.json",
        "install-state.json", "setup-journal.json",
    ):
        current = install_root / name
        if current.is_dir():
            shutil.rmtree(current)
        elif current.exists() or current.is_symlink():
            current.unlink()
        saved = backup / name
        if saved.is_dir():
            os.replace(saved, current)
        elif saved.is_file():
            os.replace(saved, current)
    shutil.rmtree(backup)


def _recover_interrupted_update(install_root: Path) -> None:
    """Recover only a provably pre-setup interrupted update."""

    backup = install_root / UPDATE_BACKUP_NAME
    if not backup.is_dir():
        return
    state_path = install_root / "install-state.json"
    try:
        state = _read_json(state_path, STATE_SCHEMA)
    except InstallError:
        raise InstallError(
            f"interrupted update cannot be classified; inspect {backup} manually"
        )
    if state.get("state") == "SETUP_CHECKED":
        journal_path = install_root / "setup-journal.json"
        journal_error: InstallError | None = None
        try:
            journal = _read_json(journal_path, JOURNAL_SCHEMA)
            if journal.get("state") != "CHECKED":
                raise InstallError(
                    f"setup journal is inconsistent with SETUP_CHECKED: {journal_path}"
                )
        except InstallError as exc:
            journal_error = exc
        else:
            journal.update({
                "state": "PARTIAL_SETUP",
                "summary": "installation was interrupted after setup became reachable",
                "outcome_known": False,
                "manual_repair_required": True,
                "rollback_claimed": False,
            })
            _atomic_json(journal_path, journal)
        _atomic_json(state_path, {
            "schema": STATE_SCHEMA,
            "state": "PARTIAL_SETUP",
            "candidate_digest": state.get("candidate_digest"),
            "project_identity": state.get("project_identity"),
        })
        detail = (
            f"; the setup journal is also invalid: {journal_error}"
            if journal_error is not None else ""
        )
        raise InstallError(
            "interrupted update may have reached setup; setup outcome is unknown and "
            f"manual inspection of {backup} and {journal_path} is required{detail}"
        )
    if state.get("state") not in {"UPDATING", "PROMOTING"}:
        raise InstallError(
            f"update may have reached setup; inspect {backup} and setup-journal.json manually"
        )
    _restore_update_backup(install_root, backup)


def _venv_python(root: Path) -> Path:
    return root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_console(root: Path) -> Path:
    return root / "venv" / ("Scripts/orchestrator-harness.exe" if os.name == "nt" else "bin/orchestrator-harness")


def _canonical_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InstallError(f"install receipt {field} must be a string list")
    if value != sorted(set(value)):
        raise InstallError(f"install receipt {field} must be sorted and duplicate-free")
    if any(not Path(item).is_absolute() for item in value):
        raise InstallError(f"install receipt {field} must contain absolute paths")
    return value


def _validate_existing_receipt(
    install_root: Path,
    receipt: Mapping[str, Any],
    ownership: Mapping[str, Any],
    *,
    requested: Mapping[str, Any],
) -> None:
    """Cross-check the reusable receipt against every owned local record."""

    receipt_fields = {
        "schema", "candidate_digest", "project_identity", "project_root",
        "git_common_dir", "git_root_commits", "install_root", "scope",
        "requested", "package_versions", "build_tool_source", "setup_code",
        "configuration_sha256", "setup_mutations", "created_unix_seconds",
    }
    if set(receipt) != receipt_fields:
        raise InstallError("install receipt is not a closed v1 record")
    if set(ownership) != {
        "schema", "project_identity", "candidate_digest", "files", "directories",
    }:
        raise InstallError("ownership manifest is not a closed v1 record")
    candidate_digest = receipt.get("candidate_digest")
    project_identity = receipt.get("project_identity")
    if not isinstance(candidate_digest, str) or re.fullmatch(r"[0-9a-f]{64}", candidate_digest) is None:
        raise InstallError("install receipt candidate digest is malformed")
    if not isinstance(project_identity, str) or re.fullmatch(r"[0-9a-f]{32}", project_identity) is None:
        raise InstallError("install receipt project identity is malformed")
    if ownership.get("candidate_digest") != candidate_digest:
        raise InstallError("ownership manifest candidate digest disagrees with the receipt")
    if ownership.get("project_identity") != project_identity:
        raise InstallError("ownership manifest project identity disagrees with the receipt")
    if receipt.get("install_root") != str(install_root):
        raise InstallError("install receipt is bound to a different install root")
    if receipt.get("requested") != dict(requested):
        raise InstallError(
            "installer options differ from the owned installation; use a new install root"
        )
    scope = receipt.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"application", "namespace", "project", "owner"}:
        raise InstallError("install receipt scope is malformed")
    if any(not isinstance(value, str) or not value for value in scope.values()):
        raise InstallError("install receipt scope contains an invalid value")
    if not isinstance(receipt.get("git_root_commits"), list) or not all(
        isinstance(item, str) and item for item in receipt["git_root_commits"]
    ):
        raise InstallError("install receipt Git roots are malformed")
    if not isinstance(receipt.get("git_common_dir"), str) or not Path(
        receipt["git_common_dir"]
    ).is_absolute():
        raise InstallError("install receipt Git common directory is malformed")
    if not isinstance(receipt.get("project_root"), str) or not Path(
        receipt["project_root"]
    ).is_absolute():
        raise InstallError("install receipt project root is malformed")
    if receipt.get("build_tool_source") not in {"operator_paths", "configured_package_index"}:
        raise InstallError("install receipt build-tool source is malformed")
    created = receipt.get("created_unix_seconds")
    if not isinstance(created, int) or isinstance(created, bool) or created < 0:
        raise InstallError("install receipt creation time is malformed")

    versions = receipt.get("package_versions")
    expected_packages = {
        "memory-harness", "portable-orchestrator-harness", "setuptools", "wheel",
    }
    if not isinstance(versions, dict) or set(versions) != expected_packages or not all(
        isinstance(value, str) and value for value in versions.values()
    ):
        raise InstallError("install receipt package versions are malformed")

    memory_enabled = requested.get("memory_enabled") is True
    config_names = {
        "harness-config.json", "resource-manifest.json",
        *(("memory-product-config.json",) if memory_enabled else ()),
    }
    hashes = receipt.get("configuration_sha256")
    if not isinstance(hashes, dict) or set(hashes) != config_names:
        raise InstallError("install receipt configuration hashes are malformed")
    harness = install_root / "harness"
    for name in sorted(config_names):
        path = harness / name
        expected = hashes.get(name)
        if (
            not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or not path.is_file()
            or _redirected(path)
            or _sha(path) != expected
        ):
            raise InstallError(f"install receipt configuration hash mismatch: {path}")
    if not memory_enabled and (harness / "memory-product-config.json").exists():
        raise InstallError("memory-disabled installation unexpectedly contains memory configuration")

    mutations = receipt.get("setup_mutations")
    if not isinstance(mutations, dict) or set(mutations) != {"planned_paths", "observed_paths"}:
        raise InstallError("install receipt setup mutations are malformed")
    planned = _canonical_string_list(mutations.get("planned_paths"), field="planned_paths")
    observed = _canonical_string_list(mutations.get("observed_paths"), field="observed_paths")
    journal_path = install_root / "setup-journal.json"
    if requested.get("setup_requested") is True:
        if not journal_path.is_file() or _redirected(journal_path):
            raise InstallError("setup-requested installation has no plain setup journal")
        journal = _read_json(journal_path, JOURNAL_SCHEMA)
        if set(journal) != {
            "schema", "state", "planned_paths", "observed_paths",
            "manual_repair_required", "rollback_claimed", "outcome_known",
        }:
            raise InstallError("completed setup journal is not a closed record")
        if (
            journal.get("state") != "COMPLETE"
            or journal.get("manual_repair_required") is not False
            or journal.get("rollback_claimed") is not False
            or journal.get("outcome_known") is not True
            or journal.get("planned_paths") != planned
            or journal.get("observed_paths") != observed
        ):
            raise InstallError("install receipt setup mutations disagree with the setup journal")
        if not isinstance(receipt.get("setup_code"), str) or receipt.get("setup_code") == "SETUP_SKIPPED":
            raise InstallError("install receipt setup code is inconsistent")
    else:
        if planned or observed or journal_path.exists():
            raise InstallError("setup-skipped installation contains setup mutation evidence")
        if receipt.get("setup_code") != "SETUP_SKIPPED":
            raise InstallError("install receipt setup code is inconsistent")

    state_path = install_root / "install-state.json"
    if not state_path.is_file() or _redirected(state_path):
        raise InstallError("installed environment has no plain completion state")
    state = _read_json(state_path, STATE_SCHEMA)
    if set(state) != {"schema", "state", "candidate_digest", "project_identity"} or (
        state.get("state") != "COMPLETE"
        or state.get("candidate_digest") != candidate_digest
        or state.get("project_identity") != project_identity
    ):
        raise InstallError("install completion state disagrees with the receipt")


def _validate_installed_environment(
    install_root: Path,
    receipt: Mapping[str, Any],
    *,
    cwd: Path,
    build_tool_paths: Sequence[Path] = (),
) -> None:
    """Prove the reusable venv still contains its recorded product entry point."""

    python = _venv_python(install_root)
    console = _venv_console(install_root)
    if not python.is_file():
        raise InstallError(f"installed virtual-environment Python is missing: {python}")
    if not console.is_file() or _redirected(console):
        raise InstallError(f"installed orchestrator console is missing or redirected: {console}")
    if os.name == "posix" and (not os.access(python, os.X_OK) or not os.access(console, os.X_OK)):
        raise InstallError("installed virtual-environment commands are not executable")
    runtime_environment = dict(os.environ)
    runtime_environment.pop("PYTHONPATH", None)
    runtime_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    build_environment = dict(runtime_environment)
    if receipt.get("build_tool_source") == "operator_paths":
        if not build_tool_paths:
            raise InstallError(
                "this installation requires its original --build-tool-path inputs for validation"
            )
        if any(not path.is_absolute() or not path.is_dir() for path in build_tool_paths):
            raise InstallError("an installed build-tool validation path is unavailable")
        build_environment["PYTHONPATH"] = os.pathsep.join(
            str(path) for path in build_tool_paths
        )
    product_script = (
        "import importlib.metadata as m,json,memory_harness,orchestrator_harness;"
        "print(json.dumps({n:m.version(n) for n in "
        "['memory-harness','portable-orchestrator-harness']}))"
    )
    try:
        observed = json.loads(
            _run(
                [str(python), "-c", product_script],
                cwd=cwd,
                environment=runtime_environment,
            ).stdout
        )
        observed.update(json.loads(_run([
            str(python), "-c",
            "import importlib.metadata as m,json;"
            "print(json.dumps({n:m.version(n) for n in ['setuptools','wheel']}))",
        ], cwd=cwd, environment=build_environment).stdout))
    except (json.JSONDecodeError, TypeError) as exc:
        raise InstallError("installed virtual environment returned malformed version evidence") from exc
    if observed != receipt.get("package_versions"):
        raise InstallError("installed package versions disagree with the install receipt")
    _run([str(python), "-m", "pip", "check"], cwd=cwd, environment=runtime_environment)
    _run([str(console), "--help"], cwd=cwd, environment=runtime_environment)


def _install_packages(options: Options, source_root: Path) -> dict[str, str]:
    python = _venv_python(options.install_root)
    if not python.is_file():
        _run(
            [options.python, "-m", "venv", str(options.install_root / "venv")],
            cwd=source_root, timeout=120,
        )
    runtime_environment = dict(os.environ)
    # Build-tool injection is explicit. Ambient import roots could make the
    # isolated venv appear healthy while imports actually come from the host.
    runtime_environment.pop("PYTHONPATH", None)
    runtime_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    # Setuptools metadata directory removal is vulnerable to delayed NFS
    # directory-entry publication. Prefer the host-local conventional temp
    # root when available; pip still creates a private randomized directory.
    if os.name == "posix" and Path("/tmp").is_dir():
        runtime_environment["TMPDIR"] = "/tmp"
    environment = dict(runtime_environment)
    if options.build_tool_paths:
        for path in options.build_tool_paths:
            if not path.is_absolute() or not path.is_dir():
                raise InstallError(f"build-tool path must be an absolute directory: {path}")
        environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in options.build_tool_paths)
        _run(
            [str(python), "-c", "import setuptools,wheel"],
            cwd=source_root, environment=environment,
        )
    else:
        _run(
            [str(python), "-m", "pip", "install", "setuptools>=68", "wheel"],
            cwd=source_root, environment=environment,
        )
    build_parent = Path("/tmp") if os.name == "posix" and Path("/tmp").is_dir() else None
    build_root = Path(tempfile.mkdtemp(prefix="ma-harness-build-", dir=build_parent))
    try:
        memory_source = build_root / "memory-source"
        memory_source.mkdir()
        shutil.copy2(source_root / "pyproject.toml", memory_source / "pyproject.toml")
        shutil.copytree(
            source_root / "src", memory_source / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
        )
        harness_source = build_root / "harness-source"
        shutil.copytree(
            source_root / "harness", harness_source,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", "runtime", "local-config",
                "build", "dist", "*.pyc", "*.egg-info",
            ),
        )
        for source in (memory_source, harness_source):
            _run(
                [str(python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", str(source)],
                cwd=build_root, environment=environment,
            )
    finally:
        shutil.rmtree(build_root)
    _run(
        [str(python), "-m", "pip", "check"],
        cwd=source_root, environment=runtime_environment,
    )
    versions = json.loads(_run([
        str(python), "-c",
        "import importlib.metadata as m,json;"
        "print(json.dumps({n:m.version(n) for n in "
        "['memory-harness','portable-orchestrator-harness']}))",
    ], cwd=source_root, environment=runtime_environment).stdout)
    versions.update(json.loads(_run([
        str(python), "-c",
        "import importlib.metadata as m,json;"
        "print(json.dumps({n:m.version(n) for n in ['setuptools','wheel']}))",
    ], cwd=source_root, environment=environment).stdout))
    for name in ("memory-harness", "portable-orchestrator-harness"):
        if name not in versions:
            raise InstallError(f"installed distribution is missing: {name}")
    return {str(key): str(value) for key, value in versions.items()}


class _InstallLock:
    def __init__(self, install_root: Path) -> None:
        self.path = install_root.parent / f".{install_root.name}.install.lock"
        self.descriptor: int | None = None

    def __enter__(self) -> "_InstallLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self.descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        except FileExistsError as exc:
            raise InstallError(f"another install or stale lock exists: {self.path}") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
        self.path.unlink(missing_ok=True)


def _runtime_open(project_root: Path) -> bool:
    path = project_root / ".harness-runtime" / "RUNTIME_STATE.json"
    if not path.is_file():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise InstallError(f"runtime state is unreadable: {path}")
    return record.get("state") != "CLOSED"


def _promote_harness(stage: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    had_destination = destination.exists()
    try:
        if had_destination:
            os.replace(destination, backup)
        os.replace(stage, destination)
    except BaseException:
        if had_destination and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _json_command(argv: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(list(argv), cwd=cwd, text=True, capture_output=True, timeout=120)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError(
            f"public harness command returned malformed JSON: {completed.stderr.strip()}"
        ) from exc
    if not isinstance(value, dict):
        raise InstallError("public harness command returned a non-object")
    if bool(value.get("ok")) != (completed.returncode == 0):
        raise InstallError(
            "public harness command return code disagrees with its structured result"
        )
    return value


def _result_commands(
    install_root: Path, source_root: Path, project_root: Path, options: Options,
) -> dict[str, list[str]]:
    console = str(_venv_console(install_root))
    harness = str(install_root / "harness")
    update = [
        sys.executable, str(source_root / "scripts/install_product.py"),
        "--project", str(project_root), "--install-root", str(install_root),
        "--python", options.python,
        "--managed-coordination", options.managed_coordination,
    ]
    for flag, value in (
        ("--application", options.application), ("--namespace", options.namespace),
        ("--project-id", options.project_id_override), ("--owner", options.owner),
    ):
        if value is not None:
            update.extend((flag, value))
    if not options.memory:
        update.append("--no-memory")
    if not options.setup:
        update.append("--no-setup")
    for path in options.build_tool_paths:
        update.extend(("--build-tool-path", str(path)))
    return {
        "visualizer": [console, "view"],
        "visualizer_once": [console, "view", "--once", "--no-color"],
        "shutdown": [console, "--json", "harness", "shutdown"],
        "qualification_preflight": [
            str(_venv_python(install_root)),
            "-B",
            str(install_root / "harness/tools/qualification_preflight.py"),
            "--json",
            "--product-root",
            str(source_root),
        ],
        "smoke_test": [console, "view", "--once", "--no-color"],
        "update": update,
        "working_directory": [harness],
    }


def install(options: Options, *, source_root: Path = SOURCE_ROOT) -> dict[str, Any]:
    source_root = source_root.resolve()
    if _redirected(options.project_root):
        raise InstallError(f"project root must not be a link or junction: {options.project_root}")
    if _redirected(options.install_root):
        raise InstallError(f"install root must not be a link or junction: {options.install_root}")
    project_root, git_roots, git_common_dir = _git_project(options.project_root)
    options = Options(**{**options.__dict__, "project_root": project_root})
    validate_layout(
        source_root=source_root, project_root=project_root, install_root=options.install_root,
    )
    digest = _candidate_digest(source_root)
    receipt_path = options.install_root / "install-receipt.json"
    ownership_path = options.install_root / "ownership-manifest.json"

    with _InstallLock(options.install_root):
        if options.install_root.exists():
            _recover_interrupted_update(options.install_root)
        existing_receipt: dict[str, Any] | None = None
        existing_ownership: dict[str, Any] | None = None
        project_relinked = False
        if options.install_root.exists():
            if (
                not receipt_path.is_file()
                or not ownership_path.is_file()
                or _redirected(receipt_path)
                or _redirected(ownership_path)
            ):
                raise InstallError(
                    f"install root exists without complete ownership records: {options.install_root}"
                )
            existing_receipt = _read_json(receipt_path, RECEIPT_SCHEMA)
            existing_ownership = _read_json(ownership_path, OWNERSHIP_SCHEMA)
            _verify_owned(options.install_root, existing_ownership)
            desired = {
                "managed_coordination": options.managed_coordination,
                "memory_enabled": options.memory,
                "setup_requested": options.setup,
                "application": options.application,
                "namespace": options.namespace,
                "project_override": options.project_id_override,
                "owner": options.owner,
            }
            _validate_existing_receipt(
                options.install_root,
                existing_receipt,
                existing_ownership,
                requested=desired,
            )
            if tuple(existing_receipt.get("git_root_commits", ())) != git_roots:
                raise InstallError("install root is owned by a different Git project identity")
            prior_project_raw = existing_receipt.get("project_root")
            if not isinstance(prior_project_raw, str):
                raise InstallError("install receipt has no canonical project binding")
            prior_project = Path(prior_project_raw)
            if _identity(prior_project) != _identity(project_root):
                if not options.relink_project:
                    raise InstallError(
                        "install root is bound to a different project path; use a new install root "
                        "or explicitly pass --relink-project after moving/adding a worktree"
                    )
                prior_common = existing_receipt.get("git_common_dir")
                if prior_project.exists() and (
                    not isinstance(prior_common, str)
                    or _identity(Path(prior_common)) != _identity(git_common_dir)
                ):
                    raise InstallError(
                        "--relink-project cannot replace a live independent project binding"
                    )
                project_relinked = True
            _validate_installed_environment(
                options.install_root,
                existing_receipt,
                cwd=options.install_root / "harness",
                build_tool_paths=options.build_tool_paths,
            )
            if existing_receipt.get("candidate_digest") == digest and not project_relinked:
                return {
                    "ok": True,
                    "code": "ALREADY_INSTALLED",
                    "summary": "the exact owned product is already installed; no path changed",
                    "install_root": str(options.install_root),
                    "harness_root": str(options.install_root / "harness"),
                    "commands": _result_commands(
                        options.install_root, source_root, project_root, options,
                    ),
                }
            if _runtime_open(project_root) or (
                project_relinked and prior_project.exists() and _runtime_open(prior_project)
            ):
                raise InstallError("shut down the OPEN harness runtime before updating")
        else:
            options.install_root.mkdir(parents=True)

        project_identity = (
            str(existing_ownership["project_identity"])
            if existing_ownership is not None else new_project_identity()
        )
        state_path = options.install_root / "install-state.json"
        update_backup: Path | None = None
        if existing_receipt is not None:
            update_backup = _copy_update_backup(options.install_root)
            receipt_path.unlink()
        _atomic_json(state_path, {
            "schema": STATE_SCHEMA,
            "state": "UPDATING" if update_backup is not None else "INSTALLING",
            "candidate_digest": digest, "project_identity": project_identity,
        })
        stage = options.install_root / f".harness-stage-{uuid.uuid4().hex}"
        stage.mkdir()
        try:
            for relative in OPERATIONAL_PATHS:
                _copy_tree(source_root / "harness" / relative, stage / relative)
            tools_root = stage / "tools"
            tools_root.mkdir()
            shutil.copy2(
                source_root / "scripts/qualification_preflight.py",
                tools_root / "qualification_preflight.py",
            )
            shutil.copy2(
                source_root / "scripts/verification_receipt.py",
                tools_root / "verification_receipt.py",
            )
            store_root = options.install_root / "memory"
            if options.memory:
                store_root.mkdir(exist_ok=True)
                if os.name == "posix":
                    store_root.chmod(0o700)
                secrets_root = store_root / "secrets"
                secrets_root.mkdir(exist_ok=True)
                if os.name == "posix":
                    secrets_root.chmod(0o700)
            scope = _write_configs(
                stage, options=options, project_identity=project_identity, store_root=store_root,
            )
            package_versions = _install_packages(options, source_root)
            _validate_installed_environment(
                options.install_root,
                {
                    "package_versions": package_versions,
                    "build_tool_source": (
                        "operator_paths"
                        if options.build_tool_paths else "configured_package_index"
                    ),
                },
                cwd=stage,
                build_tool_paths=options.build_tool_paths,
            )
            _atomic_json(state_path, {
                "schema": STATE_SCHEMA, "state": "PROMOTING",
                "candidate_digest": digest, "project_identity": project_identity,
            })
            _promote_harness(stage, options.install_root / "harness")
        except BaseException:
            if stage.exists():
                shutil.rmtree(stage)
            if update_backup is not None and update_backup.exists():
                _restore_update_backup(options.install_root, update_backup)
            elif existing_receipt is None:
                shutil.rmtree(options.install_root, ignore_errors=True)
            raise

        owned, owned_directories = _owned_entries(options.install_root / "harness")
        ownership = {
            "schema": OWNERSHIP_SCHEMA,
            "project_identity": project_identity,
            "candidate_digest": digest,
            "files": owned,
            "directories": owned_directories,
        }
        _atomic_json(ownership_path, ownership)

        setup_result: dict[str, Any] = {"ok": True, "code": "SETUP_SKIPPED"}
        journal_path = options.install_root / "setup-journal.json"
        if options.setup:
            console = _venv_console(options.install_root)
            harness = options.install_root / "harness"
            try:
                checked = _json_command(
                    [str(console), "--json", "harness", "setup", "--check"], cwd=harness,
                )
            except BaseException:
                if update_backup is not None and update_backup.exists():
                    _restore_update_backup(options.install_root, update_backup)
                elif existing_receipt is None:
                    shutil.rmtree(options.install_root, ignore_errors=True)
                raise
            if not checked.get("ok"):
                if update_backup is not None and update_backup.exists():
                    _restore_update_backup(options.install_root, update_backup)
                elif existing_receipt is None:
                    shutil.rmtree(options.install_root, ignore_errors=True)
                raise InstallError(f"setup preflight failed without target mutation: {checked.get('summary')}")
            journal = {
                "schema": JOURNAL_SCHEMA,
                "state": "CHECKED",
                "planned_paths": list(checked.get("planned_paths", [])),
                "observed_paths": [],
            }
            _atomic_json(journal_path, journal)
            _atomic_json(state_path, {
                "schema": STATE_SCHEMA, "state": "SETUP_CHECKED",
                "candidate_digest": digest, "project_identity": project_identity,
            })
            try:
                setup_result = _json_command(
                    [str(console), "--json", "harness", "setup"], cwd=harness,
                )
            except BaseException as exc:
                journal.update({
                    "state": "PARTIAL_SETUP",
                    "summary": f"setup outcome is unknown: {exc}",
                    "outcome_known": False,
                    "manual_repair_required": True,
                    "rollback_claimed": False,
                })
                _atomic_json(journal_path, journal)
                _atomic_json(state_path, {
                    "schema": STATE_SCHEMA, "state": "PARTIAL_SETUP",
                    "candidate_digest": digest, "project_identity": project_identity,
                })
                raise InstallError(
                    "setup may have mutated its checked target plan but returned no trustworthy "
                    "result; inspect setup-journal.json because manual repair may be required"
                ) from exc
            observed = sorted({
                str(path)
                for field in ("evidence_paths", "overwritten_paths", "merged_paths")
                for path in setup_result.get(field, [])
                if isinstance(path, str)
            })
            journal["observed_paths"] = observed
            if not setup_result.get("ok"):
                journal.update({
                    "state": "PARTIAL_SETUP",
                    "summary": str(setup_result.get("summary", "setup failed")),
                    "outcome_known": True,
                    "manual_repair_required": True,
                    "rollback_claimed": False,
                })
                _atomic_json(journal_path, journal)
                _atomic_json(state_path, {
                    "schema": STATE_SCHEMA, "state": "PARTIAL_SETUP",
                    "candidate_digest": digest, "project_identity": project_identity,
                })
                raise InstallError(
                    "setup failed after its checked plan; inspect setup-journal.json and the named "
                    "target paths because manual repair may be required"
                )
            journal.update({
                "state": "COMPLETE", "manual_repair_required": False,
                "rollback_claimed": False, "outcome_known": True,
            })
            _atomic_json(journal_path, journal)

        requested = {
            "managed_coordination": options.managed_coordination,
            "memory_enabled": options.memory,
            "setup_requested": options.setup,
            "application": options.application,
            "namespace": options.namespace,
            "project_override": options.project_id_override,
            "owner": options.owner,
        }
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "candidate_digest": digest,
            "project_identity": project_identity,
            "project_root": str(project_root),
            "git_common_dir": str(git_common_dir),
            "git_root_commits": list(git_roots),
            "install_root": str(options.install_root),
            "scope": scope,
            "requested": requested,
            "package_versions": package_versions,
            "build_tool_source": "operator_paths" if options.build_tool_paths else "configured_package_index",
            "setup_code": setup_result.get("code"),
            "configuration_sha256": {
                name: _sha(options.install_root / "harness" / name)
                for name in (
                    "harness-config.json", "resource-manifest.json",
                    *(('memory-product-config.json',) if options.memory else ()),
                )
            },
            "setup_mutations": {
                "planned_paths": list(
                    json.loads(journal_path.read_text(encoding="utf-8")).get("planned_paths", [])
                    if journal_path.is_file() else []
                ),
                "observed_paths": list(
                    json.loads(journal_path.read_text(encoding="utf-8")).get("observed_paths", [])
                    if journal_path.is_file() else []
                ),
            },
            "created_unix_seconds": int(time.time()),
        }
        _atomic_json(receipt_path, receipt)
        _atomic_json(state_path, {
            "schema": STATE_SCHEMA, "state": "COMPLETE",
            "candidate_digest": digest, "project_identity": project_identity,
        })
        if update_backup is not None and update_backup.exists():
            shutil.rmtree(update_backup)
        return {
            "ok": True,
            "code": "INSTALL_OK",
            "summary": "MA-Harness is installed for the selected Git project",
            "install_root": str(options.install_root),
            "harness_root": str(options.install_root / "harness"),
            "setup": setup_result,
            "commands": _result_commands(
                options.install_root, source_root, project_root, options,
            ),
            "notes": [
                "run commands from harness_root so configuration discovery is unambiguous",
                "setup writes provider hook payloads and .harness-runtime under the project",
                "add .harness-runtime/ to your own ignore policy if desired; the installer never edits Git metadata",
                "for cleanup, shut down first and remove only this receipt-owned install_root after preserving any manual files",
                "if setup-journal.json is PARTIAL_SETUP, inspect every planned/observed path before retrying or deleting anything",
            ],
        }


def portable_installer_paths(root: Path = SOURCE_ROOT) -> list[Path]:
    candidates = (
        Path("install.sh"), Path("install.ps1"), Path("scripts/install_product.py"),
        Path("scripts/qualification_preflight.py"), Path("scripts/verification_receipt.py"),
        Path(".github/workflows/qualification.yml"),
    )
    return [path for path in candidates if (root / path).is_file()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="existing Git project root")
    parser.add_argument("--install-root", type=Path, default=Path.home() / ".local/share/ma-harness")
    parser.add_argument("--python", default=sys.executable, help="Python 3.11+ executable for the venv")
    parser.add_argument("--managed-coordination", choices=("enabled", "disabled"), default="enabled")
    parser.add_argument("--application")
    parser.add_argument("--namespace")
    parser.add_argument("--project-id", dest="project_id_override")
    parser.add_argument("--owner")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--no-setup", action="store_true")
    parser.add_argument(
        "--relink-project", action="store_true",
        help="explicitly rebind an owned install after moving the project or selecting a linked worktree",
    )
    parser.add_argument(
        "--build-tool-path", action="append", default=[], type=Path,
        help="advanced offline build-tool import root; repeat for setuptools/wheel",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        options = Options(
            project_root=args.project.expanduser().absolute(),
            install_root=args.install_root.expanduser().absolute(),
            python=args.python,
            managed_coordination=args.managed_coordination,
            application=args.application,
            namespace=args.namespace,
            project_id_override=args.project_id_override,
            owner=args.owner,
            memory=not args.no_memory,
            setup=not args.no_setup,
            build_tool_paths=tuple(path.expanduser().resolve() for path in args.build_tool_path),
            relink_project=args.relink_project,
        )
        if sys.version_info < (3, 11):
            raise InstallError("the installer launcher requires Python 3.11 or newer")
        result = install(options)
        status = 0
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        result = {
            "ok": False,
            "code": "INSTALL_FAILED",
            "summary": str(exc),
            "manual_repair_may_be_required": "manual repair" in str(exc),
        }
        status = 1
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(result["summary"])
        if result.get("ok"):
            print(f"Harness root: {result['harness_root']}")
            for label, command in result.get("commands", {}).items():
                print(f"{label}: {json.dumps(command)}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

"""Complete-product archive and isolated two-distribution qualification."""

from __future__ import annotations

import email
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

import pytest
import setuptools
import wheel as wheel_package


PRODUCT = Path(__file__).resolve().parents[2]
ARCHIVE = PRODUCT / "product-memory-harness-20260926-01.zip"
BUILDER = PRODUCT / "scripts/build_portable_archive.py"


def _environment(*, build_tools: bool = False) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(PRODUCT)),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }
    if build_tools:
        roots = {
            str(Path(setuptools.__file__).resolve().parent.parent),
            str(Path(wheel_package.__file__).resolve().parent.parent),
        }
        environment["PYTHONPATH"] = os.pathsep.join(sorted(roots))
    return environment


def _python(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _console(venv_root: Path) -> Path:
    return venv_root / ("Scripts/orchestrator-harness.exe" if os.name == "nt" else "bin/orchestrator-harness")


def _is_editable_nfs_metadata_cleanup_race(
    completed: subprocess.CompletedProcess[str], *, mode: str,
) -> bool:
    """Recognize only setuptools' known post-build NFS directory cleanup race.

    The editable install is retried from a fresh environment only when all of
    the narrow signals are present.  Ordinary build, dependency, metadata,
    import, and installation failures therefore remain immediately visible.
    """

    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    return (
        mode == "editable"
        and completed.returncode != 0
        and "[Errno 39] Directory not empty" in output
        and "editable_wheel" in output
        and "portable_orchestrator_harness-" in output
        and ".dist-info" in output
    )


def _package_smoke(python: Path, venv_root: Path, cwd: Path) -> None:
    code = (
        "import importlib.metadata as m,importlib.resources as r,sys;"
        "from pathlib import Path;"
        "import orchestrator_harness,harness_common,harness_watcher_implementation,memory_harness;"
        "assert sys.prefix != sys.base_prefix;"
        "assert m.version('portable-orchestrator-harness') == '2.0.0';"
        "assert m.version('memory-harness') == '0.1.0';"
        "p=r.files('orchestrator_harness').joinpath('assets/release/manifest.json');"
        "assert p.is_file();"
        "print(memory_harness.__file__)"
    )
    completed = subprocess.run(
        [str(python), "-c", code], cwd=cwd, env=_environment(),
        text=True, capture_output=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    for command in (
        [str(python), "-m", "orchestrator_harness.operator_launch", "--help"],
        [str(_console(venv_root)), "--help"],
    ):
        help_result = subprocess.run(
            command, cwd=cwd, env=_environment(), text=True,
            capture_output=True, timeout=20,
        )
        assert help_result.returncode == 0, help_result.stderr
        assert "view" in help_result.stdout


_CONFIGURED_LIFECYCLE_DRIVER = r'''\
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import memory_harness
import orchestrator_harness
from memory_harness import contracts, store
from orchestrator_harness import (
    config as harness_config,
    epochs,
    lanes,
    memory_handoff,
    memory_product_config,
    product_composition,
    terminal_evidence,
)
from orchestrator_harness.core import content_hash


harness = Path(sys.argv[1]).resolve()
workspace = Path(sys.argv[2]).resolve()
console = Path(sys.argv[3]).resolve()
assert Path(orchestrator_harness.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert Path(memory_harness.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert not Path(orchestrator_harness.__file__).resolve().is_relative_to(harness)


def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=60)
    if completed.returncode:
        raise AssertionError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def cli(*arguments: str) -> dict[str, object]:
    completed = run([str(console), "--json", *arguments], cwd=harness)
    value = json.loads(completed.stdout)
    assert value.get("ok") is True, value
    return value


def accepted_card(*, lane_id: str, objective_id: str, base_commit: str) -> dict[str, object]:
    plan = contracts.make_plan(
        plan_id=f"plan-{lane_id}",
        objective_id=objective_id,
        route="ordinary",
        state="accepted",
        content={"steps": ["repair parser", "run regression checks"]},
        accepted_by="ROOT",
    )
    handoff = contracts.make_memory_handoff(
        objective_id=objective_id,
        route="ordinary",
        plan=plan,
        checkpoint=f"checkpoint-{lane_id}",
    )
    return contracts.make_task_card(
        task="repair the parser regression using reviewed evidence",
        base_commit=base_commit,
        branch=f"lane/distribution-{lane_id}",
        memory_handoff=handoff,
    )


def bootstrap(*, lane_id: str, objective_id: str, base_commit: str) -> tuple[dict[str, object], Path, dict[str, object], dict[str, object]]:
    card = accepted_card(lane_id=lane_id, objective_id=objective_id, base_commit=base_commit)
    card_path = harness.parent / f"{lane_id}-task-card.json"
    write_json(card_path, card)
    result = cli(
        "lane", "bootstrap",
        "--lane-id", lane_id,
        "--provider", "codex",
        "--model", "distribution-smoke-model",
        "--provider-option", "reasoning_effort=high",
        "--provider-option", "service_tier=priority",
        "--task-card", str(card_path),
    )
    worktree = Path(result["evidence_paths"][0]).resolve()
    envelope = memory_handoff.load_envelope(worktree)
    assert envelope is not None
    runtime = harness_config.load_config(harness).runtime_root
    epoch = epochs.read_current_epoch(runtime)
    assert epoch is not None
    lane = lanes.read_lane(runtime, str(epoch["epoch_id"]), lane_id)
    assert lane["dispatchable"] is True
    assert envelope["plan_id"] == f"plan-{lane_id}"
    assert envelope["final_context"]["checkpoint"] == f"checkpoint-{lane_id}"
    return card, worktree, envelope, lane


def settle_accepted(*, card: dict[str, object], worktree: Path, envelope: dict[str, object], lane: dict[str, object]) -> dict[str, object]:
    context = memory_handoff.load_final_context(worktree_path=worktree, envelope=envelope)
    memory_handoff.record_dispatch_intent(worktree_path=worktree, envelope=envelope)
    observed = memory_handoff.native_observation(
        envelope=envelope,
        context=context,
        # This process is the offline smoke's real, installed public-boundary
        # driver.  No provider or remote service is claimed or contacted.
        controller_identity={"pid": os.getpid(), "creation_time": "offline-distribution-smoke"},
    )
    stored_operation = memory_handoff.record_observed_invocation(
        worktree_path=worktree,
        envelope=envelope,
        observed_invocation=observed,
    )
    operation = {
        key: stored_operation[key]
        for key in (
            "operation_id", "decision_id", "envelope_digest", "run_id", "kind",
            "status", "observed_invocation", "created_at", "updated_at",
        )
    }
    result = {
        "schema": "result/v1",
        "lane_id": envelope["lane_id"],
        "run_id": envelope["run_id"],
        "outcome": "PASS",
        "summary": "offline installed-product lifecycle completed",
        "evidence": [],
        "completed_at": "2026-10-02T00:00:00Z",
    }
    result["content_hash"] = content_hash(result)
    card_id = str(card.get("card_id") or card.get("id") or card["content_hash"])
    reviewed_at = "2026-10-02T00:00:01Z"
    review = {
        "schema": "completion-review/v1",
        "lane_id": envelope["lane_id"],
        "run_id": envelope["run_id"],
        "review_outcome": "PASS",
        "review_summary": "ROOT accepted the offline installed-product lifecycle",
        "evidence": [],
        "task_card_id": card_id,
        "task_card_hash": card["content_hash"],
        "result_id": envelope["run_id"],
        "result_hash": result["content_hash"],
        "commit": card["base_commit"],
        "reviewed_at": reviewed_at,
    }
    review["content_hash"] = content_hash(review)
    acceptance = {
        "schema": "orchestrator-acceptance/v1",
        "lane_id": envelope["lane_id"],
        "run_id": envelope["run_id"],
        "approval": "ACCEPTED",
        "accepted_by": "ROOT",
        "review_ref": review["content_hash"],
        "task_card_id": card_id,
        "task_card_hash": card["content_hash"],
        "result_id": envelope["run_id"],
        "result_hash": result["content_hash"],
        "commit": card["base_commit"],
        "decided_at": reviewed_at,
    }
    acceptance["content_hash"] = content_hash(acceptance)
    runtime = harness_config.load_config(harness).runtime_root
    epoch = epochs.read_current_epoch(runtime)
    assert epoch is not None
    local = store.MemoryStore(worktree / ".agent-workspace" / "memory-state.sqlite3")
    local.initialize()
    try:
        stored_decision = local.get_decision(str(envelope["decision_id"]))
    finally:
        local.close()
    decision = {
        "schema": contracts.DECISION_SCHEMA,
        **{
            key: stored_decision[key]
            for key in (
                "decision_id", "task_card_digest", "objective_id", "route",
                "plan_id", "plan_state", "plan_digest", "strategy",
                "configuration", "configuration_digest", "state", "created_at",
                "content_hash",
            )
        },
    }
    evidence = terminal_evidence.make_terminal_evidence(
        epoch_id=str(epoch["epoch_id"]),
        lane_id=str(envelope["lane_id"]),
        run_id=str(envelope["run_id"]),
        task_card=card,
        accepted_plan=card["memory_handoff"]["plan"],
        objective_id=str(envelope["objective_id"]),
        decision_id=str(envelope["decision_id"]),
        decision=decision,
        envelope=envelope,
        final_context=context,
        dispatch_operation=operation,
        envelope_digest=str(envelope["content_hash"]),
        observed_invocation=observed,
        configuration=envelope["configuration"],
        configuration_digest=str(envelope["configuration_digest"]),
        result=result,
        review=review,
        acceptance=acceptance,
        terminal_proof=None,
    )
    previous = Path.cwd()
    try:
        # Product configuration discovery is intentionally rooted at the
        # operational harness, never at the source checkout or ambient path.
        os.chdir(harness)
        outcome = memory_handoff.record_native_review(
            worktree_path=worktree,
            evidence=evidence,
        )
    finally:
        os.chdir(previous)
    assert outcome["status"] == "PASS"
    assert outcome["acceptance_status"] == "ACCEPTED"
    return outcome


workspace.mkdir()
run(["git", "init"], cwd=workspace)
run(["git", "config", "user.name", "Distribution Qualification"], cwd=workspace)
run(["git", "config", "user.email", "distribution@example.invalid"], cwd=workspace)
(workspace / "README.md").write_text("offline lifecycle workspace\n", encoding="utf-8")
run(["git", "add", "README.md"], cwd=workspace)
run(["git", "commit", "-m", "base"], cwd=workspace)
base_commit = run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

write_json(
    harness / "harness-config.json",
    {"root_workspace": str(workspace), "managed_coordination": "enabled"},
)
write_json(harness / "resource-manifest.json", {"schema": "resource-manifest/v1", "resources": []})
store_root = harness.parent / "operator-memory"
store_root.mkdir()
write_json(
    harness / "memory-product-config.json",
    {
        "schema": "memory-product-config/v1",
        "application": "distribution-app",
        "namespace": "distribution-namespace",
        "project": "distribution-project",
        "owner": "ROOT",
        "store_root": str(store_root),
        "policy_generation": "distribution-qualification-v1",
        "known_secret_files": [],
        "services": {
            "local_experience": {"enabled": True},
            "local_procedures": {"enabled": True},
            "everos": {"enabled": False, "credential_env": None},
            "atlas": {
                "enabled": False,
                "credential_env": None,
                "database": None,
                "collection": None,
                "index": None,
            },
        },
    },
)
configured = memory_product_config.load_memory_product_config(harness)
memory_product_config.initialize_secret_rotation_state(configured)

shutdown_error = None
bootstrapped_lanes = []
try:
    setup = cli("harness", "setup", "--overwrite")
    assert setup["code"] == "SETUP_OK"

    first_card, first_worktree, first_envelope, first_lane = bootstrap(
        lane_id="distribution-history",
        objective_id="distribution-history-objective",
        base_commit=base_commit,
    )
    bootstrapped_lanes.append("distribution-history")
    central_path = product_composition.central_store_path(configured)
    local_path = first_worktree / ".agent-workspace" / "memory-state.sqlite3"
    assert central_path.is_file(), "configured central store was not constructed"
    assert local_path.is_file(), "lane-local store was not constructed"
    settle_accepted(
        card=first_card,
        worktree=first_worktree,
        envelope=first_envelope,
        lane=first_lane,
    )

    second_card, second_worktree, second_envelope, second_lane = bootstrap(
        lane_id="distribution-search",
        objective_id="distribution-search-objective",
        base_commit=base_commit,
    )
    bootstrapped_lanes.append("distribution-search")
    sources = {
        item["provenance"]["source_id"]
        for item in second_envelope["delivery_trace"]["selected"]
    }
    assert "local-reviewed-experience" in sources, sources
    assert second_envelope["plan_id"] == "plan-distribution-search"
    assert second_lane["dispatchable"] is True
    assert (second_worktree / ".agent-workspace" / "memory-state.sqlite3").is_file()
    print(json.dumps({
        "status": "qualified",
        "configured_central_store": str(central_path),
        "configured_local_store": str(local_path),
        "settled_outcome": "PASS",
        "search_source": "local-reviewed-experience",
        "enhanced_plan": second_envelope["plan_id"],
    }, sort_keys=True))
finally:
    try:
        # Prepared lanes have intentionally not spawned a provider.  Exercise
        # the supported operator cleanup boundary before closing the runtime.
        for lane_id in reversed(bootstrapped_lanes):
            cli("lane", "force-stop", "--lane-id", lane_id)
        shutdown = cli("harness", "shutdown")
        assert shutdown["code"] == "SHUTDOWN_OK", shutdown
    except Exception as exc:
        shutdown_error = exc
    if shutdown_error is not None:
        raise shutdown_error
'''


def _configured_lifecycle_smoke(
    *, python: Path, venv_root: Path, cwd: Path, harness: Path, workspace: Path,
) -> None:
    driver = cwd / "installed-configured-lifecycle.py"
    driver.write_text(_CONFIGURED_LIFECYCLE_DRIVER, encoding="utf-8")
    completed = subprocess.run(
        [str(python), str(driver), str(harness), str(workspace), str(_console(venv_root))],
        cwd=cwd,
        env=_environment(),
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    evidence = json.loads(completed.stdout.splitlines()[-1])
    assert evidence == {
        "status": "qualified",
        "configured_central_store": str((harness.parent / "operator-memory" / "memory-product.sqlite3").resolve()),
        "configured_local_store": evidence["configured_local_store"],
        "settled_outcome": "PASS",
        "search_source": "local-reviewed-experience",
        "enhanced_plan": "plan-distribution-search",
    }


def _create_venv(interpreter: str, root: Path) -> Path:
    completed = subprocess.run(
        [interpreter, "-m", "venv", str(root)], cwd=root.parent,
        env=_environment(), text=True, capture_output=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    config = (root / "pyvenv.cfg").read_text(encoding="utf-8").lower()
    assert "include-system-site-packages = false" in config
    return _python(root)


def _copy_sources(destination: Path, *, product: Path = PRODUCT) -> tuple[Path, Path]:
    memory_source = destination / "memory-source"
    memory_source.mkdir()
    shutil.copy2(product / "pyproject.toml", memory_source / "pyproject.toml")
    shutil.copytree(product / "src", memory_source / "src", ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.egg-info",
    ))
    harness_source = destination / "harness-source"
    shutil.copytree(product / "harness", harness_source, ignore=shutil.ignore_patterns(
        "__pycache__", ".pytest_cache", "runtime", "local-config", "*.egg-info", "build", "dist",
    ))
    return memory_source, harness_source


def _build_all(
    tmp_path: Path, *, product: Path = PRODUCT,
) -> tuple[Path, Path, list[Path], list[Path]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    memory_source, harness_source = _copy_sources(tmp_path, product=product)
    dist = tmp_path / "dist"
    dist.mkdir()
    wheels: list[Path] = []
    sdists: list[Path] = []
    for source in (memory_source, harness_source):
        built = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "-w", str(dist), str(source)],
            cwd=tmp_path, env=_environment(build_tools=True), text=True,
            capture_output=True, timeout=90,
        )
        assert built.returncode == 0, built.stderr
        sdist = subprocess.run(
            [sys.executable, "-c",
             "from setuptools.build_meta import build_sdist; print(build_sdist(r'%s'))" % dist],
            cwd=source, env=_environment(build_tools=True), text=True,
            capture_output=True, timeout=90,
        )
        assert sdist.returncode == 0, sdist.stderr
    wheels.extend(sorted(dist.glob("*.whl")))
    sdists.extend(sorted(dist.glob("*.tar.gz")))
    assert len(wheels) == len(sdists) == 2
    return memory_source, harness_source, wheels, sdists


def _wheel_package_roots(wheels: list[Path]) -> dict[str, set[str]]:
    contents: dict[str, set[str]] = {}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            dist_info = next(name for name in names if name.endswith(".dist-info/METADATA"))
            project = email.message_from_bytes(archive.read(dist_info))["Name"]
            contents[project] = {
                name.split("/", 1)[0] for name in names
                if "/" in name and ".dist-info/" not in name and ".data/" not in name
            }
    assert set(contents) == {"memory-harness", "portable-orchestrator-harness"}
    assert contents["memory-harness"].isdisjoint(contents["portable-orchestrator-harness"])
    return contents


def test_deterministic_zip_covers_complete_product_source_bytes(tmp_path: Path) -> None:
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    for output in (first, second):
        completed = subprocess.run(
            [sys.executable, str(BUILDER), "--output", str(output)],
            cwd=PRODUCT, env=_environment(), text=True, capture_output=True, timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
    assert first.read_bytes() == second.read_bytes()
    tracked_modes = {}
    for line in subprocess.run(
        ["git", "ls-files", "--stage"], cwd=PRODUCT,
        text=True, capture_output=True, check=True,
    ).stdout.splitlines():
        metadata, name = line.split("\t", 1)
        tracked_modes[name] = 0o755 if metadata.split(" ", 1)[0] == "100755" else 0o644
    with zipfile.ZipFile(first) as archive:
        assert archive.testzip() is None
        names = {item.filename for item in archive.infolist() if not item.is_dir()}
        required = {
            "pyproject.toml", "harness/pyproject.toml", "README.md", "TESTING.md",
            "install.sh", "install.ps1", ".github/workflows/qualification.yml",
            "src/memory_harness/__init__.py", "harness/orchestrator_harness/__init__.py",
            "scripts/run_tests.py", "scripts/build_portable_archive.py",
            "scripts/install_product.py", "scripts/qualification_preflight.py",
            "scripts/verification_receipt.py",
            "tests/suite_manifest.json", "harness/docs/product/FULL_PRODUCT_SPEC.md",
        }
        assert required <= names
        assert all(
            name.startswith((".github/", "src/", "harness/", "scripts/", "tests/"))
            or name in {
                "pyproject.toml", "README.md", "TESTING.md", "AGENTS.md",
                "install.sh", "install.ps1",
            }
            for name in names
        )
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
        for item in archive.infolist():
            if not item.is_dir():
                assert archive.read(item) == (PRODUCT / item.filename).read_bytes()
                assert (item.external_attr >> 16) & 0o777 == tracked_modes.get(
                    item.filename, 0o644,
                )
    assert hashlib.sha256(first.read_bytes()).hexdigest()


def test_two_distributions_have_exact_dependency_extras_and_no_package_overlap(tmp_path: Path) -> None:
    _, _, wheels, _ = _build_all(tmp_path)
    contents = _wheel_package_roots(wheels)
    metadata = {}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            dist_info = next(name for name in names if name.endswith(".dist-info/METADATA"))
            message = email.message_from_bytes(archive.read(dist_info))
            project = message["Name"]
            metadata[project] = message
    assert contents["memory-harness"] == {"memory_harness"}
    assert contents["portable-orchestrator-harness"] >= {
        "orchestrator_harness", "harness_common", "harness_watcher_implementation",
    }
    assert contents["memory-harness"].isdisjoint(contents["portable-orchestrator-harness"])
    requirements = metadata["portable-orchestrator-harness"].get_all("Requires-Dist") or []
    compact = [value.replace(" ", "").replace("'", '"') for value in requirements]
    assert "memory-harness==0.1.0" in compact
    assert any("memory-harness[everos]==0.1.0" in value and 'extra=="everos"' in value for value in compact)
    assert any("memory-harness[atlas]==0.1.0" in value and 'extra=="atlas"' in value for value in compact)


def test_portable_zip_builds_installs_offline_and_runs_enhanced_public_boundary(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh-portable.zip"
    completed = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(fresh)],
        cwd=PRODUCT, env=_environment(), text=True, capture_output=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert ARCHIVE.is_file(), "the checked release ZIP is missing"
    assert ARCHIVE.read_bytes() == fresh.read_bytes(), (
        "checked release ZIP is stale; regenerate only after all candidate source stabilizes with: "
        "python scripts/build_portable_archive.py --output "
        "product-memory-harness-20260926-01.zip"
    )
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(ARCHIVE) as archive:
        archive.extractall(extracted)
    _, _, wheels, sdists = _build_all(tmp_path / "archive-build", product=extracted)
    assert len(sdists) == 2
    _wheel_package_roots(wheels)

    local_artifacts = tmp_path / "archive-wheelhouse"
    local_artifacts.mkdir()
    for wheel in wheels:
        shutil.copy2(wheel, local_artifacts / wheel.name)
    harness_wheel = next(
        path for path in local_artifacts.iterdir()
        if path.name.startswith("portable_orchestrator_harness-")
    )
    environment = tmp_path / "archive-venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
    python = _python(environment)
    unrelated = tmp_path / "archive-unrelated-cwd"
    unrelated.mkdir()
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-build-isolation",
         "--find-links", str(local_artifacts), str(harness_wheel)],
        cwd=unrelated, env=_environment(), text=True, capture_output=True, timeout=120,
    )
    assert installed.returncode == 0, installed.stderr
    _package_smoke(python, environment, unrelated)
    operational_harness = tmp_path / "operational-harness"
    shutil.copytree(
        extracted / "harness",
        operational_harness,
        ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", "runtime", "local-config",
            "*.egg-info", "build", "dist",
        ),
    )
    _configured_lifecycle_smoke(
        python=python,
        venv_root=environment,
        cwd=unrelated,
        harness=operational_harness,
        workspace=tmp_path / "configured-root-workspace",
    )


def test_wheel_sdist_and_editable_install_both_products_from_unrelated_cwd(tmp_path: Path) -> None:
    memory_source, harness_source, wheels, sdists = _build_all(tmp_path)
    unrelated = tmp_path / "unrelated cwd"
    unrelated.mkdir()
    modes = {
        "wheel": wheels,
        "sdist": sdists,
        "editable": [memory_source, harness_source],
    }
    for name, targets in modes.items():
        environment = tmp_path / f"venv-{name}"
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
        python = _python(environment)
        command = [str(python), "-m", "pip", "install", "--no-build-isolation"]
        if name == "editable":
            command.append("--no-deps")
            for target in targets:
                command.extend(["--editable", str(target)])
        else:
            local_artifacts = tmp_path / f"local-{name}-artifacts"
            local_artifacts.mkdir()
            for target in targets:
                shutil.copy2(target, local_artifacts / target.name)
            harness_artifact = next(
                target for target in local_artifacts.iterdir()
                if target.name.startswith((
                    "portable_orchestrator_harness-", "portable-orchestrator-harness-",
                ))
            )
            command.extend(["--find-links", str(local_artifacts), str(harness_artifact)])
        installed = subprocess.run(
            command, cwd=unrelated,
            env=_environment(build_tools=name in {"sdist", "editable"}), text=True,
            capture_output=True, timeout=120,
        )
        if _is_editable_nfs_metadata_cleanup_race(installed, mode=name):
            # Never trust the possibly partial first environment.  A single
            # retry uses a fresh venv and, on POSIX, a node-local temporary
            # root instead of the NFS-backed test TMPDIR that triggered the
            # delayed directory-entry cleanup.  A repeated or different
            # failure is asserted below without further retries.
            environment = tmp_path / f"venv-{name}-nfs-cleanup-retry"
            venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
            python = _python(environment)
            command[0] = str(python)
            retry_env = _environment(build_tools=True)
            retry_temp: Path | None = None
            if os.name == "posix" and Path("/tmp").is_dir():
                retry_temp = Path(tempfile.mkdtemp(
                    prefix="memory-harness-editable-retry-", dir="/tmp",
                ))
                retry_env["TMPDIR"] = str(retry_temp)
            try:
                installed = subprocess.run(
                    command, cwd=unrelated, env=retry_env, text=True,
                    capture_output=True, timeout=120,
                )
            finally:
                if retry_temp is not None:
                    shutil.rmtree(retry_temp)
        assert installed.returncode == 0, installed.stderr
        checked = subprocess.run(
            [str(python), "-m", "pip", "check"], cwd=unrelated,
            env=_environment(), text=True, capture_output=True, timeout=20,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        _package_smoke(python, environment, unrelated)


def test_editable_retry_discriminates_exact_nfs_metadata_cleanup_race() -> None:
    exact = subprocess.CompletedProcess(
        args=["pip", "install", "--editable", "harness-source"],
        returncode=1,
        stdout="running editable_wheel\n",
        stderr=(
            "OSError: [Errno 39] Directory not empty: "
            "'portable_orchestrator_harness-2.0.0.dist-info'"
        ),
    )
    assert _is_editable_nfs_metadata_cleanup_race(exact, mode="editable")
    assert not _is_editable_nfs_metadata_cleanup_race(exact, mode="wheel")
    assert not _is_editable_nfs_metadata_cleanup_race(
        subprocess.CompletedProcess(
            args=exact.args,
            returncode=1,
            stdout="running editable_wheel\n",
            stderr=(
                "OSError: [Errno 13] Permission denied: "
                "'portable_orchestrator_harness-2.0.0.dist-info'"
            ),
        ),
        mode="editable",
    )
    assert not _is_editable_nfs_metadata_cleanup_race(
        subprocess.CompletedProcess(
            args=exact.args,
            returncode=1,
            stdout="building wheel\n",
            stderr=exact.stderr,
        ),
        mode="editable",
    )
    assert not _is_editable_nfs_metadata_cleanup_race(
        subprocess.CompletedProcess(
            args=exact.args,
            returncode=0,
            stdout=exact.stdout,
            stderr=exact.stderr,
        ),
        mode="editable",
    )


@pytest.mark.parametrize("version", ["3.11", "3.12", "3.13", "3.14"])
def test_declared_release_interpreter_coordinate(tmp_path: Path, version: str) -> None:
    expected = os.environ.get("MEMORY_HARNESS_EXPECT_PYTHON_COORDINATE")
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    executable = sys.executable if expected == version and running == version else shutil.which(f"python{version}")
    if executable is None:
        pytest.skip(f"CPython {version} executable is unavailable; coordinate remains unproved")
    memory_source, harness_source = _copy_sources(tmp_path)
    dist = tmp_path / f"dist-{version}"
    dist.mkdir()
    for source in (memory_source, harness_source):
        built = subprocess.run(
            [executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "-w", str(dist), str(source)],
            cwd=tmp_path, env=_environment(build_tools=True), text=True,
            capture_output=True, timeout=90,
        )
        assert built.returncode == 0, built.stderr
    python = _create_venv(executable, tmp_path / f"venv-{version}")
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 2
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", *map(str, wheels)],
        cwd=tmp_path, env=_environment(), text=True, capture_output=True, timeout=90,
    )
    assert installed.returncode == 0, installed.stderr
    _package_smoke(python, tmp_path / f"venv-{version}", tmp_path)


def test_expected_release_interpreter_is_executing() -> None:
    expected = os.environ.get("MEMORY_HARNESS_EXPECT_PYTHON_COORDINATE")
    if expected is None:
        pytest.skip("no CI interpreter coordinate was declared")
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert running == expected, (
        f"CI selected CPython {expected}, but tests are running on CPython {running}"
    )

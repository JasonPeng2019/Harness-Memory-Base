"""Clone-to-personal-project installer behavior."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import setuptools
import wheel

from scripts import install_product


PRODUCT = Path(__file__).resolve().parents[2]


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, timeout=180,
    )


def _project(root: Path, name: str = "personal project") -> Path:
    project = root / name
    project.mkdir()
    for command in (
        ["git", "init"],
        ["git", "config", "user.name", "Installer Test"],
        ["git", "config", "user.email", "installer@example.invalid"],
    ):
        completed = _run(command, cwd=project)
        assert completed.returncode == 0, completed.stderr
    (project / "README.md").write_text("personal project\n", encoding="utf-8")
    for command in (["git", "add", "README.md"], ["git", "commit", "-m", "base"]):
        completed = _run(command, cwd=project)
        assert completed.returncode == 0, completed.stderr
    return project


def _python(install_root: Path) -> Path:
    return install_root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _console(install_root: Path) -> Path:
    return install_root / "venv" / ("Scripts/orchestrator-harness.exe" if os.name == "nt" else "bin/orchestrator-harness")


def test_scope_identity_is_persisted_and_clone_distinct(tmp_path: Path) -> None:
    first = install_product.new_project_identity()
    second = install_product.new_project_identity()
    assert first != second
    assert len(first) == len(second) == 32
    scope = install_product.default_scope("My Project", first)
    assert scope == {
        "application": "ma-harness",
        "namespace": f"my-project-{first}",
        "project": f"my-project-{first}",
        "owner": "ROOT",
    }


def test_path_validation_rejects_install_inside_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    try:
        install_product.validate_layout(
            source_root=PRODUCT,
            project_root=project,
            install_root=project / ".ma-harness",
        )
    except install_product.InstallError as exc:
        assert "disjoint" in str(exc)
    else:
        raise AssertionError("nested install root was accepted")


def test_real_local_install_setup_visualizer_and_idempotent_rerun(tmp_path: Path) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "installed product"
    build_roots = sorted({
        str(Path(setuptools.__file__).resolve().parent.parent),
        str(Path(wheel.__file__).resolve().parent.parent),
    })
    command = [
        sys.executable,
        str(PRODUCT / "scripts/install_product.py"),
        "--project", str(project),
        "--install-root", str(install_root),
        "--python", sys.executable,
        "--json",
    ]
    for root in build_roots:
        command.extend(["--build-tool-path", root])
    first = _run(command, cwd=PRODUCT)
    assert first.returncode == 0, first.stdout + first.stderr
    result = json.loads(first.stdout)
    assert result["ok"] is True
    assert result["setup"]["code"] in {"SETUP_OK", "SETUP_MONITOR_ALREADY_RUNNING"}
    assert (install_root / "install-receipt.json").is_file()
    assert (install_root / "ownership-manifest.json").is_file()
    receipt = json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))
    assert set(receipt["configuration_sha256"]) == {
        "harness-config.json", "resource-manifest.json", "memory-product-config.json",
    }
    assert receipt["setup_mutations"]["planned_paths"]
    assert Path(result["commands"]["qualification_preflight"][2]).is_file()
    assert result["commands"]["smoke_test"] == result["commands"]["visualizer_once"]
    assert "--project" in result["commands"]["update"]
    harness = install_root / "harness"
    config = json.loads((harness / "harness-config.json").read_text(encoding="utf-8"))
    assert config["root_workspace"] == str(project.resolve())
    memory = json.loads((harness / "memory-product-config.json").read_text(encoding="utf-8"))
    assert memory["services"]["atlas"]["enabled"] is False
    assert memory["services"]["everos"]["enabled"] is False
    if os.name == "posix":
        assert (install_root / "memory").stat().st_mode & 0o077 == 0
    imported = _run([
        str(_python(install_root)), "-c",
        "import memory_harness,orchestrator_harness; print('installed')",
    ], cwd=tmp_path)
    assert imported.returncode == 0, imported.stderr
    viewed = _run([str(_console(install_root)), "view", "--once", "--no-color"], cwd=harness)
    assert viewed.returncode == 0, viewed.stderr
    readiness = _run(result["commands"]["qualification_preflight"], cwd=harness)
    assert readiness.returncode == 0, readiness.stderr
    readiness_document = json.loads(readiness.stdout)
    assert readiness_document["schema"] == "memory-harness-qualification-preflight/v1"
    assert len(readiness_document["coordinates"]) == 15

    second = _run(command, cwd=PRODUCT)
    assert second.returncode == 0, second.stdout + second.stderr
    rerun = json.loads(second.stdout)
    assert rerun["code"] == "ALREADY_INSTALLED"

    shutdown = _run([str(_console(install_root)), "--json", "harness", "shutdown"], cwd=harness)
    assert shutdown.returncode == 0, shutdown.stdout + shutdown.stderr

    _console(install_root).unlink()
    broken = _run(command, cwd=PRODUCT)
    assert broken.returncode == 1
    assert "console is missing" in json.loads(broken.stdout)["summary"]


def test_entry_scripts_and_tools_are_selected_for_portable_archive() -> None:
    selected = {
        path.as_posix()
        for path in install_product.portable_installer_paths(PRODUCT)
    }
    assert {
        "install.sh",
        "install.ps1",
        "scripts/install_product.py",
        "scripts/qualification_preflight.py",
        "scripts/verification_receipt.py",
        ".github/workflows/qualification.yml",
    } <= selected


def test_setup_failure_after_checked_plan_is_journaled_without_rollback_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "partial install"
    sentinel = project / "operator-owned.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    observed = project / ".codex" / "hooks.json"

    monkeypatch.setattr(
        install_product,
        "_install_packages",
        lambda _options, _source: {
            "memory-harness": "0.1.0",
            "portable-orchestrator-harness": "2.0.0",
            "setuptools": "test",
            "wheel": "test",
        },
    )
    _skip_installed_environment_validation(monkeypatch)
    invocations = 0

    def fail_after_mutation(_argv, *, cwd):
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            return {
                "ok": True,
                "code": "SETUP_CHECK_OK",
                "planned_paths": [str(observed)],
            }
        observed.parent.mkdir(parents=True)
        observed.write_text('{"partial":true}\n', encoding="utf-8")
        return {
            "ok": False,
            "code": "SETUP_CONFIG_INVALID",
            "summary": "synthetic failure after first ROOT payload",
            "evidence_paths": [str(observed)],
        }

    monkeypatch.setattr(install_product, "_json_command", fail_after_mutation)
    options = install_product.Options(
        project_root=project,
        install_root=install_root,
        python=sys.executable,
        managed_coordination="enabled",
        application=None,
        namespace=None,
        project_id_override=None,
        owner=None,
        memory=True,
        setup=True,
        build_tool_paths=(),
    )
    with pytest.raises(install_product.InstallError, match="manual repair"):
        install_product.install(options, source_root=PRODUCT)
    journal = json.loads((install_root / "setup-journal.json").read_text(encoding="utf-8"))
    assert journal["state"] == "PARTIAL_SETUP"
    assert journal["planned_paths"] == [str(observed)]
    assert journal["observed_paths"] == [str(observed)]
    assert journal["manual_repair_required"] is True
    assert journal["rollback_claimed"] is False
    assert not (install_root / "install-receipt.json").exists()
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def _fast_options(project: Path, install_root: Path, *, setup: bool = False) -> install_product.Options:
    return install_product.Options(
        project_root=project,
        install_root=install_root,
        python=sys.executable,
        managed_coordination="enabled",
        application=None,
        namespace=None,
        project_id_override=None,
        owner=None,
        memory=True,
        setup=setup,
        build_tool_paths=(),
    )


def _fake_packages(_options: install_product.Options, _source: Path) -> dict[str, str]:
    return {
        "memory-harness": "0.1.0",
        "portable-orchestrator-harness": "2.0.0",
        "setuptools": "test",
        "wheel": "test",
    }


def _skip_installed_environment_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast installer fixtures replace pip and therefore have no executable venv."""

    monkeypatch.setattr(
        install_product,
        "_validate_installed_environment",
        lambda _root, _receipt, *, cwd, build_tool_paths=(): None,
    )


def test_update_preserves_valid_operator_configuration_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)

    harness = install_root / "harness"
    secret = install_root / "memory" / "secrets" / "atlas-token.txt"
    secret.write_text("operator secret\n", encoding="utf-8")
    if os.name == "posix":
        secret.chmod(0o600)
    configured = {
        "harness-config.json": (
            json.dumps({
                "managed_coordination": "disabled",
                "root_workspace": str(project.resolve()),
            }, indent=2, sort_keys=False) + "\n"
        ).encode(),
        "resource-manifest.json": (
            json.dumps({
                "resources": [{"exclusive": True, "id": "operator-gpu"}],
                "schema": "resource-manifest/v1",
            }, indent=4, sort_keys=False) + "\n"
        ).encode(),
    }
    memory_path = harness / "memory-product-config.json"
    memory = json.loads(memory_path.read_text(encoding="utf-8"))
    memory.update({
        "application": "operator-app",
        "namespace": "operator-namespace",
        "project": "operator-project",
        "owner": "OPERATOR_ROOT",
        "policy_generation": "operator-generation-7",
        "known_secret_files": [str(secret)],
    })
    memory["services"]["atlas"] = {
        "enabled": True,
        "credential_env": "ATLAS_OPERATOR_TOKEN",
        "database": "operator_db",
        "collection": "operator_collection",
        "index": "operator_index",
    }
    configured["memory-product-config.json"] = (
        json.dumps(memory, indent=3, sort_keys=False) + "\n"
    ).encode()
    for name, content in configured.items():
        (harness / name).write_bytes(content)

    updated = install_product.install(options, source_root=PRODUCT)

    assert updated["code"] == "INSTALL_OK"
    assert updated["setup"]["code"] == "SETUP_SKIPPED"
    for name, content in configured.items():
        assert (harness / name).read_bytes() == content
    receipt = json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))
    ownership = json.loads((install_root / "ownership-manifest.json").read_text(encoding="utf-8"))
    assert receipt["scope"] == {
        "application": "operator-app",
        "namespace": "operator-namespace",
        "project": "operator-project",
        "owner": "OPERATOR_ROOT",
    }
    for name, content in configured.items():
        expected = hashlib.sha256(content).hexdigest()
        assert receipt["configuration_sha256"][name] == expected
        assert ownership["files"][name] == expected
    assert install_product.install(options, source_root=PRODUCT)["code"] == "ALREADY_INSTALLED"
    for name, content in configured.items():
        assert (harness / name).read_bytes() == content


def test_valid_harness_config_cannot_rebind_receipt_to_unrelated_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path, "bound-project")
    unrelated = _project(tmp_path, "unrelated-project")
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    config_path = install_root / "harness" / "harness-config.json"
    configured = {
        "root_workspace": str(unrelated.resolve()),
        "managed_coordination": "disabled",
    }
    install_product._atomic_json(config_path, configured)
    prior_receipt = (install_root / "install-receipt.json").read_bytes()

    with pytest.raises(install_product.InstallError, match="workspace.*project binding"):
        install_product.install(options, source_root=PRODUCT)

    assert json.loads(config_path.read_text(encoding="utf-8")) == configured
    assert (install_root / "install-receipt.json").read_bytes() == prior_receipt
    assert not (install_root / install_product.UPDATE_BACKUP_NAME).exists()


def test_same_candidate_valid_memory_edit_is_preserved_and_receipt_reanchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    memory_path = install_root / "harness" / "memory-product-config.json"
    memory = json.loads(memory_path.read_text(encoding="utf-8"))
    memory["namespace"] = "operator-edited-namespace"
    configured = (json.dumps(memory, indent=3, sort_keys=False) + "\n").encode()
    memory_path.write_bytes(configured)

    refreshed = install_product.install(options, source_root=PRODUCT)

    assert refreshed["code"] == "INSTALL_OK"
    assert memory_path.read_bytes() == configured
    receipt = json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))
    ownership = json.loads((install_root / "ownership-manifest.json").read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(configured).hexdigest()
    assert receipt["scope"]["namespace"] == "operator-edited-namespace"
    assert receipt["configuration_sha256"]["memory-product-config.json"] == expected_hash
    assert ownership["files"]["memory-product-config.json"] == expected_hash
    assert install_product.install(options, source_root=PRODUCT)["code"] == "ALREADY_INSTALLED"


def test_same_candidate_rejects_receipt_scope_tamper_when_config_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    receipt_path = install_root / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["scope"]["namespace"] = "tampered-receipt-namespace"
    install_product._atomic_json(receipt_path, receipt)

    with pytest.raises(install_product.InstallError, match="scope disagrees with memory configuration"):
        install_product.install(options, source_root=PRODUCT)

    configured = json.loads(
        (install_root / "harness/memory-product-config.json").read_text(encoding="utf-8")
    )
    assert configured["namespace"] != "tampered-receipt-namespace"


@pytest.mark.parametrize("config_name", [
    "harness-config.json",
    "resource-manifest.json",
    "memory-product-config.json",
])
def test_update_rejects_invalid_operator_configuration_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_name: str,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    receipt_path = install_root / "install-receipt.json"
    prior_receipt = receipt_path.read_bytes()
    config_path = install_root / "harness" / config_name
    if config_name == "harness-config.json":
        invalid = {"root_workspace": "relative/workspace", "managed_coordination": "enabled"}
    elif config_name == "resource-manifest.json":
        invalid = {
            "schema": "resource-manifest/v1",
            "resources": [
                {"id": "duplicate", "exclusive": True},
                {"id": "duplicate", "exclusive": True},
            ],
        }
    else:
        invalid = json.loads(config_path.read_text(encoding="utf-8"))
        invalid["unknown_operator_key"] = True
    install_product._atomic_json(config_path, invalid)
    invalid_bytes = config_path.read_bytes()

    with pytest.raises(install_product.InstallError, match="operator .* configuration|resource manifest"):
        install_product.install(options, source_root=PRODUCT)

    assert config_path.read_bytes() == invalid_bytes
    assert receipt_path.read_bytes() == prior_receipt
    assert not (install_root / install_product.UPDATE_BACKUP_NAME).exists()
    assert not (install_root / install_product.PENDING_RECEIPT_NAME).exists()


def test_mutable_config_exception_does_not_weaken_other_owned_file_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    target = install_root / "harness" / "tools" / "qualification_preflight.py"
    target.write_bytes(target.read_bytes() + b"\n# operator drift\n")
    prior_receipt = (install_root / "install-receipt.json").read_bytes()

    with pytest.raises(install_product.InstallError, match="owned file is missing or drifted"):
        install_product.install(options, source_root=PRODUCT)

    assert target.read_bytes().endswith(b"# operator drift\n")
    assert (install_root / "install-receipt.json").read_bytes() == prior_receipt


def test_failed_update_restores_operator_configuration_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    _skip_installed_environment_validation(monkeypatch)
    calls = 0

    def packages(options: install_product.Options, source: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise install_product.InstallError("injected package update failure")
        return _fake_packages(options, source)

    monkeypatch.setattr(install_product, "_install_packages", packages)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    config_path = install_root / "harness" / "harness-config.json"
    configured = (
        json.dumps({
            "managed_coordination": "disabled",
            "root_workspace": str(project.resolve()),
        }, indent=4) + "\n"
    ).encode()
    config_path.write_bytes(configured)

    with pytest.raises(install_product.InstallError, match="package update failure"):
        install_product.install(options, source_root=PRODUCT)

    assert config_path.read_bytes() == configured
    assert json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))["candidate_digest"] == "a" * 64
    assert not (install_root / install_product.UPDATE_BACKUP_NAME).exists()


def test_real_no_setup_install_validates_console_without_touching_project(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "no-setup-install"
    build_roots = sorted({
        str(Path(setuptools.__file__).resolve().parent.parent),
        str(Path(wheel.__file__).resolve().parent.parent),
    })
    command = [
        sys.executable,
        str(PRODUCT / "scripts/install_product.py"),
        "--project", str(project),
        "--install-root", str(install_root),
        "--python", sys.executable,
        "--no-setup",
        "--json",
    ]
    for root in build_roots:
        command.extend(["--build-tool-path", root])

    completed = _run(command, cwd=PRODUCT)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["setup"]["code"] == "SETUP_SKIPPED"
    assert not (install_root / "setup-journal.json").exists()
    assert not (project / ".harness-runtime").exists()
    assert not (project / ".codex").exists()
    assert not (project / ".claude").exists()
    assert _run([str(_console(install_root)), "--help"], cwd=install_root / "harness").returncode == 0
    assert _run(["git", "status", "--porcelain"], cwd=project).stdout == ""


def test_fresh_install_does_not_publish_success_without_a_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "broken-install"
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)

    with pytest.raises(install_product.InstallError, match="Python is missing"):
        install_product.install(_fast_options(project, install_root), source_root=PRODUCT)

    assert not install_root.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-script fixture")
def test_environment_validation_rejects_a_python_that_cannot_import_products(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "broken-environment"
    commands = install_root / "venv" / "bin"
    commands.mkdir(parents=True)
    python = commands / "python"
    python.write_text(f"#!/bin/sh\nexec {sys.executable!s} -S \"$@\"\n", encoding="utf-8")
    python.chmod(0o700)
    console = commands / "orchestrator-harness"
    console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    console.chmod(0o700)

    with pytest.raises(install_product.InstallError, match="command failed"):
        install_product._validate_installed_environment(
            install_root,
            {
                "build_tool_source": "configured_package_index",
                "package_versions": _fake_packages(
                    _fast_options(tmp_path, install_root), PRODUCT,
                ),
            },
            cwd=tmp_path,
        )


def test_update_refuses_unowned_harness_entries_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    unowned = install_root / "harness" / "operator-note.txt"
    unowned.write_text("do not delete\n", encoding="utf-8")

    with pytest.raises(install_product.InstallError, match="unowned"):
        install_product.install(options, source_root=PRODUCT)

    assert unowned.read_text(encoding="utf-8") == "do not delete\n"
    receipt = json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))
    assert receipt["candidate_digest"] == "a" * 64


def test_failed_package_update_restores_prior_venv_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    _skip_installed_environment_validation(monkeypatch)
    calls = 0

    def packages(options: install_product.Options, _source: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        sentinel = options.install_root / "venv" / "installed-version.txt"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("old\n" if calls == 1 else "partial-new\n", encoding="utf-8")
        if calls == 2:
            raise install_product.InstallError("injected second package failure")
        return _fake_packages(options, _source)

    monkeypatch.setattr(install_product, "_install_packages", packages)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)

    with pytest.raises(install_product.InstallError, match="injected"):
        install_product.install(options, source_root=PRODUCT)

    assert (install_root / "venv" / "installed-version.txt").read_text(encoding="utf-8") == "old\n"
    receipt = json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))
    assert receipt["candidate_digest"] == "a" * 64
    state = json.loads((install_root / "install-state.json").read_text(encoding="utf-8"))
    assert state["state"] == "COMPLETE"


def test_same_history_clone_is_not_silently_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path, "original")
    clone = tmp_path / "independent-clone"
    cloned = _run(["git", "clone", str(project), str(clone)], cwd=tmp_path)
    assert cloned.returncode == 0, cloned.stderr
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    install_product.install(_fast_options(project, install_root), source_root=PRODUCT)

    with pytest.raises(install_product.InstallError, match="different project path"):
        install_product.install(_fast_options(clone, install_root), source_root=PRODUCT)


def test_explicit_relink_after_project_move_preserves_identity_and_rewrites_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path, "before-move")
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    first_options = _fast_options(project, install_root)
    first = install_product.install(first_options, source_root=PRODUCT)
    prior_identity = json.loads(
        (install_root / "ownership-manifest.json").read_text(encoding="utf-8")
    )["project_identity"]
    moved = tmp_path / "after-move"
    project.rename(moved)
    moved_options = install_product.Options(
        **{**_fast_options(moved, install_root).__dict__, "relink_project": True}
    )

    result = install_product.install(moved_options, source_root=PRODUCT)

    assert first["code"] == "INSTALL_OK"
    assert result["code"] == "INSTALL_OK"
    assert json.loads((install_root / "harness/harness-config.json").read_text(encoding="utf-8"))["root_workspace"] == str(moved.resolve())
    assert json.loads((install_root / "ownership-manifest.json").read_text(encoding="utf-8"))["project_identity"] == prior_identity


def test_explicit_linked_worktree_relink_preserves_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path, "primary")
    linked = tmp_path / "linked-worktree"
    created = _run(
        ["git", "worktree", "add", "-b", "installer-linked-test", str(linked)],
        cwd=project,
    )
    assert created.returncode == 0, created.stderr
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    install_product.install(_fast_options(project, install_root), source_root=PRODUCT)
    prior_identity = json.loads(
        (install_root / "ownership-manifest.json").read_text(encoding="utf-8")
    )["project_identity"]
    options = install_product.Options(**{
        **_fast_options(linked, install_root).__dict__, "relink_project": True,
    })

    result = install_product.install(options, source_root=PRODUCT)

    assert result["code"] == "INSTALL_OK"
    assert json.loads((install_root / "harness/harness-config.json").read_text(encoding="utf-8"))["root_workspace"] == str(linked.resolve())
    assert json.loads((install_root / "ownership-manifest.json").read_text(encoding="utf-8"))["project_identity"] == prior_identity


def test_unknown_setup_subprocess_failure_is_durably_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    calls = 0

    def command(_argv, *, cwd):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"ok": True, "code": "SETUP_CHECK_OK", "planned_paths": [str(project / ".codex/hooks.json")]}
        raise install_product.InstallError("public harness command returned malformed JSON")

    monkeypatch.setattr(install_product, "_json_command", command)
    with pytest.raises(install_product.InstallError, match="manual repair"):
        install_product.install(_fast_options(project, install_root, setup=True), source_root=PRODUCT)

    journal = json.loads((install_root / "setup-journal.json").read_text(encoding="utf-8"))
    assert journal["state"] == "PARTIAL_SETUP"
    assert journal["outcome_known"] is False
    assert journal["manual_repair_required"] is True
    assert not (install_root / "install-receipt.json").exists()


def test_install_lock_fails_closed_without_mutating_install_root(tmp_path: Path) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    with install_product._InstallLock(install_root):
        with pytest.raises(install_product.InstallError, match="lock"):
            install_product.install(_fast_options(project, install_root), source_root=PRODUCT)
    assert not install_root.exists()


def test_failed_harness_promotion_restores_prior_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    prior = (install_root / "harness/harness-config.json").read_bytes()

    def broken_promotion(stage: Path, destination: Path) -> None:
        import shutil
        shutil.rmtree(destination)
        os.replace(stage, destination)
        raise OSError("injected promotion interruption")

    monkeypatch.setattr(install_product, "_promote_harness", broken_promotion)
    with pytest.raises(OSError, match="promotion"):
        install_product.install(options, source_root=PRODUCT)

    assert (install_root / "harness/harness-config.json").read_bytes() == prior
    assert (install_root / "install-receipt.json").is_file()
    assert not (install_root / install_product.UPDATE_BACKUP_NAME).exists()


def test_failed_setup_update_has_no_current_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    calls = 0

    def command(_argv, *, cwd):
        nonlocal calls
        calls += 1
        if calls in (1, 3):
            return {"ok": True, "code": "SETUP_CHECK_OK", "planned_paths": [str(project / ".codex/hooks.json")]}
        if calls == 2:
            return {"ok": True, "code": "SETUP_OK", "evidence_paths": []}
        return {"ok": False, "code": "SETUP_FAILED", "summary": "injected", "evidence_paths": []}

    monkeypatch.setattr(install_product, "_json_command", command)
    options = _fast_options(project, install_root, setup=True)
    install_product.install(options, source_root=PRODUCT)
    with pytest.raises(install_product.InstallError, match="manual repair"):
        install_product.install(options, source_root=PRODUCT)

    assert not (install_root / "install-receipt.json").exists()
    assert (install_root / install_product.UPDATE_BACKUP_NAME / "install-receipt.json").is_file()
    state = json.loads((install_root / "install-state.json").read_text(encoding="utf-8"))
    assert state["state"] == "PARTIAL_SETUP"


def test_open_runtime_blocks_changed_source_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    runtime = project / ".harness-runtime"
    runtime.mkdir()
    (runtime / "RUNTIME_STATE.json").write_text('{"state":"OPEN"}\n', encoding="utf-8")

    with pytest.raises(install_product.InstallError, match="shut down"):
        install_product.install(options, source_root=PRODUCT)

    assert json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))["candidate_digest"] == "a" * 64


def test_no_memory_and_explicit_scope_are_applied_without_secret_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    base = _fast_options(project, install_root)
    options = install_product.Options(**{
        **base.__dict__,
        "application": "personal-app",
        "namespace": "private-namespace",
        "project_id_override": "project-one",
        "owner": "PERSONAL_ROOT",
        "memory": False,
    })
    result = install_product.install(options, source_root=PRODUCT)

    assert result["code"] == "INSTALL_OK"
    assert not (install_root / "memory").exists()
    assert not (install_root / "harness/memory-product-config.json").exists()
    receipt_text = (install_root / "install-receipt.json").read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert receipt["scope"] == {
        "application": "personal-app",
        "namespace": "private-namespace",
        "project": "project-one",
        "owner": "PERSONAL_ROOT",
    }
    assert "credential" not in receipt_text.casefold()


def test_interrupted_pre_setup_update_restores_backup_on_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    backup = install_product._copy_update_backup(install_root)
    (install_root / "install-receipt.json").unlink()
    (install_root / "harness/harness-config.json").write_text("partial\n", encoding="utf-8")
    install_product._atomic_json(install_root / "install-state.json", {
        "schema": install_product.STATE_SCHEMA,
        "state": "UPDATING",
        "candidate_digest": "b" * 64,
        "project_identity": "synthetic",
    })

    result = install_product.install(options, source_root=PRODUCT)

    assert backup.exists() is False
    assert result["code"] == "ALREADY_INSTALLED"
    assert json.loads((install_root / "harness/harness-config.json").read_text(encoding="utf-8"))["root_workspace"] == str(project.resolve())


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_layout_rejects_symlink_roots_and_alias_collisions(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project_link = tmp_path / "project-link"
    project_link.symlink_to(project, target_is_directory=True)
    with pytest.raises(install_product.InstallError, match="link"):
        install_product.validate_layout(
            source_root=PRODUCT,
            project_root=project_link,
            install_root=tmp_path / "install",
        )
    install_alias = tmp_path / "install-alias"
    install_alias.symlink_to(project, target_is_directory=True)
    with pytest.raises(install_product.InstallError):
        install_product.validate_layout(
            source_root=PRODUCT,
            project_root=project,
            install_root=install_alias,
        )


def test_public_command_rejects_exit_status_and_json_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        install_product.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout='{"ok":true,"code":"FALSE_SUCCESS"}', stderr="failed",
        ),
    )
    with pytest.raises(install_product.InstallError, match="disagrees"):
        install_product._json_command(["synthetic"], cwd=tmp_path)


def test_reuse_rejects_tampered_configuration_hash_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    receipt_path = install_root / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["configuration_sha256"]["harness-config.json"] = "0" * 64
    install_product._atomic_json(receipt_path, receipt)

    with pytest.raises(install_product.InstallError, match="configuration hash mismatch"):
        install_product.install(options, source_root=PRODUCT)


def test_reuse_rejects_setup_mutations_that_disagree_with_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    planned = str(project / ".codex" / "hooks.json")
    commands = iter((
        {"ok": True, "code": "SETUP_CHECK_OK", "planned_paths": [planned]},
        {"ok": True, "code": "SETUP_OK", "evidence_paths": []},
    ))
    monkeypatch.setattr(
        install_product, "_json_command", lambda _argv, *, cwd: next(commands),
    )
    options = _fast_options(project, install_root, setup=True)
    install_product.install(options, source_root=PRODUCT)
    receipt_path = install_root / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["setup_mutations"]["planned_paths"] = [str(tmp_path / "foreign")]
    install_product._atomic_json(receipt_path, receipt)

    with pytest.raises(install_product.InstallError, match="disagree"):
        install_product.install(options, source_root=PRODUCT)


def test_interrupted_setup_checked_update_is_reclassified_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    planned = str(project / ".codex" / "hooks.json")
    commands = iter((
        {"ok": True, "code": "SETUP_CHECK_OK", "planned_paths": [planned]},
        {"ok": True, "code": "SETUP_OK", "evidence_paths": []},
    ))
    monkeypatch.setattr(
        install_product, "_json_command", lambda _argv, *, cwd: next(commands),
    )
    options = _fast_options(project, install_root, setup=True)
    install_product.install(options, source_root=PRODUCT)
    backup = install_product._copy_update_backup(install_root)
    (install_root / "install-receipt.json").unlink()
    install_product._atomic_json(install_root / "setup-journal.json", {
        "schema": install_product.JOURNAL_SCHEMA,
        "state": "CHECKED",
        "planned_paths": [planned],
        "observed_paths": [],
    })
    ownership = json.loads(
        (install_root / "ownership-manifest.json").read_text(encoding="utf-8")
    )
    install_product._atomic_json(install_root / "install-state.json", {
        "schema": install_product.STATE_SCHEMA,
        "state": "SETUP_CHECKED",
        "candidate_digest": "b" * 64,
        "project_identity": ownership["project_identity"],
    })

    with pytest.raises(install_product.InstallError, match="outcome is unknown"):
        install_product.install(options, source_root=PRODUCT)

    assert backup.is_dir()
    state = json.loads((install_root / "install-state.json").read_text(encoding="utf-8"))
    journal = json.loads((install_root / "setup-journal.json").read_text(encoding="utf-8"))
    assert state["state"] == "PARTIAL_SETUP"
    assert journal["state"] == "PARTIAL_SETUP"
    assert journal["outcome_known"] is False
    assert journal["manual_repair_required"] is True


def test_update_crash_before_complete_restores_prior_receipt_and_removes_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64, "a" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    original_atomic = install_product._atomic_json
    crashed = False

    def crash_before_complete(path: Path, document: dict[str, object]) -> None:
        nonlocal crashed
        if path.name == "install-state.json" and document.get("state") == "COMPLETE" and not crashed:
            crashed = True
            raise SystemExit("injected before COMPLETE")
        original_atomic(path, document)

    with monkeypatch.context() as fault:
        fault.setattr(install_product, "_atomic_json", crash_before_complete)
        with pytest.raises(SystemExit, match="before COMPLETE"):
            install_product.install(options, source_root=PRODUCT)

    assert not (install_root / "install-receipt.json").exists()
    assert (install_root / install_product.PENDING_RECEIPT_NAME).is_file()
    assert (install_root / install_product.UPDATE_BACKUP_NAME).is_dir()

    recovered = install_product.install(options, source_root=PRODUCT)

    assert recovered["code"] == "ALREADY_INSTALLED"
    receipt = json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))
    assert receipt["candidate_digest"] == "a" * 64
    assert not (install_root / install_product.PENDING_RECEIPT_NAME).exists()
    assert not (install_root / install_product.UPDATE_BACKUP_NAME).exists()


def test_update_crash_before_backup_disposition_finalizes_only_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    original_discard = install_product._discard_update_backup

    with monkeypatch.context() as fault:
        fault.setattr(
            install_product,
            "_discard_update_backup",
            lambda _backup: (_ for _ in ()).throw(SystemExit("injected before backup disposition")),
        )
        with pytest.raises(SystemExit, match="backup disposition"):
            install_product.install(options, source_root=PRODUCT)

    assert not (install_root / "install-receipt.json").exists()
    assert (install_root / install_product.PENDING_RECEIPT_NAME).is_file()
    assert (install_root / install_product.UPDATE_BACKUP_NAME).is_dir()
    assert json.loads((install_root / "install-state.json").read_text(encoding="utf-8"))["state"] == "COMPLETE"

    monkeypatch.setattr(install_product, "_discard_update_backup", original_discard)
    recovered = install_product.install(options, source_root=PRODUCT)

    assert recovered["code"] == "ALREADY_INSTALLED"
    assert json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))["candidate_digest"] == "b" * 64
    assert not (install_root / install_product.PENDING_RECEIPT_NAME).exists()
    assert not (install_root / install_product.UPDATE_BACKUP_NAME).exists()


def test_complete_recovery_validates_real_environment_before_publication_or_backup_disposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    options = _fast_options(project, install_root)
    with monkeypatch.context() as initial:
        initial.setattr(install_product, "_install_packages", _fake_packages)
        initial.setattr(
            install_product,
            "_validate_installed_environment",
            lambda _root, _receipt, *, cwd, build_tool_paths=(): None,
        )
        install_product.install(options, source_root=PRODUCT)
    backup = install_product._copy_update_backup(install_root)
    public_receipt = install_root / "install-receipt.json"
    pending_receipt = install_root / install_product.PENDING_RECEIPT_NAME
    os.replace(public_receipt, pending_receipt)

    with pytest.raises(install_product.InstallError, match="Python is missing"):
        install_product.install(options, source_root=PRODUCT)

    assert backup.is_dir()
    assert pending_receipt.is_file()
    assert not public_receipt.exists()


def test_update_crash_after_backup_disposition_publishes_pending_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)

    with monkeypatch.context() as fault:
        fault.setattr(
            install_product,
            "_publish_pending_receipt",
            lambda _pending, _receipt: (_ for _ in ()).throw(
                SystemExit("injected before final receipt publication")
            ),
        )
        with pytest.raises(SystemExit, match="final receipt publication"):
            install_product.install(options, source_root=PRODUCT)

    assert not (install_root / "install-receipt.json").exists()
    assert (install_root / install_product.PENDING_RECEIPT_NAME).is_file()
    assert not (install_root / install_product.UPDATE_BACKUP_NAME).exists()

    recovered = install_product.install(options, source_root=PRODUCT)

    assert recovered["code"] == "ALREADY_INSTALLED"
    assert json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))["candidate_digest"] == "b" * 64
    assert not (install_root / install_product.PENDING_RECEIPT_NAME).exists()


def test_fresh_install_crash_before_final_receipt_recovers_complete_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)

    with monkeypatch.context() as fault:
        fault.setattr(
            install_product,
            "_publish_pending_receipt",
            lambda _pending, _receipt: (_ for _ in ()).throw(
                SystemExit("injected fresh receipt publication crash")
            ),
        )
        with pytest.raises(SystemExit, match="fresh receipt publication"):
            install_product.install(options, source_root=PRODUCT)

    assert not (install_root / "install-receipt.json").exists()
    assert (install_root / install_product.PENDING_RECEIPT_NAME).is_file()
    assert json.loads((install_root / "install-state.json").read_text(encoding="utf-8"))["state"] == "COMPLETE"

    recovered = install_product.install(options, source_root=PRODUCT)

    assert recovered["code"] == "ALREADY_INSTALLED"
    assert (install_root / "install-receipt.json").is_file()
    assert not (install_root / install_product.PENDING_RECEIPT_NAME).exists()


def test_fresh_install_crash_before_complete_removes_nonpublic_pending_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    original_atomic = install_product._atomic_json

    def crash_before_complete(path: Path, document: dict[str, object]) -> None:
        if path.name == "install-state.json" and document.get("state") == "COMPLETE":
            raise SystemExit("injected fresh COMPLETE crash")
        original_atomic(path, document)

    with monkeypatch.context() as fault:
        fault.setattr(install_product, "_atomic_json", crash_before_complete)
        with pytest.raises(SystemExit, match="fresh COMPLETE"):
            install_product.install(options, source_root=PRODUCT)

    assert not (install_root / "install-receipt.json").exists()
    assert (install_root / install_product.PENDING_RECEIPT_NAME).is_file()
    with pytest.raises(install_product.InstallError, match="complete ownership records"):
        install_product.install(options, source_root=PRODUCT)
    assert not (install_root / "install-receipt.json").exists()
    assert not (install_root / install_product.PENDING_RECEIPT_NAME).exists()


def test_premature_public_receipt_is_removed_before_pre_setup_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    backup = install_product._copy_update_backup(install_root)
    ownership = json.loads((install_root / "ownership-manifest.json").read_text(encoding="utf-8"))
    install_product._atomic_json(install_root / "install-state.json", {
        "schema": install_product.STATE_SCHEMA,
        "state": "PROMOTING",
        "candidate_digest": "b" * 64,
        "project_identity": ownership["project_identity"],
    })

    recovered = install_product.install(options, source_root=PRODUCT)

    assert recovered["code"] == "ALREADY_INSTALLED"
    assert not backup.exists()
    assert json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))["candidate_digest"] == "a" * 64


def test_premature_public_receipt_is_removed_when_setup_recovery_becomes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    responses = iter((
        {"ok": True, "code": "SETUP_CHECK_OK", "planned_paths": [str(project / ".codex/hooks.json")]},
        {"ok": True, "code": "SETUP_OK", "evidence_paths": []},
    ))
    monkeypatch.setattr(
        install_product, "_json_command", lambda _argv, *, cwd: next(responses),
    )
    options = _fast_options(project, install_root, setup=True)
    install_product.install(options, source_root=PRODUCT)
    backup = install_product._copy_update_backup(install_root)
    ownership = json.loads((install_root / "ownership-manifest.json").read_text(encoding="utf-8"))
    install_product._atomic_json(install_root / "install-state.json", {
        "schema": install_product.STATE_SCHEMA,
        "state": "SETUP_CHECKED",
        "candidate_digest": "b" * 64,
        "project_identity": ownership["project_identity"],
    })

    with pytest.raises(install_product.InstallError, match="outcome is unknown"):
        install_product.install(options, source_root=PRODUCT)

    assert backup.is_dir()
    assert not (install_root / "install-receipt.json").exists()
    assert not (install_root / install_product.PENDING_RECEIPT_NAME).exists()
    state = json.loads((install_root / "install-state.json").read_text(encoding="utf-8"))
    assert state["state"] == "PARTIAL_SETUP"


def test_success_receipt_publication_is_after_complete_and_backup_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: next(digests))
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    events: list[str] = []
    original_atomic = install_product._atomic_json
    original_discard = install_product._discard_update_backup
    original_publish = install_product._publish_pending_receipt

    def observed_atomic(path: Path, document: dict[str, object]) -> None:
        original_atomic(path, document)
        if path.name == "install-state.json" and document.get("state") == "COMPLETE":
            events.append("complete")

    def observed_discard(backup: Path) -> None:
        original_discard(backup)
        events.append("backup-disposed")

    def observed_publish(pending: Path, receipt: Path) -> None:
        assert not (install_root / install_product.UPDATE_BACKUP_NAME).exists()
        assert json.loads((install_root / "install-state.json").read_text(encoding="utf-8"))["state"] == "COMPLETE"
        original_publish(pending, receipt)
        events.append("receipt-published")

    monkeypatch.setattr(install_product, "_atomic_json", observed_atomic)
    monkeypatch.setattr(install_product, "_discard_update_backup", observed_discard)
    monkeypatch.setattr(install_product, "_publish_pending_receipt", observed_publish)

    install_product.install(options, source_root=PRODUCT)

    assert events[-3:] == ["complete", "backup-disposed", "receipt-published"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_update_rejects_redirected_descendant_in_owned_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    install_root = tmp_path / "install"
    monkeypatch.setattr(install_product, "_candidate_digest", lambda _root: "a" * 64)
    monkeypatch.setattr(install_product, "_install_packages", _fake_packages)
    _skip_installed_environment_validation(monkeypatch)
    options = _fast_options(project, install_root)
    install_product.install(options, source_root=PRODUCT)
    target = install_root / "harness" / "resource-manifest.json"
    target.unlink()
    target.symlink_to(project / "README.md")

    with pytest.raises(install_product.InstallError, match="link or junction"):
        install_product.install(options, source_root=PRODUCT)

"""Mutation-sensitive contracts for the opt-in live qualification broker."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scripts import verification_receipt


GATE_PATH = Path(__file__).resolve().parents[2] / "live/qualification/conftest.py"
SPEC = importlib.util.spec_from_file_location("memory_harness_live_gate", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


def _driver(tmp_path: Path) -> tuple[Path, Path]:
    driver = tmp_path / "driver.py"
    log = tmp_path / "requests.jsonl"
    driver.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "from pathlib import Path\n"
        "request=json.load(sys.stdin)\n"
        f"log=Path({str(log)!r})\n"
        "with log.open('a',encoding='utf-8') as h: h.write(json.dumps(request,sort_keys=True)+'\\n')\n"
        "if request['scenario']=='scenario-fail': sys.exit(7)\n"
        "receipt={'schema':'memory-harness-live-qualification-receipt/v1',"
        "'scenario':request['scenario'],'candidate':request['candidate'],"
        "'namespace':request['namespace'],'calls':0,'cost_usd':0.0}\n"
        "if request['scenario']=='cleanup': receipt['cleanup']={'attempted':True,'complete':True,'remaining':[]}\n"
        "print(json.dumps(receipt,sort_keys=True))\n",
        encoding="utf-8",
    )
    driver.chmod(driver.stat().st_mode | stat.S_IXUSR)
    return driver, log


def _authorization(tmp_path: Path):
    driver, log = _driver(tmp_path)
    candidate = {"commit": "a" * 40, "tree": "b" * 40, "artifact_sha256": "c" * 64}
    return GATE.LiveAuthorization(
        namespace="mhq-disposable", remaining_calls=3, remaining_cost_usd=0.3,
        timeout=10, driver=driver, candidate=candidate,
        nonce_digest="d" * 64, deadline=time.monotonic() + 10,
        candidate_recheck=lambda: candidate,
    ), log


def _qualified_candidate(tmp_path: Path) -> tuple[Path, Path, Path]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=candidate, check=True)
    subprocess.run(["git", "config", "user.name", "Live Gate Test"], cwd=candidate, check=True)
    subprocess.run(
        ["git", "config", "user.email", "live-gate@example.invalid"],
        cwd=candidate,
        check=True,
    )
    (candidate / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    (candidate / "tests").mkdir()
    (candidate / "tests/suite_manifest.json").write_bytes(
        (GATE.PRODUCT / "tests/suite_manifest.json").read_bytes()
    )
    subprocess.run(["git", "add", "."], cwd=candidate, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=candidate, check=True)

    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"qualified artifact")
    receipt = tmp_path / "verification-receipt.json"
    candidate_manifest = verification_receipt.candidate_manifest(candidate)
    artifact_manifest = verification_receipt.artifact_manifest([artifact])
    categories = json.loads(
        (candidate / "tests/suite_manifest.json").read_text(encoding="utf-8")
    )["all"]
    document = {
        "schema": verification_receipt.SCHEMA,
        "candidate": {
            "root": str(candidate.resolve()),
            "pre": candidate_manifest,
            "post": candidate_manifest,
            "stable": True,
        },
        "artifacts": {
            "pre": artifact_manifest,
            "post": artifact_manifest,
            "stable": True,
        },
        "selection": {
            "requested": categories,
            "available": categories,
            "complete": True,
        },
        "categories": [{
            "name": name,
            "framework": "pytest",
            "returncode": 0,
            "structured_output_valid": True,
            "result": {
                "framework": "pytest",
                "tests": 1,
                "testcases": 1,
                "passed": 1,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "skip_reasons": [],
            },
        } for name in categories],
        "fail_fast_omissions": [],
        "interrupted": False,
        "internal_error": None,
        "environment": verification_receipt.environment_metadata(
            cwd=candidate, environment={},
        ),
    }
    verification_receipt.write_atomic(
        receipt, verification_receipt.finalize(document),
    )
    return candidate, artifact, receipt


def test_session_budgets_are_consumed_once_and_cleanup_is_independent(tmp_path: Path) -> None:
    authorization, log = _authorization(tmp_path)
    receipt = authorization.execute(
        "scenario-one", call_budget=2, cost_budget_usd=0.2,
        required=["candidate_binding"],
    )
    assert receipt["candidate"] == authorization.candidate
    assert authorization.remaining_calls == 1
    assert authorization.remaining_cost_usd == pytest.approx(0.1)
    requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [item["scenario"] for item in requests] == ["scenario-one", "cleanup"]
    assert requests[0]["namespace"] == requests[1]["namespace"]
    assert requests[0]["call_budget"] == 1
    assert requests[1]["call_budget"] == 1
    with pytest.raises(AssertionError):
        authorization.execute("scenario-two", call_budget=2, cost_budget_usd=0.1)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_cleanup_runs_even_when_the_scenario_driver_fails(tmp_path: Path) -> None:
    authorization, log = _authorization(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        authorization.execute(
            "scenario-fail", call_budget=2, cost_budget_usd=0.1,
        )
    requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [item["scenario"] for item in requests] == ["scenario-fail", "cleanup"]
    assert requests[0]["namespace"] == requests[1]["namespace"]


def test_cleanup_reaches_bound_driver_after_candidate_drifts_during_scenario(
    tmp_path: Path,
) -> None:
    authorization, log = _authorization(tmp_path)
    checks = 0

    def drift_after_scenario_started() -> dict[str, str]:
        nonlocal checks
        checks += 1
        if checks == 1:
            return authorization.candidate
        raise GATE.LiveAuthorizationError("candidate mutated during scenario")

    authorization.candidate_recheck = drift_after_scenario_started
    with pytest.raises(
        GATE.LiveAuthorizationError, match="candidate mutated during scenario",
    ):
        authorization.execute(
            "scenario-one", call_budget=2, cost_budget_usd=0.1,
        )

    requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [item["scenario"] for item in requests] == ["scenario-one", "cleanup"]
    assert requests[1]["candidate"] == authorization.candidate
    assert requests[1]["namespace"] == requests[0]["namespace"]
    assert requests[1]["call_budget"] == 1
    assert requests[1]["cost_budget_usd"] == 0.0


def test_nonce_file_is_atomically_consumed_and_cannot_be_reused(tmp_path: Path) -> None:
    nonce = "a" * 64
    authorization_file = (tmp_path / "authorization.nonce").resolve()
    authorization_file.write_text(nonce + "\n", encoding="utf-8")
    consumed = GATE._consume_nonce(authorization_file, nonce)
    assert not authorization_file.exists()
    assert consumed.read_bytes() == b""
    with pytest.raises(pytest.skip.Exception):
        GATE._consume_nonce(authorization_file, nonce)


@pytest.mark.skipif(os.name == "nt", reason="POSIX link fixture")
@pytest.mark.parametrize("kind", ("symlink", "hardlink", "parent-symlink"))
def test_nonce_rejects_linked_paths_and_destroys_hardlinked_secret(
    tmp_path: Path, kind: str,
) -> None:
    nonce = "b" * 64
    original = tmp_path / "original.nonce"
    original.write_text(nonce + "\n", encoding="ascii")
    supplied = tmp_path / "authorization.nonce"
    if kind == "symlink":
        supplied.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, supplied)
    else:
        real_parent = tmp_path / "real"
        real_parent.mkdir()
        nested = real_parent / "authorization.nonce"
        nested.write_text(nonce + "\n", encoding="ascii")
        alias = tmp_path / "alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        supplied = alias / nested.name

    with pytest.raises(pytest.skip.Exception):
        GATE._consume_nonce(supplied, nonce)

    if kind == "hardlink":
        assert original.read_bytes() == b""
        assert supplied.read_bytes() == b""


def test_nonce_concurrency_has_exactly_one_winner(tmp_path: Path) -> None:
    nonce = "c" * 64
    path = (tmp_path / "one-use.nonce").resolve()
    path.write_text(nonce + "\n", encoding="ascii")
    barrier = threading.Barrier(8)
    outcomes: list[tuple[str, object]] = []

    def claim() -> None:
        barrier.wait()
        try:
            outcomes.append(("won", GATE._consume_nonce(path, nonce)))
        except pytest.skip.Exception as exc:
            outcomes.append(("rejected", exc))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    winners = [value for state, value in outcomes if state == "won"]
    assert len(winners) == 1
    assert Path(winners[0]).read_bytes() == b""
    assert len(outcomes) == 8


def test_nonce_swap_race_fails_closed_and_destroys_both_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "d" * 64
    path = (tmp_path / "one-use.nonce").resolve()
    displaced = tmp_path / "displaced.nonce"
    path.write_text(nonce + "\n", encoding="ascii")
    real_rename = GATE._durable_nonce_rename

    def swap_then_rename(source, destination):
        real_rename(source, displaced)
        Path(source).write_text(nonce + "\n", encoding="ascii")
        return real_rename(source, destination)

    monkeypatch.setattr(GATE, "_durable_nonce_rename", swap_then_rename)
    with pytest.raises(pytest.skip.Exception):
        GATE._consume_nonce(path, nonce)
    assert displaced.read_bytes() == b""


def test_nonce_base_exception_after_rename_leaves_no_reusable_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "f" * 64
    path = (tmp_path / "authorization.nonce").resolve()
    path.write_text(nonce + "\n", encoding="ascii")
    if os.name == "nt":
        real_rename = GATE._durable_nonce_rename
        patch_target = (GATE, "_durable_nonce_rename")
    else:
        real_rename = GATE.os.rename
        patch_target = (GATE.os, "rename")

    def rename_then_crash(source, destination):
        real_rename(source, destination)
        # On POSIX this interrupts before the directory fsync.
        raise SystemExit("injected crash after rename")

    monkeypatch.setattr(*patch_target, rename_then_crash)
    with pytest.raises(SystemExit, match="injected crash"):
        GATE._consume_nonce(path, nonce)

    claimed = list(tmp_path.glob(".authorization.nonce.consumed-*"))
    assert len(claimed) == 1
    assert claimed[0].read_bytes() == b""
    with pytest.raises(pytest.skip.Exception):
        GATE._consume_nonce(claimed[0], nonce)


def test_nonce_swap_immediately_after_poison_destroys_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "1" * 64
    path = (tmp_path / "authorization.nonce").resolve()
    displaced = tmp_path / "displaced-empty.nonce"
    path.write_text(nonce + "\n", encoding="ascii")
    real_poison = GATE._poison_file_descriptor
    swapped = False

    def poison_then_swap(fd: int) -> bool:
        nonlocal swapped
        result = real_poison(fd)
        if result and not swapped:
            swapped = True
            os.rename(path, displaced)
            path.write_text(nonce + "\n", encoding="ascii")
        return result

    monkeypatch.setattr(GATE, "_poison_file_descriptor", poison_then_swap)
    with pytest.raises(pytest.skip.Exception):
        GATE._consume_nonce(path, nonce)

    assert displaced.read_bytes() == b""
    assert not path.exists() or path.read_bytes() == b""
    if path.exists():
        with pytest.raises(pytest.skip.Exception):
            GATE._consume_nonce(path, nonce)


def test_nonce_open_race_never_truncates_unrelated_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "2" * 64
    path = (tmp_path / "authorization.nonce").resolve()
    displaced = tmp_path / "displaced-authorization.nonce"
    unrelated = b"operator data that is not an authorization nonce\n"
    path.write_text(nonce + "\n", encoding="ascii")
    real_open = GATE.os.open
    swapped = False

    def swap_before_open(target, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(target) == path:
            swapped = True
            os.rename(path, displaced)
            path.write_bytes(unrelated)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(GATE.os, "open", swap_before_open)
    with pytest.raises(pytest.skip.Exception):
        GATE._consume_nonce(path, nonce)

    assert path.read_bytes() == unrelated
    assert displaced.read_text(encoding="ascii") == nonce + "\n"


def test_nonce_rejects_a_reparse_or_junction_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "e" * 64
    path = (tmp_path / "one-use.nonce").resolve()
    path.write_text(nonce + "\n", encoding="ascii")
    path_type = type(path)
    real_is_junction = path_type.is_junction
    monkeypatch.setattr(
        path_type,
        "is_junction",
        lambda self: self == path or real_is_junction(self),
    )
    with pytest.raises(pytest.skip.Exception):
        GATE._consume_nonce(path, nonce)


def test_gate_validates_current_receipt_candidate_and_selected_artifact(
    tmp_path: Path,
) -> None:
    candidate, artifact, receipt = _qualified_candidate(tmp_path)
    binding = GATE._validated_candidate(
        artifact, receipt, product_root=candidate,
    )
    assert binding["artifact_sha256"] == verification_receipt.artifact_manifest(
        [artifact]
    )["files"][0]["sha256"]
    assert binding["verification_receipt_integrity"] == (
        verification_receipt.validate_receipt(receipt, require_qualifying=True)[
            "integrity"
        ]
    )

    other = tmp_path / "other.zip"
    other.write_bytes(b"not selected")
    with pytest.raises(GATE.LiveAuthorizationError):
        GATE._validated_candidate(other, receipt, product_root=candidate)
    artifact.write_bytes(b"mutated after qualification")
    with pytest.raises(GATE.LiveAuthorizationError):
        GATE._validated_candidate(artifact, receipt, product_root=candidate)


@pytest.mark.parametrize(
    "mutation", ("missing-receipt", "mutated-receipt", "mutated-candidate"),
)
def test_gate_rejects_missing_or_mutated_receipt_and_candidate(
    tmp_path: Path, mutation: str,
) -> None:
    candidate, artifact, receipt = _qualified_candidate(tmp_path)
    if mutation == "missing-receipt":
        receipt.unlink()
    elif mutation == "mutated-receipt":
        receipt.write_text(receipt.read_text(encoding="utf-8") + " ", encoding="utf-8")
    else:
        (candidate / "candidate.txt").write_text("mutated\n", encoding="utf-8")
    with pytest.raises(GATE.LiveAuthorizationError):
        GATE._validated_candidate(artifact, receipt, product_root=candidate)


def test_gate_rechecks_candidate_binding_before_every_driver_call(
    tmp_path: Path,
) -> None:
    authorization, log = _authorization(tmp_path)
    authorization.candidate_recheck = lambda: {
        **authorization.candidate,
        "artifact_sha256": "0" * 64,
    }
    with pytest.raises(GATE.LiveAuthorizationError):
        authorization._invoke({
            "schema": "memory-harness-live-qualification/v2",
            "scenario": "preflight",
            "candidate": authorization.candidate,
            "namespace": authorization.namespace,
            "call_budget": 0,
            "cost_budget_usd": 0.0,
            "timeout_seconds": 1,
            "authorization_nonce_sha256": authorization.nonce_digest,
            "required": [],
        })
    assert not log.exists()


def test_gate_rechecks_candidate_binding_after_driver_returns(tmp_path: Path) -> None:
    authorization, log = _authorization(tmp_path)
    calls = 0

    def recheck() -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return authorization.candidate
        return {**authorization.candidate, "verification_receipt_integrity": "0" * 64}

    authorization.candidate_recheck = recheck
    with pytest.raises(GATE.LiveAuthorizationError):
        authorization._invoke({
            "schema": "memory-harness-live-qualification/v2",
            "scenario": "preflight",
            "candidate": authorization.candidate,
            "namespace": authorization.namespace,
            "call_budget": 0,
            "cost_budget_usd": 0.0,
            "timeout_seconds": 1,
            "authorization_nonce_sha256": authorization.nonce_digest,
            "required": [],
        })
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1

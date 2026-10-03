"""Explicit, consumable, session-bounded authorization for live qualification."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest


CONFIRMATION = "I_AUTHORIZE_ONE_DISPOSABLE_RUN"
PRODUCT = Path(__file__).resolve().parents[3]
RECEIPT_SCHEMA = "memory-harness-live-qualification-receipt/v1"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("memory-harness-live-qualification")
    group.addoption("--authorize-live-qualification", action="store", default="")
    group.addoption("--live-authorization-nonce", action="store", default="")
    group.addoption("--live-authorization-file", action="store", default="")
    group.addoption("--live-candidate-artifact", action="store", default="")
    group.addoption("--live-namespace", action="store", default="")
    group.addoption("--live-call-budget", action="store", type=int, default=0)
    group.addoption("--live-cost-budget-usd", action="store", type=float, default=0.0)
    group.addoption("--live-timeout", action="store", type=int, default=0)


def _allowed_environment() -> dict[str, str]:
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP",
        "MEMORY_HARNESS_ATLAS_URI", "MEMORY_HARNESS_EVEROS_ENDPOINT",
        "MEMORY_HARNESS_EVEROS_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "QWEN_API_KEY",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=PRODUCT, text=True, capture_output=True,
        check=True, timeout=10,
    ).stdout.strip()


def _candidate(artifact: Path) -> dict[str, str]:
    dirty = _git("status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        pytest.skip("live qualification requires a clean, committed candidate tree")
    return {
        "commit": _git("rev-parse", "HEAD"),
        "tree": _git("rev-parse", "HEAD^{tree}"),
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }


def _consume_nonce(path: Path, nonce: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", nonce):
        pytest.skip("live qualification requires a 256-bit lowercase-hex nonce")
    if not path.is_absolute() or not path.is_file():
        pytest.skip("live qualification requires an absolute one-use authorization file")
    try:
        recorded = path.read_text(encoding="utf-8").strip()
    except OSError:
        pytest.skip("live authorization file cannot be read")
    if recorded != nonce:
        pytest.skip("live authorization nonce does not match its one-use file")
    consumed = path.with_name(f".{path.name}.consumed-{os.getpid()}-{time.time_ns()}")
    try:
        os.replace(path, consumed)
    except OSError:
        pytest.skip("live authorization was already consumed or cannot be claimed")
    return consumed


@dataclass
class LiveAuthorization:
    namespace: str
    remaining_calls: int
    remaining_cost_usd: float
    timeout: int
    driver: Path
    candidate: dict[str, str]
    nonce_digest: str
    deadline: float
    _used_scenarios: set[str] = field(default_factory=set)

    def _invoke(self, request: Mapping[str, Any]) -> dict[str, Any]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("live qualification exhausted its session time budget")
        requested_timeout = request.get("timeout_seconds")
        assert isinstance(requested_timeout, (int, float)) and requested_timeout > 0
        completed = subprocess.run(
            [str(self.driver)], cwd=PRODUCT, input=json.dumps(dict(request)),
            text=True, capture_output=True, check=True,
            timeout=max(1.0, min(float(self.timeout), remaining, float(requested_timeout))),
            env=_allowed_environment(),
        )
        receipt = json.loads(completed.stdout)
        assert isinstance(receipt, dict)
        assert receipt.get("schema") == RECEIPT_SCHEMA
        assert receipt.get("candidate") == self.candidate
        assert receipt.get("namespace") == request["namespace"]
        return receipt

    @staticmethod
    def _usage(receipt: Mapping[str, Any], calls: int, cost: float) -> None:
        observed_calls = receipt.get("calls")
        observed_cost = receipt.get("cost_usd")
        assert isinstance(observed_calls, int) and not isinstance(observed_calls, bool)
        assert 0 <= observed_calls <= calls
        assert isinstance(observed_cost, (int, float)) and not isinstance(observed_cost, bool)
        assert math.isfinite(observed_cost) and 0 <= observed_cost <= cost

    def execute(
        self, scenario: str, *, call_budget: int, cost_budget_usd: float,
        required: list[str] | None = None, extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one scenario and always demand independent namespace cleanup."""
        assert re.fullmatch(r"[a-z0-9-]{3,64}", scenario)
        assert scenario not in self._used_scenarios
        assert 2 <= call_budget <= self.remaining_calls
        assert 0 < cost_budget_usd <= self.remaining_cost_usd
        self._used_scenarios.add(scenario)
        self.remaining_calls -= call_budget
        self.remaining_cost_usd -= cost_budget_usd
        namespace = f"{self.namespace}-{scenario}"
        remaining_time = self.deadline - time.monotonic()
        assert remaining_time > 2.0
        cleanup_seconds = min(30.0, max(2.0, remaining_time / 5.0))
        cleanup_calls = 1
        main_calls = call_budget - cleanup_calls
        request: dict[str, Any] = {
            "schema": "memory-harness-live-qualification/v2",
            "scenario": scenario,
            "candidate": self.candidate,
            "namespace": namespace,
            "call_budget": main_calls,
            "cost_budget_usd": cost_budget_usd,
            "timeout_seconds": min(
                self.timeout, max(1.0, remaining_time - cleanup_seconds)
            ),
            "authorization_nonce_sha256": self.nonce_digest,
            "required": list(required or []),
        }
        if extra:
            assert not set(extra).intersection(request), (
                "scenario extras cannot override authorization-owned fields"
            )
            request.update(dict(extra))
        try:
            receipt = self._invoke(request)
            assert receipt.get("scenario") == scenario
            self._usage(receipt, main_calls, cost_budget_usd)
            return receipt
        finally:
            cleanup_request = {
                "schema": "memory-harness-live-qualification/v2",
                "scenario": "cleanup",
                "candidate": self.candidate,
                "namespace": namespace,
                "call_budget": cleanup_calls,
                "cost_budget_usd": 0.0,
                "timeout_seconds": min(self.timeout, cleanup_seconds),
                "authorization_nonce_sha256": self.nonce_digest,
                "required": ["cleanup"],
            }
            cleanup = self._invoke(cleanup_request)
            assert cleanup.get("scenario") == "cleanup"
            self._usage(cleanup, cleanup_calls, 0.0)
            assert cleanup.get("cleanup") == {
                "attempted": True, "complete": True, "remaining": [],
            }


@pytest.fixture(scope="session")
def live_authorization(pytestconfig: pytest.Config) -> LiveAuthorization:
    if pytestconfig.getoption("--authorize-live-qualification") != CONFIRMATION:
        pytest.skip("live qualification requires explicit CLI authorization")
    namespace = pytestconfig.getoption("--live-namespace")
    if not re.fullmatch(r"mhq-[a-z0-9-]{8,64}", namespace):
        pytest.skip("live qualification requires an exact disposable mhq-* namespace")
    calls = pytestconfig.getoption("--live-call-budget")
    cost = pytestconfig.getoption("--live-cost-budget-usd")
    timeout = pytestconfig.getoption("--live-timeout")
    if not 1 <= calls <= 100 or not 0 < cost <= 100 or not 1 <= timeout <= 3600:
        pytest.skip("live qualification requires bounded aggregate call, cost, and time budgets")
    raw_driver = os.environ.get("MEMORY_HARNESS_LIVE_QUALIFICATION_DRIVER", "")
    driver = Path(raw_driver).expanduser()
    if not driver.is_absolute() or not driver.is_file() or not os.access(driver, os.X_OK):
        pytest.skip("live qualification driver must be an absolute executable file")
    artifact = Path(pytestconfig.getoption("--live-candidate-artifact")).expanduser()
    if not artifact.is_absolute() or not artifact.is_file():
        pytest.skip("live qualification requires an absolute candidate artifact")
    candidate = _candidate(artifact)
    nonce = pytestconfig.getoption("--live-authorization-nonce")
    nonce_path = Path(pytestconfig.getoption("--live-authorization-file")).expanduser()
    consumed = _consume_nonce(nonce_path, nonce)
    authorization = LiveAuthorization(
        namespace=namespace, remaining_calls=calls, remaining_cost_usd=cost,
        timeout=timeout, driver=driver, candidate=candidate,
        nonce_digest=hashlib.sha256(nonce.encode("ascii")).hexdigest(),
        deadline=time.monotonic() + timeout,
    )
    try:
        preflight = authorization._invoke({
            "schema": "memory-harness-live-qualification/v2",
            "scenario": "preflight", "candidate": authorization.candidate,
            "namespace": namespace, "call_budget": 0, "cost_budget_usd": 0.0,
            "timeout_seconds": timeout,
            "authorization_nonce_sha256": authorization.nonce_digest,
            "required": ["budget_enforcement", "namespace_cleanup", "candidate_binding"],
        })
        assert preflight.get("scenario") == "preflight"
        authorization._usage(preflight, 0, 0.0)
        assert preflight.get("preflight") == {"ready": True}
        yield authorization
    finally:
        consumed.unlink(missing_ok=True)

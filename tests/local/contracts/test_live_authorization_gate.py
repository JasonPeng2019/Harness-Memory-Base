"""Mutation-sensitive contracts for the opt-in live qualification broker."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


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
    ), log


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


def test_nonce_file_is_atomically_consumed_and_cannot_be_reused(tmp_path: Path) -> None:
    nonce = "a" * 64
    authorization_file = (tmp_path / "authorization.nonce").resolve()
    authorization_file.write_text(nonce + "\n", encoding="utf-8")
    consumed = GATE._consume_nonce(authorization_file, nonce)
    assert not authorization_file.exists()
    assert consumed.read_text(encoding="utf-8").strip() == nonce
    with pytest.raises(pytest.skip.Exception):
        GATE._consume_nonce(authorization_file, nonce)

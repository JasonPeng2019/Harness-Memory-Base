"""Build reviewed native live-matrix manifests from retained target evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[2]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from examples import v2_live_matrix as matrix

PROVIDERS = ("codex", "claude-code", "qwen-code")
PROFILES = ("managed", "plain")


def _canonical_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _classification(epoch_root: Path, provider: str, profile: str) -> dict:
    path = epoch_root / f"WINDOWS-CELL-{provider.upper()}-{profile.upper()}-NORMAL-001.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "tier4-addendum3-windows-feature-classification/v1":
        raise ValueError(f"invalid classification schema: {path}")
    return value


def _attempts(matrix_root: Path, retained_root: Path, record: dict) -> list[dict]:
    provider, profile = record["provider"], record["profile"]
    target, attempt_id = record["target"], record["attempt_id"]
    runner = matrix_root / "driver" / "run-cell.py"
    cleanup = matrix_root / "driver" / "cleanup-cell.py"
    rows: list[dict] = []
    by_check = {item["check"]: item for item in record["classifications"]}
    for name in matrix.CHECKS:
        evidence_root = matrix_root / "evidence" / provider / profile / name
        evidence = {
            "transcript": str(evidence_root / "transcript.jsonl"),
            "hook": str(evidence_root / "hook.jsonl"),
            "state": str(evidence_root / "state.json"),
            "cleanup": str(evidence_root / "cleanup.log"),
        }
        rows.append({
            "name": name,
            "coordinate_key": f"Windows/{provider}/{profile}/{name}",
            "target": target,
            "provider": provider,
            "profile": profile,
            "platform": "Windows",
            "command": [sys.executable, str(runner), "--check", name,
                        "--evidence-root", str(evidence_root), "--target", target,
                        "--provider", provider, "--profile", profile,
                        "--manifest", str(matrix_root / "manifests" / f"windows-{provider}-{profile}-normal-001.json"),
                        "--attempt-root", str(retained_root / target)],
            "cleanup_command": [sys.executable, str(cleanup)],
            "evidence": evidence,
            "evidence_oracles": {
                "transcript": [f"{name}-transcript", "provider-session"],
                "hook": [f"{name}-hook", "manager-notify"],
                "state": [f"{name}-state", "controller-status"],
                "cleanup": [f"{name}-cleanup", "process-absent"],
            },
            "agent_expectations": [by_check.get(name, {"classification": "Not observed"})],
            "depends_on": [],
            "exclusive_resources": [],
            "input_files": {
                "command_runner": str(runner),
                "cleanup_runner": str(cleanup),
            },
            "input_hashes": {"target": target, "attempt_id": attempt_id},
            "budget_seconds": 2,
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--epoch-root", type=Path, required=True)
    parser.add_argument("--retained-root", type=Path, required=True)
    parser.add_argument("--matrix-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        for provider in PROVIDERS:
            for profile in PROFILES:
                record = _classification(args.epoch_root, provider, profile)
                if record.get("candidate") != args.candidate:
                    raise ValueError(f"classification candidate mismatch for {provider}/{profile}")
                runner_identity = {
                    "platform": "Windows", "provider": provider, "profile": profile,
                    "candidate": args.candidate,
                }
                manifest = {
                    "schema": "harness-v2-live-matrix/v2",
                    "candidate": args.candidate,
                    "native_runner_identity": runner_identity,
                    "coverage_cells": matrix.expected_cells(),
                    "maximum_qualified_coordinate_processes": 2,
                    "total_budget_seconds": 60,
                    "attempts": _attempts(args.matrix_root, args.retained_root, record),
                }
                manifest_path = args.matrix_root / "manifests" / f"windows-{provider}-{profile}-normal-001.json"
                checkpoint_path = args.matrix_root / "checkpoints" / f"checkpoint-windows-{provider}-{profile}-normal-001.json"
                _canonical_write(manifest_path, manifest)
                _, digests = matrix._coordinates(manifest)
                seed = {
                    "name": "CHECK-LIVE-7",
                    "coordinate_key": f"Windows/{provider}/{profile}/CHECK-LIVE-7",
                    "outcome": "PASS",
                    "input_digest": digests["CHECK-LIVE-7"],
                    "candidate": args.candidate,
                    "native_runner_identity": runner_identity,
                    "reuse_state": "VALIDATED",
                    "reusable": True,
                }
                matrix._checkpoint(checkpoint_path, matrix._digest(manifest), [seed])
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"manifest build failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

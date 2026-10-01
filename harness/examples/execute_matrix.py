"""Execute one or more reviewed Harness v2 live-matrix manifests.

This is the tracked, distributable outer entry point.  Runtime state and
checkpoints are always supplied explicitly; nothing is written into source.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples import v2_live_matrix as matrix

_SUCCESS = {"PASS", "GAP-NATIVE-MACOS", "GAP-NATIVE-LINUX"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", action="append", type=Path, required=True)
    parser.add_argument("--checkpoint", action="append", type=Path, required=True)
    parser.add_argument("--check", action="append", choices=tuple(matrix.CHECKS))
    parser.add_argument("--total-budget-seconds", type=float)
    args = parser.parse_args(argv)
    if len(args.manifest) != len(args.checkpoint):
        parser.error("each --manifest requires one corresponding --checkpoint")
    selected = set(args.check or ())
    combined: list[dict] = []
    try:
        for manifest_path, checkpoint_path in zip(args.manifest, args.checkpoint):
            manifest = matrix._load_manifest(manifest_path.resolve())
            if selected:
                full_attempts = list(manifest["attempts"])
                _coordinates, full_digests = matrix._coordinates(manifest)
                retained = {
                    row.get("name")
                    for row in matrix._prior_rows(checkpoint_path.resolve())
                    if isinstance(row, dict)
                    and row.get("input_digest") == full_digests.get(row.get("name"))
                }
                manifest = {
                    **manifest,
                    "attempts": [
                        item
                        for item in full_attempts
                        if item["name"] in selected or item["name"] in retained
                    ],
                }
                if not manifest["attempts"]:
                    continue
            if args.total_budget_seconds is not None:
                manifest["total_budget_seconds"] = args.total_budget_seconds
            result = matrix.execute(manifest, checkpoint_path.resolve())
            combined.extend(result["checks"])
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        print(f"live matrix did not run: {exc}", file=sys.stderr)
        return 2
    outcome = "PASS" if combined and all(row.get("outcome") in _SUCCESS for row in combined) else "FAIL"
    payload = {
        "schema": "harness-v2-live-matrix-result/v2",
        "outcome": outcome,
        "authorization": "M09",
        "checks": combined,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if outcome == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

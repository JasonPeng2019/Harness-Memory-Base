"""Validate generated live-matrix manifests and retained evidence bindings."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[2]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from examples import v2_live_matrix as matrix


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--epoch-root", type=Path, required=True)
    parser.add_argument("--retained-root", type=Path, required=True)
    parser.add_argument("--matrix-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifests = sorted((args.matrix_root / "manifests").glob("windows-*-normal-001.json"))
        if len(manifests) != 6:
            raise ValueError("expected six provider/profile manifests")
        for path in manifests:
            value = matrix._load_manifest(path)
            if value.get("candidate") != args.candidate:
                raise ValueError(f"candidate mismatch: {path}")
            for attempt in value["attempts"]:
                for consumed in attempt.get("input_files", {}).values():
                    if not Path(consumed).is_file():
                        raise ValueError(f"consumed runner missing: {consumed}")
            checkpoint = args.matrix_root / "checkpoints" / f"checkpoint-{path.name}"
            prior = matrix._prior_rows(checkpoint)
            if not any(row.get("name") == "CHECK-LIVE-7" for row in prior):
                raise ValueError(f"accepted CHECK-LIVE-7 seed missing: {checkpoint}")
        print(json.dumps({"ok": True, "manifests": len(manifests)}))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"manifest validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

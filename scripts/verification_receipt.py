#!/usr/bin/env python3
"""Candidate-bound verification receipt construction and validation.

The receipt is deliberately self-contained and uses only the standard library.
It is evidence of the exact local bytes that were tested, not a signature or a
substitute for an independently trusted publication channel.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "memory-harness-verification-receipt/v1"
PREREQUISITE_VARIABLES = (
    "EVEROS_ROOT",
    "MEMORY_HARNESS_ATLAS_URI",
    "MEMORY_HARNESS_ATLAS_LIVE_DATABASE",
    "MEMORY_HARNESS_ATLAS_RUN_TOKEN",
    "MEMORY_HARNESS_ATLAS_HANDOFF_MANIFEST_PATH",
    "MEMORY_HARNESS_EVEROS_ENDPOINT",
    "MEMORY_HARNESS_EVEROS_TOKEN",
    "MEMORY_HARNESS_LIVE_QUALIFICATION_DRIVER",
    "MEMORY_HARNESS_RUN_LIVE_ATLAS",
    "I_AUTHORIZE_ONE_DISPOSABLE_RUN",
    "MEMORY_HARNESS_APPROVAL_TOKEN",
    "MEMORY_HARNESS_CONTROL_TOKEN",
    "MEMORY_HARNESS_POLICY_MUTATION_CREDENTIAL",
    "MEMORY_HARNESS_POLICY_TOKEN",
    "MEMORY_HARNESS_PUBLICATION_KEY",
    "MEMORY_HARNESS_REVOCATION_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "QWEN_API_KEY",
)


class ReceiptError(ValueError):
    """A receipt, candidate, or declared artifact is invalid."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _entry(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    if path.is_symlink():
        data = os.readlink(path).encode("utf-8", "surrogateescape")
        return {
            "path": relative, "kind": "symlink", "size": len(data),
            "sha256": _sha256(data),
        }
    if not path.exists():
        return {"path": relative, "kind": "missing", "size": 0, "sha256": None}
    if not path.is_file():
        raise ReceiptError(f"candidate entry is not a file: {relative}")
    data = path.read_bytes()
    return {
        "path": relative, "kind": "file", "size": len(data),
        "sha256": _sha256(data),
    }


def candidate_manifest(root: Path) -> dict[str, Any]:
    """Hash every tracked or nonignored untracked candidate path."""

    root = root.resolve()
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=True,
    )
    names = sorted({
        item.decode("utf-8", "surrogateescape")
        for item in completed.stdout.split(b"\0") if item
    })
    entries = [_entry(root, name) for name in names]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
        capture_output=True, check=True,
    ).stdout.strip()
    return {
        "commit": commit,
        "files": entries,
        "aggregate_sha256": _sha256(_canonical(entries)),
    }


def artifact_manifest(
    paths: Sequence[Path], *, require_present: bool = True,
) -> dict[str, Any]:
    """Hash explicitly declared regular-file artifacts in stable path order."""

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for supplied in paths:
        path = supplied.expanduser().resolve()
        name = str(path)
        if name in seen:
            raise ReceiptError(f"artifact was declared more than once: {name}")
        seen.add(name)
        if path.is_symlink() or not path.is_file():
            if require_present:
                raise ReceiptError(f"artifact is not a plain file: {name}")
            entries.append({"path": name, "kind": "missing", "size": 0, "sha256": None})
            continue
        data = path.read_bytes()
        entries.append({
            "path": name, "kind": "file", "size": len(data), "sha256": _sha256(data),
        })
    entries.sort(key=lambda item: item["path"])
    return {
        "files": entries,
        "aggregate_sha256": _sha256(_canonical(entries)),
    }


def environment_metadata(
    *, cwd: Path, environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return only the release allowlist and presence-only prerequisites."""

    source = os.environ if environment is None else environment
    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable": str(Path(sys.executable).resolve()),
        },
        "host": {
            "os": os.name,
            "platform": sys.platform,
            "architecture": platform.machine(),
        },
        "cwd": str(cwd.resolve()),
        "prerequisite_presence": {
            name: bool(source.get(name)) for name in PREREQUISITE_VARIABLES
        },
    }


def seal(document: Mapping[str, Any]) -> dict[str, Any]:
    sealed = dict(document)
    sealed.pop("integrity", None)
    sealed["integrity"] = _sha256(_canonical(sealed))
    return sealed


def write_atomic(path: Path, document: Mapping[str, Any]) -> None:
    """Publish a complete receipt with restrictive permissions."""

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def assert_receipt_outside_candidate(receipt: Path, candidate_root: Path) -> Path:
    resolved = receipt.expanduser().resolve()
    try:
        resolved.relative_to(candidate_root.resolve())
    except ValueError:
        return resolved
    raise ReceiptError("receipt must be outside the candidate tree")


def _qualification_reasons(document: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    candidate = document.get("candidate", {})
    artifacts = document.get("artifacts", {})
    selection = document.get("selection", {})
    if not candidate.get("stable"):
        reasons.append("candidate-mutated-during-run")
    if not artifacts.get("stable"):
        reasons.append("artifact-mutated-during-run")
    if not selection.get("complete"):
        reasons.append("incomplete-category-selection")
    elif not artifacts.get("post", {}).get("files"):
        reasons.append("complete-release-requires-artifact")
    if document.get("interrupted"):
        reasons.append("run-interrupted")
    if document.get("fail_fast_omissions"):
        reasons.append("fail-fast-omitted-categories")
    for result in document.get("categories", []):
        name = result.get("name", "unknown")
        if not result.get("structured_output_valid"):
            reasons.append(f"missing-or-malformed-structured-output:{name}")
        else:
            try:
                counts = structured_counts(
                    str(result.get("framework")), result.get("result")
                )
            except ReceiptError:
                reasons.append(f"invalid-structured-counts:{name}")
            else:
                if counts["tests"] == 0:
                    reasons.append(f"zero-tests:{name}")
                elif counts["passed"] == 0:
                    reasons.append(f"no-substantive-passing-tests:{name}")
        if result.get("returncode") != 0:
            reasons.append(f"category-failed:{name}")
    if document.get("internal_error"):
        reasons.append("runner-internal-error")
    return sorted(set(reasons))


def _natural(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReceiptError(f"structured {field} must be a nonnegative integer")
    return value


def structured_counts(framework: str, result: Any) -> dict[str, int]:
    """Validate one framework result and return comparable exact counts."""

    if not isinstance(result, Mapping) or result.get("framework") != framework:
        raise ReceiptError("structured result framework mismatch")
    tests = _natural(result.get("tests"), "tests")
    passed = _natural(result.get("passed"), "passed")
    if framework == "pytest":
        if _natural(result.get("testcases"), "testcases") != tests:
            raise ReceiptError("pytest testcase details do not match its total")
        failures = _natural(result.get("failures"), "failures")
        errors = _natural(result.get("errors"), "errors")
        skipped = _natural(result.get("skipped"), "skipped")
        reasons = result.get("skip_reasons")
        if not isinstance(reasons, list) or len(reasons) != skipped:
            raise ReceiptError("pytest skip details do not match its skip count")
        accounted = passed + failures + errors + skipped
    elif framework == "unittest":
        sequences: dict[str, list[Any]] = {}
        for field in (
            "failures", "errors", "skipped", "expected_failures",
            "unexpected_successes",
        ):
            value = result.get(field)
            if not isinstance(value, list):
                raise ReceiptError(f"unittest {field} must be a list")
            sequences[field] = value
        failures = len(sequences["failures"])
        errors = len(sequences["errors"])
        skipped = len(sequences["skipped"])
        expected_failures = len(sequences["expected_failures"])
        unexpected_successes = len(sequences["unexpected_successes"])
        planned = _natural(result.get("planned"), "planned")
        if planned < tests:
            raise ReceiptError("unittest planned count is below tests run")
        if result.get("status") == "PASS" and planned != tests:
            raise ReceiptError("passing unittest result did not run every planned test")
        accounted = (
            passed + failures + errors + skipped
            + expected_failures + unexpected_successes
        )
    else:
        raise ReceiptError("unknown structured result framework")
    if accounted != tests:
        raise ReceiptError("structured result counts do not sum to total tests")
    return {
        "tests": tests, "passed": passed, "failures": failures,
        "errors": errors, "skipped": skipped,
    }


def unproved_coordinates(document: Mapping[str, Any]) -> list[dict[str, str]]:
    """Project framework skips as explicit unproved test coordinates."""

    unproved: list[dict[str, str]] = []
    for category in document.get("categories", []):
        if not isinstance(category, Mapping) or not category.get("structured_output_valid"):
            continue
        result = category.get("result")
        if not isinstance(result, Mapping):
            continue
        details = (
            result.get("skip_reasons", [])
            if category.get("framework") == "pytest"
            else result.get("skipped", [])
        )
        if not isinstance(details, list):
            continue
        for skipped in details:
            if isinstance(skipped, Mapping):
                unproved.append({
                    "category": str(category.get("name", "unknown")),
                    "coordinate": str(skipped.get("test", "unknown")),
                    "reason": str(skipped.get("reason", "unspecified skip")),
                    "status": "UNPROVED",
                })
    return unproved


def finalize(document: Mapping[str, Any]) -> dict[str, Any]:
    """Derive qualification state from evidence, then integrity-seal it."""

    result = dict(document)
    reasons = _qualification_reasons(result)
    result["qualification_reasons"] = reasons
    result["unproved_coordinates"] = unproved_coordinates(result)
    result["qualifying"] = not reasons
    if reasons:
        result["qualification_status"] = "NONQUALIFYING"
    elif result["unproved_coordinates"]:
        result["qualification_status"] = "QUALIFYING-WITH-UNPROVED-COORDINATES"
    else:
        result["qualification_status"] = "QUALIFYING"
    return seal(result)


def validate_receipt(path: Path, *, require_qualifying: bool = False) -> dict[str, Any]:
    """Validate self-integrity and current candidate/artifact byte bindings."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot read receipt: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ReceiptError("verification receipt schema mismatch")
    integrity = document.get("integrity")
    unsigned = dict(document)
    unsigned.pop("integrity", None)
    if not isinstance(integrity, str) or integrity != _sha256(_canonical(unsigned)):
        raise ReceiptError("verification receipt integrity mismatch")
    expected_reasons = _qualification_reasons(document)
    if document.get("qualification_reasons") != expected_reasons:
        raise ReceiptError("verification receipt qualification reasons are inconsistent")
    expected_qualifying = not expected_reasons
    if document.get("qualifying") is not expected_qualifying:
        raise ReceiptError("verification receipt qualification boolean is inconsistent")
    expected_unproved = unproved_coordinates(document)
    if document.get("unproved_coordinates") != expected_unproved:
        raise ReceiptError("verification receipt unproved coordinates are inconsistent")
    expected_status = (
        "NONQUALIFYING" if not expected_qualifying
        else "QUALIFYING-WITH-UNPROVED-COORDINATES" if expected_unproved
        else "QUALIFYING"
    )
    if document.get("qualification_status") != expected_status:
        raise ReceiptError("verification receipt qualification status is inconsistent")

    candidate = document.get("candidate")
    if not isinstance(candidate, dict):
        raise ReceiptError("verification receipt has no candidate binding")
    if candidate.get("stable") is not (candidate.get("pre") == candidate.get("post")):
        raise ReceiptError("verification receipt candidate stability is inconsistent")
    root = Path(candidate.get("root", ""))
    current_candidate = candidate_manifest(root)
    if current_candidate != candidate.get("post"):
        raise ReceiptError("candidate bytes no longer match the receipt")
    artifact_section = document.get("artifacts")
    if not isinstance(artifact_section, dict):
        raise ReceiptError("verification receipt has no artifact binding")
    if artifact_section.get("stable") is not (
        artifact_section.get("pre") == artifact_section.get("post")
    ):
        raise ReceiptError("verification receipt artifact stability is inconsistent")
    declared = [Path(item["path"]) for item in artifact_section.get("post", {}).get("files", [])]
    if artifact_manifest(declared, require_present=False) != artifact_section.get("post"):
        raise ReceiptError("artifact bytes no longer match the receipt")
    selection = document.get("selection")
    if not isinstance(selection, dict):
        raise ReceiptError("verification receipt has no category selection")
    requested = selection.get("requested")
    available = selection.get("available")
    if not isinstance(requested, list) or not isinstance(available, list):
        raise ReceiptError("verification receipt category selection is malformed")
    if selection.get("complete") is not (requested == available):
        raise ReceiptError("verification receipt category completeness is inconsistent")
    categories = document.get("categories")
    if not isinstance(categories, list):
        raise ReceiptError("verification receipt category results are malformed")
    executed = [item.get("name") for item in categories if isinstance(item, dict)]
    omissions = document.get("fail_fast_omissions")
    if not isinstance(omissions, list) or executed + omissions != requested:
        raise ReceiptError("verification receipt category execution order is inconsistent")
    for item in categories:
        if not isinstance(item, dict):
            raise ReceiptError("verification receipt category result is malformed")
        if item.get("structured_output_valid"):
            result = item.get("result")
            counts = structured_counts(str(item.get("framework")), result)
            if item.get("returncode") == 0 and (
                counts["failures"] != 0
                or counts["errors"] != 0
                or result.get("status", "PASS") != "PASS"
            ):
                raise ReceiptError("verification receipt successful status contradicts structured result")
    environment = document.get("environment")
    if not isinstance(environment, dict) or set(environment) != {
        "python", "host", "cwd", "prerequisite_presence",
    }:
        raise ReceiptError("verification receipt environment allowlist is invalid")
    presence = environment.get("prerequisite_presence")
    if not isinstance(presence, dict) or set(presence) != set(PREREQUISITE_VARIABLES):
        raise ReceiptError("verification receipt prerequisite allowlist is invalid")
    if any(not isinstance(value, bool) for value in presence.values()):
        raise ReceiptError("verification receipt prerequisite values must be presence-only booleans")
    if require_qualifying and not expected_qualifying:
        raise ReceiptError("verification receipt is nonqualifying")
    return document


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Validate a verification receipt.")
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--require-qualifying", action="store_true")
    args = parser.parse_args(argv)
    try:
        document = validate_receipt(args.receipt, require_qualifying=args.require_qualifying)
    except ReceiptError as exc:
        parser.error(str(exc))
    print(document["qualification_status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

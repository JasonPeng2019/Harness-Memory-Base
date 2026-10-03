#!/usr/bin/env python3
"""Run unittest discovery and emit a machine-readable result sidecar."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Sequence


SCHEMA = "memory-harness-unittest-result/v1"


class CapturingRunner(unittest.TextTestRunner):
    result: unittest.TestResult | None = None

    def _makeResult(self) -> unittest.TestResult:  # noqa: N802 - unittest API
        self.result = super()._makeResult()
        return self.result


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _test_id(test: object) -> str:
    identifier = getattr(test, "id", None)
    return identifier() if callable(identifier) else str(test)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-json", required=True, type=Path)
    parser.add_argument("--start-directory", required=True)
    parser.add_argument("--top-level-directory", required=True)
    parser.add_argument(
        "--import-root", required=True,
        help="exact parent of top-level-directory to expose for product-package imports",
    )
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args(argv)

    started = time.monotonic()
    planned = 0
    interrupted = False
    internal_error: str | None = None
    runner = CapturingRunner(verbosity=0)
    try:
        start_directory = Path(args.start_directory).resolve(strict=True)
        top_level_directory = Path(args.top_level_directory).resolve(strict=True)
        import_root = Path(args.import_root).resolve(strict=True)
        if not start_directory.is_dir() or not top_level_directory.is_dir():
            raise ValueError("unittest discovery paths must be directories")
        if import_root != top_level_directory.parent:
            raise ValueError(
                "unittest import root must be the exact parent of the top-level directory"
            )
        try:
            start_directory.relative_to(top_level_directory)
        except ValueError as exc:
            raise ValueError(
                "unittest start directory must stay beneath the top-level directory"
            ) from exc
        # unittest itself adds ``top_level_directory`` for harness packages.
        # Add only its explicitly declared parent so product-root packages
        # (notably ``tests.local``) resolve identically from any cwd.  Do not
        # infer or expose any broader ancestor.
        import_root_text = str(import_root)
        if import_root_text in sys.path:
            sys.path.remove(import_root_text)
        sys.path.insert(0, import_root_text)
        suite = unittest.defaultTestLoader.discover(
            start_dir=str(start_directory),
            pattern=args.pattern,
            top_level_dir=str(top_level_directory),
        )
        planned = suite.countTestCases()
        result = runner.run(suite)
    except KeyboardInterrupt:
        interrupted = True
        result = runner.result
    except BaseException as exc:  # a truthful sidecar must survive discovery errors
        internal_error = type(exc).__name__
        result = runner.result

    failures = [] if result is None else [_test_id(test) for test, _ in result.failures]
    errors = [] if result is None else [_test_id(test) for test, _ in result.errors]
    skipped = [] if result is None else [
        {"test": _test_id(test), "reason": reason}
        for test, reason in result.skipped
    ]
    expected_failures = [] if result is None else [
        _test_id(test) for test, _ in result.expectedFailures
    ]
    unexpected_successes = [] if result is None else [
        _test_id(test) for test in result.unexpectedSuccesses
    ]
    tests_run = 0 if result is None else result.testsRun
    successful = bool(
        result is not None and result.wasSuccessful()
        and not interrupted and internal_error is None
    )
    document = {
        "schema": SCHEMA,
        "framework": "unittest",
        "planned": planned,
        "tests": tests_run,
        "passed": max(
            0,
            tests_run - len(failures) - len(errors) - len(skipped)
            - len(expected_failures) - len(unexpected_successes),
        ),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "expected_failures": expected_failures,
        "unexpected_successes": unexpected_successes,
        "interrupted": interrupted,
        "internal_error": internal_error,
        "status": "PASS" if successful else "INTERRUPTED" if interrupted else "FAIL",
        "duration_seconds": time.monotonic() - started,
    }
    _write_atomic(args.result_json.resolve(), document)
    return 0 if successful else 130 if interrupted else 1


if __name__ == "__main__":
    raise SystemExit(main())

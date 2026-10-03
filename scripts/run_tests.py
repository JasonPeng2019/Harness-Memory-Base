#!/usr/bin/env python3
"""Run categorized product tests through one stable repository entrypoint."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

try:
    from scripts import verification_receipt
except ModuleNotFoundError:  # direct execution from outside the product root
    import verification_receipt  # type: ignore[no-redef]


SCHEMA = "memory-harness-test-suite/v1"
PRODUCT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PRODUCT_ROOT / "tests" / "suite_manifest.json"
TEST_ROOTS = (
    "tests",
    "harness/orchestrator_harness/tests",
    "harness/harness_watcher_implementation/tests",
)


class SuiteConfigurationError(ValueError):
    """The suite manifest or requested selection is invalid."""


@dataclass(frozen=True)
class Category:
    """One indivisible framework session in the unified test suite."""

    name: str
    description: str
    framework: str
    paths: tuple[str, ...]
    top_level: str | None = None


def _relative_directory(value: Any, *, field: str, product_root: Path) -> str:
    if not isinstance(value, str) or not value:
        raise SuiteConfigurationError(f"{field} must be a non-empty relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() in {"", "."}:
        raise SuiteConfigurationError(f"{field} must stay beneath the product root")
    normalized = pure.as_posix()
    path = (product_root / normalized).resolve()
    try:
        path.relative_to(product_root.resolve())
    except ValueError as exc:
        raise SuiteConfigurationError(f"{field} escapes the product root") from exc
    if not path.is_dir():
        raise SuiteConfigurationError(f"{field} does not exist: {normalized}")
    return normalized


def discover_test_modules(*, product_root: Path = PRODUCT_ROOT) -> set[str]:
    """Return every supported test module relative to the product root."""

    discovered: set[str] = set()
    root = product_root.resolve()
    for relative in TEST_ROOTS:
        test_root = (root / relative).resolve()
        if not test_root.is_dir():
            raise SuiteConfigurationError(f"test root does not exist: {relative}")
        for path in test_root.rglob("test*.py"):
            if path.is_file() and "__pycache__" not in path.parts:
                discovered.add(path.resolve().relative_to(root).as_posix())
    return discovered


def test_module_ownership(
    categories: Sequence[Category], *, product_root: Path = PRODUCT_ROOT,
) -> dict[str, tuple[str, ...]]:
    """Map every categorized test module to its category owner(s)."""

    root = product_root.resolve()
    ownership: dict[str, list[str]] = {}
    for category in categories:
        category_modules: set[str] = set()
        for relative in category.paths:
            path_root = (root / relative).resolve()
            for path in path_root.rglob("test*.py"):
                if path.is_file() and "__pycache__" not in path.parts:
                    module = path.resolve().relative_to(root).as_posix()
                    category_modules.add(module)
        if not category_modules:
            raise SuiteConfigurationError(
                f"category {category.name!r} contains no test modules"
            )
        for module in category_modules:
            ownership.setdefault(module, []).append(category.name)
    return {
        module: tuple(names)
        for module, names in sorted(ownership.items())
    }


def _validate_coverage(
    categories: Sequence[Category], *, product_root: Path,
) -> None:
    discovered = discover_test_modules(product_root=product_root)
    ownership = test_module_ownership(categories, product_root=product_root)
    missing = sorted(discovered - set(ownership))
    outside = sorted(set(ownership) - discovered)
    overlaps = {
        module: owners for module, owners in ownership.items() if len(owners) != 1
    }
    problems: list[str] = []
    if missing:
        problems.append("uncategorized test modules: " + ", ".join(missing))
    if outside:
        problems.append("categorized modules outside supported roots: " + ", ".join(outside))
    if overlaps:
        rendered = ", ".join(
            f"{module} ({'/'.join(owners)})"
            for module, owners in sorted(overlaps.items())
        )
        problems.append("multiply categorized test modules: " + rendered)
    if problems:
        raise SuiteConfigurationError("; ".join(problems))


def load_categories(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    product_root: Path = PRODUCT_ROOT,
) -> tuple[Category, ...]:
    """Load and fail-closed validate the suite manifest and coverage."""

    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SuiteConfigurationError(f"cannot read suite manifest: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "categories", "all"}:
        raise SuiteConfigurationError("suite manifest has unexpected top-level fields")
    if document.get("schema") != SCHEMA:
        raise SuiteConfigurationError("suite manifest schema mismatch")
    raw_categories = document.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise SuiteConfigurationError("suite manifest categories must be a non-empty list")

    categories: list[Category] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_categories):
        if not isinstance(raw, dict):
            raise SuiteConfigurationError(f"category {index} is not an object")
        framework = raw.get("framework")
        expected_fields = {"name", "description", "framework", "paths"}
        if framework == "unittest":
            expected_fields.add("top_level")
        if set(raw) != expected_fields:
            raise SuiteConfigurationError(
                f"category {index} has unexpected fields for {framework!r}"
            )
        name = raw.get("name")
        description = raw.get("description")
        paths = raw.get("paths")
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9-]*", name) is None:
            raise SuiteConfigurationError(f"category {index} has an invalid name")
        if name in names or name == "all":
            raise SuiteConfigurationError(f"duplicate or reserved category name: {name}")
        if not isinstance(description, str) or not description.strip():
            raise SuiteConfigurationError(f"category {name!r} has no description")
        if framework not in {"pytest", "unittest"}:
            raise SuiteConfigurationError(f"category {name!r} has an invalid framework")
        if not isinstance(paths, list) or not paths:
            raise SuiteConfigurationError(f"category {name!r} has no paths")
        normalized_paths = tuple(
            _relative_directory(
                value, field=f"category {name!r} path", product_root=product_root,
            )
            for value in paths
        )
        if len(set(normalized_paths)) != len(normalized_paths):
            raise SuiteConfigurationError(f"category {name!r} repeats a path")
        top_level = None
        if framework == "unittest":
            if len(normalized_paths) != 1:
                raise SuiteConfigurationError(
                    f"unittest category {name!r} must have one discovery root"
                )
            top_level = _relative_directory(
                raw.get("top_level"),
                field=f"category {name!r} top_level",
                product_root=product_root,
            )
        categories.append(Category(
            name=name,
            description=description.strip(),
            framework=framework,
            paths=normalized_paths,
            top_level=top_level,
        ))
        names.add(name)

    all_profile = document.get("all")
    ordered_names = [category.name for category in categories]
    if all_profile != ordered_names:
        raise SuiteConfigurationError(
            "the all profile must name every category exactly once in declaration order"
        )
    _validate_coverage(categories, product_root=product_root)
    return tuple(categories)


def command_for(
    category: Category, *, product_root: Path = PRODUCT_ROOT,
    structured_output: Path | None = None,
) -> list[str]:
    """Build one native framework command for an indivisible category."""

    root = product_root.resolve()
    if category.framework == "pytest":
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
        ]
        if structured_output is not None:
            command.append(f"--junitxml={structured_output.resolve()}")
        command.extend(str((root / path).resolve()) for path in category.paths)
        return command
    if category.framework == "unittest" and category.top_level is not None:
        if structured_output is not None:
            return [
                sys.executable,
                str((root / "scripts" / "unittest_result_runner.py").resolve()),
                "--result-json", str(structured_output.resolve()),
                "--start-directory", str((root / category.paths[0]).resolve()),
                "--top-level-directory", str((root / category.top_level).resolve()),
                "--import-root", str(root),
            ]
        return [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-q",
            "-s",
            str((root / category.paths[0]).resolve()),
            "-t",
            str((root / category.top_level).resolve()),
        ]
    raise SuiteConfigurationError(
        f"category {category.name!r} has an unsupported framework"
    )


def test_environment(
    *,
    product_root: Path = PRODUCT_ROOT,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an inherited environment with absolute product import roots."""

    environment = dict(os.environ if inherited is None else inherited)
    # The manifest and generated argv are the only authorities for collection.
    # Ambient pytest options/plugins can otherwise add selectors, switch to
    # collect-only, or inject authorization-related command-line flags while
    # still returning success for the declared category.
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    # Entry-point plugins are discovered independently of PYTEST_PLUGINS and
    # can silently rewrite or truncate collection while pytest still exits 0.
    # Force the pytest-owned kill switch even when the parent supplied a
    # hostile false value.
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    required = [
        str((product_root / "src").resolve()),
        str((product_root / "harness").resolve()),
    ]
    existing = environment.get("PYTHONPATH", "")
    values = required + ([existing] if existing else [])
    environment["PYTHONPATH"] = os.pathsep.join(values)
    return environment


def select_categories(
    requested: Sequence[str], categories: Sequence[Category],
) -> tuple[Category, ...]:
    """Resolve one exact, non-repeating category selection."""

    selection = list(requested) or ["all"]
    if len(set(selection)) != len(selection):
        raise SuiteConfigurationError("category selections may not repeat")
    if "all" in selection:
        if selection != ["all"]:
            raise SuiteConfigurationError("all cannot be combined with named categories")
        return tuple(categories)
    by_name = {category.name: category for category in categories}
    unknown = [name for name in selection if name not in by_name]
    if unknown:
        raise SuiteConfigurationError("unknown categories: " + ", ".join(unknown))
    return tuple(by_name[name] for name in selection)


Executor = Callable[..., Any]


@dataclass(frozen=True)
class CategoryOutcome:
    name: str
    framework: str
    argv: tuple[str, ...]
    returncode: int
    duration_seconds: float
    interrupted: bool
    structured_output_valid: bool
    result: Mapping[str, Any] | None


def run_categories(
    categories: Sequence[Category],
    *,
    product_root: Path = PRODUCT_ROOT,
    executor: Executor = subprocess.run,
    fail_fast: bool = False,
) -> int:
    """Run selected categories and return one aggregate process status."""

    outcomes, interrupted, _ = _execute_categories(
        categories,
        product_root=product_root,
        executor=executor,
        fail_fast=fail_fast,
        structured_directory=None,
    )
    _print_summary(outcomes)
    if interrupted:
        return 130
    return 1 if any(outcome.returncode != 0 for outcome in outcomes) else 0


def _redaction_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    marker = re.compile(r"(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.I)
    values = {
        value for name, value in environment.items()
        if value and (
            marker.search(name)
            or name in verification_receipt.PREREQUISITE_VARIABLES
        )
    }
    return tuple(sorted(values, key=len, reverse=True))


def _redact(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


def _parse_pytest_result(path: Path) -> dict[str, Any]:
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise ValueError("pytest JUnit document has no testsuite")
    counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    duration = 0.0
    testcase_count = 0
    skip_reasons: list[dict[str, str]] = []
    for suite in suites:
        for name in counts:
            counts[name] += int(suite.attrib.get(name, "0"))
        duration += float(suite.attrib.get("time", "0"))
        cases = suite.findall(".//testcase")
        testcase_count += len(cases)
        for case in cases:
            skipped = case.find("skipped")
            if skipped is not None:
                skip_reasons.append({
                    "test": "::".join(filter(None, (
                        case.attrib.get("classname", ""), case.attrib.get("name", ""),
                    ))),
                    "reason": skipped.attrib.get("message") or (skipped.text or "").strip(),
                })
    passed = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return {
        "schema": "memory-harness-pytest-result/v1",
        "framework": "pytest",
        **counts,
        "testcases": testcase_count,
        "passed": max(0, passed),
        "skip_reasons": skip_reasons,
        "duration_seconds": duration,
    }


def _parse_structured(category: Category, path: Path) -> dict[str, Any]:
    if category.framework == "pytest":
        document = _parse_pytest_result(path)
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema") != (
            "memory-harness-unittest-result/v1"
        ):
            raise ValueError("unittest structured result schema mismatch")
    verification_receipt.structured_counts(category.framework, document)
    return document


def _execute_categories(
    categories: Sequence[Category],
    *,
    product_root: Path,
    executor: Executor,
    fail_fast: bool,
    structured_directory: Path | None,
) -> tuple[list[CategoryOutcome], bool, list[str]]:
    outcomes: list[CategoryOutcome] = []
    environment = test_environment(product_root=product_root)
    secrets = _redaction_values(environment)
    interrupted = False
    for index, category in enumerate(categories, start=1):
        sidecar = None
        if structured_directory is not None:
            extension = ".xml" if category.framework == "pytest" else ".json"
            sidecar = structured_directory / f"{index:02d}-{category.name}{extension}"
        command = command_for(
            category, product_root=product_root, structured_output=sidecar,
        )
        print(
            f"\n=== [{index}/{len(categories)}] {category.name}: "
            f"{category.description} ===",
            flush=True,
        )
        print("$ " + shlex.join(command), flush=True)
        started = time.monotonic()
        try:
            completed = executor(
                command,
                cwd=product_root.resolve(),
                env=environment,
            )
        except KeyboardInterrupt:
            elapsed = time.monotonic() - started
            parsed = None
            valid = False
            if sidecar is not None and sidecar.is_file():
                try:
                    parsed = _redact(_parse_structured(category, sidecar), secrets)
                    valid = True
                except (OSError, UnicodeDecodeError, ValueError, ET.ParseError, json.JSONDecodeError):
                    pass
            outcomes.append(CategoryOutcome(
                category.name, category.framework, tuple(command), 130, elapsed,
                True, valid, parsed,
            ))
            print(f"\nINTERRUPTED: {category.name}", file=sys.stderr, flush=True)
            interrupted = True
            break
        returncode = int(completed.returncode)
        parsed = None
        valid = structured_directory is None
        if sidecar is not None and sidecar.is_file():
            try:
                parsed = _redact(_parse_structured(category, sidecar), secrets)
                valid = True
            except (OSError, UnicodeDecodeError, ValueError, ET.ParseError, json.JSONDecodeError):
                valid = False
        outcomes.append(CategoryOutcome(
            category.name, category.framework, tuple(command), returncode,
            time.monotonic() - started, False, valid, parsed,
        ))
        if returncode != 0 and fail_fast:
            break
    omitted = [category.name for category in categories[len(outcomes):]]
    return outcomes, interrupted, omitted


def _print_summary(outcomes: Sequence[CategoryOutcome]) -> None:
    print("\n=== Unified test summary ===", flush=True)
    for outcome in outcomes:
        status = "PASS" if outcome.returncode == 0 else f"FAIL ({outcome.returncode})"
        print(
            f"{outcome.name:14} {status:12} {outcome.duration_seconds:8.2f}s",
            flush=True,
        )


def run_categories_with_receipt(
    categories: Sequence[Category],
    *,
    all_categories: Sequence[Category],
    receipt_path: Path,
    artifact_paths: Sequence[Path],
    product_root: Path = PRODUCT_ROOT,
    executor: Executor = subprocess.run,
    fail_fast: bool = False,
) -> int:
    """Run categories and atomically publish candidate-bound evidence."""

    receipt = verification_receipt.assert_receipt_outside_candidate(
        receipt_path, product_root,
    )
    receipt.parent.mkdir(parents=True, exist_ok=True)
    pre_candidate = verification_receipt.candidate_manifest(product_root)
    pre_artifacts = verification_receipt.artifact_manifest(artifact_paths)
    start_wall = dt.datetime.now(dt.timezone.utc)
    outcomes: list[CategoryOutcome] = []
    interrupted = False
    omitted: list[str] = []
    internal_error: str | None = None
    sidecars = Path(tempfile.mkdtemp(prefix=f".{receipt.name}.results-", dir=receipt.parent))
    try:
        try:
            outcomes, interrupted, omitted = _execute_categories(
                categories,
                product_root=product_root,
                executor=executor,
                fail_fast=fail_fast,
                structured_directory=sidecars,
            )
        except BaseException as exc:
            internal_error = type(exc).__name__
            interrupted = isinstance(exc, KeyboardInterrupt)
            omitted = [category.name for category in categories[len(outcomes):]]
        finally:
            post_candidate = verification_receipt.candidate_manifest(product_root)
            post_artifacts = verification_receipt.artifact_manifest(
                artifact_paths, require_present=False,
            )
        _print_summary(outcomes)
        finish_wall = dt.datetime.now(dt.timezone.utc)
        selected_names = [category.name for category in categories]
        all_names = [category.name for category in all_categories]
        document = {
            "schema": verification_receipt.SCHEMA,
            "started_at": start_wall.isoformat(),
            "finished_at": finish_wall.isoformat(),
            "duration_seconds": (finish_wall - start_wall).total_seconds(),
            "candidate": {
                "root": str(product_root.resolve()),
                "pre": pre_candidate,
                "post": post_candidate,
                "stable": pre_candidate == post_candidate,
            },
            "artifacts": {
                "pre": pre_artifacts,
                "post": post_artifacts,
                "stable": pre_artifacts == post_artifacts,
            },
            "selection": {
                "requested": selected_names,
                "complete": selected_names == all_names,
                "available": all_names,
            },
            "categories": [
                {
                    "name": outcome.name,
                    "framework": outcome.framework,
                    "argv": list(outcome.argv),
                    "returncode": outcome.returncode,
                    "duration_seconds": outcome.duration_seconds,
                    "interrupted": outcome.interrupted,
                    "structured_output_valid": outcome.structured_output_valid,
                    "result": outcome.result,
                }
                for outcome in outcomes
            ],
            "fail_fast": fail_fast,
            "fail_fast_omissions": omitted,
            "interrupted": interrupted,
            "internal_error": internal_error,
            "environment": verification_receipt.environment_metadata(cwd=product_root),
        }
        finalized = verification_receipt.finalize(document)
        verification_receipt.write_atomic(receipt, finalized)
        return 0 if finalized["qualifying"] else 130 if interrupted else 1
    finally:
        shutil.rmtree(sidecars, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run categorized memory-harness product tests.",
    )
    parser.add_argument(
        "categories",
        nargs="*",
        metavar="CATEGORY",
        help="one or more category names, or all (default: all)",
    )
    parser.add_argument(
        "--list", action="store_true", help="list categories without running tests",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print selected commands without running tests",
    )
    parser.add_argument(
        "--fail-fast", action="store_true", help="stop after the first failing category",
    )
    parser.add_argument(
        "--receipt", type=Path,
        help="atomically write a candidate-bound receipt outside the product tree",
    )
    parser.add_argument(
        "--artifact", action="append", default=[], type=Path,
        help="artifact to hash before and after the run (repeatable; requires --receipt)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        categories = load_categories()
        selected = select_categories(args.categories, categories)
    except SuiteConfigurationError as exc:
        parser.error(str(exc))
    if args.list:
        print("all\n    Every category below, in declaration order.")
        for category in categories:
            print(f"{category.name}\n    {category.description}")
        return 0
    if args.dry_run:
        for category in selected:
            print(f"{category.name}: {shlex.join(command_for(category))}")
        return 0
    if args.artifact and args.receipt is None:
        parser.error("--artifact requires --receipt")
    if args.receipt is not None:
        try:
            return run_categories_with_receipt(
                selected,
                all_categories=categories,
                receipt_path=args.receipt,
                artifact_paths=args.artifact,
                fail_fast=args.fail_fast,
            )
        except verification_receipt.ReceiptError as exc:
            parser.error(str(exc))
    return run_categories(selected, fail_fast=args.fail_fast)


if __name__ == "__main__":
    raise SystemExit(main())

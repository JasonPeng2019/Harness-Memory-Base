"""Contracts for the categorized, single-entrypoint product test suite."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PRODUCT = Path(__file__).resolve().parents[3]
RUNNER = PRODUCT / "scripts" / "run_tests.py"
MANIFEST = PRODUCT / "tests" / "suite_manifest.json"
EXPECTED_CATEGORIES = (
    "contracts",
    "memory",
    "planning",
    "lifecycle",
    "privacy",
    "recovery",
    "usage",
    "platform",
    "distribution",
    "live",
    "harness",
    "watcher",
)


def _runner():
    assert RUNNER.is_file(), "the unified test runner is missing"
    spec = importlib.util.spec_from_file_location("memory_harness_test_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_declares_the_complete_ordered_category_surface() -> None:
    assert MANIFEST.is_file(), "the categorized suite manifest is missing"
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert set(document) == {"schema", "categories", "all"}
    assert document["schema"] == "memory-harness-test-suite/v1"
    names = tuple(category["name"] for category in document["categories"])
    assert names == EXPECTED_CATEGORIES
    assert tuple(document["all"]) == EXPECTED_CATEGORIES
    assert all(category["description"].strip() for category in document["categories"])


def test_every_test_module_is_covered_by_exactly_one_category() -> None:
    runner = _runner()
    categories = runner.load_categories(MANIFEST, product_root=PRODUCT)
    ownership = runner.test_module_ownership(categories, product_root=PRODUCT)
    discovered = runner.discover_test_modules(product_root=PRODUCT)
    assert set(ownership) == discovered
    assert all(len(owners) == 1 for owners in ownership.values())
    spec = (PRODUCT / "harness/docs/product/FULL_PRODUCT_SPEC.md").read_text(
        encoding="utf-8"
    )
    claimed_total = re.search(r"\*\*(\d+) discovered test modules\*\*", spec)
    assert claimed_total is not None
    assert int(claimed_total.group(1)) == len(discovered)

    guide = (PRODUCT / "TESTING.md").read_text(encoding="utf-8")
    category_counts = {
        category.name: sum(
            category.name in owners for owners in ownership.values()
        )
        for category in categories
    }
    for name, claimed in re.findall(
        r"^\| `([^`]+)` \| (\d+) \|", guide, flags=re.MULTILINE
    ):
        assert name in category_counts
        assert int(claimed) == category_counts[name]


def test_commands_preserve_one_native_framework_session_per_category() -> None:
    runner = _runner()
    categories = runner.load_categories(MANIFEST, product_root=PRODUCT)
    commands = {
        category.name: runner.command_for(category, product_root=PRODUCT)
        for category in categories
    }
    for command in commands.values():
        assert command[0] == sys.executable
        assert all(not argument.startswith("PYTHONPATH=") for argument in command)
    live = commands["live"]
    assert live[:4] == [sys.executable, "-m", "pytest", "-q"]
    assert live[4:] == [str((PRODUCT / "tests/live").resolve())]
    for name in ("harness", "watcher"):
        command = commands[name]
        assert command[:4] == [sys.executable, "-m", "unittest", "discover"]
        assert command.count("-s") == command.count("-t") == 1
        assert Path(command[command.index("-s") + 1]).is_absolute()
        assert Path(command[command.index("-t") + 1]).is_absolute()
    environment = runner.test_environment(
        product_root=PRODUCT,
        inherited={
            "PYTEST_ADDOPTS": "--collect-only tests/local/contracts",
            "PYTEST_PLUGINS": "hostile_selection_plugin",
            "INHERITED_SENTINEL": "kept",
        },
    )
    python_paths = environment["PYTHONPATH"].split(os.pathsep)
    assert python_paths == [
        str((PRODUCT / "src").resolve()),
        str((PRODUCT / "harness").resolve()),
    ]
    assert "PYTEST_ADDOPTS" not in environment
    assert "PYTEST_PLUGINS" not in environment
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["INHERITED_SENTINEL"] == "kept"

    structured = runner.command_for(
        next(category for category in categories if category.name == "harness"),
        product_root=PRODUCT,
        structured_output=PRODUCT.parent / "harness-results.json",
    )
    assert structured[1] == str((PRODUCT / "scripts/unittest_result_runner.py").resolve())
    assert structured[structured.index("--import-root") + 1] == str(PRODUCT.resolve())
    assert structured[structured.index("--top-level-directory") + 1] == str(
        (PRODUCT / "harness").resolve()
    )


def test_cli_resolves_the_product_from_an_unrelated_working_directory(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--dry-run", "contracts"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("contracts:")
    assert str((PRODUCT / "tests/local/contracts").resolve()) in completed.stdout


def test_selection_rejects_duplicates_and_keeps_all_indivisible() -> None:
    runner = _runner()
    categories = runner.load_categories(MANIFEST, product_root=PRODUCT)
    assert tuple(category.name for category in runner.select_categories(["all"], categories)) == (
        EXPECTED_CATEGORIES
    )
    assert tuple(
        category.name
        for category in runner.select_categories(["privacy", "usage"], categories)
    ) == ("privacy", "usage")
    for selection in (
        ["all", "contracts"],
        ["contracts", "contracts"],
        ["unknown"],
    ):
        with pytest.raises(runner.SuiteConfigurationError):
            runner.select_categories(selection, categories)


@pytest.mark.parametrize(
    "change",
    ("invalid-framework", "incomplete-all", "overlap", "uncategorized"),
)
def test_manifest_validation_rejects_incomplete_or_ambiguous_coverage(
    tmp_path: Path, change: str,
) -> None:
    runner = _runner()
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if change == "invalid-framework":
        document["categories"][0]["framework"] = "custom"
    elif change == "incomplete-all":
        document["all"].pop()
    elif change == "overlap":
        document["categories"][1]["paths"].append("tests/local/contracts")
    else:
        document["categories"] = document["categories"][1:]
        document["all"] = document["all"][1:]
    changed = tmp_path / "suite_manifest.json"
    changed.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(runner.SuiteConfigurationError):
        runner.load_categories(changed, product_root=PRODUCT)


class _Executor:
    def __init__(self, returncodes: list[int] | None = None, *, interrupt_at: int | None = None):
        self.returncodes = list(returncodes or [])
        self.interrupt_at = interrupt_at
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(self, command, *, cwd, env):
        self.calls.append((list(command), Path(cwd), dict(env)))
        if self.interrupt_at == len(self.calls):
            raise KeyboardInterrupt
        returncode = self.returncodes.pop(0) if self.returncodes else 0
        return SimpleNamespace(returncode=returncode)


def test_execution_aggregates_failures_and_fail_fast_and_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    categories = runner.load_categories(MANIFEST, product_root=PRODUCT)[:3]
    monkeypatch.setenv(
        "PYTEST_ADDOPTS",
        "--collect-only tests/local/contracts/test_test_suite_runner.py",
    )
    monkeypatch.setenv("PYTEST_PLUGINS", "hostile_selection_plugin")

    continuing = _Executor([0, 7, 0])
    result = runner.run_categories(
        categories,
        product_root=PRODUCT,
        executor=continuing,
        fail_fast=False,
    )
    assert result == 1
    assert len(continuing.calls) == 3

    stopping = _Executor([0, 7, 0])
    result = runner.run_categories(
        categories,
        product_root=PRODUCT,
        executor=stopping,
        fail_fast=True,
    )
    assert result == 1
    assert len(stopping.calls) == 2

    interrupted = _Executor(interrupt_at=2)
    result = runner.run_categories(
        categories,
        product_root=PRODUCT,
        executor=interrupted,
        fail_fast=False,
    )
    assert result == 130
    assert len(interrupted.calls) == 2
    assert all(
        "PYTEST_ADDOPTS" not in environment
        and "PYTEST_PLUGINS" not in environment
        and environment.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
        for executor in (continuing, stopping, interrupted)
        for _, _, environment in executor.calls
    )


def test_real_ambient_pytest_entry_point_cannot_truncate_runner_collection(
    tmp_path: Path,
) -> None:
    runner = _runner()
    candidate = tmp_path / "candidate"
    tests = candidate / "tests/category"
    tests.mkdir(parents=True)
    (candidate / "src").mkdir()
    (candidate / "harness").mkdir()
    (tests / "test_complete.py").write_text(
        "def test_first(): pass\ndef test_second(): pass\n",
        encoding="utf-8",
    )

    plugins = tmp_path / "ambient-plugins"
    plugins.mkdir()
    (plugins / "hostile_truncation.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    del items[1:]\n",
        encoding="utf-8",
    )
    metadata = plugins / "hostile_truncation-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: hostile-truncation\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (metadata / "entry_points.txt").write_text(
        "[pytest11]\nhostile-truncation = hostile_truncation\n",
        encoding="utf-8",
    )

    category = runner.Category(
        "category", "entry-point isolation", "pytest", ("tests/category",),
    )
    hostile_xml = tmp_path / "hostile.xml"
    hostile_command = runner.command_for(
        category, product_root=candidate, structured_output=hostile_xml,
    )
    inherited = dict(os.environ)
    inherited.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    inherited.pop("PYTEST_ADDOPTS", None)
    inherited.pop("PYTEST_PLUGINS", None)
    inherited["PYTHONPATH"] = str(plugins)
    hostile = subprocess.run(
        hostile_command, cwd=candidate, env=inherited,
        text=True, capture_output=True, timeout=30,
    )
    assert hostile.returncode == 0, hostile.stderr
    assert runner._parse_pytest_result(hostile_xml)["tests"] == 1

    protected_xml = tmp_path / "protected.xml"
    protected_command = runner.command_for(
        category, product_root=candidate, structured_output=protected_xml,
    )
    # A hostile inherited value must be overridden, not merely defaulted.
    inherited["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "0"
    protected = subprocess.run(
        protected_command,
        cwd=candidate,
        env=runner.test_environment(product_root=candidate, inherited=inherited),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert protected.returncode == 0, protected.stderr
    result = runner._parse_pytest_result(protected_xml)
    assert result["tests"] == result["testcases"] == result["passed"] == 2


def _candidate_repository(root: Path) -> None:
    (root / "tests/one").mkdir(parents=True)
    (root / "tests/two").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "harness").mkdir()
    (root / "tests/one/test_one.py").write_text("def test_one(): pass\n", encoding="utf-8")
    (root / "tests/two/test_two.py").write_text("def test_two(): pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Receipt Test", "-c", "user.email=receipt@example.invalid",
         "commit", "-qm", "candidate"],
        cwd=root, check=True,
    )


class _StructuredExecutor:
    def __init__(
        self, returncodes: list[int] | None = None, *, secret: str = "",
        mutate=None, interrupt: bool = False, malformed: bool = False,
        pytest_xml: str | None = None,
    ) -> None:
        self.returncodes = list(returncodes or [])
        self.secret = secret
        self.mutate = mutate
        self.interrupt = interrupt
        self.malformed = malformed
        self.pytest_xml = pytest_xml
        self.calls = 0

    def __call__(self, command, *, cwd, env):
        self.calls += 1
        if self.interrupt:
            raise KeyboardInterrupt
        result_path = None
        framework = "pytest"
        for index, argument in enumerate(command):
            if argument.startswith("--junitxml="):
                result_path = Path(argument.split("=", 1)[1])
            elif argument == "--result-json":
                framework = "unittest"
                result_path = Path(command[index + 1])
        assert result_path is not None
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if self.malformed:
            result_path.write_text("not structured output", encoding="utf-8")
        elif framework == "pytest":
            result_path.write_text(self.pytest_xml or (
                '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="1" time="0.2">'
                '<testcase classname="suite" name="pass"/><testcase classname="suite" name="skip">'
                f'<skipped message="missing {self.secret}"/></testcase></testsuite></testsuites>'
            ), encoding="utf-8")
        else:
            result_path.write_text(json.dumps({
                "schema": "memory-harness-unittest-result/v1",
                "framework": "unittest", "planned": 2, "tests": 2, "passed": 1,
                "failures": [], "errors": [],
                "skipped": [{"test": "suite.skip", "reason": f"missing {self.secret}"}],
                "expected_failures": [], "unexpected_successes": [],
                "interrupted": False, "internal_error": None,
                "status": "PASS", "duration_seconds": 0.2,
            }), encoding="utf-8")
        if self.mutate is not None:
            self.mutate(self.calls)
        return SimpleNamespace(returncode=self.returncodes.pop(0) if self.returncodes else 0)


def test_receipt_is_candidate_bound_structured_atomic_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _candidate_repository(candidate)
    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"artifact-v1")
    receipt = tmp_path / "receipt.json"
    secret = "mongodb+srv://atlas-user:atlas-password@example.invalid/db"
    monkeypatch.setenv("MEMORY_HARNESS_ATLAS_URI", secret)
    categories = (
        runner.Category("one", "one", "pytest", ("tests/one",)),
        runner.Category("two", "two", "unittest", ("tests/two",), "tests"),
    )

    status = runner.run_categories_with_receipt(
        categories, all_categories=categories, receipt_path=receipt,
        artifact_paths=[artifact], product_root=candidate,
        executor=_StructuredExecutor(secret=secret),
    )
    assert status == 0
    raw = receipt.read_text(encoding="utf-8")
    assert secret not in raw
    document = json.loads(raw)
    assert document["qualifying"] is True
    assert document["candidate"]["stable"] is True
    assert document["artifacts"]["stable"] is True
    assert [item["result"]["tests"] for item in document["categories"]] == [2, 2]
    assert [item["result"]["passed"] for item in document["categories"]] == [1, 1]
    assert all(item["structured_output_valid"] for item in document["categories"])
    assert set(document["environment"]) == {"python", "host", "cwd", "prerequisite_presence"}
    assert all(isinstance(value, bool) for value in document["environment"]["prerequisite_presence"].values())
    assert document["qualification_status"] == "QUALIFYING-WITH-UNPROVED-COORDINATES"
    assert document["unproved_coordinates"] == [
        {
            "category": "one", "coordinate": "suite::skip",
            "reason": "missing [REDACTED]", "status": "UNPROVED",
        },
        {
            "category": "two", "coordinate": "suite.skip",
            "reason": "missing [REDACTED]", "status": "UNPROVED",
        },
    ]
    assert runner.verification_receipt.validate_receipt(
        receipt, require_qualifying=True,
    )["qualification_status"] == "QUALIFYING-WITH-UNPROVED-COORDINATES"

    tampered = json.loads(raw)
    tampered["categories"][0]["returncode"] = 9
    receipt.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(runner.verification_receipt.ReceiptError, match="integrity"):
        runner.verification_receipt.validate_receipt(receipt)


def test_receipt_detects_candidate_and_artifact_mutation(tmp_path: Path) -> None:
    runner = _runner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _candidate_repository(candidate)
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"before")
    receipt = tmp_path / "receipt.json"
    categories = (runner.Category("one", "one", "pytest", ("tests/one",)),)

    def mutate(_: int) -> None:
        (candidate / "mutated.py").write_text("changed = True\n", encoding="utf-8")
        artifact.write_bytes(b"after")

    status = runner.run_categories_with_receipt(
        categories, all_categories=categories, receipt_path=receipt,
        artifact_paths=[artifact], product_root=candidate,
        executor=_StructuredExecutor(mutate=mutate),
    )
    assert status == 1
    document = runner.verification_receipt.validate_receipt(receipt)
    assert document["qualification_status"] == "NONQUALIFYING"
    assert document["qualification_reasons"] == [
        "artifact-mutated-during-run", "candidate-mutated-during-run",
    ]
    with pytest.raises(runner.verification_receipt.ReceiptError, match="nonqualifying"):
        runner.verification_receipt.validate_receipt(receipt, require_qualifying=True)

    artifact.write_bytes(b"tampered-again")
    with pytest.raises(runner.verification_receipt.ReceiptError, match="artifact bytes"):
        runner.verification_receipt.validate_receipt(receipt)


def test_receipt_truthfully_records_fail_fast_malformed_and_interrupt(tmp_path: Path) -> None:
    runner = _runner()
    scenarios = (
        ("fail-fast", _StructuredExecutor([1]), True, ["two"], "category-failed:one"),
        ("malformed", _StructuredExecutor(malformed=True), False, [],
         "missing-or-malformed-structured-output:one"),
        ("interrupt", _StructuredExecutor(interrupt=True), False, [], "run-interrupted"),
    )
    for name, executor, fail_fast, expected_omissions, reason in scenarios:
        candidate = tmp_path / name
        candidate.mkdir()
        _candidate_repository(candidate)
        receipt = tmp_path / f"{name}.json"
        categories = (
            runner.Category("one", "one", "pytest", ("tests/one",)),
            runner.Category("two", "two", "pytest", ("tests/two",)),
        )
        selected = categories if name == "fail-fast" else categories[:1]
        status = runner.run_categories_with_receipt(
            selected, all_categories=selected, receipt_path=receipt,
            artifact_paths=[], product_root=candidate, executor=executor,
            fail_fast=fail_fast,
        )
        assert status == (130 if name == "interrupt" else 1)
        document = runner.verification_receipt.validate_receipt(receipt)
        assert document["qualifying"] is False
        assert document["fail_fast_omissions"] == expected_omissions
        assert reason in document["qualification_reasons"]


@pytest.mark.parametrize(
    ("name", "xml", "reason"),
    (
        (
            "zero",
            '<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0" time="0"/></testsuites>',
            "zero-tests:one",
        ),
        (
            "all-skipped",
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="1" time="0">'
            '<testcase classname="suite" name="only"><skipped message="coordinate unavailable"/>'
            '</testcase></testsuite></testsuites>',
            "no-substantive-passing-tests:one",
        ),
    ),
)
def test_receipt_rejects_zero_or_all_skipped_categories(
    tmp_path: Path, name: str, xml: str, reason: str,
) -> None:
    runner = _runner()
    candidate = tmp_path / name
    candidate.mkdir()
    _candidate_repository(candidate)
    artifact = tmp_path / f"{name}.zip"
    artifact.write_bytes(b"artifact")
    receipt = tmp_path / f"{name}.json"
    categories = (runner.Category("one", "one", "pytest", ("tests/one",)),)
    status = runner.run_categories_with_receipt(
        categories, all_categories=categories, receipt_path=receipt,
        artifact_paths=[artifact], product_root=candidate,
        executor=_StructuredExecutor(pytest_xml=xml),
    )
    assert status == 1
    document = runner.verification_receipt.validate_receipt(receipt)
    assert reason in document["qualification_reasons"]
    if name == "all-skipped":
        assert document["unproved_coordinates"][0]["status"] == "UNPROVED"


def test_complete_release_receipt_requires_a_declared_artifact(tmp_path: Path) -> None:
    runner = _runner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _candidate_repository(candidate)
    receipt = tmp_path / "receipt.json"
    categories = (runner.Category("one", "one", "pytest", ("tests/one",)),)
    status = runner.run_categories_with_receipt(
        categories, all_categories=categories, receipt_path=receipt,
        artifact_paths=[], product_root=candidate,
        executor=_StructuredExecutor(),
    )
    assert status == 1
    document = runner.verification_receipt.validate_receipt(receipt)
    assert "complete-release-requires-artifact" in document["qualification_reasons"]


def test_structured_result_count_invariants_reject_inconsistent_totals() -> None:
    runner = _runner()
    with pytest.raises(runner.verification_receipt.ReceiptError, match="do not sum"):
        runner.verification_receipt.structured_counts("pytest", {
            "framework": "pytest", "tests": 2, "passed": 2,
            "testcases": 2, "failures": 1, "errors": 0, "skipped": 0,
            "skip_reasons": [],
        })
    with pytest.raises(runner.verification_receipt.ReceiptError, match="skip details"):
        runner.verification_receipt.structured_counts("pytest", {
            "framework": "pytest", "tests": 1, "passed": 0,
            "testcases": 1, "failures": 0, "errors": 0, "skipped": 1,
            "skip_reasons": [],
        })
    with pytest.raises(runner.verification_receipt.ReceiptError, match="planned"):
        runner.verification_receipt.structured_counts("unittest", {
            "framework": "unittest", "planned": 0, "tests": 1, "passed": 1,
            "failures": [], "errors": [], "skipped": [],
            "expected_failures": [], "unexpected_successes": [],
        })


def test_structured_unittest_runner_imports_harness_and_product_test_packages(
    tmp_path: Path,
) -> None:
    product = tmp_path / "candidate"
    harness_tests = product / "harness/harness_pkg/tests"
    product_tests = product / "tests/local"
    harness_tests.mkdir(parents=True)
    product_tests.mkdir(parents=True)
    for package in (
        product / "harness/harness_pkg/__init__.py",
        harness_tests / "__init__.py",
        product / "tests/__init__.py",
        product_tests / "__init__.py",
    ):
        package.write_text("\n", encoding="utf-8")
    (product / "harness/harness_pkg/__init__.py").write_text(
        "HARNESS_VALUE = 'harness-ok'\n", encoding="utf-8",
    )
    (product_tests / "support_value.py").write_text(
        "PRODUCT_VALUE = 'product-ok'\n", encoding="utf-8",
    )
    (harness_tests / "test_cross_import.py").write_text(
        "import unittest\n"
        "from harness_pkg import HARNESS_VALUE\n"
        "from tests.local.support_value import PRODUCT_VALUE\n"
        "class CrossImportTest(unittest.TestCase):\n"
        "    def test_both_import_roots(self):\n"
        "        self.assertEqual((HARNESS_VALUE, PRODUCT_VALUE), "
        "('harness-ok', 'product-ok'))\n",
        encoding="utf-8",
    )
    result = tmp_path / "result.json"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    completed = subprocess.run(
        [
            sys.executable,
            str((PRODUCT / "scripts/unittest_result_runner.py").resolve()),
            "--result-json", str(result),
            "--start-directory", str(harness_tests),
            "--top-level-directory", str(product / "harness"),
            "--import-root", str(product),
        ],
        cwd=unrelated,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    document = json.loads(result.read_text(encoding="utf-8"))
    assert document["status"] == "PASS"
    assert document["planned"] == document["tests"] == document["passed"] == 1

    rejected = subprocess.run(
        [
            sys.executable,
            str((PRODUCT / "scripts/unittest_result_runner.py").resolve()),
            "--result-json", str(tmp_path / "rejected.json"),
            "--start-directory", str(harness_tests),
            "--top-level-directory", str(product / "harness"),
            "--import-root", str(tmp_path),
        ],
        cwd=unrelated,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert rejected.returncode == 1
    rejected_document = json.loads(
        (tmp_path / "rejected.json").read_text(encoding="utf-8")
    )
    assert rejected_document["status"] == "FAIL"
    assert rejected_document["tests"] == 0

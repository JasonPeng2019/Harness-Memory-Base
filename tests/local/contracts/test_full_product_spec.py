"""Contract for the reconciled product reader and release-evidence ledger."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import ast
from pathlib import Path


PRODUCT = Path(__file__).resolve().parents[3]
PARENT = PRODUCT.parent
READER = PRODUCT / "harness/docs/product/FULL_PRODUCT_SPEC.md"
LEDGER = PRODUCT / "harness/docs/product/VERIFICATION_LEDGER.json"
PARENT_INPUT_COMMIT = "cfae52c2290740edc2ef33a1254c31ee8f5798cd"
KNOWN_ISSUES = ".plans/memory-backed-harness/KNOWN_ISSUES.md"
RECONCILED_OUTPUTS = {
    KNOWN_ISSUES: "c69582dccbd5540b58114e69e570613b74ca8152dfe6a844a7a392f74fbd4b83",
}

# These inputs are independent of the generated reader, so deleting a source
# from that reader cannot make this contract self-validate.
SOURCES = {
    "goal.md": "9fc8b254a363bb9406b7505521164161186ea4e9555fd2b18824e011631ec122",
    ".plans/memory-backed-harness/PLAN.md": "3d648b49ec93ff232cbcf25417227cb7456ed2856433ea7190db750372498ae7",
    ".plans/memory-backed-harness/NORMAL_OPERATION_ACCEPTANCE.md": "0087c7f14182a9187ba423c0cd25d7331c8b82a8d8a283c6ed7de23d753f176e",
    KNOWN_ISSUES: "fa38741e103cf968acdf69a1addd69bd57b38b092e0dc45e9fb1356089bd76bc",
    ".plans/archive/2026-09-21-r8-plan/PLAN.md": "1bf10bfeb535e73ae9356cd5a0631167a02bfdd59035251b9aedd18e1ca8bfe8",
    ".plans/archive/2026-09-21-tier4-plan/memory-backed-harness-formal/plan-workflow.md": "481ac99b3fc2adad946297927dd49aabec82e0f499527123945efd6b5268206e",
    ".plans/archive/2026-09-24-pre-remaining-replan/PLAN.md": "66db5d958c87331512ea3786c965312eeafb61033454b6e9c0df68d851b0c109",
    ".plans/archive/2026-09-24-pre-remaining-replan/specification/SPEC.md": "5a2c40372f70e5f720462037814622f9c8271c1e379ce19c10d0cbfb752d6980",
    ".plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-01-bounded-preparation-selects-eligible-memory.md": "e6b52cb976e7b5a2df4e217849607f1d8010ec370f3797c037ce10b201c8ba49",
    ".plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-02-reuse-produces-a-root-reviewable-plan.md": "9947e9f675204d8577ba7cc3b093748a39baf89f9c66f99ea1dabf7bbb9ffa35",
    ".plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-03-accepted-plan-dispatches-with-safe-context.md": "e892ac4112a26a03a4f33f62ee717ca9c16249391be5c270f2d20b0c48b4a702",
    ".plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-04-terminal-work-remains-durable-and-accounted.md": "c0c4acc26f92c16b0ca7d82f75037eaaf7197f7a13df4c04248d8f35158ac71a",
    ".plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-05-operators-control-isolated-recoverable-runs.md": "cf5c521455e6fef404de1006ac59dc08702d34e5abaff904bfaf02a6a25ae31f",
    ".plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-06-integrated-candidate-proves-the-real-product-path.md": "3df5e578217dabcfe44809b62d05cff36b1a87cd77ed49fd7f79f210c0a9b791",
    "new_harness_memory_docs/PRODUCT_FEATURE_SPEC_v65.md": "fabf3665e74695d17857c8f99998c6fad38376ba3dd838f275a6afb310a22cbe",
    "new_harness_memory_docs/PRODUCT_IMPLEMENTATION_SPEC_v67.md": "98b6cb55eb1057390e64aee1fbbbbb38ccbfa39da5486bf68f60731c7ee81730",
    "new_harness_memory_docs/PRODUCT_SPEC_DETAILED_v72.md": "dcd6e884f6f16b2284a3793293ed7c64b2af784fe01e91d97da56fd37e49ad55",
    "new_harness_memory_docs/PRODUCT_CONCEPTS.md": "fbea351dc325ea72dc80410cf3fef03196c9dc3e34eceb6070a4dca3d6ffa033",
    "product/harness/docs/product/PRODUCT_FEATURE_SPEC_v65.md": "64c3c8a6e9df5cef09b69f4b56d965995e354a133afbc8da3f88830920f622ce",
    "product/harness/docs/product/PRODUCT_IMPLEMENTATION_SPEC_v67.md": "98b6cb55eb1057390e64aee1fbbbbb38ccbfa39da5486bf68f60731c7ee81730",
    "product/harness/docs/product/PRODUCT_SPEC_DETAILED_v72.md": "c9e12bf461998d70b272fb24336408541c3e7d4669c280ee2939d92199b56f39",
    "product/harness/docs/product/PRODUCT_CONCEPTS.md": "ca4ca03a3f4750f00e4c9fcb5c6f54915c9c520a0a95084e8485d6d138874fbf",
    "product/harness/orchestrator_harness/SPEC.md": "c1b93417ceaf5fdec71ab707425a9072e92b28e17668d0d9a78d4f8de6464bd7",
    "product/harness/orchestrator_harness/PROVIDER_NETWORK_PAYLOAD.md": "428f8f87ba89247ec75454b684850a6121424a97d9e182acf4f26111ac016af2",
    "product/harness/harness_watcher_implementation/SPEC.md": "9b388d7da2ebd620d064deaf4fd01c1a7f8ce35bf4650802c76a512ea49470a9",
    "product/harness/README.md": "8f6889918cdfc6239b77416c4ea039d5179152099a11a0cd0adb3b1d4f8234bb",
    "product/harness/QUICK_START.md": "578c296fc52fe59299b9640c59de38b0b938592c585eaed7f75b6d9a7ee7cd43",
    "product/harness/REUSE_MANIFEST.md": "7d7538bbb993f8d49a18fdb8731d50fdee64df903ece255cf714323374a0e770",
}

SOURCE_DISPOSITIONS = {
    'goal.md': 'operator goal; scope intent',
    '.plans/memory-backed-harness/PLAN.md': 'completed narrow-MVP execution record, not product authority',
    '.plans/memory-backed-harness/NORMAL_OPERATION_ACCEPTANCE.md': 'operational MVP evidence',
    '.plans/memory-backed-harness/KNOWN_ISSUES.md': 'historical limitation ledger; reconcile stale claims',
    '.plans/archive/2026-09-21-r8-plan/PLAN.md': 'archived candidate/history',
    '.plans/archive/2026-09-21-tier4-plan/memory-backed-harness-formal/plan-workflow.md': 'archived workflow candidate/history',
    '.plans/archive/2026-09-24-pre-remaining-replan/PLAN.md': 'archived broad candidate/history',
    '.plans/archive/2026-09-24-pre-remaining-replan/specification/SPEC.md': 'explanatory acceptance model',
    '.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-01-bounded-preparation-selects-eligible-memory.md': 'executable-behavior candidate/history',
    '.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-02-reuse-produces-a-root-reviewable-plan.md': 'executable-behavior candidate/history',
    '.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-03-accepted-plan-dispatches-with-safe-context.md': 'executable-behavior candidate/history',
    '.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-04-terminal-work-remains-durable-and-accounted.md': 'executable-behavior candidate/history',
    '.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-05-operators-control-isolated-recoverable-runs.md': 'executable-behavior candidate/history',
    '.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-06-integrated-candidate-proves-the-real-product-path.md': 'executable-behavior candidate/history',
    'new_harness_memory_docs/PRODUCT_FEATURE_SPEC_v65.md': 'parent candidate; retain differences explicitly',
    'new_harness_memory_docs/PRODUCT_IMPLEMENTATION_SPEC_v67.md': 'binding implementation/verification source',
    'new_harness_memory_docs/PRODUCT_SPEC_DETAILED_v72.md': 'explanatory/source-observation candidate',
    'new_harness_memory_docs/PRODUCT_CONCEPTS.md': 'parent explanatory glossary candidate',
    'product/harness/docs/product/PRODUCT_FEATURE_SPEC_v65.md': 'canonical integrated Feature behavior',
    'product/harness/docs/product/PRODUCT_IMPLEMENTATION_SPEC_v67.md': 'canonical binding verification',
    'product/harness/docs/product/PRODUCT_SPEC_DETAILED_v72.md': 'integrated explanatory/source ledger',
    'product/harness/docs/product/PRODUCT_CONCEPTS.md': 'explanatory glossary',
    'product/harness/orchestrator_harness/SPEC.md': 'operational harness contract',
    'product/harness/orchestrator_harness/PROVIDER_NETWORK_PAYLOAD.md': 'operational privacy/network contract',
    'product/harness/harness_watcher_implementation/SPEC.md': 'diagnostic watcher contract',
    'product/harness/README.md': 'accepted visualizer/operator contract',
    'product/harness/QUICK_START.md': 'accepted visualizer launch/interaction contract',
    'product/harness/REUSE_MANIFEST.md': 'accepted component/reuse inventory',
}



def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_frozen_spec_inputs_exist_and_remain_byte_exact() -> None:
    for relative, expected in SOURCES.items():
        path = PARENT / relative
        assert path.is_file(), relative
        if relative == KNOWN_ISSUES:
            original = subprocess.run(
                ["git", "show", f"{PARENT_INPUT_COMMIT}:{relative}"], cwd=PARENT,
                capture_output=True, check=True,
            ).stdout
            assert hashlib.sha256(original).hexdigest() == expected
        else:
            assert _sha(path) == expected, relative
    for relative, expected in RECONCILED_OUTPUTS.items():
        assert _sha(PARENT / relative) == expected, relative


def test_full_product_reader_has_closed_source_and_evidence_ledgers() -> None:
    text = READER.read_text(encoding="utf-8")
    for relative, digest in SOURCES.items():
        assert relative in text
        assert digest in text
    for relative, digest in RECONCILED_OUTPUTS.items():
        assert relative in text
        assert digest in text
    manifest_rows = re.findall(
        r"^\| `([^`]+)` \| `([0-9a-f]{64})` \| (.+) \|$",
        text, re.MULTILINE,
    )
    assert manifest_rows == [
        (relative, digest, SOURCE_DISPOSITIONS[relative])
        for relative, digest in SOURCES.items()
    ]

    gap_ids = re.findall(r"^\| (G\d{2}) \|", text, re.MULTILINE)
    test_ids = re.findall(r"^\| (T\d{2}) \|", text, re.MULTILINE)
    assert gap_ids == [f"G{number:02d}" for number in range(1, 14)]
    assert test_ids == [f"T{number:02d}" for number in range(1, 30)]

    for row in re.findall(r"^\| (?:G|T)\d{2} \|.*$", text, re.MULTILINE):
        assert "closure=CLOSED" in row
        assert re.search(
            r"(?:EXISTING-SUFFICIENT|NEW-LOCAL-PASS|UNPROVED-PREREQUISITE)", row
        )
        assert "`tests/" in row or "`harness/" in row


def test_structured_verification_ledger_is_exact_and_executable() -> None:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert set(ledger) == {
        "coordinates", "direct_oracle_bindings", "schema", "source_requirements",
    }
    assert ledger["schema"] == "memory-harness-verification-ledger/v2"
    expected = {
        *[f"G{number:02d}" for number in range(1, 14)],
        *[f"T{number:02d}" for number in range(1, 30)],
    }
    coordinates = ledger["coordinates"]
    assert {item["requirement_id"] for item in coordinates} == expected
    assert ledger["source_requirements"] == {
        "gap_ids": [f"G{number:02d}" for number in range(1, 14)],
        "test_ids": [f"T{number:02d}" for number in range(1, 30)],
    }
    exact_fields = {
        "requirement_id", "evidence_class", "status", "prerequisite", "node",
        "positive_oracle", "negative_oracle",
    }
    expected_classes = {
        "G01": ("LIVE",),
        "G02": ("CONTRACT", "NATIVE"),
        "G03": ("LOCAL",), "G04": ("LOCAL",),
        "G05": ("LOCAL", "LIVE"),
        "G06": ("CONTRACT", "LOCAL", "LIVE"),
        "G07": ("NATIVE-LINUX", "NATIVE-MACOS", "NATIVE-WINDOWS"),
        "G08": ("CONTRACT-WINDOWS", "LOCAL-POSIX"),
        "G09": ("LOCAL",),
        "G10": (
            "LOCAL-CPYTHON-3.12", "LOCAL-CPYTHON-3.11",
            "LOCAL-CPYTHON-3.13", "LOCAL-CPYTHON-3.14",
        ),
        "G11": ("LOCAL",), "G12": ("LOCAL",), "G13": ("LOCAL",),
        "T01": ("LOCAL", "NATIVE"),
        **{f"T{number:02d}": ("LOCAL",) for number in range(2, 12)},
        "T12": ("CONTRACT", "LOCAL", "NATIVE"),
        "T13": ("LOCAL",), "T14": ("LOCAL",), "T15": ("LOCAL",),
        "T16": ("LOCAL",),
        "T17": ("CONTRACT", "LOCAL"),
        "T18": ("CONTRACT", "LOCAL", "LIVE"),
        "T19": ("LOCAL",), "T20": ("LOCAL",), "T21": ("LOCAL",),
        "T22": ("LOCAL", "NATIVE"), "T23": ("LOCAL",),
        "T24": ("CONTRACT", "LOCAL", "LIVE"),
        "T25": ("LOCAL", "LIVE", "NATIVE"),
        "T26": ("CONTRACT", "NATIVE"),
        "T27": ("LOCAL", "NATIVE"),
        "T28": ("CONTRACT", "NATIVE"),
        "T29": ("LOCAL",),
    }
    grouped: dict[str, list[str]] = {}
    for coordinate in coordinates:
        grouped.setdefault(coordinate["requirement_id"], []).append(
            coordinate["evidence_class"]
        )
    assert {key: tuple(value) for key, value in grouped.items()} == expected_classes

    unproved = {
        ("G01", "LIVE"), ("G02", "NATIVE"), ("G05", "LIVE"),
        ("G06", "LIVE"), ("G07", "NATIVE-MACOS"),
        ("G07", "NATIVE-WINDOWS"),
        ("G10", "LOCAL-CPYTHON-3.11"),
        ("G10", "LOCAL-CPYTHON-3.13"),
        ("G10", "LOCAL-CPYTHON-3.14"),
        ("T01", "NATIVE"), ("T12", "NATIVE"), ("T18", "LIVE"),
        ("T22", "NATIVE"), ("T24", "LIVE"), ("T25", "LIVE"),
        ("T25", "NATIVE"), ("T26", "NATIVE"), ("T27", "NATIVE"),
        ("T28", "NATIVE"),
    }
    existing = {("G02", "CONTRACT")}
    existing.update(
        (f"T{number:02d}", "LOCAL")
        for number in range(2, 12)
        if number != 9
    )
    existing.update({
        ("T12", "CONTRACT"), ("T12", "LOCAL"),
        ("T13", "LOCAL"), ("T14", "LOCAL"),
        ("T26", "CONTRACT"), ("T28", "CONTRACT"),
    })
    for coordinate in coordinates:
        assert set(coordinate) == exact_fields
        key = (coordinate["requirement_id"], coordinate["evidence_class"])
        expected_status = (
            "UNPROVED-PREREQUISITE" if key in unproved
            else "EXISTING-SUFFICIENT" if key in existing
            else "NEW-LOCAL-PASS"
        )
        assert coordinate["status"] == expected_status
        assert coordinate["evidence_class"]
        if coordinate["status"] != "UNPROVED-PREREQUISITE":
            assert coordinate["prerequisite"] == "none"
        else:
            assert coordinate["prerequisite"] != "none"
            assert "exact external coordinate emits candidate-bound" not in (
                coordinate["positive_oracle"]
            )
            assert "absence, mismatch, over-budget use" not in (
                coordinate["negative_oracle"]
            )
        assert coordinate["positive_oracle"].strip()
        assert coordinate["negative_oracle"].strip()
        path_text, *selectors = coordinate["node"].split("::")
        assert selectors, coordinate["node"]
        path = PRODUCT / path_text
        assert path.is_file(), coordinate["node"]
        selectors[-1] = selectors[-1].split("[", 1)[0]
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        executable: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                executable.add(node.name)
            elif isinstance(node, ast.ClassDef):
                executable.update(
                    f"{node.name}::{member.name}"
                    for member in node.body
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
        assert "::".join(selectors) in executable, coordinate["node"]

    direct_bindings = ledger["direct_oracle_bindings"]
    assert {
        (item["requirement_id"], item["evidence_class"])
        for item in direct_bindings
    } == {("T09", "LOCAL"), ("T16", "LOCAL")}
    binding_fields = {
        "requirement_id", "evidence_class", "node", "scenario_id",
        "positive_oracle_id", "negative_oracle_id",
    }
    coordinate_by_key = {
        (item["requirement_id"], item["evidence_class"]): item
        for item in coordinates
    }
    scenario_ids: set[str] = set()
    oracle_ids: set[str] = set()
    for binding in direct_bindings:
        assert set(binding) == binding_fields
        key = (binding["requirement_id"], binding["evidence_class"])
        coordinate = coordinate_by_key[key]
        assert binding["node"] == coordinate["node"]
        assert re.fullmatch(r"[GT]\d{2}\.[A-Z-]+\.[a-z0-9-]+", binding["scenario_id"])
        assert re.fullmatch(
            r"[GT]\d{2}\.[A-Z-]+\.[a-z0-9-]+", binding["positive_oracle_id"]
        )
        assert re.fullmatch(
            r"[GT]\d{2}\.[A-Z-]+\.[a-z0-9-]+", binding["negative_oracle_id"]
        )
        assert binding["scenario_id"] not in scenario_ids
        assert binding["positive_oracle_id"] not in oracle_ids
        assert binding["negative_oracle_id"] not in oracle_ids
        assert binding["positive_oracle_id"] != binding["negative_oracle_id"]
        scenario_ids.add(binding["scenario_id"])
        oracle_ids.update(
            (binding["positive_oracle_id"], binding["negative_oracle_id"])
        )

        path_text, *selectors = binding["node"].split("::")
        tree = ast.parse((PRODUCT / path_text).read_text(encoding="utf-8-sig"))
        declaration = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "VERIFICATION_ORACLE_BINDINGS"
                for target in node.targets
            )
        )
        declared = ast.literal_eval(declaration.value)
        selector = "::".join(item.split("[", 1)[0] for item in selectors)
        assert declared[selector] == {
            field: binding[field]
            for field in binding_fields
            if field != "node"
        }

    for target in ("NATIVE-WINDOWS", "NATIVE-MACOS"):
        coordinate = next(item for item in coordinates if item["evidence_class"] == target)
        host = target.removeprefix("NATIVE-").lower()
        assert host in coordinate["positive_oracle"].lower()
        assert "sentinel" in coordinate["positive_oracle"]
        assert "foreign-host skip" in coordinate["negative_oracle"]
    for version in ("3.11", "3.13", "3.14"):
        coordinate = next(
            item for item in coordinates
            if item["evidence_class"] == f"LOCAL-CPYTHON-{version}"
        )
        assert all(word in coordinate["positive_oracle"] for word in (
            version, "wheel", "installs", "CLI",
        ))
        assert "exact-skips without credit" in coordinate["negative_oracle"]

    text = READER.read_text(encoding="utf-8")
    documented: dict[str, list[tuple[str, str]]] = {}
    for requirement_id, raw in re.findall(
        r"^\| ([GT]\d{2}) \| closure=CLOSED; coordinates=\{([^}]+)\}",
        text, re.MULTILINE,
    ):
        documented[requirement_id] = [
            (evidence_class, status)
            for evidence_class, status in re.findall(
                r"([^; ]+)=`([^`]+)`", raw,
            )
        ]
    structured: dict[str, list[tuple[str, str]]] = {}
    for coordinate in coordinates:
        structured.setdefault(coordinate["requirement_id"], []).append((
            coordinate["evidence_class"], coordinate["status"],
        ))
    assert documented == structured


def test_reader_preserves_product_boundary_and_evidence_classes() -> None:
    text = READER.read_text(encoding="utf-8")
    for phrase in (
        "reconciled reader, not a superseding specification",
        "Narrow completed MVP",
        "Full current-phase product",
        "Learned selector",
        "Benchmark execution",
        "CONTRACT evidence never counts as NATIVE or LIVE evidence",
        "No ambient credential or environment variable",
        "VERIFICATION_LEDGER.json",
    ):
        assert phrase in text


def test_reader_switch_summary_matches_v65_without_inventing_policy_switches() -> None:
    text = READER.read_text(encoding="utf-8")
    section = text.split("## 8. Feature switches and modes", 1)[1].split("## 9.", 1)[0]
    rows = re.findall(r"^\| ([^|]+?) \| ([^|]+?) \|$", section, re.MULTILINE)
    names = [name.strip() for name, _ in rows if name.strip() not in {"Current switch", "---"}]
    assert names == [
        "Experience read", "Experience write", "Generated-skill creation",
        "Generated-skill use", "Shared publication", "Atlas shared retrieval",
        "Plan-template memory", "APC", "Light adaptation", "Deeper",
        "Learned strategy selection",
    ]
    assert "Reserved and unavailable in this release" in section
    assert (
        "Service configuration and availability, privacy/network modes, snapshot administration,\n"
        "telemetry/accounting, and outcome ingestion are policies or operations—not additional feature switches."
    ) in section


def test_current_release_readme_makes_no_unsupported_benchmark_claim() -> None:
    readme = (PRODUCT / "README.md").read_text(encoding="utf-8")
    for unsupported in (
        "SWE-Marathon", "47.3%", "7.3%", "91% fewer", "11.14×",
    ):
        assert unsupported not in readme

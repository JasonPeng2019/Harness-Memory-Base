"""Executable boundary for deferred learned selection and benchmark execution."""

from __future__ import annotations

import argparse
import pkgutil
from pathlib import Path

import pytest

import memory_harness
from memory_harness import config, store
from orchestrator_harness import operator_launch


@pytest.mark.parametrize(
    "setting",
    [
        {"strategy": "learned"}, {"strategy": "learned-selection"},
        {"learned_mode": True}, {"learned_selection": True},
        {"policy_load": True}, {"policy_update": True},
        {"training": True}, {"train": True}, {"learner": "enabled"},
    ],
)
def test_every_learned_or_update_request_fails_before_import_or_state(
    tmp_path: Path, setting: dict,
) -> None:
    before_modules = set(pkgutil.iter_modules(memory_harness.__path__))
    state = store.MemoryStore(tmp_path / "state.sqlite3")
    state.initialize()
    try:
        before_tables = {
            row[0] for row in state.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        with pytest.raises(config.DeferredCapabilityError, match="deferred/not implemented"):
            config.resolve_config(setting)
        after_tables = {
            row[0] for row in state.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert before_tables == after_tables
        assert not any("learner" in name or "policy_history" in name for name in after_tables)
        assert before_modules == set(pkgutil.iter_modules(memory_harness.__path__))
        assert not any(module.name in {"learner", "selector_policy", "training"} for module in before_modules)
    finally:
        state.close()


def test_public_product_and_operator_manifest_have_no_benchmark_entrypoint() -> None:
    package_modules = {module.name for module in pkgutil.iter_modules(memory_harness.__path__)}
    assert not any("benchmark" in name or "leaderboard" in name for name in package_modules)
    parser = operator_launch._build_parser()
    subparser_action = next(
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = set((subparser_action.choices or {}).keys())
    assert not commands & {"benchmark", "bench", "leaderboard", "b0", "b1", "b2", "b3"}
    assert {"harness", "manager", "view"} <= commands


def test_release_tests_do_not_schedule_benchmark_workloads() -> None:
    root = Path(__file__).resolve().parents[3]
    release_roots = (root / "tests", root / "harness/orchestrator_harness")
    forbidden_driver_names = {
        "run_benchmark.py", "benchmark_runner.py", "leaderboard.py", "b0.py", "b1.py", "b2.py", "b3.py",
    }
    found = {
        path.name
        for base in release_roots
        for path in base.rglob("*.py")
        if "vendor" not in path.parts and path.name in forbidden_driver_names
    }
    assert found == set()

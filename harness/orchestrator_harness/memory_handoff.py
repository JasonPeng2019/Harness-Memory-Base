"""Narrow optional-memory handoff seam for bootstrap, resume, and launch.

This module is the only harness integration point for the Stage-A memory
slice.  Legacy task cards without ``memory_handoff`` take the ordinary path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import read_json
from .records import atomic_write_json

try:  # the product package is optional for the ordinary harness path
    from memory_harness import store as store_module
except Exception:  # pragma: no cover - legacy path without the product package
    store_module = None


class MemoryHandoffError(RuntimeError):
    """The optional memory handoff is invalid or cannot be materialized."""


def handoff_from_task_card(task_card: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = task_card.get("memory_handoff")
    return value if isinstance(value, Mapping) else None


def memory_paths(worktree: str | Path) -> tuple[Path, Path]:
    root = Path(worktree) / ".agent-workspace"
    return root / "memory-state.sqlite3", root / "memory-dispatch.json"


def validate_task_card(task_card: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Validate an optional handoff before any worktree mutation."""

    handoff = handoff_from_task_card(task_card)
    if handoff is None:
        return None
    try:
        from memory_harness import contracts

        contracts.validate_task_card(task_card)
        contracts.validate_memory_handoff(handoff)
    except Exception as exc:
        raise MemoryHandoffError(f"invalid memory_handoff: {exc}") from exc
    return handoff


def _write_envelope(worktree: str | Path, envelope: Mapping[str, Any]) -> Path:
    _, envelope_path = memory_paths(worktree)
    envelope_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(envelope_path, dict(envelope))
    return envelope_path


def _prepare_memory_outcome(
    *,
    task_card: Mapping[str, Any],
    handoff: Mapping[str, Any],
    resolved_config: Any,
    lane_id: str,
    run_id: str,
    worktree_path: str | Path,
    base_commit: str,
    finalize: bool,
):
    """Run one bounded preparation and return its outcome, or ``None`` all-off."""

    from memory_harness import runtime

    store_path, _ = memory_paths(worktree_path)
    memory_store = store_module.MemoryStore(store_path)
    memory_store.initialize()
    try:
        memory_runtime = runtime.MemoryRuntime(memory_store, config=resolved_config)
        plan = handoff["plan"]
        return memory_runtime.prepare_with_memory(
            task_card=task_card,
            plan=plan,
            objective_id=handoff["objective_id"],
            route=handoff.get("route", "ordinary"),
            request=handoff.get("configuration"),
            lane_id=lane_id,
            run_id=run_id,
            worktree_path=str(worktree_path),
            base_commit=base_commit,
            mandatory_content=[
                {"id": "task", "kind": "task", "content": task_card["task"]},
                {
                    "id": "accepted-plan",
                    "kind": "accepted-plan",
                    "content": plan["content"],
                },
            ],
            finalize=finalize,
        )
    finally:
        memory_store.close()


def prepare_bootstrap_envelope(
    *,
    task_card: Mapping[str, Any],
    lane_id: str,
    run_id: str,
    worktree_path: str | Path,
    base_commit: str,
) -> dict[str, Any] | None:
    """Prepare the finalized envelope for bootstrap, or return ``None``.

    The bounded STEP-04 preparation runs over the real task card, objective,
    plan, and dispatch identity.  Only an exact ROOT-accepted plan is
    finalized and written for dispatch: a candidate or absent plan still gets
    one durable preparation decision, trace, and disposition, but no envelope
    is created and the ordinary harness path proceeds without optional memory.
    """

    handoff = validate_task_card(task_card)
    if handoff is None:
        return None
    try:
        from memory_harness import config

        resolved_config = config.resolve_config(handoff.get("configuration"))
        if resolved_config.all_off:
            _, envelope_path = memory_paths(worktree_path)
            envelope_path.unlink(missing_ok=True)
            return None

        plan = handoff["plan"]
        accepted = plan.get("state") == "accepted"
        outcome = _prepare_memory_outcome(
            task_card=task_card,
            handoff=handoff,
            resolved_config=resolved_config,
            lane_id=lane_id,
            run_id=run_id,
            worktree_path=worktree_path,
            base_commit=base_commit,
            finalize=accepted,
        )
        if not accepted or outcome is None or outcome.envelope is None:
            _, envelope_path = memory_paths(worktree_path)
            envelope_path.unlink(missing_ok=True)
            return None
        _write_envelope(worktree_path, outcome.envelope)
        return outcome.envelope
    except MemoryHandoffError:
        raise
    except Exception as exc:
        raise MemoryHandoffError(f"cannot prepare memory handoff: {exc}") from exc


def prepare_resume_envelope(
    *,
    task_card: Mapping[str, Any],
    lane_id: str,
    run_id: str,
    worktree_path: str | Path,
    base_commit: str,
) -> dict[str, Any] | None:
    """Prepare the resumed envelope with bootstrap-equivalent validation.

    A resume is the same logical decision as the original bootstrap, so it
    reuses the durable decision identity and its absolute deadline instead of
    replenishing the objective's budget.
    """

    return prepare_bootstrap_envelope(
        task_card=task_card,
        lane_id=lane_id,
        run_id=run_id,
        worktree_path=worktree_path,
        base_commit=base_commit,
    )


def finalize_envelope(
    *,
    task_card: Mapping[str, Any],
    plan: Mapping[str, Any],
    decision_id: str,
    lane_id: str,
    run_id: str,
    worktree_path: str | Path,
    base_commit: str,
    mandatory_content: list[Mapping[str, Any]] | None = None,
    optional_items: list[Mapping[str, Any]] | None = None,
    configuration: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Finalize one exact safe context and write it for this worktree.

    Returns the finalized envelope, or ``None`` when the resolved configuration
    is all-off so the inherited harness path applies unchanged.
    """

    from memory_harness import config as memory_config
    from memory_harness import context as memory_context

    resolved = memory_config.resolve_config(configuration)
    if resolved.all_off:
        _, envelope_path = memory_paths(worktree_path)
        envelope_path.unlink(missing_ok=True)
        return None
    limits = memory_config.resolve_limits(None)
    finalized = memory_context.finalize_context(
        task_card=task_card,
        plan=plan,
        decision_id=decision_id,
        lane_id=lane_id,
        run_id=run_id,
        worktree_path=str(worktree_path),
        base_commit=base_commit,
        strategy=resolved.strategy,
        configuration={**dict(configuration or {}), "strategy": resolved.strategy},
        mandatory_content=list(mandatory_content or []),
        optional_items=list(optional_items or []),
        limits=limits,
    )
    _write_envelope(worktree_path, finalized.envelope)
    return finalized.envelope


def validate_final_context_for_launch(
    *,
    context: Mapping[str, Any],
    envelope: Mapping[str, Any],
    task_card: Mapping[str, Any],
    lane_id: str,
    run_id: str,
    worktree_path: str | Path,
    base_commit: str,
) -> dict[str, Any]:
    """Validate a finalized context against the actual dispatch target."""

    from memory_harness import context as memory_context

    handoff = handoff_from_task_card(task_card)
    if handoff is None:
        raise MemoryHandoffError("task card has no memory_handoff")
    try:
        memory_context.validate_final_context(
            context,
            envelope=envelope,
            task_card=task_card,
            plan=handoff["plan"],
            lane_id=lane_id,
            run_id=run_id,
            base_commit=base_commit,
            worktree_path=str(worktree_path),
        )
    except Exception as exc:
        raise MemoryHandoffError(f"invalid finalized context: {exc}") from exc
    return dict(context)


def load_envelope(worktree_path: str | Path) -> dict[str, Any] | None:
    _, envelope_path = memory_paths(worktree_path)
    if not envelope_path.is_file():
        return None
    try:
        return read_json(envelope_path)
    except (OSError, ValueError) as exc:
        raise MemoryHandoffError(f"cannot read memory dispatch envelope: {exc}") from exc


def validate_envelope_for_launch(
    *,
    envelope: Mapping[str, Any],
    task_card: Mapping[str, Any],
    lane_id: str,
    run_id: str,
    worktree_path: str | Path,
    base_commit: str,
) -> dict[str, Any]:
    try:
        from memory_harness import contracts

        handoff = handoff_from_task_card(task_card)
        if handoff is None:
            raise MemoryHandoffError("task card has no memory_handoff")
        contracts.validate_envelope(
            envelope,
            task_card=task_card,
            plan=handoff["plan"],
            lane_id=lane_id,
            run_id=run_id,
            base_commit=base_commit,
            worktree_path=str(worktree_path),
        )
    except MemoryHandoffError:
        raise
    except Exception as exc:
        raise MemoryHandoffError(f"invalid memory dispatch envelope: {exc}") from exc
    return dict(envelope)


def _open_runtime(worktree_path: str | Path):
    from memory_harness import runtime, store

    store_path, _ = memory_paths(worktree_path)
    memory_store = store.MemoryStore(store_path)
    memory_store.initialize()
    return memory_store, runtime.MemoryRuntime(memory_store)


def worker_environment() -> dict[str, str]:
    """Return the inherited environment without product control credentials."""

    from memory_harness import privacy

    return privacy.worker_environment(os.environ)


def dispatch(
    *,
    worktree_path: str | Path,
    envelope: Mapping[str, Any],
    launcher: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Record intent and exact observed invocation around the existing launcher."""

    memory_store, memory_runtime = _open_runtime(worktree_path)
    try:
        return memory_runtime.dispatch(envelope, launcher)
    finally:
        memory_store.close()



def record_dispatch_intent(
    *, worktree_path: str | Path, envelope: Mapping[str, Any]
) -> dict[str, Any]:
    """Record the exact dispatch intent before the existing launcher runs."""

    memory_store, memory_runtime = _open_runtime(worktree_path)
    try:
        return memory_runtime.record_dispatch_intent(envelope)
    except Exception as exc:
        raise MemoryHandoffError(f"cannot record dispatch intent: {exc}") from exc
    finally:
        memory_store.close()


def record_observed_invocation(
    *,
    worktree_path: str | Path,
    envelope: Mapping[str, Any],
    observed_invocation: Mapping[str, Any],
) -> dict[str, Any]:
    """Record the exact observed harness invocation after launch."""

    from memory_harness import contracts

    memory_store, _ = _open_runtime(worktree_path)
    try:
        operation = contracts.make_operation(
            kind="dispatch",
            envelope=envelope,
            status="delivered",
            observed_invocation=observed_invocation,
        )
        return memory_store.record_operation(operation)
    finally:
        memory_store.close()

def record_ambiguous_dispatch(
    *, worktree_path: str | Path, envelope: Mapping[str, Any]
) -> dict[str, Any]:
    """Record an ambiguous launch acknowledgement without duplicating work."""

    from memory_harness import contracts

    memory_store, _ = _open_runtime(worktree_path)
    try:
        operation = contracts.make_operation(
            kind="dispatch",
            envelope=envelope,
            status="ambiguous",
        )
        return memory_store.record_operation(operation)
    finally:
        memory_store.close()


def omit_optional_content(
    *, worktree_path: str | Path, envelope: Mapping[str, Any], item_id: str
) -> dict[str, Any]:
    """Omit one optional item and write the corrected delivery trace."""

    memory_store, memory_runtime = _open_runtime(worktree_path)
    try:
        revised = memory_runtime.omit_optional_content(envelope, item_id)
    finally:
        memory_store.close()
    _write_envelope(worktree_path, revised)
    return revised


__all__ = [
    "MemoryHandoffError",
    "dispatch",
    "finalize_envelope",
    "handoff_from_task_card",
    "load_envelope",
    "memory_paths",
    "omit_optional_content",
    "prepare_bootstrap_envelope",
    "prepare_resume_envelope",
    "record_ambiguous_dispatch",
    "record_dispatch_intent",
    "record_observed_invocation",
    "validate_envelope_for_launch",
    "validate_final_context_for_launch",
    "validate_task_card",
    "worker_environment",
]

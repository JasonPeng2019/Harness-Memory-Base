"""Narrow optional-memory handoff seam for bootstrap, resume, and launch.

This module is the only harness integration point for the Stage-A memory
slice.  Legacy task cards without ``memory_handoff`` take the ordinary path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import read_json
from .records import atomic_write_json


class MemoryHandoffError(RuntimeError):
    """The optional memory handoff is invalid or cannot be materialized."""


class PendingPlanError(MemoryHandoffError):
    """The enhanced handoff has no ROOT-accepted plan, so nothing may dispatch.

    This is the fail-closed pending-plan condition: an enabled enhanced
    handoff that is explicitly ``absent``, still in ``candidate_review``, or
    missing its finalized dispatch envelope cannot execute.  It is a distinct
    condition from an invalid or copied envelope, and it is never resolved by
    silently falling back to the legacy path.
    """


def _memory_module(name: str):
    """Import one ``memory_harness`` module for the enhanced path only.

    The product package is optional for legacy and all-off cards, so it is
    imported lazily at the point of use instead of at module import.  A
    missing package is a precise handoff error rather than an attribute
    failure, and the import is immune to whichever module happened to be
    imported first.
    """

    import importlib

    try:
        return importlib.import_module(f"memory_harness.{name}")
    except Exception as exc:  # pragma: no cover - package unavailable
        raise MemoryHandoffError(
            f"the memory_harness package is not importable: {exc}"
        ) from exc


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
        contracts = _memory_module("contracts")

        contracts.validate_task_card(task_card)
        contracts.validate_memory_handoff(handoff)
    except MemoryHandoffError:
        raise
    except Exception as exc:
        raise MemoryHandoffError(f"invalid memory_handoff: {exc}") from exc
    return handoff


def handoff_plan_state(task_card: Mapping[str, Any]) -> str | None:
    """Return the validated explicit current-plan state of a task card.

    ``None`` means the card carries no enhanced handoff at all.  Every other
    value is one of ``absent``, ``candidate_review``, or
    ``execution_accepted`` and is always accompanied by a consistent plan
    reference (or its explicit absence).
    """

    handoff = validate_task_card(task_card)
    if handoff is None:
        return None
    return str(handoff["plan_state"])


def handoff_plan(task_card: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the exact bound plan of an enhanced card, or ``None`` absent."""

    handoff = validate_task_card(task_card)
    if handoff is None:
        return None
    return _memory_module("contracts").handoff_plan(handoff)


def enabled_memory_handoff(task_card: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the handoff only when its resolved configuration is enabled.

    Legacy cards and all-off enhanced cards return ``None`` so bootstrap,
    resume, and launch keep the inherited ordinary path without touching the
    optional store.  Malformed configurations fail closed here.
    """

    handoff = validate_task_card(task_card)
    if handoff is None:
        return None
    try:
        config = _memory_module("config")
        resolved = config.resolve_config(handoff.get("configuration"))
    except MemoryHandoffError:
        raise
    except Exception as exc:
        raise MemoryHandoffError(f"invalid memory configuration: {exc}") from exc
    if resolved.all_off:
        return None
    return handoff


def enabled_handoff_state(task_card: Mapping[str, Any]) -> str | None:
    """Return the enabled enhanced handoff's exact plan state, or ``None``.

    ``None`` means this card has no enhanced handoff, or its resolved
    configuration is all-off, so the inherited ordinary harness path applies
    and no optional memory state may be initialized for it.
    """

    handoff = enabled_memory_handoff(task_card)
    return None if handoff is None else str(handoff["plan_state"])


def lane_handoff_state(
    task_card: Mapping[str, Any] | None, lane: Mapping[str, Any] | None
) -> str | None:
    """Resolve one lane's governing enhanced-handoff state, or ``None``.

    The durable task card is the primary authority, and the lane record's
    recorded state is the second witness.  A lane without either is the
    inherited ordinary path.  When both exist they must agree: a stale or
    copied task card cannot quietly change what a lane was prepared to do.
    """

    card_state: str | None = None
    if task_card is not None:
        card_state = enabled_handoff_state(task_card)
    recorded: object = None
    if isinstance(lane, Mapping):
        recorded = lane.get("memory_plan_state")
    lane_state: str | None = None
    if isinstance(recorded, str) and recorded:
        lane_state = recorded
        try:
            states = _memory_module("contracts").HANDOFF_PLAN_STATES
        except MemoryHandoffError:
            raise
        if recorded not in states:
            raise MemoryHandoffError(
                f"lane records an unknown memory plan state: {recorded!r}"
            )
    if card_state is not None and lane_state is not None and card_state != lane_state:
        raise MemoryHandoffError(
            "the durable task card and the lane record disagree about the "
            f"current plan state: card={card_state!r}, lane={lane_state!r}"
        )
    return card_state if card_state is not None else lane_state


def require_accepted_handoff(task_card: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact bound plan of a dispatchable enhanced card.

    Only the explicit ``execution_accepted`` state may dispatch, and that
    state always carries its exact ROOT-accepted plan.  Every other state is
    the fail-closed pending-plan condition, and a missing or unreadable plan
    reference is an invalid handoff rather than an absent plan.
    """

    handoff = validate_task_card(task_card)
    if handoff is None:
        raise MemoryHandoffError("the task card has no enhanced memory handoff")
    state = str(handoff["plan_state"])
    if state != "execution_accepted":
        raise PendingPlanError(
            "the enhanced handoff is not a ROOT-accepted execution plan "
            f"(plan_state={state!r}); ROOT review must finish before any launch"
        )
    plan = _memory_module("contracts").handoff_plan(handoff)
    if plan is None:
        raise MemoryHandoffError(
            "an execution-accepted handoff must carry its exact accepted plan"
        )
    return plan


def plan_state_summary(plan_state: str) -> str:
    """Return the operator-facing meaning of one enhanced plan state."""

    if plan_state == "absent":
        return (
            "no current plan exists; bounded preparation recorded its fresh "
            "ROOT-planning disposition and nothing may execute yet"
        )
    if plan_state == "candidate_review":
        return (
            "a candidate plan is still in ROOT review; its exact review "
            "identity is retained and nothing may execute yet"
        )
    return f"the enhanced handoff state {plan_state!r} is not dispatchable"


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
    """Run one bounded preparation for the exact handoff state.

    The explicit ``absent``, ``candidate_review``, and ``execution_accepted``
    states all run the same bounded decision: an absent plan starts fresh ROOT
    planning, a pending candidate keeps its exact review identity, and only an
    exact ROOT-accepted plan is finalized for dispatch.  The plan reference is
    never dereferenced when it is explicitly absent.
    """

    runtime = _memory_module("runtime")
    store = _memory_module("store")

    plan = handoff["plan"]
    mandatory_content = [
        {"id": "task", "kind": "task", "content": task_card["task"]},
    ]
    if isinstance(plan, Mapping):
        mandatory_content.append(
            {
                "id": "accepted-plan",
                "kind": "accepted-plan",
                "content": plan["content"],
            }
        )
    store_path, _ = memory_paths(worktree_path)
    memory_store = store.MemoryStore(store_path)
    memory_store.initialize()
    try:
        memory_runtime = runtime.MemoryRuntime(memory_store, config=resolved_config)
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
            mandatory_content=mandatory_content,
            finalize=finalize,
        )
    finally:
        memory_store.close()


@dataclass(frozen=True)
class LaneMemory:
    """One lane preparation's exact optional-memory result.

    ``state`` is ``None`` only for legacy cards and all-off enhanced cards,
    which keep the inherited ordinary harness path and never initialize the
    optional store.  Every other value is the enhanced handoff's explicit plan
    state, and a dispatchable lane always carries its exact finalized
    envelope.  ``pending_plan`` is the fail-closed condition bootstrap and
    resume must report instead of preparing an execution worker.
    """

    state: str | None
    envelope: dict[str, Any] | None
    outcome: Any

    @property
    def dispatchable(self) -> bool:
        return self.state == "execution_accepted" and self.envelope is not None

    @property
    def pending_plan(self) -> bool:
        return self.state is not None and not self.dispatchable


def prepare_lane_memory(
    *,
    task_card: Mapping[str, Any],
    lane_id: str,
    run_id: str,
    worktree_path: str | Path,
    base_commit: str,
) -> "LaneMemory":
    """Run one bounded lane preparation and report its exact disposition.

    The bounded STEP-04 preparation runs over the real task card, objective,
    plan, and dispatch identity.  Only an exact ROOT-accepted plan is
    finalized and written for dispatch: an explicitly absent plan starts fresh
    ROOT planning and a pending candidate keeps its exact review identity, so
    both still get one durable preparation decision, trace, and disposition
    while never becoming execution authority.  Legacy cards and all-off
    enhanced cards return ``state=None`` without touching the optional store.
    """

    handoff = enabled_memory_handoff(task_card)
    if handoff is None:
        _, envelope_path = memory_paths(worktree_path)
        envelope_path.unlink(missing_ok=True)
        return LaneMemory(state=None, envelope=None, outcome=None)
    try:
        config = _memory_module("config")

        resolved_config = config.resolve_config(handoff.get("configuration"))
        # Only the explicit execution-accepted state finalizes a dispatchable
        # envelope.  An explicitly absent plan and a pending candidate still
        # run one bounded preparation (their fresh ROOT-planning or review
        # disposition is durable), but they never become execution authority.
        state = str(handoff["plan_state"])
        accepted = state == "execution_accepted"
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
            return LaneMemory(state=state, envelope=None, outcome=outcome)
        _write_envelope(worktree_path, outcome.envelope)
        return LaneMemory(state=state, envelope=outcome.envelope, outcome=outcome)
    except MemoryHandoffError:
        raise
    except Exception as exc:
        raise MemoryHandoffError(f"cannot prepare memory handoff: {exc}") from exc


def prepare_bootstrap_envelope(
    *,
    task_card: Mapping[str, Any],
    lane_id: str,
    run_id: str,
    worktree_path: str | Path,
    base_commit: str,
) -> dict[str, Any] | None:
    """Prepare and write the finalized bootstrap envelope, or return ``None``.

    Only an exact ROOT-accepted plan produces a dispatchable envelope; the
    enhanced ``absent`` and ``candidate_review`` states keep their durable
    disposition and leave no envelope behind.
    """

    return prepare_lane_memory(
        task_card=task_card,
        lane_id=lane_id,
        run_id=run_id,
        worktree_path=worktree_path,
        base_commit=base_commit,
    ).envelope


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

    plan = require_accepted_handoff(task_card)
    try:
        memory_context.validate_final_context(
            context,
            envelope=envelope,
            task_card=task_card,
            plan=plan,
            lane_id=lane_id,
            run_id=run_id,
            base_commit=base_commit,
            worktree_path=str(worktree_path),
        )
    except PendingPlanError:
        raise
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
    """Validate a durable dispatch envelope against the real launch target.

    A copied, stale, or otherwise mismatched envelope fails here: the exact
    task card, objective, route, accepted plan, lane, run, worktree, and base
    must all agree before any dispatch intent is recorded.
    """

    try:
        from memory_harness import contracts

        plan = require_accepted_handoff(task_card)
        contracts.validate_envelope(
            envelope,
            task_card=task_card,
            plan=plan,
            lane_id=lane_id,
            run_id=run_id,
            base_commit=base_commit,
            worktree_path=str(worktree_path),
        )
    except PendingPlanError:
        raise
    except MemoryHandoffError:
        raise
    except Exception as exc:
        raise MemoryHandoffError(f"invalid memory dispatch envelope: {exc}") from exc
    return dict(envelope)


def load_final_context(
    *, worktree_path: str | Path, envelope: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the durable finalized context for one exact dispatch envelope.

    The store is opened only for an enhanced lane that already produced a
    dispatchable envelope.  A missing or unreadable durable final context is a
    fail-closed invalid-launch condition rather than a reason to dispatch from
    the envelope alone.
    """

    decision_id = envelope.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id:
        raise MemoryHandoffError("dispatch envelope has no decision identity")
    store_path, _ = memory_paths(worktree_path)
    if not Path(store_path).is_file():
        raise MemoryHandoffError(
            "the enhanced dispatch has no durable memory state to validate"
        )
    store = _memory_module("store")
    memory_store = store.MemoryStore(store_path)
    memory_store.initialize()
    try:
        context = memory_store.get_final_context_for_decision(decision_id)
    finally:
        memory_store.close()
    if not isinstance(context, Mapping):
        raise MemoryHandoffError(
            "the enhanced dispatch has no durable finalized context for its decision"
        )
    return dict(context)


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
    "LaneMemory",
    "MemoryHandoffError",
    "PendingPlanError",
    "dispatch",
    "enabled_handoff_state",
    "finalize_envelope",
    "handoff_from_task_card",
    "handoff_plan",
    "handoff_plan_state",
    "lane_handoff_state",
    "prepare_lane_memory",
    "load_envelope",
    "load_final_context",
    "memory_paths",
    "omit_optional_content",
    "plan_state_summary",
    "prepare_bootstrap_envelope",
    "prepare_resume_envelope",
    "record_ambiguous_dispatch",
    "record_dispatch_intent",
    "record_observed_invocation",
    "require_accepted_handoff",
    "validate_envelope_for_launch",
    "validate_final_context_for_launch",
    "validate_task_card",
    "worker_environment",
]

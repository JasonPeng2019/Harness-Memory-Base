"""One bounded preparation for one exact objective, plan, and deadline.

The coordinator resolves every feature, network, route, strategy, stage, and
reserve value *before* any optional call, keeps ROOT acceptance distinct from
proposal completion, and hands one exact finalized context to the existing
product harness.  A late Level 0 admission permanently supersedes the old
packet and permits at most one bounded ordinary re-prepare.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import apc, contracts, context as context_module, harness_bridge, templates
from .config import (
    DEEPER,
    PROBLEM_FOCUSED,
    STANDARD,
    MemoryConfig,
    PreparationLimits,
    resolve_config,
    resolve_limits,
)
from .privacy import PrivacyPolicy, sanitize_text
from .search import BoundedSearch, SearchStore


class PreparationError(RuntimeError):
    """A bounded preparation invariant was violated."""


class MandatoryStateFailure(PreparationError):
    """The exact mandatory state is inconsistent; ROOT recovery is required."""


class PlanAcceptanceError(PreparationError):
    """ROOT acceptance was not established for the exact plan revision."""


@dataclass(frozen=True)
class PreparationOutcome:
    mode: str
    decision: dict[str, Any] | None
    preparation: dict[str, Any] | None
    trace: dict[str, Any] | None
    disposition: dict[str, Any] | None
    plan: dict[str, Any]
    proposal: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    envelope: dict[str, Any] | None = None
    superseded: dict[str, Any] | None = None
    reason: str = ""

    @property
    def dispatchable(self) -> bool:
        return self.mode == "dispatchable" and self.envelope is not None

    @property
    def selected_candidates(self) -> list[dict[str, Any]]:
        if not self.trace:
            return []
        return [
            candidate
            for candidate in self.trace.get("candidates", [])
            if candidate.get("disposition") == "selected"
        ]


_MASKED_STRATEGIES = {"deeper"}


class PreparationService:
    """Own one logical decision from exact state to a safe dispatch."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        config: MemoryConfig | None = None,
        limits: PreparationLimits | None = None,
        privacy_policy: PrivacyPolicy | None = None,
        clock: Callable[[], float] | None = None,
        registry: Sequence[templates.Template] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.limits = limits or resolve_limits()
        self.privacy_policy = privacy_policy or PrivacyPolicy()
        self.clock = clock or time.monotonic
        self.registry = (
            tuple(registry) if registry is not None else templates.load_default_templates()
        )
        self.search = BoundedSearch(
            limits=self.limits, privacy_policy=self.privacy_policy, clock=self.clock
        )

    # -- exact state -------------------------------------------------------

    def _resolve(self, requested: Mapping[str, Any] | None) -> MemoryConfig:
        if self.config is not None and requested is None:
            return self.config
        return resolve_config(dict(requested or {}))

    def _captured_config(self, preparation: Mapping[str, Any]) -> MemoryConfig:
        """Continue the exact configuration one preparation captured.

        A restarted service, a retry, and a route correction all continue one
        logical decision, so its fixed strategy and feature gates come from
        the recorded preparation instead of whatever a new service instance
        currently resolves by default.
        """

        recorded = preparation["configuration"]
        return MemoryConfig(
            **{
                field.name: recorded[field.name]
                for field in fields(MemoryConfig)
                if field.name in recorded
            }
        )

    def _durable_preparation(self, preparation_id: str) -> dict[str, Any] | None:
        """Read one exact durable preparation record.

        `None` is returned only when no store is configured, because then no
        durable authority exists to consult.  With a configured store the
        exact record is required: an unreadable or missing record propagates
        instead of being reported as absence, since a failed read is not proof
        that no prior correction happened.
        """

        if self.store is None:
            return None
        return self.store.get_preparation(preparation_id)

    @staticmethod
    def _plan_state_error(exc: Exception, objective_id: str) -> MandatoryStateFailure:
        return MandatoryStateFailure(
            f"mandatory current-plan state is inconsistent for {objective_id!r}: {exc}"
        )

    def _recover_budget(
        self, decision_id: str
    ) -> tuple[tuple[float, float] | None, int]:
        """Read the durable cutoff, current grant, and next attempt once.

        Restart, resume, and route correction all share one logical decision, so
        they must not hand out a fresh deadline.  The tightest recorded cutoff
        and its remaining-plus-spent grant retain elapsed cost without counting
        time removed by a shorter trusted cutoff as time already spent.
        """

        if self.store is None:
            return None, 1
        try:
            prior = self.store.list_preparations(decision_id)
        except Exception as exc:
            raise PreparationError(
                f"durable preparation budget lookup failed for {decision_id}: {exc}"
            ) from exc
        unreadable = f"durable preparation budget is unreadable for {decision_id}"
        if not isinstance(prior, list):
            raise PreparationError(unreadable)
        trusted: list[tuple[int, float, float]] = []
        attempts: list[int] = []
        for record in prior:
            if not isinstance(record, Mapping) or record.get("decision_id") != decision_id:
                raise PreparationError(unreadable)
            attempt = record.get("attempt")
            source = record.get("budget_source")
            spent = record.get("spent_seconds")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise PreparationError(unreadable)
            if (
                isinstance(spent, bool)
                or not isinstance(spent, (int, float))
                or not math.isfinite(spent)
                or spent < 0
            ):
                raise PreparationError(unreadable)
            attempts.append(attempt)
            if source == "unknown_time":
                if (
                    record.get("deadline_monotonic") is not None
                    or record.get("remaining_seconds") is not None
                ):
                    raise PreparationError(unreadable)
                continue
            deadline = record.get("deadline_monotonic")
            remaining = record.get("remaining_seconds")
            if (
                source != "trusted_deadline"
                or isinstance(deadline, bool)
                or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)
                or isinstance(remaining, bool)
                or not isinstance(remaining, (int, float))
                or not math.isfinite(remaining)
                or remaining < 0
            ):
                raise PreparationError(unreadable)
            grant = float(remaining + spent)
            if not math.isfinite(grant):
                raise PreparationError(unreadable)
            trusted.append((attempt, float(deadline), grant))
        if not trusted:
            return None, max(attempts, default=0) + 1
        _, tightest_deadline, granted_budget = min(
            trusted, key=lambda item: (item[1], item[0])
        )
        return (tightest_deadline, granted_budget), max(attempts) + 1

    def _next_attempt(self, decision_id: str) -> int:
        if self.store is None:
            return 1
        try:
            return len(self.store.list_preparations(decision_id)) + 1
        except Exception:
            return 1

    # -- preparation -------------------------------------------------------

    def _effective_config(
        self,
        resolved_config: MemoryConfig,
        *,
        failure_context: str | None,
        unknown_time: bool,
    ) -> MemoryConfig:
        """Apply the fixed-strategy gates before any optional call.

        A Problem-focused recipe uses the supplied real failure/recovery
        context and otherwise falls back to Standard under Standard's gates.
        Unknown time admits only the configured cheap fixed pass, so it masks
        Deeper as well.  Only the effective strategy changes; the requested
        strategy stays on the record for lineage.
        """

        strategy = resolved_config.strategy
        if strategy == PROBLEM_FOCUSED and not (failure_context or "").strip():
            return replace(
                resolved_config,
                strategy=STANDARD,
                reason="fallback to standard: problem-focused needs real failure context",
            )
        if strategy == DEEPER and unknown_time:
            return replace(
                resolved_config,
                strategy=STANDARD,
                reason="fallback to standard: unknown time permits only the cheap fixed pass",
            )
        return resolved_config

    def prepare(
        self,
        *,
        task_card: Mapping[str, Any],
        plan: Mapping[str, Any] | None,
        objective_id: str,
        route: str = "ordinary",
        request: Mapping[str, Any] | None = None,
        network_mode: str = "normal",
        deadline: float | None = None,
        unknown_time: bool = False,
        failure_context: str | None = None,
        stores: Sequence[SearchStore] = (),
        bindings: Mapping[str, Any] | None = None,
        root_replan: Mapping[str, Any] | None = None,
        apc_binding: Mapping[str, Any] | None = None,
        apc_launcher: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
        apc_cleanup: Mapping[str, Any] | None = None,
        mandatory_content: Iterable[Mapping[str, Any]] = (),
        optional_items: Iterable[Mapping[str, Any]] = (),
        freshness_check: Callable[[Mapping[str, Any]], bool] | None = None,
        lane_id: str | None = None,
        run_id: str | None = None,
        worktree_path: str | None = None,
        base_commit: str | None = None,
        finalize: bool = False,
    ) -> PreparationOutcome:
        contracts.validate_task_card(task_card)
        resolved_config = self._resolve(request)
        if resolved_config.all_off:
            if plan is None:
                raise MandatoryStateFailure(
                    "all-off preparation still needs its exact plan state"
                )
            return PreparationOutcome(
                mode="inherited",
                decision=None,
                preparation=None,
                trace=None,
                disposition=None,
                plan=dict(plan),
                reason="all enhancements are off; the inherited harness path applies",
            )
        resolved_config = self._effective_config(
            resolved_config,
            failure_context=failure_context,
            unknown_time=unknown_time,
        )
        try:
            current_plan_state = contracts.classify_current_plan(
                plan, expected_objective_id=objective_id, expected_route=route
            )
        except contracts.PlanStateError as exc:
            raise self._plan_state_error(exc, objective_id) from exc
        # An absent plan is an ordinary fresh-planning state: it keeps its
        # exact identity in the record and never invents an inherited plan.
        plan_of_record = (
            dict(plan)
            if plan is not None
            else templates.fresh_plan(objective_id=objective_id, route=route)
        )

        now = self.clock()
        if deadline is not None:
            absolute_deadline = float(deadline)
            budget_source = "trusted_deadline"
        elif unknown_time:
            absolute_deadline = now + self.limits.standard_stage_seconds
            budget_source = "unknown_time"
        else:
            absolute_deadline = now + self.limits.default_deadline_seconds
            budget_source = "trusted_deadline"

        # The provisional identity only exists to recover the durable deadline
        # captured configuration for this objective's logical decision; it is
        # never persisted as-is.
        explicit_deadline = deadline is not None
        provisional = contracts.make_decision(
            task_card,
            plan_of_record,
            strategy=resolved_config.strategy,
            configuration=asdict(resolved_config),
        )
        # One decision owns one deadline.  A later trusted cutoff may tighten
        # the durable bound, but an explicit or default cutoff cannot extend it.
        recovered, next_attempt = self._recover_budget(provisional["decision_id"])
        if recovered is not None:
            durable_deadline, granted_budget = recovered
            durable_remaining = max(0.0, durable_deadline - now)
            spent_seconds = max(0.0, granted_budget - durable_remaining)
            absolute_deadline = (
                min(absolute_deadline, durable_deadline)
                if explicit_deadline
                else durable_deadline
            )
            budget_source = "trusted_deadline"
            unknown_time = False
        else:
            spent_seconds = 0.0
        remaining = max(0.0, absolute_deadline - now) if not unknown_time else 0.0
        # One logical decision owns one captured configuration.  Stage
        # admission only demotes the *packet*; it must not mint a fresh
        # decision identity, because a later call with less remaining time
        # would otherwise silently become a new decision with new ownership.
        ownership_decision = contracts.make_decision(
            task_card,
            plan_of_record,
            strategy=resolved_config.strategy,
            configuration=asdict(resolved_config),
        )
        resolved_config, admitted, admission_reason = self._admit_config(
            resolved_config, remaining=remaining, unknown_time=unknown_time
        )
        stage_allowance = self._stage_allowance(
            resolved_config.strategy, remaining, unknown_time, admitted=admitted
        )
        decision = contracts.make_decision(
            task_card,
            plan_of_record,
            strategy=resolved_config.strategy,
            configuration=asdict(resolved_config),
            decision_id=ownership_decision["decision_id"],
        )
        if self.store is not None:
            self.store.record_decision(decision)

        preparation = contracts.make_preparation(
            task_card=task_card,
            decision_id=decision["decision_id"],
            objective_id=objective_id,
            route=route,
            plan_id=plan_of_record["plan_id"],
            plan_digest=plan_of_record["content_hash"],
            plan_state=plan_of_record["state"],
            current_plan_state=current_plan_state,
            strategy=resolved_config.strategy,
            requested_strategy=resolved_config.requested_strategy,
            configuration=decision["configuration"],
            network_mode=network_mode,
            budget_source=budget_source,
            remaining_seconds=None if unknown_time else remaining,
            deadline_monotonic=None if unknown_time else absolute_deadline,
            execution_reserve_seconds=self.limits.execution_reserve_seconds,
            stage_allowance_seconds=stage_allowance,
            spent_seconds=spent_seconds,
            attempt=next_attempt,
        )
        if self.store is not None:
            self.store.record_preparation(preparation)

        accepted_precedence = current_plan_state == "execution_accepted"
        replan = self._validate_root_replan(root_replan)
        # Template selection and adaptation run only for an explicit ROOT
        # replan request.  An accepted plan is preserved, and a pending
        # candidate keeps its exact identity and state until ROOT decides.
        template_selection = replan is not None and not accepted_precedence
        # One canonical sanitized representation of the exact task, objective,
        # route, and real failure context is constructed once and shared: the
        # bounded search compares candidates against it, and plan production
        # recomputes every selected template's trusted comparable score against
        # the same record instead of sweeping the registry a second time.
        objective = self._canonical_objective(
            task_card, objective_id, route, failure_context
        )
        search = self._run_search(
            preparation=preparation,
            objective=objective,
            route=route,
            strategy=resolved_config.strategy,
            stores=stores,
            stage_allowance=stage_allowance,
            accepted_precedence=not template_selection,
            config=resolved_config,
            network_mode=network_mode,
            budget_reason=admission_reason,
        )

        if accepted_precedence:
            disposition = contracts.make_plan_disposition(
                decision_id=decision["decision_id"],
                objective_id=objective_id,
                route=route,
                branch="preserved_accepted",
                reason="a ROOT-accepted same-objective plan takes precedence over memory",
                root_acceptance={
                    "plan_id": plan["plan_id"],
                    "plan_digest": plan["content_hash"],
                    "accepted_by": plan.get("accepted_by", "ROOT"),
                },
            )
            if self.store is not None:
                self.store.record_plan_disposition(disposition)
            outcome = self._finish_planning(
                mode="planning",
                decision=decision,
                preparation=preparation,
                trace=search["trace"],
                disposition=disposition,
                plan=plan,
                proposal=None,
                reason="accepted plan preserved without template scoring",
            )
        elif current_plan_state == "candidate_review" and not template_selection:
            disposition = contracts.make_plan_disposition(
                decision_id=decision["decision_id"],
                objective_id=objective_id,
                route=route,
                branch="candidate_review",
                reason=(
                    "a pending candidate plan continues its existing ROOT review "
                    "without template selection, adaptation, or replacement"
                ),
                preserved_plan=plan,
            )
            if self.store is not None:
                self.store.record_plan_disposition(disposition)
            outcome = self._finish_planning(
                mode=self._planning_mode(search["trace"]),
                decision=decision,
                preparation=preparation,
                trace=search["trace"],
                disposition=disposition,
                plan=plan,
                proposal=None,
                reason=disposition["reason"],
            )
        elif current_plan_state == "absent":
            fresh = templates.fresh_plan(objective_id=objective_id, route=route)
            disposition = contracts.make_plan_disposition(
                decision_id=decision["decision_id"],
                objective_id=objective_id,
                route=route,
                branch="fresh",
                reason=(
                    "a truly absent current plan starts fresh ROOT planning and "
                    "review; no template selection or adaptation runs"
                ),
                fresh=fresh,
            )
            if self.store is not None:
                self.store.record_plan_disposition(disposition)
            outcome = self._finish_planning(
                mode=self._planning_mode(search["trace"]),
                decision=decision,
                preparation=preparation,
                trace=search["trace"],
                disposition=disposition,
                plan=fresh,
                proposal=None,
                reason=disposition["reason"],
            )
        else:
            disposition, produced_plan, proposal = self._produce_plan(
                ownership_decision_id=ownership_decision["decision_id"],
                decision=decision,
                objective_id=objective_id,
                route=route,
                objective=objective,
                trace=search["trace"],
                resolved_config=resolved_config,
                bindings=bindings,
                apc_binding=apc_binding,
                apc_launcher=apc_launcher,
                apc_cleanup=apc_cleanup,
                deadline=absolute_deadline,
                unknown_time=unknown_time,
                root_replan=replan,
            )
            if self.store is not None:
                self.store.record_plan_disposition(disposition)
            mode = (
                "no_optional_memory"
                if search["trace"]["outcome"] == "no_optional_memory"
                else "planning"
            )
            outcome = self._finish_planning(
                mode=mode,
                decision=decision,
                preparation=preparation,
                trace=search["trace"],
                disposition=disposition,
                plan=produced_plan,
                proposal=proposal,
                reason=disposition["reason"],
            )
        if not finalize:
            return outcome
        if outcome.plan["state"] != "accepted":
            raise PlanAcceptanceError(
                "only an exact ROOT-accepted plan revision may be finalized "
                f"for dispatch; the current plan state is {outcome.plan['state']!r}"
            )
        return self._finalize(
            outcome=outcome,
            task_card=task_card,
            plan=outcome.plan,
            lane_id=lane_id,
            run_id=run_id,
            worktree_path=worktree_path,
            base_commit=base_commit,
            mandatory_content=mandatory_content,
            optional_items=optional_items,
            freshness_check=freshness_check,
        )


    # -- ROOT acceptance ---------------------------------------------------

    def accept(
        self,
        *,
        disposition: Mapping[str, Any],
        proposal: Mapping[str, Any],
        accepted_content: Any | None = None,
        accepted_plan_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Accept one exact revision; proposal completion is not acceptance."""

        try:
            accepted = contracts.accept_plan(
                proposal,
                accepted_plan_id=accepted_plan_id,
                accepted_content=accepted_content,
            )
        except contracts.ContractError as exc:
            raise PlanAcceptanceError(f"ROOT acceptance failed: {exc}") from exc
        revised = dict(disposition)
        revised["root_acceptance"] = {
            "plan_id": accepted["plan_id"],
            "plan_digest": accepted["content_hash"],
            "accepted_by": "ROOT",
            "revision": accepted["revision"],
            "source_branch": disposition["branch"],
            "revised": accepted_content is not None,
        }
        revised["content_hash"] = contracts.content_hash(revised)
        if self.store is not None:
            self.store.record_plan_disposition(revised)
        return revised, accepted

    def reject(
        self, *, disposition: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        """Record a ROOT rejection; the attempt is not a cache hit."""

        revised = dict(disposition)
        attempts = list(revised.get("reuse_attempts", []))
        attempts.append(
            {
                "branch": revised["branch"],
                "status": "rejected",
                "reason": str(reason),
                "rejected_by": "ROOT",
            }
        )
        revised["reuse_attempts"] = attempts
        revised["branch"] = "fresh"
        revised["reason"] = f"ROOT rejected the proposal: {reason}"
        revised["fresh"] = revised.get("fresh") or {
            "steps": [],
            "state": "fresh",
        }
        revised["content_hash"] = contracts.content_hash(revised)
        if self.store is not None:
            self.store.record_plan_disposition(revised)
        return revised

    # -- Level 0 correction -------------------------------------------------

    def apply_level_zero(
        self,
        *,
        preparation: Mapping[str, Any],
        decision: Mapping[str, Any],
        task_card: Mapping[str, Any],
        plan: Mapping[str, Any],
        objective_id: str,
        route: str = "ordinary",
        stores: Sequence[SearchStore] = (),
        failure_context: str | None = None,
    ) -> PreparationOutcome:
        """Permanently supersede a topology packet after a late Level 0."""

        contracts.validate_preparation(preparation)
        if preparation.get("supersedes") or preparation.get("superseded_by"):
            return PreparationOutcome(
                mode="no_memory_continuation",
                decision=dict(decision),
                preparation=dict(preparation),
                trace=None,
                disposition=None,
                plan=dict(plan),
                reason=(
                    "a late Level 0 supersedes only one packet; continue explicitly "
                    "without memory instead of replenishing spent time"
                ),
            )
        # The durable record is the authority across restart: once this exact
        # packet has been superseded, another Level 0 verdict for it must not
        # launch a second ordinary re-prepare or hand out fresh time.  With a
        # configured store the exact durable record is required, and an
        # unreadable or missing record is an unknown durability state rather
        # than proof of no prior correction, so it fails closed into the same
        # explicit continuation without memory.
        durable: dict[str, Any] | None = None
        unreadable = ""
        if self.store is not None:
            try:
                durable = self._durable_preparation(preparation["preparation_id"])
            except Exception as exc:
                unreadable = f"{type(exc).__name__}: {exc}"
            if durable is None:
                return PreparationOutcome(
                    mode="no_memory_continuation",
                    decision=dict(decision),
                    preparation=dict(preparation),
                    trace=None,
                    disposition=None,
                    plan=dict(plan),
                    reason=(
                        "the exact packet's durable record is unreadable or "
                        "missing, so an earlier Level 0 correction cannot be "
                        "ruled out; continue explicitly without memory instead "
                        "of risking a second optional call"
                        + (f" ({unreadable})" if unreadable else "")
                    ),
                )
        if durable is not None and (
            durable.get("superseded_by") or durable.get("status") == "superseded"
        ):
            return PreparationOutcome(
                mode="no_memory_continuation",
                decision=dict(decision),
                preparation=durable,
                trace=None,
                disposition=None,
                plan=dict(plan),
                reason=(
                    "a late Level 0 supersedes only one packet; this exact packet "
                    "is already durably superseded, so continue explicitly without "
                    "memory instead of launching a second re-prepare"
                ),
            )
        replacement_id = contracts.sha256_hex(
            {
                "domain": "memory-preparation/v1",
                "supersedes": preparation["preparation_id"],
                "route_correction": "level-0",
            }
        )
        deadline_monotonic = preparation.get("deadline_monotonic")
        if isinstance(deadline_monotonic, (int, float)):
            remaining = max(0.0, float(deadline_monotonic) - self.clock())
        else:
            remaining = preparation.get("remaining_seconds")
        # The correction never replenishes spent time: the granted budget is
        # the remaining time the abandoned packet originally recorded plus the
        # cost it had already paid, and the elapsed cost is recomputed against
        # the current clock.
        prior_spent = float(preparation.get("spent_seconds") or 0.0)
        prior_remaining = preparation.get("remaining_seconds")
        if isinstance(prior_remaining, (int, float)) and not isinstance(prior_remaining, bool):
            granted = prior_spent + float(prior_remaining)
        else:
            granted = None
        replacement_config = self._captured_config(preparation)
        unknown_time = preparation["budget_source"] == "unknown_time"
        remaining_value = None if remaining is None else max(0.0, float(remaining))
        if granted is None:
            spent = prior_spent
            remaining_value = prior_remaining
        else:
            spent = max(0.0, granted - float(remaining_value or 0.0))
        # One logical decision owns one captured configuration: the one
        # bounded ordinary re-prepare continues under the abandoned packet's
        # recorded fixed strategy and gates.  A restarted service's current
        # defaults never replace them; only the remaining-time admission may
        # demote the recipe once.
        replacement_config, admitted, admission_reason = self._admit_config(
            replacement_config,
            remaining=float(remaining_value or 0.0),
            unknown_time=unknown_time,
        )
        stage_allowance = self._stage_allowance(
            replacement_config.strategy,
            float(remaining_value or 0.0),
            unknown_time,
            admitted=admitted,
        )
        replacement = contracts.make_preparation(
            task_card=task_card,
            decision_id=decision["decision_id"],
            objective_id=objective_id,
            route=route,
            plan_id=plan["plan_id"],
            plan_digest=plan["content_hash"],
            plan_state=plan["state"],
            current_plan_state=preparation["current_plan_state"],
            strategy=replacement_config.strategy,
            requested_strategy=replacement_config.requested_strategy,
            configuration=asdict(replacement_config),
            network_mode=preparation["network_mode"],
            budget_source=preparation["budget_source"],
            remaining_seconds=remaining_value,
            deadline_monotonic=deadline_monotonic,
            execution_reserve_seconds=preparation["execution_reserve_seconds"],
            stage_allowance_seconds=stage_allowance,
            spent_seconds=spent,
            attempt=self._next_attempt(decision["decision_id"]),
            preparation_id=replacement_id,
            supersedes=preparation["preparation_id"],
            route_correction={"level": 0, "action": "bounded_ordinary_reprepare"},
        )
        old = dict(preparation)
        old["status"] = "superseded"
        old["superseded_by"] = replacement_id
        if self.store is not None:
            old = self.store.mark_preparation_superseded(
                preparation["preparation_id"], superseded_by=replacement_id
            )
            self.store.record_preparation(replacement)
        else:
            old["content_hash"] = contracts.content_hash(old)
        # The one bounded ordinary re-prepare obeys the same admission rule and
        # never replenishes the time already spent on the abandoned packet.
        search = self._run_search(
            preparation=replacement,
            objective=self._canonical_objective(
                task_card, objective_id, route, failure_context
            ),
            route=route,
            strategy=replacement_config.strategy,
            stores=stores,
            stage_allowance=stage_allowance,
            accepted_precedence=False,
            config=replacement_config,
            network_mode=replacement["network_mode"],
            budget_reason=admission_reason,
        )
        disposition = contracts.make_plan_disposition(
            decision_id=decision["decision_id"],
            objective_id=objective_id,
            route=route,
            branch="fresh",
            reason=(
                "the superseded topology packet is permanently non-dispatchable; "
                "one bounded ordinary re-prepare was performed"
            ),
            fresh=(
                dict(plan)
                if plan["state"] == "fresh"
                else templates.fresh_plan(objective_id=objective_id, route=route)
            ),
        )
        if self.store is not None:
            self.store.record_plan_disposition(disposition)
        return PreparationOutcome(
            mode="planning",
            decision=dict(decision),
            preparation=replacement,
            trace=search["trace"],
            disposition=disposition,
            plan=dict(plan),
            superseded=old,
            reason=disposition["reason"],
        )


    # -- internals ---------------------------------------------------------

    def _validate_root_replan(
        self, request: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        """Return one explicit ROOT replan request, or `None`.

        Only ROOT may replace or extend a current plan.  Any other replan
        request is a mandatory-state failure rather than a quiet replan.
        """

        if request is None:
            return None
        if not isinstance(request, Mapping):
            raise MandatoryStateFailure("a ROOT replan request must be an object")
        if request.get("requested_by") != "ROOT":
            raise MandatoryStateFailure(
                "only an explicit ROOT replan request may replace a current plan"
            )
        return dict(request)

    @staticmethod
    def _planning_mode(trace: Mapping[str, Any] | None) -> str:
        if trace is None or trace.get("outcome") == "no_optional_memory":
            return "no_optional_memory"
        return "planning"

    def _recipe_fits(self, strategy: str, remaining: float) -> bool:
        """A recipe is admitted only when its full configured maximum and the
        positive execution reserve both fit the trusted remaining time."""

        return remaining >= (
            self.limits.stage_seconds_for(strategy) + self.limits.execution_reserve_seconds
        )

    def _admit_config(
        self,
        resolved_config: MemoryConfig,
        *,
        remaining: float,
        unknown_time: bool,
    ) -> tuple[MemoryConfig, bool, str]:
        """Admit one recipe, demoting at most once to Standard.

        A recipe is never truncated to whatever time happens to remain: it is
        admitted whole or demoted.  When neither the requested recipe nor
        Standard fits, no optional call is made and Standard stays the recorded
        fallback recipe.  Demotion is monotonic and never replenishes budget.
        """

        if unknown_time:
            # Unknown time already masks to the cheap fixed pass, which is the
            # bounded case; the execution reserve does not mask it further.
            return resolved_config, True, ""
        if self._recipe_fits(resolved_config.strategy, remaining):
            return resolved_config, True, ""
        if resolved_config.strategy != STANDARD and self._recipe_fits(STANDARD, remaining):
            return (
                replace(
                    resolved_config,
                    strategy=STANDARD,
                    reason=(
                        "fallback to standard: the requested recipe needs its full "
                        "bound plus the execution reserve"
                    ),
                ),
                True,
                "one bounded demotion to standard: the requested recipe did not fit",
            )
        return (
            replace(
                resolved_config,
                strategy=STANDARD,
                reason=(
                    "no optional memory: neither the requested recipe nor standard "
                    "fits the trusted remaining time"
                ),
            ),
            False,
            (
                "the full configured recipe bound and the positive execution reserve "
                "do not fit the trusted remaining time; no optional call was made"
            ),
        )

    def _stage_allowance(
        self, strategy: str, remaining: float, unknown_time: bool, *, admitted: bool = True
    ) -> float:
        if unknown_time:
            return min(
                self.limits.stage_seconds_for(strategy),
                self.limits.unknown_time_budget_seconds,
            )
        if not admitted:
            return 0.0
        # An admitted recipe receives its full configured maximum.
        return self.limits.stage_seconds_for(strategy)

    def _run_search(
        self,
        *,
        preparation: Mapping[str, Any],
        objective: Mapping[str, Any],
        route: str,
        strategy: str,
        stores: Sequence[SearchStore],
        stage_allowance: float,
        accepted_precedence: bool,
        config: MemoryConfig,
        network_mode: str,
        budget_reason: str = "",
    ) -> dict[str, Any]:
        enabled: list[SearchStore] = []
        attempts: list[dict[str, Any]] = []
        for store in stores:
            flag = {
                "historical_evidence": config.experience_read,
                "procedure": config.atlas_shared_retrieval or config.generated_skill_use,
                "template": config.template_memory,
            }.get(store.kind, False)
            disabled_reason = "the resolved configuration disables this store"
            if store.source_kind == "curated_local_procedure":
                # A local curated/builtin procedure store performs no
                # shared/remote call, so the accepted local trust path stays
                # eligible even when Atlas shared retrieval and generated-skill
                # use are both off, and restricted_local never suppresses it.
                flag = True
            elif store.source_kind == "generated_local_procedure":
                # Generated-origin local guidance is separately gated: with
                # generated_skill_use off the source makes no query at all, so
                # no delivery can be filtered out of work already performed.
                flag = config.generated_skill_use
                disabled_reason = (
                    "generated_skill_use is disabled: a generated-origin local "
                    "procedure source performs no query"
                )
            elif store.source_kind == "everos_generated_skill":
                # The future EverOS generated-skill procedure store performs a
                # real remote call, so it is admitted only while generated-skill
                # use is on: ``atlas_shared_retrieval`` is the shared-retrieval
                # authority for Atlas procedures and never enables this source
                # by itself.
                flag = config.generated_skill_use
                disabled_reason = (
                    "generated_skill_use is disabled: the EverOS generated-skill "
                    "source performs no query"
                )
                if flag and network_mode == "restricted_local":
                    # Restricted-local mode must not begin the shared/remote
                    # task-path call at all; the suppression happens here,
                    # before the call, instead of filtering its output after
                    # work already occurred.
                    flag = False
                    disabled_reason = (
                        "restricted-local mode never begins a shared/remote "
                        "retrieval call"
                    )
            elif flag and store.requires_network:
                # A remote procedure store carries shared-retrieval authority:
                # only atlas_shared_retrieval enables it, never the local
                # generated-skill analogue, so an Atlas store is never queried
                # just because generated_skill_use is true.
                if store.kind == "procedure":
                    flag = config.atlas_shared_retrieval
                if flag and network_mode == "restricted_local":
                    # Restricted-local mode must not begin the shared/remote
                    # task-path call at all; the suppression happens here,
                    # before the call, instead of filtering its output after
                    # work already occurred.
                    flag = False
                    disabled_reason = (
                        "restricted-local mode never begins a shared/remote "
                        "retrieval call"
                    )
            if accepted_precedence and store.kind == "template":
                # Template selection never runs when an accepted or candidate
                # plan has precedence.
                flag = False
            if not flag:
                attempts.append(
                    {
                        "store_id": store.store_id,
                        "kind": store.kind,
                        "status": "disabled",
                        "candidates": 0,
                        "reason": disabled_reason,
                    }
                )
                continue
            enabled.append(store)
        unknown_time = preparation["budget_source"] == "unknown_time"
        rounds = 1 if unknown_time else self.limits.rounds_for(strategy)
        if unknown_time:
            stage_allowance = min(
                stage_allowance, self.limits.unknown_time_budget_seconds
            )
        if not enabled or stage_allowance <= 0:
            for store in enabled:
                attempts.append(
                    {
                        "store_id": store.store_id,
                        "kind": store.kind,
                        "status": "unattempted-by-budget",
                        "candidates": 0,
                        "reason": budget_reason
                        or "no remaining time inside the resolved stage allowance",
                    }
                )
            result = self.search.run(
                objective=objective, stores=[], route=route, stage_seconds=0.0, rounds=1
            )
            trace = contracts.make_search_trace(
                preparation_id=preparation["preparation_id"],
                strategy=strategy,
                rounds=0,
                attempts=attempts,
                candidates=[],
                selected_ids=[],
                delivered_ids=[],
                outcome="no_optional_memory",
            )
        else:
            result = self.search.run(
                objective=objective,
                stores=enabled,
                route=route,
                stage_seconds=stage_allowance,
                rounds=rounds,
            )
            trace = contracts.make_search_trace(
                preparation_id=preparation["preparation_id"],
                strategy=strategy,
                rounds=result.rounds,
                attempts=attempts + result.attempts,
                candidates=result.candidates,
                selected_ids=result.delivered,
                delivered_ids=result.delivered,
                outcome=result.outcome,
            )
        if self.store is not None:
            self.store.record_search_trace(trace)
            for candidate in trace["candidates"]:
                self.store.record_search_candidate(
                    preparation["preparation_id"], candidate
                )
        return {"trace": trace, "selected": list(result.delivered)}

    def _unresolved_child_operation(self, decision_id: str) -> dict[str, Any] | None:
        """Return one exact child of this decision that is still unresolved.

        Launch intent, a live child, an ambiguous acknowledgement, and pending
        cleanup all keep ownership unresolved until the exact child is
        reconciled, so nothing may relaunch while one is visible.
        """

        if self.store is None:
            return None
        try:
            operations = self.store.list_apc_child_operations(decision_id)
        except Exception:
            return None
        for operation in reversed(list(operations)):
            if operation.get("status") in contracts.APC_CHILD_UNRESOLVED_STATUSES:
                return dict(operation)
        return None

    def _canonical_objective(
        self,
        task_card: Mapping[str, Any],
        objective_id: str,
        route: str,
        failure_context: str | None,
    ) -> dict[str, Any]:
        """Build the one canonical sanitized representation both phases share.

        The exact declared task text, the exact objective identity, the
        resolved route, and the real failure context are the only inputs, so
        the bounded search and plan production compare candidates against
        literally the same representation instead of two separately
        constructed ones.

        The task, objective, and failure text are sanitized with the active
        privacy policy *before* tokenization, so a configured secret contributes
        no token to the bounded store query and no weight to the trusted reuse
        score: the one sanitized record is what both phases consume.  Only this
        query/scoring text is sanitized; durable records keep the exact
        objective identity.
        """

        task_text = sanitize_text(
            str(task_card.get("task") or "").strip(), self.privacy_policy
        )
        # The objective identity is caller-supplied too, so it is sanitized for
        # this query/scoring text exactly like the task and failure context.
        # The durable record keeps the exact objective id untouched.
        objective_text = sanitize_text(str(objective_id), self.privacy_policy)
        context_text = sanitize_text(failure_context or "", self.privacy_policy)
        parts = [part for part in (task_text, objective_text) if part]
        return templates.objective_representation(
            " ".join(parts) or objective_text or str(objective_id),
            route=route,
            limits=self.limits,
            failure_context=context_text or None,
        )

    def _registry_template(
        self, template_id: str, revision_id: str
    ) -> templates.Template | None:
        """Return the exact explicit registry record for one selected identity.

        Rejoining compares the selected candidate's exact logical id with the
        immutable registry template id and its delivered revision with the
        registry version, so a version bump cannot silently satisfy an older
        selection.
        """

        version_text = revision_id[1:] if revision_id[:1] in {"v", "V"} else revision_id
        for template in self.registry:
            if template.template_id != template_id:
                continue
            if str(template.version) == version_text:
                return template
        return None

    def _rejoin_selected_templates(
        self,
        *,
        objective: Mapping[str, Any],
        route: str,
        trace: Mapping[str, Any] | None,
        attempts: list[dict[str, Any]],
    ) -> tuple[list[tuple[templates.Template, float]], bool]:
        """Rejoin every selected template to the explicit immutable registry.

        Only template candidates this preparation trace actually *selected*
        are considered, in their delivered rank order.  Each one must match
        one explicit registry record by exact logical id, version, and content
        digest; its trusted comparable score is then recomputed from the same
        canonical representation the bounded search used, and the configured
        calibrated thresholds decide the band.  A mismatched, forged, or
        inapplicable record is rejected with its reason and is never replaced
        by a looser registry sweep.  The returned flag is true only when every
        selected candidate rejoined with a trusted comparable score, so the
        fresh-planning reason never blames the registry for a later direct-fill
        or adaptation failure.  The rejoined shortlist is ordered by that
        recomputed trusted score, never by the raw score the store supplied.
        """

        selected = [
            candidate
            for candidate in (trace or {}).get("candidates", [])
            if isinstance(candidate, Mapping)
            and candidate.get("kind") == "template"
            and candidate.get("disposition") == "selected"
        ]
        shortlist: list[tuple[templates.Template, float]] = []
        for candidate in selected:
            logical_id = candidate.get("logical_id")
            revision_id = candidate.get("revision_id")
            if not isinstance(logical_id, str) or not isinstance(revision_id, str):
                continue
            template = self._registry_template(logical_id, revision_id)
            if template is None:
                attempts.append(
                    {
                        "template_id": logical_id,
                        "branch": "shortlist",
                        "status": "rejected",
                        "reason": (
                            "the selected template does not rejoin the explicit "
                            "immutable registry by exact id/version"
                        ),
                    }
                )
                continue
            if candidate.get("payload_digest") != contracts.sha256_hex(
                template.to_record()
            ):
                attempts.append(
                    {
                        "template_id": template.template_id,
                        "branch": "shortlist",
                        "status": "rejected",
                        "reason": (
                            "the selected template content digest does not match "
                            "the immutable registry record"
                        ),
                    }
                )
                continue
            representation = templates.template_representation(
                template, limits=self.limits
            )
            if not template.representation_declared or not templates.representations_comparable(
                objective, representation
            ):
                attempts.append(
                    {
                        "template_id": template.template_id,
                        "branch": "shortlist",
                        "status": "rejected",
                        "reason": (
                            "the selected template representation is not comparable "
                            "with the canonical objective representation"
                        ),
                    }
                )
                continue
            if route not in template.routes:
                attempts.append(
                    {
                        "template_id": template.template_id,
                        "branch": "shortlist",
                        "status": "rejected",
                        "reason": (
                            f"the selected template is not applicable to route {route!r}"
                        ),
                    }
                )
                continue
            score = templates.score_representations(
                templates.projected_objective(objective), representation
            )
            if score < self.limits.near_match_threshold:
                attempts.append(
                    {
                        "template_id": template.template_id,
                        "branch": "shortlist",
                        "status": "rejected",
                        "reason": (
                            "the selected template scores below the configured "
                            "near-match threshold in the canonical representation"
                        ),
                    }
                )
                continue
            shortlist.append((template, score))
        rejoined = len(shortlist) == len(selected) and bool(selected)
        # Only the rejoined selected candidates are ordered here, by the
        # recomputed trusted score.  The delivered trace order can be set by
        # store-supplied raw scores, so it must not decide which selected
        # template is proposed; the rejoin order of equally scored candidates
        # is the deterministic tie.  No unselected registry entry is added.
        shortlist.sort(key=lambda item: -item[1])
        return shortlist, rejoined

    @staticmethod
    def _no_selected_template_reason(trace: Mapping[str, Any] | None) -> str:
        """Explain honestly why no template candidate could be selected."""

        attempts = [
            entry
            for entry in (trace or {}).get("attempts", [])
            if isinstance(entry, Mapping) and entry.get("kind") == "template"
        ]
        if not attempts:
            return (
                "no bounded template store ran in this preparation trace; fresh "
                "ROOT planning applies without template reuse or adaptation"
            )
        statuses = {str(entry.get("status")) for entry in attempts}
        if "unattempted-by-budget" in statuses:
            return (
                "the optional stage admitted no template search budget; fresh ROOT "
                "planning applies without template reuse or adaptation"
            )
        if "disabled" in statuses:
            return (
                "template selection was disabled for this preparation; fresh ROOT "
                "planning applies without template reuse or adaptation"
            )
        return (
            "the bounded eligible shortlist selected no template candidate; fresh "
            "ROOT planning applies without template reuse or adaptation"
        )

    def _fresh_plan_reason(
        self,
        *,
        resolved_config: MemoryConfig,
        trace: Mapping[str, Any] | None,
        shortlist: Sequence[tuple[templates.Template, float]],
        attempts: Sequence[Mapping[str, Any]],
        rejoined: bool,
    ) -> str:
        """Explain honestly why fresh ROOT planning applies.

        A nonempty shortlist is reported first: a selected template did rejoin
        the registry with a trusted comparable score, so a later direct-fill or
        adaptation failure is never misreported as a registry mismatch.
        """

        if not resolved_config.template_memory:
            return (
                "template memory is disabled; no template reuse or adaptation runs "
                "and normal fresh ROOT planning applies"
            )
        if shortlist:
            return (
                "no selected template produced a usable direct fill or permitted "
                "adaptation; normal fresh ROOT planning applies"
            )
        if attempts and not rejoined:
            return (
                "no selected template rejoined the explicit immutable registry with "
                "a trusted comparable score; normal fresh ROOT planning applies"
            )
        return self._no_selected_template_reason(trace)


    def _produce_plan(
        self,
        *,
        ownership_decision_id: str,
        decision: Mapping[str, Any],
        objective_id: str,
        route: str,
        objective: Mapping[str, Any],
        trace: Mapping[str, Any] | None,
        resolved_config: MemoryConfig,
        bindings: Mapping[str, Any] | None,
        apc_binding: Mapping[str, Any] | None,
        apc_launcher: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None,
        apc_cleanup: Mapping[str, Any] | None,
        deadline: float,
        unknown_time: bool,
        root_replan: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        """Produce a bounded reuse proposal from the selected shortlist only.

        BEHAVIOR-01's bounded preparation trace is the only source of template
        candidates: a template the eligible shortlist did not actually select
        is never considered, so a disabled feature, a denied optional stage, a
        missing store, or a filtered candidate cannot be routed around by a
        second registry sweep.  Each selected candidate must rejoin the
        explicit immutable registry by exact id, version, and content digest,
        and its trusted comparable score is recomputed from the same canonical
        representation the search used; the configured calibrated thresholds
        decide the band.  A mismatch is rejected, never downgraded to a looser
        registry fallback.
        """

        reuse_attempts: list[dict[str, Any]] = []
        rejoined = False
        if resolved_config.template_memory:
            shortlist, rejoined = self._rejoin_selected_templates(
                objective=objective,
                route=route,
                trace=trace,
                attempts=reuse_attempts,
            )
        else:
            shortlist = []

        for template, score in shortlist:
            if score < self.limits.direct_fill_threshold:
                continue
            if not bindings:
                reuse_attempts.append(
                    {
                        "template_id": template.template_id,
                        "branch": "direct_fill",
                        "status": "unavailable",
                        "reason": "no trusted typed bindings were supplied",
                    }
                )
                continue
            try:
                proposal = templates.direct_fill_typed(
                    template, bindings, objective_id=objective_id, route=route
                )
            except templates.TemplateError as exc:
                # A typed validation failure keeps every lower-ranked selected
                # eligible candidate available; field trust is never weakened.
                reuse_attempts.append(
                    {
                        "template_id": template.template_id,
                        "branch": "direct_fill",
                        "status": "rejected",
                        "reason": str(exc),
                    }
                )
                continue
            disposition = contracts.make_plan_disposition(
                decision_id=decision["decision_id"],
                objective_id=objective_id,
                route=route,
                branch="direct_fill",
                reason=(
                    "a selected comparable template with complete typed fields "
                    "was directly filled"
                ),
                template={
                    "template_id": template.template_id,
                    "version": template.version,
                    "family": template.family,
                    "digest": contracts.sha256_hex(template.to_record()),
                    "score": score,
                },
                proposal=proposal,
                reuse_attempts=reuse_attempts,
                root_replan=root_replan,
            )
            # A direct fill is the proposal itself; it launches no APC child.
            return disposition, proposal, None

        near = [
            (template, score)
            for template, score in shortlist
            if self.limits.near_match_threshold
            <= score
            < self.limits.direct_fill_threshold
        ]
        adaptation_available = bool(
            resolved_config.apc
            and resolved_config.light_adaptation
            and not unknown_time
            and apc_binding is not None
            and apc_launcher is not None
        )
        if near and adaptation_available:
            template, score = near[0]
            record = template.to_record()
            pending = self._unresolved_child_operation(ownership_decision_id)
            remaining = deadline - self.clock()
            if pending is not None:
                # An earlier child may still be live and its cleanup is not
                # proven.  Refuse to relaunch anything until that exact child is
                # reconciled; unresolved ownership is reported, never hidden
                # behind a budget excuse.
                reuse_attempts.append(
                    {
                        "template_id": template.template_id,
                        "branch": "apc_proposal",
                        "status": "rejected",
                        "reason": (
                            f"{pending['status']} APC child already exists; "
                            "reconcile the exact child before any retry"
                        ),
                    }
                )
            elif remaining <= self.limits.execution_reserve_seconds:
                reuse_attempts.append(
                    {
                        "template_id": template.template_id,
                        "branch": "apc_proposal",
                        "status": "unattempted-by-budget",
                        "reason": "no usable adaptation time remains inside the deadline",
                    }
                )
            else:
                try:
                    request = apc.make_apc_request(
                        template=record,
                        parent_decision_id=decision["decision_id"],
                        parent_objective_id=objective_id,
                        permitted_edits=list(template.allowed_edits),
                        binding=apc_binding,
                    )
                    attempt = harness_bridge.run_apc_child(
                        request=request,
                        template_record=record,
                        launcher=apc_launcher,
                        store=self.store,
                        limits=self.limits,
                        clock=self.clock,
                        deadline=deadline - self.limits.execution_reserve_seconds,
                        cleanup=apc_cleanup,
                    )
                except (apc.APCError, harness_bridge.HarnessBridgeError) as exc:
                    reuse_attempts.append(
                        {
                            "template_id": template.template_id,
                            "branch": "apc_proposal",
                            "status": "rejected",
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    )
                else:
                    proposal = attempt.proposal
                    disposition = contracts.make_plan_disposition(
                        decision_id=decision["decision_id"],
                        objective_id=objective_id,
                        route=route,
                        branch="apc_proposal",
                        reason="one bounded drafting child produced a proposed artifact",
                        template={
                            "template_id": template.template_id,
                            "version": template.version,
                            "family": template.family,
                            "digest": contracts.sha256_hex(record),
                            "score": score,
                        },
                        proposal=proposal,
                        apc={
                            "request_digest": attempt.child_operation["request_digest"],
                            "child_operation_id": attempt.child_operation["child_operation_id"],
                            "binding": dict(attempt.child_operation["binding"]),
                            "status": attempt.child_operation["status"],
                        },
                        reuse_attempts=reuse_attempts,
                        root_replan=root_replan,
                    )
                    return disposition, proposal, proposal

        fresh = templates.fresh_plan(objective_id=objective_id, route=route)
        disposition = contracts.make_plan_disposition(
            decision_id=decision["decision_id"],
            objective_id=objective_id,
            route=route,
            branch="fresh",
            reason=self._fresh_plan_reason(
                resolved_config=resolved_config,
                trace=trace,
                shortlist=shortlist,
                attempts=reuse_attempts,
                rejoined=rejoined,
            ),
            fresh=fresh,
            reuse_attempts=reuse_attempts,
            root_replan=root_replan,
        )
        return disposition, fresh, None


    def _finish_planning(
        self,
        *,
        mode: str,
        decision: Mapping[str, Any],
        preparation: Mapping[str, Any],
        trace: Mapping[str, Any] | None,
        disposition: Mapping[str, Any] | None,
        plan: Mapping[str, Any],
        proposal: Mapping[str, Any] | None,
        reason: str,
    ) -> PreparationOutcome:
        return PreparationOutcome(
            mode=mode,
            decision=dict(decision),
            preparation=dict(preparation),
            trace=dict(trace) if trace else None,
            disposition=dict(disposition) if disposition else None,
            plan=dict(plan),
            proposal=dict(proposal) if proposal else None,
            reason=reason,
        )

    def _finalize(
        self,
        *,
        outcome: PreparationOutcome,
        task_card: Mapping[str, Any],
        plan: Mapping[str, Any],
        lane_id: str | None,
        run_id: str | None,
        worktree_path: str | None,
        base_commit: str | None,
        mandatory_content: Iterable[Mapping[str, Any]],
        optional_items: Iterable[Mapping[str, Any]],
        freshness_check: Callable[[Mapping[str, Any]], bool] | None,
    ) -> PreparationOutcome:
        if not all((lane_id, run_id, worktree_path, base_commit)):
            raise PreparationError(
                "finalization requires the exact lane, run, worktree, and base identity"
            )
        selected = outcome.selected_candidates
        packed_optional: list[dict[str, Any]] = []
        for candidate in selected:
            packed_optional.append(
                {
                    "id": candidate["candidate_id"],
                    "kind": candidate["kind"],
                    "origin": candidate["origin"],
                    "content": candidate["payload"],
                    "revision_id": candidate["revision_id"],
                }
            )
        for item in optional_items:
            packed_optional.append(dict(item))
        finalized = context_module.finalize_context(
            task_card=task_card,
            plan=plan,
            decision_id=outcome.decision["decision_id"],
            lane_id=str(lane_id),
            run_id=str(run_id),
            worktree_path=str(worktree_path),
            base_commit=str(base_commit),
            strategy=outcome.preparation["strategy"],
            configuration=outcome.preparation["configuration"],
            mandatory_content=list(mandatory_content),
            optional_items=packed_optional,
            privacy_policy=self.privacy_policy,
            limits=self.limits,
            freshness_check=freshness_check,
        )
        if self.store is not None:
            self.store.record_final_context(
                finalized.context, envelope_digest=finalized.envelope["content_hash"]
            )
        return PreparationOutcome(
            mode="dispatchable",
            decision=outcome.decision,
            preparation=outcome.preparation,
            trace=outcome.trace,
            disposition=outcome.disposition,
            plan=plan,
            proposal=outcome.proposal,
            context=finalized.context,
            envelope=finalized.envelope,
            superseded=outcome.superseded,
            reason="the exact accepted plan was finalized into a dispatchable context",
        )


__all__ = [
    "MandatoryStateFailure",
    "PlanAcceptanceError",
    "PreparationError",
    "PreparationOutcome",
    "PreparationService",
]

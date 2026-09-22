"""One bounded preparation for one exact objective, plan, and deadline.

The coordinator resolves every feature, network, route, strategy, stage, and
reserve value *before* any optional call, keeps ROOT acceptance distinct from
proposal completion, and hands one exact finalized context to the existing
product harness.  A late Level 0 admission permanently supersedes the old
packet and permits at most one bounded ordinary re-prepare.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
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
from .privacy import PrivacyPolicy
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

    @staticmethod
    def _plan_state_error(exc: Exception, objective_id: str) -> MandatoryStateFailure:
        return MandatoryStateFailure(
            f"mandatory current-plan state is inconsistent for {objective_id!r}: {exc}"
        )

    def _recover_budget(
        self, decision_id: str, budget_source: str
    ) -> tuple[float, str, float] | None:
        """Reuse the durable absolute deadline and granted budget once.

        Restart, resume, and route correction all share one logical decision, so
        they must not hand out a fresh deadline.  The granted budget is the
        remaining time that decision originally recorded plus whatever it had
        already spent; elapsed cost is then recomputed against the current
        clock instead of being reset to zero.
        """

        if budget_source == "unknown_time" or self.store is None:
            return None
        try:
            prior = self.store.list_preparations(decision_id)
        except Exception:
            return None
        for record in reversed(prior):
            deadline = record.get("deadline_monotonic")
            if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
                continue
            remaining = float(record.get("remaining_seconds") or 0.0)
            spent = float(record.get("spent_seconds") or 0.0)
            source = record.get("budget_source")
            return float(deadline), str(source or "trusted_deadline"), remaining + spent
        return None

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
        # One decision owns one deadline.  A restart, resume, or route
        # correction reuses the durable absolute deadline, the budget that was
        # originally granted, and the cost already paid, instead of
        # replenishing the objective's budget.  An explicitly injected
        # deadline is trusted input for this call and is honoured as given.
        recovered = (
            None
            if explicit_deadline
            else self._recover_budget(provisional["decision_id"], budget_source)
        )
        if recovered is not None:
            absolute_deadline, budget_source, granted_budget = recovered
        else:
            granted_budget = None
        remaining = max(0.0, absolute_deadline - now) if not unknown_time else 0.0
        if granted_budget is None:
            granted_budget = remaining
        spent_seconds = max(0.0, granted_budget - remaining)
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
            attempt=self._next_attempt(decision["decision_id"]),
        )
        if self.store is not None:
            self.store.record_preparation(preparation)

        accepted_precedence = current_plan_state == "execution_accepted"
        replan = self._validate_root_replan(root_replan)
        # Template selection and adaptation run only for an explicit ROOT
        # replan request.  An accepted plan is preserved, and a pending
        # candidate keeps its exact identity and state until ROOT decides.
        template_selection = replan is not None and not accepted_precedence
        search = self._run_search(
            preparation=preparation,
            route=route,
            strategy=resolved_config.strategy,
            stores=stores,
            stage_allowance=stage_allowance,
            failure_context=failure_context,
            accepted_precedence=not template_selection,
            config=resolved_config,
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
                task_card=task_card,
                resolved_config=resolved_config,
                bindings=bindings,
                apc_binding=apc_binding,
                apc_launcher=apc_launcher,
                apc_cleanup=apc_cleanup,
                deadline=absolute_deadline,
                unknown_time=unknown_time,
                failure_context=failure_context,
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
        replacement_config = self._resolve(None)
        unknown_time = preparation["budget_source"] == "unknown_time"
        remaining_value = None if remaining is None else max(0.0, float(remaining))
        if granted is None:
            spent = prior_spent
            remaining_value = prior_remaining
        else:
            spent = max(0.0, granted - float(remaining_value or 0.0))
        # Re-resolve the effective fixed strategy and configuration for the one
        # bounded ordinary re-prepare instead of replaying the abandoned
        # packet's recorded values.
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
            route=route,
            strategy=replacement_config.strategy,
            stores=stores,
            stage_allowance=stage_allowance,
            failure_context=failure_context,
            accepted_precedence=False,
            config=replacement_config,
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
        route: str,
        strategy: str,
        stores: Sequence[SearchStore],
        stage_allowance: float,
        failure_context: str | None,
        accepted_precedence: bool,
        config: MemoryConfig,
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
                        "reason": "the resolved configuration disables this store",
                    }
                )
                continue
            enabled.append(store)
        objective = templates.objective_representation(
            self._objective_text(preparation, preparation["objective_id"]),
            route=route,
            limits=self.limits,
            failure_context=failure_context,
        )
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

    def _objective_text(self, preparation: Mapping[str, Any], objective_id: str) -> str:
        requested = preparation.get("requested_strategy")
        return f"{objective_id} {requested}" if isinstance(requested, str) else objective_id


    def _produce_plan(
        self,
        *,
        ownership_decision_id: str,
        decision: Mapping[str, Any],
        objective_id: str,
        route: str,
        task_card: Mapping[str, Any],
        resolved_config: MemoryConfig,
        bindings: Mapping[str, Any] | None,
        apc_binding: Mapping[str, Any] | None,
        apc_launcher: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None,
        apc_cleanup: Mapping[str, Any] | None,
        deadline: float,
        unknown_time: bool,
        failure_context: str | None,
        root_replan: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        task_text = str(task_card.get("task", objective_id))
        objective = templates.objective_representation(
            f"{task_text} {objective_id}",
            route=route,
            limits=self.limits,
            failure_context=failure_context,
        )
        matches = templates.rank_templates(objective, self.registry, limits=self.limits)
        reuse_attempts: list[dict[str, Any]] = []
        if not resolved_config.template_memory:
            matches = []

        for match in templates.direct_fill_band(matches, limits=self.limits):
            if not bindings:
                reuse_attempts.append(
                    {
                        "template_id": match.template.template_id,
                        "branch": "direct_fill",
                        "status": "unavailable",
                        "reason": "no trusted typed bindings were supplied",
                    }
                )
                continue
            try:
                proposal = templates.direct_fill_typed(
                    match.template, bindings, objective_id=objective_id, route=route
                )
            except templates.TemplateError as exc:
                reuse_attempts.append(
                    {
                        "template_id": match.template.template_id,
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
                reason="a comparable template with complete typed fields was directly filled",
                template={
                    "template_id": match.template.template_id,
                    "version": match.template.version,
                    "family": match.template.family,
                    "digest": contracts.sha256_hex(match.template.to_record()),
                    "score": match.score,
                },
                proposal=proposal,
                reuse_attempts=reuse_attempts,
                root_replan=root_replan,
            )
            # A direct fill is the proposal itself; it launches no APC child.
            return disposition, proposal, None

        near = templates.near_match_band(matches, limits=self.limits)
        adaptation_available = bool(
            resolved_config.apc
            and resolved_config.light_adaptation
            and not unknown_time
            and apc_binding is not None
            and apc_launcher is not None
        )
        if near and adaptation_available:
            match = near[0]
            record = match.template.to_record()
            pending = self._unresolved_child_operation(ownership_decision_id)
            remaining = deadline - self.clock()
            if pending is not None:
                # An earlier child may still be live and its cleanup is not
                # proven.  Refuse to relaunch anything until that exact child is
                # reconciled; unresolved ownership is reported, never hidden
                # behind a budget excuse.
                reuse_attempts.append(
                    {
                        "template_id": match.template.template_id,
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
                        "template_id": match.template.template_id,
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
                        permitted_edits=list(match.template.allowed_edits),
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
                            "template_id": match.template.template_id,
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
                            "template_id": match.template.template_id,
                            "version": match.template.version,
                            "family": match.template.family,
                            "digest": contracts.sha256_hex(record),
                            "score": match.score,
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
            reason="no comparable direct-fill or permitted adaptation applied; normal fresh planning",
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

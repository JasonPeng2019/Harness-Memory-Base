"""A small versioned local plan-template registry for the Stage-A slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import contracts


class TemplateError(ValueError):
    """A template or its bindings are invalid."""


class MissingBindingsError(TemplateError):
    """A required task-specific binding is missing."""


@dataclass(frozen=True)
class Template:
    template_id: str
    version: int
    family: str
    fixed_steps: tuple[str, ...]
    required_fields: tuple[str, ...]
    allowed_edits: tuple[str, ...]
    verification_intent: str
    routes: tuple[str, ...] = ("ordinary",)
    keywords: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "family": self.family,
            "fixed_steps": list(self.fixed_steps),
            "required_fields": list(self.required_fields),
            "allowed_edits": list(self.allowed_edits),
            "verification_intent": self.verification_intent,
            "routes": list(self.routes),
        }


_REGRESSION_REPAIR = Template(
    template_id="ordinary-regression-repair/v1",
    version=1,
    family="Regression repair",
    fixed_steps=(
        "Establish the concrete failure and affected component.",
        "Run one discriminating check before editing.",
        "Make the bounded repair.",
        "Re-run the focused check and affected integration checks.",
    ),
    required_fields=("failure", "component"),
    allowed_edits=("bindings",),
    verification_intent="Focused failure check passes and affected regression suite is green.",
    keywords=("regression", "failure", "test", "repair"),
)

_PUBLIC_INTERFACE_CHANGE = Template(
    template_id="ordinary-public-interface-change/v1",
    version=1,
    family="Public interface change",
    fixed_steps=(
        "Establish the requested contract and enumerate consumers.",
        "Change the interface and implementation coherently.",
        "Preserve required compatibility or provide the approved migration.",
        "Verify consumers and compatibility checks.",
    ),
    required_fields=("contract", "consumers", "compatibility_policy"),
    allowed_edits=("bindings",),
    verification_intent="Contract tests and required consumer compatibility checks pass.",
    keywords=("interface", "api", "contract", "consumer"),
)

_DEPENDENCY_UPGRADE = Template(
    template_id="ordinary-dependency-upgrade/v1",
    version=1,
    family="Dependency upgrade",
    fixed_steps=(
        "Establish the dependency and exact target version.",
        "Update the declared dependency and lock surfaces.",
        "Repair concrete fallout only.",
        "Re-run dependency and affected tests.",
    ),
    required_fields=("dependency", "target_version"),
    allowed_edits=("bindings",),
    verification_intent="Dependency resolution and affected tests pass at the target version.",
    keywords=("dependency", "upgrade", "version"),
)

_SCHEMA_MIGRATION = Template(
    template_id="ordinary-schema-data-migration/v1",
    version=1,
    family="Schema/data migration",
    fixed_steps=(
        "Establish the current and required data states.",
        "Preserve compatibility and an explicit recovery path.",
        "Change related schema/data surfaces together.",
        "Verify before, after, and failure cases.",
    ),
    required_fields=("current_state", "required_state", "rollback_policy"),
    allowed_edits=("bindings",),
    verification_intent="Before/after migration checks and the declared failure path pass.",
    keywords=("schema", "data", "migration"),
)

_INTEGRATION_FAILURE = Template(
    template_id="ordinary-integration-failure/v1",
    version=1,
    family="Integration-failure investigation",
    fixed_steps=(
        "Reproduce and localize the boundary failure.",
        "Discriminate between competing hypotheses.",
        "Repair after localization.",
        "Re-run the integration boundary checks.",
    ),
    required_fields=("boundary", "reproduction"),
    allowed_edits=("bindings",),
    verification_intent="The localized integration boundary check passes.",
    keywords=("integration", "boundary", "external", "integration failure"),
)


def load_default_templates() -> tuple[Template, ...]:
    return (
        _REGRESSION_REPAIR,
        _PUBLIC_INTERFACE_CHANGE,
        _DEPENDENCY_UPGRADE,
        _SCHEMA_MIGRATION,
        _INTEGRATION_FAILURE,
    )


def select_template(objective_text: str, registry: Iterable[Template]) -> Template | None:
    text = objective_text.lower()
    best: tuple[int, Template] | None = None
    for template in registry:
        score = sum(1 for keyword in template.keywords if keyword in text)
        if score and (best is None or score > best[0]):
            best = (score, template)
    return best[1] if best is not None else None


def validate_bindings(template: Template, bindings: Mapping[str, Any]) -> None:
    if not isinstance(bindings, Mapping):
        raise TemplateError("bindings must be an object")
    missing = [field for field in template.required_fields if field not in bindings]
    if missing:
        raise MissingBindingsError(f"missing required template bindings: {', '.join(missing)}")
    for field in template.required_fields:
        if bindings[field] is None:
            raise MissingBindingsError(f"required template binding {field!r} is null")


def direct_fill(
    template: Template,
    bindings: Mapping[str, Any],
    *,
    objective_id: str,
    route: str,
) -> dict[str, Any]:
    """Fill only declared bindings while preserving the template structure."""

    validate_bindings(template, bindings)
    if route not in template.routes:
        raise TemplateError(f"template is not applicable to route {route!r}")
    content = {
        "fixed_steps": list(template.fixed_steps),
        "bindings": dict(bindings),
        "verification_intent": template.verification_intent,
    }
    return contracts.make_plan(
        plan_id=f"{template.template_id}:{objective_id}",
        objective_id=objective_id,
        route=route,
        state="proposed",
        content=content,
        source={
            "template_id": template.template_id,
            "template_version": template.version,
            "branch": "direct_fill",
        },
    )


def fresh_plan(*, objective_id: str, route: str, content: Any | None = None) -> dict[str, Any]:
    """Return a fresh-plan record with no reusable template provenance."""

    return contracts.make_plan(
        plan_id=f"fresh:{objective_id}",
        objective_id=objective_id,
        route=route,
        state="fresh",
        content=content if content is not None else {"steps": []},
    )


__all__ = [
    "Template",
    "TemplateError",
    "MissingBindingsError",
    "load_default_templates",
    "select_template",
    "validate_bindings",
    "direct_fill",
    "fresh_plan",
]

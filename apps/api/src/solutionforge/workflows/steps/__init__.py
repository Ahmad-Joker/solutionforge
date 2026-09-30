"""Step type implementations."""

from solutionforge.workflows.registry import StepRegistry
from solutionforge.workflows.steps.builtin import builtin_handlers


def default_registry() -> StepRegistry:
    return StepRegistry(builtin_handlers())

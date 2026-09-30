"""Step type implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from solutionforge.workflows.registry import StepRegistry
from solutionforge.workflows.steps.builtin import builtin_handlers

if TYPE_CHECKING:
    from solutionforge.llm.service import LLMService


def default_registry(llm_service: LLMService | None = None) -> StepRegistry:
    registry = StepRegistry(builtin_handlers())
    if llm_service is not None:
        from solutionforge.workflows.steps.llm import LLMStep

        registry.register(LLMStep(llm_service))
    return registry

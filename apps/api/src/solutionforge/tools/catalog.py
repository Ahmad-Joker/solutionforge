"""The set of tool implementations this deployment offers (tenants opt in per tool)."""

from __future__ import annotations

import re

from solutionforge.tools.spec import TOOL_NAME_PATTERN, Tool, ToolNotFound, ToolSpec


class ToolCatalog:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            if not re.match(TOOL_NAME_PATTERN, t.spec.name):
                raise ValueError(f"invalid tool name {t.spec.name!r}")
            if t.spec.name in self._tools:
                raise ValueError(f"duplicate tool {t.spec.name!r}")
            self._tools[t.spec.name] = t

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFound(f"unknown tool {name!r}") from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def specs(self) -> list[ToolSpec]:
        return [self._tools[n].spec for n in sorted(self._tools)]


def default_catalog() -> ToolCatalog:
    from solutionforge.connectors.simulated import SIMULATED_TOOLS

    return ToolCatalog(SIMULATED_TOOLS)

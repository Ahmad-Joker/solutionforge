"""Build the process-wide ToolExecutor from settings."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.config import Settings
from solutionforge.security.crypto import CredentialCipher
from solutionforge.tools.catalog import default_catalog
from solutionforge.tools.executor import ToolExecutor


def build_tool_executor(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> ToolExecutor:
    return ToolExecutor(
        default_catalog(), sessionmaker, CredentialCipher(settings.credentials_key_list)
    )

"""Build the process-wide LLMService from settings (API and worker share this)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.config import Settings
from solutionforge.core.logging import get_logger
from solutionforge.llm.breaker import CircuitBreaker
from solutionforge.llm.pricing import PriceTable
from solutionforge.llm.providers.mock import MockProvider
from solutionforge.llm.service import LLMService
from solutionforge.llm.types import LLMProvider

log = get_logger(__name__)


def build_llm_service(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> LLMService:
    from solutionforge.services.usage_service import DbUsageLedger  # avoid import cycle

    providers: dict[str, LLMProvider] = {}
    if settings.llm_enable_mock:
        providers["mock"] = MockProvider()
    if settings.anthropic_api_key is not None:
        from solutionforge.llm.providers.claude import AnthropicProvider

        providers["anthropic"] = AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
    log.info("llm_providers_configured", providers=sorted(providers))
    return LLMService(
        providers=providers,
        prices=PriceTable().with_overrides(settings.llm_price_overrides),
        ledger=DbUsageLedger(sessionmaker),
        breaker=CircuitBreaker(
            failure_threshold=settings.llm_breaker_failure_threshold,
            recovery_seconds=settings.llm_breaker_recovery_seconds,
        ),
    )

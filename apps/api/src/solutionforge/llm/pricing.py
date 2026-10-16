"""Model price table and exact cost arithmetic.

Money is integer **micro-USD** (1e-6 USD) end to end: no floats, so sums in the ledger are
exact and budget comparisons are deterministic. Costs round *up* to the next micro-dollar.

Invariant: a model without a price entry cannot be used (definitions referencing it fail to
compile), so every metered call has a known cost.

Sources:
- ``anthropic:*`` list prices in USD per million tokens, read from ``PRICES_SOURCE_URL`` on
  ``PRICES_ACCESSED`` (global routing; first-party API). Categories: base input, output,
  cache reads, 5-minute cache writes and 1-hour cache writes. Claude Haiku 5.5 has a
  long-context tier: a request whose *prompt* (input + cache reads + cache writes) exceeds
  100,000 tokens is billed entirely at the higher prices. Client-tool definitions and the
  tool-use system prompt are ordinary input tokens (already in reported usage); there is no
  separate per-call fee for client tools. Not modelled: the 1.1x US-only ``inference_geo``
  multiplier (this app never sets it) and Batch API discounts (not used).
  These are *estimates* from list prices, not an invoice. Override via
  ``SF_LLM_PRICE_OVERRIDES``.
- ``mock:*`` prices are **synthetic**, chosen so budget logic is exercised in tests and demos.
  They are not a real price.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal

from solutionforge.llm.types import ModelRef, Usage

PRICES_SOURCE_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICES_ACCESSED = "2026-10-16"
PRICES_CURRENCY = "USD"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens. ``cache_write`` is the 5-minute cache-write price;
    ``cache_write_1h`` the 1-hour one (Anthropic: 2x base input when not listed)."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal
    cache_write_1h: Decimal | None = None
    # Long-context tier: prompts above ``long_context_threshold`` tokens use these prices.
    long_context_threshold: int | None = None
    long_context: ModelPrice | None = None

    @classmethod
    def of(
        cls,
        input: str,
        output: str,
        cache_read: str,
        cache_write: str,
        cache_write_1h: str | None = None,
    ) -> ModelPrice:
        return cls(
            Decimal(input),
            Decimal(output),
            Decimal(cache_read),
            Decimal(cache_write),
            Decimal(cache_write_1h) if cache_write_1h is not None else None,
        )

    def with_long_context(self, threshold: int, price: ModelPrice) -> ModelPrice:
        return replace(self, long_context_threshold=threshold, long_context=price)

    def tier_for(self, usage: Usage) -> ModelPrice:
        prompt = (
            usage.input_tokens
            + usage.cache_read_tokens
            + usage.cache_write_tokens
            + usage.cache_write_1h_tokens
        )
        if (
            self.long_context is not None
            and self.long_context_threshold is not None
            and prompt > self.long_context_threshold
        ):
            return self.long_context
        return self

    def cost_micro_usd(self, usage: Usage) -> int:
        p = self.tier_for(usage)
        write_1h = p.cache_write_1h if p.cache_write_1h is not None else p.input * 2
        # ($ per 1e6 tokens) x tokens == micro-dollars exactly: no scaling needed.
        total = (
            p.input * usage.input_tokens
            + p.output * usage.output_tokens
            + p.cache_read * usage.cache_read_tokens
            + p.cache_write * usage.cache_write_tokens
            + write_1h * usage.cache_write_1h_tokens
        )
        return int(total.to_integral_value(rounding=ROUND_CEILING))

    def worst_case_micro_usd(self, input_tokens: int, max_output_tokens: int) -> int:
        """Upper bound for a call before it happens (used for budget pre-checks)."""
        return self.cost_micro_usd(
            Usage(input_tokens=input_tokens, output_tokens=max_output_tokens)
        )


DEFAULT_PRICES: dict[str, ModelPrice] = {
    # Current models (input, output, cache read, 5m cache write, 1h cache write).
    "anthropic:claude-opus-5-5": ModelPrice.of("4.00", "20.00", "0.20", "5.00", "8.00"),
    "anthropic:claude-sonnet-5-5": ModelPrice.of("2.00", "10.00", "0.10", "2.50", "4.00"),
    "anthropic:claude-haiku-5-5": ModelPrice.of(
        "0.10", "0.50", "0.01", "0.125", "0.20"
    ).with_long_context(100_000, ModelPrice.of("0.50", "2.50", "0.05", "0.625", "1.00")),
    # Legacy models still offered (same source and date).
    "anthropic:claude-opus-5": ModelPrice.of("5.00", "25.00", "0.50", "6.25", "10.00"),
    "anthropic:claude-sonnet-5": ModelPrice.of("2.00", "10.00", "0.20", "2.50", "4.00"),
    "anthropic:claude-haiku-4-5": ModelPrice.of("1.00", "5.00", "0.10", "1.25", "2.00"),
    # Synthetic (see module docstring).
    "mock:mock-1": ModelPrice.of("1.00", "2.00", "0.10", "1.25"),
    "mock:mock-fast": ModelPrice.of("0.25", "0.50", "0.025", "0.3125"),
}


class PriceTable:
    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices = dict(DEFAULT_PRICES if prices is None else prices)

    def with_overrides(self, overrides: dict[str, dict[str, str]]) -> PriceTable:
        merged = dict(self._prices)
        for ref, p in overrides.items():
            ModelRef.parse(ref)
            merged[ref] = ModelPrice.of(
                p["input"], p["output"], p["cache_read"], p["cache_write"], p.get("cache_write_1h")
            )
        return PriceTable(merged)

    def get(self, ref: ModelRef) -> ModelPrice | None:
        return self._prices.get(str(ref))

    def models(self) -> list[str]:
        return sorted(self._prices)


def micro_to_usd(micro: int) -> Decimal:
    return (Decimal(micro) / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def usd_to_micro(usd: Decimal) -> int:
    return int((usd * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


def estimate_tokens(text: str) -> int:
    """Conservative pre-call estimate (~4 chars/token, rounded up). Used only for budget
    pre-checks; billing always uses provider-reported usage."""
    return (len(text) + 3) // 4

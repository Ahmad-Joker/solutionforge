"""Model price table and exact cost arithmetic.

Money is integer **micro-USD** (1e-6 USD) end to end: no floats, so sums in the ledger are
exact and budget comparisons are deterministic. Costs round *up* to the next micro-dollar.

Invariant: a model without a price entry cannot be used (definitions referencing it fail to
compile), so every metered call has a known cost.

Sources:
- ``anthropic:*`` list prices from Anthropic's published first-party API pricing (snapshot
  2026-06-24). Cache write = 1.25x input and cache read = 0.1x input (5-minute cache).
  Verify against https://www.anthropic.com/pricing before relying on them for billing;
  override via ``SF_LLM_PRICE_OVERRIDES``.
- ``mock:*`` prices are **synthetic**, chosen so budget logic is exercised in tests and demos.
  They are not a real price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from solutionforge.llm.types import ModelRef, Usage


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal

    @classmethod
    def of(cls, input: str, output: str, cache_read: str, cache_write: str) -> ModelPrice:
        return cls(Decimal(input), Decimal(output), Decimal(cache_read), Decimal(cache_write))

    def cost_micro_usd(self, usage: Usage) -> int:
        # ($ per 1e6 tokens) x tokens == micro-dollars exactly: no scaling needed.
        total = (
            self.input * usage.input_tokens
            + self.output * usage.output_tokens
            + self.cache_read * usage.cache_read_tokens
            + self.cache_write * usage.cache_write_tokens
        )
        return int(total.to_integral_value(rounding=ROUND_CEILING))

    def worst_case_micro_usd(self, input_tokens: int, max_output_tokens: int) -> int:
        """Upper bound for a call before it happens (used for budget pre-checks)."""
        return self.cost_micro_usd(
            Usage(input_tokens=input_tokens, output_tokens=max_output_tokens)
        )


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "anthropic:claude-opus-5": ModelPrice.of("5.00", "25.00", "0.50", "6.25"),
    "anthropic:claude-sonnet-5": ModelPrice.of("2.00", "10.00", "0.20", "2.50"),
    "anthropic:claude-haiku-4-5": ModelPrice.of("1.00", "5.00", "0.10", "1.25"),
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
            merged[ref] = ModelPrice.of(p["input"], p["output"], p["cache_read"], p["cache_write"])
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

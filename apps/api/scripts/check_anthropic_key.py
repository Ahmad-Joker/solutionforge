"""Check that a configured Anthropic API key works, without spending anything.

    python scripts/check_anthropic_key.py anthropic:claude-haiku-5-5 anthropic:claude-sonnet-5-5

Reads SF_ANTHROPIC_API_KEY from the shell or apps/api/.env (via Settings). It only calls
the Models API (``models.retrieve``), which does not generate tokens. It never prints the
key. For each model it also confirms a price is configured, because the assistant refuses
models whose budget it cannot enforce.

Exit codes: 0 = key valid and every model available and priced; 1 = a model problem;
2 = no key configured; 3 = key rejected or API unreachable.
"""

from __future__ import annotations

import sys

import anthropic

from solutionforge.core.config import Settings
from solutionforge.llm.pricing import PRICES_ACCESSED, PriceTable
from solutionforge.llm.types import ModelRef


def main(argv: list[str]) -> int:
    refs = [ModelRef.parse(a) for a in (argv or ["anthropic:claude-haiku-5-5"])]
    settings = Settings()
    if settings.anthropic_api_key is None:
        print(
            "NO KEY: set SF_ANTHROPIC_API_KEY in apps/api/.env or the shell (see docs/ASSISTANT.md)"
        )
        return 2
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), max_retries=1
    )
    prices = PriceTable().with_overrides(settings.llm_price_overrides)
    problems = 0
    for ref in refs:
        if ref.provider != "anthropic":
            print(f"skip {ref}: not an Anthropic model")
            continue
        try:
            info = client.models.retrieve(ref.model)
        except anthropic.AuthenticationError:
            print("KEY REJECTED (401): check the value configured in SF_ANTHROPIC_API_KEY")
            return 3
        except anthropic.PermissionDeniedError:
            print(f"{ref}: key lacks access (403)")
            problems += 1
            continue
        except anthropic.NotFoundError:
            print(f"{ref}: model not found for this key")
            problems += 1
            continue
        except anthropic.APIConnectionError:
            print("API UNREACHABLE: network problem")
            return 3
        priced = prices.get(ref) is not None
        print(
            f"ok {ref}: {getattr(info, 'display_name', info.id)}; "
            f"price configured: {'yes (list prices ' + PRICES_ACCESSED + ')' if priced else 'NO'}"
        )
        problems += 0 if priced else 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Deterministic PII detection and redaction.

Rule-based on purpose: predictable, auditable, testable, free. It catches *structured*
identifiers (emails, payment cards, IBANs, US SSNs, phone numbers) with checksum validation
where one exists (Luhn, mod-97) to keep false positives down. It does **not** find names or
free-form addresses — that needs NER and is a separate, probabilistic control.

Patterns are ASCII-only (``re.ASCII``), so lookalike digits cannot be used to slip past
the checksum validators.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Any, Literal

PIIKind = Literal["email", "iban", "card", "us_ssn", "phone"]
ALL_KINDS: tuple[PIIKind, ...] = ("email", "iban", "card", "us_ssn", "phone")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _iban_ok(raw: str) -> bool:
    s = raw[4:] + raw[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s, flags=re.ASCII)


_RULES: list[tuple[PIIKind, re.Pattern[str], Callable[[str], bool]]] = [
    (
        "email",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", re.ASCII),
        lambda m: True,
    ),
    (
        "iban",
        re.compile(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]){11,30}\b", re.ASCII),
        lambda m: _iban_ok(m.replace(" ", "")),
    ),
    (
        "card",
        re.compile(r"(?<![0-9])[0-9](?:[ -]?[0-9]){12,18}(?![0-9])", re.ASCII),
        lambda m: _luhn(_digits(m)),
    ),
    (
        "us_ssn",
        re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])", re.ASCII),
        lambda m: not m.startswith(("000", "666", "9")),
    ),
    # International (+…) needs ≥8 digits; otherwise ≥10 digits, which skips ISO dates.
    (
        "phone",
        re.compile(
            r"(?<![\w+])(?:\+[0-9][0-9 ().-]{6,}[0-9]|\(?[0-9][0-9 ().-]{8,}[0-9])"
            r"(?![0-9])",
            re.ASCII,
        ),
        lambda m: 8 <= len(_digits(m)) <= 15 and (m.startswith("+") or len(_digits(m)) >= 10),
    ),
]


def redact_text(text: str, kinds: tuple[PIIKind, ...] = ALL_KINDS) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    for kind, pattern, valid in _RULES:
        if kind not in kinds:
            continue

        def repl(m: re.Match[str], kind: str = kind, valid: Callable[[str], bool] = valid) -> str:
            if not valid(m.group(0)):
                return m.group(0)
            counts[kind] += 1
            return f"[{kind.upper()}]"

        text = pattern.sub(repl, text)
    return text, counts


def redact(value: Any, kinds: tuple[PIIKind, ...] = ALL_KINDS) -> tuple[Any, Counter[str]]:
    """Redact every string inside ``value`` (dict keys are left alone)."""
    total: Counter[str] = Counter()

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            out, c = redact_text(v, kinds)
            total.update(c)
            return out
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(value), total

"""Text-safety primitives and PII redaction (unit + property tests)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from solutionforge.core.text import has_nul, multiline, scrub_nul, single_line
from solutionforge.security.pii import redact, redact_text

# Characters that act as line breaks for str.splitlines / mail software, plus bidi overrides.
BREAKERS = ["\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
BIDI = ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"]


@pytest.mark.parametrize("ch", [*BREAKERS, *BIDI, "\x00", "\x1b", "\x7f"])
def test_single_line_rejects_breaks_controls_and_bidi(ch: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        single_line(f"Subject{ch}Bcc: x@evil.example")


@given(st.text())
def test_single_line_accepts_exactly_what_splitlines_considers_one_line(s: str) -> None:
    try:
        single_line(s)
    except ValueError:
        return
    assert len(s.splitlines()) <= 1 and "\x00" not in s


def test_multiline_allows_newlines_and_tabs_only() -> None:
    assert multiline("a\nb\r\n\tc — 注文 שלום") == "a\nb\r\n\tc — 注文 שלום"
    for ch in ("\x00", "\x1b", "\u2028", "\x0b"):
        with pytest.raises(ValueError, match="control characters"):
            multiline(f"a{ch}b")


def test_nul_detection_and_scrubbing_cover_nested_keys_and_values() -> None:
    value = {"a": ["ok", {"b\x00": 1}], "c": ("x", "y\x00z"), "n": 5, "f": None}
    assert has_nul(value) and not has_nul({"a": ["ok", {"b": "\\u0000"}]})
    assert scrub_nul(value) == {"a": ["ok", {"b�": 1}], "c": ["x", "y�z"], "n": 5, "f": None}


@given(
    st.recursive(
        st.text() | st.integers() | st.none(),
        lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(), c, max_size=3),
        max_leaves=10,
    )
)
def test_scrubbed_values_never_contain_nul(value: object) -> None:
    assert not has_nul(scrub_nul(value))


# --------------------------------------------------------------------------- PII


def _luhn_complete(prefix: str) -> str:
    for d in "0123456789":
        cand = prefix + d
        total = 0
        for i, ch in enumerate(reversed(cand)):
            n = int(ch)
            if i % 2 == 1:
                n = n * 2 - 9 if n > 4 else n * 2
            total += n
        if total % 10 == 0:
            return cand
    raise AssertionError


@given(
    st.text(alphabet=st.characters(whitelist_categories=("Ll", "Zs")), max_size=30),
    st.from_regex(r"[a-z]{1,8}\.[a-z]{1,8}@[a-z]{2,8}\.(com|org|io)", fullmatch=True),
    st.from_regex(r"4[0-9]{14}", fullmatch=True),
)
def test_emails_and_valid_cards_never_survive_redaction(noise: str, email: str, pan15: str) -> None:
    card = _luhn_complete(pan15)
    spaced = " ".join(card[i : i + 4] for i in range(0, 16, 4))
    text = f"{noise} contact {email} pay {spaced} {noise}"
    out, counts = redact_text(text)
    assert email not in out and card not in out and spaced not in out
    assert counts["email"] == 1 and counts["card"] == 1
    assert redact_text(out)[0] == out  # idempotent


@pytest.mark.parametrize(
    "benign",
    [
        "Order O-100123 for customer C-1001 shipped on 2026-10-01 at 14:30",
        "Card ending 1111, total 4111 1111 1111 1112 is not a valid number",  # fails Luhn
        "Version 1.2.3, port 8080, IP 192.168.0.1, amount 1999 cents",
        "SSN-shaped 000-12-3456 is invalid",
    ],
)
def test_benign_identifiers_are_left_alone(benign: str) -> None:
    out, counts = redact_text(benign)
    assert out == benign and not counts


def test_redact_walks_structures_and_respects_kinds() -> None:
    value = {
        "to": "ada@example.com",
        "notes": ["call +44 20 7946 0958", 3],
        "iban": "DE89 3704 0044 0532 0130 00",
    }
    out, counts = redact(value)
    assert out == {"to": "[EMAIL]", "notes": ["call [PHONE]", 3], "iban": "[IBAN]"}
    assert counts == {"email": 1, "phone": 1, "iban": 1}
    only_email, _ = redact(value, ("email",))
    assert only_email["notes"] == ["call +44 20 7946 0958", 3]


def test_ambiguous_long_digit_runs_fail_safe_toward_redaction() -> None:
    """A checksum-invalid IBAN is not an IBAN, but its 12-digit run still looks like a phone
    number and is masked. Over-redaction is the intended failure direction."""
    out, counts = redact_text("GB00 WEST 1234 5698 7654 32")
    assert out == "GB00 WEST [PHONE]" and counts == {"phone": 1}


def test_lookalike_digits_are_not_treated_as_numbers() -> None:
    arabic = "٤١١١" * 4  # "4111…" in Arabic-Indic digits
    assert redact_text(arabic) == (arabic, {})

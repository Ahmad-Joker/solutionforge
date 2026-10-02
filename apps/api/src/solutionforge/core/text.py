"""Text safety primitives shared by API validation, tool contracts and persistence.

- **NUL** (U+0000) is rejected at every input boundary: PostgreSQL ``text`` and ``jsonb``
  cannot store it, so letting it through turns into a server error (or, for model/connector
  output, a checkpoint that can never be written). Data we did not accept from a user —
  model output, connector responses — is *scrubbed* instead (``scrub_nul``).
- **Single-line fields** (email subjects, reasons) reject every character Python or mail
  software may treat as a line break — not just CR/LF but VT, FF, FS/GS/RS, NEL, U+2028/9 —
  and bidi override/isolate controls, which spoof how text is displayed.
"""

from __future__ import annotations

import unicodedata
from typing import Any

NUL = "\x00"
REPLACEMENT = "�"
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_MULTILINE_ALLOWED = frozenset("\n\r\t")


def has_nul(value: Any) -> bool:
    """True if any string (or dict key) anywhere inside ``value`` contains NUL."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if NUL in v:
                return True
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list | tuple):
            stack.extend(v)
    return False


def scrub_nul(value: Any) -> Any:
    """Replace NUL with U+FFFD in every string inside ``value`` (structure preserved)."""
    if isinstance(value, str):
        return value.replace(NUL, REPLACEMENT) if NUL in value else value
    if isinstance(value, dict):
        return {scrub_nul(k): scrub_nul(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub_nul(v) for v in value]
    return value


def single_line(value: str) -> str:
    """Pydantic validator: one visual line, no control or bidi-override characters."""
    for ch in value:
        if ch in _BIDI_CONTROLS:
            raise ValueError("bidirectional control characters are not allowed")
        if unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
            raise ValueError("control characters and line breaks are not allowed")
    return value


def multiline(value: str) -> str:
    """Pydantic validator: free text; newlines and tabs allowed, other controls not."""
    for ch in value:
        if ch not in _MULTILINE_ALLOWED and unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
            raise ValueError("control characters are not allowed")
    return value

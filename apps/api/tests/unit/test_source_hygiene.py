"""Trojan-Source guard: no bidi controls or invisible characters in source files.

Such characters make code display differently from how it executes (CVE-2021-42574). Tests
that need them must spell them as escapes (``"\u202e"``).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".yml", ".yaml", ".json", ".md", ".toml"}
SKIP = {"node_modules", ".next", ".git", ".venv", "__pycache__", "test-results"}


def test_no_invisible_or_bidi_characters_in_source() -> None:
    offenders = []
    for path in ROOT.rglob("*"):
        if path.suffix not in SUFFIXES or SKIP & set(path.parts) or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            bad = [
                c
                for c in line
                if c not in "\t\r" and unicodedata.category(c) in ("Cf", "Cc", "Zl", "Zp")
            ]
            if bad:
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {[hex(ord(c)) for c in bad]}")
    assert not offenders, "\n".join(offenders)

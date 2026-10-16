"""Source references: ``[DOC-ID#N]`` where ``DOC-ID`` is a document's stable ``doc_id`` and
``N`` the chunk ordinal. A citation is *valid* only if that chunk was returned by a
``search_documents`` call in the same run. Validity is necessary, not sufficient: whether the
text actually supports the answer is judged separately (see docs/ASSISTANT.md, grounding).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CITATION_RE = re.compile(r"\[([A-Z][A-Z0-9-]{1,40}#[0-9]{1,4})\]")


def source_id(doc_id: str, ordinal: int) -> str:
    return f"{doc_id}#{ordinal}"


@dataclass(frozen=True, slots=True)
class CitationCheck:
    cited: tuple[str, ...]  # in order of first appearance
    valid: tuple[str, ...]
    invalid: tuple[str, ...]  # cited but never retrieved in this run


def check_citations(answer: str, retrieved: set[str]) -> CitationCheck:
    seen: list[str] = []
    for m in CITATION_RE.finditer(answer):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    valid = tuple(c for c in seen if c in retrieved)
    invalid = tuple(c for c in seen if c not in retrieved)
    return CitationCheck(tuple(seen), valid, invalid)


def strip_invalid(answer: str, invalid: tuple[str, ...]) -> str:
    """Remove citations the run never retrieved, so the UI never links to a fabricated
    source. The run records them, so this is visible in telemetry and evals."""
    for c in invalid:
        answer = answer.replace(f"[{c}]", "")
    return answer

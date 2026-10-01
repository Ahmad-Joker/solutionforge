"""Structure-aware chunking with exact character offsets.

Text is split into *units* (paragraphs; over-long paragraphs into sentences; over-long
sentences hard-wrapped), then units are packed greedily up to ``size`` characters with
``overlap`` characters of trailing context carried into the next chunk. Every chunk keeps
``[char_start, char_end)`` into the original document, so a citation can always be traced
back to the exact source span, and the nearest Markdown heading as ``section``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHUNKS_PER_DOCUMENT = 5000
_PARA = re.compile(r"\n\s*\n")
_SENT = re.compile(r"(?<=[.!?])\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class TextChunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    section: str | None


def _units(text: str, size: int) -> list[tuple[int, int]]:
    """(start, end) spans of paragraphs, refined to sentences / hard splits when too long."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in [*_PARA.finditer(text), None]:
        end = m.start() if m else len(text)
        if text[pos:end].strip():
            spans.extend(_refine(text, pos, end, size))
        if m:
            pos = m.end()
    return spans


def _refine(text: str, start: int, end: int, size: int) -> list[tuple[int, int]]:
    if end - start <= size:
        return [(start, end)]
    out: list[tuple[int, int]] = []
    pos = start
    for m in [*_SENT.finditer(text, start, end), None]:
        stop = m.start() if m else end
        if stop > pos:
            while stop - pos > size:  # a single huge sentence: hard wrap
                out.append((pos, pos + size))
                pos += size
            out.append((pos, stop))
        if m:
            pos = m.end()
    return out


def _sections(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(1).strip()[:300]) for m in _HEADING.finditer(text)]


def chunk_text(text: str, *, size: int = 800, overlap: int = 120) -> list[TextChunk]:
    if size < 100 or not 0 <= overlap < size // 2:
        raise ValueError("require size >= 100 and 0 <= overlap < size/2")
    headings = _sections(text)
    units = _units(text, size)

    def section_of(pos: int) -> int:
        """Index of the heading governing ``pos`` (-1 before the first heading)."""
        idx = -1
        for n, (hpos, _) in enumerate(headings):
            if hpos <= pos:
                idx = n
        return idx

    unit_section = [section_of(u[0]) for u in units]
    chunks: list[TextChunk] = []
    i = 0
    while i < len(units):
        start = units[i][0]
        end = units[i][1]
        j = i + 1
        # Pack units up to `size`, never across a heading: a chunk belongs to one section.
        while j < len(units) and units[j][1] - start <= size and unit_section[j] == unit_section[i]:
            end = units[j][1]
            j += 1
        sec = unit_section[i]
        section = headings[sec][1] if sec >= 0 else None
        chunks.append(TextChunk(len(chunks), text[start:end].strip(), start, end, section))
        if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
            raise ValueError(f"document produces more than {MAX_CHUNKS_PER_DOCUMENT} chunks")
        if j >= len(units):
            break
        # Overlap: start the next chunk at the last unit(s) fitting in `overlap` chars.
        k = j
        while (
            k - 1 > i
            and end - units[k - 1][0] <= overlap
            and unit_section[k - 1] == unit_section[j]  # overlap stays within a section
        ):
            k -= 1
        i = k if k > i else j
    return chunks

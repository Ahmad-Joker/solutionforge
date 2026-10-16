"""``fixture:policy-v1`` — a deterministic, rule-based stand-in for the model.

It speaks the same tool-calling protocol as the real provider (``tool_use`` out,
``tool_result`` in) so the loop, validation, approvals and telemetry can be exercised with
no API key. It is **not a language model** and its results are never reported as model
results: eval reports label it FIXTURE. Its rules are simple on purpose:

- a ticket-looking token in the question → ``get_ticket_status`` with that token verbatim
  (malformed IDs included, so argument validation is exercised);
- "open/create/file/raise … ticket" → ``create_ticket_draft``;
- anything else → ``search_documents``, then answer with the first sentence of the
  best-overlapping passage and its source ID, or abstain when overlap is low.

It never follows instructions found in tool results (trivially: it has no instruction
following), so injection cases passing under the fixture prove nothing about a model.
"""

from __future__ import annotations

import json
import re
from typing import Any

from solutionforge.llm.types import ConverseRequest, ConverseResponse, ToolCall, Usage

FIXTURE_MODEL = "policy-v1"

_TICKET_TOKEN = re.compile(
    r"\b(T-[A-Za-z0-9]{1,12})\b|(?:ticket\s*(?:#|no\.?|number)?\s*)(#?\d{3,})", re.I
)
_CREATE = re.compile(r"\b(open|create|file|raise|log|submit|start)\b[^.?!]*\bticket\b", re.I)
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "be",
        "do",
        "does",
        "can",
        "could",
        "would",
        "should",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "why",
        "your",
        "you",
        "our",
        "my",
        "me",
        "i",
        "it",
        "its",
        "this",
        "that",
        "there",
        "their",
        "have",
        "has",
        "any",
        "about",
        "from",
        "at",
        "by",
        "as",
        "if",
        "not",
        "no",
        "yes",
        "please",
        "tell",
        "know",
        "much",
        "many",
        "long",
    ]
)
ABSTAIN = (
    "I couldn't find this in the help-centre documents, so I won't guess. "
    "I can prepare a ticket so a staff member can follow up."
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


def _first_sentence(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    m = re.search(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[: limit - 1] + "…"


class FixturePolicyProvider:
    name = "fixture"

    def __init__(self, min_overlap: float = 0.5) -> None:
        self.min_overlap = min_overlap

    async def converse(self, request: ConverseRequest) -> ConverseResponse:
        question = next(
            m["content"]
            for m in request.messages
            if m["role"] == "user" and isinstance(m["content"], str)
        )
        last = request.messages[-1]
        if last["role"] == "user" and isinstance(last["content"], str):
            if not request.tools:  # fixed-workflow mode: context is already in the prompt
                return self._final(self._answer_from_text(question, last["content"]))
            return self._first_turn(question)
        results = [b for b in last["content"] if b.get("type") == "tool_result"]
        calls = {
            b["id"]: b
            for m in request.messages
            if m["role"] == "assistant"
            for b in m["content"]
            if b.get("type") == "tool_use"
        }
        return self._final(self._answer(question, results, calls))

    # ------------------------------------------------------------------ turns

    def _first_turn(self, question: str) -> ConverseResponse:
        token = _TICKET_TOKEN.search(question)
        if token:
            ticket = (token.group(1) or token.group(2)).upper()
            return self._call("get_ticket_status", {"ticket_id": ticket})
        if _CREATE.search(question):
            subject = " ".join(question.split())[:120]
            return self._call(
                "create_ticket_draft", {"subject": subject, "description": question.strip()}
            )
        return self._call("search_documents", {"query": " ".join(question.split())[:300]})

    def _answer(
        self, question: str, results: list[dict[str, Any]], calls: dict[str, dict[str, Any]]
    ) -> str:
        for r in results:
            name = calls.get(r["tool_use_id"], {}).get("name")
            content = r.get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            if r.get("is_error"):
                if name == "get_ticket_status":
                    return (
                        "I couldn't look that ticket up: "
                        + text
                        + " Please check the ID (it looks like T-1A2B3C4D)."
                    )
                return "I couldn't complete that: " + text
            data = json.loads(text)
            if name == "get_ticket_status":
                return (
                    f"Ticket {data['ticket_id']} is {data['status']} (priority {data['priority']})."
                )
            if name == "create_ticket_draft":
                return (
                    f"I've prepared ticket draft {data['draft_id']}. A staff member must "
                    "approve it before a ticket is opened."
                )
            if name == "search_documents":
                return self._best_passage(question, data.get("passages", []))
        return ABSTAIN

    def _best_passage(self, question: str, passages: list[dict[str, Any]]) -> str:
        q = _words(question)
        best: tuple[float, dict[str, Any]] | None = None
        for p in passages:
            overlap = len(q & _words(p.get("title", "") + " " + p.get("text", ""))) / max(len(q), 1)
            if best is None or overlap > best[0]:
                best = (overlap, p)
        if best is None or best[0] < self.min_overlap:
            return ABSTAIN
        p = best[1]
        return f"{_first_sentence(p['text'])} [{p['source_id']}]"

    def _answer_from_text(self, question: str, context: str) -> str:
        m = re.search(r"<tool_output name=\"search_documents\">(.*?)</tool_output>", context, re.S)
        if m:
            return self._best_passage(question, json.loads(m.group(1)).get("passages", []))
        return ABSTAIN

    # ------------------------------------------------------------------ responses

    @staticmethod
    def _call(name: str, args: dict[str, Any]) -> ConverseResponse:
        call = ToolCall(id=f"fx_{name}", name=name, input=args)
        return ConverseResponse(
            text="",
            tool_calls=(call,),
            content=({"type": "tool_use", "id": call.id, "name": name, "input": args},),
            stop_reason="tool_use",
            usage=Usage(),
        )

    @staticmethod
    def _final(text: str) -> ConverseResponse:
        return ConverseResponse(
            text=text,
            tool_calls=(),
            content=({"type": "text", "text": text},),
            stop_reason="end_turn",
            usage=Usage(),
        )

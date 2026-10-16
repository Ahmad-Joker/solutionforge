"""The eval datasets are well-formed and the deterministic scorer does what it claims."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from solutionforge.assistant.citations import check_citations, strip_invalid
from solutionforge.assistant.corpus import load_corpus
from solutionforge.assistant.evals import EvalCase, Observation, load_cases, percentile, score
from solutionforge.assistant.tools import ToolInputError, draft_hash, summarize_args, validate_args

EVALS = Path(__file__).resolve().parents[4] / "evals" / "assistant"


def test_datasets_have_the_agreed_shape() -> None:
    dev = load_cases(EVALS / "dev.jsonl")
    hold = load_cases(EVALS / "holdout.jsonl")
    assert Counter(c.category for c in dev) == {
        "answerable": 12,
        "tool": 8,
        "unanswerable": 5,
        "injection": 5,
    }
    assert len(hold) == 10
    assert not {c.id for c in dev} & {c.id for c in hold}
    assert not {c.input for c in dev} & {c.input for c in hold}
    known = {d.doc_id for d in load_corpus(include_injection=True)}
    for c in dev + hold:
        assert set(c.relevant_sources) <= known, c.id


def case(**kw: Any) -> EvalCase:
    base: dict[str, Any] = {
        "id": "t",
        "category": "answerable",
        "input": "q",
        "expected_outcome": "answer",
        "relevant_sources": ["HP-POL-02"],
        "expected_actions": ["search_documents"],
        "forbidden_actions": ["create_ticket_draft"],
        "expected_facts": [["30 days"]],
        "forbidden_patterns": [],
        "scoring": "",
    }
    return EvalCase.from_dict({**base, **kw})


def obs(**kw: Any) -> Observation:
    base: dict[str, Any] = {
        "run_status": "completed",
        "answer": "You have 30 days. [HP-POL-02#1]",
        "citations": ["HP-POL-02#1"],
        "invalid_citations": [],
        "tool_events": [
            {
                "name": "search_documents",
                "outcome": "4 passages",
                "args": {"query": "refund"},
                "results": ["HP-POL-02#1"],
            }
        ],
        "draft_statuses": [],
        "tickets_created": 0,
    }
    return Observation(**{**base, **kw})


def test_answerable_pass() -> None:
    s = score(case(), obs())
    assert s.verdict == "PASS", s.reasons
    assert s.dimensions["approval_enforcement"] is None  # not applicable


def test_fabricated_citation_fails_grounding() -> None:
    s = score(case(), obs(invalid_citations=["HP-POL-99#0"]))
    assert s.verdict == "FAIL" and s.dimensions["grounded_citations"] is False


def test_wrong_source_fails_grounding_and_retrieval() -> None:
    s = score(
        case(),
        obs(
            citations=["HP-SVC-01#0"],
            tool_events=[
                {
                    "name": "search_documents",
                    "outcome": "1 passages",
                    "args": {},
                    "results": ["HP-SVC-01#0"],
                }
            ],
        ),
    )
    assert s.dimensions["retrieval"] is False and s.dimensions["grounded_citations"] is False


def test_unapproved_ticket_fails_approval_enforcement() -> None:
    c = case(
        category="tool",
        expected_outcome="draft_pending",
        relevant_sources=[],
        expected_actions=["create_ticket_draft"],
        forbidden_actions=[],
        expected_facts=[["approv"]],
        approval_case=True,
    )
    ok = obs(
        answer="Draft prepared; it needs approval.",
        citations=[],
        tool_events=[
            {
                "name": "create_ticket_draft",
                "outcome": "draft_created",
                "args": {},
                "draft_id": "d1",
            }
        ],
        draft_statuses=["pending"],
    )
    assert score(c, ok).verdict == "PASS"
    leaked = obs(**{**ok.__dict__, "tickets_created": 1})
    assert score(c, leaked).dimensions["approval_enforcement"] is False


def test_abstention_needs_next_step_and_no_citations() -> None:
    c = case(
        category="unanswerable", expected_outcome="abstain", relevant_sources=[], expected_facts=[]
    )
    good = obs(
        run_status="insufficient_evidence",
        citations=[],
        answer="I couldn't find this in the documents. I can prepare a ticket for staff.",
    )
    assert score(c, good).verdict == "PASS"
    no_next = obs(
        run_status="insufficient_evidence",
        citations=[],
        answer="I couldn't find this in the documents.",
    )
    assert score(c, no_next).dimensions["abstention"] is False


def test_malformed_input_skips_argument_validity() -> None:
    c = case(
        category="tool",
        expected_outcome="ask_to_check_id",
        relevant_sources=[],
        expected_actions=[],
        expected_facts=[["check"]],
        malformed_input=True,
    )
    o = obs(
        answer="Please check the ID format.",
        citations=[],
        tool_events=[
            {
                "name": "get_ticket_status",
                "outcome": "invalid_arguments",
                "args": {"ticket_id": "T-1"},
            }
        ],
    )
    s = score(c, o)
    assert s.dimensions["argument_validity"] is None and s.verdict == "PASS"


def test_percentile_nearest_rank() -> None:
    assert percentile([], 0.95) is None
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
    assert percentile([5], 0.5) == 5


def test_citation_check_and_strip() -> None:
    chk = check_citations("A [HP-POL-02#1] and [HP-X-9#0] again [HP-POL-02#1]", {"HP-POL-02#1"})
    assert chk.cited == ("HP-POL-02#1", "HP-X-9#0") and chk.invalid == ("HP-X-9#0",)
    assert strip_invalid("x [HP-X-9#0]", chk.invalid) == "x "


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("approve_draft", {}, "Unknown tool"),
        ("get_ticket_status", {"ticket_id": "t-1a2b3c4d"}, "must look like T-1A2B3C4D"),
        ("get_ticket_status", {}, "ticket_id: Field required"),
        ("search_documents", {"query": "x"}, "query"),
        ("search_documents", "refunds", "must be a JSON object"),
        ("create_ticket_draft", {"subject": "a\nb", "description": "long enough text"}, "subject"),
    ],
)
def test_invalid_tool_input_messages(name: str, raw: Any, message: str) -> None:
    with pytest.raises(ToolInputError, match=message):
        validate_args(name, raw)


def test_safe_argument_summaries() -> None:
    s = summarize_args("search_documents", {"query": "my key is sk-ant-abcdefghijklmnop"})
    assert "sk-ant" not in s["query"] and "[redacted]" in s["query"]
    d = summarize_args("create_ticket_draft", {"subject": "Leak", "description": "x" * 900})
    assert d == {"subject": "Leak", "description_chars": 900}


def test_draft_hash_binds_content() -> None:
    assert draft_hash("a", "b") == draft_hash("a", "b")
    assert draft_hash("a", "b") != draft_hash("a", "b ")


def test_cost_is_unknown_not_zero_when_nothing_ran() -> None:
    from solutionforge.assistant.evals import summarize

    rows = [
        {
            "verdict": "NOT RUN",
            "category": "answerable",
            "dimensions": {},
            "latency_ms": None,
            "cost_micro_usd": None,
            "run_status": None,
        }
    ]
    s = summarize(rows)
    assert s["cost"]["total_micro_usd"] is None and s["not_run"] == 1 and s["pass_rate"] is None

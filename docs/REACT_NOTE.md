# ReAct: paper note and an application-specific experiment

**Paper:** Yao, Zhao, Yu, Du, Shafran, Narasimhan, Cao. *ReAct: Synergizing Reasoning and
Acting in Language Models.* ICLR 2023. [arXiv:2210.03629](https://arxiv.org/abs/2210.03629).
Read on 2026-10-16 (arXiv abstract page and the ar5iv HTML rendering).

## Core idea

A language model solves a task by **interleaving free-text reasoning steps ("thoughts")
with actions** in an environment, and reading back each observation:

```
thought → action → observation → thought → …
```

Reasoning on its own (chain-of-thought) can't check facts, so errors compound. Acting on
its own can't plan or recover from surprises. ReAct puts both in one trajectory, so the
reasoning decides the next action and the observations correct the reasoning. In the
paper this is done by **few-shot prompting**, with a handful of human-written example
trajectories per task, plus some fine-tuning experiments on smaller models.

## Evidence

The main experiments prompt PaLM-540B, with GPT-3 results in an appendix. The action
spaces are small and task-specific: for the QA tasks, a Wikipedia API with `search`,
`lookup` and `finish`.

| Task | Metric | Best baseline (as reported) | ReAct | Best combination |
|---|---|---|---|---|
| HotpotQA (multi-hop QA) | exact match | CoT-SC 33.4 | 27.4 | ReAct→CoT-SC 35.1 |
| FEVER (fact verification) | accuracy | CoT-SC 60.4 | 60.9 | CoT-SC→ReAct 64.6 |
| ALFWorld (text household tasks) | success % | Act best-of-6: 45; BUTLER (imitation): 37 | 71 (best of 6), 57 (average) | — |
| WebShop (online shopping) | success % | IL+RL 28.7; Act 30.1 | 40.0 | — (human expert: 59.6) |

- On the **knowledge tasks**, ReAct on its own did *not* beat chain-of-thought with
  self-consistency on HotpotQA. The best results came from **combining** them: switch to
  the other method when the first one fails or is unsure.
- **Error analysis** (200 HotpotQA cases): ReAct's successes were less often hallucinated
  than chain-of-thought's (6% vs 14% false positives). Hallucination caused 0% of ReAct's
  failures and 56% of chain-of-thought's. But ReAct failed more on reasoning errors,
  including **repetitive loops** (47% vs 16%), and 23% of its failures came from
  uninformative searches.
- On the **decision-making tasks**, the authors report absolute success-rate gains of 34
  (ALFWorld) and 10 (WebShop) points over imitation and reinforcement-learning baselines,
  using one or two in-context examples.

## Limitations (from the paper, and for us)

- **Prompting:** a few demonstrations. Complex tasks and large action spaces need more
  examples than fit in context. The authors point to fine-tuning and multi-task training,
  which need more annotated trajectories.
- **Fragile retrieval:** a bad search result derails the trajectory. The paper's error
  analysis puts uninformative search at nearly a quarter of failures.
- **Loops:** the model can repeat the same thought and action. A harness needs explicit
  limits, as ours has (turn, tool-call and deadline caps).
- **Older setup:** 2022 models with text-parsed actions. Today's models have **native
  tool calling**, where the provider returns structured `tool_use` blocks. Much of the
  parsing fragility is gone, but tool selection, argument quality and loop control remain
  the open questions ReAct raised.

## Relevance to this assistant

The assistant's default mode is the modern form of "model chooses actions from
observations". The model decides whether to search, look up a ticket or prepare a draft.
It sees each `tool_result` and decides again, inside a bounded loop.

We **don't** ask for or store free-text "thoughts". Private reasoning isn't shown to users,
and the comparison below doesn't depend on it. The experiment is about **who chooses the
actions**, not about printing reasoning traces.

## Experiment (application-specific, inspired by the paper; not a reproduction)

**Question.** On this assistant's own tasks, does letting the model select tools beat a
fixed, code-chosen tool sequence?

| Arm | `--tool-mode` | Who decides the actions | Model calls |
|---|---|---|---|
| Fixed workflow | `fixed` | Code. A ticket ID in the question → `get_ticket_status`. An "open/file/raise a ticket" request → `create_ticket_draft`. Then always `search_documents(question)`. The model gets the results and writes the answer with no tools | 1 |
| Model-selected | `model` (default) | The model, through native tool use, observing results, within the limits | 1–6 |

**Setup.**

- Same 30 dev cases, same model, prompt version, corpus, retrieval settings and scorer.
- Held-out set excluded.
- Both arms through `scripts/assistant_eval.py`, with the same request and spend caps.

**What we would compare:**

- pass rate;
- `tool_selection` and `argument_validity`. Expected: the fixed arm always searches, even
  for ticket lookups, and can't recover from a malformed ID;
- abstention and injection resistance;
- median and p95 latency, and cost per success. The fixed arm makes one model call, so it
  should be cheaper and faster.

**What it would not show:**

- anything about the paper's benchmarks;
- the effect of reasoning traces (we have none);
- statistical significance. 30 cases can show large differences only.

**Status: BLOCKED.**

- **Live runs:** need an Anthropic API key, which was not available in this environment.
- **Harness:** both arms are implemented and were checked with the rule-based fixture
  policy ([fixed-arm fixture run](../evals/assistant/results/1016-152137-dev-fw/report.md):
  11/30; model-mode fixture: 16/30).
- **Those numbers are not evidence either way.** The fixture is not a model.

Command once a key is approved:

```bash
for mode in fixed model; do
  python scripts/assistant_eval.py --dataset dev --models anthropic:claude-haiku-5-5 \
    --tool-mode $mode --max-requests 200 --max-spend-usd 1.00 --label "react-$mode"
done
```

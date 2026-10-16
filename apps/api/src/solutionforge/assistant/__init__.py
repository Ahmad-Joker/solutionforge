"""Business assistant: a bounded agent loop over provider-native tool calling.

- :mod:`.tools` — the three allowlisted tools, their JSON schemas and argument validation.
- :mod:`.service` — the loop (limits, retries, budgets, cancellation, telemetry) and the
  human approval of ticket drafts.
- :mod:`.citations` — source-ID parsing and validation against what the run retrieved.
- :mod:`.fixture` — a deterministic, rule-based stand-in for the model (offline tests and
  harness checks only; never reported as model results).
"""

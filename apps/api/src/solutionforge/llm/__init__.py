"""LLM access layer.

Everything in SolutionForge that talks to a model goes through :class:`LLMService`
(``llm/service.py``). Nothing outside ``llm/providers/`` imports a vendor SDK; a test
enforces it. The service owns retries, fallback, circuit breaking, structured-output
validation/repair, budget checks and usage metering, so every call gets them uniformly.
"""

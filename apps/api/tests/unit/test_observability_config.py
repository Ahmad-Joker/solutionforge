"""Dashboards and alert rules may only reference metrics the application registers."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from solutionforge.observability.metrics import REGISTRY

ROOT = Path(__file__).resolve().parents[4]
_SUFFIXES = ("_total", "_bucket", "_count", "_sum", "_created")


def _registered() -> set[str]:
    names = set()
    for metric in REGISTRY.collect():
        names.add(metric.name)
        names.update(s.name for s in metric.samples)
    return names


def _referenced(text: str) -> set[str]:
    return set(re.findall(r"\bsf_[a-z_]+\b", text))


def _dashboard_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "build_dashboard", ROOT / "apps/api/scripts/build_dashboard.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboard_and_alerts_reference_only_real_metrics() -> None:
    registered = _registered()
    text = "\n".join(_dashboard_module().expressions())
    text += (ROOT / "infra/obs/alerts.yml").read_text(encoding="utf-8")
    referenced = _referenced(text)
    assert referenced, "expected PromQL references"
    unknown = {
        m
        for m in referenced
        if m not in registered and not any(m.removesuffix(s) in registered for s in _SUFFIXES)
    }
    assert not unknown, f"unknown metrics referenced: {sorted(unknown)}"


def test_committed_dashboard_is_generated_from_code() -> None:
    mod = _dashboard_module()
    committed = (ROOT / "infra/obs/dashboards/solutionforge.json").read_text(encoding="utf-8")
    assert committed == mod.render(), "run: python apps/api/scripts/build_dashboard.py"

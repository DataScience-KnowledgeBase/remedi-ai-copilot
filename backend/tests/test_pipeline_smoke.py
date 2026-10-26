import runpy
from copy import deepcopy
from pathlib import Path

import pytest

smoke = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "smoke.py"))
validate_evidence = smoke["validate_evidence"]


@pytest.fixture
def evidence():
    incident = {"status": "closed", "provider": "copilot", "model": "test-model"}
    events = [
        {"stage": stage, "payload": {}}
        for stage in ("triage", "adk", "observe", "decision", "execute", "verify", "learn")
    ]
    events.extend([
        {"stage": "reason", "payload": {"provider": "copilot", "model": "test-model"}},
        {"stage": "close", "payload": {"healthy": True, "samples": [{}, {}, {}]}},
    ])
    return incident, events


def test_live_smoke_accepts_complete_evidence(evidence):
    validate_evidence(*evidence)


@pytest.mark.parametrize(
    "mutation",
    (
        "fixture", "unclosed", "missing_adk", "wrong_model",
        "missing_reason", "two_samples", "unhealthy",
    ),
)
def test_live_smoke_rejects_incomplete_or_fixture_evidence(evidence, mutation):
    incident, events = deepcopy(evidence)
    if mutation == "fixture":
        incident["provider"] = "demo"
    elif mutation == "unclosed":
        incident["status"] = "awaiting_approval"
    elif mutation == "missing_adk":
        events = [event for event in events if event["stage"] != "adk"]
    elif mutation == "wrong_model":
        events[-2]["payload"]["model"] = "fixture"
    elif mutation == "missing_reason":
        events = [event for event in events if event["stage"] != "reason"]
    elif mutation == "two_samples":
        events[-1]["payload"]["samples"].pop()
    elif mutation == "unhealthy":
        events[-1]["payload"]["healthy"] = False
    with pytest.raises(RuntimeError):
        validate_evidence(incident, events)

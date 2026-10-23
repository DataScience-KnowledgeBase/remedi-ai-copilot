from copy import deepcopy
from typing import TypedDict

from app.domain import SCENARIOS, evidence


class Health(TypedDict):
    error_rate: float
    queue_age_seconds: float
    cache_usage: float
    cache_valid: bool
    worker_count: int


class SimulatedService:
    """Fault-injectable state; deliberately cannot access real infrastructure."""

    def __init__(self, scenario: str):
        metrics = evidence(scenario)
        self.state: Health = {
            "error_rate": float(metrics["error_rate"]),
            "queue_age_seconds": float(metrics["queue_age_seconds"]),
            "cache_usage": float(metrics["cache_usage"]),
            "cache_valid": scenario == "dynamic_worker",
            "worker_count": 2,
        }
        self.recover = bool(SCENARIOS[scenario]["recover"])
        self.recovery_action = SCENARIOS[scenario]["expected_action"]

    def snapshot(self) -> Health:
        return deepcopy(self.state)

    def apply(self, action: str):
        if action == "clear_cache":
            self.state["cache_usage"] = 0.30
        elif action == "restart_worker":
            self.state["queue_age_seconds"] = 1.0
        elif action == "scale_workers":
            self.state["worker_count"] += 2
            self.state["queue_age_seconds"] = 2.0
        else:
            raise ValueError("Simulator refuses non-allowlisted actions")
        if self.recover and action == self.recovery_action:
            self.state["error_rate"] = 0.001

    def probe(self) -> Health:
        return self.snapshot()

    def restore(self, checkpoint: Health) -> bool:
        self.state = deepcopy(checkpoint)
        return self.probe() == checkpoint


def healthy(samples: list[Health]) -> bool:
    return len(samples) >= 3 and all(
        sample["error_rate"] < 0.01
        and sample["queue_age_seconds"] < 10
        and (sample["cache_usage"] < 0.80 or sample["cache_valid"])
        for sample in samples
    )

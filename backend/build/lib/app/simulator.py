from copy import deepcopy
from typing import TypedDict

from app.domain import SCENARIOS, evidence


class Health(TypedDict):
    error_rate: float
    queue_age_seconds: float
    cache_usage: float


class SimulatedService:
    """Fault-injectable state; deliberately cannot access real infrastructure."""

    def __init__(self, scenario: str):
        metrics = evidence(scenario)
        self.state: Health = {
            "error_rate": float(metrics["error_rate"]),
            "queue_age_seconds": float(metrics["queue_age_seconds"]),
            "cache_usage": float(metrics["cache_usage"]),
        }
        self.recover = bool(SCENARIOS[scenario]["recover"])

    def snapshot(self) -> Health:
        return deepcopy(self.state)

    def apply(self, action: str):
        if action == "clear_cache":
            self.state["cache_usage"] = 0.30
        elif action == "restart_worker":
            self.state["queue_age_seconds"] = 1.0
        else:
            raise ValueError("Simulator refuses non-allowlisted actions")
        if self.recover:
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
        and sample["cache_usage"] < 0.80
        for sample in samples
    )

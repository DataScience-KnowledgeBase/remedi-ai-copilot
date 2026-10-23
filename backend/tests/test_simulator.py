import pytest

from app.simulator import SimulatedService, healthy
from app.store import Store


def test_partial_failure_rollback_restores_actual_state():
    service = SimulatedService("verification_failure")
    checkpoint = service.snapshot()
    service.apply("clear_cache")
    assert service.probe()["cache_usage"] != checkpoint["cache_usage"]
    assert not healthy([service.probe() for _ in range(3)])
    assert service.restore(checkpoint)
    assert service.probe() == checkpoint
    checkpoint["cache_usage"] = 0
    assert service.probe()["cache_usage"] != 0


def test_success_requires_repeated_measured_state():
    service = SimulatedService("worker_stall")
    assert not healthy([service.probe() for _ in range(3)])
    service.apply("restart_worker")
    assert healthy([service.probe() for _ in range(3)])
    assert not healthy([service.probe()])
    with pytest.raises(ValueError, match="allowlisted"):
        service.apply("shell")


@pytest.mark.parametrize(
    ("scenario", "correct", "wrong"),
    [
        ("dynamic_cache", "clear_cache", "restart_worker"),
        ("dynamic_worker", "restart_worker", "clear_cache"),
        ("dynamic_capacity", "scale_workers", "restart_worker"),
    ],
)
def test_dynamic_recovery_depends_on_causal_action(scenario, correct, wrong):
    service = SimulatedService(scenario)
    checkpoint = service.snapshot()
    service.apply(wrong)
    assert not healthy([service.probe() for _ in range(3)])
    assert service.restore(checkpoint)
    service.apply(correct)
    assert healthy([service.probe() for _ in range(3)])
    if correct == "scale_workers":
        assert service.probe()["worker_count"] == checkpoint["worker_count"] + 2
    assert service.restore(checkpoint)
    assert service.probe() == checkpoint


@pytest.mark.parametrize("interrupted", ["triaging", "ready", "executing", "verifying"])
def test_all_inflight_states_fail_closed_on_restart(tmp_path, interrupted):
    store = Store(str(tmp_path / "recover.db"))
    try:
        job, _ = store.create("cache_pressure", "recovery-key", "demo", "fixture", 25)
        store.transition(job["id"], {"queued"}, interrupted, "test", "Interrupted")
        store.recover()
        assert store.get(job["id"])["status"] == "failed"
        assert store.next_job() is None
    finally:
        store.close()

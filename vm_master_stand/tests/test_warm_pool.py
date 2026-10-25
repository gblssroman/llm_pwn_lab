import time

from lab_service.models import ExecResult
from lab_service.warm_pool import WarmPool


class PoolRunner:
    def __init__(self):
        self.calls = []

    def warm(self, scenario, *, reuse_running=False):
        self.calls.append(("warm", scenario.id, reuse_running))
        return ExecResult(exit_code=0)

    def up(self, scenario, *, restore=True):
        self.calls.append(("up", scenario.id, restore))
        return ExecResult(exit_code=0)

    def down(self, scenario):
        self.calls.append(("down", scenario.id))
        return ExecResult(exit_code=0)


def wait_state(pool, scenario_id, expected):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if pool.get(scenario_id)["state"] == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"pool did not reach {expected}: {pool.get(scenario_id)}")


def test_manual_warm_and_shutdown_are_persistent(settings, attack):
    runner = PoolRunner()
    pool = WarmPool(settings.root, runner, [attack])
    try:
        assert pool.get(attack.id)["state"] == "cold"
        assert pool.request_warm(attack.id)["desired"] == "warm"
        wait_state(pool, attack.id, "ready")
        assert runner.calls == [("warm", attack.id, False)]

        assert pool.request_shutdown(attack.id)["desired"] == "cold"
        wait_state(pool, attack.id, "cold")
        assert runner.calls[-1] == ("down", attack.id)
    finally:
        pool.close()

    restored = WarmPool(settings.root, PoolRunner(), [attack])
    try:
        assert restored.get(attack.id)["desired"] == "cold"
        assert restored.get(attack.id)["state"] == "cold"
    finally:
        restored.close()


def test_ready_vm_is_leased_without_restore_then_rewarmed(settings, attack):
    runner = PoolRunner()
    pool = WarmPool(settings.root, runner, [attack])
    try:
        pool.request_warm(attack.id)
        wait_state(pool, attack.id, "ready")
        runner.calls.clear()

        pool.prepare_for_run(attack)
        assert pool.get(attack.id)["state"] == "in_use"
        assert runner.calls == [("up", attack.id, False)]

        pool.release_after_run(attack)
        wait_state(pool, attack.id, "ready")
        assert runner.calls[1:] == [
            ("down", attack.id),
            ("warm", attack.id, False),
        ]
    finally:
        pool.close()


def test_cold_run_enables_lazy_warm_and_shutdown_can_defer(settings, attack):
    runner = PoolRunner()
    pool = WarmPool(settings.root, runner, [attack])
    try:
        pool.prepare_for_run(attack)
        assert runner.calls == [("up", attack.id, True)]
        assert pool.get(attack.id)["desired"] == "warm"

        state = pool.request_shutdown(attack.id)
        assert state["state"] == "in_use"
        assert state["desired"] == "cold"
        pool.release_after_run(attack)
        wait_state(pool, attack.id, "cold")
        assert runner.calls == [
            ("up", attack.id, True),
            ("down", attack.id),
        ]
    finally:
        pool.close()

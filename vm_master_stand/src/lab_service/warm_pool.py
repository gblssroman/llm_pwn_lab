"""Persistent desired state and serialized lifecycle for warm scenario VMs."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


class WarmPool:
    """Keeps selected per-scenario clones resumed without weakening reset isolation.

    The canonical memory snapshot is never changed here. A warm VM is a clean clone
    resumed from that snapshot. A benchmark leases it, and release always restores
    the snapshot before an optional background warm-up.
    """

    STATE_VERSION = 1
    MEMORY_MB = 4096

    def __init__(self, root: Path, runner, scenarios):
        self.root = root.resolve()
        self.runner = runner
        self.scenarios = {scenario.id: scenario for scenario in scenarios}
        self.path = self.root / ".runtime" / "vm-pool.json"
        self._state_lock = threading.RLock()
        # Lifecycle is intentionally serialized. The benchmark itself supports one
        # active scenario and VirtualBox restore is the fragile part of this stack.
        self._operation_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vm-warm")
        self._pending = set()
        self._closed = False
        self._items = self._load()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _default_item(self):
        return {
            "desired": "cold",
            "state": "cold",
            "error": None,
            "updated_at": self._now(),
        }

    def _load(self):
        items = {scenario_id: self._default_item() for scenario_id in self.scenarios}
        try:
            payload = json.loads(self.path.read_text())
            if payload.get("version") != self.STATE_VERSION:
                return items
            stored = payload.get("scenarios")
            if not isinstance(stored, dict):
                return items
            for scenario_id, item in stored.items():
                if scenario_id not in items or not isinstance(item, dict):
                    continue
                desired = item.get("desired")
                state = item.get("state")
                if desired not in {"warm", "cold"}:
                    continue
                if state not in {
                    "cold",
                    "queued",
                    "warming",
                    "ready",
                    "in_use",
                    "resetting",
                    "dirty",
                    "error",
                }:
                    state = "cold"
                items[scenario_id] = {
                    "desired": desired,
                    "state": state,
                    "error": str(item["error"])[:2_000]
                    if item.get("error")
                    else None,
                    "updated_at": item.get("updated_at") or self._now(),
                }
        except (OSError, ValueError, TypeError):
            pass
        return items

    def _persist_locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"version": self.STATE_VERSION, "scenarios": self._items},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def _update(self, scenario_id, **changes):
        with self._state_lock:
            item = self._items[scenario_id]
            item.update(changes, updated_at=self._now())
            self._persist_locked()

    def _desired(self, scenario_id):
        with self._state_lock:
            return self._items[scenario_id]["desired"]

    def _state(self, scenario_id):
        with self._state_lock:
            return self._items[scenario_id]["state"]

    def _check_result(self, result, action):
        if result.exit_code or result.timed_out:
            message = result.stderr.strip() or result.stdout.strip() or f"VM {action} failed"
            raise RuntimeError(message)
        return result

    def start(self):
        """Reconcile only VMs which were explicitly desired warm before restart."""
        for scenario_id in self.scenarios:
            if self._desired(scenario_id) == "warm" and self._state(scenario_id) != "in_use":
                # A persisted ready state is verified by SSH before it is reused.
                self._enqueue(scenario_id, force=self._state(scenario_id) == "ready")

    def close(self):
        with self._state_lock:
            self._closed = True
        self._executor.shutdown(wait=True)

    def list(self):
        with self._state_lock:
            return [self._view_locked(scenario_id) for scenario_id in self.scenarios]

    def get(self, scenario_id):
        with self._state_lock:
            return self._view_locked(scenario_id)

    def _view_locked(self, scenario_id):
        scenario = self.scenarios[scenario_id]
        item = self._items[scenario_id]
        return {
            "scenario_id": scenario.id,
            "title": scenario.title,
            "mode": scenario.mode,
            "desired": item["desired"],
            "state": item["state"],
            "error": item["error"],
            "updated_at": item["updated_at"],
            "memory_mb": self.MEMORY_MB,
        }

    def request_warm(self, scenario_id):
        changes = {"desired": "warm", "error": None}
        if self._state(scenario_id) == "error":
            changes["state"] = "queued"
        self._update(scenario_id, **changes)
        if self._state(scenario_id) != "in_use":
            self._enqueue(scenario_id)
        return self.get(scenario_id)

    def request_shutdown(self, scenario_id):
        changes = {"desired": "cold", "error": None}
        if self._state(scenario_id) == "error":
            changes["state"] = "queued"
        self._update(scenario_id, **changes)
        if self._state(scenario_id) != "in_use":
            self._enqueue(scenario_id)
        return self.get(scenario_id)

    def request_warm_all(self):
        for scenario_id in self.scenarios:
            changes = {"desired": "warm", "error": None}
            if self._state(scenario_id) == "error":
                changes["state"] = "queued"
            self._update(scenario_id, **changes)
            if self._state(scenario_id) != "in_use":
                self._enqueue(scenario_id)
        return self.list()

    def request_shutdown_all(self):
        for scenario_id in self.scenarios:
            changes = {"desired": "cold", "error": None}
            if self._state(scenario_id) == "error":
                changes["state"] = "queued"
            self._update(scenario_id, **changes)
            if self._state(scenario_id) != "in_use":
                self._enqueue(scenario_id)
        return self.list()

    def _needs_reconcile_locked(self, scenario_id):
        item = self._items[scenario_id]
        if item["state"] in {"in_use", "error"}:
            return False
        if item["desired"] == "warm":
            return item["state"] != "ready"
        return item["state"] != "cold"

    def _enqueue(self, scenario_id, *, force=False):
        with self._state_lock:
            if self._closed or scenario_id in self._pending:
                return
            if not force and not self._needs_reconcile_locked(scenario_id):
                return
            self._pending.add(scenario_id)
            state = self._items[scenario_id]["state"]
            if state not in {"warming", "resetting"} and not (
                force and state == "ready"
            ):
                self._items[scenario_id].update(
                    state="queued", error=None, updated_at=self._now()
                )
                self._persist_locked()
        future = self._executor.submit(self._reconcile, scenario_id, force)
        future.add_done_callback(lambda _future: self._reconcile_done(scenario_id))

    def _reconcile_done(self, scenario_id):
        again = False
        with self._state_lock:
            self._pending.discard(scenario_id)
            again = not self._closed and self._needs_reconcile_locked(scenario_id)
        if again:
            self._enqueue(scenario_id)

    def _reconcile(self, scenario_id, force=False):
        scenario = self.scenarios[scenario_id]
        with self._operation_lock:
            with self._state_lock:
                if self._items[scenario_id]["state"] == "in_use":
                    return
                if not force and not self._needs_reconcile_locked(scenario_id):
                    return
            desired = self._desired(scenario_id)
            previous_state = self._state(scenario_id)
            action = "warm" if desired == "warm" else "shutdown"
            self._update(
                scenario_id,
                state="warming" if desired == "warm" else "resetting",
                error=None,
            )
            try:
                if desired == "warm":
                    result = self.runner.warm(
                        scenario, reuse_running=previous_state == "ready"
                    )
                    self._check_result(result, action)
                    self._update(scenario_id, state="ready", error=None)
                else:
                    result = self.runner.down(scenario)
                    self._check_result(result, action)
                    self._update(scenario_id, state="cold", error=None)
            except Exception as exc:
                self._update(scenario_id, state="error", error=str(exc)[:2_000])

    def prepare_for_run(self, scenario):
        """Lease a clean VM; a ready warm VM skips the expensive restore."""
        self._update(scenario.id, desired="warm", error=None)
        with self._operation_lock:
            warm_ready = self._state(scenario.id) == "ready"
            self._update(scenario.id, state="in_use", error=None)
            try:
                result = self.runner.up(scenario, restore=not warm_ready)
                if (result.exit_code or result.timed_out) and warm_ready:
                    # A persisted ready marker can become stale after host changes.
                    result = self.runner.up(scenario, restore=True)
                return self._check_result(result, "start")
            except Exception as exc:
                self._update(scenario.id, state="error", error=str(exc)[:2_000])
                raise

    def release_after_run(self, scenario):
        """Reset synchronously for isolation, then rewarm asynchronously if desired."""
        with self._operation_lock:
            self._update(scenario.id, state="resetting", error=None)
            try:
                result = self.runner.down(scenario)
                self._check_result(result, "reset")
                self._update(scenario.id, state="cold", error=None)
            except Exception as exc:
                self._update(scenario.id, state="error", error=str(exc)[:2_000])
                raise
        if self._desired(scenario.id) == "warm":
            self._enqueue(scenario.id)
        return result

    def shutdown_now(self, scenario):
        """Explicit synchronous shutdown keeps the VM cold after its clean reset."""
        self._update(scenario.id, desired="cold", error=None)
        return self.release_after_run(scenario)

    def mark_dirty(self, scenario):
        """Prevent reuse without reset after manual commands in an idle warm VM."""
        if self._state(scenario.id) == "ready":
            self._update(scenario.id, state="dirty")

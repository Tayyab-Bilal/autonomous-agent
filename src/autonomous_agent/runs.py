"""Run lifecycle state machine. Illegal moves raise; racing moves lose with ConflictError."""
from datetime import datetime

from .events import EventSink, emit_safely, make_event
from .store import Store

TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running"},
    "running": {"awaiting_approval", "completed", "failed", "parked"},
    "awaiting_approval": {"running"},
    "parked": {"running"},
    "completed": set(),
    "failed": set(),
}
RESUMABLE = ("awaiting_approval", "parked")


class IllegalTransition(Exception):
    pass


class ConflictError(Exception):
    """Someone else already moved this run (HTTP 409 in an API)."""


class PlanRetryLimit(Exception):
    """The plan's runs already failed too many times; more runs would only repeat the failure."""


class RunManager:
    def __init__(self, store: Store, sink: EventSink, max_plan_failures: int = 3) -> None:
        self.store, self.sink, self.max_plan_failures = store, sink, max_plan_failures

    def plan_exhausted(self, plan_id: str) -> bool:
        return self.store.failed_runs(plan_id) >= self.max_plan_failures

    def create(self, plan_id: str, tenant: str, context: dict, trigger: str = "manual") -> str:
        """`trigger` is 'manual' or 'event'; only event runs count toward the event-run cap."""
        if self.plan_exhausted(plan_id):
            raise PlanRetryLimit(
                f"plan {plan_id} has failed {self.max_plan_failures} times; "
                "fix the goal or create a new plan instead of running it again")
        run_id = self.store.create_run(plan_id, tenant, context, trigger)
        self._emit(run_id, "pending", 0, "")
        return run_id

    def transition(self, run_id: str, to: str, reason: str = "", extra: dict | None = None, *,
                   token: int | None = None, stale_before: datetime | None = None) -> None:
        """`token` fences the mover to the claim it holds; `stale_before` makes the move conditional
        on the heartbeat still being old (the reaper), so a run that just beat is left alone."""
        current = self.store.get_run(run_id)["status"]
        if token is not None and not self.store.holds(run_id, token):
            raise ConflictError(f"{run_id} is no longer owned by this worker")  # before legality
        if to not in TRANSITIONS[current]:
            raise IllegalTransition(f"{current} -> {to}")
        # Conditional on the status we just read: a concurrent mover makes this return None.
        seq = self.store.update_status(run_id, (current,), to, reason, seq=token, stale_before=stale_before)
        if seq is None:
            raise ConflictError(f"{run_id} is no longer {current} (or not yours to move)")
        self._emit(run_id, to, seq, reason, extra)

    def start(self, run_id: str) -> int:
        """pending -> running. Returns the fencing token for this worker."""
        seq = self.store.update_status(run_id, ("pending",), "running", "started")
        if seq is None:
            raise ConflictError(f"{run_id} is not pending (already started)")
        self._emit(run_id, "running", seq, "started")
        return seq

    def claim_for_resume(self, run_id: str, allowed: tuple[str, ...] = RESUMABLE) -> int:
        """Atomic claim; the loser gets ConflictError. Returns the fencing token."""
        self.store.get_run(run_id)  # unknown id is a KeyError, not a conflict
        seq = self.store.update_status(run_id, allowed, "running", "resumed")
        if seq is None:
            raise ConflictError(f"{run_id} is not resumable (already claimed or finished)")
        self._emit(run_id, "running", seq, "resumed")
        return seq

    def _emit(self, run_id: str, status: str, seq: int, reason: str, extra: dict | None = None) -> None:
        run = self.store.get_run(run_id)
        data = {"run_id": run_id, "plan_id": run["plan_id"], "tenant": run["tenant"],
                "status": status, "reason": reason, **(extra or {})}
        key = f"run.{status}:{run_id}:{seq}"
        emit_safely(self.sink, make_event(f"acme.run.{status}", data, key, self.store.clock()))

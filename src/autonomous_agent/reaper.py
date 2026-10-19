"""Crash recovery: a worker that died mid-run leaves it in 'running' forever unless swept."""
from datetime import datetime, timedelta

from .runs import ConflictError, RunManager


def reap_stranded(runs: RunManager, now: datetime, max_age: timedelta = timedelta(minutes=60)) -> list[str]:
    reaped = []
    cutoff = now - max_age
    for run_id in runs.store.running_older_than(cutoff):
        try:
            # re-checks staleness inside the UPDATE: a heartbeat since the scan wins
            runs.transition(run_id, "parked", "worker lost", stale_before=cutoff)
            reaped.append(run_id)
        except ConflictError:
            pass  # the run moved on between the scan and the update; leave it alone
    return reaped

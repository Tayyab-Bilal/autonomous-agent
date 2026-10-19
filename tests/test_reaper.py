from datetime import timedelta

from autonomous_agent.reaper import reap_stranded


def test_stranded_running_run_is_parked(new_run, runs, store, clock):
    run_id = new_run()
    runs.transition(run_id, "running")
    clock.advance(minutes=61)
    assert reap_stranded(runs, clock()) == [run_id]
    row = store.get_run(run_id)
    assert (row["status"], row["reason"]) == ("parked", "worker lost")
    runs.claim_for_resume(run_id)  # and it can be resumed


def test_fresh_running_run_untouched(new_run, runs, store, clock):
    run_id = new_run()
    runs.transition(run_id, "running")
    clock.advance(minutes=59)
    assert reap_stranded(runs, clock()) == []
    assert store.get_run(run_id)["status"] == "running"


def test_heartbeat_keeps_a_long_run_alive(new_run, runs, store, clock):
    run_id = new_run()
    runs.transition(run_id, "running")
    clock.advance(minutes=50)
    store.tick(run_id, store.get_run(run_id)["seq"])
    clock.advance(minutes=50)
    assert reap_stranded(runs, clock(), max_age=timedelta(minutes=60)) == []


def test_only_running_runs_are_reaped(new_run, runs, clock):
    pending = new_run()
    clock.advance(hours=3)
    assert reap_stranded(runs, clock()) == []
    assert pending


def test_heartbeat_between_scan_and_update_is_not_reaped(new_run, runs, store, clock, monkeypatch):
    run_id = new_run()
    runs.transition(run_id, "running")
    clock.advance(minutes=61)
    stale_ids = store.running_older_than(clock() - timedelta(minutes=60))
    assert stale_ids == [run_id]
    store.tick(run_id, store.get_run(run_id)["seq"])  # the worker beats after the scan
    monkeypatch.setattr(store, "running_older_than", lambda cutoff: stale_ids)  # reaper uses its scan
    assert reap_stranded(runs, clock()) == []
    assert store.get_run(run_id)["status"] == "running"

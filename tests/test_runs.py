import logging
import threading

import pytest
from conftest import make_plan

from autonomous_agent.events import ListEventSink
from autonomous_agent.runs import ConflictError, IllegalTransition, PlanRetryLimit, RunManager
from autonomous_agent.store import Store


def test_illegal_transition_raises(new_run, runs):
    run_id = new_run()
    with pytest.raises(IllegalTransition):
        runs.transition(run_id, "completed")  # pending cannot jump to completed
    runs.transition(run_id, "running")
    runs.transition(run_id, "completed")
    with pytest.raises(IllegalTransition):
        runs.transition(run_id, "running")  # terminal


def test_full_happy_path_with_approval(new_run, runs, store):
    run_id = new_run()
    for to in ("running", "awaiting_approval"):
        runs.transition(run_id, to)
    runs.claim_for_resume(run_id)
    runs.transition(run_id, "completed")
    assert store.get_run(run_id)["status"] == "completed"


def test_claim_rejects_run_that_is_not_resumable(new_run, runs):
    run_id = new_run()
    with pytest.raises(ConflictError):
        runs.claim_for_resume(run_id)  # still pending


def test_every_transition_emits_a_cloudevent(new_run, runs, sink):
    run_id = new_run()
    runs.transition(run_id, "running")
    runs.transition(run_id, "parked", "boom")
    types = [e["type"] for e in sink.events]
    assert types == ["acme.run.pending", "acme.run.running", "acme.run.parked"]
    assert sink.events[-1]["data"]["reason"] == "boom"
    assert len({e["idempotencykey"] for e in sink.events}) == 3


def test_event_sink_failure_does_not_block_transition(store, caplog):
    class Broken:
        def emit(self, event):
            raise RuntimeError("broker down")

    runs = RunManager(store, Broken())
    with caplog.at_level(logging.WARNING):
        run_id = runs.create(make_plan(store), "t1", {})
        runs.transition(run_id, "running")
    assert store.get_run(run_id)["status"] == "running"
    assert "event sink failed" in caplog.text


def test_double_resume_one_wins_one_conflicts(tmp_path, clock):
    """Two workers, two connections, one database file: a real race on the conditional UPDATE."""
    path = str(tmp_path / "race.db")
    managers = [RunManager(Store(path, clock), ListEventSink()) for _ in range(2)]
    setup = managers[0]
    for _ in range(25):
        run_id = setup.create(make_plan(setup.store), "t1", {})
        setup.transition(run_id, "running")
        setup.transition(run_id, "awaiting_approval")
        barrier, outcomes = threading.Barrier(2), []

        def worker(m):
            barrier.wait()
            try:
                m.claim_for_resume(run_id)
                outcomes.append("won")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=worker, args=(m,)) for m in managers]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(outcomes) == ["conflict", "won"]


def test_fencing_token_cuts_off_a_replaced_worker(new_run, runs, store):
    from autonomous_agent.store import Fenced
    run_id = new_run()
    old = runs.start(run_id)
    runs.transition(run_id, "parked", "worker lost")  # reaped
    new = runs.claim_for_resume(run_id)  # a second worker takes over
    assert new != old
    with pytest.raises(Fenced):
        store.tick(run_id, old)
    with pytest.raises(Fenced):
        store.record_write(run_id, "k", {}, old)
    with pytest.raises(Fenced):
        store.set_step(run_id, "s1", "complete", "", old)
    with pytest.raises(ConflictError):
        runs.transition(run_id, "completed", token=old)
    store.tick(run_id, new)


def test_plan_with_repeated_failed_runs_is_refused_with_a_readable_cause(store, sink):
    runs = RunManager(store, sink, max_plan_failures=3)
    plan_id = make_plan(store)
    for _ in range(3):
        run_id = runs.create(plan_id, "t1", {})
        runs.start(run_id)
        runs.transition(run_id, "failed", "boom")
    with pytest.raises(PlanRetryLimit, match="failed 3 times"):
        runs.create(plan_id, "t1", {})
    assert runs.create(make_plan(store), "t1", {})  # other plans are unaffected


def test_resumable_failures_do_not_count_toward_the_plan_retry_limit(store, sink):
    runs = RunManager(store, sink, max_plan_failures=1)
    plan_id = make_plan(store)
    run_id = runs.create(plan_id, "t1", {})
    runs.start(run_id)
    runs.transition(run_id, "parked", "blip")
    assert runs.create(plan_id, "t1", {})

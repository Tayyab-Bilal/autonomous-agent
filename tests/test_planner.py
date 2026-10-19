import asyncio
from datetime import timedelta

import pytest

from autonomous_agent.grounding import EntityRef, FakeGrounder
from autonomous_agent.llm import ExtractedPlan, FakeExtractor, StepIntent
from autonomous_agent.planner import Planner
from autonomous_agent.store import LockBusy

GOOD = ExtractedPlan(steps=[StepIntent(key="a", instruction="x", scope_refs=["Apollo"])],
                     event="task overdue")
GROUNDER = FakeGrounder([EntityRef(id="p-1", kind="project", name="Apollo")])


def planner(store, sink, scripted):
    ex = FakeExtractor(scripted)
    return Planner(store, GROUNDER, ex, sink), ex


async def test_retry_within_window_reuses_plan_and_does_not_call_llm_twice(store, sink, clock):
    p, ex = planner(store, sink, [GOOD])
    first = await p.create_plan("Move overdue tasks", "t1", "u1")
    clock.advance(minutes=5)
    second = await p.create_plan("  move OVERDUE   tasks ", "t1", "u1")  # normalised goal
    assert ex.calls == 1
    assert second.reused and second.plan_id == first.plan_id


async def test_reuse_expires_after_window(store, sink, clock):
    p, ex = planner(store, sink, [GOOD, GOOD])
    first = await p.create_plan("goal", "t1", "u1")
    clock.advance(minutes=11)
    second = await p.create_plan("goal", "t1", "u1")
    assert ex.calls == 2 and second.plan_id != first.plan_id


async def test_reuse_is_per_tenant(store, sink):
    p, ex = planner(store, sink, [GOOD, GOOD])
    await p.create_plan("goal", "t1", "u1")
    await p.create_plan("goal", "t2", "u1")
    assert ex.calls == 2


async def test_plan_events_are_cloudevents(store, sink):
    p, _ = planner(store, sink, [GOOD, ExtractedPlan()])
    await p.create_plan("goal one", "t1", "u1")
    await p.create_plan("goal two", "t1", "u1")  # zero steps -> refusal
    ready, failed = sink.events
    assert (ready["type"], failed["type"]) == ("acme.plan.ready", "acme.plan.failed")
    for e in (ready, failed):
        assert {"specversion", "type", "source", "id", "time", "data", "idempotencykey"} <= e.keys()
        assert e["specversion"] == "1.0"
    assert failed["data"]["reason"] == "ZERO_STEPS"
    assert ready["idempotencykey"] == f"plan.ready:{ready['data']['plan_id']}"


async def test_refusal_is_not_persisted_or_reused(store, sink):
    p, ex = planner(store, sink, [ExtractedPlan(), GOOD])
    assert (await p.create_plan("goal", "t1", "u1")).refusal is not None
    assert (await p.create_plan("goal", "t1", "u1")).plan is not None
    assert ex.calls == 2


def test_plan_lock_is_exclusive_and_expires(store, clock):
    with store.plan_lock("t1", "h"), pytest.raises(LockBusy), store.plan_lock("t1", "h"):
        pass
    with store.plan_lock("t1", "h", ttl=timedelta(seconds=5)):
        with pytest.raises(LockBusy):  # still held and not expired: no stealing
            with store.plan_lock("t1", "h"):
                pass
        clock.advance(seconds=6)  # holder "died": only now can the lock be taken over
        with store.plan_lock("t1", "h"):
            pass


def test_expired_holder_does_not_release_the_new_holders_lock(store, clock):
    with store.plan_lock("t1", "h", ttl=timedelta(seconds=5)):  # slow holder A
        clock.advance(seconds=6)
        with store.plan_lock("t1", "h"):  # B takes over after expiry
            pass
        with store.plan_lock("t1", "h"):  # B released: free again
            pass
    # Re-run with A releasing while B still holds:
    a = store.plan_lock("t1", "h2", ttl=timedelta(seconds=5))
    a.__enter__()
    clock.advance(seconds=6)
    b = store.plan_lock("t1", "h2")
    b.__enter__()
    a.__exit__(None, None, None)  # A finishes late
    with pytest.raises(LockBusy):  # B's lock must still be there
        with store.plan_lock("t1", "h2"):
            pass
    b.__exit__(None, None, None)


async def test_slow_extractor_concurrent_requests_make_one_llm_call(store, sink):
    release = asyncio.Event()

    class Slow(FakeExtractor):
        async def extract(self, goal, grounding):
            await release.wait()
            return await super().extract(goal, grounding)

    ex = Slow([GOOD, GOOD])
    p = Planner(store, GROUNDER, ex, sink)
    first = asyncio.create_task(p.create_plan("goal", "t1", "u1"))
    await asyncio.sleep(0)  # first now holds the lock inside the slow extractor
    with pytest.raises(LockBusy):
        await p.create_plan("goal", "t1", "u1")
    release.set()
    done = await first
    retry = await p.create_plan("goal", "t1", "u1")
    assert ex.calls == 1 and retry.reused and retry.plan_id == done.plan_id


async def test_reuse_is_per_user(store, sink):
    p, ex = planner(store, sink, [GOOD, GOOD])
    await p.create_plan("goal", "t1", "u1")
    await p.create_plan("goal", "t1", "u2")
    assert ex.calls == 2


async def test_plan_failed_idempotency_key_is_per_attempt(store, sink):
    p, _ = planner(store, sink, [ExtractedPlan(), ExtractedPlan()])
    await p.create_plan("goal", "t1", "u1")
    await p.create_plan("goal", "t1", "u1")
    assert sink.events[0]["idempotencykey"] != sink.events[1]["idempotencykey"]


def test_lock_ttl_default_outlasts_llm_latency():
    from autonomous_agent.store import LOCK_TTL
    assert LOCK_TTL >= timedelta(seconds=120)


async def test_planner_passes_lock_ttl_through(store, sink, clock):
    seen = []
    real = store.plan_lock
    store.plan_lock = lambda t, h, ttl: (seen.append(ttl), real(t, h, ttl))[1]
    p = Planner(store, GROUNDER, FakeExtractor([GOOD]), sink, lock_ttl=timedelta(minutes=9))
    await p.create_plan("goal", "t1", "u1")
    assert seen == [timedelta(minutes=9)]

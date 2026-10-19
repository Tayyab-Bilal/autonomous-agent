import asyncio

import pytest
from conftest import complete, turn, write

from autonomous_agent.executor import FakeStepAgent
from autonomous_agent.gate import ToolPolicyGate, hold
from autonomous_agent.runs import ConflictError


async def go(ex, run_id, *script):
    agent = FakeStepAgent(list(script))
    return await ex.execute(run_id, agent), agent


async def test_error_response_is_classified_without_an_exception(make_executor, new_run, store, world):
    run_id = new_run()
    answers = iter([{"error": "409 write conflict"}, {"ok": True}])

    async def conflict_once(args):
        r = next(answers)
        if "ok" in r:
            world.writes.append(args)
        return r

    ex = make_executor()
    ex.tools["move_task"].fn = conflict_once
    status, _ = await go(ex, run_id, turn(write("a", "s1"), complete("s1"), complete("s2"), done=True))
    assert status == "completed" and len(world.writes) == 1  # the conflict was retried


async def test_terminal_error_response_fails_the_step(make_executor, new_run, store):
    run_id = new_run()

    async def denied(args):
        return {"error": "permission denied"}

    ex = make_executor()
    ex.tools["move_task"].fn = denied
    status, _ = await go(ex, run_id, turn(write("a", "s1"), done=True))
    assert status == "failed" and store.step_states(run_id)["s1"][0] == "failed"


async def test_partial_bulk_create_is_not_retried_and_not_recorded_as_a_clean_write(make_executor, new_run, store):
    run_id = new_run()
    calls = []

    async def bulk(args):
        calls.append(args)
        return {"created": ["a"], "failed": ["b"], "status": 409}

    ex = make_executor()
    ex.tools["move_task"].fn = bulk
    status, _ = await go(ex, run_id, turn(write("a", "s1")))
    assert status == "failed" and len(calls) == 1
    assert store.writes_used(run_id) == 0


async def test_cancelled_run_is_parked_then_reraises(make_executor, new_run, store, sink):
    run_id = new_run()

    class Hangs:
        async def step(self, view):
            await asyncio.sleep(60)

    task = asyncio.create_task(make_executor().execute(run_id, Hangs()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = store.get_run(run_id)
    assert row["status"] == "parked" and "cancelled" in row["reason"]
    assert sink.events[-1]["type"] == "acme.run.parked"
    status, _ = await go(make_executor(), run_id, turn(complete("s1"), complete("s2"), done=True))
    assert status == "completed"  # and it resumes


class HoldAll:
    def check(self, call):
        return hold("needs a person")


async def test_gate_hold_parks_awaiting_approval(make_executor, new_run, store, world):
    run_id = new_run()
    status, _ = await go(make_executor(gate=HoldAll()), run_id, turn(write("a", "s1")))
    assert status == "awaiting_approval" and world.writes == []
    assert store.step_states(run_id)["s1"][0] == "pending"
    assert store.held_writes(run_id)[0]["call"]["args"] == {"task": "a"}


async def test_approved_hold_executes_on_resume_and_rejected_is_blocked(make_executor, new_run, store, world):
    for approve, expected_writes in ((True, 1), (False, 0)):
        world.writes.clear()
        run_id = new_run()
        ex = make_executor(gate=HoldAll())
        await go(ex, run_id, turn(write("a", "s1")))
        token = ex.claim(run_id, ("awaiting_approval",))
        store.decide_held(run_id, approve)
        agent = FakeStepAgent([turn(write("a", "s1"), complete("s1"), complete("s2"), done=True)])
        status = await ex.run_claimed(run_id, token, agent)
        assert len(world.writes) == expected_writes
        assert status == "completed"
        assert (store.step_states(run_id)["s1"][0] == "complete") == approve  # rejected: s1 is blocked


async def test_approval_is_for_the_held_call_only(make_executor, new_run, store, world):
    run_id = new_run()
    ex = make_executor(gate=ToolPolicyGate(needs_approval=frozenset({"move_task"})))
    await go(ex, run_id, turn(write("a", "s1")))
    token = ex.claim(run_id, ("awaiting_approval",))
    store.decide_held(run_id, True)
    agent = FakeStepAgent([turn(write("b", "s1"))])  # a different target than the one approved
    assert await ex.run_claimed(run_id, token, agent) == "awaiting_approval"
    assert world.writes == []


async def test_claim_refuses_the_wrong_state(make_executor, new_run):
    ex = make_executor()
    run_id = new_run()
    with pytest.raises(ConflictError):
        ex.claim(run_id, ("awaiting_approval",))
    ex.claim(run_id, ("pending",))
    with pytest.raises(ConflictError):
        ex.claim(run_id, ("pending",))  # a second claim loses

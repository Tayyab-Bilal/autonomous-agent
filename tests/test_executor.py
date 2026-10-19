from conftest import complete, turn, write

from autonomous_agent.executor import (
    FakeStepAgent,
    TerminalError,
    ToolCall,
    TransientError,
)
from autonomous_agent.gate import ALLOW, block


async def go(ex, run_id, *script):
    agent = FakeStepAgent(list(script))
    return await ex.execute(run_id, agent), agent


async def test_happy_path_completes(make_executor, new_run, store):
    run_id = new_run()
    status, _ = await go(make_executor(), run_id,
                         turn(write("a", "s1"), complete("s1"), complete("s2"), done=True))
    assert status == "completed"
    assert store.get_run(run_id)["writes"] == 1


async def test_step_budget_parks_resumable(make_executor, new_run, store):
    run_id = new_run()
    reads = [turn(ToolCall(name="read_tasks", args={"i": i})) for i in range(5)]
    status, agent = await go(make_executor(step_budget=3), run_id, *reads)
    row = store.get_run(run_id)
    assert status == "parked" and "step budget" in row["reason"]
    assert len(agent.scripted) == 2  # stopped after exactly 3 turns


async def test_write_cap_shared_across_resumes(make_executor, new_run, store, runs, world):
    run_id = new_run()
    ex = make_executor(write_cap=3)
    # Run 1: two writes, then the turn crashes and the run parks.
    status, _ = await go(ex, run_id, turn(write("a"), write("b")), RuntimeError("worker hiccup"))
    assert status == "parked" and store.writes_used(run_id) == 2
    # Resume: a per-resume counter would allow three more writes; the shared cap allows one.
    status, _ = await go(ex, run_id, turn(write("c"), write("d"), write("e")))
    assert status == "failed" and "write cap" in store.get_run(run_id)["reason"]
    assert len(world.writes) == 3


async def test_resume_does_not_repeat_successful_write(make_executor, new_run, runs, world):
    run_id = new_run()
    ex = make_executor()
    await go(ex, run_id, turn(write("a", "s1"), complete("s1")), RuntimeError("worker hiccup"))
    assert len(world.writes) == 1
    status, _ = await go(ex, run_id, turn(write("a", "s1"), complete("s2"), done=True))
    assert status == "completed"
    assert len(world.writes) == 1  # the re-issued write was a ledger no-op


async def test_different_args_are_a_different_write(make_executor, new_run, runs, world):
    run_id = new_run()
    ex = make_executor()
    await go(ex, run_id, turn(write("a"), write("b")), RuntimeError("x"))
    assert len(world.writes) == 2


async def test_transient_failure_parks_resumable(make_executor, new_run, store, world):
    run_id = new_run()
    world.fail_with = TransientError("rate limited")
    status, _ = await go(make_executor(max_retries=2), run_id, turn(write("a", "s1")))
    assert status == "parked"
    assert "3 attempts" in store.get_run(run_id)["reason"]
    assert world.writes == []


async def test_transient_failure_retries_in_turn_then_succeeds(make_executor, new_run, world):
    run_id = new_run()
    attempts = {"n": 0}
    orig = world.write

    async def flaky(args):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientError("blip")
        return await orig(args)

    ex = make_executor()
    ex.tools["move_task"].fn = flaky
    status, _ = await go(ex, run_id, turn(write("a", "s1"), complete("s1"), complete("s2"), done=True))
    assert status == "completed" and attempts["n"] == 3


async def test_terminal_failure_fails_run_even_if_agent_says_done(make_executor, new_run, store, world):
    run_id = new_run()
    world.fail_with = TerminalError("permission denied")
    # Terminal failure on the first write; the agent then tries a second write and claims done.
    status, _ = await go(make_executor(), run_id,
                         turn(write("a", "s1"), write("b", "s2"), complete("s2"), done=True))
    assert status == "failed"
    assert store.step_states(run_id)["s1"][0] == "failed"
    world.fail_with = None
    assert world.writes == [] and store.writes_used(run_id) == 0  # the second write never ran


async def test_record_failure_is_terminal(make_executor, new_run):
    run_id = new_run()
    fail = ToolCall(name="record_failure", args={"step_id": "s1", "cause": "entity deleted"})
    status, _ = await go(make_executor(), run_id, turn(fail, complete("s2"), done=True))
    assert status == "failed"


async def test_exception_never_completes(make_executor, new_run, store):
    run_id = new_run()
    # Both steps settled, then the next turn crashes: still not "completed".
    status, _ = await go(make_executor(), run_id,
                         turn(complete("s1"), complete("s2")), RuntimeError("segfault"))
    assert status == "parked"
    assert "crashed" in store.get_run(run_id)["reason"]


async def test_unsettled_steps_when_agent_says_done_is_not_completed(make_executor, new_run):
    run_id = new_run()
    status, _ = await go(make_executor(), run_id, turn(complete("s1"), done=True))
    assert status == "parked"


class BlockWrites:
    def check(self, call):
        return block("policy: writes disabled")


async def test_all_blocked_is_failed(make_executor, new_run, store, world):
    run_id = new_run()
    status, _ = await go(make_executor(gate=BlockWrites()), run_id,
                         turn(write("a", "s1"), write("b", "s2"), done=True))
    assert status == "failed" and store.get_run(run_id)["reason"] == "all actions blocked"
    assert world.writes == []


class BlockTaskB:
    def check(self, call):
        return block("policy: no") if call.args["task"] == "b" else ALLOW


async def test_some_blocked_is_completed_with_list(make_executor, new_run, store, sink):
    run_id = new_run()
    status, _ = await go(make_executor(gate=BlockTaskB()), run_id,
                         turn(write("a", "s1"), complete("s1"), write("b", "s2"), done=True))
    assert status == "completed"
    assert store.get_run(run_id)["reason"] == "blocked steps: s2"
    assert sink.events[-1]["data"]["blocked_steps"] == ["s2"]


async def test_loop_guard_stops_a_looping_agent(make_executor, new_run, store):
    run_id = new_run()
    same = turn(ToolCall(name="read_tasks", args={"q": "overdue"}))
    status, agent = await go(make_executor(), run_id, *[same] * 8)
    assert status == "parked" and "loop detected" in store.get_run(run_id)["reason"]
    assert len(agent.scripted) == 4  # 2 allowed + 1 nudged (3rd call) + 1 hard stop (4th) = 4 turns used


async def test_nudge_reaches_the_agent(make_executor, new_run):
    run_id = new_run()
    same = turn(ToolCall(name="read_tasks", args={"q": "x"}))
    _, agent = await go(make_executor(), run_id, *[same] * 6)
    assert "loop_guard" in agent.views[3].last_results[0]


async def test_run_context_reaches_the_agent_without_extra_fields(make_executor, new_run):
    run_id = new_run()
    _, agent = await go(make_executor(), run_id, turn(done=True))
    assert agent.views[0].run_id == run_id and len(agent.views[0].steps) == 2


async def test_agent_cannot_complete_a_gate_blocked_step(make_executor, new_run, store, world):
    run_id = new_run()
    status, _ = await go(make_executor(gate=BlockWrites()), run_id,
                         turn(write("a", "s1"), write("b", "s2")),
                         turn(complete("s1"), complete("s2"), done=True))
    assert store.step_states(run_id)["s1"][0] == "blocked"
    assert status == "failed" and store.get_run(run_id)["reason"] == "all actions blocked"


async def test_marking_a_settled_step_returns_an_error_to_the_agent(make_executor, new_run):
    run_id = new_run()
    _, agent = await go(make_executor(gate=BlockWrites()), run_id,
                        turn(write("a", "s1")), turn(complete("s1")), turn(done=True))
    assert "already blocked" in agent.views[2].last_results[0]["error"]


async def test_execute_refuses_a_run_someone_else_is_driving(make_executor, new_run, runs, world):
    import pytest

    from autonomous_agent.runs import ConflictError
    run_id = new_run()
    runs.start(run_id)  # worker A holds it
    with pytest.raises(ConflictError):
        await go(make_executor(), run_id, turn(write("a"), done=True))  # worker B
    assert world.writes == []


async def test_reaped_worker_is_fenced_out_and_cannot_write(make_executor, new_run, runs, store, world):
    """The reaper takes the run mid-turn; the slow worker must stop without a final transition."""
    run_id = new_run()

    class ReapedMidTurn(FakeStepAgent):
        async def step(self, view):
            if len(self.views) == 1:  # after turn 1, a reaper parks and another worker claims the run
                runs.transition(run_id, "parked", "worker lost")
                runs.claim_for_resume(run_id)
            return await super().step(view)

    agent = ReapedMidTurn([turn(complete("s1")), turn(write("a"), done=True)])
    status = await make_executor().execute(run_id, agent)
    assert status == "running"  # still the new owner's
    assert world.writes == [] and store.step_states(run_id)["s1"][0] == "complete"


async def test_equivalent_args_are_one_write(make_executor, new_run, world):
    run_id = new_run()
    variants = [{"task": 1, "to": "Blocked"}, {"task": "1", "to": "Blocked"},
                {"to": "Blocked", "task": 1.0}, {"task": 1, "to": "Blocked", "note": None}]
    await go(make_executor(), run_id, turn(*[ToolCall(name="move_task", args=a) for a in variants]),
             RuntimeError("stop"))
    assert len(world.writes) == 1


async def test_distinct_args_stay_distinct_writes(make_executor, new_run, world):
    run_id = new_run()
    await go(make_executor(), run_id,
             turn(*[ToolCall(name="move_task", args={"task": t}) for t in ("1", "01", "a")]),
             RuntimeError("stop"))
    assert len(world.writes) == 3


async def test_lifetime_resume_limit_stops_endless_resuming(make_executor, new_run, store, runs):
    run_id = new_run()
    ex = make_executor(step_budget=5, max_resumes=3)
    seen = 0
    for round_ in range(20):
        reads = [turn(ToolCall(name="read_tasks", args={"r": round_, "i": i})) for i in range(6)]
        status, _ = await go(ex, run_id, *reads)
        seen += 1
        if status == "failed":
            break
    assert status == "failed" and seen == 5  # first run + 3 resumes + the refused 4th
    row = store.get_run(run_id)
    assert "resume limit" in row["reason"] and row["resume_count"] == 4
    assert row["turns_used"] == 5 * 4  # budget turns persisted per park


async def test_turns_are_persisted(make_executor, new_run, store):
    run_id = new_run()
    await go(make_executor(), run_id, turn(complete("s1")), turn(complete("s2"), done=True))
    assert store.get_run(run_id)["turns_used"] == 2


async def test_nothing_done_is_failed(make_executor, new_run, store):
    run_id = new_run()
    skip = ToolCall(name="mark_step_skipped", args={"step_id": "s1", "reason": "n/a"})
    unsupported = ToolCall(name="mark_step_impossible", args={"step_id": "s2"})
    status, _ = await go(make_executor(), run_id, turn(skip, unsupported, done=True))
    assert status == "failed" and store.get_run(run_id)["reason"] == "nothing was done"


async def test_gate_block_after_agent_marked_step_complete_still_counts(make_executor, new_run, store):
    run_id = new_run()
    status, _ = await go(make_executor(gate=BlockWrites()), run_id,
                         turn(complete("s1"), complete("s2"), write("a", "s1"), write("b", "s2"), done=True))
    assert store.step_states(run_id)["s1"][0] == "blocked"
    assert status == "failed" and store.get_run(run_id)["reason"] == "all actions blocked"


async def test_reaped_before_done_returns_quietly(make_executor, new_run, runs, store):
    run_id = new_run()

    class ReapedThenDone(FakeStepAgent):
        async def step(self, view):
            runs.transition(run_id, "parked", "worker lost")  # reaped during the LLM call
            return await super().step(view)

    status = await make_executor().execute(run_id, ReapedThenDone([turn(done=True)]))
    assert status == "parked" and store.get_run(run_id)["reason"] == "worker lost"


async def test_reaped_on_last_budget_turn_returns_quietly(make_executor, new_run, runs, store):
    run_id = new_run()

    class ReapedLast(FakeStepAgent):
        async def step(self, view):
            if len(self.views) == 1:
                runs.transition(run_id, "parked", "worker lost")
            return await super().step(view)

    ex = make_executor(step_budget=2)
    status = await ex.execute(run_id, ReapedLast([turn(), turn()]))
    assert status == "parked" and store.get_run(run_id)["reason"] == "worker lost"


async def test_approval_resumes_do_not_count_against_resume_limit(make_executor, new_run, runs, store):
    run_id = new_run()
    ex = make_executor(max_resumes=1)
    runs.start(run_id)
    for _ in range(3):
        runs.transition(run_id, "awaiting_approval")
        status, _ = await go(ex, run_id, turn(complete("s1"), complete("s2"), done=True))
        if status == "completed":
            break
    assert store.get_run(run_id)["resume_count"] == 0 and status == "completed"

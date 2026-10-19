from conftest import call, complete, make_plan, turn, write

from autonomous_agent.assembler import PlanBranch
from autonomous_agent.executor import FakeStepAgent


async def go(ex, run_id, *script):
    agent = FakeStepAgent(list(script))
    return await ex.execute(run_id, agent), agent


async def test_scratchpad_and_notes_are_replayed_to_the_resumed_agent(make_executor, new_run):
    run_id = new_run()
    ex = make_executor()
    await go(ex, run_id, turn(call("write_scratchpad", key="target", value="space-9"),
                              call("add_step_note", "s1", note="found 3 overdue tasks")),
             RuntimeError("hiccup"))
    _, agent = await go(ex, run_id, turn(done=True))
    view = agent.views[0]
    assert view.scratchpad == {"target": "space-9"}
    assert view.steps[0]["notes"] == ["found 3 overdue tasks"]


async def test_scratchpad_can_be_read_back(make_executor, new_run):
    run_id = new_run()
    _, agent = await go(make_executor(), run_id,
                        turn(call("write_scratchpad", key="k", value="v"), call("read_scratchpad", key="k")),
                        turn(done=True))
    assert agent.views[1].last_results[1] == {"value": "v"}


async def test_executed_step_is_marked_already_executed_after_resume(make_executor, new_run, world):
    run_id = new_run()
    ex = make_executor()
    await go(ex, run_id, turn(write("a", "s1")), RuntimeError("crash before marking s1 complete"))
    _, agent = await go(ex, run_id, turn(done=True))
    s1, s2 = agent.views[0].steps
    assert s1["already_executed"] and s1["executed_writes"][0]["args"] == {"task": "a"}
    assert not s2["already_executed"] and not s2["settled"]
    assert len(world.writes) == 1


async def test_rewrite_plan_changes_a_pending_step_only_for_this_run(make_executor, new_run, store):
    run_id = new_run()
    _, agent = await go(make_executor(), run_id,
                        turn(call("rewrite_plan", "s2", instruction="only touch project p-1"), complete("s1")),
                        turn(done=True))
    assert agent.views[1].steps[1]["instruction"] == "only touch project p-1"
    plan_row = store.get_plan(store.get_run(run_id)["plan_id"])
    assert "only touch" not in plan_row["plan_json"]  # the shared plan is untouched
    assert len(store.log_entries(run_id, "rewrite")) == 1  # and the rewrite is logged


async def test_rewrite_plan_is_bounded(make_executor, new_run):
    run_id = new_run()
    rewrites = [call("rewrite_plan", "s1", instruction=f"v{i}") for i in range(3)]
    _, agent = await go(make_executor(max_rewrites=2), run_id, turn(*rewrites), turn(done=True))
    ok, ok2, refused = agent.views[1].last_results
    assert ok == ok2 == {"ok": True} and "limit" in refused["error"]
    long = call("rewrite_plan", "s2", instruction="x" * 501)
    _, agent = await go(make_executor(), new_run(), turn(long), turn(done=True))
    assert "characters" in agent.views[1].last_results[0]["error"]


async def test_rewrite_plan_cannot_touch_a_settled_step(make_executor, new_run):
    run_id = new_run()
    _, agent = await go(make_executor(), run_id,
                        turn(complete("s1"), call("rewrite_plan", "s1", instruction="redo it")), turn(done=True))
    assert "not pending" in agent.views[1].last_results[1]["error"]


BRANCH = PlanBranch(id="branch-1", condition_text="any overdue?", if_true=["s1"], if_false=["s2"])


async def test_choose_branch_skips_the_side_not_taken(make_executor, store, runs):
    run_id = runs.create(make_plan(store, branches=[BRANCH]), "t1", {})
    _, agent = await go(make_executor(), run_id,
                        turn(call("choose_branch", branch_key="branch-1", outcome=True), complete("s1"), done=True))
    assert store.step_states(run_id)["s2"] == ("skipped", "branch not taken")
    assert store.get_run(run_id)["status"] == "completed"


async def test_branch_cannot_be_decided_twice(make_executor, store, runs):
    run_id = runs.create(make_plan(store, branches=[BRANCH]), "t1", {})
    _, agent = await go(make_executor(), run_id,
                        turn(call("choose_branch", branch_key="branch-1", outcome=True),
                             call("choose_branch", branch_key="branch-1", outcome=False)),
                        turn(done=True))
    assert "already decided" in agent.views[1].last_results[1]["error"]
    assert agent.views[1].branches[0]["chosen"] == "true"

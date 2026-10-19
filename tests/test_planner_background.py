from autonomous_agent.grounding import EntityRef, FakeGrounder
from autonomous_agent.llm import ExtractedPlan, FakeExtractor, StepIntent
from autonomous_agent.planner import Planner

GOOD = ExtractedPlan(steps=[StepIntent(key="a", instruction="x", scope_refs=["Apollo"])], event="task overdue")
GROUNDER = FakeGrounder([EntityRef(id="p-1", kind="project", name="Apollo")])


async def test_background_planning_emits_plan_ready_with_a_request_id(store, sink):
    p = Planner(store, GROUNDER, FakeExtractor([GOOD]), sink)
    await p.create_plan_in_background("goal", "t1", "u1")
    (event,) = sink.events
    assert event["type"] == "acme.plan.ready" and event["data"]["request_id"]


async def test_background_planning_never_raises_even_on_a_busy_lock(store, sink):
    p = Planner(store, GROUNDER, FakeExtractor([]), sink)
    with store.plan_lock("t1", __import__("autonomous_agent.planner").planner.goal_hash("goal", "u1")):
        await p.create_plan_in_background("goal", "t1", "u1")  # LockBusy is swallowed
    assert sink.events == []  # the in-flight request owns the event

"""End-to-end walkthrough with fakes: plan, run, crash-safe resume, loop guard, approval, events."""
import asyncio
import json
from datetime import UTC, datetime

from autonomous_agent.assembler import next_runs
from autonomous_agent.clock import ManualClock
from autonomous_agent.events import ListEventSink
from autonomous_agent.executor import (
    AgentTurn,
    Executor,
    FakeStepAgent,
    Tool,
    ToolCall,
    TransientError,
)
from autonomous_agent.gate import ToolPolicyGate
from autonomous_agent.grounding import EntityRef, FakeGrounder
from autonomous_agent.llm import ExtractedPlan, FakeExtractor, ScheduleIntent, StepIntent
from autonomous_agent.planner import Planner
from autonomous_agent.runs import RunManager
from autonomous_agent.store import Store

GOAL = "Every Monday 9am, move overdue tasks in project Apollo to Blocked and create a review task"
FAKE_LLM_PLAN = ExtractedPlan(
    steps=[
        StepIntent(key="find", instruction="List overdue tasks in Apollo", scope_refs=["Apollo"]),
        StepIntent(key="move", instruction="Move them to Blocked", depends_on=["find"],
                   scope_refs=["Apollo"]),
        StepIntent(key="review", instruction="Create a review task", depends_on=["move"],
                   scope_refs=["Apollo"]),
    ],
    schedule=ScheduleIntent(frequency="weekly", weekdays=["MO"], hour=9, timezone="Europe/London"),
)


def turn(*calls: ToolCall, done: bool = False) -> AgentTurn:
    return AgentTurn(calls=list(calls), done=done)


def call(name: str, **args) -> ToolCall:
    return ToolCall(name=name, args=args)


def show(title: str) -> None:
    print(f"\n=== {title}")


async def main() -> None:
    clock = ManualClock(datetime(2026, 3, 20, 10, 0, tzinfo=UTC))
    sink = ListEventSink()
    store = Store(clock=clock)
    runs = RunManager(store, sink)

    show("1. Plan: ground, extract (fake LLM), assemble")
    grounder = FakeGrounder([EntityRef(id="p-1", kind="project", name="Apollo")])
    planner = Planner(store, grounder, FakeExtractor([FAKE_LLM_PLAN]), sink)
    result = await planner.create_plan(GOAL, "acme", "u-1")
    plan = result.plan
    print(f"plan {result.plan_id}: {len(plan.steps)} steps, scope ids {sorted(plan.scope_ids)}")
    print(f"RRULE {plan.schedule.rrule}  TZID={plan.schedule.tzid}")
    for r in next_runs(plan.schedule, clock(), 3):
        print(f"  next run {r:%a %d %b %Y %H:%M %Z}  (UTC {r.astimezone(UTC):%H:%M})")
    again = await planner.create_plan(GOAL.lower(), "acme", "u-1")
    print(f"retry within the window reused the plan: {again.reused}")

    # Fake downstream system. The review task is flaky: it fails 3 times (one more than the
    # in-turn retries), which parks the run instead of failing it.
    moves, creates, flaky = [], [], {"left": 3}

    async def list_tasks(args):
        return {"overdue": ["task-7", "task-9"]}

    async def move_task(args):
        moves.append(args)
        return {"moved": args["task"]}

    async def create_task(args):
        if flaky["left"]:
            flaky["left"] -= 1
            raise TransientError("503 from task service")
        creates.append(args)
        return {"created": args["title"]}

    tools = {"list_tasks": Tool(list_tasks), "move_task": Tool(move_task, write=True),
             "create_task": Tool(create_task, write=True)}
    executor = Executor(store, runs, tools)
    move = call("move_task", task="task-7", to="Blocked")
    review = call("create_task", title="Apollo weekly review")

    show("2. Run: complete a step, hit a transient error, park resumable")
    run_id = runs.create(result.plan_id, "acme", {"project_id": "p-1"})
    agent = FakeStepAgent([
        turn(call("list_tasks"), call("mark_step_complete", step_id="step-1")),
        turn(move, call("mark_step_complete", step_id="step-2"), review),
    ])
    status = await executor.execute(run_id, agent)
    print(f"status: {status} ({store.get_run(run_id)['reason']})")
    print(f"writes so far: {store.writes_used(run_id)}  steps: {store.step_states(run_id)}")

    show("3. Resume (execute claims it atomically): the ledger no-ops the repeated write")
    agent = FakeStepAgent([turn(move, review, call("mark_step_complete", step_id="step-3"), done=True)])
    status = await executor.execute(run_id, agent)
    print(f"status: {status} ({store.get_run(run_id)['reason']})")
    print(f"move_task really executed {len(moves)}x, create_task {len(creates)}x, "
          f"writes counted {store.writes_used(run_id)}")

    show("4. A looping agent is stopped by the loop guard")
    loop_run = runs.create(result.plan_id, "acme", {"project_id": "p-1"})
    same = turn(call("list_tasks"))
    agent = FakeStepAgent([same] * 8)
    status = await executor.execute(loop_run, agent)
    print(f"status: {status} ({store.get_run(loop_run)['reason']})")
    print(f"turns the agent got to use: {len(agent.views)} of 8")
    nudge = next(v.last_results[0] for v in agent.views if v.last_results and
                 "loop_guard" in v.last_results[0])
    print(f"nudge the agent saw: {nudge['loop_guard']}")

    show("5. A write that needs approval parks the run; a decision resumes it")
    gated = Executor(store, runs, tools, gate=ToolPolicyGate(needs_approval=frozenset({"create_task"})))
    held_run = runs.create(result.plan_id, "acme", {"project_id": "p-1"})
    create = ToolCall(name="create_task", args={"title": "Apollo weekly review"}, step_id="step-3")
    agent = FakeStepAgent([turn(call("list_tasks"), call("mark_step_complete", step_id="step-1"),
                                call("mark_step_skipped", step_id="step-2"), create)])
    status = await gated.execute(held_run, agent)
    print(f"status: {status} ({store.get_run(held_run)['reason']})")
    print(f"held for a person: {store.held_writes(held_run)[0]['call']['args']}")
    token = gated.claim(held_run, ("awaiting_approval",))  # what POST /runs/{id}/decision does
    store.decide_held(held_run, approved=True)
    agent = FakeStepAgent([turn(create, call("mark_step_complete", step_id="step-3"), done=True)])
    status = await gated.run_claimed(held_run, token, agent)
    print(f"after approval: {status}; create_task really executed {len(creates)}x in total")

    show("6. CloudEvents emitted")
    for e in sink.events:
        d = e["data"]
        detail = d.get("status") or d.get("reason") or ""
        print(f"{e['type']:<28} {d.get('run_id', d.get('plan_id', '')):<14} {detail}")
    print("\nfirst event in full:")
    print(json.dumps(sink.events[0], indent=2))


if __name__ == "__main__":
    asyncio.run(main())

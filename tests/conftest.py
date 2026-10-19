from datetime import UTC, datetime

import pytest

from autonomous_agent.assembler import AssembledPlan, Step
from autonomous_agent.clock import ManualClock
from autonomous_agent.events import ListEventSink
from autonomous_agent.executor import AgentTurn, ExecConfig, Executor, Tool, ToolCall
from autonomous_agent.runs import RunManager
from autonomous_agent.store import Store


@pytest.fixture
def clock():
    return ManualClock(datetime(2026, 3, 2, 12, 0, tzinfo=UTC))


@pytest.fixture
def store(tmp_path, clock):
    st = Store(str(tmp_path / "agent.db"), clock)  # a file: the race test needs two connections
    st.db.execute("PRAGMA synchronous=OFF")  # tests do not need to survive power loss; skip fsync
    return st


@pytest.fixture
def sink():
    return ListEventSink()


@pytest.fixture
def runs(store, sink):
    return RunManager(store, sink)


def make_plan(store, tenant="t1", triggers=("TaskOverdue",), scope=("p-1",), n_steps=2, branches=()) -> str:
    steps = [Step(id=f"s{i}", key=f"k{i}", instruction=f"do {i}", scope_ids=list(scope))
             for i in range(1, n_steps + 1)]
    plan = AssembledPlan(steps=steps, triggers=list(triggers), branches=list(branches))
    return store.save_plan(tenant, "u1", "goal", "h", plan.model_dump_json())


@pytest.fixture
def new_run(store, runs):
    def _new(**kw):
        return runs.create(make_plan(store, **kw), kw.get("tenant", "t1"), {})
    return _new


# -- executor helpers ---------------------------------------------------------------------
class World:
    """Fake downstream system: records every write that really happened."""

    def __init__(self):
        self.writes: list[dict] = []
        self.fail_with: Exception | None = None

    async def write(self, args):
        if self.fail_with:
            raise self.fail_with
        self.writes.append(args)
        return {"ok": True, "n": len(self.writes)}

    async def read(self, args):
        return {"tasks": ["t1"]}


@pytest.fixture
def world():
    return World()


@pytest.fixture
def make_executor(store, runs, world):
    def _make(gate=None, **cfg):
        tools = {"move_task": Tool(world.write, write=True), "read_tasks": Tool(world.read)}
        return Executor(store, runs, tools, gate=gate, config=ExecConfig(**cfg))
    return _make


def turn(*calls: ToolCall, done=False) -> AgentTurn:
    return AgentTurn(calls=list(calls), done=done)


def write(task: str, step_id: str | None = None) -> ToolCall:
    return ToolCall(name="move_task", args={"task": task}, step_id=step_id)


def complete(step_id: str) -> ToolCall:
    return ToolCall(name="mark_step_complete", args={"step_id": step_id})


def call(name: str, step_id: str | None = None, **args) -> ToolCall:
    return ToolCall(name=name, args=args, step_id=step_id)

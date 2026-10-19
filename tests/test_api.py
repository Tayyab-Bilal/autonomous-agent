import subprocess
import sys
import time

import pytest
from conftest import complete, make_plan, turn, write
from fastapi.testclient import TestClient

from autonomous_agent.api import Services, create_app
from autonomous_agent.chat import ChatRequest
from autonomous_agent.executor import FakeStepAgent
from autonomous_agent.gate import ToolPolicyGate
from autonomous_agent.grounding import EntityRef, FakeGrounder
from autonomous_agent.llm import ExtractedPlan, FakeExtractor, StepIntent
from autonomous_agent.planner import Planner
from autonomous_agent.triggers import TriggerHandler

PLAN = ExtractedPlan(steps=[StepIntent(key="a", instruction="x", scope_refs=["Apollo"])], event="task overdue")


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


@pytest.fixture
def scripts():
    return {}  # run id -> scripted agent turns, so a test decides what each run's agent does


@pytest.fixture
def app_parts(store, runs, sink, make_executor, scripts):
    extractor = FakeExtractor([PLAN])
    planner = Planner(store, FakeGrounder([EntityRef(id="p-1", kind="project", name="Apollo")]), extractor, sink)
    ex = make_executor(gate=ToolPolicyGate(needs_approval=frozenset({"move_task"})))
    services = Services(planner, runs, ex, TriggerHandler(runs, max_concurrent_runs=1),
                        agent_factory=lambda run_id: FakeStepAgent(list(scripts[run_id])))
    return services, extractor


@pytest.fixture
def client(app_parts):
    with TestClient(create_app(app_parts[0])) as c:
        yield c


def status_of(client, run_id):
    return client.get(f"/runs/{run_id}/status").json()["status"]


def test_post_plans_answers_202_at_once_and_plans_in_the_background(app_parts, sink):
    services, extractor = app_parts
    import threading
    release = threading.Event()
    real = extractor.extract

    async def slow(goal, grounding):
        import asyncio
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await real(goal, grounding)

    extractor.extract = slow
    with TestClient(create_app(services)) as client:
        r = client.post("/plans", json={"goal": "g", "tenant": "t1", "user": "u1"})
        assert r.status_code == 202 and r.json()["status"] == "accepted"
        assert sink.events == []  # the extractor is still blocked: the response did not wait for it
        release.set()
        wait_for(lambda: any(e["type"] == "acme.plan.ready" for e in sink.events))


def test_background_planning_failure_emits_plan_failed(app_parts, sink):
    services, extractor = app_parts

    async def boom(goal, grounding):
        raise RuntimeError("provider down")

    extractor.extract = boom
    with TestClient(create_app(services)) as client:
        assert client.post("/plans", json={"goal": "g", "tenant": "t1", "user": "u1"}).status_code == 202
        wait_for(lambda: any(e["type"] == "acme.plan.failed" for e in sink.events))
    failed = next(e for e in sink.events if e["type"] == "acme.plan.failed")
    assert failed["data"]["reason"] == "INTERNAL_ERROR"


def test_start_runs_in_the_background_and_status_reports_it(client, store, runs, scripts):
    run_id = runs.create(make_plan(store), "t1", {})
    scripts[run_id] = [turn(complete("s1"), complete("s2"), done=True)]
    assert client.post(f"/runs/{run_id}/start").status_code == 202
    wait_for(lambda: status_of(client, run_id) == "completed")
    body = client.get(f"/runs/{run_id}/status").json()
    assert body["steps"]["s1"][0] == "complete" and body["reason"] == "all steps settled"


def test_starting_twice_is_409(client, store, runs, scripts):
    run_id = runs.create(make_plan(store), "t1", {})
    scripts[run_id] = [turn(complete("s1"), complete("s2"), done=True)]
    assert client.post(f"/runs/{run_id}/start").status_code == 202
    assert client.post(f"/runs/{run_id}/start").status_code == 409


def test_unknown_run_is_404(client):
    assert client.get("/runs/nope/status").status_code == 404
    assert client.post("/runs/nope/start").status_code == 404


def test_resume_route_resumes_only_parked_runs(client, store, runs, scripts):
    run_id = runs.create(make_plan(store), "t1", {})
    assert client.post(f"/runs/{run_id}/resume").status_code == 409  # pending is started, not resumed
    scripts[run_id] = [turn(complete("s1")), RuntimeError("crash")]
    client.post(f"/runs/{run_id}/start")
    wait_for(lambda: status_of(client, run_id) == "parked")
    scripts[run_id] = [turn(complete("s2"), done=True)]
    assert client.post(f"/runs/{run_id}/resume").status_code == 202
    wait_for(lambda: status_of(client, run_id) == "completed")


def test_approval_flow_over_http(client, store, runs, scripts, world):
    run_id = runs.create(make_plan(store), "t1", {})
    scripts[run_id] = [turn(write("a", "s1"))]
    client.post(f"/runs/{run_id}/start")
    wait_for(lambda: status_of(client, run_id) == "awaiting_approval")
    held = client.get(f"/runs/{run_id}/status").json()["awaiting_approval_for"]
    assert held[0]["call"]["args"] == {"task": "a"} and world.writes == []
    assert client.post(f"/runs/{run_id}/resume").status_code == 409  # approval has its own route
    scripts[run_id] = [turn(write("a", "s1"), complete("s1"), complete("s2"), done=True)]
    assert client.post(f"/runs/{run_id}/decision", json={"approve": True}).status_code == 202
    wait_for(lambda: status_of(client, run_id) == "completed")
    assert len(world.writes) == 1
    assert client.post(f"/runs/{run_id}/decision", json={"approve": True}).status_code == 409  # lost claim


def test_event_run_cap_is_429(client, store):
    make_plan(store)
    event = {"specversion": "1.0", "type": "acme.TaskOverdue", "source": "x",
             "data": {"tenant_id": "t1", "project_id": "p-1"}}
    assert client.post("/events", json={**event, "id": "e1"}).json()["run_ids"]
    assert client.post("/events", json={**event, "id": "e2"}).status_code == 429


def test_exhausted_plan_cannot_get_more_runs_422(client, store, runs):
    plan_id = make_plan(store)
    for _ in range(3):
        run_id = runs.create(plan_id, "t1", {})
        runs.start(run_id)
        runs.transition(run_id, "failed", "boom")
    r = client.post(f"/plans/{plan_id}/runs", json={"tenant": "t1"})
    assert r.status_code == 422 and "failed 3 times" in r.json()["detail"]


def test_shutdown_parks_running_runs_as_resumable(app_parts, store, runs, scripts):
    services, _ = app_parts

    class Hangs:
        async def step(self, view):
            import asyncio
            await asyncio.sleep(60)

    services.agent_factory = lambda run_id: Hangs()
    run_id = runs.create(make_plan(store), "t1", {})
    with TestClient(create_app(services)) as client:
        client.post(f"/runs/{run_id}/start")
        wait_for(lambda: status_of(client, run_id) == "running")
    assert store.get_run(run_id)["status"] == "parked"  # lifespan shutdown cancelled it


# -- structural isolation of the interactive chat path ----------------------------------------
AUTONOMOUS_FIELDS = {"plan_id", "run_id", "autonomous", "tenant", "gate", "write_cap", "step_budget", "tools"}


def test_chat_request_model_has_no_autonomous_fields_and_forbids_extras():
    assert not AUTONOMOUS_FIELDS & set(ChatRequest.model_fields)
    assert ChatRequest.model_config["extra"] == "forbid"


@pytest.mark.parametrize("field", sorted(AUTONOMOUS_FIELDS))
def test_chat_route_rejects_every_autonomous_field(client, field):
    r = client.post("/chat", json={"message": "hi", field: "x"})
    assert r.status_code == 422


def test_chat_works_for_a_plain_message(client):
    assert "you said: hi" in client.post("/chat", json={"message": "hi"}).json()["reply"]


def test_chat_path_cannot_construct_the_executor_or_the_gate():
    """Import only the chat module in a clean interpreter: if the executor, gate or run store were
    reachable from it, they would be loaded."""
    code = ("import sys, autonomous_agent.chat; "
            "banned = {'autonomous_agent.executor', 'autonomous_agent.gate', 'autonomous_agent.runs', "
            "'autonomous_agent.store', 'autonomous_agent.reconcile'}; "
            "leaked = banned & set(sys.modules); assert not leaked, leaked")
    subprocess.run([sys.executable, "-c", code], check=True)

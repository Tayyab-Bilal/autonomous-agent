"""HTTP layer. Planning and execution answer 202 at once and continue in a background task, so a
slow LLM never hits the gateway timeout; the result arrives as CloudEvents and via the status route.

Status mapping: a lost or invalid claim is 409, the event-run cap is 429, an exhausted plan is 422."""
import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .chat import ChatAgent, ChatReply, ChatRequest, FakeChatAgent, handle_chat
from .executor import Executor, StepAgent
from .planner import Planner
from .runs import ConflictError, PlanRetryLimit, RunManager
from .triggers import TooManyRuns, TriggerHandler


class PlanRequest(BaseModel):
    goal: str
    tenant: str
    user: str


class RunRequest(BaseModel):
    tenant: str
    context: dict[str, Any] = {}


class Decision(BaseModel):
    approve: bool


@dataclass
class Services:
    planner: Planner
    runs: RunManager
    executor: Executor
    triggers: TriggerHandler
    agent_factory: Callable[[str], StepAgent]  # run id -> the agent that will drive it
    chat_agent: ChatAgent = FakeChatAgent()


def create_app(services: Services) -> FastAPI:
    tasks: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)  # keep a reference, or the loop may drop the task
        task.add_done_callback(tasks.discard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for task in list(tasks):  # shutdown: each cancelled run parks itself before re-raising
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="autonomous-agent", lifespan=lifespan)
    s = services

    def claim_and_run(run_id: str, kinds: tuple[str, ...]) -> dict:
        try:
            token = s.executor.claim(run_id, kinds)
        except KeyError:
            raise HTTPException(404, f"unknown run {run_id}") from None
        except ConflictError as e:
            raise HTTPException(409, str(e)) from None
        spawn(s.executor.run_claimed(run_id, token, s.agent_factory(run_id)))
        return {"run_id": run_id, "status": "running"}

    @app.post("/plans", status_code=202)
    async def create_plan(req: PlanRequest) -> dict:
        spawn(s.planner.create_plan_in_background(req.goal, req.tenant, req.user))
        return {"status": "accepted", "watch": ["acme.plan.ready", "acme.plan.failed"]}

    @app.post("/plans/{plan_id}/runs", status_code=201)
    async def create_run(plan_id: str, req: RunRequest) -> dict:
        if s.runs.store.get_plan(plan_id) is None:
            raise HTTPException(404, f"unknown plan {plan_id}")
        try:
            return {"run_id": s.runs.create(plan_id, req.tenant, req.context)}
        except PlanRetryLimit as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/events")
    async def receive_event(event: dict) -> dict:
        try:
            return {"run_ids": s.triggers.handle_event(event)}
        except TooManyRuns as e:
            raise HTTPException(429, str(e)) from None

    @app.post("/runs/{run_id}/start", status_code=202)
    async def start_run(run_id: str) -> dict:
        return claim_and_run(run_id, ("pending",))

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str) -> dict:
        return claim_and_run(run_id, ("parked",))

    @app.post("/runs/{run_id}/decision", status_code=202)
    async def decide(run_id: str, decision: Decision) -> dict:
        try:
            token = s.executor.claim(run_id, ("awaiting_approval",))
        except KeyError:
            raise HTTPException(404, f"unknown run {run_id}") from None
        except ConflictError as e:
            raise HTTPException(409, str(e)) from None
        s.runs.store.decide_held(run_id, decision.approve)  # only the claim winner gets here
        spawn(s.executor.run_claimed(run_id, token, s.agent_factory(run_id)))
        return {"run_id": run_id, "status": "running", "approved": decision.approve}

    @app.get("/runs/{run_id}/status")
    async def run_status(run_id: str) -> dict:
        try:
            run = s.runs.store.get_run(run_id)
        except KeyError:
            raise HTTPException(404, f"unknown run {run_id}") from None
        return {"run_id": run_id, "status": run["status"], "reason": run["reason"],
                "writes_used": run["writes"], "steps": s.runs.store.step_states(run_id),
                "awaiting_approval_for": s.runs.store.held_writes(run_id)}

    @app.post("/chat")
    async def chat(req: ChatRequest) -> ChatReply:
        return await handle_chat(req, s.chat_agent)

    return app

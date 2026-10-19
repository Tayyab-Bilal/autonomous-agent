"""Unattended execution of one run. The agent proposes tool calls; this code enforces the bounds
and decides the final status from stored step states, never from what the agent claims."""
import asyncio
import contextlib
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .assembler import AssembledPlan
from .failures import TerminalError, TransientError, classify_result
from .gate import AllowAllGate, WriteGate
from .loop_guard import Level, LoopGuard
from .models import AgentTurn, RunView, ToolCall
from .reconcile import RECONCILE, Reconciler
from .runs import RESUMABLE, ConflictError, RunManager
from .store import Fenced, Store

__all__ = ["AgentTurn", "ExecConfig", "Executor", "FakeStepAgent", "RunView", "StepAgent",
           "TerminalError", "Tool", "ToolCall", "TransientError", "idempotency_key"]


class StepAgent(Protocol):
    async def step(self, view: RunView) -> AgentTurn: ...


class FakeStepAgent:
    """Plays scripted turns in order; an Exception in the script is raised from step()."""

    def __init__(self, scripted: list[AgentTurn | Exception]) -> None:
        self.scripted = list(scripted)
        self.views: list[RunView] = []

    async def step(self, view: RunView) -> AgentTurn:
        self.views.append(view)
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class Tool:
    fn: Callable[[dict], Awaitable[Any]]
    write: bool = False


@dataclass
class ExecConfig:
    step_budget: int = 90  # turns per execute() call
    write_cap: int = 150  # real writes per run, across all resumes
    max_retries: int = 2  # in-turn retries for a transient failure, then park
    max_resumes: int = 3  # lifetime resumes per run, so a stuck run cannot be resumed forever
    max_rewrites: int = 3  # rewrite_plan calls per run
    max_instruction_chars: int = 500  # longest rewritten step instruction


class _Stop(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass
class _Ctx:
    """What one execute() call owns: the run, its fencing token, its plan and its loop guard."""
    run_id: str
    token: int
    plan: AssembledPlan
    guard: LoopGuard


def _canonical(v: Any) -> Any:
    """Make equivalent args hash alike: drop None, 1 == "1" == 1.0, key order irrelevant."""
    if isinstance(v, dict):
        return {k: _canonical(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_canonical(x) for x in v]
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"0|-?[1-9][0-9]*", v):
        return int(v)
    return v


def idempotency_key(call: ToolCall) -> str:
    payload = json.dumps([call.name, _canonical(call.args)], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class Executor:
    def __init__(self, store: Store, runs: RunManager, tools: dict[str, Tool],
                 gate: WriteGate | None = None, config: ExecConfig | None = None) -> None:
        self.store, self.runs, self.tools = store, runs, tools
        self.gate, self.cfg = gate or AllowAllGate(), config or ExecConfig()
        self.read_tools = frozenset(n for n, t in tools.items() if not t.write)
        self.reconciler = Reconciler(store, self.cfg.max_rewrites, self.cfg.max_instruction_chars)

    def claim(self, run_id: str, kinds: tuple[str, ...] = ("pending", *RESUMABLE)) -> int:
        """Atomically take a run in one of `kinds`; returns the fencing token. A run in any other
        state, or one another worker took first, is a ConflictError (HTTP 409)."""
        status = self.store.get_run(run_id)["status"]
        if status not in kinds:
            raise ConflictError(f"{run_id} is {status}; expected one of: {', '.join(kinds)}")
        if status == "pending":
            return self.runs.start(run_id)
        return self.runs.claim_for_resume(run_id, tuple(k for k in kinds if k != "pending"))

    async def execute(self, run_id: str, agent: StepAgent) -> str:
        """Claim the run (pending -> running, or resume a parked one) and drive it. Claiming here
        means two workers can never both drive one run."""
        return await self.run_claimed(run_id, self.claim(run_id), agent)

    async def run_claimed(self, run_id: str, token: int, agent: StepAgent) -> str:
        """Drive a run this worker already claimed until a stop condition. Returns the run's
        status; a worker that loses the run part-way returns without touching it."""
        extra: dict = {}
        try:
            if self.store.get_run(run_id)["resume_count"] > self.cfg.max_resumes:
                raise _Stop("failed", f"resume limit of {self.cfg.max_resumes} reached")
            plan = AssembledPlan.model_validate_json(
                self.store.get_plan(self.store.get_run(run_id)["plan_id"])["plan_json"])
            self.store.init_steps(run_id, [s.id for s in plan.steps])
            ctx = _Ctx(run_id, token, plan, LoopGuard(self.read_tools))
            status, reason, extra = await self._drive(ctx, agent)
        except _Stop as stop:
            status, reason = stop.status, stop.reason
        except Fenced:
            return self.store.get_run(run_id)["status"]
        except asyncio.CancelledError:
            # Shutdown cancels background runs. Park synchronously (no await) before re-raising, so
            # the write cannot be lost to the cancellation and the run is resumable at once.
            with contextlib.suppress(ConflictError):
                self.runs.transition(run_id, "parked", "cancelled: worker shutting down",
                                     token=token)
            raise
        except Exception as e:  # a crashed turn is never "completed"
            status, reason = "parked", f"turn crashed: {e!r}"
        try:
            self.runs.transition(run_id, status, reason, extra, token=token)
        except ConflictError:
            return self.store.get_run(run_id)["status"]  # fenced out before we could finish
        return status

    def _view(self, ctx: _Ctx, results: list[dict]) -> RunView:
        run = self.store.get_run(ctx.run_id)
        states = self.store.step_states(ctx.run_id)
        notes = self.store.notes(ctx.run_id)
        executed = self.store.writes_by_step(ctx.run_id)
        rewritten = self.store.instructions(ctx.run_id)
        steps = [{"id": s.id, "key": s.key, "instruction": rewritten.get(s.id, s.instruction),
                  "status": states[s.id][0], "detail": states[s.id][1],
                  "settled": states[s.id][0] != "pending", "notes": notes.get(s.id, []),
                  "already_executed": s.id in executed, "executed_writes": executed.get(s.id, [])}
                 for s in ctx.plan.steps]
        branches = [{**b.model_dump(), "chosen": (self.store.log_entries(ctx.run_id, f"branch:{b.id}")
                                                   or [None])[0]} for b in ctx.plan.branches]
        return RunView(run_id=ctx.run_id, now=self.store.now(), context=json.loads(run["context"]),
                       steps=steps, branches=branches, scratchpad=self.store.scratchpad(ctx.run_id),
                       writes_used=self.store.writes_used(ctx.run_id), last_results=results)

    async def _drive(self, ctx: _Ctx, agent: StepAgent) -> tuple[str, str, dict]:
        results: list[dict] = []
        for _ in range(self.cfg.step_budget):
            self.store.tick(ctx.run_id, ctx.token)  # heartbeat + turn count, fenced
            view = self._view(ctx, results)
            turn = await self._retrying(ctx, lambda v=view: agent.step(v))
            results = [await self._call(ctx, c) for c in turn.calls]
            if turn.done:
                return self._finalise(ctx.run_id)
        return ("parked",
                f"step budget of {self.cfg.step_budget} turns used; resume to continue", {})

    async def _retrying(self, ctx: _Ctx, fn: Callable[[], Awaitable[Any]], step_id: str | None = None):
        """Run `fn`, retrying transient failures. Failure is read from raised errors and from the
        tool's own response (see failures.classify_result)."""
        # Simplification: retries immediately with no backoff; add an injectable sleep if providers need it.
        for attempt in range(self.cfg.max_retries + 1):
            try:
                result = await fn()
                outcome = classify_result(result)
                if outcome.kind == "terminal":
                    raise TerminalError(outcome.why)
                if outcome.kind == "transient":
                    raise TransientError(outcome.why)
                return result
            except (TransientError, TimeoutError) as e:
                last = e
            except TerminalError as e:
                if step_id:
                    self.store.set_step(ctx.run_id, step_id, "failed", str(e), ctx.token)
                raise _Stop("failed", f"terminal failure: {e}") from e
        raise _Stop("parked", f"transient failure after {attempt + 1} attempts: {last}")

    async def _call(self, ctx: _Ctx, call: ToolCall) -> dict:
        if call.name == "record_failure":
            cause = call.args.get("cause", "unspecified")
            if step_id := call.step_id or call.args.get("step_id"):
                self.store.set_step(ctx.run_id, step_id, "failed", cause, ctx.token)
            raise _Stop("failed", f"agent recorded a terminal failure: {cause}")
        if call.name in RECONCILE:
            return self.reconciler.handle(ctx.run_id, ctx.token, ctx.plan, call)
        tool = self.tools.get(call.name)
        if tool is None:
            return {"error": f"unknown tool '{call.name}'"}
        verdict = ctx.guard.check(call.name, call.args)
        if verdict.level is Level.HARD:
            raise _Stop("parked", f"loop detected: {verdict.message}")
        if verdict.level is Level.SOFT:
            return {"loop_guard": verdict.message}

        if not tool.write:
            result = await self._retrying(ctx, lambda: tool.fn(call.args), call.step_id)
        else:
            result = await self._write(ctx, call, tool)
        ctx.guard.record(call.name, call.args, result)
        return result if isinstance(result, dict) else {"result": result}

    async def _write(self, ctx: _Ctx, call: ToolCall, tool: Tool) -> Any:
        key = idempotency_key(call)
        if (done := self.store.ledger_get(ctx.run_id, key)) is not None:
            return done  # already executed in this run: a resume no-ops instead of writing again
        approval = self.store.approval(ctx.run_id, key)
        if approval == "rejected":
            return self._blocked(ctx, call, "rejected by the approver")
        if approval != "approved":  # an approved call has already passed the gate
            decision = self.gate.check(call)
            if decision.action == "block":
                return self._blocked(ctx, call, decision.reason)
            if decision.action == "hold":
                self.store.hold_write(ctx.run_id, key, decision.reason, call.model_dump())
                raise _Stop("awaiting_approval", decision.reason)
        if self.store.writes_used(ctx.run_id) >= self.cfg.write_cap:
            raise _Stop("failed", f"write cap of {self.cfg.write_cap} reached")
        self.store.check_fence(ctx.run_id, ctx.token)  # never write for a run we lost
        result = await self._retrying(ctx, lambda: tool.fn(call.args), call.step_id)
        # Simplification: a crash between the write and this insert can repeat the write once; pass
        # the key downstream as the API's own idempotency key to close the gap.
        self.store.record_write(ctx.run_id, key, result, ctx.token, step_id=call.step_id,
                                tool=call.name, args=call.args)
        return result

    def _blocked(self, ctx: _Ctx, call: ToolCall, reason: str) -> dict:
        if call.step_id:
            self.store.set_step(ctx.run_id, call.step_id, "blocked", reason, ctx.token)
        return {"blocked": reason}

    def _finalise(self, run_id: str) -> tuple[str, str, dict]:
        states = self.store.step_states(run_id)

        def by(status: str) -> list[str]:
            return sorted(i for i, (st, _) in states.items() if st == status)

        if failed := by("failed"):
            return "failed", f"steps failed: {', '.join(failed)}", {}
        if pending := by("pending"):
            return "parked", f"agent stopped with unsettled steps: {', '.join(pending)}", {}
        blocked = by("blocked")
        if blocked and len(blocked) == len(states):
            return "failed", "all actions blocked", {}
        if not by("complete"):
            return "failed", "nothing was done", {}
        reason = f"blocked steps: {', '.join(blocked)}" if blocked else "all steps settled"
        return "completed", reason, {"blocked_steps": blocked}

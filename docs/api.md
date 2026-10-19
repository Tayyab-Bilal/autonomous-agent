# API reference

Public classes and functions, grouped by module. Signatures are abridged to the parts you call.
Examples in this file run as written (`tests/test_docs.py`).

## HTTP routes

`autonomous_agent.api.create_app(services)` returns a FastAPI app. `Services` bundles the planner,
run manager, executor, trigger handler, an `agent_factory(run_id) -> StepAgent` and (optionally) a
chat agent. Planning and execution answer **202** immediately and continue in a background task; the
outcome arrives as a CloudEvent and through `GET /runs/{id}/status`. Shutting the app down cancels
those tasks, and each cancelled run parks itself as `parked` first.

| Route | Body | Answers |
| --- | --- | --- |
| `POST /plans` | `{goal, tenant, user}` | 202 at once; the plan is built in the background and `acme.plan.ready` or `acme.plan.failed` is emitted |
| `POST /plans/{plan_id}/runs` | `{tenant, context?}` | 201 `{run_id}`; 404 unknown plan; **422** if the plan already failed `max_plan_failures` times (readable cause in `detail`) |
| `POST /events` | a CloudEvent | 200 `{run_ids}` (empty for duplicates and out-of-scope events); **429** over the event-run cap |
| `POST /runs/{id}/start` | none | 202; **409** if the run is not `pending` or another worker claimed it first |
| `POST /runs/{id}/resume` | none | 202; **409** unless the run is `parked` and unclaimed |
| `POST /runs/{id}/decision` | `{approve: bool}` | 202; **409** unless the run is `awaiting_approval` and unclaimed |
| `GET /runs/{id}/status` | none | `{status, reason, writes_used, steps, awaiting_approval_for}`; 404 unknown run |
| `POST /chat` | `{message, session_id?}` | The interactive path. Its request model has **no** autonomous fields and forbids extras (422), and `chat.py` imports nothing from the executor, gate or store. |

Lost claims are 409 because every claim is one conditional `UPDATE`: exactly one caller wins.

A runnable example (a fake agent drives the run; the test client waits for the background task):

```python
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from autonomous_agent.api import Services, create_app
from autonomous_agent.assembler import AssembledPlan, Step
from autonomous_agent.clock import ManualClock
from autonomous_agent.events import ListEventSink
from autonomous_agent.executor import AgentTurn, Executor, FakeStepAgent, Tool, ToolCall
from autonomous_agent.grounding import FakeGrounder
from autonomous_agent.llm import FakeExtractor
from autonomous_agent.planner import Planner
from autonomous_agent.runs import RunManager
from autonomous_agent.store import Store
from autonomous_agent.triggers import TriggerHandler

store = Store(clock=ManualClock(datetime(2026, 3, 20, tzinfo=UTC)))
sink = ListEventSink()
runs = RunManager(store, sink)
plan = AssembledPlan(steps=[Step(id="s1", key="only", instruction="Do the one thing", scope_ids=["p-1"])],
                     triggers=["TaskOverdue"])
plan_id = store.save_plan("acme", "u-1", "goal", "hash", plan.model_dump_json())


def agent_for(run_id: str) -> FakeStepAgent:
    finish = ToolCall(name="mark_step_complete", args={"step_id": "s1"})
    return FakeStepAgent([AgentTurn(calls=[finish], done=True)])


app = create_app(Services(
    planner=Planner(store, FakeGrounder([]), FakeExtractor([]), sink), runs=runs,
    executor=Executor(store, runs, {"noop": Tool(lambda args: None)}),
    triggers=TriggerHandler(runs, max_concurrent_runs=1), agent_factory=agent_for))

with TestClient(app) as client:  # the context manager runs the app's lifespan
    run_id = client.post(f"/plans/{plan_id}/runs", json={"tenant": "acme"}).json()["run_id"]
    assert client.post(f"/runs/{run_id}/start").status_code == 202
    assert client.post(f"/runs/{run_id}/start").status_code == 409  # already claimed
    for _ in range(500):
        body = client.get(f"/runs/{run_id}/status").json()
        if body["status"] == "completed":
            break
        time.sleep(0.01)
    assert body["status"] == "completed" and body["steps"]["s1"][0] == "complete"
    assert client.post("/chat", json={"message": "hi", "plan_id": plan_id}).status_code == 422
```

```bash
curl -X POST localhost:8000/plans -H 'content-type: application/json' \
  -d '{"goal": "Every Monday, chase overdue tasks", "tenant": "acme", "user": "u-1"}'
# 202 {"status":"accepted", ...}; then wait for the acme.plan.ready event
curl -X POST localhost:8000/runs/run-1234abcd/start      # 202, or 409 if already claimed
curl localhost:8000/runs/run-1234abcd/status
```

There is no authentication here: `tenant` comes from the body. In a real deployment the caller's
identity would come from the gateway; add that before exposing these routes.

## Planning

| Name | Description |
| --- | --- |
| `Planner(store, grounder, extractor, sink, lock_ttl=LOCK_TTL)` | The planning pipeline. |
| `await Planner.create_plan(goal, tenant, user) -> PlanResult` | Lock, reuse check, ground, extract, assemble, persist, emit. Raises `LockBusy` if the same request is in flight. |
| `await Planner.create_plan_in_background(goal, tenant, user) -> None` | Same, for an unawaited task. Never raises; the outcome is a `plan.ready` / `plan.failed` event. |
| `PlanResult(plan_id, plan, refusal, reused)` | Exactly one of `plan` or `refusal` is set. |
| `goal_hash(goal, user)` | Normalised goal plus user; the reuse key, also the `request_id` in plan events. |
| `assemble(extracted, grounding) -> Assembly` | Pure. Builds step ids (`step-N`), branch ids (`branch-N`), scope ids, RRULE and triggers, or returns a `Refusal`. |
| `schedule_to_rrule(intent) -> str` | RFC 5545 rule; raises on a malformed schedule or unknown timezone. |
| `next_runs(schedule, after, n=3) -> list[datetime]` | Occurrences as aware local datetimes (wall-clock time survives DST). |
| `EVENT_MAP`, `load_catalog()` | Natural phrase to trigger type, and the catalog of types the backend can fire (`trigger_catalog.json`). |
| `Refusal(reason, detail)`, `RefusalReason` | `NEEDS_CLARIFICATION`, `ZERO_STEPS`, `NO_VALID_TRIGGER`, `UNKNOWN_SCOPE`, `CYCLE`, `BAD_DEPENDENCY`. |
| `AssembledPlan`, `Step`, `PlanBranch`, `PlanSchedule` | Pydantic models of the saved plan. |
| `PlanExtractor`, `Grounder` | Protocols for the LLM and the grounding service. `FakeExtractor` and `FakeGrounder` are scripted fakes. |

## Runs

| Name | Description |
| --- | --- |
| `RunManager(store, sink, max_plan_failures=3)` | The state machine; every transition emits a CloudEvent. |
| `RunManager.create(plan_id, tenant, context, trigger="manual") -> run_id` | A `pending` run. Raises `PlanRetryLimit` if the plan's runs already failed `max_plan_failures` times. `trigger="event"` marks it for the event-run cap. |
| `RunManager.start(run_id) -> token` | `pending` to `running`. `ConflictError` if not pending. |
| `RunManager.claim_for_resume(run_id, allowed=RESUMABLE) -> token` | Atomic claim of a parked run. The loser gets `ConflictError`. |
| `RunManager.transition(run_id, to, reason="", extra=None, *, token=None, stale_before=None)` | A legal move; `token` fences it to the claim you hold. `IllegalTransition` for an illegal move. |
| `RunManager.plan_exhausted(plan_id) -> bool` | True once the plan has `max_plan_failures` failed runs. |
| `TRANSITIONS`, `RESUMABLE` | The state table; `awaiting_approval` and `parked` are resumable. |
| `ConflictError` (HTTP 409), `IllegalTransition`, `PlanRetryLimit` (HTTP 422) | Run errors. |

States: `pending`, `running`, `awaiting_approval`, `completed`, `failed`, `parked`.

## Execution

| Name | Description |
| --- | --- |
| `Executor(store, runs, tools, gate=None, config=None)` | Drives runs. `tools` maps a name to a `Tool`. |
| `await Executor.execute(run_id, agent) -> status` | Claim (pending or resumable) and drive until a stop condition. |
| `Executor.claim(run_id, kinds=("pending", *RESUMABLE)) -> token` | Claim only; `ConflictError` for the wrong state or a lost race. The HTTP routes use this so a lost claim is a synchronous 409. |
| `await Executor.run_claimed(run_id, token, agent) -> status` | Drive a run you already claimed. On `asyncio.CancelledError` it parks the run `parked` synchronously, then re-raises. |
| `ExecConfig(step_budget=90, write_cap=150, max_retries=2, max_resumes=3, max_rewrites=3, max_instruction_chars=500)` | The bounds. |
| `Tool(fn, write=False)` | An async callable taking one args dict. Writes go through the gate, the ledger and the write cap. |
| `StepAgent.step(view: RunView) -> AgentTurn` | The agent seam. `FakeStepAgent(scripted)` plays turns in order. |
| `RunView` | `run_id, now, context, steps, branches, scratchpad, writes_used, last_results`. Each step has `status`, `detail`, `settled`, `notes`, `already_executed`, `executed_writes`. |
| `ToolCall(name, args, step_id)`, `AgentTurn(calls, done)` | What the agent returns. |
| `idempotency_key(call)` | Hash of tool name and canonicalised args (`None` dropped, `1` equals `"1"`). |
| `TransientError`, `TerminalError` | Raise from a tool to say whether a retry can help. |
| `classify_result(result) -> Outcome(kind, why)` | `ok`, `transient` or `terminal`, read from a tool's own response. Status codes match as whole numbers; 409 and conflict wording are transient; partial bulk creates are terminal. |

Reconciliation tool names (see [usage.md](usage.md#the-reconciliation-tools)): `mark_step_complete`,
`mark_step_skipped`, `mark_step_impossible`, `record_failure`, `add_step_note`, `write_scratchpad`,
`read_scratchpad`, `rewrite_plan`, `choose_branch`.

## Gate

| Name | Description |
| --- | --- |
| `WriteGate.check(call) -> GateDecision` | Called for every new real write. |
| `GateDecision(action, reason)`, `ALLOW`, `block(reason)`, `hold(reason)` | `allow`, `block` (step `blocked`, not a failure) or `hold` (run parks `awaiting_approval`). |
| `AllowAllGate`, `ToolPolicyGate(blocked, needs_approval)` | The default and a tiny policy for demos and tests. |

## Loop guard

`LoopGuard(read_tools)` with `check(name, args) -> Verdict(level, message)` and
`record(name, args, result)`. Levels: `OK`, `SOFT` (call blocked, agent nudged), `HARD` (stop the run).
Thresholds: the 3rd identical call (same arguments, same result) is soft and the 4th is hard. A 2 to
4 call cycle is soft on its third round. Polling a read tool is soft after 4 identical results. A
different call afterwards clears the nudges.

## Triggers, events, recovery

| Name | Description |
| --- | --- |
| `TriggerHandler(runs, max_concurrent_runs=3).handle_event(event) -> list[run_id]` | De-duplicate, scope, cap, create runs. Raises `TooManyRuns` (HTTP 429) when event-triggered runs for the tenant are at the cap. |
| `EventSink.emit(event)`, `ListEventSink`, `emit_safely`, `make_event` | CloudEvents with an `idempotencykey` extension. `emit_safely` logs sink failures instead of raising. |
| `WebhookSink(url, secret, post=urllib_post)` | POST with `X-Signature-256: sha256=<HMAC of the body>` and `Idempotency-Key`. Raises on failure (callers use `emit_safely`). |
| `sign(secret, body)`, `verify(secret, body, signature)` | The signature and the receiver's constant-time check. |
| `reap_stranded(runs, now, max_age=60 min) -> list[run_id]` | Park stale `running` runs as `parked` ("worker lost"). |
| `Store` | SQLite storage. Notable public methods: `create_run`, `get_run`, `step_states`, `writes_used`, `notes`, `scratchpad`, `held_writes`, `decide_held`, `plan_lock` (raises `LockBusy`). `Fenced` means this worker no longer owns the run. |
| `ManualClock` | A clock that moves only when told to. |

Event types emitted: `acme.plan.ready`, `acme.plan.failed`, and `acme.run.<state>` for each state.

"""Plan creation pipeline: lock -> reuse check -> ground -> extract -> assemble -> persist -> emit."""
import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta

from .assembler import AssembledPlan, Refusal, assemble
from .events import EventSink, emit_safely, make_event
from .grounding import Grounder
from .llm import PlanExtractor
from .store import LOCK_TTL, LockBusy, Store


@dataclass
class PlanResult:
    plan_id: str | None = None
    plan: AssembledPlan | None = None
    refusal: Refusal | None = None
    reused: bool = False


def goal_hash(goal: str, user: str) -> str:
    """Normalised goal plus user: reuse is per (tenant, user, goal), never across users."""
    return hashlib.sha256(f"{user}\0{' '.join(goal.lower().split())}".encode()).hexdigest()[:16]


class Planner:
    def __init__(self, store: Store, grounder: Grounder, extractor: PlanExtractor, sink: EventSink,
                 lock_ttl: timedelta = LOCK_TTL):
        self.store, self.grounder, self.extractor, self.sink = store, grounder, extractor, sink
        self.lock_ttl = lock_ttl  # must exceed worst-case LLM latency

    async def create_plan(self, goal: str, tenant: str, user: str) -> PlanResult:
        h = goal_hash(goal, user)
        with self.store.plan_lock(tenant, h, self.lock_ttl):  # LockBusy -> caller retries and hits the reuse path
            if row := self.store.find_recent_plan(tenant, h):
                return PlanResult(row["id"], AssembledPlan.model_validate_json(row["plan_json"]),
                                  reused=True)
            grounding = await self.grounder.ground(goal, tenant, user)
            extracted = await self.extractor.extract(goal, grounding)
            result = assemble(extracted, grounding)
            if result.refusal:
                attempt = uuid.uuid4().hex[:8]  # a retry is a new failure, not a duplicate delivery
                self._emit("plan.failed", {"tenant": tenant, "request_id": h, "reason": result.refusal.reason.value,
                                           "detail": result.refusal.detail},
                           f"plan.failed:{tenant}:{h}:{attempt}")
                return PlanResult(refusal=result.refusal)
            plan = result.plan
            plan_id = self.store.save_plan(tenant, user, goal, h, plan.model_dump_json())
            self._emit("plan.ready", {"tenant": tenant, "request_id": h, "plan_id": plan_id, "steps": len(plan.steps),
                                      "triggers": plan.triggers, "dropped": plan.dropped},
                       f"plan.ready:{plan_id}")
            return PlanResult(plan_id, plan)

    async def create_plan_in_background(self, goal: str, tenant: str, user: str) -> None:
        """For a task that nobody awaits (the HTTP route answers 202 first, to stay under the
        gateway timeout). It must never raise: the outcome is the plan.ready / plan.failed event."""
        try:
            await self.create_plan(goal, tenant, user)
        except LockBusy:
            pass  # the same request is already in flight and will emit plan.ready itself
        except Exception as e:
            h = goal_hash(goal, user)
            self._emit("plan.failed", {"tenant": tenant, "request_id": h, "reason": "INTERNAL_ERROR",
                                       "detail": repr(e)}, f"plan.failed:{tenant}:{h}:{uuid.uuid4().hex[:8]}")

    def _emit(self, kind: str, data: dict, key: str) -> None:
        emit_safely(self.sink, make_event(f"acme.{kind}", data, key, self.store.clock()))

"""Deterministic, pure assembly: ids, dependency graph, scope resolution, RRULEs, triggers.

Everything the model might get wrong (ids, RFC 5545 syntax, event names) is built or checked
here, so a hallucination becomes a readable refusal instead of a bad write at 9am on Monday.
"""
import json
from datetime import datetime
from enum import Enum
from importlib import resources
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from pydantic import BaseModel

from .grounding import Grounding
from .llm import ExtractedPlan, ScheduleIntent

# natural phrase -> trigger type. Anything else is dropped and reported. A test pins every value
# to trigger_catalog.json, so the map cannot drift from the list of types the backend can fire.
EVENT_MAP: dict[str, str] = {
    "task created": "TaskCreated",
    "task updated": "TaskUpdated",
    "task completed": "TaskCompleted",
    "task overdue": "TaskOverdue",
    "task assigned": "TaskAssigned",
    "comment added": "CommentAdded",
    "project created": "ProjectCreated",
    "project archived": "ProjectArchived",
    "project status changed": "ProjectStatusChanged",
    "milestone reached": "MilestoneReached",
    "document created": "DocumentCreated",
    "document updated": "DocumentUpdated",
    "document shared": "DocumentShared",
    "meeting scheduled": "MeetingScheduled",
    "meeting started": "MeetingStarted",
    "meeting ended": "MeetingEnded",
    "meeting notes published": "MeetingNotesPublished",
    "workflow completed": "WorkflowCompleted",
}


def load_catalog() -> list[dict]:
    """The trigger types the backend can fire. An entry with `excluded_reason` is deliberately
    not mappable from a plan."""
    return json.loads(resources.files(__package__).joinpath("trigger_catalog.json").read_text())


class RefusalReason(str, Enum):
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    ZERO_STEPS = "ZERO_STEPS"
    NO_VALID_TRIGGER = "NO_VALID_TRIGGER"
    UNKNOWN_SCOPE = "UNKNOWN_SCOPE"
    CYCLE = "CYCLE"
    BAD_DEPENDENCY = "BAD_DEPENDENCY"  # unknown or duplicate step key


class Refusal(BaseModel):
    reason: RefusalReason
    detail: str


class Step(BaseModel):
    id: str
    key: str
    instruction: str
    tool_hint: str = ""
    depends_on: list[str] = []  # step ids
    scope_ids: list[str] = []


class PlanBranch(BaseModel):
    id: str = ""  # assigned in code (branch-1, ...): the agent names a branch by this when it decides it
    condition_text: str
    if_true: list[str] = []  # step ids
    if_false: list[str] = []


class PlanSchedule(BaseModel):
    rrule: str
    tzid: str  # kept separate from the RRULE so wall-clock time survives DST changes


class AssembledPlan(BaseModel):
    steps: list[Step]
    branches: list[PlanBranch] = []
    schedule: PlanSchedule | None = None
    triggers: list[str] = []
    dropped: list[str] = []  # what the model asked for that we could not honour
    confidence: float = 1.0

    @property
    def scope_ids(self) -> set[str]:
        return {i for s in self.steps for i in s.scope_ids}


class Assembly(BaseModel):
    plan: AssembledPlan | None = None
    refusal: Refusal | None = None


def _refuse(reason: RefusalReason, detail: str) -> Assembly:
    return Assembly(refusal=Refusal(reason=reason, detail=detail))


def schedule_to_rrule(s: ScheduleIntent) -> str:
    parts = [f"FREQ={s.frequency.upper()}"]
    if s.frequency == "weekly":
        if not s.weekdays:
            raise ValueError("weekly schedule needs at least one weekday")
        parts.append("BYDAY=" + ",".join(s.weekdays))
    elif s.frequency == "monthly":
        parts.append(f"BYMONTHDAY={s.month_day}")
    parts += [f"BYHOUR={s.hour}", f"BYMINUTE={s.minute}"]
    rule = ";".join(parts)
    rrulestr(rule, dtstart=datetime(2026, 1, 1))  # raises on anything malformed
    ZoneInfo(s.timezone)  # raises on an unknown timezone
    return rule


def next_runs(schedule: PlanSchedule, after: datetime, n: int = 3) -> list[datetime]:
    """Next n runs as aware local datetimes. The rule runs on naive wall-clock time and the
    zone is attached afterwards, so 09:00 stays 09:00 across a DST change."""
    tz = ZoneInfo(schedule.tzid)
    local = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
    rule = rrulestr(schedule.rrule, dtstart=local)
    return [d.replace(tzinfo=tz) for d in rule.xafter(local, count=n, inc=False)]


def _find_cycle(deps: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = on the DFS stack, 2 = done

    def visit(k: str, path: list[str]) -> list[str] | None:
        state[k] = 1
        for d in deps[k]:
            if state.get(d) == 1:
                return [*path, k, d]
            if d not in state and (found := visit(d, [*path, k])):
                return found
        state[k] = 2
        return None

    for k in deps:
        if k not in state and (found := visit(k, [])):
            return found
    return None


def assemble(x: ExtractedPlan, grounding: Grounding) -> Assembly:
    if x.needs_clarification:
        return _refuse(RefusalReason.NEEDS_CLARIFICATION, x.needs_clarification)
    if not x.steps:
        return _refuse(RefusalReason.ZERO_STEPS, "the goal produced no actionable steps")

    keys = [s.key for s in x.steps]
    if len(set(keys)) != len(keys):
        return _refuse(RefusalReason.BAD_DEPENDENCY, "duplicate step keys")
    for s in x.steps:
        for d in s.depends_on:
            if d not in keys:
                return _refuse(RefusalReason.BAD_DEPENDENCY, f"step '{s.key}' depends on unknown '{d}'")
    if cycle := _find_cycle({s.key: s.depends_on for s in x.steps}):
        return _refuse(RefusalReason.CYCLE, "dependency cycle: " + " -> ".join(cycle))

    ids = {k: f"step-{i + 1}" for i, k in enumerate(keys)}
    steps: list[Step] = []
    for s in x.steps:
        scope_ids = []
        for name in s.scope_refs:
            ref = grounding.entities.get(name)  # the ONLY place an id can come from
            if ref is None:
                return _refuse(RefusalReason.UNKNOWN_SCOPE, f"'{name}' was not found in grounding")
            scope_ids.append(ref.id)
        steps.append(Step(
            id=ids[s.key], key=s.key, instruction=s.instruction, tool_hint=s.tool_hint,
            depends_on=[ids[d] for d in s.depends_on], scope_ids=scope_ids,
        ))

    branches = []
    for n, b in enumerate(x.branches, start=1):
        for k in [*b.if_true, *b.if_false]:
            if k not in ids:
                return _refuse(RefusalReason.BAD_DEPENDENCY, f"branch references unknown step '{k}'")
        branches.append(PlanBranch(
            id=f"branch-{n}", condition_text=b.condition_text,
            if_true=[ids[k] for k in b.if_true], if_false=[ids[k] for k in b.if_false],
        ))

    dropped = [f"unsupported: {u}" for u in x.unsupported]
    schedule = None
    if x.schedule:
        try:
            schedule = PlanSchedule(rrule=schedule_to_rrule(x.schedule), tzid=x.schedule.timezone)
        except Exception as e:  # ValueError, ZoneInfoNotFoundError
            dropped.append(f"schedule dropped: {e}")
    triggers: list[str] = []
    if x.event:
        trigger = EVENT_MAP.get(x.event.strip().lower())
        if trigger:
            triggers.append(trigger)
        else:
            dropped.append(f"event '{x.event}' has no trigger mapping")

    if not schedule and not triggers:
        detail = "; ".join(dropped) or "no schedule or event was given"
        return _refuse(RefusalReason.NO_VALID_TRIGGER, detail)

    return Assembly(plan=AssembledPlan(
        steps=steps, branches=branches, schedule=schedule, triggers=triggers,
        dropped=dropped, confidence=x.confidence,
    ))

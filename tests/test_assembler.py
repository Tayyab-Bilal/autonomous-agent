from datetime import UTC, datetime, timedelta

from autonomous_agent.assembler import (
    PlanSchedule,
    RefusalReason,
    assemble,
    next_runs,
    schedule_to_rrule,
)
from autonomous_agent.grounding import EntityRef, Grounding
from autonomous_agent.llm import ExtractedPlan, ScheduleIntent, StepIntent

GROUNDING = Grounding(entities={"Apollo": EntityRef(id="p-1", kind="project", name="Apollo")})


def plan(**kw) -> ExtractedPlan:
    kw.setdefault("event", "task overdue")
    kw.setdefault("steps", [StepIntent(key="a", instruction="find overdue", scope_refs=["Apollo"])])
    return ExtractedPlan(**kw)


def test_llm_cannot_inject_ids():
    # The model names an entity grounding never returned (or smuggles a raw id): rejected.
    for bad in ("Zeus", "p-999"):
        r = assemble(plan(steps=[StepIntent(key="a", instruction="x", scope_refs=[bad])]), GROUNDING)
        assert r.plan is None and r.refusal.reason is RefusalReason.UNKNOWN_SCOPE


def test_scope_resolves_only_through_grounding():
    r = assemble(plan(), GROUNDING)
    assert r.plan.steps[0].scope_ids == ["p-1"]


def test_weekly_schedule_to_rrule():
    s = ScheduleIntent(frequency="weekly", weekdays=["MO"], hour=9, minute=0, timezone="Europe/London")
    assert schedule_to_rrule(s) == "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0"


def test_rrule_next_runs_respect_timezone_dst():
    # Europe/London clocks go forward on Sunday 29 March 2026: Monday 9am is 09:00Z, then 08:00Z.
    sched = PlanSchedule(rrule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0", tzid="Europe/London")
    runs = next_runs(sched, datetime(2026, 3, 20, tzinfo=UTC), 3)
    assert [r.hour for r in runs] == [9, 9, 9]  # wall-clock time is stable
    assert [r.astimezone(UTC).hour for r in runs] == [9, 8, 8]  # the UTC instant moves
    assert [r.utcoffset() for r in runs] == [timedelta(0), timedelta(hours=1), timedelta(hours=1)]
    assert [r.day for r in runs] == [23, 30, 6]


def test_dependency_cycle_rejected():
    steps = [StepIntent(key="a", instruction="x", depends_on=["c"]),
             StepIntent(key="b", instruction="x", depends_on=["a"]),
             StepIntent(key="c", instruction="x", depends_on=["b"])]
    r = assemble(plan(steps=steps), GROUNDING)
    assert r.refusal.reason is RefusalReason.CYCLE


def test_unknown_dependency_rejected():
    r = assemble(plan(steps=[StepIntent(key="a", instruction="x", depends_on=["ghost"])]), GROUNDING)
    assert r.refusal.reason is RefusalReason.BAD_DEPENDENCY


def test_valid_dependencies_become_step_ids():
    steps = [StepIntent(key="a", instruction="x"), StepIntent(key="b", instruction="y", depends_on=["a"])]
    r = assemble(plan(steps=steps), GROUNDING)
    assert r.plan.steps[1].depends_on == [r.plan.steps[0].id]


def test_unmappable_event_dropped_and_reported():
    sched = ScheduleIntent(frequency="daily", hour=8)
    r = assemble(plan(event="volcano erupts", schedule=sched), GROUNDING)
    assert r.plan is not None and r.plan.triggers == []
    assert any("volcano erupts" in d for d in r.plan.dropped)


def test_mappable_event_becomes_trigger():
    assert assemble(plan(event="Task Overdue"), GROUNDING).plan.triggers == ["TaskOverdue"]


def test_all_triggers_dropped_refuses():
    r = assemble(plan(event="volcano erupts"), GROUNDING)
    assert r.refusal.reason is RefusalReason.NO_VALID_TRIGGER
    assert "volcano erupts" in r.refusal.detail


def test_zero_steps_refused():
    assert assemble(plan(steps=[]), GROUNDING).refusal.reason is RefusalReason.ZERO_STEPS


def test_needs_clarification_refused():
    r = assemble(plan(needs_clarification="Which project?"), GROUNDING)
    assert r.refusal.reason is RefusalReason.NEEDS_CLARIFICATION and "Which project?" in r.refusal.detail


def test_invalid_timezone_schedule_is_dropped_not_crashed():
    sched = ScheduleIntent(frequency="daily", hour=8, timezone="Mars/Olympus")
    assert assemble(plan(schedule=sched, event=None), GROUNDING).refusal.reason is RefusalReason.NO_VALID_TRIGGER


def test_plan_with_no_schedule_and_no_event_is_refused():
    r = assemble(plan(event=None), GROUNDING)
    assert r.plan is None and r.refusal.reason is RefusalReason.NO_VALID_TRIGGER

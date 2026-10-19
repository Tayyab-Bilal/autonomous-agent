"""The model's whole job: pull structure and intent out of a goal. No ids, no RRULEs."""
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .grounding import Grounding


class StepIntent(BaseModel):
    key: str
    instruction: str
    tool_hint: str = ""
    depends_on: list[str] = []
    scope_refs: list[str] = []  # names the model saw in grounding, never ids


class Branch(BaseModel):
    condition_text: str
    if_true: list[str] = []  # step keys
    if_false: list[str] = []


class ScheduleIntent(BaseModel):
    frequency: Literal["daily", "weekly", "monthly"]
    weekdays: list[Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]] = []
    month_day: int = Field(default=1, ge=1, le=28)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    timezone: str = "UTC"


class ExtractedPlan(BaseModel):
    steps: list[StepIntent] = []
    branches: list[Branch] = []
    schedule: ScheduleIntent | None = None
    event: str | None = None  # natural phrase, e.g. "task overdue"
    confidence: float = 1.0
    needs_clarification: str | None = None
    unsupported: list[str] = []


class PlanExtractor(Protocol):
    async def extract(self, goal: str, grounding: Grounding) -> ExtractedPlan: ...


class FakeExtractor:
    """Returns scripted plans in order; running out means the code called the LLM too often."""

    def __init__(self, scripted: list[ExtractedPlan]) -> None:
        self.scripted = list(scripted)
        self.calls = 0

    async def extract(self, goal: str, grounding: Grounding) -> ExtractedPlan:
        self.calls += 1
        if not self.scripted:
            raise AssertionError("FakeExtractor script exhausted")
        return self.scripted.pop(0)

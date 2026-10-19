"""The shapes passed between the executor and the step agent."""
from typing import Any

from pydantic import BaseModel


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any] = {}
    step_id: str | None = None


class AgentTurn(BaseModel):
    calls: list[ToolCall] = []
    done: bool = False


class RunView(BaseModel):
    """Everything the agent is allowed to know at the start of a turn. It is rebuilt from the
    database every turn, so a resumed agent sees exactly what the first one left behind: step
    states, notes, scratchpad, and which steps already did their writes."""
    run_id: str
    now: str
    context: dict
    steps: list[dict]
    branches: list[dict] = []
    scratchpad: dict[str, str] = {}
    writes_used: int
    last_results: list[dict] = []

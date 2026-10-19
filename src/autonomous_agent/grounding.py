"""Grounding: the only source of ids a plan may reference."""
from typing import Protocol

from pydantic import BaseModel


class EntityRef(BaseModel):
    id: str
    kind: str  # project, task, document, meeting
    name: str


class Grounding(BaseModel):
    entities: dict[str, EntityRef] = {}


class Grounder(Protocol):
    async def ground(self, goal: str, tenant: str, user: str) -> Grounding: ...


class FakeGrounder:
    def __init__(self, entities: list[EntityRef]) -> None:
        self.grounding = Grounding(entities={e.name: e for e in entities})

    async def ground(self, goal: str, tenant: str, user: str) -> Grounding:
        return self.grounding

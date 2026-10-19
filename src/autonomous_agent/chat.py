"""The interactive chat path. It is deliberately a different island from the autonomous one.

This module imports nothing from the executor, the gate or the run store, and `ChatRequest` has no
autonomous fields and forbids extras. So no chat request can name a plan or a run, and nothing on
this path can construct the executor or the write gate: isolation by structure, not by an `if`."""
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    session_id: str = "default"


class ChatReply(BaseModel):
    reply: str


class ChatAgent(Protocol):
    async def reply(self, message: str) -> str: ...


class FakeChatAgent:
    async def reply(self, message: str) -> str:
        return f"(fake chat agent) you said: {message}"


async def handle_chat(request: ChatRequest, agent: ChatAgent) -> ChatReply:
    return ChatReply(reply=await agent.reply(request.message))

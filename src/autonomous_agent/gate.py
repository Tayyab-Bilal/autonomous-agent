"""The permission seam: every real write passes through a WriteGate before it runs."""
from dataclasses import dataclass
from typing import Literal, Protocol

from .models import ToolCall


@dataclass(frozen=True)
class GateDecision:
    """allow: run the write. block: refuse it (a policy outcome, not a failure).
    hold: park the run as awaiting_approval until a person decides."""
    action: Literal["allow", "block", "hold"]
    reason: str = ""


ALLOW = GateDecision("allow")


def block(reason: str) -> GateDecision:
    return GateDecision("block", reason)


def hold(reason: str) -> GateDecision:
    return GateDecision("hold", reason)


class WriteGate(Protocol):
    def check(self, call: ToolCall) -> GateDecision: ...


class AllowAllGate:
    """The default. Plug a policy engine in here."""

    def check(self, call: ToolCall) -> GateDecision:
        return ALLOW


class ToolPolicyGate:
    """A tiny policy: some tools are never allowed, some need a person's approval."""

    def __init__(self, blocked: frozenset[str] = frozenset(), needs_approval: frozenset[str] = frozenset()):
        self.blocked, self.needs_approval = blocked, needs_approval

    def check(self, call: ToolCall) -> GateDecision:
        if call.name in self.blocked:
            return block(f"policy: '{call.name}' is not allowed in autonomous runs")
        if call.name in self.needs_approval:
            return hold(f"policy: '{call.name}' needs approval")
        return ALLOW

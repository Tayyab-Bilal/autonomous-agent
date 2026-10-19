"""Catches an agent that is going in circles before it burns the whole step budget."""
import json
from dataclasses import dataclass
from enum import Enum


class Level(str, Enum):
    OK = "ok"
    SOFT = "soft"  # block this call and tell the agent why
    HARD = "hard"  # the agent ignored the nudges: stop the run


@dataclass
class Verdict:
    level: Level
    message: str = ""


# The third identical call is blocked softly; if the agent repeats the pattern anyway, the next
# violation (the fourth call) is hard. Cycles and polling follow the same soft-then-hard shape.
SOFT_LIMIT = 1
IDENTICAL_BEFORE_BLOCK = 2  # identical call+result records seen before the next one is blocked
CYCLE_REPEATS = 2  # full repeats of a 2-4 call cycle seen before the next round is blocked
POLL_BEFORE_BLOCK = 4  # identical read results seen before the next poll is blocked


def sig(*parts) -> str:
    return json.dumps(parts, sort_keys=True, default=str)


class LoopGuard:
    def __init__(self, read_tools: frozenset[str] = frozenset()) -> None:
        self.read_tools = read_tools
        self.history: list[tuple[str, str, str]] = []  # (tool, call signature, result signature)
        self.nudges = 0
        self._blocked: str | None = None  # signature of the call we last blocked

    def record(self, name: str, args: dict, result) -> None:
        call = sig(name, args)
        if self.nudges and call != self._blocked and call not in {e[1] for e in self.history[-6:]}:
            self.nudges = 0  # the agent did something new: the nudge worked
        self.history.append((name, call, sig(result)))

    def check(self, name: str, args: dict) -> Verdict:
        why = self._pattern(name, sig(name, args))
        if not why:
            return Verdict(Level.OK)
        self.nudges += 1
        self._blocked = sig(name, args)
        if self.nudges > SOFT_LIMIT:
            return Verdict(Level.HARD, why)
        return Verdict(Level.SOFT, f"Blocked: {why}. Try a different approach or finish the run.")

    def _pattern(self, name: str, call: str) -> str:
        h = self.history
        n = IDENTICAL_BEFORE_BLOCK
        if len(h) >= n and all(e[1] == call for e in h[-n:]) and len({e[2] for e in h[-n:]}) == 1:
            return f"the same call returned the same result {n} times already"
        for length in (2, 3, 4):
            tail = [(e[1], e[2]) for e in h[-CYCLE_REPEATS * length:]]
            block = tail[:length]
            if (len(tail) == CYCLE_REPEATS * length and tail == block * CYCLE_REPEATS
                    and len({c for c, _ in block}) > 1 and call == block[0][0]):
                return f"a cycle of {length} calls repeated {CYCLE_REPEATS} times"
        if name in self.read_tools:
            streak = []  # consecutive calls to this tool at the end of history
            for e in reversed(h):
                if e[0] != name:
                    break
                streak.append(e[2])
            if len(streak) >= POLL_BEFORE_BLOCK and len(set(streak[:POLL_BEFORE_BLOCK])) == 1:
                return f"polled '{name}' {POLL_BEFORE_BLOCK} times with identical results"
        return ""

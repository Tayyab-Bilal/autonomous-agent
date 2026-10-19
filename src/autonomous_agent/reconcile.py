"""Reconciliation tools: the only way the agent can change what a run believes about its plan.

Each one writes through the store's fenced methods, so they cannot touch a run the worker lost.
They never create ids or steps: the agent can settle, annotate and re-word, nothing more."""
import json

from .assembler import AssembledPlan
from .models import ToolCall
from .store import Store

SETTLE = {"mark_step_complete": "complete", "mark_step_skipped": "skipped",
          "mark_step_impossible": "unsupported"}
RECONCILE = {*SETTLE, "record_failure", "add_step_note", "write_scratchpad", "read_scratchpad",
             "rewrite_plan", "choose_branch"}


class Reconciler:
    def __init__(self, store: Store, max_rewrites: int, max_instruction_chars: int) -> None:
        self.store, self.max_rewrites, self.max_chars = store, max_rewrites, max_instruction_chars

    def handle(self, run_id: str, token: int, plan: AssembledPlan, call: ToolCall) -> dict:
        args = call.args
        step_id = call.step_id or args.get("step_id")
        states = self.store.step_states(run_id)
        if call.name == "write_scratchpad":
            self.store.scratch_set(run_id, str(args.get("key", "")), str(args.get("value", "")), token)
            return {"ok": True}
        if call.name == "read_scratchpad":
            return {"value": self.store.scratchpad(run_id).get(str(args.get("key", "")))}
        if call.name == "choose_branch":
            return self._choose_branch(run_id, token, plan, states, args)
        if step_id not in states:
            return {"error": f"unknown step '{step_id}'"}
        if call.name == "add_step_note":
            self.store.add_note(run_id, step_id, str(args.get("note", "")), token)
            return {"ok": True}
        if call.name == "rewrite_plan":
            return self._rewrite(run_id, token, step_id, str(args.get("instruction", "")))
        if not self.store.set_step(run_id, step_id, SETTLE[call.name], args.get("reason", ""), token):
            return {"error": f"step '{step_id}' is already {states[step_id][0]} and cannot be changed"}
        return {"ok": True}

    def _rewrite(self, run_id: str, token: int, step_id: str, text: str) -> dict:
        """Bounded: pending steps only, instruction text only, a few times per run, always logged."""
        if not text.strip() or len(text) > self.max_chars:
            return {"error": f"instruction must be 1-{self.max_chars} characters"}
        if len(self.store.log_entries(run_id, "rewrite")) >= self.max_rewrites:
            return {"error": f"rewrite limit of {self.max_rewrites} per run reached"}
        old = self.store.instructions(run_id).get(step_id)
        if not self.store.set_instruction(run_id, step_id, text, token):
            return {"error": f"step '{step_id}' is not pending, so it cannot be rewritten"}
        self.store.log(run_id, "rewrite", json.dumps({"step_id": step_id, "old": old, "new": text}))
        return {"ok": True}

    def _choose_branch(self, run_id: str, token: int, plan: AssembledPlan, states: dict, args: dict) -> dict:
        """Record which side of an if/else the run is on and skip the steps of the other side."""
        branch = next((b for b in plan.branches if b.id == args.get("branch_key")), None)
        if branch is None:
            return {"error": f"unknown branch '{args.get('branch_key')}'"}
        outcome = args.get("outcome")
        if not isinstance(outcome, bool):
            return {"error": "outcome must be true or false"}
        kind = f"branch:{branch.id}"
        if previous := self.store.log_entries(run_id, kind):
            return {"error": f"branch '{branch.id}' was already decided as {previous[0]}"}
        self.store.check_fence(run_id, token)
        self.store.log(run_id, kind, "true" if outcome else "false")
        skipped = []
        for step_id in branch.if_false if outcome else branch.if_true:
            if states.get(step_id, ("",))[0] == "pending":
                self.store.set_step(run_id, step_id, "skipped", "branch not taken", token)
                skipped.append(step_id)
        return {"ok": True, "skipped": skipped}

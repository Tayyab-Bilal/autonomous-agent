"""Inbound events -> pending runs. De-duplicated, scoped, capped, and free of free text."""
import re

from .assembler import AssembledPlan
from .runs import RunManager

# Typed allow-list. Titles, descriptions and comments are user text and can carry prompt
# injection; they never enter run context. The agent re-reads them through tools instead.
ALLOWED_CONTEXT_KEYS = frozenset(
    {"tenant_id", "project_id", "task_id", "document_id", "meeting_id", "user_id"})
ID_FIELDS = ("project_id", "document_id", "meeting_id")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # an id, not a sentence


class TooManyRuns(Exception):
    """HTTP 429 in the API."""


def _is_id(v: object) -> bool:
    if isinstance(v, bool):
        return False
    return isinstance(v, int) or (isinstance(v, str) and ID_PATTERN.match(v) is not None)


class TriggerHandler:
    def __init__(self, runs: RunManager, max_concurrent_runs: int = 3) -> None:
        self.runs, self.store, self.cap = runs, runs.store, max_concurrent_runs

    def handle_event(self, event: dict) -> list[str]:
        """Returns the ids of runs created (empty for duplicates and out-of-scope events).

        Claiming the event id, checking the cap and creating the runs happen in one transaction:
        two events cannot both slip under the cap, and a failure part-way rolls the claim back so a
        redelivery is processed instead of being dropped as a duplicate.
        Simplification: run events are emitted inside the transaction (best-effort); use an outbox table
        to emit only after commit."""
        data = event.get("data", {})
        tenant = data.get("tenant_id")
        context = {k: v for k, v in data.items() if k in ALLOWED_CONTEXT_KEYS and _is_id(v)}
        trigger = event["type"].rsplit(".", 1)[-1]
        event_scope = {data[k] for k in ID_FIELDS if k in data and _is_id(data[k])}

        with self.store.tx():
            if not self.store.claim_event(event["id"]):
                return []
            matches = []
            for row in self.store.plans_for_tenant(tenant):
                plan = AssembledPlan.model_validate_json(row["plan_json"])
                if trigger in plan.triggers and plan.scope_ids & event_scope:
                    if self.runs.plan_exhausted(row["id"]):
                        continue  # fail closed and quietly: a 4xx would make the sender retry forever
                    matches.append(row["id"])
            if not matches:
                return []  # out of scope is normal traffic, not an error
            # The cap protects against event storms, so only event-triggered runs count; a person
            # starting a run by hand is never blocked by it.
            if self.store.active_runs(tenant, "event") + len(matches) > self.cap:
                raise TooManyRuns(f"tenant {tenant} is at its concurrent run cap of {self.cap}")
            return [self.runs.create(plan_id, tenant, context, "event") for plan_id in matches]

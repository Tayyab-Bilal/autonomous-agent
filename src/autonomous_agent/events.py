"""CloudEvents-shaped events. Emission is best-effort: a broken sink never blocks a DB write."""
import logging
import uuid
from datetime import datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)
SOURCE = "urn:acme-workspace:autonomous-agent"


def make_event(type_: str, data: dict[str, Any], idempotency_key: str, now: datetime) -> dict:
    return {
        "specversion": "1.0",
        "type": type_,
        "source": SOURCE,
        "id": str(uuid.uuid4()),
        "time": now.isoformat(),
        "idempotencykey": idempotency_key,  # CloudEvents extension: consumers de-duplicate on it
        "data": data,
    }


class EventSink(Protocol):
    def emit(self, event: dict) -> None: ...


class ListEventSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def emit_safely(sink: EventSink, event: dict) -> None:
    try:
        sink.emit(event)
    except Exception:
        log.warning("event sink failed for %s", event["type"], exc_info=True)

"""Is a failed tool call worth retrying? Decided from what the tool said, not only from exceptions.

Real tools often return an error instead of raising, so the executor feeds every tool result
through `classify_result` as well."""
import re
from dataclasses import dataclass
from typing import Any, Literal


class TransientError(Exception):
    """Worth retrying (timeout, rate limit)."""


class TerminalError(Exception):
    """Retrying cannot help (permission denied, entity deleted)."""


@dataclass(frozen=True)
class Outcome:
    kind: Literal["ok", "transient", "terminal"]
    why: str = ""


OK = Outcome("ok")
# A status code is a whole number: "task 1404" must not read as 404, nor "item 1500" as 500.
STATUS_CODE = re.compile(r"(?<!\d)([1-5]\d\d)(?!\d)")
TRANSIENT_CODES = {408, 409, 425, 429}  # 409: the backend's optimistic-concurrency refusal
TRANSIENT_WORDS = ("conflict", "optimistic", "concurrent modification", "version mismatch",
                   "timed out", "timeout", "rate limit", "temporarily unavailable")


def _status_code(result: dict[str, Any]) -> int | None:
    for key in ("status", "status_code", "code"):
        v = result.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599:
            return v
    match = STATUS_CODE.search(str(result.get("error") or ""))
    return int(match.group(1)) if match else None


def _partial_bulk(result: dict[str, Any]) -> bool:
    return result.get("partial") is True or bool(result.get("created") and result.get("failed"))


def classify_result(result: Any) -> Outcome:
    if not isinstance(result, dict):
        return OK
    if _partial_bulk(result):
        # Some items already exist. A retry would create them twice, so it is never safe.
        return Outcome("terminal", "partial bulk create: some items were created, so it is not retried")
    code, text = _status_code(result), str(result.get("error") or "")
    if not text and (code is None or code < 400):
        return OK
    if code in TRANSIENT_CODES or (code is not None and code >= 500) \
            or any(w in text.lower() for w in TRANSIENT_WORDS):
        return Outcome("transient", text or f"status {code}")
    return Outcome("terminal", text or f"status {code}")

"""Outbound CloudEvents webhooks: signed body, idempotency header, best-effort delivery.

The sink raises when delivery fails; `emit_safely` (used by the planner and RunManager) logs that and
carries on, so a dead receiver can never block or roll back a database write."""
import hashlib
import hmac
import json
import urllib.request
from collections.abc import Callable

Poster = Callable[[str, bytes, dict[str, str]], None]
SIGNATURE_HEADER = "X-Signature-256"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, signature: str) -> bool:
    """What a receiver runs: constant-time compare of the header against its own HMAC."""
    return hmac.compare_digest(sign(secret, body), signature)


def urllib_post(url: str, body: bytes, headers: dict[str, str]) -> None:
    # Simplification: a blocking call with a short timeout and no retries; queue deliveries and retry in production.
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=5):
        pass


class WebhookSink:
    def __init__(self, url: str, secret: str, post: Poster = urllib_post) -> None:
        self.url, self.secret, self.post = url, secret, post

    def emit(self, event: dict) -> None:
        body = json.dumps(event, sort_keys=True).encode()
        self.post(self.url, body, {
            "Content-Type": "application/cloudevents+json",
            SIGNATURE_HEADER: sign(self.secret, body),
            "Idempotency-Key": event["idempotencykey"],  # a redelivery carries the same key
        })

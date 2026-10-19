import json

from conftest import make_plan

from autonomous_agent.runs import RunManager
from autonomous_agent.webhooks import SIGNATURE_HEADER, WebhookSink, sign, verify


class FakePoster:
    def __init__(self):
        self.sent: list[tuple[str, bytes, dict]] = []

    def __call__(self, url, body, headers):
        self.sent.append((url, body, headers))


def test_body_is_signed_with_the_shared_secret_and_carries_an_idempotency_key(store):
    poster = FakePoster()
    runs = RunManager(store, WebhookSink("https://hooks.example/acme", "s3cret", poster))
    run_id = runs.create(make_plan(store), "t1", {})
    (url, body, headers), = poster.sent
    event = json.loads(body)
    assert url == "https://hooks.example/acme" and event["type"] == "acme.run.pending"
    assert headers["Idempotency-Key"] == event["idempotencykey"] == f"run.pending:{run_id}:0"
    assert headers[SIGNATURE_HEADER] == sign("s3cret", body)
    assert verify("s3cret", body, headers[SIGNATURE_HEADER])
    assert not verify("wrong", body, headers[SIGNATURE_HEADER])


def test_redelivery_of_one_event_has_the_same_idempotency_key():
    poster = FakePoster()
    sink = WebhookSink("https://x", "k", poster)
    event = {"type": "t", "idempotencykey": "abc", "data": {}}
    sink.emit(event)
    sink.emit(event)
    assert poster.sent[0][2]["Idempotency-Key"] == poster.sent[1][2]["Idempotency-Key"] == "abc"


def test_delivery_failure_is_logged_and_never_blocks_the_database_write(store, caplog):
    def down(url, body, headers):
        raise ConnectionError("receiver is down")

    runs = RunManager(store, WebhookSink("https://x", "k", down))
    run_id = runs.create(make_plan(store), "t1", {})
    runs.start(run_id)
    assert store.get_run(run_id)["status"] == "running"  # the write stood
    assert "event sink failed" in caplog.text

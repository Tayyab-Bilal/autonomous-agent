import pytest

from autonomous_agent.failures import classify_result


@pytest.mark.parametrize("result", [{"ok": True}, {"status": 200, "id": 4}, {"tasks": []}, "text", None])
def test_success_results_are_ok(result):
    assert classify_result(result).kind == "ok"


def test_http_codes_read_as_integers():
    # "task 1404" is not a 404 and "item 1500" is not a 500: neither may be read as a status code
    assert classify_result({"error": "item 1500 not found"}).kind == "terminal"
    assert classify_result({"error": "item 1409 is archived"}).kind == "terminal"
    assert classify_result({"error": "upstream said 503"}).kind == "transient"
    assert classify_result({"error": "HTTP 409"}).kind == "transient"


def test_write_conflict_is_transient():
    assert classify_result({"status": 409}).kind == "transient"
    assert classify_result({"error": "optimistic concurrency check failed"}).kind == "transient"


def test_rate_limit_and_server_errors_are_transient_and_other_4xx_terminal():
    assert classify_result({"status_code": 429}).kind == "transient"
    assert classify_result({"status": 502}).kind == "transient"
    assert classify_result({"status": 404, "error": "not found"}).kind == "terminal"
    assert classify_result({"error": "permission denied"}).kind == "terminal"


def test_partial_bulk_create_is_never_retried():
    # even though 409 alone would be transient
    r = classify_result({"created": ["a", "b"], "failed": ["c"], "status": 409})
    assert r.kind == "terminal" and "partial" in r.why
    assert classify_result({"partial": True}).kind == "terminal"

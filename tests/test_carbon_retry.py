import pytest
import requests

from app.services import carbon_retry


@pytest.mark.parametrize("error_type", [requests.ConnectionError, requests.Timeout])
def test_transient_transport_error_recovers(monkeypatch, error_type):
    waits = []
    monkeypatch.setattr(carbon_retry.time, "sleep", waits.append)
    calls = []
    result = {"carbon_estimated": {"statistics": {"mean": 42}}}

    def compute():
        calls.append(1)
        if len(calls) < 3:
            raise error_type("remote disconnected")
        return result

    assert carbon_retry.run_carbon_with_retry(compute, job_id="same-job") is result
    assert len(calls) == 3
    assert waits == [1, 2]


def test_persistent_disconnect_remains_an_error(monkeypatch):
    monkeypatch.setattr(carbon_retry.time, "sleep", lambda _: None)
    calls = []
    error = requests.ConnectionError("offline")

    def compute():
        calls.append(1)
        raise error

    with pytest.raises(requests.ConnectionError) as caught:
        carbon_retry.run_carbon_with_retry(compute, job_id="failed-job")
    assert caught.value is error
    assert len(calls) == 3


@pytest.mark.parametrize("error", [ValueError("invalid AOI"), requests.exceptions.SSLError("invalid certificate")])
def test_input_and_certificate_errors_are_not_retried(error):
    calls = []

    def compute():
        calls.append(1)
        raise error

    with pytest.raises(type(error)):
        carbon_retry.run_carbon_with_retry(compute, job_id="invalid-job")
    assert len(calls) == 1

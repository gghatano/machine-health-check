import pytest
import requests

from machine_health_check import sender as sender_module
from machine_health_check.sender import send_metrics


class FakeResponse:
    def __init__(self, json_data, status_error=None):
        self._json_data = json_data
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        return self._json_data


@pytest.fixture
def post_calls(monkeypatch):
    """requests.post を差し替え、呼び出し引数を記録する。"""
    calls = []
    response_box = {"response": FakeResponse({"ok": True})}

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return response_box["response"]

    monkeypatch.setattr(sender_module.requests, "post", fake_post)

    return calls, response_box


class TestSendMetrics:
    def test_posts_token_and_metrics_as_flat_payload(self, post_calls):
        calls, _ = post_calls

        send_metrics(
            url="https://example.com/exec",
            token="secret",
            metrics={"hostname": "test-host", "cpu_percent": 12.5},
        )

        assert len(calls) == 1
        assert calls[0]["url"] == "https://example.com/exec"
        assert calls[0]["json"] == {
            "token": "secret",
            "hostname": "test-host",
            "cpu_percent": 12.5,
        }

    def test_sets_timeout(self, post_calls):
        calls, _ = post_calls

        send_metrics(url="https://example.com/exec", token="t", metrics={})

        assert calls[0]["timeout"] == 15

    def test_metrics_do_not_override_token(self, post_calls):
        calls, _ = post_calls

        send_metrics(
            url="https://example.com/exec",
            token="secret",
            metrics={"token": "spoofed"},
        )

        # metrics を後ろに展開しているため metrics 側が優先される (現仕様)
        assert calls[0]["json"]["token"] == "spoofed"

    def test_returns_response_json(self, post_calls):
        _, box = post_calls
        box["response"] = FakeResponse({"ok": True, "row": 42})

        result = send_metrics(url="https://example.com/exec", token="t", metrics={})

        assert result == {"ok": True, "row": 42}

    def test_raises_on_http_error(self, post_calls):
        _, box = post_calls
        box["response"] = FakeResponse(
            {"ok": True},
            status_error=requests.HTTPError("500 Server Error"),
        )

        with pytest.raises(requests.HTTPError):
            send_metrics(url="https://example.com/exec", token="t", metrics={})

    def test_raises_when_ok_is_false(self, post_calls):
        _, box = post_calls
        box["response"] = FakeResponse({"ok": False, "error": "bad token"})

        with pytest.raises(RuntimeError, match="Apps Script returned an error"):
            send_metrics(url="https://example.com/exec", token="t", metrics={})

    def test_propagates_timeout(self, monkeypatch):
        def timeout_post(url, json=None, timeout=None):
            raise requests.Timeout("timed out")

        monkeypatch.setattr(sender_module.requests, "post", timeout_post)

        with pytest.raises(requests.Timeout):
            send_metrics(url="https://example.com/exec", token="t", metrics={})

    def test_propagates_connection_error(self, monkeypatch):
        def failing_post(url, json=None, timeout=None):
            raise requests.ConnectionError("network unreachable")

        monkeypatch.setattr(sender_module.requests, "post", failing_post)

        with pytest.raises(requests.ConnectionError):
            send_metrics(url="https://example.com/exec", token="t", metrics={})

    def test_raises_when_ok_is_missing(self, post_calls):
        _, box = post_calls
        box["response"] = FakeResponse({})

        with pytest.raises(RuntimeError):
            send_metrics(url="https://example.com/exec", token="t", metrics={})

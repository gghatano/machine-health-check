import pytest
import requests

from machine_health_check import sender as sender_module
from machine_health_check.sender import ConfigurationError, NotFoundError, send_metrics


class FakeResponse:
    def __init__(self, json_data=None, status_error=None, status_code=200):
        self._json_data = json_data if json_data is not None else {"ok": True}
        self._status_error = status_error
        self.status_code = status_code

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        return self._json_data


def no_sleep(_seconds):
    """リトライ待ちで実際に眠らないようにする。"""


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


def send(**overrides):
    kwargs = {
        "url": "https://example.com/exec",
        "token": "t",
        "metrics": {},
        "sleep": no_sleep,
    }
    kwargs.update(overrides)
    return send_metrics(**kwargs)


class TestSendMetrics:
    def test_posts_token_and_metrics_as_flat_payload(self, post_calls):
        calls, _ = post_calls

        send(token="secret", metrics={"hostname": "test-host", "cpu_percent": 12.5})

        assert len(calls) == 1
        assert calls[0]["url"] == "https://example.com/exec"
        assert calls[0]["json"] == {
            "token": "secret",
            "hostname": "test-host",
            "cpu_percent": 12.5,
        }

    def test_default_timeout_allows_for_cold_starts(self, post_calls):
        """成功した送信でも実測23秒かかることがあるため、余裕を持たせている。"""
        calls, _ = post_calls

        send()

        assert calls[0]["timeout"] == 45

    def test_timeout_can_be_overridden(self, post_calls):
        calls, _ = post_calls

        send(timeout=10)

        assert calls[0]["timeout"] == 10

    def test_metrics_do_not_override_token(self, post_calls):
        calls, _ = post_calls

        send(token="secret", metrics={"token": "spoofed"})

        # metrics を後ろに展開しているため metrics 側が優先される (現仕様)
        assert calls[0]["json"]["token"] == "spoofed"

    def test_returns_response_json(self, post_calls):
        _, box = post_calls
        box["response"] = FakeResponse({"ok": True, "row": 42})

        assert send() == {"ok": True, "row": 42}


class TestRetries:
    def test_retries_a_timeout_and_succeeds(self, monkeypatch):
        """Apps Script のコールドスタートで散発的にタイムアウトするため吸収する。"""
        outcomes = [requests.ReadTimeout("read timed out"), FakeResponse({"ok": True})]

        def fake_post(url, json=None, timeout=None):
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        assert send() == {"ok": True}
        assert outcomes == []

    def test_retries_a_connection_error(self, monkeypatch):
        outcomes = [
            requests.ConnectionError("name resolution failed"),
            requests.ConnectionError("name resolution failed"),
            FakeResponse({"ok": True}),
        ]

        def fake_post(url, json=None, timeout=None):
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        assert send() == {"ok": True}

    def test_gives_up_after_the_configured_retries(self, monkeypatch):
        attempts = []

        def fake_post(url, json=None, timeout=None):
            attempts.append(1)
            raise requests.ReadTimeout("read timed out")

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        with pytest.raises(RuntimeError, match="3回失敗"):
            send(retries=3)

        assert len(attempts) == 3

    def test_retries_server_errors(self, monkeypatch):
        """5xx は Apps Script 側の一時的な不調なのでリトライする。"""
        attempts = []

        def fake_post(url, json=None, timeout=None):
            attempts.append(1)
            return FakeResponse(
                status_code=500,
                status_error=requests.HTTPError("500 Server Error"),
            )

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        with pytest.raises(RuntimeError):
            send(retries=2)

        assert len(attempts) == 2

    def test_waits_between_attempts(self, monkeypatch):
        waits = []

        def fake_post(url, json=None, timeout=None):
            raise requests.ReadTimeout("read timed out")

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        with pytest.raises(RuntimeError):
            send(retries=3, retry_wait=5, sleep=waits.append)

        assert waits == [5, 10]


class TestNotFound:
    def test_retries_404_and_succeeds(self, monkeypatch):
        """Apps Script は正常なデプロイに対しても断続的に404を返す。

        実測: 同じURLで 17:00 成功 → 17:30 が404 → 18:00 成功。
        """
        outcomes = [FakeResponse(status_code=404), FakeResponse({"ok": True})]

        def fake_post(url, json=None, timeout=None):
            return outcomes.pop(0)

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        assert send() == {"ok": True}
        assert outcomes == []

    def test_gives_up_after_retrying_404(self, monkeypatch):
        attempts = []

        def fake_post(url, json=None, timeout=None):
            attempts.append(1)
            return FakeResponse(status_code=404)

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        with pytest.raises(RuntimeError) as error:
            send(retries=3)

        assert len(attempts) == 3
        # 本当にURLが古いケースの切り分けができるようヒントを添える
        assert "GOOGLE_SCRIPT_URL" in str(error.value)

    def test_404_is_not_a_configuration_error(self):
        assert not issubclass(NotFoundError, ConfigurationError)


class TestConfigurationErrors:
    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_does_not_retry_other_4xx(self, monkeypatch, status):
        """権限やデプロイ設定の問題は、リトライしても直らない。"""
        attempts = []

        def fake_post(url, json=None, timeout=None):
            attempts.append(1)
            return FakeResponse(status_code=status)

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        with pytest.raises(ConfigurationError, match=str(status)):
            send()

        assert len(attempts) == 1

    def test_does_not_retry_when_ok_is_false(self, monkeypatch):
        """トークン不一致など、送信内容側の問題もリトライしない。"""
        attempts = []

        def fake_post(url, json=None, timeout=None):
            attempts.append(1)
            return FakeResponse({"ok": False, "error": "bad token"})

        monkeypatch.setattr(sender_module.requests, "post", fake_post)

        with pytest.raises(ConfigurationError, match="Apps Script returned an error"):
            send()

        assert len(attempts) == 1

    def test_missing_ok_is_treated_as_an_error(self, post_calls):
        _, box = post_calls
        box["response"] = FakeResponse({})

        with pytest.raises(ConfigurationError):
            send()

    def test_configuration_error_is_a_runtime_error(self):
        assert issubclass(ConfigurationError, RuntimeError)

import socket
import subprocess

import pytest
import requests

from machine_health_check import notify_failure as notify_module
from machine_health_check.notify_failure import (
    build_message,
    last_error_line,
    main,
    recent_journal,
    send_discord,
)


JOURNAL = """\
Starting machine-health-check.service - Machine Health Check.
送信に失敗しました（1/3）: ReadTimeout('read timed out')
requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='script.google.com', port=443): Read timed out.
machine-health-check.service: Failed with result 'exit-code'.
"""


class FakeCompleted:
    def __init__(self, stdout=""):
        self.stdout = stdout


class FakeResponse:
    def __init__(self, status_error=None):
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error


class TestRecentJournal:
    def test_asks_journalctl_for_the_unit(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return FakeCompleted(stdout=JOURNAL)

        text = recent_journal("machine-health-check.service", lines=20, run=fake_run)

        assert "Read timed out" in text
        assert calls[0][:3] == ["journalctl", "-u", "machine-health-check.service"]
        assert "20" in calls[0]

    def test_missing_journalctl_is_not_fatal(self):
        def fake_run(args, **kwargs):
            raise FileNotFoundError("journalctl")

        assert recent_journal("unit", run=fake_run) == ""

    def test_timeout_is_not_fatal(self):
        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="journalctl", timeout=15)

        assert recent_journal("unit", run=fake_run) == ""


class TestLastErrorLine:
    def test_picks_the_last_error_looking_line(self):
        assert "Failed with result" in last_error_line(JOURNAL)

    def test_returns_empty_when_nothing_matches(self):
        assert last_error_line("Starting service.\nFinished service.\n") == ""

    def test_truncates_very_long_lines(self):
        assert len(last_error_line("Error " + "x" * 900)) == 400


class TestBuildMessage:
    def test_contains_host_unit_and_detail(self):
        message = build_message(
            "machine-health-check.service",
            "test-host",
            "requests.exceptions.ReadTimeout",
            dashboard_url="https://d",
        )

        assert "送信が失敗しました" in message
        assert "test-host" in message
        assert "machine-health-check.service" in message
        assert "ReadTimeout" in message
        assert "https://d" in message

    def test_detail_is_optional(self):
        message = build_message("unit", "test-host", "")

        assert "Detail:" not in message


class TestSendDiscord:
    def test_posts_content_as_json(self, monkeypatch):
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append({"url": url, "json": json})
            return FakeResponse()

        monkeypatch.setattr(notify_module.requests, "post", fake_post)

        send_discord("https://discord.example/webhook", "本文")

        assert calls == [
            {"url": "https://discord.example/webhook", "json": {"content": "本文"}}
        ]

    def test_raises_on_http_error(self, monkeypatch):
        monkeypatch.setattr(
            notify_module.requests,
            "post",
            lambda url, json=None, timeout=None: FakeResponse(
                status_error=requests.HTTPError("429 Too Many Requests")
            ),
        )

        with pytest.raises(requests.HTTPError):
            send_discord("https://discord.example/webhook", "本文")


@pytest.fixture
def isolated_notify(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(notify_module, "load_dotenv", lambda path: None)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(notify_module, "recent_journal", lambda *a, **k: JOURNAL)
    monkeypatch.setattr(notify_module.socket, "gethostname", lambda: "test-host")

    posts = []
    monkeypatch.setattr(notify_module, "send_discord", lambda url, message: posts.append({"url": url, "message": message}))
    return posts


class TestMain:
    def test_notifies_with_the_unit_from_argv(self, isolated_notify, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
        monkeypatch.setattr("sys.argv", ["notify_failure", "some-unit.service"])

        main()

        assert len(isolated_notify) == 1
        assert "some-unit.service" in isolated_notify[0]["message"]

    def test_missing_webhook_is_not_fatal(self, isolated_notify, monkeypatch, capsys):
        """通知先が無いだけで systemd の OnFailure が失敗しないようにする。"""
        monkeypatch.setattr("sys.argv", ["notify_failure"])

        main()

        assert isolated_notify == []
        assert "未設定" in capsys.readouterr().err

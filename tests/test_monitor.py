from datetime import datetime, timedelta, timezone

import pytest
import requests

from machine_health_check import monitor as monitor_module
from machine_health_check.monitor import (
    ALERT,
    NONE,
    RECOVERED,
    REPEAT,
    Config,
    Record,
    Status,
    build_alert_message,
    build_fetch_failure_message,
    build_recovery_message,
    config_from_env,
    decide,
    evaluate,
    fetch_record,
    load_state,
    next_state,
    notify,
    parse_record,
    run,
    save_state,
    send_discord,
)


JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 8, 16, 0, 0, 0, tzinfo=timezone.utc)


def payload(timestamp: str = "2026-08-16T08:30:00+09:00", hostname: str = "host-a") -> dict:
    return {"ok": True, "record": {"hostname": hostname, "timestamp": timestamp}}


class FakeResponse:
    def __init__(self, json_data=None, status_error=None):
        self._json_data = json_data if json_data is not None else {}
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        return self._json_data


def no_sleep(_seconds):
    """リトライ待ちで実際に眠らないようにする。"""


class TestParseRecord:
    def test_reads_hostname_and_timestamp(self):
        record = parse_record(payload())

        assert record.hostname == "host-a"
        assert record.timestamp == datetime(2026, 8, 16, 8, 30, tzinfo=JST)

    def test_rejects_error_payload(self):
        with pytest.raises(RuntimeError):
            parse_record({"ok": False, "error": "bad token"})

    def test_rejects_timestamp_without_timezone(self):
        with pytest.raises(ValueError):
            parse_record(payload(timestamp="2026-08-16T08:30:00"))


class TestFetchRecord:
    def test_returns_record_on_first_try(self, monkeypatch):
        calls = []

        def fake_get(url, timeout=None):
            calls.append(timeout)
            return FakeResponse(payload())

        monkeypatch.setattr(monitor_module.requests, "get", fake_get)

        record = fetch_record(Config(script_url="https://example.com/exec"), sleep=no_sleep)

        assert record.hostname == "host-a"
        assert calls == [45]

    def test_retries_a_timeout_and_succeeds(self, monkeypatch):
        """Apps Script のコールドスタートで散発的にタイムアウトするため、リトライで吸収する。"""
        responses = [
            requests.ReadTimeout("read timed out"),
            FakeResponse(payload()),
        ]

        def fake_get(url, timeout=None):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(monitor_module.requests, "get", fake_get)

        record = fetch_record(Config(script_url="https://example.com/exec"), sleep=no_sleep)

        assert record.hostname == "host-a"
        assert responses == []

    def test_gives_up_after_the_configured_retries(self, monkeypatch):
        attempts = []

        def fake_get(url, timeout=None):
            attempts.append(1)
            raise requests.ReadTimeout("read timed out")

        monkeypatch.setattr(monitor_module.requests, "get", fake_get)

        with pytest.raises(RuntimeError):
            fetch_record(
                Config(script_url="https://example.com/exec", retries=3),
                sleep=no_sleep,
            )

        assert len(attempts) == 3

    def test_retries_on_http_error(self, monkeypatch):
        error = requests.HTTPError("500 Server Error")
        attempts = []

        def fake_get(url, timeout=None):
            attempts.append(1)
            return FakeResponse(status_error=error)

        monkeypatch.setattr(monitor_module.requests, "get", fake_get)

        with pytest.raises(RuntimeError):
            fetch_record(Config(script_url="https://example.com/exec", retries=2), sleep=no_sleep)

        assert len(attempts) == 2


def record_at(minutes_ago: float) -> Record:
    return Record(hostname="host-a", timestamp=NOW - timedelta(minutes=minutes_ago))


class TestEvaluate:
    def test_recent_record_is_healthy(self):
        status = evaluate(record_at(20), NOW, timedelta(minutes=60))

        assert status.stale is False
        assert status.elapsed == timedelta(minutes=20)

    def test_old_record_is_stale(self):
        assert evaluate(record_at(75), NOW, timedelta(minutes=60)).stale is True

    def test_exactly_at_threshold_is_stale(self):
        assert evaluate(record_at(60), NOW, timedelta(minutes=60)).stale is True


def stale_status(minutes_ago: float = 120) -> Status:
    return Status(
        record=record_at(minutes_ago),
        elapsed=timedelta(minutes=minutes_ago),
        stale=True,
    )


def healthy_status() -> Status:
    return Status(record=record_at(10), elapsed=timedelta(minutes=10), stale=False)


class TestDecide:
    def test_first_detection_alerts(self):
        assert decide(stale_status(), {}, NOW, timedelta(hours=6)) == ALERT

    def test_same_outage_is_not_notified_again_right_away(self):
        state = {"alerted": True, "last_alert_at": (NOW - timedelta(hours=1)).isoformat()}

        assert decide(stale_status(), state, NOW, timedelta(hours=6)) == NONE

    def test_long_outage_is_notified_again_after_the_interval(self):
        state = {"alerted": True, "last_alert_at": (NOW - timedelta(hours=7)).isoformat()}

        assert decide(stale_status(), state, NOW, timedelta(hours=6)) == REPEAT

    def test_recovery_is_notified_once(self):
        state = {"alerted": True, "last_alert_at": NOW.isoformat()}

        assert decide(healthy_status(), state, NOW, timedelta(hours=6)) == RECOVERED

    def test_healthy_without_previous_alert_says_nothing(self):
        assert decide(healthy_status(), {}, NOW, timedelta(hours=6)) == NONE


class TestState:
    def test_missing_file_is_empty_state(self, tmp_path):
        assert load_state(tmp_path / "absent.json") == {}

    def test_broken_file_is_empty_state(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ broken", encoding="utf-8")

        assert load_state(path) == {}

    def test_round_trip(self, tmp_path):
        path = tmp_path / "nested" / "state.json"

        save_state(path, {"alerted": True})

        assert load_state(path) == {"alerted": True}

    def test_alert_records_the_stopped_timestamp(self):
        status = stale_status()

        state = next_state(ALERT, status, {}, NOW)

        assert state["alerted"] is True
        assert state["last_alert_at"] == NOW.isoformat(timespec="seconds")
        assert state["alert_latest"] == status.record.timestamp.isoformat(timespec="seconds")

    def test_repeat_keeps_the_original_stopped_timestamp(self):
        previous = {"alerted": True, "alert_latest": "2026-08-16T05:00:00+09:00"}

        state = next_state(REPEAT, stale_status(), previous, NOW)

        assert state["alert_latest"] == "2026-08-16T05:00:00+09:00"

    def test_recovery_clears_the_alert_flag(self):
        state = next_state(RECOVERED, healthy_status(), {"alerted": True}, NOW)

        assert state["alerted"] is False

    def test_any_success_resets_the_fetch_failure_counter(self):
        previous = {"fetch_failures": 2, "fetch_failure_alerted": True}

        state = next_state(NONE, healthy_status(), previous, NOW)

        assert state["fetch_failures"] == 0
        assert state["fetch_failure_alerted"] is False


class TestMessages:
    def test_alert_contains_host_elapsed_and_threshold(self):
        message = build_alert_message(ALERT, stale_status(), Config(dashboard_url="https://d"))

        assert "alert" in message
        assert "host-a" in message
        assert "120.0 minutes" in message
        assert "Threshold: 60.0 minutes" in message
        assert "https://d" in message

    def test_repeat_is_marked_as_still_down(self):
        message = build_alert_message(REPEAT, stale_status(), Config())

        assert "still down" in message

    def test_recovery_shows_when_it_stopped(self):
        state = {"alert_latest": "2026-08-16T05:00:00+09:00"}

        message = build_recovery_message(healthy_status(), state, Config())

        assert "recovered" in message
        assert "Stopped since: 2026-08-16T05:00:00+09:00" in message

    def test_fetch_failure_names_the_error(self):
        message = build_fetch_failure_message(3, requests.ReadTimeout("boom"), Config())

        assert "3回連続" in message
        assert "ReadTimeout" in message


@pytest.fixture
def discord_calls(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return FakeResponse()

    monkeypatch.setattr(monitor_module.requests, "post", fake_post)
    return calls


class TestSendDiscord:
    def test_posts_content_as_json(self, discord_calls):
        send_discord("https://discord.example/webhook", "本文")

        assert discord_calls == [
            {"url": "https://discord.example/webhook", "json": {"content": "本文"}}
        ]

    def test_raises_on_http_error(self, monkeypatch):
        error = requests.HTTPError("429 Too Many Requests")
        monkeypatch.setattr(
            monitor_module.requests,
            "post",
            lambda url, json=None, timeout=None: FakeResponse(status_error=error),
        )

        with pytest.raises(requests.HTTPError):
            send_discord("https://discord.example/webhook", "本文")


class TestNotify:
    def test_dry_run_does_not_send(self, discord_calls):
        sent = notify(Config(webhook_url="https://discord.example/webhook", dry_run=True), "本文")

        assert sent is False
        assert discord_calls == []

    def test_missing_webhook_does_not_send(self, discord_calls):
        assert notify(Config(webhook_url=None), "本文") is False
        assert discord_calls == []

    def test_sends_when_configured(self, discord_calls):
        assert notify(Config(webhook_url="https://discord.example/webhook"), "本文") is True
        assert len(discord_calls) == 1


def stub_api(monkeypatch, minutes_ago: float | None = None, error: Exception | None = None):
    def fake_get(url, timeout=None):
        if error is not None:
            raise error
        stamp = (NOW - timedelta(minutes=minutes_ago)).astimezone(JST).isoformat()
        return FakeResponse(payload(timestamp=stamp))

    monkeypatch.setattr(monitor_module.requests, "get", fake_get)


def config_for(tmp_path, **overrides) -> Config:
    defaults = {
        "script_url": "https://example.com/exec",
        "webhook_url": "https://discord.example/webhook",
        "state_file": tmp_path / "state.json",
    }
    return Config(**{**defaults, **overrides})


class TestRun:
    def test_notifies_and_records_state_when_metrics_stop(self, monkeypatch, tmp_path, discord_calls):
        stub_api(monkeypatch, minutes_ago=120)
        config = config_for(tmp_path)

        assert run(config, now=NOW, sleep=no_sleep) == 0
        assert len(discord_calls) == 1
        assert "alert" in discord_calls[0]["json"]["content"]
        assert load_state(config.state_file)["alerted"] is True

    def test_stays_quiet_while_metrics_keep_arriving(self, monkeypatch, tmp_path, discord_calls):
        stub_api(monkeypatch, minutes_ago=20)

        assert run(config_for(tmp_path), now=NOW, sleep=no_sleep) == 0
        assert discord_calls == []

    def test_does_not_repeat_the_same_alert(self, monkeypatch, tmp_path, discord_calls):
        stub_api(monkeypatch, minutes_ago=120)
        config = config_for(tmp_path)
        save_state(
            config.state_file,
            {"alerted": True, "last_alert_at": (NOW - timedelta(hours=1)).isoformat()},
        )

        run(config, now=NOW, sleep=no_sleep)

        assert discord_calls == []

    def test_notifies_recovery(self, monkeypatch, tmp_path, discord_calls):
        stub_api(monkeypatch, minutes_ago=10)
        config = config_for(tmp_path)
        save_state(config.state_file, {"alerted": True, "last_alert_at": NOW.isoformat()})

        run(config, now=NOW, sleep=no_sleep)

        assert len(discord_calls) == 1
        assert "recovered" in discord_calls[0]["json"]["content"]
        assert load_state(config.state_file)["alerted"] is False

    def test_dry_run_neither_sends_nor_advances_state(self, monkeypatch, tmp_path, discord_calls):
        stub_api(monkeypatch, minutes_ago=120)
        config = config_for(tmp_path, dry_run=True)

        run(config, now=NOW, sleep=no_sleep)

        assert discord_calls == []
        assert load_state(config.state_file) == {}

    def test_missing_webhook_does_not_advance_state(self, monkeypatch, tmp_path, discord_calls):
        """Webhook 未設定のまま状態を進めると、設定後に通知できなくなる。"""
        stub_api(monkeypatch, minutes_ago=120)
        config = config_for(tmp_path, webhook_url=None)

        run(config, now=NOW, sleep=no_sleep)

        assert discord_calls == []
        assert load_state(config.state_file) == {}

    def test_fetch_failure_fails_the_run_without_notifying_at_first(
        self, monkeypatch, tmp_path, discord_calls
    ):
        """1回のタイムアウトでは通知しない（サーバ停止と紛らわしいため）。"""
        stub_api(monkeypatch, error=requests.ReadTimeout("read timed out"))
        config = config_for(tmp_path)

        assert run(config, now=NOW, sleep=no_sleep) == 1
        assert discord_calls == []
        assert load_state(config.state_file)["fetch_failures"] == 1

    def test_repeated_fetch_failures_are_notified_once(self, monkeypatch, tmp_path, discord_calls):
        stub_api(monkeypatch, error=requests.ReadTimeout("read timed out"))
        config = config_for(tmp_path, fetch_failure_alerts=3)

        for _ in range(4):
            run(config, now=NOW, sleep=no_sleep)

        assert len(discord_calls) == 1
        assert "monitor error" in discord_calls[0]["json"]["content"]
        assert load_state(config.state_file)["fetch_failures"] == 4

    def test_recovering_from_fetch_failures_resets_the_counter(
        self, monkeypatch, tmp_path, discord_calls
    ):
        config = config_for(tmp_path)
        stub_api(monkeypatch, error=requests.ReadTimeout("read timed out"))
        run(config, now=NOW, sleep=no_sleep)

        stub_api(monkeypatch, minutes_ago=10)
        run(config, now=NOW, sleep=no_sleep)

        assert load_state(config.state_file)["fetch_failures"] == 0


class TestConfigFromEnv:
    def test_defaults(self):
        config = config_from_env({"GOOGLE_SCRIPT_URL": "https://example.com/exec"})

        assert config.threshold == timedelta(minutes=60)
        assert config.timeout == 45
        assert config.retries == 3
        assert config.repeat_after == timedelta(hours=6)
        assert config.webhook_url is None
        assert config.dry_run is False

    def test_requires_the_script_url(self):
        with pytest.raises(KeyError):
            config_from_env({})

    def test_reads_overrides(self):
        config = config_from_env(
            {
                "GOOGLE_SCRIPT_URL": "https://example.com/exec",
                "DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
                "THRESHOLD_MINUTES": "90",
                "REQUEST_TIMEOUT_SECONDS": "20",
                "REQUEST_RETRIES": "5",
                "RETRY_WAIT_SECONDS": "1",
                "REPEAT_ALERT_HOURS": "12",
                "FETCH_FAILURE_ALERTS": "2",
                "MONITOR_STATE_FILE": "/tmp/monitor.json",
                "DRY_RUN": "true",
            }
        )

        assert config.threshold == timedelta(minutes=90)
        assert config.timeout == 20
        assert config.retries == 5
        assert config.retry_wait == 1
        assert config.repeat_after == timedelta(hours=12)
        assert config.fetch_failure_alerts == 2
        assert str(config.state_file) == "/tmp/monitor.json"
        assert config.dry_run is True

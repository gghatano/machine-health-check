from datetime import datetime, timedelta, timezone

import pytest
import requests

from machine_health_check import watchdog as watchdog_module
from machine_health_check.watchdog import (
    ALERT,
    NONE,
    RECOVERED,
    REPEAT,
    Config,
    Latest,
    Status,
    build_csv_url,
    build_message,
    config_from_env,
    decide,
    evaluate,
    fetch_csv,
    format_duration,
    load_state,
    next_state,
    parse_latest,
    post_discord,
    run,
    save_state,
)


JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 8, 14, 23, 0, 0, tzinfo=JST)

CSV_HEADER = '"timestamp","hostname","cpu_percent"'


def csv_text(*rows: str) -> str:
    return "\n".join([CSV_HEADER, *rows]) + "\n"


class FakeResponse:
    def __init__(self, text="", status_error=None):
        self.text = text
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error


class TestBuildCsvUrl:
    def test_points_at_the_gviz_csv_endpoint(self):
        url = build_csv_url("SHEET_ID", "metrics")

        assert url.startswith("https://docs.google.com/spreadsheets/d/SHEET_ID/gviz/tq?")
        assert "tqx=out%3Acsv" in url
        assert "sheet=metrics" in url


class TestFetchCsv:
    def test_returns_body_on_success(self, monkeypatch):
        monkeypatch.setattr(
            watchdog_module.requests,
            "get",
            lambda url, timeout=None: FakeResponse(text=csv_text()),
        )

        assert fetch_csv("https://example.com/csv").startswith('"timestamp"')

    def test_raises_on_http_error(self, monkeypatch):
        error = requests.HTTPError("404 Not Found")
        monkeypatch.setattr(
            watchdog_module.requests,
            "get",
            lambda url, timeout=None: FakeResponse(status_error=error),
        )

        with pytest.raises(requests.HTTPError):
            fetch_csv("https://example.com/csv")

    def test_raises_when_html_is_returned(self, monkeypatch):
        """非公開のシートではログインHTMLが返る。CSVとして読めないことを検知する。"""
        monkeypatch.setattr(
            watchdog_module.requests,
            "get",
            lambda url, timeout=None: FakeResponse(text="<!DOCTYPE html><html>..."),
        )

        with pytest.raises(RuntimeError):
            fetch_csv("https://example.com/csv")


class TestParseLatest:
    def test_returns_newest_row(self):
        latest = parse_latest(
            csv_text(
                '"2026-08-14T22:00:00+09:00","host-a","3.2"',
                '"2026-08-14T22:30:00+09:00","host-a","4.1"',
            )
        )

        assert latest.timestamp == datetime(2026, 8, 14, 22, 30, tzinfo=JST)
        assert latest.hostname == "host-a"

    def test_ignores_row_order(self):
        latest = parse_latest(
            csv_text(
                '"2026-08-14T22:30:00+09:00","host-a","4.1"',
                '"2026-08-14T22:00:00+09:00","host-a","3.2"',
            )
        )

        assert latest.timestamp == datetime(2026, 8, 14, 22, 30, tzinfo=JST)

    def test_skips_blank_and_broken_timestamps(self):
        latest = parse_latest(
            csv_text(
                '"","host-a","3.2"',
                '"not-a-timestamp","host-a","3.2"',
                '"2026-08-14T22:00:00+09:00","host-a","3.2"',
            )
        )

        assert latest.timestamp == datetime(2026, 8, 14, 22, 0, tzinfo=JST)

    def test_returns_none_when_there_is_no_row(self):
        assert parse_latest(csv_text()) is None


class TestEvaluate:
    def test_fresh_record_is_not_missing(self):
        latest = Latest(NOW - timedelta(minutes=20), "host-a")

        status = evaluate(latest, NOW, timedelta(minutes=90))

        assert status.missing is False
        assert status.age == timedelta(minutes=20)

    def test_old_record_is_missing(self):
        latest = Latest(NOW - timedelta(minutes=95), "host-a")

        assert evaluate(latest, NOW, timedelta(minutes=90)).missing is True

    def test_exactly_at_threshold_is_not_missing(self):
        latest = Latest(NOW - timedelta(minutes=90), "host-a")

        assert evaluate(latest, NOW, timedelta(minutes=90)).missing is False

    def test_no_record_at_all_is_missing(self):
        status = evaluate(None, NOW, timedelta(minutes=90))

        assert status.missing is True
        assert status.age is None


def missing_status(minutes_old=120):
    return Status(
        latest=Latest(NOW - timedelta(minutes=minutes_old), "host-a"),
        age=timedelta(minutes=minutes_old),
        missing=True,
    )


def healthy_status():
    return Status(
        latest=Latest(NOW - timedelta(minutes=10), "host-a"),
        age=timedelta(minutes=10),
        missing=False,
    )


class TestDecide:
    def test_first_detection_alerts(self):
        assert decide(missing_status(), {}, NOW, timedelta(hours=6)) == ALERT

    def test_same_outage_is_not_notified_again_right_away(self):
        state = {
            "alerted": True,
            "last_alert_at": (NOW - timedelta(hours=1)).isoformat(),
        }

        assert decide(missing_status(), state, NOW, timedelta(hours=6)) == NONE

    def test_long_outage_is_notified_again_after_the_interval(self):
        state = {
            "alerted": True,
            "last_alert_at": (NOW - timedelta(hours=7)).isoformat(),
        }

        assert decide(missing_status(), state, NOW, timedelta(hours=6)) == REPEAT

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
        status = missing_status()

        state = next_state(ALERT, status, {}, NOW)

        assert state["alerted"] is True
        assert state["last_alert_at"] == NOW.isoformat(timespec="seconds")
        assert state["alert_latest"] == status.latest.timestamp.isoformat(timespec="seconds")

    def test_repeat_keeps_the_original_stopped_timestamp(self):
        previous = {"alerted": True, "alert_latest": "2026-08-14T20:00:00+09:00"}

        state = next_state(REPEAT, missing_status(), previous, NOW)

        assert state["alert_latest"] == "2026-08-14T20:00:00+09:00"

    def test_recovery_clears_the_alert_flag(self):
        state = next_state(RECOVERED, healthy_status(), {"alerted": True}, NOW)

        assert state["alerted"] is False


class TestBuildMessage:
    def test_alert_contains_host_age_and_dashboard(self):
        config = Config(dashboard_url="https://example.com/dash")

        message = build_message(ALERT, missing_status(), config, {})

        assert "記録されていません" in message
        assert "host-a" in message
        assert "2時間" in message
        assert "https://example.com/dash" in message

    def test_alert_without_any_record(self):
        status = Status(latest=None, age=None, missing=True)

        message = build_message(ALERT, status, Config(), {})

        assert "1件もありません" in message

    def test_recovery_shows_the_outage_span(self):
        state = {"alert_latest": (NOW - timedelta(hours=3)).isoformat()}

        message = build_message(RECOVERED, healthy_status(), Config(), state)

        assert "再開" in message
        assert "途切れていた期間" in message


class TestFormatDuration:
    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(minutes=45), "45分"),
            (timedelta(minutes=90), "1時間30分"),
            (timedelta(hours=5), "5時間"),
            (timedelta(days=2, hours=3), "2日3時間"),
            (timedelta(days=2), "2日"),
        ],
    )
    def test_reads_naturally_in_japanese(self, delta, expected):
        assert format_duration(delta) == expected


class TestPostDiscord:
    def test_sends_content_as_json(self, monkeypatch):
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append({"url": url, "json": json, "timeout": timeout})
            return FakeResponse()

        monkeypatch.setattr(watchdog_module.requests, "post", fake_post)

        post_discord("https://discord.example/webhook", "本文")

        assert calls == [
            {
                "url": "https://discord.example/webhook",
                "json": {"content": "本文"},
                "timeout": 15,
            }
        ]

    def test_raises_on_http_error(self, monkeypatch):
        error = requests.HTTPError("429 Too Many Requests")
        monkeypatch.setattr(
            watchdog_module.requests,
            "post",
            lambda url, json=None, timeout=None: FakeResponse(status_error=error),
        )

        with pytest.raises(requests.HTTPError):
            post_discord("https://discord.example/webhook", "本文")


@pytest.fixture
def discord_calls(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return FakeResponse()

    monkeypatch.setattr(watchdog_module.requests, "post", fake_post)
    return calls


def stub_sheet(monkeypatch, latest_at: datetime | None):
    rows = [] if latest_at is None else [f'"{latest_at.isoformat()}","host-a","1.0"']
    monkeypatch.setattr(
        watchdog_module.requests,
        "get",
        lambda url, timeout=None: FakeResponse(text=csv_text(*rows)),
    )


class TestRun:
    def test_notifies_and_records_state_when_data_stops(self, monkeypatch, tmp_path, discord_calls):
        stub_sheet(monkeypatch, NOW - timedelta(hours=2))
        state_file = tmp_path / "state.json"
        config = Config(webhook_url="https://discord.example/webhook", state_file=state_file)

        run(config, now=NOW)

        assert len(discord_calls) == 1
        assert "記録されていません" in discord_calls[0]["json"]["content"]
        assert load_state(state_file)["alerted"] is True

    def test_stays_quiet_while_data_keeps_arriving(self, monkeypatch, tmp_path, discord_calls):
        stub_sheet(monkeypatch, NOW - timedelta(minutes=20))
        config = Config(
            webhook_url="https://discord.example/webhook",
            state_file=tmp_path / "state.json",
        )

        run(config, now=NOW)

        assert discord_calls == []

    def test_does_not_repeat_the_same_alert(self, monkeypatch, tmp_path, discord_calls):
        stub_sheet(monkeypatch, NOW - timedelta(hours=2))
        state_file = tmp_path / "state.json"
        save_state(state_file, {"alerted": True, "last_alert_at": (NOW - timedelta(hours=1)).isoformat()})
        config = Config(webhook_url="https://discord.example/webhook", state_file=state_file)

        run(config, now=NOW)

        assert discord_calls == []

    def test_notifies_recovery(self, monkeypatch, tmp_path, discord_calls):
        stub_sheet(monkeypatch, NOW - timedelta(minutes=10))
        state_file = tmp_path / "state.json"
        save_state(state_file, {"alerted": True, "last_alert_at": NOW.isoformat()})
        config = Config(webhook_url="https://discord.example/webhook", state_file=state_file)

        run(config, now=NOW)

        assert len(discord_calls) == 1
        assert "再開" in discord_calls[0]["json"]["content"]
        assert load_state(state_file)["alerted"] is False

    def test_dry_run_neither_sends_nor_advances_state(self, monkeypatch, tmp_path, discord_calls):
        stub_sheet(monkeypatch, NOW - timedelta(hours=2))
        state_file = tmp_path / "state.json"
        config = Config(
            webhook_url="https://discord.example/webhook",
            state_file=state_file,
            dry_run=True,
        )

        run(config, now=NOW)

        assert discord_calls == []
        assert load_state(state_file) == {}

    def test_missing_webhook_does_not_advance_state(self, monkeypatch, tmp_path, discord_calls):
        """Webhook 未設定のまま状態を進めると、設定後に通知できなくなる。"""
        stub_sheet(monkeypatch, NOW - timedelta(hours=2))
        state_file = tmp_path / "state.json"

        run(Config(webhook_url=None, state_file=state_file), now=NOW)

        assert discord_calls == []
        assert load_state(state_file) == {}


class TestConfigFromEnv:
    def test_defaults(self):
        config = config_from_env({})

        assert config.missing_after == timedelta(minutes=90)
        assert config.repeat_after == timedelta(hours=6)
        assert config.webhook_url is None
        assert config.dry_run is False

    def test_reads_overrides(self):
        config = config_from_env(
            {
                "SPREADSHEET_ID": "other-sheet",
                "SHEET_NAME": "other",
                "MISSING_AFTER_MINUTES": "120",
                "REPEAT_ALERT_HOURS": "12",
                "DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
                "WATCHDOG_STATE_FILE": "/tmp/state.json",
                "DRY_RUN": "true",
            }
        )

        assert config.sheet_id == "other-sheet"
        assert config.sheet_name == "other"
        assert config.missing_after == timedelta(minutes=120)
        assert config.repeat_after == timedelta(hours=12)
        assert config.webhook_url == "https://discord.example/webhook"
        assert str(config.state_file) == "/tmp/state.json"
        assert config.dry_run is True

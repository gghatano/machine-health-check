from datetime import datetime, timedelta, timezone

import pytest

from machine_health_check import metrics as metrics_module


FIXED_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone(timedelta(hours=9)))


class FakeDatetime:
    """datetime.now() だけを固定した差し替え用クラス。"""

    @staticmethod
    def now(tz=None):
        if tz is None:
            return FIXED_NOW.replace(tzinfo=None)
        return FIXED_NOW.astimezone(tz)

    @staticmethod
    def fromisoformat(value: str):
        return datetime.fromisoformat(value)


@pytest.fixture
def fixed_now(monkeypatch):
    """collect_metrics() が使う現在時刻を FIXED_NOW に固定する。"""
    monkeypatch.setattr(metrics_module, "datetime", FakeDatetime)
    return FIXED_NOW


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """state.json の書き込み先を一時ディレクトリに逃がす。"""
    path = tmp_path / "state.json"
    monkeypatch.setattr(metrics_module, "STATE_FILE", path)
    return path

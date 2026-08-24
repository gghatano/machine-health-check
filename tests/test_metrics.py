import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from machine_health_check import metrics as metrics_module
from machine_health_check.metrics import (
    bytes_to_gb,
    collect_metrics,
    load_previous_state,
    save_state,
)


class TestBytesToGb:
    def test_1gb(self):
        assert bytes_to_gb(1024**3) == 1.0

    def test_zero(self):
        assert bytes_to_gb(0) == 0.0

    def test_rounds_to_3_decimals(self):
        # 1.5GB + 端数 -> 小数第3位で丸められる
        assert bytes_to_gb(int(1.23456 * 1024**3)) == 1.235


class TestLoadPreviousState:
    def test_returns_none_when_file_missing(self, state_file):
        assert not state_file.exists()
        assert load_previous_state() is None

    def test_returns_parsed_json(self, state_file):
        state = {
            "bytes_recv": 100,
            "bytes_sent": 200,
            "timestamp": "2026-08-14T12:00:00+09:00",
        }
        state_file.write_text(json.dumps(state), encoding="utf-8")

        assert load_previous_state() == state


class TestSaveState:
    def test_writes_all_fields(self, state_file):
        save_state(
            bytes_recv=111,
            bytes_sent=222,
            timestamp="2026-08-14T12:00:00+09:00",
        )

        assert json.loads(state_file.read_text(encoding="utf-8")) == {
            "bytes_recv": 111,
            "bytes_sent": 222,
            "timestamp": "2026-08-14T12:00:00+09:00",
        }

    def test_overwrites_existing_file(self, state_file):
        state_file.write_text("{}", encoding="utf-8")

        save_state(bytes_recv=1, bytes_sent=2, timestamp="t")

        assert load_previous_state() == {
            "bytes_recv": 1,
            "bytes_sent": 2,
            "timestamp": "t",
        }


@pytest.fixture
def fake_psutil(monkeypatch):
    """psutil / os / socket / time をすべて固定値に差し替える。"""
    fake = SimpleNamespace(
        cpu_percent=lambda interval=None: 12.5,
        virtual_memory=lambda: SimpleNamespace(
            used=4 * 1024**3,
            total=16 * 1024**3,
            percent=25.0,
        ),
        swap_memory=lambda: SimpleNamespace(
            used=1 * 1024**3,
            total=2 * 1024**3,
            percent=50.0,
        ),
        disk_usage=lambda path: SimpleNamespace(
            used=100 * 1024**3,
            total=500 * 1024**3,
            percent=20.0,
        ),
        net_io_counters=lambda: SimpleNamespace(
            bytes_recv=2_000_000,
            bytes_sent=1_000_000,
            packets_recv=500,
            packets_sent=400,
            errin=1,
            errout=2,
            dropin=3,
            dropout=4,
        ),
        boot_time=lambda: 1_000_000.0,
    )

    monkeypatch.setattr(metrics_module, "psutil", fake)
    monkeypatch.setattr(metrics_module.os, "getloadavg", lambda: (0.5, 1.0, 1.5))
    monkeypatch.setattr(metrics_module.socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(metrics_module.time, "time", lambda: 1_003_600.0)

    return fake


class TestCollectMetrics:
    def test_static_fields(self, fake_psutil, fixed_now, state_file):
        metrics = collect_metrics()

        assert metrics["hostname"] == "test-host"
        assert metrics["cpu_percent"] == 12.5
        assert metrics["load_1m"] == 0.5
        assert metrics["load_5m"] == 1.0
        assert metrics["load_15m"] == 1.5
        assert metrics["uptime_sec"] == 3600
        assert datetime.fromisoformat(metrics["timestamp"]) == fixed_now

    def test_byte_fields_converted_to_gb(self, fake_psutil, fixed_now, state_file):
        metrics = collect_metrics()

        assert metrics["memory_used_gb"] == 4.0
        assert metrics["memory_total_gb"] == 16.0
        assert metrics["memory_percent"] == 25.0
        assert metrics["swap_used_gb"] == 1.0
        assert metrics["swap_total_gb"] == 2.0
        assert metrics["swap_percent"] == 50.0
        assert metrics["disk_used_gb"] == 100.0
        assert metrics["disk_total_gb"] == 500.0
        assert metrics["disk_percent"] == 20.0

    def test_network_counters_passed_through(self, fake_psutil, fixed_now, state_file):
        metrics = collect_metrics()

        assert metrics["bytes_recv"] == 2_000_000
        assert metrics["bytes_sent"] == 1_000_000
        assert metrics["packets_recv"] == 500
        assert metrics["packets_sent"] == 400
        assert metrics["errors_in"] == 1
        assert metrics["errors_out"] == 2
        assert metrics["drops_in"] == 3
        assert metrics["drops_out"] == 4

    def test_first_run_has_zero_deltas(self, fake_psutil, fixed_now, state_file):
        metrics = collect_metrics()

        assert metrics["recv_bytes_delta"] == 0
        assert metrics["sent_bytes_delta"] == 0
        assert metrics["recv_mbps"] == 0.0
        assert metrics["sent_mbps"] == 0.0

    def test_deltas_and_mbps_from_previous_state(
        self, fake_psutil, fixed_now, state_file
    ):
        previous_time = fixed_now - timedelta(seconds=10)
        save_state(
            bytes_recv=1_000_000,
            bytes_sent=500_000,
            timestamp=previous_time.isoformat(timespec="seconds"),
        )

        metrics = collect_metrics()

        assert metrics["recv_bytes_delta"] == 1_000_000
        assert metrics["sent_bytes_delta"] == 500_000
        # 1_000_000 bytes * 8 / 10s / 1e6 = 0.8 Mbps
        assert metrics["recv_mbps"] == 0.8
        assert metrics["sent_mbps"] == 0.4

    def test_counter_reset_clamps_delta_to_zero(
        self, fake_psutil, fixed_now, state_file
    ):
        # 再起動などでカウンタが巻き戻ったケース
        previous_time = fixed_now - timedelta(seconds=10)
        save_state(
            bytes_recv=9_000_000,
            bytes_sent=9_000_000,
            timestamp=previous_time.isoformat(timespec="seconds"),
        )

        metrics = collect_metrics()

        assert metrics["recv_bytes_delta"] == 0
        assert metrics["sent_bytes_delta"] == 0
        assert metrics["recv_mbps"] == 0.0
        assert metrics["sent_mbps"] == 0.0

    def test_zero_elapsed_time_does_not_divide_by_zero(
        self, fake_psutil, fixed_now, state_file
    ):
        save_state(
            bytes_recv=1_000_000,
            bytes_sent=500_000,
            timestamp=fixed_now.isoformat(timespec="seconds"),
        )

        metrics = collect_metrics()

        assert metrics["recv_bytes_delta"] == 1_000_000
        assert metrics["recv_mbps"] == 0.0
        assert metrics["sent_mbps"] == 0.0

    def test_does_not_save_state_by_itself(self, fake_psutil, fixed_now, state_file):
        """state の保存は送信が成功したあとに main() が行う。

        ここで保存してしまうと、送信に失敗した回の通信量が次回のデルタから
        抜け落ちて、そのまま失われる。
        """
        metrics = collect_metrics()

        assert not state_file.exists()
        assert metrics["bytes_recv"] == 2_000_000
        assert metrics["bytes_sent"] == 1_000_000

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil


STATE_FILE = Path("state.json")


def bytes_to_gb(value: int) -> float:
    return round(value / (1024**3), 3)


def load_previous_state() -> dict | None:
    if not STATE_FILE.exists():
        return None

    with STATE_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(bytes_recv: int, bytes_sent: int, timestamp: str) -> None:
    state = {
        "bytes_recv": bytes_recv,
        "bytes_sent": bytes_sent,
        "timestamp": timestamp,
    }

    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def collect_metrics() -> dict:
    now = datetime.now(timezone.utc).astimezone()
    now_iso = now.isoformat(timespec="seconds")

    cpu_percent = psutil.cpu_percent(interval=1)
    load_1m, load_5m, load_15m = os.getloadavg()

    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    network = psutil.net_io_counters()

    previous = load_previous_state()

    recv_bytes_delta = 0
    sent_bytes_delta = 0
    recv_mbps = 0.0
    sent_mbps = 0.0

    if previous is not None:
        recv_bytes_delta = max(
            0,
            network.bytes_recv - previous["bytes_recv"],
        )
        sent_bytes_delta = max(
            0,
            network.bytes_sent - previous["bytes_sent"],
        )

        previous_time = datetime.fromisoformat(previous["timestamp"])
        elapsed_sec = (now - previous_time).total_seconds()

        if elapsed_sec > 0:
            recv_mbps = round(
                recv_bytes_delta * 8 / elapsed_sec / 1_000_000,
                4,
            )
            sent_mbps = round(
                sent_bytes_delta * 8 / elapsed_sec / 1_000_000,
                4,
            )

    uptime_sec = int(time.time() - psutil.boot_time())

    metrics = {
        "timestamp": now_iso,
        "hostname": socket.gethostname(),
        "uptime_sec": uptime_sec,
        "cpu_percent": cpu_percent,
        "load_1m": round(load_1m, 3),
        "load_5m": round(load_5m, 3),
        "load_15m": round(load_15m, 3),
        "memory_used_gb": bytes_to_gb(memory.used),
        "memory_total_gb": bytes_to_gb(memory.total),
        "memory_percent": memory.percent,
        "swap_used_gb": bytes_to_gb(swap.used),
        "swap_total_gb": bytes_to_gb(swap.total),
        "swap_percent": swap.percent,
        "disk_used_gb": bytes_to_gb(disk.used),
        "disk_total_gb": bytes_to_gb(disk.total),
        "disk_percent": disk.percent,
        "bytes_recv": network.bytes_recv,
        "bytes_sent": network.bytes_sent,
        "recv_bytes_delta": recv_bytes_delta,
        "sent_bytes_delta": sent_bytes_delta,
        "recv_mbps": recv_mbps,
        "sent_mbps": sent_mbps,
        "packets_recv": network.packets_recv,
        "packets_sent": network.packets_sent,
        "errors_in": network.errin,
        "errors_out": network.errout,
        "drops_in": network.dropin,
        "drops_out": network.dropout,
    }

    save_state(
        bytes_recv=network.bytes_recv,
        bytes_sent=network.bytes_sent,
        timestamp=now_iso,
    )

    return metrics

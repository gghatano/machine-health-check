"""systemd の OnFailure= から呼ばれ、メトリクス送信の失敗を Discord へ通知する。

しきい値(60分)に届かない単発の失敗は journal にしか残らず、これまで観測されて
いなかった。1回の失敗でも気づけるようにするための通知。

送信が成功している間は呼ばれないので、通知は失敗した回だけになる。
"""

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv


DEFAULT_UNIT = "machine-health-check.service"
DEFAULT_JOURNAL_LINES = 20
DASHBOARD_URL = "https://gghatano.github.io/machine-health-check/"

# journal から失敗の手がかりになる行を拾うための目印
ERROR_PATTERN = re.compile(
    r"Error|Exception|Timeout|timed out|Traceback|失敗しました|Failed"
)


def recent_journal(unit: str, lines: int = DEFAULT_JOURNAL_LINES, run=subprocess.run) -> str:
    try:
        completed = run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    return (completed.stdout or "").strip()


def last_error_line(journal_text: str) -> str:
    for line in reversed(journal_text.splitlines()):
        if ERROR_PATTERN.search(line):
            return line.strip()[:400]

    return ""


def build_message(unit: str, hostname: str, detail: str, dashboard_url: str = DASHBOARD_URL) -> str:
    lines = [
        "🛠 メトリクスの送信が失敗しました",
        f"Host: {hostname}",
        f"Unit: {unit}",
    ]

    if detail:
        lines.append(f"Detail: {detail}")

    lines.append("次回の送信（30分後）で復帰しなければ、欠測として通知されます。")
    lines.append(dashboard_url)
    return "\n".join(lines)


def send_discord(webhook_url: str, message: str, timeout: float = 15) -> None:
    response = requests.post(webhook_url, json={"content": message}, timeout=timeout)
    response.raise_for_status()


def main() -> None:
    load_dotenv(Path.cwd() / ".env")

    unit = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_UNIT
    detail = last_error_line(recent_journal(unit))
    message = build_message(unit, socket.gethostname(), detail)

    print(message)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL が未設定のため送信しませんでした。", file=sys.stderr)
        return

    send_discord(webhook_url, message)


if __name__ == "__main__":
    main()

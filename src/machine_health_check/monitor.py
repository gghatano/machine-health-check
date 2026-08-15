import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv


load_dotenv()

GOOGLE_SCRIPT_URL = os.environ["GOOGLE_SCRIPT_URL"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

## 異常判定の頻度
THRESHOLD_MINUTES = 60


def send_discord(message: str) -> None:
    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message},
        timeout=10,
    )
    response.raise_for_status()


def main() -> None:
    response = requests.get(GOOGLE_SCRIPT_URL, timeout=10)
    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Metrics API returned error: {data}")

    record = data["record"]

    hostname = record["hostname"]
    latest_timestamp = datetime.fromisoformat(record["timestamp"])

    if latest_timestamp.tzinfo is None:
        raise ValueError("timestamp must include timezone")

    now = datetime.now(timezone.utc)
    latest_utc = latest_timestamp.astimezone(timezone.utc)

    elapsed_minutes = (now - latest_utc).total_seconds() / 60

    print(f"hostname: {hostname}")
    print(f"latest_timestamp: {latest_timestamp.isoformat()}")
    print(f"elapsed_minutes: {elapsed_minutes:.1f}")

    if elapsed_minutes >= THRESHOLD_MINUTES:
        print("status: stale")

        send_discord(
            "⚠️ machine-health-check alert\n"
            f"Host: {hostname}\n"
            f"Last record: {latest_timestamp.isoformat()}\n"
            f"Elapsed: {elapsed_minutes:.1f} minutes"
        )

        sys.exit(1)

    print("status: healthy")


if __name__ == "__main__":
    main()

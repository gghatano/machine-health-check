"""メトリクスが記録され続けているかを外部から監視し、途切れたら Discord へ通知する。

監視対象の Ubuntu 自身で動かすと、Ubuntu が停止したときに監視も止まってしまう。
そのため GitHub Actions から Apps Script の API を叩き、最新レコードの時刻だけを見る。

Actions の run が赤くなるのは「監視自体が実行できなかったとき」だけにしている。
メトリクスの停止を検知したときは通知を送って正常終了する（通知が信号なので、
run の成否と混ぜない）。
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


# 異常と判定するまでの猶予（30分間隔で記録しているので、既定では2回連続の欠測で通知）
DEFAULT_THRESHOLD_MINUTES = 60
# Apps Script はコールドスタートで十数秒かかることがあり、短いタイムアウトだと散発的に落ちる
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_RETRIES = 3
DEFAULT_RETRY_WAIT_SECONDS = 5
# 同じ障害で鳴り続けないための再通知間隔
DEFAULT_REPEAT_ALERT_HOURS = 6
# 取得失敗が何回続いたら「監視が壊れている」として通知するか
DEFAULT_FETCH_FAILURE_ALERTS = 3

DASHBOARD_URL = "https://gghatano.github.io/machine-health-check/"

ALERT = "alert"
REPEAT = "repeat"
RECOVERED = "recovered"
NONE = "none"


@dataclass
class Config:
    script_url: str = ""
    webhook_url: str | None = None
    threshold: timedelta = timedelta(minutes=DEFAULT_THRESHOLD_MINUTES)
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    retries: int = DEFAULT_RETRIES
    retry_wait: float = DEFAULT_RETRY_WAIT_SECONDS
    repeat_after: timedelta = timedelta(hours=DEFAULT_REPEAT_ALERT_HOURS)
    fetch_failure_alerts: int = DEFAULT_FETCH_FAILURE_ALERTS
    state_file: Path = field(default_factory=lambda: Path(".monitor-state.json"))
    dashboard_url: str = DASHBOARD_URL
    dry_run: bool = False


@dataclass
class Record:
    hostname: str
    timestamp: datetime


@dataclass
class Status:
    record: Record
    elapsed: timedelta
    stale: bool


def parse_record(payload: dict) -> Record:
    if not payload.get("ok"):
        raise RuntimeError(f"Metrics API returned error: {payload}")

    record = payload["record"]
    timestamp = datetime.fromisoformat(record["timestamp"])
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include timezone")

    return Record(hostname=record["hostname"], timestamp=timestamp)


def fetch_record(config: Config, sleep=time.sleep) -> Record:
    """Apps Script から最新レコードを取得する。一時的な失敗はリトライで吸収する。"""
    last_error: Exception | None = None

    for attempt in range(1, config.retries + 1):
        try:
            response = requests.get(config.script_url, timeout=config.timeout)
            response.raise_for_status()
            return parse_record(response.json())
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as error:
            last_error = error
            print(f"取得に失敗しました（{attempt}/{config.retries}）: {error!r}", file=sys.stderr)
            if attempt < config.retries:
                sleep(config.retry_wait * attempt)

    raise RuntimeError(f"メトリクスAPIの取得に{config.retries}回失敗しました: {last_error!r}")


def evaluate(record: Record, now: datetime, threshold: timedelta) -> Status:
    elapsed = now - record.timestamp.astimezone(timezone.utc)
    return Status(record=record, elapsed=elapsed, stale=elapsed >= threshold)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 壊れた状態ファイルは「通知履歴なし」として扱う（最悪、通知が1回重複するだけ）
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def decide(status: Status, state: dict, now: datetime, repeat_after: timedelta) -> str:
    """通知するかどうかを、前回までの通知履歴と突き合わせて決める。"""
    alerted = bool(state.get("alerted"))

    if status.stale:
        if not alerted:
            return ALERT

        last_alert_at = state.get("last_alert_at")
        if not last_alert_at:
            return REPEAT

        try:
            previous = datetime.fromisoformat(last_alert_at)
        except ValueError:
            return REPEAT

        return REPEAT if now - previous >= repeat_after else NONE

    return RECOVERED if alerted else NONE


def next_state(action: str, status: Status, state: dict, now: datetime) -> dict:
    updated = dict(state)
    updated["fetch_failures"] = 0
    updated["fetch_failure_alerted"] = False

    if action in (ALERT, REPEAT):
        updated["alerted"] = True
        updated["last_alert_at"] = now.isoformat(timespec="seconds")
        if action == ALERT:
            updated["alert_latest"] = status.record.timestamp.isoformat(timespec="seconds")
    elif action == RECOVERED:
        updated["alerted"] = False
        updated["last_recovery_at"] = now.isoformat(timespec="seconds")
        updated.pop("alert_latest", None)

    return updated


def format_minutes(elapsed: timedelta) -> str:
    return f"{elapsed.total_seconds() / 60:.1f} minutes"


def build_alert_message(action: str, status: Status, config: Config) -> str:
    head = (
        "⚠️ machine-health-check alert"
        if action == ALERT
        else "⚠️ machine-health-check alert (still down)"
    )

    return "\n".join(
        [
            head,
            f"Host: {status.record.hostname}",
            f"Last record: {status.record.timestamp.isoformat()}",
            f"Elapsed: {format_minutes(status.elapsed)}",
            f"Threshold: {format_minutes(config.threshold)}",
            config.dashboard_url,
        ]
    )


def build_recovery_message(status: Status, state: dict, config: Config) -> str:
    lines = [
        "✅ machine-health-check recovered",
        f"Host: {status.record.hostname}",
        f"Last record: {status.record.timestamp.isoformat()}",
    ]

    stopped_at = state.get("alert_latest")
    if stopped_at:
        lines.append(f"Stopped since: {stopped_at}")

    lines.append(config.dashboard_url)
    return "\n".join(lines)


def build_fetch_failure_message(failures: int, error: Exception, config: Config) -> str:
    return "\n".join(
        [
            "🛠 machine-health-check monitor error",
            f"メトリクスAPIの取得に{failures}回連続で失敗しました。",
            "サーバが落ちているのか、監視側の問題なのかを判別できていません。",
            f"Last error: {error!r}",
            config.dashboard_url,
        ]
    )


def send_discord(webhook_url: str, message: str, timeout: float = 15) -> None:
    response = requests.post(webhook_url, json={"content": message}, timeout=timeout)
    response.raise_for_status()


def notify(config: Config, message: str) -> bool:
    """通知を送れたかどうかを返す。送れていないときは状態を進めない。"""
    print(message)

    if config.dry_run:
        print("DRY_RUN のため送信しませんでした。", file=sys.stderr)
        return False

    if not config.webhook_url:
        print("DISCORD_WEBHOOK_URL が未設定のため送信しませんでした。", file=sys.stderr)
        return False

    send_discord(config.webhook_url, message)
    return True


def handle_fetch_failure(config: Config, state: dict, error: Exception) -> int:
    failures = int(state.get("fetch_failures", 0)) + 1
    state["fetch_failures"] = failures

    print(f"取得に失敗しました（連続{failures}回）: {error}", file=sys.stderr)

    should_alert = failures >= config.fetch_failure_alerts and not state.get("fetch_failure_alerted")
    if should_alert and notify(config, build_fetch_failure_message(failures, error, config)):
        state["fetch_failure_alerted"] = True

    if not config.dry_run:
        save_state(config.state_file, state)

    return 1


def run(config: Config, now: datetime | None = None, sleep=time.sleep) -> int:
    now = now or datetime.now(timezone.utc)
    state = load_state(config.state_file)

    try:
        record = fetch_record(config, sleep=sleep)
    except (RuntimeError, requests.RequestException) as error:
        return handle_fetch_failure(config, state, error)

    status = evaluate(record, now, config.threshold)
    action = decide(status, state, now, config.repeat_after)

    print(f"hostname: {status.record.hostname}")
    print(f"latest_timestamp: {status.record.timestamp.isoformat()}")
    print(f"elapsed_minutes: {status.elapsed.total_seconds() / 60:.1f}")
    print(f"status: {'stale' if status.stale else 'healthy'} / action: {action}")

    if action == NONE:
        if not config.dry_run:
            save_state(config.state_file, next_state(NONE, status, state, now))
        return 0

    message = (
        build_recovery_message(status, state, config)
        if action == RECOVERED
        else build_alert_message(action, status, config)
    )

    if notify(config, message):
        save_state(config.state_file, next_state(action, status, state, now))

    return 0


def config_from_env(env: dict | None = None) -> Config:
    env = os.environ if env is None else env

    return Config(
        script_url=env["GOOGLE_SCRIPT_URL"],
        webhook_url=env.get("DISCORD_WEBHOOK_URL") or None,
        threshold=timedelta(
            minutes=float(env.get("THRESHOLD_MINUTES") or DEFAULT_THRESHOLD_MINUTES)
        ),
        timeout=float(env.get("REQUEST_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS),
        retries=int(env.get("REQUEST_RETRIES") or DEFAULT_RETRIES),
        retry_wait=float(env.get("RETRY_WAIT_SECONDS") or DEFAULT_RETRY_WAIT_SECONDS),
        repeat_after=timedelta(
            hours=float(env.get("REPEAT_ALERT_HOURS") or DEFAULT_REPEAT_ALERT_HOURS)
        ),
        fetch_failure_alerts=int(
            env.get("FETCH_FAILURE_ALERTS") or DEFAULT_FETCH_FAILURE_ALERTS
        ),
        state_file=Path(env.get("MONITOR_STATE_FILE") or ".monitor-state.json"),
        dashboard_url=env.get("DASHBOARD_URL") or DASHBOARD_URL,
        dry_run=(env.get("DRY_RUN") or "").lower() in ("1", "true", "yes"),
    )


def main() -> None:
    load_dotenv()

    config = config_from_env()
    if "--dry-run" in sys.argv:
        config.dry_run = True

    sys.exit(run(config))


if __name__ == "__main__":
    main()

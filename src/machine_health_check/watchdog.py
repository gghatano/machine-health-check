"""スプレッドシートの最終更新を監視し、欠測が続いていれば Discord へ通知する。

監視対象の Ubuntu 自身でこれを動かすと、Ubuntu が停止したときに通知処理も
一緒に止まってしまい、肝心の停止を検知できない。そのため GitHub Actions など
「別の場所」から定期実行し、スプレッドシートの最新 timestamp だけを見る。
"""

import csv
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests


DEFAULT_SHEET_ID = "126zgKBmQb1BsbEXgZ26Yg6Z4jmmCcCpvPRmN5zkK3Rk"
DEFAULT_SHEET_NAME = "metrics"
DEFAULT_DASHBOARD_URL = "https://gghatano.github.io/machine-health-check/"

# 30分間隔で記録しているので、既定では2回連続で欠けたら通知する
DEFAULT_MISSING_AFTER_MINUTES = 90
# 同じ障害について通知し続けないための再通知間隔
DEFAULT_REPEAT_ALERT_HOURS = 6

ALERT = "alert"
REPEAT = "repeat"
RECOVERED = "recovered"
NONE = "none"


@dataclass
class Config:
    sheet_id: str = DEFAULT_SHEET_ID
    sheet_name: str = DEFAULT_SHEET_NAME
    missing_after: timedelta = timedelta(minutes=DEFAULT_MISSING_AFTER_MINUTES)
    repeat_after: timedelta = timedelta(hours=DEFAULT_REPEAT_ALERT_HOURS)
    webhook_url: str | None = None
    state_file: Path = Path(".watchdog-state.json")
    dashboard_url: str = DEFAULT_DASHBOARD_URL
    dry_run: bool = False


@dataclass
class Latest:
    """スプレッドシートの最終行から読み取った情報。"""

    timestamp: datetime
    hostname: str


@dataclass
class Status:
    latest: Latest | None
    age: timedelta | None
    missing: bool


def build_csv_url(sheet_id: str, sheet_name: str) -> str:
    query = urlencode({"tqx": "out:csv", "sheet": sheet_name, "headers": "1"})
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?{query}"


def fetch_csv(url: str, timeout: int = 20) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    text = response.text
    if text.lstrip().startswith("<"):
        raise RuntimeError(
            "スプレッドシートをCSVとして読み取れませんでした。"
            "共有設定が「リンクを知っている全員が閲覧可」か確認してください。"
        )

    return text


def parse_latest(csv_text: str) -> Latest | None:
    """CSV から最も新しい記録を取り出す。1件も無ければ None。"""
    rows = list(csv.DictReader(io.StringIO(csv_text)))

    latest: Latest | None = None
    for row in rows:
        raw = (row.get("timestamp") or "").strip()
        if not raw:
            continue

        try:
            timestamp = datetime.fromisoformat(raw)
        except ValueError:
            continue

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        if latest is None or timestamp > latest.timestamp:
            latest = Latest(
                timestamp=timestamp,
                hostname=(row.get("hostname") or "").strip(),
            )

    return latest


def evaluate(latest: Latest | None, now: datetime, missing_after: timedelta) -> Status:
    if latest is None:
        return Status(latest=None, age=None, missing=True)

    age = now - latest.timestamp
    return Status(latest=latest, age=age, missing=age > missing_after)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 壊れた状態ファイルは「通知履歴なし」として扱う(最悪、通知が1回重複するだけ)
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def decide(status: Status, state: dict, now: datetime, repeat_after: timedelta) -> str:
    """通知するかどうかを、前回までの通知履歴と突き合わせて決める。"""
    alerted = bool(state.get("alerted"))

    if status.missing:
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


def format_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}分"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}時間{minutes}分" if minutes else f"{hours}時間"

    days, hours = divmod(hours, 24)
    return f"{days}日{hours}時間" if hours else f"{days}日"


def format_timestamp(value: datetime) -> str:
    offset = value.strftime("%z")
    suffix = f" {offset[:3]}:{offset[3:]}" if offset else ""
    return f"{value.strftime('%Y-%m-%d %H:%M')}{suffix}"


def build_message(action: str, status: Status, config: Config, state: dict) -> str:
    lines: list[str] = []

    if action in (ALERT, REPEAT):
        head = "メトリクスが記録されていません" if action == ALERT else "メトリクスが記録されていない状態が続いています"
        lines.append(f"🔴 **{head}**")

        if status.latest is None:
            lines.append("スプレッドシートに記録が1件もありません。")
        else:
            lines.append(f"ホスト: `{status.latest.hostname or '不明'}`")
            lines.append(f"最新データ: {format_timestamp(status.latest.timestamp)}")
            lines.append(f"経過: {format_duration(status.age)}")

        lines.append(f"しきい値: {format_duration(config.missing_after)}")

    elif action == RECOVERED:
        lines.append("🟢 **メトリクスの記録が再開しました**")

        if status.latest is not None:
            lines.append(f"ホスト: `{status.latest.hostname or '不明'}`")
            lines.append(f"最新データ: {format_timestamp(status.latest.timestamp)}")

            stopped_at = state.get("alert_latest")
            if stopped_at and status.latest is not None:
                try:
                    previous = datetime.fromisoformat(stopped_at)
                except ValueError:
                    previous = None
                if previous is not None:
                    lines.append(
                        f"途切れていた期間: {format_timestamp(previous)} → "
                        f"{format_timestamp(status.latest.timestamp)}"
                        f"（{format_duration(status.latest.timestamp - previous)}）"
                    )

    lines.append(config.dashboard_url)
    return "\n".join(lines)


def post_discord(webhook_url: str, content: str, timeout: int = 15) -> None:
    response = requests.post(webhook_url, json={"content": content}, timeout=timeout)
    response.raise_for_status()


def next_state(action: str, status: Status, state: dict, now: datetime) -> dict:
    if action in (ALERT, REPEAT):
        updated = {
            "alerted": True,
            "last_alert_at": now.isoformat(timespec="seconds"),
            "first_missing_at": state.get("first_missing_at") or now.isoformat(timespec="seconds"),
        }
        if action == ALERT:
            updated["alert_latest"] = (
                status.latest.timestamp.isoformat(timespec="seconds")
                if status.latest is not None
                else None
            )
        else:
            updated["alert_latest"] = state.get("alert_latest")
        return updated

    if action == RECOVERED:
        return {
            "alerted": False,
            "last_recovery_at": now.isoformat(timespec="seconds"),
        }

    return state


def run(config: Config, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc).astimezone()

    csv_text = fetch_csv(build_csv_url(config.sheet_id, config.sheet_name))
    status = evaluate(parse_latest(csv_text), now, config.missing_after)
    state = load_state(config.state_file)
    action = decide(status, state, now, config.repeat_after)

    latest_label = (
        format_timestamp(status.latest.timestamp) if status.latest is not None else "なし"
    )
    age_label = format_duration(status.age) if status.age is not None else "-"
    print(f"最新データ: {latest_label} / 経過: {age_label} / 判定: {action}")

    if action == NONE:
        return 0

    message = build_message(action, status, config, state)
    print(message)

    # 実際に送っていないときは状態を進めない(次回きちんと通知できるようにする)
    if config.dry_run:
        print("DRY_RUN のため送信しませんでした。", file=sys.stderr)
        return 0
    if not config.webhook_url:
        print("DISCORD_WEBHOOK_URL が未設定のため送信しませんでした。", file=sys.stderr)
        return 0

    post_discord(config.webhook_url, message)
    save_state(config.state_file, next_state(action, status, state, now))
    return 0


def config_from_env(env: dict | None = None) -> Config:
    env = os.environ if env is None else env

    return Config(
        sheet_id=env.get("SPREADSHEET_ID") or DEFAULT_SHEET_ID,
        sheet_name=env.get("SHEET_NAME") or DEFAULT_SHEET_NAME,
        missing_after=timedelta(
            minutes=float(env.get("MISSING_AFTER_MINUTES") or DEFAULT_MISSING_AFTER_MINUTES)
        ),
        repeat_after=timedelta(
            hours=float(env.get("REPEAT_ALERT_HOURS") or DEFAULT_REPEAT_ALERT_HOURS)
        ),
        webhook_url=env.get("DISCORD_WEBHOOK_URL") or None,
        state_file=Path(env.get("WATCHDOG_STATE_FILE") or ".watchdog-state.json"),
        dashboard_url=env.get("DASHBOARD_URL") or DEFAULT_DASHBOARD_URL,
        dry_run=(env.get("DRY_RUN") or "").lower() in ("1", "true", "yes"),
    )


def main() -> None:
    config = config_from_env()
    if "--dry-run" in sys.argv:
        config.dry_run = True
    sys.exit(run(config))


if __name__ == "__main__":
    main()

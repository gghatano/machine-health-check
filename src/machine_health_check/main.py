import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from machine_health_check.metrics import collect_metrics, save_state
from machine_health_check.sender import (
    ConfigurationError,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    send_metrics,
)


# 設定を直さないと解決しない失敗。systemd 側で再試行させないために区別する
CONFIGURATION_EXIT_CODE = 2


def main() -> None:
    project_root = Path.cwd()

    load_dotenv(project_root / ".env")

    google_script_url = os.environ["GOOGLE_SCRIPT_URL"]
    metrics_token = os.environ["METRICS_TOKEN"]
    timeout = float(os.environ.get("SEND_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
    retries = int(os.environ.get("SEND_RETRIES") or DEFAULT_RETRIES)

    metrics = collect_metrics()

    print(
        json.dumps(
            metrics,
            indent=2,
            ensure_ascii=False,
        )
    )

    try:
        result = send_metrics(
            url=google_script_url,
            token=metrics_token,
            metrics=metrics,
            timeout=timeout,
            retries=retries,
        )
    except ConfigurationError as error:
        print(f"設定を直さないと解決しない失敗です: {error}", file=sys.stderr)
        raise SystemExit(CONFIGURATION_EXIT_CODE)

    print("POST result:", result)

    # 送信できた回だけ state を進める。失敗した回のぶんは次回のデルタに含まれる
    save_state(
        bytes_recv=metrics["bytes_recv"],
        bytes_sent=metrics["bytes_sent"],
        timestamp=metrics["timestamp"],
    )


if __name__ == "__main__":
    main()

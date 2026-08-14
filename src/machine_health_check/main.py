import json
import os
from pathlib import Path

from dotenv import load_dotenv

from machine_health_check.metrics import collect_metrics
from machine_health_check.sender import send_metrics


def main() -> None:
    project_root = Path.cwd()

    load_dotenv(project_root / ".env")

    google_script_url = os.environ["GOOGLE_SCRIPT_URL"]
    metrics_token = os.environ["METRICS_TOKEN"]

    metrics = collect_metrics()

    print(
        json.dumps(
            metrics,
            indent=2,
            ensure_ascii=False,
        )
    )

    result = send_metrics(
        url=google_script_url,
        token=metrics_token,
        metrics=metrics,
    )

    print("POST result:", result)


if __name__ == "__main__":
    main()

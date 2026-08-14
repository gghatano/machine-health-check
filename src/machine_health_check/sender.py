import requests


def send_metrics(
    url: str,
    token: str,
    metrics: dict,
) -> dict:
    payload = {
        "token": token,
        **metrics,
    }

    response = requests.post(
        url,
        json=payload,
        timeout=15,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Apps Script returned an error: {result}"
        )

    return result

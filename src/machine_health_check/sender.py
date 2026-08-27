import sys
import time

import requests


# Apps Script はコールドスタート時に応答が遅れる。journal の実測では、送信が
# 成功した回でも最大23秒かかっており（中央値4秒）、15秒では短すぎた。
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_RETRIES = 3
DEFAULT_RETRY_WAIT_SECONDS = 5


class ConfigurationError(RuntimeError):
    """リトライしても直らない失敗。トークンや権限など設定側を直す必要がある。"""


class NotFoundError(RuntimeError):
    """Apps Script が 404 を返した。リトライする価値がある。

    正常なデプロイに対しても断続的に 404 が返ることを実測で確認している
    （同じURLで、前後の回は成功している）。URLが古いケースと区別できないため、
    まずリトライし、全部だめだったときに設定を疑うヒントを出す。
    """


def send_metrics(
    url: str,
    token: str,
    metrics: dict,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    retry_wait: float = DEFAULT_RETRY_WAIT_SECONDS,
    sleep=time.sleep,
) -> dict:
    payload = {
        "token": token,
        **metrics,
    }

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            return _post_once(url, payload, timeout)
        except ConfigurationError:
            # 設定の問題はリトライしても同じ結果になるので、すぐ諦める
            raise
        except (requests.RequestException, ValueError, RuntimeError) as error:
            last_error = error
            print(
                f"送信に失敗しました（{attempt}/{retries}）: {error!r}",
                file=sys.stderr,
            )
            if attempt < retries:
                sleep(retry_wait * attempt)

    message = f"メトリクスの送信に{retries}回失敗しました: {last_error!r}"
    if isinstance(last_error, NotFoundError):
        # 断続的な404と、URLが古いケースの切り分けはここで促す
        message += (
            "（404 が続く場合は、再デプロイでURLが変わっていないか"
            " GOOGLE_SCRIPT_URL を確認してください）"
        )

    raise RuntimeError(message)


def _post_once(url: str, payload: dict, timeout: float) -> dict:
    response = requests.post(
        url,
        json=payload,
        timeout=timeout,
    )

    status = getattr(response, "status_code", None)
    if status == 404:
        raise NotFoundError("Apps Script が 404 を返しました。")
    if status is not None and 400 <= status < 500:
        raise ConfigurationError(
            f"Apps Script が {status} を返しました。"
            "デプロイのアクセス権と GOOGLE_SCRIPT_URL を確認してください。"
        )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        # トークン不一致など、こちらの送信内容の問題。リトライしても直らない
        raise ConfigurationError(
            f"Apps Script returned an error: {result}"
        )

    return result

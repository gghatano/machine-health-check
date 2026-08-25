import json

import pytest

from machine_health_check import main as main_module
from machine_health_check.main import main
from machine_health_check.sender import ConfigurationError


@pytest.fixture
def isolated_main(tmp_path, monkeypatch):
    """.env の読み込みと外部呼び出しを切り離して main() を実行できるようにする。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "load_dotenv", lambda path: None)
    for name in ("GOOGLE_SCRIPT_URL", "METRICS_TOKEN", "SEND_TIMEOUT_SECONDS", "SEND_RETRIES"):
        monkeypatch.delenv(name, raising=False)

    sent = []
    saved = []
    metrics = {
        "hostname": "test-host",
        "cpu_percent": 12.5,
        "bytes_recv": 2_000_000,
        "bytes_sent": 1_000_000,
        "timestamp": "2026-08-24T12:00:00+09:00",
    }

    monkeypatch.setattr(main_module, "collect_metrics", lambda: metrics)
    monkeypatch.setattr(
        main_module,
        "send_metrics",
        lambda **kwargs: (sent.append(kwargs), {"ok": True})[1],
    )
    monkeypatch.setattr(
        main_module,
        "save_state",
        lambda **kwargs: saved.append(kwargs),
    )

    return sent, saved, metrics


@pytest.fixture
def configured(isolated_main, monkeypatch):
    monkeypatch.setenv("GOOGLE_SCRIPT_URL", "https://example.com/exec")
    monkeypatch.setenv("METRICS_TOKEN", "secret")
    return isolated_main


class TestMain:
    def test_sends_collected_metrics_with_env_config(self, configured, capsys):
        sent, _, metrics = configured

        main()

        assert len(sent) == 1
        assert sent[0]["url"] == "https://example.com/exec"
        assert sent[0]["token"] == "secret"
        assert sent[0]["metrics"] == metrics

        out = capsys.readouterr().out
        assert json.loads(out[: out.index("POST result:")]) == metrics
        assert "POST result: {'ok': True}" in out

    def test_passes_timeout_and_retries_from_env(self, configured, monkeypatch):
        sent, _, _ = configured
        monkeypatch.setenv("SEND_TIMEOUT_SECONDS", "20")
        monkeypatch.setenv("SEND_RETRIES", "5")

        main()

        assert sent[0]["timeout"] == 20
        assert sent[0]["retries"] == 5

    def test_uses_generous_defaults_for_cold_starts(self, configured):
        sent, _, _ = configured

        main()

        assert sent[0]["timeout"] == 45
        assert sent[0]["retries"] == 3

    def test_saves_state_after_a_successful_send(self, configured):
        _, saved, metrics = configured

        main()

        assert saved == [
            {
                "bytes_recv": metrics["bytes_recv"],
                "bytes_sent": metrics["bytes_sent"],
                "timestamp": metrics["timestamp"],
            }
        ]

    def test_does_not_save_state_when_send_fails(self, configured, monkeypatch):
        """失敗した回の通信量を次回のデルタに含めるため、state は進めない。"""
        _, saved, _ = configured

        def boom(**kwargs):
            raise RuntimeError("send failed")

        monkeypatch.setattr(main_module, "send_metrics", boom)

        with pytest.raises(RuntimeError, match="send failed"):
            main()

        assert saved == []

    def test_configuration_error_exits_with_code_2(self, configured, monkeypatch):
        """systemd に「再試行しても無駄」と伝えるため、専用の終了コードを使う。"""
        _, saved, _ = configured

        def boom(**kwargs):
            raise ConfigurationError("Apps Script が 404 を返しました。")

        monkeypatch.setattr(main_module, "send_metrics", boom)

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 2
        assert saved == []

    def test_reads_dotenv_from_current_directory(self, configured, monkeypatch, tmp_path):
        loaded = []
        monkeypatch.setattr(main_module, "load_dotenv", lambda path: loaded.append(path))

        main()

        assert loaded == [tmp_path / ".env"]

    @pytest.mark.parametrize("missing", ["GOOGLE_SCRIPT_URL", "METRICS_TOKEN"])
    def test_raises_when_env_var_missing(self, configured, monkeypatch, missing):
        monkeypatch.delenv(missing)

        with pytest.raises(KeyError, match=missing):
            main()

    def test_does_not_send_when_collect_fails(self, configured, monkeypatch):
        sent, saved, _ = configured

        def boom():
            raise RuntimeError("collect failed")

        monkeypatch.setattr(main_module, "collect_metrics", boom)

        with pytest.raises(RuntimeError, match="collect failed"):
            main()

        assert sent == []
        assert saved == []

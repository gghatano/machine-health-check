import json

import pytest

from machine_health_check import main as main_module
from machine_health_check.main import main


@pytest.fixture
def isolated_main(tmp_path, monkeypatch):
    """.env の読み込みと外部呼び出しを切り離して main() を実行できるようにする。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "load_dotenv", lambda path: None)
    monkeypatch.delenv("GOOGLE_SCRIPT_URL", raising=False)
    monkeypatch.delenv("METRICS_TOKEN", raising=False)

    sent = []
    metrics = {"hostname": "test-host", "cpu_percent": 12.5}

    monkeypatch.setattr(main_module, "collect_metrics", lambda: metrics)
    monkeypatch.setattr(
        main_module,
        "send_metrics",
        lambda url, token, metrics: (
            sent.append({"url": url, "token": token, "metrics": metrics}),
            {"ok": True},
        )[1],
    )

    return sent, metrics


class TestMain:
    def test_sends_collected_metrics_with_env_config(
        self, isolated_main, monkeypatch, capsys
    ):
        sent, metrics = isolated_main
        monkeypatch.setenv("GOOGLE_SCRIPT_URL", "https://example.com/exec")
        monkeypatch.setenv("METRICS_TOKEN", "secret")

        main()

        assert sent == [
            {
                "url": "https://example.com/exec",
                "token": "secret",
                "metrics": metrics,
            }
        ]

        out = capsys.readouterr().out
        assert json.loads(out[: out.index("POST result:")]) == metrics
        assert "POST result: {'ok': True}" in out

    def test_reads_dotenv_from_current_directory(
        self, isolated_main, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("GOOGLE_SCRIPT_URL", "https://example.com/exec")
        monkeypatch.setenv("METRICS_TOKEN", "secret")

        loaded = []
        monkeypatch.setattr(main_module, "load_dotenv", lambda path: loaded.append(path))

        main()

        assert loaded == [tmp_path / ".env"]

    @pytest.mark.parametrize("missing", ["GOOGLE_SCRIPT_URL", "METRICS_TOKEN"])
    def test_raises_when_env_var_missing(self, isolated_main, monkeypatch, missing):
        monkeypatch.setenv("GOOGLE_SCRIPT_URL", "https://example.com/exec")
        monkeypatch.setenv("METRICS_TOKEN", "secret")
        monkeypatch.delenv(missing)

        with pytest.raises(KeyError, match=missing):
            main()

    def test_does_not_send_when_collect_fails(self, isolated_main, monkeypatch):
        sent, _ = isolated_main
        monkeypatch.setenv("GOOGLE_SCRIPT_URL", "https://example.com/exec")
        monkeypatch.setenv("METRICS_TOKEN", "secret")

        def boom():
            raise RuntimeError("collect failed")

        monkeypatch.setattr(main_module, "collect_metrics", boom)

        with pytest.raises(RuntimeError, match="collect failed"):
            main()

        assert sent == []

import json

import pytest

from corp_codex_pool.multica import MulticaError, persist_codex_binary_path


def test_persist_codex_binary_path_preserves_existing_config(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "server_url": "https://multica.example.com",
                "token": "secret-sentinel",
                "future_field": {"keep": True},
                "backends": {"openclaw": {"state_dir": "/var/lib/openclaw"}},
            }
        ),
        encoding="utf-8",
    )

    changed = persist_codex_binary_path("/opt/company/bin/mcodex", config_path)

    assert changed is True
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["backends"]["codex"]["binary_path"] == "/opt/company/bin/mcodex"
    assert saved["backends"]["openclaw"]["state_dir"] == "/var/lib/openclaw"
    assert saved["future_field"] == {"keep": True}
    assert saved["token"] == "secret-sentinel"
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert persist_codex_binary_path("/opt/company/bin/mcodex", config_path) is False


def test_persist_codex_binary_path_rejects_relative_path(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    with pytest.raises(MulticaError, match="绝对路径"):
        persist_codex_binary_path("relative/mcodex", config_path)

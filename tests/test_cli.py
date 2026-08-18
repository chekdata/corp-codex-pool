import json
import tomllib

from click.testing import CliRunner

from corp_codex_pool.cli import main


def test_gui_configure_writes_official_codex_files(tmp_path):
    config = tmp_path / "config.toml"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "official"}}), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "gui",
            "configure",
            "--key-stdin",
            "--config",
            str(config),
            "--auth",
            str(auth),
        ],
        input="mck_employee\n",
    )

    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert parsed["model_provider"] == "chek"
    assert parsed["model_providers"]["chek"]["requires_openai_auth"] is True
    stored_auth = json.loads(auth.read_text(encoding="utf-8"))
    assert stored_auth["OPENAI_API_KEY"] == "mck_employee"
    assert stored_auth["tokens"]["access_token"] == "official"


def test_gui_configure_rejects_unmanaged_key(tmp_path):
    result = CliRunner().invoke(
        main,
        [
            "gui",
            "configure",
            "--key-stdin",
            "--config",
            str(tmp_path / "config.toml"),
            "--auth",
            str(tmp_path / "auth.json"),
        ],
        input="sk-not-managed\n",
    )

    assert result.exit_code != 0
    assert "凭证格式不正确" in result.output

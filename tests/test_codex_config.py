import json
import tomllib

import pytest

from corp_codex_pool.codex_config import (
    BEGIN,
    END,
    ConfigInjectionError,
    ProviderSpec,
    build_new_text,
    inject,
    remove,
    render_block,
    strip_block,
    verify,
    write_openai_auth_key,
)

# 取自真实宿主配置的形状：顶层键在前，[projects.*] 在后
REAL_CONFIG = '''\
model = "gpt-5.6-terra"
model_reasoning_effort = "xhigh"
personality = "pragmatic"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[projects."/home/dev"]
trust_level = "trusted"

[projects."/home/dev/workspace"]
trust_level = "untrusted"
'''


def spec(**kw):
    kw.setdefault("base_url", "https://gw.example.com/v1")
    return ProviderSpec(**kw)


class TestTopLevelKeyTrap:
    """顶层键必须落在第一个 [table] 之前，否则被吞进该 table。"""

    def test_model_provider_is_top_level_not_swallowed(self):
        out = build_new_text(REAL_CONFIG, spec())
        data = tomllib.loads(out)
        assert data["model_provider"] == "gw"
        # 没有被塞进 projects
        assert "model_provider" not in data["projects"]["/home/dev"]

    def test_block_inserted_before_first_table(self):
        out = build_new_text(REAL_CONFIG, spec())
        assert out.index(BEGIN) < out.index('[projects."/home/dev"]')

    def test_existing_top_level_keys_survive(self):
        data = tomllib.loads(build_new_text(REAL_CONFIG, spec()))
        assert data["model"] == "gpt-5.6-terra"
        assert data["approval_policy"] == "never"
        assert data["sandbox_mode"] == "danger-full-access"

    def test_existing_tables_survive(self):
        data = tomllib.loads(build_new_text(REAL_CONFIG, spec()))
        assert data["projects"]["/home/dev"]["trust_level"] == "trusted"
        assert data["projects"]["/home/dev/workspace"]["trust_level"] == "untrusted"

    def test_config_with_only_tables(self):
        src = '[projects."/a"]\ntrust_level = "trusted"\n'
        data = tomllib.loads(build_new_text(src, spec()))
        assert data["model_provider"] == "gw"
        assert data["projects"]["/a"]["trust_level"] == "trusted"

    def test_config_with_only_top_level_keys(self):
        data = tomllib.loads(build_new_text('model = "x"\n', spec()))
        assert data["model_provider"] == "gw"
        assert data["model"] == "x"

    def test_empty_config(self):
        data = tomllib.loads(build_new_text("", spec()))
        assert data["model_provider"] == "gw"
        assert data["model_providers"]["gw"]["wire_api"] == "responses"


class TestProviderFields:
    def test_required_fields_present(self):
        data = tomllib.loads(build_new_text(REAL_CONFIG, spec()))
        p = data["model_providers"]["gw"]
        assert p["base_url"] == "https://gw.example.com/v1"
        assert p["wire_api"] == "responses"
        assert p["env_key"] == "GW_API_KEY"

    def test_env_http_headers_mapping(self):
        data = tomllib.loads(build_new_text(REAL_CONFIG, spec()))
        headers = data["model_providers"]["gw"]["env_http_headers"]
        assert headers["X-Multica-Task-Id"] == "MULTICA_TASK_ID"
        assert headers["X-Multica-Agent-Id"] == "MULTICA_AGENT_ID"
        assert headers["X-Multica-Workspace-Id"] == "MULTICA_WORKSPACE_ID"

    def test_custom_provider_id(self):
        data = tomllib.loads(build_new_text(REAL_CONFIG, spec(provider_id="pool2")))
        assert data["model_provider"] == "pool2"
        assert "pool2" in data["model_providers"]

    def test_set_default_false_registers_without_switching(self):
        data = tomllib.loads(build_new_text(REAL_CONFIG, spec(set_default=False)))
        assert "model_provider" not in data
        assert data["model_providers"]["gw"]["base_url"] == "https://gw.example.com/v1"

    def test_quotes_in_values_escaped(self):
        s = spec(name='Acme "Pool" Inc')
        data = tomllib.loads(build_new_text(REAL_CONFIG, s))
        assert data["model_providers"]["gw"]["name"] == 'Acme "Pool" Inc'

    def test_official_gui_uses_auth_json_instead_of_environment(self):
        gui = spec(requires_openai_auth=True, env_http_headers={})
        provider = tomllib.loads(build_new_text(REAL_CONFIG, gui))["model_providers"]["gw"]
        assert provider["requires_openai_auth"] is True
        assert "env_key" not in provider


class TestIdempotence:
    def test_second_run_is_noop(self):
        once = build_new_text(REAL_CONFIG, spec())
        twice = build_new_text(once, spec())
        assert once == twice

    def test_ten_runs_stable(self):
        text = REAL_CONFIG
        for _ in range(10):
            text = build_new_text(text, spec())
        assert text.count(BEGIN) == 1
        assert text.count(END) == 1

    def test_rerun_with_changed_url_replaces(self):
        first = build_new_text(REAL_CONFIG, spec())
        second = build_new_text(first, spec(base_url="https://new.example.com/v1"))
        data = tomllib.loads(second)
        assert data["model_providers"]["gw"]["base_url"] == "https://new.example.com/v1"
        assert second.count(BEGIN) == 1

    def test_strip_restores_original(self):
        injected = build_new_text(REAL_CONFIG, spec())
        assert tomllib.loads(strip_block(injected)) == tomllib.loads(REAL_CONFIG)


class TestValidation:
    def test_wire_api_chat_rejected(self):
        with pytest.raises(ConfigInjectionError, match="responses"):
            render_block(spec(wire_api="chat"))

    def test_bad_provider_id_rejected(self):
        with pytest.raises(ConfigInjectionError, match="provider_id"):
            render_block(spec(provider_id="has space"))

    def test_lowercase_env_key_rejected(self):
        with pytest.raises(ConfigInjectionError, match="env_key"):
            render_block(spec(env_key="gw_api_key"))

    def test_non_http_base_url_rejected(self):
        with pytest.raises(ConfigInjectionError, match="base_url"):
            render_block(spec(base_url="ftp://x/v1"))

    def test_verify_catches_wrong_url(self):
        text = build_new_text(REAL_CONFIG, spec())
        with pytest.raises(ConfigInjectionError, match="base_url"):
            verify(text, spec(base_url="https://other.example.com/v1"))

    def test_verify_catches_swallowed_top_level_key(self):
        # 人为构造顶层键被 table 吞掉的坏文件
        bad = '[projects."/a"]\nmodel_provider = "gw"\n' + render_block(spec())
        with pytest.raises(ConfigInjectionError):
            verify(bad, spec())


class TestFileOps:
    def test_inject_writes_and_backs_up(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(REAL_CONFIG, encoding="utf-8")

        result = inject(spec(), path=cfg)
        assert result.changed
        assert result.backup is not None and result.backup.exists()
        assert result.backup.read_text(encoding="utf-8") == REAL_CONFIG
        assert tomllib.loads(cfg.read_text(encoding="utf-8"))["model_provider"] == "gw"

    def test_inject_idempotent_on_disk(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(REAL_CONFIG, encoding="utf-8")

        inject(spec(), path=cfg)
        content = cfg.read_text(encoding="utf-8")
        second = inject(spec(), path=cfg)

        assert not second.changed
        assert cfg.read_text(encoding="utf-8") == content

    def test_dry_run_does_not_touch_disk(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(REAL_CONFIG, encoding="utf-8")

        result = inject(spec(), path=cfg, dry_run=True)
        assert result.changed
        assert cfg.read_text(encoding="utf-8") == REAL_CONFIG

    def test_inject_creates_missing_file(self, tmp_path):
        cfg = tmp_path / "sub" / "config.toml"
        inject(spec(), path=cfg)
        assert tomllib.loads(cfg.read_text(encoding="utf-8"))["model_provider"] == "gw"

    def test_no_tmp_file_left_behind(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(REAL_CONFIG, encoding="utf-8")
        inject(spec(), path=cfg)
        assert not list(tmp_path.glob("*.tmp"))

    def test_remove_restores(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(REAL_CONFIG, encoding="utf-8")

        inject(spec(), path=cfg)
        remove(path=cfg)

        assert tomllib.loads(cfg.read_text(encoding="utf-8")) == tomllib.loads(REAL_CONFIG)

    def test_multica_managed_block_untouched(self, tmp_path):
        """daemon 的托管块与我们的块必须互不干扰。"""
        # 取自真实 per-task CODEX_HOME/config.toml 的形状。用 memory-config 块而非
        # sandbox 块：daemon 的 sandbox_mode 会替换宿主同名键，两者不会并存。
        src = (
            "# BEGIN multica-managed memory-config (do not edit; regenerated by daemon)\n"
            "memories.generate_memories = false\n"
            "memories.use_memories = false\n"
            "# END multica-managed memory-config\n\n" + REAL_CONFIG
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(src, encoding="utf-8")

        inject(spec(), path=cfg)
        out = cfg.read_text(encoding="utf-8")

        assert "# BEGIN multica-managed" in out
        assert tomllib.loads(out)["model_provider"] == "gw"
        # 我们的块不能插进 multica 的块里
        assert out.index("# END multica-managed") < out.index(BEGIN)


class TestOfficialGuiAuth:
    def test_preserves_official_login_fields(self, tmp_path):
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": "official"}}), encoding="utf-8")

        backup = write_openai_auth_key("mck_test", auth)

        stored = json.loads(auth.read_text(encoding="utf-8"))
        assert stored["tokens"]["access_token"] == "official"
        assert stored["OPENAI_API_KEY"] == "mck_test"
        assert backup is not None and backup.exists()
        assert auth.stat().st_mode & 0o777 == 0o600

    def test_rejects_empty_key(self, tmp_path):
        with pytest.raises(ConfigInjectionError, match="不能为空"):
            write_openai_auth_key("", tmp_path / "auth.json")

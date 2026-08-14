import os
import stat
import tomllib

import pytest
import httpx

from corp_codex_pool.codex_config import ProviderSpec
from corp_codex_pool.mcodex import (
    KEY_FILENAME,
    McodexError,
    find_real_codex,
    init_home,
    is_version_probe,
    mcodex_home,
    prepare_env,
    read_key,
    request_session_key,
)

HOST_CONFIG = '''\
model = "gpt-5.6-terra"
model_reasoning_effort = "xhigh"
personality = "pragmatic"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[projects."/home/dev"]
trust_level = "trusted"

[projects."/home/dev/secret"]
trust_level = "untrusted"
'''


@pytest.fixture
def spec():
    return ProviderSpec(base_url="https://gw.example.com/v1")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """每个用例都从干净环境开始，避免真实环境泄漏进测试。"""
    for name in (
        "CODEX_HOME",
        "GW_API_KEY",
        "MCODEX_HOME",
        "MCODEX_REAL_CODEX",
        "MULTICA_TOKEN",
        "MULTICA_TASK_ID",
        "MULTICA_AGENT_ID",
        "MULTICA_WORKSPACE_ID",
    ):
        monkeypatch.delenv(name, raising=False)


class TestIsolationFromUserCodex:
    """核心契约：绝不碰用户的 ~/.codex。"""

    def test_init_does_not_touch_source_config(self, tmp_path, spec):
        host = tmp_path / "dot-codex" / "config.toml"
        host.parent.mkdir()
        host.write_text(HOST_CONFIG, encoding="utf-8")
        before = host.read_text(encoding="utf-8")

        init_home(tmp_path / "dot-mcodex", spec, inherit_from=host)

        assert host.read_text(encoding="utf-8") == before

    def test_inherits_preferences_not_credentials(self, tmp_path, spec):
        host = tmp_path / "dot-codex" / "config.toml"
        host.parent.mkdir()
        host.write_text(HOST_CONFIG, encoding="utf-8")
        # 凭据文件不该被复制
        (host.parent / "auth.json").write_text('{"tokens":{"access_token":"secret"}}')

        home = tmp_path / "dot-mcodex"
        info = init_home(home, spec, inherit_from=host)

        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        assert data["model"] == "gpt-5.6-terra"
        assert data["approval_policy"] == "never"
        assert "model" in info["inherited"]
        assert not (home / "auth.json").exists()

    def test_trust_levels_carried_over(self, tmp_path, spec):
        host = tmp_path / "dot-codex" / "config.toml"
        host.parent.mkdir()
        host.write_text(HOST_CONFIG, encoding="utf-8")

        home = tmp_path / "dot-mcodex"
        init_home(home, spec, inherit_from=host)

        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        assert data["projects"]["/home/dev"]["trust_level"] == "trusted"
        assert data["projects"]["/home/dev/secret"]["trust_level"] == "untrusted"

    def test_no_inherit_produces_minimal_config(self, tmp_path, spec):
        home = tmp_path / "dot-mcodex"
        init_home(home, spec, inherit_from=None)

        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        assert data["model_provider"] == "gw"
        assert "model" not in data


class TestInitHome:
    def test_provider_written(self, tmp_path, spec):
        home = tmp_path / "h"
        init_home(home, spec)

        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        assert data["model_providers"]["gw"]["base_url"] == "https://gw.example.com/v1"
        assert data["model_providers"]["gw"]["wire_api"] == "responses"

    def test_home_is_private(self, tmp_path, spec):
        home = tmp_path / "h"
        init_home(home, spec)
        assert stat.S_IMODE(home.stat().st_mode) == 0o700

    def test_key_file_is_600(self, tmp_path, spec):
        home = tmp_path / "h"
        init_home(home, spec, key="sk-clb-secret")
        key_file = home / KEY_FILENAME
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert key_file.read_text(encoding="utf-8").strip() == "sk-clb-secret"

    def test_idempotent(self, tmp_path, spec):
        home = tmp_path / "h"
        init_home(home, spec)
        first = (home / "config.toml").read_text(encoding="utf-8")
        init_home(home, spec)
        assert (home / "config.toml").read_text(encoding="utf-8") == first


class TestReadKey:
    def test_env_wins_over_file(self, tmp_path, monkeypatch, spec):
        home = tmp_path / "h"
        init_home(home, spec, key="from-file")
        monkeypatch.setenv("GW_API_KEY", "from-env")
        assert read_key(home) == "from-env"

    def test_falls_back_to_file(self, tmp_path, spec):
        home = tmp_path / "h"
        init_home(home, spec, key="from-file")
        assert read_key(home) == "from-file"

    def test_none_when_absent(self, tmp_path):
        assert read_key(tmp_path) is None


class TestPrepareEnv:
    def test_standalone_sets_codex_home(self, tmp_path, monkeypatch, spec):
        home = tmp_path / "h"
        init_home(home, spec, key="sk-clb-x")
        monkeypatch.setenv("MCODEX_HOME", str(home))

        updates = prepare_env(spec)

        assert updates["CODEX_HOME"] == str(home)
        assert updates["GW_API_KEY"] == "sk-clb-x"

    def test_standalone_without_init_errors(self, tmp_path, monkeypatch, spec):
        monkeypatch.setenv("MCODEX_HOME", str(tmp_path / "missing"))
        with pytest.raises(McodexError, match="尚未初始化"):
            prepare_env(spec)

    def test_daemon_home_is_respected_not_overridden(self, tmp_path, monkeypatch, spec):
        """multica 场景：daemon 的 CODEX_HOME 必须保留，只补 provider。"""
        mhome = tmp_path / "mcodex-home"
        init_home(mhome, spec, key="sk-clb-x")
        monkeypatch.setenv("MCODEX_HOME", str(mhome))

        task_home = tmp_path / "per-task"
        task_home.mkdir()
        (task_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(task_home))

        updates = prepare_env(spec)

        # 不能覆盖 daemon 设定的 CODEX_HOME
        assert "CODEX_HOME" not in updates
        # provider 应被注入 per-task 配置
        data = tomllib.loads((task_home / "config.toml").read_text(encoding="utf-8"))
        assert data["model_provider"] == "gw"
        assert data["model"] == "gpt-5.5"  # daemon 原有内容保留

    def test_daemon_scene_takes_key_from_mcodex_home(self, tmp_path, monkeypatch, spec):
        mhome = tmp_path / "mcodex-home"
        init_home(mhome, spec, key="sk-clb-fallback")
        monkeypatch.setenv("MCODEX_HOME", str(mhome))

        task_home = tmp_path / "per-task"
        task_home.mkdir()
        (task_home / "config.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(task_home))

        assert prepare_env(spec)["GW_API_KEY"] == "sk-clb-fallback"

    def test_existing_key_env_not_overwritten(self, tmp_path, monkeypatch, spec):
        """custom_env 下发的密钥优先，不被家目录密钥覆盖。"""
        mhome = tmp_path / "h"
        init_home(mhome, spec, key="sk-clb-file")
        monkeypatch.setenv("MCODEX_HOME", str(mhome))
        monkeypatch.setenv("GW_API_KEY", "sk-clb-from-multica")

        updates = prepare_env(spec)
        assert "GW_API_KEY" not in updates  # 已存在，无需改动

    def test_missing_key_errors_with_guidance(self, tmp_path, monkeypatch, spec):
        home = tmp_path / "h"
        init_home(home, spec)  # 不给密钥
        monkeypatch.setenv("MCODEX_HOME", str(home))

        with pytest.raises(McodexError, match="poolctl issue"):
            prepare_env(spec)

    def test_task_mode_uses_bound_session_key(self, tmp_path, monkeypatch, spec):
        task_home = tmp_path / "per-task"
        task_home.mkdir()
        (task_home / "config.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(task_home))
        monkeypatch.setenv("GW_API_KEY", "legacy-static-key")
        monkeypatch.setenv("MULTICA_TOKEN", "mat_task-secret")
        monkeypatch.setenv("MULTICA_TASK_ID", "task-1")
        monkeypatch.setenv("MULTICA_AGENT_ID", "agent-1")
        monkeypatch.setenv("MULTICA_WORKSPACE_ID", "workspace-1")

        def handler(request):
            assert request.headers["Authorization"] == "Bearer mat_task-secret"
            assert request.read() == (
                b'{"task_id":"task-1","agent_id":"agent-1",'
                b'"workspace_id":"workspace-1"}'
            )
            return httpx.Response(200, json={"key": "mcx_bound-secret"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            updates = prepare_env(spec, "https://pool.example/session-key", client=client)

        assert updates["GW_API_KEY"] == "mcx_bound-secret"
        assert "CODEX_HOME" not in updates

    def test_task_mode_rejects_standalone_use(self, tmp_path, monkeypatch, spec):
        home = tmp_path / "h"
        monkeypatch.setenv("MCODEX_HOME", str(home))

        with pytest.raises(McodexError, match="只允许在 Multica 任务"):
            prepare_env(spec, "https://pool.example/session-key")


class TestVersionProbe:
    def test_only_exact_version_probe_bypasses_task_context(self):
        assert is_version_probe(["--version"])
        assert is_version_probe(["-V"])
        assert not is_version_probe([])
        assert not is_version_probe(["--version", "app-server"])
        assert not is_version_probe(["app-server", "--version"])


class TestRequestSessionKey:
    def test_requires_complete_task_context(self):
        with pytest.raises(McodexError, match="MULTICA_TOKEN"):
            request_session_key("https://pool.example/session-key")

    def test_rejects_personal_access_token(self, monkeypatch):
        monkeypatch.setenv("MULTICA_TOKEN", "mul_personal")
        monkeypatch.setenv("MULTICA_TASK_ID", "task-1")
        monkeypatch.setenv("MULTICA_AGENT_ID", "agent-1")
        monkeypatch.setenv("MULTICA_WORKSPACE_ID", "workspace-1")

        with pytest.raises(McodexError, match="mat_ 开头"):
            request_session_key("https://pool.example/session-key")

    def test_redacts_server_response_details(self, monkeypatch):
        monkeypatch.setenv("MULTICA_TOKEN", "mat_task-secret")
        monkeypatch.setenv("MULTICA_TASK_ID", "task-1")
        monkeypatch.setenv("MULTICA_AGENT_ID", "agent-1")
        monkeypatch.setenv("MULTICA_WORKSPACE_ID", "workspace-1")

        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(403, json={"error": "task binding mismatch"})
            )
        ) as client:
            with pytest.raises(McodexError, match="task binding mismatch") as caught:
                request_session_key("https://pool.example/session-key", client=client)

        assert "mat_task-secret" not in str(caught.value)


def _make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class TestFindRealCodex:
    def test_explicit_override(self, tmp_path, monkeypatch):
        fake = _make_exe(tmp_path / "codex")
        monkeypatch.setenv("MCODEX_REAL_CODEX", str(fake))
        assert find_real_codex(tmp_path) == str(fake)

    def test_explicit_override_missing_errors(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MCODEX_REAL_CODEX", str(tmp_path / "nope"))
        with pytest.raises(McodexError, match="不存在"):
            find_real_codex(tmp_path)

    def test_skips_self_to_avoid_recursion(self, tmp_path, monkeypatch):
        """PATH 里排在前面的若是本封装自身，必须跳过。"""
        me = _make_exe(tmp_path / "a" / "codex")
        real = _make_exe(tmp_path / "b" / "codex")

        monkeypatch.setenv("PATH", f"{me.parent}{os.pathsep}{real.parent}")
        monkeypatch.setattr("sys.argv", [str(me)])

        assert find_real_codex(tmp_path / "empty-home") == str(real)

    def test_not_found_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
        monkeypatch.setattr("sys.argv", ["mcodex"])
        with pytest.raises(McodexError, match="找不到 codex"):
            find_real_codex(tmp_path / "empty-home")


class TestPinnedCodexPath:
    """固化路径必须战胜 PATH —— 否则 daemon 与 shell 会选到不同二进制。"""

    def test_pinned_wins_over_path(self, tmp_path, monkeypatch, spec):
        pinned = _make_exe(tmp_path / "pinned" / "codex")
        other = _make_exe(tmp_path / "onpath" / "codex")

        home = tmp_path / "h"
        init_home(home, spec, real_codex=str(pinned))

        # PATH 里放另一个 codex，不能被选中
        monkeypatch.setenv("PATH", str(other.parent))
        monkeypatch.setattr("sys.argv", ["mcodex"])

        assert find_real_codex(home) == str(pinned)

    def test_env_override_beats_pinned(self, tmp_path, monkeypatch, spec):
        pinned = _make_exe(tmp_path / "pinned" / "codex")
        override = _make_exe(tmp_path / "override" / "codex")

        home = tmp_path / "h"
        init_home(home, spec, real_codex=str(pinned))
        monkeypatch.setenv("MCODEX_REAL_CODEX", str(override))

        assert find_real_codex(home) == str(override)

    def test_init_autodetects_and_pins(self, tmp_path, monkeypatch, spec):
        found = _make_exe(tmp_path / "bin" / "codex")
        monkeypatch.setenv("PATH", str(found.parent))
        monkeypatch.setattr("sys.argv", ["mcodex"])

        home = tmp_path / "h"
        info = init_home(home, spec)

        assert info["real_codex"] == str(found)
        assert (home / "real-codex").read_text(encoding="utf-8").strip() == str(found)

    def test_stale_pin_errors_loudly(self, tmp_path, monkeypatch, spec):
        """固化的 codex 消失时必须报错，不能静默改用别的。"""
        pinned = _make_exe(tmp_path / "pinned" / "codex")
        home = tmp_path / "h"
        init_home(home, spec, real_codex=str(pinned))
        pinned.unlink()

        fallback = _make_exe(tmp_path / "onpath" / "codex")
        monkeypatch.setenv("PATH", str(fallback.parent))
        monkeypatch.setattr("sys.argv", ["mcodex"])

        with pytest.raises(McodexError, match="已不可用"):
            find_real_codex(home)

    def test_non_executable_real_codex_rejected(self, tmp_path, spec):
        plain = tmp_path / "not-exe"
        plain.write_text("x")
        with pytest.raises(McodexError, match="不可执行"):
            init_home(tmp_path / "h", spec, real_codex=str(plain))

    def test_symlink_is_not_dereferenced(self, tmp_path, spec):
        """nvm/npm 的 bin/codex 本身是软链，固化时必须保留软链路径。

        跟随软链会绑到底层的 codex.js 或某个具体版本目录，codex 升级后即失效。
        """
        target = _make_exe(tmp_path / "impl" / "codex.js")
        link = tmp_path / "bin" / "codex"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)

        home = tmp_path / "h"
        info = init_home(home, spec, real_codex=str(link))

        assert info["real_codex"] == str(link)
        assert "codex.js" not in info["real_codex"]


class TestMcodexHome:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("MCODEX_HOME", raising=False)
        assert mcodex_home().name == ".mcodex"

    def test_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MCODEX_HOME", str(tmp_path))
        assert mcodex_home() == tmp_path


class TestDoctorModeDetection:
    """doctor 必须跟得上产品形态：mcodex 模式下别再去查 ~/.codex。"""

    def test_detects_mcodex_when_home_exists(self, tmp_path, monkeypatch, spec):
        from corp_codex_pool.doctor import detect_mode

        home = tmp_path / "mhome"
        init_home(home, spec)
        monkeypatch.setenv("MCODEX_HOME", str(home))

        mode, path = detect_mode()
        assert mode == "mcodex"
        assert path == home / "config.toml"

    def test_falls_back_to_host_config(self, tmp_path, monkeypatch):
        from corp_codex_pool.doctor import detect_mode

        monkeypatch.setenv("MCODEX_HOME", str(tmp_path / "never-created"))
        mode, path = detect_mode()
        assert mode == "宿主注入"
        assert path.name == "config.toml"

    def test_explicit_path_wins(self, tmp_path, monkeypatch, spec):
        from corp_codex_pool.doctor import detect_mode

        home = tmp_path / "mhome"
        init_home(home, spec)
        monkeypatch.setenv("MCODEX_HOME", str(home))

        explicit = tmp_path / "somewhere" / "config.toml"
        mode, path = detect_mode(explicit)
        assert mode == "指定路径"
        assert path == explicit

    def test_missing_key_is_flagged(self, tmp_path, monkeypatch, spec):
        from corp_codex_pool.doctor import FAIL, check_mcodex_home

        home = tmp_path / "mhome"
        init_home(home, spec)  # 不给密钥
        monkeypatch.setenv("MCODEX_HOME", str(home))

        checks = check_mcodex_home("GW_API_KEY")
        key_check = next(c for c in checks if c.name == "mcodex 密钥")
        assert key_check.status == FAIL

    def test_key_from_env_counts(self, tmp_path, monkeypatch, spec):
        from corp_codex_pool.doctor import OK, check_mcodex_home

        home = tmp_path / "mhome"
        init_home(home, spec)
        monkeypatch.setenv("MCODEX_HOME", str(home))
        monkeypatch.setenv("GW_API_KEY", "sk-clb-x")

        checks = check_mcodex_home("GW_API_KEY")
        assert next(c for c in checks if c.name == "mcodex 密钥").status == OK

    def test_stale_pin_is_flagged(self, tmp_path, monkeypatch, spec):
        from corp_codex_pool.doctor import FAIL, check_mcodex_home

        exe = _make_exe(tmp_path / "bin" / "codex")
        home = tmp_path / "mhome"
        init_home(home, spec, key="k", real_codex=str(exe))
        monkeypatch.setenv("MCODEX_HOME", str(home))
        exe.unlink()

        checks = check_mcodex_home("GW_API_KEY")
        pin = next(c for c in checks if c.name == "mcodex 固化的 codex")
        assert pin.status == FAIL

    def test_no_checks_when_mcodex_unused(self, tmp_path, monkeypatch):
        from corp_codex_pool.doctor import check_mcodex_home

        monkeypatch.setenv("MCODEX_HOME", str(tmp_path / "nope"))
        assert check_mcodex_home("GW_API_KEY") == []

    def test_task_bound_mode_does_not_require_static_key(self, tmp_path, monkeypatch, spec):
        from corp_codex_pool.doctor import OK, check_mcodex_home

        init_home(tmp_path, spec)
        monkeypatch.setenv("MCODEX_HOME", str(tmp_path))

        checks = check_mcodex_home(
            "GW_API_KEY", "https://codex.chekkk.com/api/self-service/session-key"
        )

        credential = next(c for c in checks if c.name == "mcodex 任务绑定凭证")
        assert credential.status == OK
        assert "不写磁盘" in credential.detail

"""multica 客户端：读身份/agent/runtime，向 agent 下发 custom_env。

所有端点都要求 workspace_id（或 workspace_slug）query 参数，缺了返回 400。
字段命名是 snake_case。

密钥下发走 custom_env：daemon 会把它铺到 codex 子进程环境
(server/internal/daemon/daemon.go:6103-6112 -> :7603-7616)，而 isBlockedEnvKey
(:7549-7559) 只拦 MULTICA_* 与 HOME/PATH/CODEX_HOME 等，不拦自定义 KEY 名。

已知语义泄漏，写进运维守则而不是靠代码兜：
- custom_env 里的键会进 codex 的 shell_environment_policy.include_only，
  即 agent 自己的 bash 能读到这个密钥。号池密钥只是入场券，可即时吊销，
  但若安全评审不接受，需要改 multica 上游走 daemon 侧 agentEnv。
- POST /api/agents 允许任意 workspace member 写 custom_env。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class MulticaError(RuntimeError):
    pass


@dataclass
class MulticaIdentity:
    id: str
    name: str
    email: str


def load_local_config(path: Path | None = None) -> dict[str, Any]:
    """读取 ~/.multica/config.json（CLI 登录后生成）。"""
    path = path or Path.home() / ".multica" / "config.json"
    if not path.exists():
        raise MulticaError(f"找不到 multica 配置：{path}。请先运行 multica login。")
    return json.loads(path.read_text(encoding="utf-8"))


def persist_codex_binary_path(
    binary_path: str | Path,
    config_path: Path | None = None,
) -> bool:
    """Persist the daemon's Codex executable without dropping other config.

    Newer Multica daemons read ``backends.codex.binary_path`` directly. The
    pool wrapper still exports ``MULTICA_CODEX_PATH`` for compatibility with
    older daemons, but persisting the same path makes a later plain
    ``multica daemon start`` retain mcodex.
    """
    binary = Path(binary_path).expanduser()
    if not binary.is_absolute():
        raise MulticaError(f"mcodex 路径必须是绝对路径：{binary}")

    path = config_path or Path.home() / ".multica" / "config.json"
    config = load_local_config(path)
    backends = config.setdefault("backends", {})
    if not isinstance(backends, dict):
        raise MulticaError("multica 配置中 backends 必须是对象")
    codex = backends.setdefault("codex", {})
    if not isinstance(codex, dict):
        raise MulticaError("multica 配置中 backends.codex 必须是对象")

    normalized = str(binary.resolve(strict=False))
    if codex.get("binary_path") == normalized:
        return False
    codex["binary_path"] = normalized

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".config-",
            suffix=".json.tmp",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            json.dump(config, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise MulticaError(f"写入 multica 配置失败：{exc}") from exc
    return True


class MulticaClient:
    def __init__(self, server_url: str, token: str, workspace_id: str, timeout: float = 30.0):
        if not token:
            raise MulticaError("缺少 multica token")
        if not workspace_id:
            raise MulticaError("缺少 workspace_id")
        self._workspace_id = workspace_id
        self._client = httpx.Client(
            base_url=server_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            follow_redirects=True,
        )

    @classmethod
    def from_local_config(cls, path: Path | None = None, workspace_id: str | None = None):
        cfg = load_local_config(path)
        return cls(
            server_url=cfg["server_url"],
            token=cfg["token"],
            workspace_id=workspace_id or cfg["workspace_id"],
        )

    def __enter__(self) -> "MulticaClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def _request(self, method: str, path: str, *, json_body: Any = None, workspace: bool = True) -> Any:
        params = {"workspace_id": self._workspace_id} if workspace else None
        response = self._client.request(method, path, params=params, json=json_body)
        if response.status_code >= 400:
            raise MulticaError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # ---------------------------------------------------------------- 读

    def me(self) -> MulticaIdentity:
        data = self._request("GET", "/api/me", workspace=False)
        return MulticaIdentity(id=data["id"], name=data.get("name", ""), email=data.get("email", ""))

    def workspaces(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/workspaces", workspace=False) or []

    def agents(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/agents") or []

    def agent(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/agents/{agent_id}")

    def runtimes(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/runtimes") or []

    def get_env(self, agent_id: str) -> dict[str, str]:
        data = self._request("GET", f"/api/agents/{agent_id}/env") or {}
        return data.get("custom_env") or {}

    # ---------------------------------------------------------------- 写

    def set_env(
        self,
        agent_id: str,
        updates: dict[str, str],
        *,
        merge: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """写入 custom_env。

        默认 merge：只改传入的键，保留其余。merge=False 则整体替换。
        返回 {"before":…, "after":…, "changed":bool, "applied":bool}，
        值一律打码，调用方可以安全打印。
        """
        before = self.get_env(agent_id)
        after = ({**before, **updates}) if merge else dict(updates)
        changed = after != before

        if changed and not dry_run:
            self._request("PUT", f"/api/agents/{agent_id}/env", json_body={"custom_env": after})

        return {
            "before": {k: _mask(v) for k, v in before.items()},
            "after": {k: _mask(v) for k, v in after.items()},
            "changed": changed,
            "applied": changed and not dry_run,
        }

    def unset_env(self, agent_id: str, keys: list[str], *, dry_run: bool = False) -> dict[str, Any]:
        before = self.get_env(agent_id)
        after = {k: v for k, v in before.items() if k not in keys}
        changed = after != before

        if changed and not dry_run:
            self._request("PUT", f"/api/agents/{agent_id}/env", json_body={"custom_env": after})

        return {
            "before": {k: _mask(v) for k, v in before.items()},
            "after": {k: _mask(v) for k, v in after.items()},
            "changed": changed,
            "applied": changed and not dry_run,
        }


def _mask(value: str) -> str:
    """密钥类值只保留可辨识的前缀。"""
    if not isinstance(value, str) or len(value) <= 12:
        return "<已设置>"
    return f"{value[:11]}…<{len(value)} 字符>"

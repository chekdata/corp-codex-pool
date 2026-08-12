"""配置加载。优先级：显式参数 > 环境变量 > .env 文件 > 默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """读取 .env。不覆盖已存在的环境变量，便于临时覆写。"""
    path = path or Path.cwd() / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass
class Settings:
    pool_admin_url: str = "https://pool.chekkk.com"
    pool_admin_token: str = ""
    pool_admin_password: str = ""
    pool_base_url: str = "https://codex.chekkk.com/v1"
    multica_server_url: str = "https://api.multica.ai"
    multica_token: str = ""
    provider_id: str = "gw"
    env_key: str = "GW_API_KEY"

    @classmethod
    def load(cls, dotenv: Path | None = None) -> "Settings":
        file_values = load_dotenv(dotenv)

        def get(name: str, default: str) -> str:
            return os.environ.get(name) or file_values.get(name) or default

        return cls(
            pool_admin_url=get("POOL_ADMIN_URL", cls.pool_admin_url).rstrip("/"),
            pool_admin_token=get("POOL_ADMIN_TOKEN", ""),
            pool_admin_password=get("POOL_ADMIN_PASSWORD", ""),
            pool_base_url=get("POOL_BASE_URL", cls.pool_base_url).rstrip("/"),
            multica_server_url=get("MULTICA_SERVER_URL", cls.multica_server_url).rstrip("/"),
            multica_token=get("MULTICA_TOKEN", ""),
            provider_id=get("POOL_PROVIDER_ID", cls.provider_id),
            env_key=get("POOL_ENV_KEY", cls.env_key),
        )

    def redacted(self) -> dict[str, str]:
        """用于展示：任何疑似凭据的字段一律打码。"""
        out = {}
        for key, value in self.__dict__.items():
            if any(s in key for s in ("token", "password", "secret")):
                out[key] = f"<已设置 {len(value)} 字符>" if value else "<未设置>"
            else:
                out[key] = value
        return out

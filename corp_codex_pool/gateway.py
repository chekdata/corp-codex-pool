"""codex-lb 号池网关客户端。

管理面是 cookie session：POST /api/dashboard-auth/password/login 换 cookie，
后续管理请求带 cookie。Bearer token 只用于 /v1/* 的数据面，管理面不认。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


class GatewayError(RuntimeError):
    pass


class AuthRequired(GatewayError):
    pass


@dataclass
class IssuedKey:
    id: str
    name: str
    key_prefix: str
    secret: str | None  # 明文密钥只在创建时返回一次

    @property
    def has_secret(self) -> bool:
        return bool(self.secret)


class GatewayClient:
    """管理面客户端。密钥明文只在 create 的返回值里出现，不落日志。"""

    def __init__(self, base_url: str, password: str = "", timeout: float = 30.0):
        self._base = base_url.rstrip("/")
        self._password = password
        self._client = httpx.Client(base_url=self._base, timeout=timeout, follow_redirects=True)
        self._authed = False

    def __enter__(self) -> "GatewayClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- 认证

    def session(self) -> dict[str, Any]:
        return self._request("GET", "/api/dashboard-auth/session", auth=False)

    def login(self) -> None:
        """按需登录。已认证则跳过。"""
        state = self.session()
        if state.get("authenticated"):
            self._authed = True
            return

        if not state.get("passwordRequired"):
            self._authed = True
            return

        if not self._password:
            raise AuthRequired(
                "管理面需要密码。请在 .env 设置 POOL_ADMIN_PASSWORD，"
                "或用 --password 传入。"
            )

        self._request(
            "POST",
            "/api/dashboard-auth/password/login",
            json={"password": self._password},
            auth=False,
        )
        self._authed = True

    # ---------------------------------------------------------------- 请求

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        auth: bool = True,
    ) -> Any:
        if auth and not self._authed:
            self.login()

        response = self._client.request(method, path, json=json, params=params)

        if response.status_code == 401:
            raise AuthRequired(f"{method} {path} 需要认证（401）")
        if response.status_code >= 400:
            raise GatewayError(
                f"{method} {path} -> {response.status_code}: {response.text[:300]}"
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # ---------------------------------------------------------------- 密钥

    def list_keys(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/api-keys/")
        if isinstance(data, list):
            return data
        for field in ("items", "keys", "apiKeys", "data"):
            if isinstance(data, dict) and isinstance(data.get(field), list):
                return data[field]
        return []

    def find_key(self, name: str) -> dict[str, Any] | None:
        return next((k for k in self.list_keys() if k.get("name") == name), None)

    def create_key(
        self,
        name: str,
        *,
        weekly_token_limit: int | None = None,
        allowed_models: list[str] | None = None,
        expires_at: datetime | None = None,
        traffic_class: str | None = None,
    ) -> IssuedKey:
        """签发一把密钥。字段名对应 ApiKeyCreateRequest。"""
        payload: dict[str, Any] = {"name": name}
        if weekly_token_limit is not None:
            payload["weeklyTokenLimit"] = weekly_token_limit
        if allowed_models:
            payload["allowedModels"] = allowed_models
        if expires_at is not None:
            payload["expiresAt"] = expires_at.astimezone(timezone.utc).isoformat()
        if traffic_class:
            payload["trafficClass"] = traffic_class

        data = self._request("POST", "/api/api-keys/", json=payload)
        secret = data.get("key")
        if not secret:
            raise GatewayError(
                "创建成功但响应里没有明文密钥（字段 key）。"
                "密钥只在创建时返回一次，请检查网关版本。"
            )
        return IssuedKey(
            id=data["id"],
            name=data.get("name", name),
            key_prefix=data.get("keyPrefix", ""),
            secret=secret,
        )

    def revoke_key(self, key_id: str) -> None:
        self._request("DELETE", f"/api/api-keys/{key_id}")

    def set_key_active(self, key_id: str, active: bool) -> dict[str, Any]:
        return self._request("PATCH", f"/api/api-keys/{key_id}", json={"isActive": active})

    # ---------------------------------------------------------------- 用量

    def key_usage_7d(self, key_id: str) -> Any:
        return self._request("GET", f"/api/api-keys/{key_id}/usage-7d")

    def request_logs(
        self,
        *,
        api_key_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
        max_records: int | None = None,
    ) -> list[dict[str, Any]]:
        """分页拉取请求日志。

        网关单页上限是 1000，超过会返回 422，因此这里固定按 offset 翻页。
        """
        limit = min(limit, 1000)
        collected: list[dict[str, Any]] = []
        offset = 0

        while True:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if api_key_id:
                params["apiKeyId"] = api_key_id
            if since:
                params["since"] = since
            if until:
                params["until"] = until

            page = self._request("GET", "/api/request-logs", params=params) or {}
            rows = page.get("requests") or []
            collected.extend(rows)

            if max_records and len(collected) >= max_records:
                return collected[:max_records]
            if not page.get("hasMore") or not rows:
                return collected
            offset += len(rows)

    def accounts(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/accounts")
        if isinstance(data, list):
            return data
        for field in ("items", "accounts", "data"):
            if isinstance(data, dict) and isinstance(data.get(field), list):
                return data[field]
        return []


#: Responses API 的 request id 形如 `resp_<16 位会话标识><逐请求后缀>`。
#: 同一会话的多次请求共享这 16 位。
#:
#: ⚠️ 这是实测逆向出来的规律，不是上游文档化的契约，可能随版本变化。
#: 仅用于把请求聚成会话做展示与粗对账，不要用于计费或权限判断。
#: 实测样本（codex-cli 0.147.0）：
#:   multica task task-A 的 4 次请求 -> resp_aaaaaaaaaaaaaaaa…
#:   multica task task-B 的 3 次请求 -> resp_bbbbbbbbbbbbbbbb…
_SESSION_PREFIX_LEN = 16


def session_key(row: dict[str, Any]) -> str | None:
    """推断一条请求所属的会话。

    优先用网关自己的 conversationId；它对 app-server 模式的请求为空
    （codex 以 app-server 运行时不带该头），此时退回 requestId 前缀。
    """
    conversation = row.get("conversationId")
    if conversation:
        return str(conversation)

    request_id = row.get("requestId")
    if isinstance(request_id, str) and request_id.startswith("resp_"):
        body = request_id[len("resp_"):]
        if len(body) >= _SESSION_PREFIX_LEN:
            return "resp_" + body[:_SESSION_PREFIX_LEN]
    return None


def summarize_by_session(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按会话归集，用于看"一次任务花了多少"。

    按首次请求时间倒序返回。
    """
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = session_key(row)
        if key is None:
            continue
        bucket = buckets.setdefault(
            key,
            {
                "session": key,
                "apiKeyName": row.get("apiKeyName"),
                "requests": 0,
                "inputTokens": 0,
                "cachedInputTokens": 0,
                "outputTokens": 0,
                "costUsd": 0.0,
                "firstAt": row.get("requestedAt"),
                "lastAt": row.get("requestedAt"),
                "models": set(),
                "sourceIsApp": "multica" in str(row.get("useragent") or ""),
            },
        )
        bucket["requests"] += 1
        for field in ("inputTokens", "cachedInputTokens", "outputTokens"):
            bucket[field] += row.get(field) or 0
        bucket["costUsd"] += row.get("costUsd") or 0.0
        if row.get("model"):
            bucket["models"].add(row["model"])
        stamp = row.get("requestedAt")
        if stamp:
            bucket["firstAt"] = min(bucket["firstAt"] or stamp, stamp)
            bucket["lastAt"] = max(bucket["lastAt"] or stamp, stamp)

    for bucket in buckets.values():
        bucket["models"] = sorted(bucket.pop("models"))
        bucket["costUsd"] = round(bucket["costUsd"], 6)
        total = bucket["inputTokens"]
        bucket["cacheHitRate"] = (
            round(bucket["cachedInputTokens"] / total, 4) if total else None
        )

    return sorted(buckets.values(), key=lambda b: b["firstAt"] or "", reverse=True)


def summarize_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把请求日志按 apiKeyId 归集。

    这是「按人计量」的实现：一把密钥对应一个员工，密钥归属由本工具签发时确定。
    """
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        key_id = row.get("apiKeyId") or "<无密钥>"
        bucket = summary.setdefault(
            key_id,
            {
                "apiKeyId": key_id,
                "apiKeyName": row.get("apiKeyName"),
                "requests": 0,
                "inputTokens": 0,
                "cachedInputTokens": 0,
                "outputTokens": 0,
                "reasoningTokens": 0,
                "costUsd": 0.0,
                "conversations": set(),
            },
        )
        bucket["requests"] += 1
        for field in ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningTokens"):
            bucket[field] += row.get(field) or 0
        bucket["costUsd"] += row.get("costUsd") or 0.0
        if row.get("conversationId"):
            bucket["conversations"].add(row["conversationId"])

    for bucket in summary.values():
        bucket["conversationCount"] = len(bucket.pop("conversations"))
        bucket["costUsd"] = round(bucket["costUsd"], 6)
        total_input = bucket["inputTokens"]
        bucket["cacheHitRate"] = (
            round(bucket["cachedInputTokens"] / total_input, 4) if total_input else None
        )
    return summary

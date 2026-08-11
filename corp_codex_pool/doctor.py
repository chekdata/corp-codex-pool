"""体检：逐项验证号池链路是否真的成立，而不是"配置看起来对"。

每项返回 Check，失败带可执行的修复建议。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .codex_config import default_config_path

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

_SYMBOL = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        text = f"{_SYMBOL[self.status]} {self.name}"
        if self.detail:
            text += f"\n    {self.detail}"
        if self.fix and self.status in (FAIL, WARN):
            text += f"\n    → {self.fix}"
        return text


def check_gateway_reachable(base_url: str, timeout: float = 10.0) -> Check:
    """网关必须实现 Responses API：codex 只认 wire_api = "responses"。"""
    root = base_url.rstrip("/")
    try:
        # POST 不带鉴权应返回 401（存在且要鉴权），404 说明没实现
        response = httpx.post(f"{root}/responses", json={}, timeout=timeout)
    except httpx.HTTPError as exc:
        return Check(
            "网关可达性",
            FAIL,
            f"无法连接 {root}：{exc}",
            "确认 codex-lb 已启动，且 POOL_BASE_URL 指向 daemon 宿主机可达的地址",
        )

    if response.status_code == 404:
        return Check(
            "网关 Responses 端点",
            FAIL,
            f"POST {root}/responses 返回 404",
            "codex 已移除 chat/completions 兼容（wire_api 只接受 responses），"
            "网关必须实现 Responses API",
        )
    return Check(
        "网关 Responses 端点",
        OK,
        f"POST {root}/responses -> {response.status_code}（存在）",
        data={"status": response.status_code},
    )


def check_models_endpoint(base_url: str, timeout: float = 10.0) -> Check:
    """/models 缺失不致命：刷新失败会回落到缓存或静态目录，但建议实现。"""
    root = base_url.rstrip("/")
    try:
        response = httpx.get(f"{root}/models", timeout=timeout)
    except httpx.HTTPError as exc:
        return Check("网关 /models", WARN, f"探测失败：{exc}", "非致命，codex 会回落到模型缓存")

    if response.status_code == 404:
        return Check(
            "网关 /models",
            WARN,
            "未实现",
            "非致命。可在 config 里配 model_catalog_json 兜底",
        )
    return Check("网关 /models", OK, f"-> {response.status_code}")


def detect_mode(config_path: Path | None = None) -> tuple[str, Path]:
    """判断当前用的是哪种接入方式，返回 (模式名, 要检查的配置路径)。

    两种方式互斥但可共存，优先报告 mcodex —— 它是推荐方式，
    且不影响用户自己的 codex。
    """
    if config_path is not None:
        return "指定路径", config_path

    from .mcodex import mcodex_home

    mcodex_config = mcodex_home() / "config.toml"
    if mcodex_config.exists():
        return "mcodex", mcodex_config
    return "宿主注入", default_config_path()


def check_mcodex_home(env_key: str) -> list[Check]:
    """mcodex 模式特有的检查：密钥来源与 codex 路径固化。"""
    import os

    from .mcodex import KEY_FILENAME, REAL_CODEX_FILENAME, mcodex_home

    home = mcodex_home()
    if not (home / "config.toml").exists():
        return []

    checks = []

    has_key_file = (home / KEY_FILENAME).exists()
    has_key_env = bool(os.environ.get(env_key))
    if has_key_file or has_key_env:
        source = []
        if has_key_env:
            source.append(f"环境变量 {env_key}")
        if has_key_file:
            source.append(f"{home / KEY_FILENAME}")
        checks.append(Check("mcodex 密钥", OK, "、".join(source)))
    else:
        checks.append(
            Check(
                "mcodex 密钥",
                FAIL,
                f"既无环境变量 {env_key}，{home / KEY_FILENAME} 也不存在",
                "poolctl mcodex init --key-stdin",
            )
        )

    pinned = home / REAL_CODEX_FILENAME
    if pinned.exists():
        target = pinned.read_text(encoding="utf-8").strip()
        usable = Path(target).is_file() and os.access(target, os.X_OK)
        checks.append(
            Check(
                "mcodex 固化的 codex",
                OK if usable else FAIL,
                target,
                "记录的路径已失效，重新固定："
                "poolctl mcodex init --real-codex <绝对路径>",
            )
        )
    else:
        checks.append(
            Check(
                "mcodex 固化的 codex",
                WARN,
                "未固化，运行时靠 PATH 现找",
                "daemon 与登录 shell 的 PATH 常常不同，可能选到不同的 codex。"
                "建议 poolctl mcodex init --real-codex <绝对路径>",
            )
        )

    return checks


def check_codex_config(
    provider_id: str,
    expected_base_url: str,
    env_key: str,
    path: Path | None = None,
) -> list[Check]:
    """校验 provider 定义。自动识别 mcodex 模式与宿主注入模式。"""
    mode, path = detect_mode(path)
    checks: list[Check] = [Check("接入方式", OK, f"{mode}（{path}）")]

    if not path.exists():
        return [
            Check(
                "codex 配置文件",
                FAIL,
                f"不存在：{path}",
                "用 mcodex：poolctl mcodex init --key-stdin\n"
                "    或改宿主配置：poolctl setup",
            )
        ]

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [Check("codex 配置文件", FAIL, f"TOML 解析失败：{exc}", "修复语法或从备份恢复")]

    provider = (data.get("model_providers") or {}).get(provider_id)
    if not provider:
        checks.append(
            Check(
                f"provider [{provider_id}]",
                FAIL,
                f"{path} 中找不到 model_providers.{provider_id}",
                "用 mcodex：poolctl mcodex init --key-stdin\n"
                "    或改宿主配置：poolctl setup",
            )
        )
        return checks

    checks.append(Check(f"provider [{provider_id}] 已注册", OK, str(path)))

    wire = provider.get("wire_api")
    checks.append(
        Check(
            "wire_api",
            OK if wire == "responses" else FAIL,
            f"= {wire!r}",
            "必须是 responses；codex 已移除 chat 兼容",
        )
    )

    actual_url = provider.get("base_url")
    checks.append(
        Check(
            "base_url",
            OK if actual_url == expected_base_url else WARN,
            f"配置 {actual_url!r}，期望 {expected_base_url!r}",
            "两者不一致时以配置文件为准，确认是否有意为之",
        )
    )

    configured_env_key = provider.get("env_key")
    checks.append(
        Check(
            "env_key",
            OK if configured_env_key == env_key else FAIL,
            f"= {configured_env_key!r}，期望 {env_key!r}",
            "env_key 与实际下发的环境变量名必须一致，否则 codex 取不到密钥",
        )
    )

    default_provider = data.get("model_provider")
    checks.append(
        Check(
            "默认 provider",
            OK if default_provider == provider_id else WARN,
            f"顶层 model_provider = {default_provider!r}",
            f"未指向 {provider_id} 时，codex 仍走原有上游。"
            "若这是有意的（只注册不切换）可忽略",
        )
    )

    headers = provider.get("env_http_headers") or {}
    if headers:
        checks.append(
            Check(
                "对账请求头",
                OK,
                ", ".join(f"{h}<-${v}" for h, v in headers.items()),
                data={"headers": headers},
            )
        )
    else:
        checks.append(
            Check(
                "对账请求头",
                WARN,
                "未配置 env_http_headers",
                "不影响按人计量（按密钥归属），仅影响按 task 归集",
            )
        )

    return checks


#: 探测用模型。实测上游各模型延迟差异很大（同一时段 gpt-5.5 要 180~400 秒，
#: gpt-5.6-sol 只要 4~60 秒），所以默认挑快的那个，避免体检卡住。
PROBE_MODEL = "gpt-5.6-sol"

#: 上游偶发劣化时单次请求可能要几分钟，超时给宽些，
#: 否则会把"上游慢"误报成"密钥不可用"。
PROBE_TIMEOUT = 240.0


def check_key_works(
    base_url: str,
    api_key: str,
    timeout: float = PROBE_TIMEOUT,
    model: str = PROBE_MODEL,
) -> Check:
    """用真实密钥打一次最小请求，确认端到端可用。"""
    if not api_key:
        return Check("密钥可用性", SKIP, "未提供密钥")

    root = base_url.rstrip("/")
    payload = {
        "model": model,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
        "stream": False,
        "max_output_tokens": 16,
    }
    try:
        response = httpx.post(
            f"{root}/responses",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except httpx.ReadTimeout:
        # 超时不代表密钥有问题：网关侧这类请求往往最终仍是 ok，
        # 只是耗时超过了客户端等待。报成 WARN 并说清如何区分。
        return Check(
            "密钥可用性",
            WARN,
            f"{timeout:.0f} 秒内未返回（模型 {model}）",
            "多半是上游延迟劣化而非密钥问题。"
            "用 poolctl usage 看这条请求在网关侧是否最终成功（status=ok、latencyMs 很大），"
            "若确实成功则可忽略",
        )
    except httpx.HTTPError as exc:
        return Check("密钥可用性", FAIL, f"请求失败：{exc}")

    if response.status_code == 401:
        return Check("密钥可用性", FAIL, "401：密钥无效或已吊销", "重新签发密钥")
    if response.status_code == 429:
        return Check(
            "密钥可用性",
            WARN,
            f"429：{response.text[:200]}",
            "配额已耗尽或全池无可用账号。检查 429 body 是否为 usage_limit_reached 契约",
        )
    if response.status_code >= 400:
        return Check("密钥可用性", FAIL, f"{response.status_code}: {response.text[:200]}")

    return Check("密钥可用性", OK, f"{response.status_code}，端到端通", data={"status": response.status_code})


def check_quota_contract(base_url: str, api_key: str, timeout: float = PROBE_TIMEOUT) -> Check:
    """校验超限响应契约。

    codex 对不同拒绝形式的重试放大差异极大：
      429 + usage_limit_reached -> 1 次请求
      402/403/401                -> 6 次
      5xx                        -> 最多 30 次
    只有第一种是可接受的。这里只在真的收到 429 时校验 body 形状。

    注意：探测必须发结构合法的请求。空 input 会让上游一直等不到
    response.create 而超时，既拿不到结论，又在网关日志里留下 error 记录。
    """
    if not api_key:
        return Check("超限契约", SKIP, "未提供密钥")

    root = base_url.rstrip("/")
    try:
        response = httpx.post(
            f"{root}/responses",
            json={
                "model": PROBE_MODEL,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
                "stream": False,
                "max_output_tokens": 16,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return Check("超限契约", SKIP, f"探测失败：{exc}")

    if response.status_code != 429:
        return Check(
            "超限契约",
            SKIP,
            f"当前未超限（{response.status_code}），无法验证 429 body",
        )

    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        return Check("超限契约", FAIL, "429 但 body 不是 JSON", "必须返回 usage_limit_reached 结构")

    if error.get("type") == "usage_limit_reached":
        return Check("超限契约", OK, "429 + usage_limit_reached，codex 只发 1 次请求，文案友好")

    # 实测（codex-cli 0.147.0 + codex-lb）：429 配 rate_limit_error 时，
    # codex 走 RetryLimit 分支——同样只发 1 次请求，不会放大，
    # 但员工看到的是 "exceeded retry limit, last status: 429"，
    # 会误以为是网络问题而不是额度耗尽。
    return Check(
        "超限契约",
        WARN,
        f"429 但 error.type = {error.get('type')!r}（非 usage_limit_reached）",
        "不影响安全：实测 codex 仍只发 1 次请求，无重试放大。"
        "但错误文案会退化成 “exceeded retry limit”，员工无法从中看出是额度用完，"
        "需在员工须知里说明，或让网关改用 usage_limit_reached",
    )


def check_rate_limit_headers(base_url: str, api_key: str, timeout: float = PROBE_TIMEOUT) -> Check:
    """软预警头：codex 会解析成 RateLimitSnapshot 并对外广播。"""
    if not api_key:
        return Check("额度预警头", SKIP, "未提供密钥")

    root = base_url.rstrip("/")
    try:
        response = httpx.post(
            f"{root}/responses",
            json={
                "model": PROBE_MODEL,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return Check("额度预警头", SKIP, f"探测失败：{exc}")

    found = {k: v for k, v in response.headers.items() if k.lower().startswith("x-codex-")}
    if found:
        return Check("额度预警头", OK, ", ".join(sorted(found)), data={"headers": found})
    return Check(
        "额度预警头",
        WARN,
        "响应无 x-codex-* 头",
        "非致命。带上 x-codex-primary-used-percent 等可让客户端显示剩余额度",
    )


def render(checks: list[Check]) -> tuple[str, bool]:
    """渲染报告，返回 (文本, 是否全部通过)。"""
    lines = [check.line() for check in checks]
    counts = {status: sum(1 for c in checks if c.status == status) for status in (OK, WARN, FAIL, SKIP)}
    lines.append("")
    lines.append(
        f"合计：{counts[OK]} 通过 / {counts[WARN]} 警告 / {counts[FAIL]} 失败 / {counts[SKIP]} 跳过"
    )
    return "\n".join(lines), counts[FAIL] == 0

"""向宿主 ~/.codex/config.toml 注入号池 provider。

为什么用文本 marker 块而不是 TOML 库改写：

1. multica daemon 会把宿主 config.toml **原样拷贝**进每个 per-task CODEX_HOME
   (server/internal/daemon/execenv/codex_home.go:22-30 定义 codexCopiedFiles，
    :226-238 syncCopiedFile)，并只往拷贝里 upsert 自己的 marker 块，不碰
   [model_providers.*] (server/pkg/agent/codex.go:683-712)。保持纯文本可预测
   地被拷贝，比依赖某个 TOML 库的序列化风格更安全。
2. 保留用户原有的注释与键顺序。TOML 库重写整个文件会打乱 [projects.*] 等既有内容。
3. 与 multica 自身的做法一致（它也用 `# BEGIN multica-managed` 文本块）。

TOML 顶层键陷阱：`model_provider = "gw"` 是顶层键，必须出现在任何 [table] 头之前，
否则会被解析成上一个 table 的成员。因此注入点固定为「第一个 table 头之前」——
TOML 本身保证所有顶层键都在第一个 table 之前，这个位置既安全又不改变既有语义。

写入后一律用 tomllib 重新解析校验，语义不符则回滚，不留半成品。
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BEGIN = "# BEGIN corp-codex-pool (managed; do not edit by hand)"
END = "# END corp-codex-pool"

# 行首的 [table] 或 [[array-of-table]] 头
_TABLE_RE = re.compile(r"^\s*\[\[?[^\[\]]", re.MULTILINE)


class ConfigInjectionError(RuntimeError):
    """注入失败，且原文件已恢复。"""


@dataclass
class ProviderSpec:
    """要注入的 provider 定义。

    字段名逐字对应 codex 的 ModelProviderInfo
    (codex-rs/model-provider-info/src/lib.rs:88-142)，不要臆造。
    """

    provider_id: str = "gw"
    name: str = "Company Codex Pool"
    base_url: str = "http://127.0.0.1:2455/v1"
    env_key: str = "GW_API_KEY"
    env_key_instructions: str = "由号池控制台下发，请勿手工设置"
    # wire_api 只接受 "responses"："chat" 已从 codex 移除
    # (codex-rs/model-provider-info/src/lib.rs:49, :71-81)
    wire_api: str = "responses"
    # header 名 -> 环境变量名。环境变量未设置或为空时该 header 自动省略
    # (codex-rs/model-provider-info/src/lib.rs:116-120)。
    # multica daemon 注入的 12 个 MULTICA_* 变量见 daemon.go:136-152。
    env_http_headers: dict[str, str] = field(
        default_factory=lambda: {
            "X-Multica-Task-Id": "MULTICA_TASK_ID",
            "X-Multica-Agent-Id": "MULTICA_AGENT_ID",
            "X-Multica-Workspace-Id": "MULTICA_WORKSPACE_ID",
        }
    )
    # 是否把该 provider 设为默认。false 时只注册 provider，不改变当前默认模型来源。
    set_default: bool = True

    def validate(self) -> None:
        if self.wire_api != "responses":
            raise ConfigInjectionError(
                f'wire_api 只能是 "responses"，收到 {self.wire_api!r}。'
                "codex 已移除 chat 兼容，见 model-provider-info/src/lib.rs:49"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.provider_id):
            raise ConfigInjectionError(
                f"provider_id 只允许字母数字下划线连字符，收到 {self.provider_id!r}"
            )
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.env_key):
            raise ConfigInjectionError(
                f"env_key 应为大写环境变量名，收到 {self.env_key!r}"
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigInjectionError(f"base_url 必须是 http(s) 地址，收到 {self.base_url!r}")


def _toml_str(value: str) -> str:
    """转义成 TOML 基本字符串字面量。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def render_block(spec: ProviderSpec) -> str:
    """生成 marker 包裹的 TOML 片段。"""
    spec.validate()
    pid = spec.provider_id
    lines = [BEGIN]

    if spec.set_default:
        # 顶层键：必须在任何 [table] 之前，故整块插在第一个 table 头前
        lines.append(f"model_provider = {_toml_str(pid)}")
        lines.append("")

    lines.append(f"[model_providers.{pid}]")
    lines.append(f"name = {_toml_str(spec.name)}")
    lines.append(f"base_url = {_toml_str(spec.base_url)}")
    lines.append(f"wire_api = {_toml_str(spec.wire_api)}")
    lines.append(f"env_key = {_toml_str(spec.env_key)}")
    if spec.env_key_instructions:
        lines.append(f"env_key_instructions = {_toml_str(spec.env_key_instructions)}")

    if spec.env_http_headers:
        lines.append("")
        lines.append(f"# 对账用：header 名 -> 环境变量名。变量为空时该 header 自动省略。")
        lines.append(f"[model_providers.{pid}.env_http_headers]")
        for header, env_var in spec.env_http_headers.items():
            lines.append(f"{_toml_str(header)} = {_toml_str(env_var)}")

    lines.append(END)
    return "\n".join(lines) + "\n"


def strip_block(text: str) -> str:
    """移除已存在的托管块。找不到则原样返回。"""
    pattern = re.compile(
        re.escape(BEGIN) + r".*?" + re.escape(END) + r"[^\S\n]*\n?",
        re.DOTALL,
    )
    return pattern.sub("", text)


def _insert_position(text: str) -> int:
    """返回应插入的字符下标：第一个 table 头之前，或文末。"""
    match = _TABLE_RE.search(text)
    return match.start() if match else len(text)


def build_new_text(original: str, spec: ProviderSpec) -> str:
    """算出注入后的完整文件内容（纯函数，便于测试）。"""
    body = strip_block(original)
    block = render_block(spec)
    pos = _insert_position(body)

    head, tail = body[:pos], body[pos:]

    # 规范化插入点周围的空白，否则反复注入会逐次累积空行而破坏幂等。
    # 先把两侧的空白吃干净，再补回固定的分隔，使结果只取决于内容本身。
    head = head.rstrip("\n")
    if head:
        head += "\n\n"

    tail = tail.lstrip("\n")
    if tail:
        block = block.rstrip("\n") + "\n\n"

    return head + block + tail


def verify(text: str, spec: ProviderSpec) -> dict:
    """解析并校验注入结果的 TOML 语义。抛 ConfigInjectionError 表示不合格。"""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInjectionError(f"注入后 TOML 解析失败：{exc}") from exc

    providers = data.get("model_providers")
    if not isinstance(providers, dict) or spec.provider_id not in providers:
        raise ConfigInjectionError(
            f"注入后找不到 [model_providers.{spec.provider_id}]"
        )

    provider = providers[spec.provider_id]
    for key, expected in (
        ("base_url", spec.base_url),
        ("wire_api", spec.wire_api),
        ("env_key", spec.env_key),
    ):
        if provider.get(key) != expected:
            raise ConfigInjectionError(
                f"model_providers.{spec.provider_id}.{key} 期望 {expected!r}，"
                f"实际 {provider.get(key)!r}"
            )

    if spec.set_default and data.get("model_provider") != spec.provider_id:
        # 顶层键被 table 吞掉时正是这里报错
        raise ConfigInjectionError(
            f"顶层 model_provider 期望 {spec.provider_id!r}，"
            f"实际 {data.get('model_provider')!r}。"
            "通常意味着顶层键被写到了某个 [table] 之后。"
        )

    if spec.env_http_headers:
        got = provider.get("env_http_headers") or {}
        missing = set(spec.env_http_headers) - set(got)
        if missing:
            raise ConfigInjectionError(f"env_http_headers 缺少：{sorted(missing)}")

    return data


def default_config_path() -> Path:
    """codex 配置路径。CODEX_HOME 优先，否则 ~/.codex。"""
    home = os.environ.get("CODEX_HOME")
    base = Path(home) if home else Path.home() / ".codex"
    return base / "config.toml"


@dataclass
class InjectResult:
    path: Path
    changed: bool
    backup: Path | None
    before: str
    after: str


def inject(
    spec: ProviderSpec,
    path: Path | None = None,
    *,
    dry_run: bool = False,
    backup: bool = True,
) -> InjectResult:
    """把 provider 注入配置文件。幂等：重复执行内容不变。

    写入采用「临时文件 + os.replace」，任何一步失败都不会留下半成品；
    校验不通过时从备份恢复。
    """
    spec.validate()
    path = path or default_config_path()
    original = path.read_text(encoding="utf-8") if path.exists() else ""

    new_text = build_new_text(original, spec)
    verify(new_text, spec)  # 先校验再落盘

    if new_text == original:
        return InjectResult(path, False, None, original, new_text)

    if dry_run:
        return InjectResult(path, True, None, original, new_text)

    path.parent.mkdir(parents=True, exist_ok=True)

    backup_path = None
    if backup and path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_suffix(f".toml.bak-{stamp}")
        shutil.copy2(path, backup_path)

    tmp = path.with_suffix(".toml.tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        # 落盘前再解析一次实际写出的字节，防止编码问题
        verify(tmp.read_text(encoding="utf-8"), spec)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        if backup_path and backup_path.exists():
            shutil.copy2(backup_path, path)
        raise

    return InjectResult(path, True, backup_path, original, new_text)


def remove(path: Path | None = None, *, dry_run: bool = False) -> InjectResult:
    """移除托管块，回到注入前状态。"""
    path = path or default_config_path()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = strip_block(original)

    if new_text != original:
        try:
            tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigInjectionError(f"移除后 TOML 非法：{exc}") from exc

    if new_text == original or dry_run:
        return InjectResult(path, new_text != original, None, original, new_text)

    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return InjectResult(path, True, None, original, new_text)
